"""Ad-hoc script to pull historical stock + EOD option bars from Massive.com.

Fetches full date ranges in bulk (`/v2/aggs/ticker/.../range/1/day/{from}/{to}`) so
6 weeks of daily bars take minutes instead of hours even on the 5 req/min Basic plan.
Supports 1..N API keys via a shared `KeyPool` worker queue, deterministic workload
sharding across processes, right/expiry/entry-band scoping, and incremental bar cache
extension. Writes 27-column canonical CSV snapshots to `data/snapshots/{TICKER}/` and
updates `data/metadata/snapshot_index.json` (`spot_source="LAST"`) without clobbering
`data/latest/{TICKER}_latest.csv`.

Usage:
    $env:MASSIVE_API_KEYS = "key1,key2,key3"
    python scripts/fetch_massive_options.py --ticker NVDA --start-date 2026-10-01 `
        --end-date 2026-11-12
    python scripts/fetch_massive_options.py --ticker NVDA --date 2026-10-15 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import errno
import json
import os
import queue
import re
import sys
import threading
import time
import zlib
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

COLUMNS: tuple[str, ...] = (
    "snapshot_id",
    "snapshot_ts_utc",
    "market_date",
    "market_data_type",
    "underlying",
    "underlying_conid",
    "underlying_price",
    "option_conid",
    "expiry",
    "dte",
    "strike",
    "moneyness_pct",
    "right",
    "bid",
    "ask",
    "mid",
    "last",
    "close",
    "mark_price",
    "volume",
    "open_interest",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "greeks_source",
)

US_EASTERN = ZoneInfo("America/New_York")
DEFAULT_BASE_URL = "https://api.massive.com"
_RETRY_BACKOFF_SEC = 62.0
_CACHE_FLUSH_INTERVAL = 10
_LOG_LOCK = threading.Lock()


def _log(msg: str, *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    with _LOG_LOCK:
        print(msg, file=stream, flush=True)


def _stable_conid(key: str, *, offset: int = 1) -> int:
    return offset + int(zlib.crc32(key.encode("utf-8")) % 900_000_000)


def _mask_key(api_key: str) -> str:
    clean = api_key.strip()
    if len(clean) <= 4:
        return "***"
    return f"...{clean[-4:]}"


def _split_key_tokens(raw: str) -> list[str]:
    return [tok.strip() for tok in re.split(r"[,;\s]+", raw) if tok.strip()]


def _resolve_api_keys(
    cli_key_items: Sequence[str] | None = None,
    cli_keys_csv: str | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Collect, split, and deduplicate API keys from CLI flags or env vars."""
    source_env = os.environ if env is None else env
    candidates: list[str] = []
    for item in cli_key_items or ():
        candidates.extend(_split_key_tokens(item))
    if cli_keys_csv:
        candidates.extend(_split_key_tokens(cli_keys_csv))
    if not candidates:
        for var_name in ("MASSIVE_API_KEYS", "MASSIVE_API_KEY", "POLYGON_API_KEY"):
            raw_val = source_env.get(var_name, "")
            if raw_val:
                candidates.extend(_split_key_tokens(raw_val))
                if var_name == "MASSIVE_API_KEYS":
                    break
    seen: set[str] = set()
    unique: list[str] = []
    for key in candidates:
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


@dataclass
class KeySlot:
    """Rate-limit and health state for one API key."""

    index: int
    api_key: str
    label: str
    next_available_at: float = 0.0
    cooldown_until: float = 0.0
    in_flight: bool = False
    disabled_reason: str | None = None
    requests_made: int = 0
    rate_limits_hit: int = 0

    def ready_at(self) -> float:
        return max(self.next_available_at, self.cooldown_until)


class KeyPool:
    """Thread-safe multi-key scheduler enforcing per-key pacing and 429 cooldowns."""

    def __init__(
        self,
        api_keys: Sequence[str],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        cleaned = [k.strip() for k in api_keys if k and k.strip()]
        if not cleaned:
            raise ValueError("KeyPool requires at least one non-empty API key.")
        self._clock = clock
        self._slots: list[KeySlot] = [
            KeySlot(index=i, api_key=key, label=f"key#{i} ({_mask_key(key)})")
            for i, key in enumerate(cleaned, 1)
        ]
        self._cond = threading.Condition()
        self._stop_event = threading.Event()

    @property
    def slots(self) -> tuple[KeySlot, ...]:
        return tuple(self._slots)

    def size(self) -> int:
        return len(self._slots)

    def active_count(self) -> int:
        with self._cond:
            return sum(1 for s in self._slots if s.disabled_reason is None)

    def stop(self) -> None:
        self._stop_event.set()
        with self._cond:
            self._cond.notify_all()

    def is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def acquire(self) -> KeySlot:
        """Wait for and lease the earliest-ready active key slot."""
        with self._cond:
            while True:
                if self._stop_event.is_set():
                    raise InterruptedError("KeyPool stopped.")
                active = [s for s in self._slots if s.disabled_reason is None]
                if not active:
                    reasons = "; ".join(f"{s.label}: {s.disabled_reason}" for s in self._slots)
                    raise RuntimeError(f"All API keys are disabled ({reasons}).")
                free = [s for s in active if not s.in_flight]
                if free:
                    now = self._clock()
                    best = min(free, key=lambda s: (s.ready_at(), s.requests_made, s.index))
                    wait_sec = best.ready_at() - now
                    if wait_sec <= 0:
                        best.in_flight = True
                        best.requests_made += 1
                        return best
                    self._cond.wait(timeout=min(wait_sec, 0.5))
                else:
                    self._cond.wait(timeout=0.25)

    def release(
        self,
        slot: KeySlot,
        *,
        delay_sec: float = 0.0,
        cooldown_sec: float = 0.0,
        disabled_reason: str | None = None,
    ) -> None:
        """Return a key slot and record pacing delay, 429 cooldown, or fatal disable."""
        with self._cond:
            now = self._clock()
            slot.in_flight = False
            if disabled_reason is not None:
                slot.disabled_reason = disabled_reason
            elif cooldown_sec > 0:
                slot.cooldown_until = max(slot.cooldown_until, now + cooldown_sec)
                slot.rate_limits_hit += 1
            elif delay_sec > 0:
                slot.next_available_at = max(slot.next_available_at, now + delay_sec)
            self._cond.notify_all()

    def summary(self) -> str:
        with self._cond:
            parts: list[str] = []
            for s in self._slots:
                status = f", DISABLED ({s.disabled_reason})" if s.disabled_reason else ""
                parts.append(f"{s.label}: {s.requests_made} req, {s.rate_limits_hit}x429{status}")
            return " | ".join(parts)


def _format_403_error(url: str, err_msg: str) -> str:
    return (
        f"\n[HTTP 403 NOT_AUTHORIZED] URL: {url}\nResponse: {err_msg}\n\n"
        "Check on Massive.com / Polygon.io:\n"
        "  1. Subscriptions are separate per asset class: Stocks vs Options.\n"
        "     Make sure BOTH 'Options Basic' AND 'Stocks Basic' ($0/mo) are Subscribed\n"
        "     on the Pricing / Subscriptions page (https://massive.com/pricing).\n"
        "  2. Check that your requested dates fall within your plan's 2-year window.\n"
        "  3. Or pass `--spot <price>` to skip the stock endpoint (single-day mode)."
    )


def _api_get_once(
    url: str,
    api_key: str,
    *,
    key_label: str,
    debug: bool = False,
) -> dict[str, Any]:
    if debug:
        _log(f"    [DEBUG {key_label}] GET {url}", err=True)
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "FOMO-Sapiens/1.0",
        },
    )
    with urlopen(req, timeout=30) as resp:
        payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        if debug:
            res = payload.get("results")
            res_len = len(res) if isinstance(res, list) else ("dict" if res else 0)
            _log(
                f"    [DEBUG {key_label}] -> status={payload.get('status')} "
                f"results={res_len} next_url={bool(payload.get('next_url'))}",
                err=True,
            )
        return payload


def _api_get(
    base_url: str,
    path: str,
    params: dict[str, str],
    api_key: str | KeyPool,
    *,
    delay_sec: float,
    debug: bool = False,
) -> dict[str, Any]:
    pool = api_key if isinstance(api_key, KeyPool) else KeyPool([api_key])
    query = urlencode(params)
    url = f"{base_url.rstrip('/')}{path}?{query}" if query else f"{base_url.rstrip('/')}{path}"
    max_attempts = max(4, pool.size() * 4)
    last_403: RuntimeError | None = None

    for attempt in range(1, max_attempts + 1):
        slot = pool.acquire()
        try:
            payload = _api_get_once(url, slot.api_key, key_label=slot.label, debug=debug)
            pool.release(slot, delay_sec=delay_sec)
            return payload
        except HTTPError as exc:
            err_msg = exc.read().decode("utf-8", errors="ignore")
            if exc.code == 429:
                retry_hdr = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    backoff = max(float(retry_hdr), 1.0) if retry_hdr else _RETRY_BACKOFF_SEC
                except ValueError:
                    backoff = _RETRY_BACKOFF_SEC
                pool.release(slot, cooldown_sec=backoff)
                _log(
                    f"\n[WARN] Rate limit HTTP 429 on {slot.label} "
                    f"(cooldown {backoff:.0f}s, attempt {attempt}/{max_attempts}); "
                    "using next available key...",
                    err=True,
                )
                continue
            if exc.code in {401, 403}:
                pool.release(slot, disabled_reason=f"HTTP {exc.code}")
                last_403 = RuntimeError(
                    _format_403_error(url, err_msg)
                    if exc.code == 403
                    else f"HTTP 401 Unauthorized on {slot.label} for {url}: {err_msg}"
                )
                if pool.active_count() > 0:
                    _log(
                        f"\n[WARN] {slot.label} returned HTTP {exc.code}; "
                        "disabling key and failing over...",
                        err=True,
                    )
                    continue
                raise last_403 from exc
            pool.release(slot, delay_sec=delay_sec)
            raise RuntimeError(f"HTTP {exc.code} for {url}: {err_msg}") from exc
        except (URLError, TimeoutError) as exc:
            pool.release(slot, delay_sec=max(delay_sec, 1.0))
            if attempt >= max_attempts:
                raise RuntimeError(f"Network error accessing {url}: {exc}") from exc
            _log(
                f"\n[WARN] Network error on {slot.label} (attempt {attempt}/{max_attempts}): {exc}",
                err=True,
            )

    if last_403 is not None:
        raise last_403
    raise RuntimeError(f"Exhausted {max_attempts} API retry attempts for {url}.")


def fetch_stock_daily_bars(
    base_url: str,
    ticker: str,
    start_date: str,
    end_date: str,
    api_key: str | KeyPool,
    *,
    delay_sec: float,
    debug: bool,
) -> dict[str, float]:
    """Fetch `{YYYY-MM-DD: close}` for the stock in ONE range API call."""
    path = f"/v2/aggs/ticker/{ticker}/range/1/day/{start_date}/{end_date}"
    data = _api_get(
        base_url,
        path,
        {"adjusted": "true", "sort": "asc", "limit": "5000"},
        api_key,
        delay_sec=delay_sec,
        debug=debug,
    )
    bars: dict[str, float] = {}
    for item in data.get("results") or []:
        ts_ms = item.get("t")
        close_px = item.get("c")
        if ts_ms is None or close_px is None:
            continue
        day_et = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=UTC).astimezone(US_EASTERN).date()
        bars[day_et.isoformat()] = float(close_px)
    return bars


def _fetch_contracts_slice(
    base_url: str,
    ticker: str,
    *,
    expired: bool,
    min_strike: float,
    max_strike: float,
    min_exp: str,
    max_exp: str,
    api_key: str | KeyPool,
    delay_sec: float,
    debug: bool,
) -> list[dict[str, Any]]:
    path = "/v3/reference/options/contracts"
    params: dict[str, str] = {
        "underlying_ticker": ticker,
        "expired": "true" if expired else "false",
        "strike_price.gte": f"{min_strike:.2f}",
        "strike_price.lte": f"{max_strike:.2f}",
        "expiration_date.gte": min_exp,
        "expiration_date.lte": max_exp,
        "limit": "1000",
        "sort": "expiration_date",
        "order": "asc",
    }
    found: list[dict[str, Any]] = []
    while True:
        data = _api_get(base_url, path, params, api_key, delay_sec=delay_sec, debug=debug)
        found.extend(data.get("results") or [])
        next_url = data.get("next_url")
        if not next_url:
            break
        parsed = str(next_url)
        path = parsed.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path.split("?", 1)[0]
        q_str = parsed.split("?", 1)[1] if "?" in parsed else ""
        params = {k: v[0] for k, v in parse_qs(q_str).items() if k != "apiKey" and v}
    return found


def fetch_contracts_for_window(
    base_url: str,
    ticker: str,
    start_date: str,
    end_date: str,
    min_strike: float,
    max_strike: float,
    min_dte: int,
    max_dte: int,
    api_key: str | KeyPool,
    *,
    delay_sec: float,
    debug: bool,
) -> list[dict[str, Any]]:
    """Fetch BOTH active (`expired=false`) and expired (`expired=true`) contracts without `as_of`.

    Note: Massive evaluates `expired` relative to `as_of` when `as_of` is supplied, so passing
    `as_of=start_date` + `expired=true` contradicts `expiration_date >= start_date + min_dte`
    and returns empty results. Querying without `as_of` across `expired=false` and `expired=true`
    relies cleanly on today's clock and captures all expirations across `[min_exp, max_exp]`.
    """
    start_dt = date.fromisoformat(start_date)
    end_dt = date.fromisoformat(end_date)
    min_exp = (start_dt + timedelta(days=min_dte)).isoformat()
    max_exp = (end_dt + timedelta(days=max_dte)).isoformat()

    by_ticker: dict[str, dict[str, Any]] = {}
    for expired_flag in (False, True):
        batch = _fetch_contracts_slice(
            base_url,
            ticker,
            expired=expired_flag,
            min_strike=min_strike,
            max_strike=max_strike,
            min_exp=min_exp,
            max_exp=max_exp,
            api_key=api_key,
            delay_sec=delay_sec,
            debug=debug,
        )
        for item in batch:
            c_sym = item.get("ticker")
            if isinstance(c_sym, str) and c_sym:
                by_ticker[c_sym] = item
    return sorted(
        by_ticker.values(),
        key=lambda c: (
            str(c.get("expiration_date", "")),
            float(c.get("strike_price", 0.0)),
            str(c.get("contract_type", "")),
        ),
    )


def _is_monthly_third_friday(exp_str: str) -> bool:
    d = date.fromisoformat(exp_str)
    return d.weekday() == 4 and 15 <= d.day <= 21


def _matches_strike_step(strike: float, step: float) -> bool:
    if step <= 0:
        return True
    rem = strike % step
    return rem < 0.01 or abs(rem - step) < 0.01


def _is_standard_equity_option(contract: dict[str, Any], underlying: str) -> bool:
    """Return True only for standard 100-share OCC contracts matching `O:{TICKER}YYMMDD...`.

    Excludes binary/fixed-return roots (e.g. `O:B1VDA...`), adjusted roots (`O:NVDA1...`),
    and mini/non-standard deliverables (`shares_per_contract != 100`).
    """
    ticker_sym = str(contract.get("ticker") or "")
    pattern = rf"^O:{re.escape(underlying.upper())}\d{{6}}[CP]\d{{8}}$"
    if not re.match(pattern, ticker_sym):
        return False
    shares = contract.get("shares_per_contract")
    return shares is None or int(shares) == 100


def _contract_key(expiry: str, strike: float, right: str) -> tuple[str, float, str]:
    """Canonical deduplication key `(expiry, strike, right)` within a daily snapshot CSV."""
    return (expiry, round(float(strike), 4), right.upper())


def _in_any_daily_band(
    strike: float,
    expiry: str,
    daily_bands: dict[str, tuple[float, float]],
) -> bool:
    """Return True if `strike` falls inside `spot[d] +- range_pct` for any active day `d <= exp`."""
    for day_str, (lo, hi) in daily_bands.items():
        if day_str <= expiry and lo <= strike <= hi:
            return True
    return False


def fetch_option_daily_bars(
    base_url: str,
    contract_ticker: str,
    start_date: str,
    end_date: str,
    api_key: str | KeyPool,
    *,
    delay_sec: float,
    debug: bool,
) -> dict[str, tuple[float, int]]:
    """Fetch `{YYYY-MM-DD: (close, volume)}` for one option across `[start_date, end_date]`."""
    path = f"/v2/aggs/ticker/{contract_ticker}/range/1/day/{start_date}/{end_date}"
    data = _api_get(
        base_url,
        path,
        {"adjusted": "true", "sort": "asc", "limit": "5000"},
        api_key,
        delay_sec=delay_sec,
        debug=debug,
    )
    out: dict[str, tuple[float, int]] = {}
    for item in data.get("results") or []:
        ts_ms = item.get("t")
        close_px = item.get("c")
        if ts_ms is None or close_px is None or float(close_px) <= 0:
            continue
        day_et = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=UTC).astimezone(US_EASTERN).date()
        vol = int(item.get("v") or 0)
        out[day_et.isoformat()] = (float(close_px), vol)
    return out


def _snapshot_timestamps(day_str: str) -> tuple[str, str]:
    """Return `(YYYYMMDD_HHMMSS, ISO_UTC)` for 16:00 ET close on `day_str`."""
    d = date.fromisoformat(day_str)
    close_utc = datetime(d.year, d.month, d.day, 16, 0, tzinfo=US_EASTERN).astimezone(UTC)
    return close_utc.strftime("%Y%m%d_%H%M%S"), close_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _tmp_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def _is_transient_replace_error(exc: OSError) -> bool:
    winerr = getattr(exc, "winerror", None)
    return (
        isinstance(exc, PermissionError)
        or winerr in {5, 32}
        or exc.errno
        in {
            errno.EACCES,
            errno.EBUSY,
            errno.EPERM,
        }
    )


def _atomic_replace(
    src: Path,
    dst: Path,
    *,
    max_attempts: int = 8,
    initial_delay_sec: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Replace `dst` with `src`, retrying transient Windows AV/indexer handle locks."""
    for attempt in range(max_attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if not _is_transient_replace_error(exc) or attempt + 1 >= max_attempts:
                src.unlink(missing_ok=True)
                raise
            delay = min(initial_delay_sec * (2**attempt), 0.5)
            sleep(delay)


def _atomic_write_text(target: Path, content: str) -> None:
    tmp = _tmp_path(target)
    try:
        tmp.write_text(content, encoding="utf-8")
        _atomic_replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


@contextmanager
def _file_lock(lock_path: Path, *, timeout_sec: float = 60.0) -> Iterator[None]:
    """Cross-process advisory file lock for metadata JSON / snapshot CSV writes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as fh:
        if sys.platform == "win32":
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
            deadline = time.monotonic() + timeout_sec
            while True:
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for lock {lock_path}") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _update_snapshot_index(
    output_base: Path,
    ticker: str,
    snapshot_id: str,
    out_file: Path,
    snapshot_ts_utc: str,
    market_date: str,
    spot_price: float,
    rows: list[dict[str, Any]],
    *,
    range_pct: float,
    strike_step: float,
) -> None:
    """Register snapshot metadata with `spot_source='LAST'` without touching `latest/`."""
    data_root = output_base.parent if output_base.name == "snapshots" else output_base
    index_path = data_root / "metadata" / "snapshot_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(index_path.with_name(f".{index_path.name}.lock")):
        if index_path.is_file():
            index: dict[str, Any] = json.loads(index_path.read_text(encoding="utf-8"))
        else:
            index = {
                "updated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tickers": {},
            }

        tickers_map: dict[str, Any] = index.setdefault("tickers", {})
        entry: dict[str, Any] = tickers_map.get(
            ticker,
            {
                "latest_snapshot_id": snapshot_id,
                "latest_file": f"data/latest/{ticker}_latest.csv",
                "snapshots": [],
            },
        )
        existing = [s for s in entry.get("snapshots", []) if s.get("snapshot_id") != snapshot_id]
        try:
            rel_path = out_file.relative_to(data_root.parent).as_posix()
        except ValueError:
            rel_path = out_file.as_posix()

        meta: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "file_path": rel_path,
            "snapshot_ts_utc": snapshot_ts_utc,
            "market_date": market_date,
            "market_data_type": 3,
            "underlying_price": round(spot_price, 4),
            "contract_count": len(rows),
            "expirations": sorted({str(r["expiry"]) for r in rows}),
            "range_pct": range_pct,
            "strike_step": strike_step,
            "requested_strike_step": strike_step,
            "spot_source": "LAST",
            "quoted_contract_count": 0,
            "quote_coverage": 0.0,
            "prior_close_only_count": 0,
        }
        ordered = sorted([*existing, meta], key=lambda s: str(s["snapshot_id"]), reverse=True)
        entry["latest_snapshot_id"] = ordered[0]["snapshot_id"]
        entry["snapshots"] = ordered
        tickers_map[ticker] = entry
        index["updated_at_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        _atomic_write_text(index_path, json.dumps(index, indent=2))


def _cache_path_for(output_base: Path, ticker: str) -> Path:
    data_root = output_base.parent if output_base.name == "snapshots" else output_base
    return data_root / "metadata" / f".massive_cache_{ticker}.json"


def _load_bar_cache(cache_path: Path) -> dict[str, dict[str, Any]]:
    """Load `{contract_ticker: {from, to, bars}}` from disk, returning `{}` if absent/corrupt."""
    if not cache_path.is_file():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        contracts = payload.get("contracts")
        return dict(contracts) if isinstance(contracts, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_bar_cache(
    cache_path: Path,
    ticker: str,
    contracts: dict[str, dict[str, Any]],
    *,
    updated_keys: set[str] | None = None,
) -> None:
    """Atomically persist downloaded contract bars, merging safely under a file lock."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(cache_path.with_name(f".{cache_path.name}.lock")):
        if updated_keys is not None and cache_path.is_file():
            merged = _load_bar_cache(cache_path)
            for k in updated_keys:
                if k in contracts:
                    merged[k] = contracts[k]
        else:
            merged = contracts
        payload = {"version": 1, "ticker": ticker, "contracts": merged}
        _atomic_write_text(cache_path, json.dumps(payload, separators=(",", ":")))


def _bars_from_cache_entry(
    entry: dict[str, Any] | None, start_date: str, end_date: str
) -> dict[str, tuple[float, int]] | None:
    """Return cached `{day: (close, vol)}` if cached range covers `[start_date, end_date]`."""
    cached, fetch_from = _plan_cache_fetch(entry, start_date, end_date, use_cache=True)
    return cached if fetch_from is None else None


def _plan_cache_fetch(
    entry: dict[str, Any] | None,
    start_date: str,
    end_date: str,
    *,
    use_cache: bool,
) -> tuple[dict[str, tuple[float, int]], str | None]:
    """Return `(existing_bars, fetch_start_date_or_None)` supporting incremental tail extension."""
    if not use_cache or not entry:
        return {}, start_date
    cached_from = str(entry.get("from") or "")
    cached_to = str(entry.get("to") or "")
    raw_bars = entry.get("bars")
    if not cached_from or not cached_to or not isinstance(raw_bars, dict):
        return {}, start_date
    parsed = {str(d): (float(pair[0]), int(pair[1])) for d, pair in raw_bars.items()}
    if cached_from <= start_date and cached_to >= end_date:
        return parsed, None
    if cached_from <= start_date <= cached_to < end_date:
        delta_start = (date.fromisoformat(cached_to) + timedelta(days=1)).isoformat()
        if delta_start <= end_date:
            return parsed, delta_start
        return parsed, None
    return parsed, start_date


def _build_day_row(
    *,
    ticker: str,
    u_conid: int,
    opt_conid: int,
    day_str: str,
    spot_px: float,
    expiry: str,
    dte: int,
    strike: float,
    right: str,
    close_px: float,
    volume: int,
) -> dict[str, Any]:
    ts_compact, ts_utc = _snapshot_timestamps(day_str)
    snapshot_id = f"{ticker}_{ts_compact}"
    moneyness = ((strike / spot_px) - 1.0) * 100.0
    return {
        "snapshot_id": snapshot_id,
        "snapshot_ts_utc": ts_utc,
        "market_date": day_str,
        "market_data_type": 3,
        "underlying": ticker,
        "underlying_conid": u_conid,
        "underlying_price": round(spot_px, 4),
        "option_conid": opt_conid,
        "expiry": expiry,
        "dte": dte,
        "strike": round(strike, 4),
        "moneyness_pct": round(moneyness, 4),
        "right": right,
        "bid": "",
        "ask": "",
        "mid": round(close_px, 4),
        "last": round(close_px, 4),
        "close": round(close_px, 4),
        "mark_price": round(close_px, 4),
        "volume": volume,
        "open_interest": "",
        "iv": "",
        "delta": "",
        "gamma": "",
        "theta": "",
        "vega": "",
        "greeks_source": "NONE",
    }


_LIVE_QUOTE_COLS: tuple[str, ...] = (
    "bid",
    "ask",
    "open_interest",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
)


def _read_existing_day_rows(csv_path: Path) -> dict[tuple[str, float, str], dict[str, Any]]:
    """Read an existing snapshot CSV into `{contract_key: row}` with zero duplicates."""
    by_key: dict[tuple[str, float, str], dict[str, Any]] = {}
    if not csv_path.is_file():
        return by_key
    try:
        with csv_path.open("r", encoding="utf-8", newline="") as fh:
            for raw in csv.DictReader(fh):
                expiry = str(raw.get("expiry") or "")
                strike_raw = raw.get("strike")
                right = str(raw.get("right") or "").upper()
                if not expiry or not strike_raw or right not in {"C", "P"}:
                    continue
                key = _contract_key(expiry, float(strike_raw), right)
                row: dict[str, Any] = {col: raw.get(col, "") for col in COLUMNS}
                prev = by_key.get(key)
                if prev is None or (prev.get("greeks_source") in {"", "NONE"} and row.get("bid")):
                    by_key[key] = row
    except (OSError, ValueError):
        return {}
    return by_key


def _merge_row(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """Upsert `incoming` onto `existing`, preserving non-empty live quote/Greeks fields."""
    if existing is None:
        return dict(incoming)
    merged = dict(incoming)
    for col in _LIVE_QUOTE_COLS:
        if str(merged.get(col, "")) == "" and str(existing.get(col, "")) != "":
            merged[col] = existing[col]
    prev_gs = str(existing.get("greeks_source") or "")
    if prev_gs not in {"", "NONE"} and str(merged.get("greeks_source") or "") in {"", "NONE"}:
        merged["greeks_source"] = prev_gs
        if str(existing.get("market_data_type") or "") != "":
            merged["market_data_type"] = existing["market_data_type"]
    return merged


def _rows_differ(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return any(str(a.get(col, "")) != str(b.get(col, "")) for col in COLUMNS)


def _write_day_snapshot(
    out_base: Path,
    ticker_dir: Path,
    ticker: str,
    day_str: str,
    spot_px: float,
    rows: list[dict[str, Any]],
    *,
    overwrite: bool,
    range_pct: float,
    strike_step: float,
    existing_map: dict[tuple[str, float, str], dict[str, Any]] | None = None,
) -> tuple[Path | None, int, int]:
    """Atomically write or merge one day CSV and update `snapshot_index.json`.

    Returns `(out_file_or_None, appended_count, updated_count)`.
    """
    ts_compact, ts_utc = _snapshot_timestamps(day_str)
    snapshot_id = f"{ticker}_{ts_compact}"
    out_file = ticker_dir / f"{snapshot_id}.csv"

    with _file_lock(out_file.with_name(f".{out_file.name}.lock")):
        base_map: dict[tuple[str, float, str], dict[str, Any]] = {}
        if not overwrite:
            if out_file.exists():
                base_map = _read_existing_day_rows(out_file)
            elif existing_map is not None:
                base_map = dict(existing_map)

        appended = 0
        updated = 0
        for row in rows:
            key = _contract_key(str(row["expiry"]), float(row["strike"]), str(row["right"]))
            prev = base_map.get(key)
            merged = _merge_row(prev, row)
            if prev is None:
                appended += 1
                base_map[key] = merged
            elif _rows_differ(prev, merged):
                updated += 1
                base_map[key] = merged

        if not base_map or (out_file.exists() and not overwrite and appended == 0 and updated == 0):
            return None, 0, 0

        rows_sorted = sorted(
            base_map.values(),
            key=lambda r: (str(r["expiry"]), float(r["strike"]), str(r["right"])),
        )
        tmp = _tmp_path(out_file)
        try:
            with tmp.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=COLUMNS)
                writer.writeheader()
                writer.writerows(rows_sorted)
            _atomic_replace(tmp, out_file)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    _update_snapshot_index(
        out_base,
        ticker,
        snapshot_id,
        out_file,
        ts_utc,
        day_str,
        spot_px,
        rows_sorted,
        range_pct=range_pct,
        strike_step=strike_step,
    )
    return out_file, appended, updated


def _parse_expiry_filter(raw_expiries: Sequence[str] | None) -> set[str]:
    selected: set[str] = set()
    for item in raw_expiries or ():
        for part in item.split(","):
            clean = part.strip()
            if clean:
                date.fromisoformat(clean)
                selected.add(clean)
    return selected


def _select_shard(
    contracts: list[dict[str, Any]],
    shard_index: int,
    shard_count: int,
) -> list[dict[str, Any]]:
    """Deterministically partition `contracts` into `shard_count` disjoint slices."""
    if shard_count < 1:
        raise ValueError("--shard-count must be >= 1.")
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError(f"--shard-index must be in [0, {shard_count - 1}].")
    if shard_count == 1:
        return list(contracts)
    return [c for idx, c in enumerate(contracts) if idx % shard_count == shard_index]


def _order_contracts_deterministically(contracts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort reverse by expiry (earliest flush first) with deterministic tie-breaking."""
    tie_sorted = sorted(
        contracts,
        key=lambda c: (
            float(c.get("strike_price", 0.0)),
            str(c.get("contract_type", "")),
            str(c.get("ticker", "")),
        ),
    )
    return sorted(
        tie_sorted,
        key=lambda c: str(c.get("expiration_date", "")),
        reverse=True,
    )


def _fetch_bars_with_pool(
    work_items: Sequence[tuple[dict[str, Any], dict[str, tuple[float, int]], str]],
    *,
    base_url: str,
    bars_end_date: str,
    key_pool: KeyPool,
    delay_sec: float,
    debug: bool,
    on_result: Callable[[dict[str, Any], dict[str, tuple[float, int]], bool], None],
) -> None:
    """Execute uncached/tail option bar fetches across `key_pool.size()` worker threads."""
    if not work_items:
        return
    work_q: queue.Queue[tuple[dict[str, Any], dict[str, tuple[float, int]], str]] = queue.Queue()
    for item in work_items:
        work_q.put(item)

    results_q: queue.Queue[tuple[dict[str, Any], dict[str, tuple[float, int]], bool]] = (
        queue.Queue()
    )
    num_workers = min(key_pool.size(), len(work_items))

    def _worker() -> None:
        while not key_pool.is_stopped():
            try:
                contract, cached_bars, fetch_from = work_q.get_nowait()
            except queue.Empty:
                return
            c_ticker = str(contract["ticker"])
            new_bars = fetch_option_daily_bars(
                base_url,
                c_ticker,
                fetch_from,
                bars_end_date,
                key_pool,
                delay_sec=delay_sec,
                debug=debug,
            )
            merged_bars = {**cached_bars, **new_bars} if cached_bars else new_bars
            results_q.put((contract, merged_bars, bool(cached_bars)))

    executor = ThreadPoolExecutor(max_workers=num_workers, thread_name_prefix="massive-key")
    futures: list[Future[None]] = []
    try:
        futures = [executor.submit(_worker) for _ in range(num_workers)]
        pending = len(work_items)
        while pending > 0:
            try:
                contract, merged_bars, was_tail = results_q.get(timeout=0.2)
            except queue.Empty:
                for fut in futures:
                    if fut.done():
                        exc = fut.exception()
                        if exc is not None:
                            key_pool.stop()
                            raise exc from None
                if all(f.done() for f in futures) and results_q.empty():
                    break
                continue
            on_result(contract, merged_bars, was_tail)
            pending -= 1
        # Surface any unexpected worker exceptions even if all expected items finished.
        wait(futures, return_when=FIRST_COMPLETED)
        for fut in futures:
            if fut.done():
                exc = fut.exception()
                if exc is not None:
                    raise exc
    except BaseException:
        key_pool.stop()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pull stock + EOD options range bars from Massive.com for backtesting."
    )
    parser.add_argument("--ticker", required=True, help="Underlying stock symbol (e.g. NVDA)")
    parser.add_argument("--date", default=None, help="Single session date YYYY-MM-DD")
    parser.add_argument("--start-date", default=None, help="Range start YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="Range end YYYY-MM-DD")
    parser.add_argument(
        "--api-key",
        action="append",
        default=None,
        help="Massive.com / Polygon.io API key (repeatable or comma-separated)",
    )
    parser.add_argument(
        "--api-keys",
        default=None,
        help="Comma/semicolon-separated Massive.com API keys (overrides MASSIVE_API_KEYS)",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base URL")
    parser.add_argument("--spot", type=float, default=None, help="Manual spot (single-day only)")
    parser.add_argument("--min-dte", type=int, default=25, help="Min DTE per session (default: 25)")
    parser.add_argument("--max-dte", type=int, default=60, help="Max DTE per session (default: 60)")
    parser.add_argument("--range-pct", type=float, default=0.20, help="Strike range +-pct (0.20)")
    parser.add_argument("--strike-step", type=float, default=5.0, help="Strike increment ($5.0)")
    parser.add_argument(
        "--right",
        choices=("both", "C", "P"),
        default="both",
        help="Option side filter: both, C (calls only), or P (puts only)",
    )
    parser.add_argument(
        "--expiry-cadence",
        choices=("monthly", "all"),
        default="monthly",
        help="'monthly' picks 3rd-Friday expiries (~12m for 6w); 'all' adds weeklies",
    )
    parser.add_argument(
        "--expiry",
        action="append",
        default=None,
        help="Target specific expiration date(s) YYYY-MM-DD (repeatable or comma-separated)",
    )
    parser.add_argument("--expiry-from", default=None, help="Min expiration date YYYY-MM-DD")
    parser.add_argument("--expiry-to", default=None, help="Max expiration date YYYY-MM-DD")
    parser.add_argument(
        "--entry-bands-only",
        action="store_true",
        help="Compute +-range-pct strike bands only from [start-date, end-date] spot prices",
    )
    parser.add_argument(
        "--shard-index",
        type=int,
        default=0,
        help="0-based worker shard index for multi-process splitting (default: 0)",
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=1,
        help="Total worker shard count for multi-process splitting (default: 1)",
    )
    parser.add_argument(
        "--delay-sec",
        type=float,
        default=12.5,
        help="Seconds between calls per API key (12.5s avoids 5 req/min Basic limit)",
    )
    parser.add_argument(
        "--bars-end-date",
        default=None,
        help="End date for daily prices (default: min(max_expiry, yesterday ET))",
    )
    parser.add_argument(
        "--no-until-expiry",
        action="store_true",
        help="Stop pulling daily bars at --end-date instead of following contracts to expiry",
    )
    parser.add_argument("--output-dir", default="data/snapshots", help="Snapshots output folder")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing day CSVs from scratch instead of merging",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Leave existing day CSVs untouched instead of merging new/updated strikes",
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Re-fetch & update prices even for contracts already present in existing day CSVs",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore local per-contract bar cache and re-fetch from Massive",
    )
    parser.add_argument("--dry-run", action="store_true", help="Check contracts without bars")
    parser.add_argument("--debug", action="store_true", help="Print raw API URLs and counts")
    return parser.parse_args()


def _resolve_bars_end_date(
    start_date: str,
    end_date: str,
    max_dte: int,
    *,
    bars_end_override: str | None,
    no_until_expiry: bool,
) -> str:
    if bars_end_override:
        return bars_end_override
    if no_until_expiry:
        return end_date
    yesterday_et = datetime.now(US_EASTERN).date() - timedelta(days=1)
    max_exp = date.fromisoformat(end_date) + timedelta(days=max_dte)
    resolved = min(max_exp, yesterday_et)
    if resolved < date.fromisoformat(start_date):
        return end_date
    return max(resolved, date.fromisoformat(end_date)).isoformat()


def main() -> None:
    args = _parse_args()
    api_keys = _resolve_api_keys(args.api_key, args.api_keys)
    if not api_keys:
        print(
            "[ERROR] Set MASSIVE_API_KEYS / MASSIVE_API_KEY or pass --api-key / --api-keys.",
            file=sys.stderr,
        )
        sys.exit(1)

    start_date = args.start_date or args.date
    end_date = args.end_date or args.date or args.start_date
    if not start_date or not end_date:
        print("[ERROR] Pass --date YYYY-MM-DD or --start-date + --end-date.", file=sys.stderr)
        sys.exit(1)

    try:
        expiry_filter = _parse_expiry_filter(args.expiry)
    except ValueError as exc:
        print(f"[ERROR] Invalid --expiry date: {exc}", file=sys.stderr)
        sys.exit(1)

    key_pool = KeyPool(api_keys)
    bars_end_date = _resolve_bars_end_date(
        start_date,
        end_date,
        int(args.max_dte),
        bars_end_override=args.bars_end_date,
        no_until_expiry=bool(args.no_until_expiry),
    )

    ticker = str(args.ticker).strip().upper()
    print(
        f"--> [1/4] Fetching {ticker} daily stock closes "
        f"[entry {start_date}..{end_date}, bars through {bars_end_date}] "
        f"using {key_pool.size()} API key(s)..."
    )
    if args.spot is not None and start_date == bars_end_date:
        spot_by_day = {start_date: float(args.spot)}
    else:
        spot_by_day = fetch_stock_daily_bars(
            args.base_url,
            ticker,
            start_date,
            bars_end_date,
            key_pool,
            delay_sec=0.2,
            debug=args.debug,
        )
    if not spot_by_day:
        print(
            f"[ERROR] Massive returned 0 trading days for {ticker} "
            f"in [{start_date} .. {bars_end_date}]."
        )
        sys.exit(1)

    band_spot_days = (
        {d: px for d, px in spot_by_day.items() if start_date <= d <= end_date}
        if args.entry_bands_only
        else spot_by_day
    )
    if not band_spot_days:
        band_spot_days = spot_by_day
    daily_bands: dict[str, tuple[float, float]] = {
        day_s: (px * (1.0 - float(args.range_pct)), px * (1.0 + float(args.range_pct)))
        for day_s, px in band_spot_days.items()
    }
    lo_spot = min(spot_by_day.values())
    hi_spot = max(spot_by_day.values())
    min_strike = min(lo for lo, _ in daily_bands.values())
    max_strike = max(hi for _, hi in daily_bands.values())
    print(
        f"    Loaded {len(spot_by_day)} trading days through {bars_end_date} | "
        f"Spot range: ${lo_spot:.2f} .. ${hi_spot:.2f}"
    )

    print(
        f"--> [2/4] Fetching {ticker} option contracts "
        f"(strikes ${min_strike:.1f}..${max_strike:.1f}, cadence={args.expiry_cadence}, "
        f"right={args.right})..."
    )
    raw_contracts = fetch_contracts_for_window(
        args.base_url,
        ticker,
        start_date,
        end_date,
        min_strike,
        max_strike,
        args.min_dte,
        args.max_dte,
        key_pool,
        delay_sec=0.2 if args.delay_sec < 1.0 else 1.0,
        debug=args.debug,
    )
    print(f"    Massive returned {len(raw_contracts)} raw contracts across active+expired slices.")

    standard_contracts = [c for c in raw_contracts if _is_standard_equity_option(c, ticker)]
    skipped_non_std = len(raw_contracts) - len(standard_contracts)
    if skipped_non_std:
        print(
            f"    Skipped {skipped_non_std} binary / non-standard contract(s) "
            "(non-OCC root or non-100 shares)"
        )

    want_right_type = {"C": "call", "P": "put"}.get(args.right)
    filtered = [
        c
        for c in standard_contracts
        if (want_right_type is None or c.get("contract_type") == want_right_type)
        and _matches_strike_step(float(c.get("strike_price", 0.0)), float(args.strike_step))
        and _in_any_daily_band(
            float(c.get("strike_price", 0.0)),
            str(c.get("expiration_date", "")),
            daily_bands,
        )
        and (
            args.expiry_cadence == "all"
            or _is_monthly_third_friday(str(c.get("expiration_date", "")))
        )
        and (not expiry_filter or str(c.get("expiration_date", "")) in expiry_filter)
        and (not args.expiry_from or str(c.get("expiration_date", "")) >= args.expiry_from)
        and (not args.expiry_to or str(c.get("expiration_date", "")) <= args.expiry_to)
    ]
    expirations = sorted({str(c["expiration_date"]) for c in filtered})
    print(
        f"    After ${args.strike_step:g} step, per-day +-{float(args.range_pct) * 100:g}% "
        f"& {args.expiry_cadence} filter: "
        f"{len(filtered)} contracts across {len(expirations)} expirations: {expirations}"
    )
    if not filtered:
        print("[WARN] 0 contracts matched filters. Re-run with --debug --expiry-cadence all.")
        sys.exit(0)

    out_base = Path(args.output_dir)
    ticker_dir = out_base / ticker
    target_days: dict[str, float] = {}
    existing_rows_by_day: dict[str, dict[tuple[str, float, str], dict[str, Any]]] = {}
    kept_days = 0
    for day_str, spot_px in sorted(spot_by_day.items()):
        ts_compact, _ = _snapshot_timestamps(day_str)
        out_file = ticker_dir / f"{ticker}_{ts_compact}.csv"
        if args.skip_existing and out_file.exists():
            kept_days += 1
            continue
        target_days[day_str] = spot_px
        if out_file.exists() and not args.overwrite:
            existing_rows_by_day[day_str] = _read_existing_day_rows(out_file)

    if not target_days:
        print(
            f"\n[DONE] Skipped all {kept_days} existing CSV(s) under {ticker_dir}/. "
            "Remove --skip-existing to merge updates."
        )
        return

    # Reverse expiry sort lets later dates finish first so full-day CSVs flush incrementally.
    ordered_contracts = _order_contracts_deterministically(filtered)
    try:
        sharded_contracts = _select_shard(
            ordered_contracts, int(args.shard_index), int(args.shard_count)
        )
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    if int(args.shard_count) > 1:
        print(
            f"    Shard {args.shard_index + 1}/{args.shard_count}: "
            f"{len(sharded_contracts)}/{len(ordered_contracts)} contract(s) assigned."
        )

    force_fetch = bool(args.overwrite or args.refresh_existing)
    pending_by_day: dict[str, set[str]] = {}
    for day_str in target_days:
        existing_keys = existing_rows_by_day.get(day_str, {})
        req: set[str] = set()
        for c in sharded_contracts:
            exp_s = str(c["expiration_date"])
            if exp_s < day_str:
                continue
            key = _contract_key(
                exp_s,
                float(c["strike_price"]),
                "C" if c.get("contract_type") == "call" else "P",
            )
            if force_fetch or key not in existing_keys:
                req.add(str(c["ticker"]))
        pending_by_day[day_str] = req

    needed_tickers = {t for req in pending_by_day.values() for t in req}
    contracts_to_process = [c for c in sharded_contracts if str(c["ticker"]) in needed_tickers]
    skipped_existing = len(sharded_contracts) - len(contracts_to_process)
    if skipped_existing:
        print(
            f"    Skipping {skipped_existing} contract(s) already present in every target day CSV."
        )

    cache_path = _cache_path_for(out_base, ticker)
    use_cache = not args.no_cache and not args.refresh_existing
    bar_cache = {} if args.no_cache else _load_bar_cache(cache_path)

    planned: list[tuple[dict[str, Any], dict[str, tuple[float, int]], str | None]] = []
    uncached_count = 0
    for contract in contracts_to_process:
        c_ticker = str(contract["ticker"])
        cached_bars, fetch_from = _plan_cache_fetch(
            bar_cache.get(c_ticker), start_date, bars_end_date, use_cache=use_cache
        )
        if fetch_from is not None:
            uncached_count += 1
        planned.append((contract, cached_bars, fetch_from))

    est_min = (uncached_count * float(args.delay_sec)) / (60.0 * key_pool.size())
    if args.dry_run:
        print(
            f"\n[DRY RUN] {len(contracts_to_process)} contracts needed ({uncached_count} uncached, "
            f"~{est_min:.1f} min across {key_pool.size()} key(s) at {args.delay_sec}s delay/key) "
            f"-> {len(target_days)} day(s) active."
        )
        for sample in contracts_to_process[:6]:
            print(
                f"    {sample['ticker']} | exp={sample['expiration_date']} "
                f"| strike={sample['strike_price']} | {sample['contract_type']}"
            )
        return

    ticker_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"\n--> [3/4] Fetching daily EOD bars for {len(contracts_to_process)} contracts "
        f"({uncached_count} uncached, ~{est_min:.1f} min across {key_pool.size()} key(s) "
        f"at {args.delay_sec}s pacing/key)..."
    )
    print(f"--> [4/4] Completed trading days flush immediately to {ticker_dir}/ (merge+resume on).")

    rows_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_keys: dict[str, set[tuple[str, float, str]]] = defaultdict(set)
    u_conid = _stable_conid(ticker, offset=1_000_000)
    written = 0
    merged_days = 0
    unchanged_days = 0
    total_contracts = len(contracts_to_process)
    progress_idx = 0
    dirty_cache_keys: set[str] = set()

    def _flush_bar_cache() -> None:
        if not dirty_cache_keys:
            return
        _save_bar_cache(cache_path, ticker, bar_cache, updated_keys=set(dirty_cache_keys))
        dirty_cache_keys.clear()

    def _flush_ready_days() -> None:
        nonlocal written, merged_days, unchanged_days
        for d_str in sorted(list(pending_by_day)):
            if pending_by_day[d_str]:
                continue
            pending_by_day.pop(d_str)
            day_rows = rows_by_day.pop(d_str, [])
            seen_keys.pop(d_str, None)
            existing_map = existing_rows_by_day.get(d_str)
            if not day_rows and existing_map is None:
                if int(args.shard_count) == 1:
                    _log(f"    [DAY SKIP] {d_str} -> 0 priced contracts")
                continue
            if not day_rows and existing_map is not None:
                unchanged_days += 1
                continue
            saved_path, appended, updated = _write_day_snapshot(
                out_base,
                ticker_dir,
                ticker,
                d_str,
                target_days[d_str],
                day_rows,
                overwrite=bool(args.overwrite),
                range_pct=float(args.range_pct),
                strike_step=float(args.strike_step),
                existing_map=existing_map,
            )
            if saved_path is None:
                unchanged_days += 1
            elif existing_map is not None:
                merged_days += 1
                total = len(existing_map) + appended
                _log(
                    f"    [DAY MERGED] {d_str} -> +{appended} new, ~{updated} updated "
                    f"({total} total) -> {saved_path.name}"
                )
            else:
                written += 1
                _log(f"    [DAY SAVED] {d_str} -> {len(day_rows)} contracts -> {saved_path.name}")

    def _record_contract(
        contract: dict[str, Any],
        bars: dict[str, tuple[float, int]],
        *,
        from_cache: bool,
        was_tail: bool = False,
    ) -> None:
        nonlocal progress_idx
        progress_idx += 1
        c_ticker = str(contract["ticker"])
        strike = float(contract["strike_price"])
        expiry = str(contract["expiration_date"])
        exp_dt = date.fromisoformat(expiry)
        right = "C" if contract.get("contract_type") == "call" else "P"
        opt_conid = _stable_conid(c_ticker)

        if from_cache:
            _log(f"    [{progress_idx}/{total_contracts}] {c_ticker} -> {len(bars)} bars (cached)")
        else:
            prev_entry = bar_cache.get(c_ticker)
            prev_from = str(prev_entry.get("from") or "") if isinstance(prev_entry, dict) else ""
            cache_from = min(prev_from, start_date) if (was_tail and prev_from) else start_date
            bar_cache[c_ticker] = {
                "from": cache_from,
                "to": bars_end_date,
                "bars": {d: [round(px, 4), vol] for d, (px, vol) in bars.items()},
            }
            dirty_cache_keys.add(c_ticker)
            if len(dirty_cache_keys) >= _CACHE_FLUSH_INTERVAL:
                _flush_bar_cache()
            suffix = " (tail extended)" if was_tail else ""
            _log(
                f"    [{progress_idx}/{total_contracts}] {c_ticker} -> "
                f"{len(bars)} daily bars{suffix}"
            )

        strike_key = round(strike, 4)
        for day_str, spot_px in target_days.items():
            pending = pending_by_day.get(day_str)
            if pending is None:
                continue
            pending.discard(c_ticker)
            day_dt = date.fromisoformat(day_str)
            dte = (exp_dt - day_dt).days
            # Persist every downloaded trading day (0 DTE up through max DTE) so open
            # positions entered earlier in the window keep daily marks even as DTE drops
            # or spot moves over subsequent weeks.
            if dte < 0 or day_str not in bars:
                continue
            dedup_key = _contract_key(expiry, strike_key, right)
            if dedup_key in seen_keys[day_str]:
                continue
            seen_keys[day_str].add(dedup_key)
            close_px, volume = bars[day_str]
            rows_by_day[day_str].append(
                _build_day_row(
                    ticker=ticker,
                    u_conid=u_conid,
                    opt_conid=opt_conid,
                    day_str=day_str,
                    spot_px=spot_px,
                    expiry=expiry,
                    dte=dte,
                    strike=strike,
                    right=right,
                    close_px=close_px,
                    volume=volume,
                )
            )
        _flush_ready_days()

    _flush_ready_days()
    work_items: list[tuple[dict[str, Any], dict[str, tuple[float, int]], str]] = []
    try:
        for contract, cached_bars, fetch_from in planned:
            if fetch_from is None:
                _record_contract(contract, cached_bars, from_cache=True)
            else:
                work_items.append((contract, cached_bars, fetch_from))

        _fetch_bars_with_pool(
            work_items,
            base_url=args.base_url,
            bars_end_date=bars_end_date,
            key_pool=key_pool,
            delay_sec=float(args.delay_sec),
            debug=bool(args.debug),
            on_result=lambda c, b, tail: _record_contract(c, b, from_cache=False, was_tail=tail),
        )
    except KeyboardInterrupt:
        try:
            _flush_bar_cache()
        except OSError as exc:
            _log(f"\n[WARN] Failed to flush remaining bar cache: {exc}", err=True)
        _log(
            f"\n[INTERRUPTED] Cached {len(bar_cache)} contract(s) in {cache_path}; "
            f"{written} new + {merged_days} merged day CSV(s) already saved. "
            f"Keys: {key_pool.summary()}. Re-run the same command to resume.",
            err=True,
        )
        sys.exit(130)
    finally:
        _flush_bar_cache()

    if kept_days:
        print(f"    Skipped {kept_days} existing day CSV(s) (--skip-existing).")
    if unchanged_days:
        print(f"    Unchanged {unchanged_days} existing day CSV(s) (all strikes up to date).")
    print(f"    Key usage: {key_pool.summary()}")
    print(
        f"\n[SUCCESS] Wrote {written} new + {merged_days} merged "
        f"daily snapshot CSV(s) for {ticker}."
    )


if __name__ == "__main__":
    main()
