"""Ad-hoc script to pull historical stock + EOD option bars from Massive.com.

Fetches full date ranges in bulk (`/v2/aggs/ticker/.../range/1/day/{from}/{to}`) so
6 weeks of daily bars take minutes instead of hours even on the 5 req/min Basic plan.
Writes 27-column canonical CSV snapshots to `data/snapshots/{TICKER}/` and updates
`data/metadata/snapshot_index.json` (`spot_source="LAST"`) without clobbering
`data/latest/{TICKER}_latest.csv`.

Usage:
    $env:MASSIVE_API_KEY = "your_api_key"
    python scripts/fetch_massive_options.py --ticker NVDA --start-date 2026-10-01 `
        --end-date 2026-11-12
    python scripts/fetch_massive_options.py --ticker NVDA --date 2026-10-15 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import zlib
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

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
_RETRY_BACKOFF_SEC = 62


def _stable_conid(key: str, *, offset: int = 1) -> int:
    return offset + int(zlib.crc32(key.encode("utf-8")) % 900_000_000)


def _api_get(
    base_url: str,
    path: str,
    params: dict[str, str],
    api_key: str,
    *,
    delay_sec: float,
    debug: bool = False,
) -> dict[str, Any]:
    if delay_sec > 0:
        time.sleep(delay_sec)
    query = urlencode(params)
    url = f"{base_url.rstrip('/')}{path}?{query}" if query else f"{base_url.rstrip('/')}{path}"
    if debug:
        print(f"    [DEBUG] GET {url}", file=sys.stderr)
    req = Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "FOMO-Sapiens/1.0",
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
            if debug:
                res = payload.get("results")
                res_len = len(res) if isinstance(res, list) else ("dict" if res else 0)
                print(
                    f"    [DEBUG] -> status={payload.get('status')} "
                    f"results={res_len} next_url={bool(payload.get('next_url'))}",
                    file=sys.stderr,
                )
            return payload
    except HTTPError as exc:
        err_msg = exc.read().decode("utf-8", errors="ignore")
        if exc.code == 429:
            print(
                f"\n[WARN] Rate limit HTTP 429 (5 calls/min). Waiting {_RETRY_BACKOFF_SEC}s...",
                file=sys.stderr,
            )
            for remaining in range(_RETRY_BACKOFF_SEC, 0, -15):
                print(f"       Resuming in {remaining}s...", file=sys.stderr)
                time.sleep(min(15, remaining))
            return _api_get(base_url, path, params, api_key, delay_sec=0.5, debug=debug)
        if exc.code == 403:
            raise RuntimeError(
                f"\n[HTTP 403 NOT_AUTHORIZED] URL: {url}\nResponse: {err_msg}\n\n"
                "Check on Massive.com / Polygon.io:\n"
                "  1. Subscriptions are separate per asset class: Stocks vs Options.\n"
                "     Make sure BOTH 'Options Basic' AND 'Stocks Basic' ($0/mo) are Subscribed\n"
                "     on the Pricing / Subscriptions page (https://massive.com/pricing).\n"
                "  2. Check that your requested dates fall within your plan's 2-year window.\n"
                "  3. Or pass `--spot <price>` to skip the stock endpoint (single-day mode)."
            ) from exc
        raise RuntimeError(f"HTTP {exc.code} for {url}: {err_msg}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error accessing {url}: {exc}") from exc


def fetch_stock_daily_bars(
    base_url: str,
    ticker: str,
    start_date: str,
    end_date: str,
    api_key: str,
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
    api_key: str,
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
    api_key: str,
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
    api_key: str,
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
    if index_path.is_file():
        index: dict[str, Any] = json.loads(index_path.read_text(encoding="utf-8"))
    else:
        index = {"updated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "tickers": {}}

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

    tmp = index_path.with_name(f".{index_path.name}.tmp")
    tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
    os.replace(tmp, index_path)


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


def _save_bar_cache(cache_path: Path, ticker: str, contracts: dict[str, dict[str, Any]]) -> None:
    """Atomically persist downloaded contract bars after each API response."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_name(f".{cache_path.name}.tmp")
    payload = {"version": 1, "ticker": ticker, "contracts": contracts}
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, cache_path)


def _bars_from_cache_entry(
    entry: dict[str, Any] | None, start_date: str, end_date: str
) -> dict[str, tuple[float, int]] | None:
    """Return cached `{day: (close, vol)}` if cached range covers `[start_date, end_date]`."""
    if not entry:
        return None
    cached_from = str(entry.get("from") or "")
    cached_to = str(entry.get("to") or "")
    raw_bars = entry.get("bars")
    if not cached_from or not cached_to or not isinstance(raw_bars, dict):
        return None
    if cached_from > start_date or cached_to < end_date:
        return None
    return {str(d): (float(pair[0]), int(pair[1])) for d, pair in raw_bars.items()}


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

    base_map: dict[tuple[str, float, str], dict[str, Any]] = {}
    if not overwrite:
        base_map = (
            dict(existing_map) if existing_map is not None else _read_existing_day_rows(out_file)
        )

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
    tmp = out_file.with_name(f".{out_file.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows_sorted)
    os.replace(tmp, out_file)
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
        default=os.getenv("MASSIVE_API_KEY") or os.getenv("POLYGON_API_KEY"),
        help="Massive.com / Polygon.io API key",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base URL")
    parser.add_argument("--spot", type=float, default=None, help="Manual spot (single-day only)")
    parser.add_argument("--min-dte", type=int, default=25, help="Min DTE per session (default: 25)")
    parser.add_argument("--max-dte", type=int, default=60, help="Max DTE per session (default: 60)")
    parser.add_argument("--range-pct", type=float, default=0.20, help="Strike range +-pct (0.20)")
    parser.add_argument("--strike-step", type=float, default=5.0, help="Strike increment ($5.0)")
    parser.add_argument(
        "--expiry-cadence",
        choices=("monthly", "all"),
        default="monthly",
        help="'monthly' picks 3rd-Friday expiries (~12m for 6w); 'all' adds weeklies",
    )
    parser.add_argument(
        "--delay-sec",
        type=float,
        default=12.5,
        help="Seconds between API calls (12.5s avoids 5 req/min Basic limit; 0.1 for Starter+)",
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
    if not args.api_key:
        print("[ERROR] Set MASSIVE_API_KEY or pass --api-key.", file=sys.stderr)
        sys.exit(1)

    start_date = args.start_date or args.date
    end_date = args.end_date or args.date or args.start_date
    if not start_date or not end_date:
        print("[ERROR] Pass --date YYYY-MM-DD or --start-date + --end-date.", file=sys.stderr)
        sys.exit(1)

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
        f"[entry {start_date}..{end_date}, bars through {bars_end_date}]..."
    )
    if args.spot is not None and start_date == bars_end_date:
        spot_by_day = {start_date: float(args.spot)}
    else:
        spot_by_day = fetch_stock_daily_bars(
            args.base_url,
            ticker,
            start_date,
            bars_end_date,
            args.api_key,
            delay_sec=0.2,
            debug=args.debug,
        )
    if not spot_by_day:
        print(
            f"[ERROR] Massive returned 0 trading days for {ticker} "
            f"in [{start_date} .. {bars_end_date}]."
        )
        sys.exit(1)

    daily_bands: dict[str, tuple[float, float]] = {
        day_s: (px * (1.0 - float(args.range_pct)), px * (1.0 + float(args.range_pct)))
        for day_s, px in spot_by_day.items()
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
        f"(strikes ${min_strike:.1f}..${max_strike:.1f}, cadence={args.expiry_cadence})..."
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
        args.api_key,
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

    filtered = [
        c
        for c in standard_contracts
        if _matches_strike_step(float(c.get("strike_price", 0.0)), float(args.strike_step))
        and _in_any_daily_band(
            float(c.get("strike_price", 0.0)),
            str(c.get("expiration_date", "")),
            daily_bands,
        )
        and (
            args.expiry_cadence == "all"
            or _is_monthly_third_friday(str(c.get("expiration_date", "")))
        )
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
    ordered_contracts = sorted(
        filtered,
        key=lambda c: str(c.get("expiration_date", "")),
        reverse=True,
    )
    force_fetch = bool(args.overwrite or args.refresh_existing)
    pending_by_day: dict[str, set[str]] = {}
    for day_str in target_days:
        existing_keys = existing_rows_by_day.get(day_str, {})
        req: set[str] = set()
        for c in ordered_contracts:
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
    contracts_to_process = [c for c in ordered_contracts if str(c["ticker"]) in needed_tickers]
    skipped_existing = len(filtered) - len(contracts_to_process)
    if skipped_existing:
        print(
            f"    Skipping {skipped_existing} contract(s) already present in every target day CSV."
        )

    cache_path = _cache_path_for(out_base, ticker)
    use_cache = not args.no_cache and not args.refresh_existing
    bar_cache = {} if args.no_cache else _load_bar_cache(cache_path)
    uncached_count = (
        len(contracts_to_process)
        if not use_cache
        else sum(
            1
            for c in contracts_to_process
            if _bars_from_cache_entry(bar_cache.get(str(c["ticker"])), start_date, bars_end_date)
            is None
        )
    )

    if args.dry_run:
        est_min = (uncached_count * float(args.delay_sec)) / 60.0
        print(
            f"\n[DRY RUN] {len(contracts_to_process)} contracts needed ({uncached_count} uncached, "
            f"~{est_min:.1f} min at {args.delay_sec}s delay) -> {len(target_days)} day(s) active."
        )
        for sample in contracts_to_process[:6]:
            print(
                f"    {sample['ticker']} | exp={sample['expiration_date']} "
                f"| strike={sample['strike_price']} | {sample['contract_type']}"
            )
        return

    ticker_dir.mkdir(parents=True, exist_ok=True)
    est_min = (uncached_count * float(args.delay_sec)) / 60.0
    print(
        f"\n--> [3/4] Fetching daily EOD bars for {len(contracts_to_process)} contracts "
        f"({uncached_count} uncached, ~{est_min:.1f} min at {args.delay_sec}s pacing)..."
    )
    print(f"--> [4/4] Completed trading days flush immediately to {ticker_dir}/ (merge+resume on).")

    rows_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_keys: dict[str, set[tuple[str, float, str]]] = defaultdict(set)
    u_conid = _stable_conid(ticker, offset=1_000_000)
    written = 0
    merged_days = 0
    unchanged_days = 0

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
                print(f"    [DAY SKIP] {d_str} -> 0 priced contracts")
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
                print(
                    f"    [DAY MERGED] {d_str} -> +{appended} new, ~{updated} updated "
                    f"({total} total) -> {saved_path.name}"
                )
            else:
                written += 1
                print(f"    [DAY SAVED] {d_str} -> {len(day_rows)} contracts -> {saved_path.name}")

    _flush_ready_days()
    total_contracts = len(contracts_to_process)
    try:
        for idx, contract in enumerate(contracts_to_process, 1):
            c_ticker = str(contract["ticker"])
            strike = float(contract["strike_price"])
            expiry = str(contract["expiration_date"])
            exp_dt = date.fromisoformat(expiry)
            right = "C" if contract.get("contract_type") == "call" else "P"
            opt_conid = _stable_conid(c_ticker)

            cached_bars = (
                None
                if not use_cache
                else _bars_from_cache_entry(bar_cache.get(c_ticker), start_date, bars_end_date)
            )
            if cached_bars is not None:
                bars = cached_bars
                print(f"    [{idx}/{total_contracts}] {c_ticker} -> {len(bars)} bars (cached)")
            else:
                bars = fetch_option_daily_bars(
                    args.base_url,
                    c_ticker,
                    start_date,
                    bars_end_date,
                    args.api_key,
                    delay_sec=float(args.delay_sec),
                    debug=args.debug,
                )
                bar_cache[c_ticker] = {
                    "from": start_date,
                    "to": bars_end_date,
                    "bars": {d: [round(px, 4), vol] for d, (px, vol) in bars.items()},
                }
                _save_bar_cache(cache_path, ticker, bar_cache)
                print(f"    [{idx}/{total_contracts}] {c_ticker} -> {len(bars)} daily bars")

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
    except KeyboardInterrupt:
        print(
            f"\n[INTERRUPTED] Cached {len(bar_cache)} contract(s) in {cache_path}; "
            f"{written} new + {merged_days} merged day CSV(s) already saved. "
            "Re-run the same command to resume.",
            file=sys.stderr,
        )
        sys.exit(130)

    if kept_days:
        print(f"    Skipped {kept_days} existing day CSV(s) (--skip-existing).")
    if unchanged_days:
        print(f"    Unchanged {unchanged_days} existing day CSV(s) (all strikes up to date).")
    print(
        f"\n[SUCCESS] Wrote {written} new + {merged_days} merged "
        f"daily snapshot CSV(s) for {ticker}."
    )


if __name__ == "__main__":
    main()
