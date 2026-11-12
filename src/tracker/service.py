"""Position tracker: keeps quoting the legs of open bull put spreads on every collection slot.

Normal snapshots only hold 30-120 DTE contracts, so a spread opened at ~38 DTE drops out of them
after about 8 days. The tracker requests quotes for the exact leg contracts of every ACTIVE
tracked spread (never a chain) right after the collector's snapshot of that ticker, under the
collector's refresh lock, and stores them as normal `OptionContractRow` rows in
`data/tracked/{TICKER}/` (see `src/tracker/store.py`).

Spreads come from two sources:
- `AUTO`: the Strategy Lab Auto-Builder `BULL_PUT_SPREAD` pick on the first usable snapshot of
  the day at/after `tracker.auto_pick_not_before_et`, computed exactly like backtest mode
  `AUTO_BUILD` (same Greeks enrichment, walls `top_n`, no corridor overlay).
- `USER`: registered through `POST /api/tracker/spreads` (validated like backtest mode `LEGS`).

Spreads are held to expiry: ACTIVE until the closing slot of the expiry date, then EXPIRED.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Protocol

import polars as pl
from pydantic import BaseModel, ConfigDict

from src.analytics.backtest import (
    WALLS_TOP_N,
    _enrich,
    resolve_price_with_source,
    validate_bull_put_legs,
)
from src.analytics.strategies import StrategyLegInput, evaluate_strategy
from src.analytics.walls import compute_walls
from src.broker.fetcher import ExactContractSpec, ExactQuoteResult
from src.collector.calendar import US_EASTERN, Slot, et_date
from src.core.config import Settings
from src.core.exceptions import (
    AnalyzerError,
    GatewayConnectionError,
    StorageSchemaError,
)
from src.storage.csv_repository import CSVRepository
from src.storage.schema import OptionContractRow
from src.tracker.store import (
    TrackedLeg,
    TrackedSpread,
    TrackerRunRecord,
    TrackerStore,
)

logger = logging.getLogger(__name__)

TRACKER_STEP_TIMEOUT_SEC: Final = 120.0
CAP_WARNING_RATIO: Final = 0.8
CAP_SKIP_WARNING_DAYS: Final = 7
# AUTO decisions that use up the one pick per ticker per market date.
AUTO_DECISIONS: Final = frozenset(
    {"REGISTERED", "AUTO_DUPLICATE", "AUTO_SKIPPED_CAP", "AUTO_SKIPPED_NO_USABLE"}
)
FROZEN_MARKET_DATA_TYPES: Final = frozenset({2, 4})

ClockFn = Callable[[], datetime]


class ExactQuoteSource(Protocol):
    async def fetch_exact_contracts(
        self,
        ticker: str,
        specs: Sequence[ExactContractSpec],
        *,
        underlying_conid: int = 0,
    ) -> ExactQuoteResult: ...


class DuplicateSpreadError(ValueError):
    """The same legs are already tracked as an ACTIVE spread of this ticker."""


class TrackerCapError(ValueError):
    """`tracker.max_active_spreads` ACTIVE spreads are already tracked."""


class TrackerStateError(ValueError):
    """The requested change does not apply to the spread's current status."""


class TrackerStatus(BaseModel):
    """Tracker summary shown in `GET /api/collector/status`."""

    model_config = ConfigDict(extra="forbid")

    active_count: int
    max_active_spreads: int
    auto_pick: bool
    auto_pick_not_before_et: str
    warnings: list[str]
    last_result: TrackerRunRecord | None
    last_auto: TrackerRunRecord | None
    registry_error: str | None


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _leg_label(leg: TrackedLeg) -> str:
    return f"{leg.side} {leg.right} {leg.strike:g} {leg.expiry}"


def _row_key(expiry: str, strike: float, right: str) -> tuple[str, float, str]:
    return (expiry, round(float(strike), 4), right)


def _find_row(df: pl.DataFrame, expiry: str, strike: float, right: str) -> dict[str, Any] | None:
    rows = df.filter(
        (pl.col("expiry") == expiry)
        & (pl.col("right") == right)
        & ((pl.col("strike") - strike).abs() < 1e-4)
    )
    return rows.row(0, named=True) if rows.height else None


class TrackerService:
    def __init__(
        self,
        *,
        settings: Settings,
        repo: CSVRepository,
        fetcher: ExactQuoteSource,
        store: TrackerStore,
        clock: ClockFn,
        validate_ticker: Callable[[str], str],
    ) -> None:
        self.settings = settings
        self.cfg = settings.tracker
        self.repo = repo
        self.fetcher = fetcher
        self.store = store
        self.clock = clock
        self.validate_ticker = validate_ticker
        self.risk_free_rate = settings.analytics.risk_free_rate
        self.registry_error: str | None = None
        try:
            self._spreads: list[TrackedSpread] = store.load_spreads()
        except StorageSchemaError as exc:
            # Keep the unreadable file untouched; the tracker stays idle until it is fixed.
            logger.error("tracker registry unreadable; tracker idle: %s", exc)
            self.registry_error = str(exc)
            self._spreads = []
        self._decided: set[tuple[str, str]] = {
            (r.ticker, r.market_date)
            for r in store.read_runs()
            if r.kind == "AUTO" and r.status in AUTO_DECISIONS and r.market_date
        }

    # ------------------------------------------------------------------ helpers
    def _now(self) -> datetime:
        now = self.clock()
        return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)

    def _save(self) -> None:
        if self.registry_error:
            raise StorageSchemaError(f"Tracker registry unreadable: {self.registry_error}")
        self.store.save_spreads(self._spreads)

    def spreads(self) -> list[TrackedSpread]:
        return list(self._spreads)

    def active(self, ticker: str | None = None) -> list[TrackedSpread]:
        return [
            s
            for s in self._spreads
            if s.status == "ACTIVE" and (ticker is None or s.ticker == ticker)
        ]

    def active_tickers(self) -> list[str]:
        return sorted({s.ticker for s in self.active()})

    def get(self, spread_id: str) -> TrackedSpread:
        for spread in self._spreads:
            if spread.id == spread_id:
                return spread
        raise KeyError(spread_id)

    def _replace(self, spread: TrackedSpread) -> None:
        self._spreads = [spread if s.id == spread.id else s for s in self._spreads]

    def _duplicate_of(self, ticker: str, legs: Sequence[TrackedLeg]) -> TrackedSpread | None:
        key = tuple(sorted(leg.key() for leg in legs))
        return next((s for s in self.active(ticker) if s.legs_key() == key), None)

    def _new_spread(
        self,
        *,
        ticker: str,
        source: str,
        legs: list[TrackedLeg],
        qty: int,
        underlying_conid: int,
        snapshot_id: str,
        market_date: str,
        entry_net_credit: float | None,
    ) -> TrackedSpread:
        now = self._now()
        spread = TrackedSpread.model_validate(
            {
                "id": f"{ticker}-{now:%Y%m%d}-{uuid.uuid4().hex[:8]}",
                "ticker": ticker,
                "source": source,
                "legs": [leg.model_dump() for leg in legs],
                "qty": qty,
                "underlying_conid": underlying_conid,
                "created_from_snapshot_id": snapshot_id,
                "created_at_utc": _iso(now),
                "created_market_date": market_date,
                "entry_net_credit": entry_net_credit,
            }
        )
        self._spreads.append(spread)
        self._save()
        return spread

    def _legs_from_snapshot(
        self, df: pl.DataFrame, legs: Iterable[StrategyLegInput | Any]
    ) -> list[TrackedLeg]:
        """Leg specs with the `conId` from the snapshot; every leg must exist in it."""
        out: list[TrackedLeg] = []
        for leg in legs:
            row = _find_row(df, leg.expiry, float(leg.strike), leg.right)
            if row is None:
                raise ValueError(
                    f"{leg.side} {leg.right} {float(leg.strike):g} {leg.expiry} is not in "
                    "the snapshot"
                )
            out.append(
                TrackedLeg(
                    side=leg.side,
                    right=leg.right,
                    expiry=leg.expiry,
                    strike=float(leg.strike),
                    option_conid=int(row["option_conid"]),
                )
            )
        return out

    # ------------------------------------------------------------------ USER source
    def register_user(
        self, ticker: str, snapshot_id: str, legs: list[StrategyLegInput]
    ) -> TrackedSpread:
        """Validate (backtest mode `LEGS` rules) and register a user spread."""
        symbol = self.validate_ticker(ticker)
        validate_bull_put_legs(legs)
        market_day = et_date(self._now())
        if date.fromisoformat(legs[0].expiry) < market_day:
            raise ValueError(f"Expiry {legs[0].expiry} is in the past")
        df = self.repo.load_snapshot(symbol, snapshot_id)  # SnapshotNotFoundError -> 404
        tracked = self._legs_from_snapshot(df, legs)
        if self._duplicate_of(symbol, tracked) is not None:
            raise DuplicateSpreadError(f"These {symbol} legs are already tracked")
        if len(self.active()) >= self.cfg.max_active_spreads:
            raise TrackerCapError(
                f"{self.cfg.max_active_spreads} spreads are already tracked "
                "(tracker.max_active_spreads)"
            )
        credit: float | None = None
        try:
            ev = evaluate_strategy(
                _enrich(df, self.risk_free_rate),
                strategy_kind="BULL_PUT_SPREAD",
                legs=legs,
                risk_free_rate=self.risk_free_rate,
            )
            credit = round(ev.pricing.net_mid_per_share, 4)
        except ValueError:
            credit = None
        spread = self._new_spread(
            ticker=symbol,
            source="USER",
            legs=tracked,
            qty=legs[0].qty,
            underlying_conid=int(df["underlying_conid"][0]),
            snapshot_id=snapshot_id,
            market_date=str(df["market_date"][0]),
            entry_net_credit=credit,
        )
        logger.info("tracker registered USER spread %s", spread.id)
        return spread

    def stop(self, spread_id: str) -> TrackedSpread:
        spread = self.get(spread_id)  # KeyError -> 404
        if spread.status != "ACTIVE":
            raise TrackerStateError(f"Spread {spread_id} is {spread.status}, not ACTIVE")
        now = self._now()
        updated = spread.model_copy(update={"status": "STOPPED", "status_changed_utc": _iso(now)})
        self._replace(updated)
        self._save()
        self.store.append_runs(
            [
                TrackerRunRecord(
                    kind="LIFECYCLE",
                    planned_slot_utc=None,
                    actual_run_utc=_iso(now),
                    ticker=spread.ticker,
                    market_date=et_date(now).isoformat(),
                    spread_id=spread.id,
                    status="STOPPED",
                    note="Stopped by the user; recorded quotes are kept.",
                )
            ]
        )
        return updated

    # ------------------------------------------------------------------ AUTO source
    def auto_pick(
        self,
        slot: Slot,
        ticker: str,
        *,
        snapshot_id: str | None,
        usable: bool | None,
    ) -> TrackerRunRecord | None:
        """Register today's Auto-Builder pick from this snapshot if it is the one to use.

        The pick uses the first usable snapshot of the market date taken at/after
        `auto_pick_not_before_et`; one decision per ticker per market date.
        """
        if not self.cfg.auto_pick or self.registry_error or not snapshot_id or not usable:
            return None
        symbol = self.validate_ticker(ticker)
        df = self.repo.load_snapshot(symbol, snapshot_id)
        ts = _parse_utc(str(df["snapshot_ts_utc"][0]))
        local = ts.astimezone(US_EASTERN)
        market_date = local.date().isoformat()
        if (symbol, market_date) in self._decided:
            return None
        if local.hour * 60 + local.minute < self.cfg.not_before_minutes():
            return None

        base = {
            "kind": "AUTO",
            "planned_slot_utc": _iso(slot.at_utc),
            "slot_kind": slot.kind,
            "actual_run_utc": _iso(self._now()),
            "ticker": symbol,
            "market_date": market_date,
            "source_snapshot_id": snapshot_id,
        }
        try:
            enriched = _enrich(df, self.risk_free_rate)
            walls = compute_walls(enriched, top_n=WALLS_TOP_N)
            ev = evaluate_strategy(
                enriched,
                strategy_kind="BULL_PUT_SPREAD",
                target_expiry=None,
                risk_free_rate=self.risk_free_rate,
                walls_report=walls,
            )
            if any(leg.mid <= 0.0 for leg in ev.legs):
                raise ValueError("a picked leg has no price in the snapshot")
            legs = self._legs_from_snapshot(enriched, ev.legs)
        except ValueError as exc:
            rec = TrackerRunRecord.model_validate(
                {**base, "status": "AUTO_FAILED", "error": f"Auto-Builder: {exc}"}
            )
            self.store.append_runs([rec])
            return rec

        status: str
        extra: dict[str, Any] = {"legs": [leg.model_dump() for leg in legs]}
        dup = self._duplicate_of(symbol, legs)
        if dup is not None:
            status = "AUTO_DUPLICATE"
            extra["spread_id"] = dup.id
            extra["note"] = f"Same legs as ACTIVE spread {dup.id}; not tracked twice."
        elif len(self.active()) >= self.cfg.max_active_spreads:
            status = "AUTO_SKIPPED_CAP"
            extra["note"] = (
                f"{self.cfg.max_active_spreads} spreads already tracked "
                "(tracker.max_active_spreads)."
            )
        else:
            spread = self._new_spread(
                ticker=symbol,
                source="AUTO",
                legs=legs,
                qty=1,
                underlying_conid=int(df["underlying_conid"][0]),
                snapshot_id=snapshot_id,
                market_date=market_date,
                entry_net_credit=round(ev.pricing.net_mid_per_share, 4),
            )
            status = "REGISTERED"
            extra["spread_id"] = spread.id
        rec = TrackerRunRecord.model_validate({**base, "status": status, **extra})
        self._decided.add((symbol, market_date))
        self.store.append_runs([rec])
        logger.info("tracker AUTO %s %s %s", symbol, market_date, status)
        return rec

    # ------------------------------------------------------------------ quoting
    def due(self, ticker: str, slot: Slot, done: set[str]) -> list[TrackedSpread]:
        """ACTIVE spreads of `ticker` not yet quoted in this slot and not past expiry."""
        day = et_date(slot.at_utc).isoformat()
        return [s for s in self.active(ticker) if s.id not in done and s.expiry >= day]

    def _record(
        self, slot: Slot, spread: TrackedSpread, status: str, **extra: Any
    ) -> TrackerRunRecord:
        return TrackerRunRecord.model_validate(
            {
                "kind": "QUOTE",
                "planned_slot_utc": _iso(slot.at_utc),
                "slot_kind": slot.kind,
                "actual_run_utc": extra.pop("actual_run_utc", None),
                "ticker": spread.ticker,
                "market_date": et_date(slot.at_utc).isoformat(),
                "spread_id": spread.id,
                "status": status,
                **extra,
            }
        )

    async def track_ticker(self, slot: Slot, ticker: str, done: set[str]) -> list[TrackerRunRecord]:
        """Quote the legs of every due spread of `ticker` once; never raises."""
        if self.registry_error:
            return []
        try:
            symbol = self.validate_ticker(ticker)
        except AnalyzerError:
            return []
        spreads = self.due(symbol, slot, done)
        if not spreads:
            return []
        started = self._now()
        specs: dict[tuple[str, float, str], ExactContractSpec] = {}
        for spread in spreads:
            for leg in spread.legs:
                key = _row_key(leg.expiry, leg.strike, leg.right)
                specs.setdefault(
                    key, ExactContractSpec(leg.option_conid, leg.expiry, leg.strike, leg.right)
                )
        result: ExactQuoteResult | None = None
        file_id: str | None = None
        error: str | None = None
        try:
            result = await asyncio.wait_for(
                self.fetcher.fetch_exact_contracts(
                    symbol, list(specs.values()), underlying_conid=spreads[0].underlying_conid
                ),
                timeout=TRACKER_STEP_TIMEOUT_SEC,
            )
            if result.rows:
                self.store.save_quotes(symbol, list(result.rows))
                file_id = result.snapshot_id
        except TimeoutError:
            error = f"TIMEOUT: no result within {TRACKER_STEP_TIMEOUT_SEC:.0f}s"
        except GatewayConnectionError as exc:
            error = f"GATEWAY_OFFLINE: {exc}"
        except AnalyzerError as exc:
            error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # never crash the collector cycle
            logger.exception("tracker step failed for %s", symbol)
            error = f"UNEXPECTED: {type(exc).__name__}: {exc}"

        records: list[TrackerRunRecord] = []
        if error is not None or result is None:
            for spread in spreads:
                done.add(spread.id)
                rec = self._record(
                    slot,
                    spread,
                    "FAILED",
                    actual_run_utc=_iso(started),
                    error=error,
                    # Spot and data type are recorded when the quote request got that far.
                    underlying_price=result.underlying_price if result else None,
                    spot_source=result.spot_source if result else None,
                    market_data_type=result.market_data_type if result else None,
                )
                records.append(rec)
            self.store.append_runs(records)
            return records

        rows = {_row_key(r.expiry, r.strike, r.right): r for r in result.rows}
        common_flags: list[str] = []
        if result.market_data_type in FROZEN_MARKET_DATA_TYPES:
            common_flags.append("FROZEN_DATA")
        if result.spot_source == "CLOSE":
            common_flags.append("SPOT_SOURCE_CLOSE")
        quote_ts = result.snapshot_ts_utc
        for spread in spreads:
            done.add(spread.id)
            priced = 0
            missing: list[str] = []
            flags = list(common_flags)
            for leg in spread.legs:
                row: OptionContractRow | None = rows.get(
                    _row_key(leg.expiry, leg.strike, leg.right)
                )
                if row is None:
                    missing.append(f"{_leg_label(leg)}: NOT_RETURNED")
                    continue
                _price, source = resolve_price_with_source(row.model_dump())
                if source is None:
                    missing.append(f"{_leg_label(leg)}: NO_PRICE")
                    continue
                priced += 1
                if source != "BID_ASK" and "LEG_FALLBACK_PRICE" not in flags:
                    flags.append("LEG_FALLBACK_PRICE")
            status = "OK" if not missing else ("NO_QUOTE" if priced == 0 else "PARTIAL")
            records.append(
                self._record(
                    slot,
                    spread,
                    status,
                    actual_run_utc=_iso(started),
                    tracked_snapshot_id=file_id,
                    underlying_price=round(result.underlying_price, 4),
                    spot_source=result.spot_source,
                    market_data_type=result.market_data_type,
                    priced_legs=priced,
                    missing_legs=missing,
                    quality_flags=flags,
                    note="; ".join(result.notices) or None,
                )
            )
            if priced:
                self._replace(spread.model_copy(update={"last_quote_utc": quote_ts}))
        self._save()
        self.store.append_runs(records)
        return records

    def log_unreached(
        self, slot: Slot, ticker: str, done: set[str], *, status: str, error: str
    ) -> list[TrackerRunRecord]:
        """One record per due spread the slot could not quote (no silent gaps)."""
        records = []
        for spread in self.due(ticker, slot, done):
            done.add(spread.id)
            records.append(
                self._record(slot, spread, status, actual_run_utc=_iso(self._now()), error=error)
            )
        self.store.append_runs(records)
        return records

    # ------------------------------------------------------------------ lifecycle
    def finish_slot(self, slot: Slot, collected: Sequence[str]) -> list[TrackerRunRecord]:
        """After the CLOSING slot: log days without an AUTO pick, expire spreads due today."""
        if slot.kind != "CLOSING" or self.registry_error:
            return []
        now = self._now()
        day = et_date(slot.at_utc).isoformat()
        records: list[TrackerRunRecord] = []
        if self.cfg.auto_pick:
            for ticker in collected:
                if (ticker, day) in self._decided:
                    continue
                self._decided.add((ticker, day))
                records.append(
                    TrackerRunRecord(
                        kind="AUTO",
                        planned_slot_utc=_iso(slot.at_utc),
                        slot_kind=slot.kind,
                        actual_run_utc=_iso(now),
                        ticker=ticker,
                        market_date=day,
                        status="AUTO_SKIPPED_NO_USABLE",
                        note=(
                            "No usable snapshot at/after "
                            f"{self.cfg.auto_pick_not_before_et} ET on this market date."
                        ),
                    )
                )
        records.extend(
            self._expire(lambda s: s.expiry <= day, now, "Closing slot of expiry day done.")
        )
        self.store.append_runs(records)
        return records

    def expire_overdue(self) -> list[TrackerRunRecord]:
        """Expire ACTIVE spreads whose expiry date has passed (e.g. while the app was off)."""
        if self.registry_error:
            return []
        now = self._now()
        today = et_date(now).isoformat()
        records = self._expire(
            lambda s: s.expiry < today, now, "Expiry passed without a closing slot run."
        )
        self.store.append_runs(records)
        return records

    def _expire(
        self, due: Callable[[TrackedSpread], bool], now: datetime, note: str
    ) -> list[TrackerRunRecord]:
        records: list[TrackerRunRecord] = []
        for spread in self.active():
            if not due(spread):
                continue
            self._replace(
                spread.model_copy(update={"status": "EXPIRED", "status_changed_utc": _iso(now)})
            )
            records.append(
                TrackerRunRecord(
                    kind="LIFECYCLE",
                    planned_slot_utc=None,
                    actual_run_utc=_iso(now),
                    ticker=spread.ticker,
                    market_date=et_date(now).isoformat(),
                    spread_id=spread.id,
                    status="EXPIRED",
                    note=note,
                )
            )
        if records:
            self._save()
        return records

    def record_missed(self, slots: Sequence[Slot]) -> list[TrackerRunRecord]:
        """MISSED records for slots that passed while the app was off (per spread then active)."""
        if self.registry_error:
            return []
        records: list[TrackerRunRecord] = []
        for slot in slots:
            day = et_date(slot.at_utc).isoformat()
            for spread in self._spreads:
                created = _parse_utc(spread.created_at_utc)
                ended = (
                    _parse_utc(spread.status_changed_utc)
                    if spread.status != "ACTIVE" and spread.status_changed_utc
                    else None
                )
                if created > slot.at_utc or spread.expiry < day:
                    continue
                if ended is not None and ended <= slot.at_utc:
                    continue
                records.append(
                    self._record(
                        slot, spread, "MISSED", note="App was not running at the planned time."
                    )
                )
        self.store.append_runs(records)
        return records

    # ------------------------------------------------------------------ status
    def status(self) -> TrackerStatus:
        runs = self.store.read_runs()
        quotes = [r for r in runs if r.kind == "QUOTE"]
        autos = [r for r in runs if r.kind == "AUTO"]
        active = len(self.active())
        cap = self.cfg.max_active_spreads
        warnings: list[str] = []
        if active > CAP_WARNING_RATIO * cap:
            warnings.append(
                f"{active} of {cap} tracked spreads are active (over "
                f"{CAP_WARNING_RATIO:.0%} of tracker.max_active_spreads)."
            )
        cut = self._now() - timedelta(days=CAP_SKIP_WARNING_DAYS)
        cap_skips = [
            r
            for r in autos
            if r.status == "AUTO_SKIPPED_CAP"
            and r.actual_run_utc
            and _parse_utc(r.actual_run_utc) >= cut
        ]
        if cap_skips:
            last = cap_skips[-1]
            warnings.append(
                f"{len(cap_skips)} AUTO pick(s) skipped at the cap in the last "
                f"{CAP_SKIP_WARNING_DAYS} days (last: {last.ticker} {last.market_date})."
            )
        if self.registry_error:
            warnings.append(f"Tracker idle: {self.registry_error}")
        return TrackerStatus(
            active_count=active,
            max_active_spreads=cap,
            auto_pick=self.cfg.auto_pick,
            auto_pick_not_before_et=self.cfg.auto_pick_not_before_et,
            warnings=warnings,
            last_result=quotes[-1] if quotes else None,
            last_auto=autos[-1] if autos else None,
            registry_error=self.registry_error,
        )
