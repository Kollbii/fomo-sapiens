"""Bull put spread backtest engine: synthetic snapshots with hand-computed P&L."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from src.analytics import strategies
from src.analytics.backtest import (
    QUALITY_THRESHOLDS,
    US_EASTERN,
    BacktestStep,
    load_backtest_steps,
    resolve_price_with_source,
    run_bull_put_backtest,
    strategies_source_sha256,
    validate_bull_put_legs,
)
from src.analytics.strategies import StrategyLegInput
from src.core.config import StorageSettings
from src.core.exceptions import InvalidTickerError, SnapshotNotFoundError
from src.storage.csv_repository import CSVRepository
from src.storage.quality import MIN_QUOTE_COVERAGE
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA, OptionContractRow, SnapshotMeta
from tests.unit.test_strategies import _build_synthetic_chain

EXPIRY = "2026-12-18"  # Friday
STRIKES = (90.0, 95.0, 100.0, 105.0, 110.0)
Quote = tuple[float | None, float | None, float | None, float | None]  # bid, ask, last, close

# Entry: SELL 100P @ mid 2.10 (2.00/2.20), BUY 95P @ mid 1.00 (0.90/1.10)
# credit at mid = (2.10 - 1.00) x 100 = $110; natural = (2.00 - 1.10) x 100 = $90
# max loss = 5 x 100 - 110 = $390
ENTRY_QUOTES: dict[tuple[float, str], Quote] = {
    (100.0, "P"): (2.00, 2.20, 2.10, 2.10),
    (95.0, "P"): (0.90, 1.10, 1.00, 1.00),
}
LEGS = [
    StrategyLegInput(side="SELL", right="P", expiry=EXPIRY, strike=100.0, qty=1),
    StrategyLegInput(side="BUY", right="P", expiry=EXPIRY, strike=95.0, qty=1),
]


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _chain(
    ts: datetime,
    spot: float,
    quotes: dict[tuple[float, str], Quote] | None = None,
    *,
    strikes: tuple[float, ...] = STRIKES,
    expiries: tuple[str, ...] = (EXPIRY,),
    md_type: int = 3,
    unquoted_others: bool = False,
    ticker: str = "NVDA",
) -> pl.DataFrame:
    market_date = ts.astimezone(US_EASTERN).date()
    sid = f"{ticker}_{ts.strftime('%Y%m%d_%H%M%S')}"
    rows: list[dict[str, Any]] = []
    conid = 1000
    for expiry in expiries:
        dte = (date.fromisoformat(expiry) - market_date).days
        for strike in strikes:
            for right in ("C", "P"):
                conid += 1
                default: Quote = (
                    (None, None, None, 0.5) if unquoted_others else (1.0, 1.2, 1.1, 1.1)
                )
                bid, ask, last, close = (quotes or {}).get((strike, right), default)
                rows.append(
                    OptionContractRow(
                        snapshot_id=sid,
                        snapshot_ts_utc=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        market_date=market_date.isoformat(),
                        market_data_type=md_type,
                        underlying=ticker,
                        underlying_conid=4815,
                        underlying_price=spot,
                        option_conid=conid,
                        expiry=expiry,
                        dte=dte,
                        strike=strike,
                        moneyness_pct=(strike - spot) / spot * 100.0,
                        right=right,
                        bid=bid,
                        ask=ask,
                        last=last,
                        close=close,
                        volume=100,
                        open_interest=1000,
                        iv=0.30,
                        delta=-0.30 if right == "P" else 0.30,
                        gamma=0.02,
                        theta=-0.05,
                        vega=0.10,
                        greeks_source="IBKR_MODEL",
                    ).model_dump()
                )
    return pl.DataFrame(rows, schema=OPTION_CHAIN_POLARS_SCHEMA)


def _step(df: pl.DataFrame, meta: SnapshotMeta | None = None) -> BacktestStep:
    ts = datetime.fromisoformat(str(df["snapshot_ts_utc"][0]).replace("Z", "+00:00"))
    return BacktestStep(str(df["snapshot_id"][0]), ts, df, meta)


ENTRY_TS = _ts("2026-11-16T15:00:00")  # Mon 10:00 ET


def _entry() -> BacktestStep:
    return _step(_chain(ENTRY_TS, 102.0, ENTRY_QUOTES))


def _run(steps: list[BacktestStep], **kw: Any) -> Any:
    kw.setdefault("mode", "LEGS")
    if kw["mode"] == "LEGS":
        kw.setdefault("legs", LEGS)
    return run_bull_put_backtest(steps, ticker="NVDA", **kw)


# ------------------------------------------------------------------ entry
def test_entry_uses_mid_credit_and_reports_natural_slippage() -> None:
    r = _run([_entry()])
    assert r.entry.entry_cash_mid_dollars == pytest.approx(110.0)
    assert r.entry.entry_cash_natural_dollars == pytest.approx(90.0)
    assert r.entry.natural_slippage_dollars == pytest.approx(20.0)
    assert r.entry.max_loss_dollars == pytest.approx(390.0)
    assert r.steps[0].pnl_dollars == 0.0
    assert r.steps[0].status == "OK"  # SPOT_SOURCE_UNKNOWN is informational only
    assert r.steps[0].flags == ["SPOT_SOURCE_UNKNOWN"]
    assert r.notes == ["Only the entry snapshot is available; there are no later steps."]
    assert r.settlement.status == "NOT_REACHED"


# ------------------------------------------------------------------ hand-computed P&L
def test_winning_spread_mark_to_market() -> None:
    later = _chain(
        _ts("2026-11-20T18:00:00"),
        106.0,
        {(100.0, "P"): (0.95, 1.05, 1.0, 1.0), (95.0, "P"): (0.35, 0.45, 0.4, 0.4)},
    )
    step = _run([_entry(), _step(later)]).steps[1]
    # spread 1.00 - 0.40 = 0.60 -> P&L = 110 - 60 = +50; 50 / 390 = 12.82%
    assert step.spread_value_per_share == pytest.approx(0.60)
    assert step.pnl_dollars == pytest.approx(50.0)
    assert step.pnl_pct_of_max_risk == pytest.approx(12.82)
    assert step.short_dte == 28
    assert step.status == "OK"
    assert step.net_greeks is not None
    assert step.net_greeks.delta_shares == pytest.approx(0.0)  # -(-0.30) + (-0.30)


def test_losing_spread_mark_to_market() -> None:
    later = _chain(
        _ts("2026-11-20T18:00:00"),
        97.0,
        {(100.0, "P"): (3.90, 4.10, 4.0, 4.0), (95.0, "P"): (1.40, 1.60, 1.5, 1.5)},
    )
    step = _run([_entry(), _step(later)]).steps[1]
    # spread 4.00 - 1.50 = 2.50 -> P&L = 110 - 250 = -140; -35.90%
    assert step.pnl_dollars == pytest.approx(-140.0)
    assert step.pnl_pct_of_max_risk == pytest.approx(-35.9)


def test_qty_multiplier_scales_dollars_not_per_share_values() -> None:
    later = _chain(
        _ts("2026-11-20T18:00:00"),
        106.0,
        {(100.0, "P"): (0.95, 1.05, 1.0, 1.0), (95.0, "P"): (0.35, 0.45, 0.4, 0.4)},
    )
    r = _run([_entry(), _step(later)], qty=3)
    assert r.entry.entry_cash_mid_dollars == pytest.approx(330.0)
    assert r.steps[1].spread_value_per_share == pytest.approx(0.60)
    assert r.steps[1].pnl_dollars == pytest.approx(150.0)
    assert r.steps[1].pnl_pct_of_max_risk == pytest.approx(12.82)


# ------------------------------------------------------------------ settlement
def _settle_step(spot: float, when: str = "2026-12-18T21:30:00") -> BacktestStep:
    # After expiry the leg contracts are no longer listed; only the spot matters.
    return _step(_chain(_ts(when), spot, expiries=("2027-01-15",)))


def test_expired_worthless_settles_full_credit() -> None:
    r = _run([_entry(), _settle_step(104.0)])
    assert r.settlement.status == "SETTLED"
    assert r.settlement.spot == 104.0
    assert r.settlement.pnl_dollars == pytest.approx(110.0)
    assert r.settlement.pnl_pct_of_max_risk == pytest.approx(28.21)
    assert len(r.steps) == 1  # nothing is marked after the expiry close


def test_expired_in_the_money_settles_at_intrinsic() -> None:
    r = _run([_entry(), _settle_step(97.0)])
    # intrinsic 100 - 97 = 3.00 -> P&L = 110 - 300 = -190
    assert r.settlement.intrinsic_spread_per_share == pytest.approx(3.0)
    assert r.settlement.pnl_dollars == pytest.approx(-190.0)


def test_settlement_needs_a_closing_time_snapshot() -> None:
    before_settle_time = _settle_step(104.0, when="2026-12-18T21:05:00")  # 16:05 ET
    next_day = _settle_step(104.0, when="2026-12-21T15:00:00")
    r = _run([_entry(), before_settle_time, next_day])
    assert r.settlement.status == "UNRESOLVED"
    assert r.settlement.pnl_dollars is None


def test_settlement_ignores_close_sourced_spot() -> None:
    settle = _settle_step(104.0)
    meta = SnapshotMeta(
        snapshot_id=settle.snapshot_id,
        file_path="x.csv",
        snapshot_ts_utc="2026-12-18T21:30:00Z",
        market_date="2026-12-18",
        market_data_type=3,
        underlying_price=104.0,
        contract_count=10,
        expirations=["2027-01-15"],
        spot_source="CLOSE",
    )
    r = _run([_entry(), BacktestStep(settle.snapshot_id, settle.ts_utc, settle.df, meta)])
    assert r.settlement.status == "UNRESOLVED"


# ------------------------------------------------------------------ flags
def test_leg_outside_dte_window_is_flagged_and_not_priced() -> None:
    # 2026-11-25: the Dec-18 legs have 23 DTE; this snapshot only lists 51+ DTE (Jan-15).
    later = _step(_chain(_ts("2026-11-25T16:00:00"), 101.0, expiries=("2027-01-15",)))
    step = _run([_entry(), later]).steps[1]
    assert step.status == "INCOMPLETE"
    assert {m.status for m in step.legs} == {"MISSING_OUTSIDE_DTE_WINDOW"}
    assert "LEG_MISSING_OUTSIDE_DTE_WINDOW:100P" in step.flags
    assert step.pnl_dollars is None
    assert step.spread_value_per_share is None


def test_leg_not_listed_differs_from_outside_window() -> None:
    # Same expiry, strikes 90/100/110 ($10 grid): 95 is inside the range but not listed.
    later = _step(
        _chain(
            _ts("2026-11-20T18:00:00"),
            101.0,
            {(100.0, "P"): (1.9, 2.1, 2.0, 2.0)},
            strikes=(90.0, 100.0, 110.0),
        )
    )
    step = _run([_entry(), later]).steps[1]
    by_strike = {m.strike: m.status for m in step.legs}
    assert by_strike == {100.0: "PRICED", 95.0: "MISSING_NOT_LISTED"}
    assert "LEG_MISSING_NOT_LISTED:95P" in step.flags
    assert "FILTER_CHANGED" in step.flags
    assert step.status == "INCOMPLETE"


def test_leg_outside_strike_range() -> None:
    later = _step(_chain(_ts("2026-11-20T18:00:00"), 110.0, strikes=(100.0, 105.0, 110.0)))
    step = _run([_entry(), later]).steps[1]
    assert {m.strike: m.status for m in step.legs}[95.0] == "MISSING_OUTSIDE_STRIKE_RANGE"


def test_fallback_price_is_used_and_flagged() -> None:
    later = _chain(
        _ts("2026-11-20T18:00:00"),
        104.0,
        {(100.0, "P"): (1.40, 1.60, 1.5, 1.5), (95.0, "P"): (None, None, 0.70, 0.65)},
    )
    step = _run([_entry(), _step(later)]).steps[1]
    leg95 = next(m for m in step.legs if m.strike == 95.0)
    assert (leg95.price, leg95.price_source) == (0.70, "LAST")
    assert step.pnl_dollars == pytest.approx(110.0 - 80.0)  # spread 1.50 - 0.70
    assert "LEG_FALLBACK_PRICE" in step.flags
    assert step.status == "UNRELIABLE"


def test_quality_flags_low_coverage_frozen_off_session() -> None:
    later = _chain(
        _ts("2026-11-20T23:30:00"),  # 18:30 ET
        104.0,
        {(100.0, "P"): (1.40, 1.60, 1.5, 1.5), (95.0, "P"): (0.60, 0.80, 0.7, 0.7)},
        md_type=4,
        unquoted_others=True,
    )
    step = _run([_entry(), _step(later)]).steps[1]
    assert step.quote_coverage < MIN_QUOTE_COVERAGE
    assert {"LOW_QUOTE_COVERAGE", "FROZEN_DATA", "OFF_SESSION"} <= set(step.flags)
    assert step.status == "UNRELIABLE"
    assert step.pnl_dollars == pytest.approx(30.0)  # still computed, never hidden


def test_inverted_spread_value_is_flagged() -> None:
    later = _chain(
        _ts("2026-11-20T18:00:00"),
        104.0,
        {(100.0, "P"): (0.50, 0.60, 0.55, 0.55), (95.0, "P"): (0.90, 1.00, 0.95, 0.95)},
    )
    step = _run([_entry(), _step(later)]).steps[1]
    assert step.spread_value_per_share == pytest.approx(-0.40)
    assert "SPREAD_VALUE_OUT_OF_BOUNDS" in step.flags
    assert step.status == "UNRELIABLE"


def test_summary_counts_only_ok_steps() -> None:
    ok_step = _chain(
        _ts("2026-11-20T18:00:00"),
        106.0,
        {(100.0, "P"): (0.95, 1.05, 1.0, 1.0), (95.0, "P"): (0.35, 0.45, 0.4, 0.4)},
    )
    missing = _chain(_ts("2026-11-25T16:00:00"), 101.0, expiries=("2027-01-15",))
    r = _run([_entry(), _step(ok_step), _step(missing)])
    s = r.summary
    assert (s.steps_total, s.steps_ok, s.steps_incomplete) == (3, 2, 1)
    assert s.last_ok_pnl_dollars == pytest.approx(50.0)
    assert (s.min_ok_pnl_dollars, s.max_ok_pnl_dollars) == (0.0, 50.0)


# ------------------------------------------------------------------ modes & validation
def _auto_chain(ts: str, spot: float) -> pl.DataFrame:
    df = _build_synthetic_chain(spot=spot)
    t = _ts(ts)
    return df.with_columns(
        pl.lit(f"NVDA_{t.strftime('%Y%m%d_%H%M%S')}").alias("snapshot_id"),
        pl.lit(t.strftime("%Y-%m-%dT%H:%M:%SZ")).alias("snapshot_ts_utc"),
    )


def test_mode_b_auto_build_matches_mode_a_with_the_same_legs() -> None:
    steps = [
        _step(_auto_chain("2026-11-11T15:30:00", 100.0)),
        _step(_auto_chain("2026-11-11T18:30:00", 103.0)),
    ]
    auto = run_bull_put_backtest(steps, ticker="NVDA", mode="AUTO_BUILD")
    assert [leg.right for leg in auto.entry.legs] == ["P", "P"]
    assert auto.entry.legs[0].side == "SELL"
    assert auto.entry.rationale  # builder explanation kept
    legs = [
        StrategyLegInput(side=leg.side, right=leg.right, expiry=leg.expiry, strike=leg.strike)
        for leg in auto.entry.legs
    ]
    manual = run_bull_put_backtest(steps, ticker="NVDA", mode="LEGS", legs=legs)
    assert [s.model_dump() for s in manual.steps] == [s.model_dump() for s in auto.steps]
    assert manual.entry.entry_cash_mid_dollars == auto.entry.entry_cash_mid_dollars


def test_same_inputs_same_output() -> None:
    steps = [
        _step(_auto_chain("2026-11-11T15:30:00", 100.0)),
        _step(_auto_chain("2026-11-11T18:30:00", 97.0)),
    ]
    a = run_bull_put_backtest(steps, ticker="NVDA", mode="AUTO_BUILD")
    b = run_bull_put_backtest(steps, ticker="NVDA", mode="AUTO_BUILD")
    assert a.model_dump() == b.model_dump()
    repro = a.reproducibility
    assert repro.strategies_sha256 == strategies_source_sha256()
    assert len(repro.strategies_sha256) == 64
    assert repro.quality_thresholds == QUALITY_THRESHOLDS
    assert repro.step_snapshot_ids == [s.snapshot_id for s in steps]
    assert repro.strategy_lab_constants["target_dte"] == 38


def test_auto_build_without_a_valid_spread_raises() -> None:
    calls_only = _auto_chain("2026-11-11T15:30:00", 100.0).filter(pl.col("right") == "C")
    with pytest.raises(ValueError, match="Auto-build found no valid bull put spread"):
        run_bull_put_backtest([_step(calls_only)], ticker="NVDA", mode="AUTO_BUILD")


def test_mode_a_leg_missing_from_entry_snapshot_raises() -> None:
    legs = [
        StrategyLegInput(side="SELL", right="P", expiry=EXPIRY, strike=101.0),
        StrategyLegInput(side="BUY", right="P", expiry=EXPIRY, strike=95.0),
    ]
    with pytest.raises(ValueError, match="Contract not found.*101P"):
        _run([_entry()], legs=legs)


@pytest.mark.parametrize(
    ("legs", "message"),
    [
        ([("SELL", "P", 100.0, 1)], "exactly 2 legs"),
        ([("SELL", "C", 100.0, 1), ("BUY", "P", 95.0, 1)], "must be puts"),
        ([("SELL", "P", 100.0, 1), ("SELL", "P", 95.0, 1)], "one SELL leg and one BUY leg"),
        ([("SELL", "P", 100.0, 1), ("BUY", "P", 100.0, 1)], "different strikes"),
        ([("SELL", "P", 95.0, 1), ("BUY", "P", 100.0, 1)], "must be above the BUY put strike"),
        ([("SELL", "P", 100.0, 2), ("BUY", "P", 95.0, 1)], "quantities must match"),
    ],
)
def test_mode_a_validation_names_the_problem(
    legs: list[tuple[str, str, float, int]], message: str
) -> None:
    specs = [
        StrategyLegInput(side=s, right=r, expiry=EXPIRY, strike=k, qty=q)  # type: ignore[arg-type]
        for s, r, k, q in legs
    ]
    with pytest.raises(ValueError, match=message):
        validate_bull_put_legs(specs)


def test_mode_a_rejects_mixed_expiries_and_bad_quantity() -> None:
    legs = [
        StrategyLegInput(side="SELL", right="P", expiry=EXPIRY, strike=100.0),
        StrategyLegInput(side="BUY", right="P", expiry="2027-01-15", strike=95.0),
    ]
    with pytest.raises(ValueError, match="share one expiry"):
        validate_bull_put_legs(legs)
    with pytest.raises(ValueError, match="Quantity must be positive"):
        _run([_entry()], qty=0)


# ------------------------------------------------------------------ price-source parity
@pytest.mark.parametrize(
    "row",
    [
        {"bid": 2.0, "ask": 2.2, "mid": 2.1, "mark_price": 2.1, "last": 2.05, "close": 2.0},
        {"bid": None, "ask": 0.05, "mid": None, "mark_price": 0.04, "last": None, "close": 0.04},
        {"bid": 1.2, "ask": 1.0, "mid": 1.1, "mark_price": 1.1, "last": 1.0, "close": 1.0},
        {"bid": None, "ask": None, "mid": None, "mark_price": 0.7, "last": 0.7, "close": 0.65},
        {"bid": None, "ask": None, "mid": None, "mark_price": 0.65, "last": None, "close": 0.65},
        {"bid": None, "ask": None, "mid": None, "mark_price": None, "last": 0.3, "close": None},
        {"bid": None, "ask": None, "mid": None, "mark_price": 0.9, "last": None, "close": None},
        {"bid": None, "ask": None, "mid": None, "mark_price": None, "last": None, "close": None},
        {"bid": 0.0, "ask": 0.0, "mid": 0.0, "mark_price": 0.0, "last": 0.0, "close": 0.0},
        {
            "bid": float("nan"),
            "ask": 1.0,
            "mid": None,
            "mark_price": None,
            "last": None,
            "close": 0.9,
        },
    ],
)
def test_price_matches_strategy_lab_fallback_chain(row: dict[str, Any]) -> None:
    value, _source = resolve_price_with_source(row)
    assert value == pytest.approx(strategies._resolve_row_mid(row))


def test_price_source_labels() -> None:
    assert resolve_price_with_source({"bid": 2.0, "ask": 2.2})[1] == "BID_ASK"
    assert resolve_price_with_source({"mark_price": 0.7, "last": 0.7, "close": 0.6})[1] == "LAST"
    assert resolve_price_with_source({"mark_price": 0.6, "close": 0.6})[1] == "CLOSE"
    assert resolve_price_with_source({"mark_price": 0.9})[1] == "MARK"
    assert resolve_price_with_source({}) == (0.0, None)


# ------------------------------------------------------------------ loading from the repository
def _save(repo: CSVRepository, df: pl.DataFrame) -> None:
    repo.save_snapshot(str(df["underlying"][0]), df.to_dicts())


def test_loader_reads_in_time_order_without_writing(tmp_path: Path) -> None:
    repo = CSVRepository(StorageSettings(), root=tmp_path)
    t1, t2, t3 = (_ts(f"2026-11-1{d}T15:00:00") for d in (6, 7, 8))
    for t in (t3, t1, t2):
        _save(repo, _chain(t, 102.0, ENTRY_QUOTES))
    # list_snapshots() would rebuild (write) a missing index; the loader must not.
    repo.index_path.unlink()
    files_before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))

    steps, notes = load_backtest_steps(repo, "NVDA", "NVDA_20261117_150000")
    assert [s.snapshot_id for s in steps] == ["NVDA_20261117_150000", "NVDA_20261118_150000"]
    assert notes == ["Snapshot index unavailable; spot_source / collection_mode unknown."]
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*")) == files_before

    ended, _ = load_backtest_steps(
        repo, "NVDA", "NVDA_20261116_150000", end_snapshot_id="NVDA_20261117_150000"
    )
    assert [s.snapshot_id for s in ended] == ["NVDA_20261116_150000", "NVDA_20261117_150000"]
    with pytest.raises(ValueError, match="before the entry"):
        load_backtest_steps(
            repo, "NVDA", "NVDA_20261117_150000", end_snapshot_id="NVDA_20261116_150000"
        )


def test_loader_uses_index_metadata_when_present(tmp_path: Path) -> None:
    repo = CSVRepository(StorageSettings(), root=tmp_path)
    df = _chain(ENTRY_TS, 102.0, ENTRY_QUOTES)
    repo.save_snapshot("NVDA", df.to_dicts(), spot_source="LAST", collection_mode="MANUAL")
    index_before = repo.index_path.read_text(encoding="utf-8")
    steps, notes = load_backtest_steps(repo, "NVDA", "NVDA_20261116_150000")
    assert notes == []
    assert steps[0].meta is not None and steps[0].meta.spot_source == "LAST"
    r = run_bull_put_backtest(steps, ticker="NVDA", mode="LEGS", legs=LEGS)
    assert r.steps[0].flags == []
    assert repo.index_path.read_text(encoding="utf-8") == index_before
    assert json.loads(index_before)["tickers"]["NVDA"]["latest_snapshot_id"] == steps[0].snapshot_id


def test_loader_errors(tmp_path: Path) -> None:
    repo = CSVRepository(StorageSettings(), root=tmp_path)
    with pytest.raises(SnapshotNotFoundError, match="No snapshot history"):
        load_backtest_steps(repo, "NVDA", "NVDA_20261116_150000")
    _save(repo, _chain(ENTRY_TS, 102.0, ENTRY_QUOTES))
    with pytest.raises(SnapshotNotFoundError, match="Entry snapshot not found"):
        load_backtest_steps(repo, "NVDA", "NVDA_20261116_160000")
    with pytest.raises(InvalidTickerError):
        load_backtest_steps(repo, "../etc", "NVDA_20261116_150000")
