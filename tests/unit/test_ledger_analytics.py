"""Unit tests for pure Polars Trade Ledger analytics (`src/analytics/ledger`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

import polars as pl
import pytest

from src.analytics.ledger import (
    AggregatedLeg,
    CompiledLedgerData,
    LedgerAnalyticsReport,
    NormalizedExecution,
    build_ledger_report,
    classify_option_legs,
    cluster_option_episodes,
    compile_ledger_data,
    compute_window_start,
)
from src.analytics.ledger.classifier import (
    aggregate_legs,
    format_legs_position_label,
    format_legs_strike_summary,
    format_step_summary,
    format_utc_iso,
)
from src.storage.ledger_schema import (
    LEDGER_EXECUTION_COLUMNS,
    LEDGER_EXECUTION_POLARS_SCHEMA,
    LEDGER_POSITION_COLUMNS,
    LEDGER_POSITION_POLARS_SCHEMA,
    LedgerEventCode,
    LedgerSide,
)

AS_OF = datetime(2026, 11, 12, 20, 0, 0, tzinfo=UTC)


def _empty_execs() -> pl.DataFrame:
    return pl.DataFrame(schema=LEDGER_EXECUTION_POLARS_SCHEMA)


def _empty_positions() -> pl.DataFrame:
    return pl.DataFrame(schema=LEDGER_POSITION_POLARS_SCHEMA)


def _exec_df(rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return _empty_execs()
    normalized: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        ac = str(row.get("asset_class", "OPT"))
        mult = int(row.get("multiplier", 1 if ac == "STK" else 100))
        side = str(row["side"])
        qty = float(row["quantity"])
        price = float(row["price"])
        default_proceeds = (-qty * price * mult) if side == "BOT" else (qty * price * mult)
        item: dict[str, Any] = {
            "exec_id": row.get("exec_id", f"ex-{idx:04d}"),
            "ib_order_id": row.get("ib_order_id"),
            "conid": row.get("conid"),
            "underlying_conid": row.get("underlying_conid"),
            "symbol": row.get("symbol", row["underlying"]),
            "underlying": row["underlying"],
            "asset_class": ac,
            "expiry": row.get("expiry"),
            "strike": float(row["strike"]) if row.get("strike") is not None else None,
            "right": row.get("right"),
            "multiplier": mult,
            "side": side,
            "quantity": qty,
            "price": price,
            "proceeds": float(row.get("proceeds", default_proceeds)),
            "commission": float(row.get("commission", 0.0)),
            "realized_pnl_ib": row.get("realized_pnl_ib"),
            "open_close": row.get("open_close"),
            "event_code": row.get("event_code", "TRADE"),
            "currency": row.get("currency", "USD"),
            "fx_rate_to_base": float(row.get("fx_rate_to_base", 1.0)),
            "executed_at": row["executed_at"],
            "source": row.get("source", "FLEX_IMPORT"),
        }
        normalized.append({col: item[col] for col in LEDGER_EXECUTION_COLUMNS})
    return pl.DataFrame(normalized, schema=LEDGER_EXECUTION_POLARS_SCHEMA)


def _pos_df(rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return _empty_positions()
    normalized: list[dict[str, Any]] = []
    for row in rows:
        ac = str(row.get("asset_class", "OPT"))
        mult = int(row.get("multiplier", 1 if ac == "STK" else 100))
        item: dict[str, Any] = {
            "conid": row.get("conid"),
            "symbol": row.get("symbol", row["underlying"]),
            "underlying": row["underlying"],
            "asset_class": ac,
            "expiry": row.get("expiry"),
            "strike": float(row["strike"]) if row.get("strike") is not None else None,
            "right": row.get("right"),
            "multiplier": mult,
            "position": float(row["position"]),
            "avg_cost": float(row.get("avg_cost", 0.0)),
            "mark_price": float(row["mark_price"]) if row.get("mark_price") is not None else None,
            "market_value": (
                float(row["market_value"]) if row.get("market_value") is not None else None
            ),
            "unrealized_pnl": (
                float(row["unrealized_pnl"]) if row.get("unrealized_pnl") is not None else None
            ),
            "source": row.get("source", "FLEX_IMPORT"),
            "updated_at": row.get("updated_at", "2026-11-12T16:00:00Z"),
        }
        normalized.append({col: item[col] for col in LEDGER_POSITION_COLUMNS})
    return pl.DataFrame(normalized, schema=LEDGER_POSITION_POLARS_SCHEMA)


def _ts(days_ago: float, *, hours_offset: float = 0.0) -> str:
    dt = AS_OF - timedelta(days=days_ago) + timedelta(hours=hours_offset)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _leg(
    expiry: str,
    strike: float,
    right: Literal["C", "P"],
    side: Literal["BOT", "SLD"],
    qty: float = 1.0,
) -> AggregatedLeg:
    return AggregatedLeg(
        underlying="NVDA",
        expiry=expiry,
        strike=strike,
        right=right,
        side=side,
        quantity=qty,
        price=2.0,
        cashflow_ex_fee=-200.0 * qty if side == "BOT" else 200.0 * qty,
        fees=0.65,
        multiplier=100,
        conid=None,
        symbol=f"NVDA {expiry} {strike:g}{right}",
    )


def test_empty_ledger_realized_and_open_mtm() -> None:
    report = build_ledger_report(_empty_execs(), _empty_positions(), as_of=AS_OF)
    assert isinstance(report, LedgerAnalyticsReport)
    assert report.available_tickers == []
    assert report.campaigns == []
    assert report.breakdowns == []
    assert report.executions == []
    assert report.open_positions == []
    assert report.chart_points == []
    assert report.kpis.realized_pnl_ex_fee == 0.0
    assert report.kpis.stock_pnl_ex_fee == 0.0
    assert report.kpis.total_pnl_ex_fee == 0.0
    assert report.kpis.net_premium_ex_fee == 0.0
    assert report.kpis.fees == 0.0
    assert report.kpis.win_rate_pct is None
    assert report.kpis.closed_count == 0
    assert report.kpis.open_count == 0
    assert report.kpis.roll_count == 0
    assert report.kpis.unrealized_mtm_ex_fee is None

    mtm_report = build_ledger_report(
        _empty_execs(), _empty_positions(), view="open_mtm", as_of=AS_OF
    )
    assert mtm_report.open_positions == []
    assert mtm_report.kpis.unrealized_mtm_ex_fee is None


def test_option_strategy_classifier_covers_all_archetypes() -> None:
    assert classify_option_legs([_leg("20261218", 130, "P", "SLD")]) == "SHORT_PUT"
    assert classify_option_legs([_leg("20261218", 130, "P", "BOT")]) == "LONG_PUT"
    assert classify_option_legs([_leg("20261218", 140, "C", "BOT")]) == "LONG_CALL"
    assert (
        classify_option_legs([_leg("20261218", 140, "C", "SLD")], available_stk_shares=0.0)
        == "NAKED_CALL"
    )
    assert (
        classify_option_legs([_leg("20261218", 140, "C", "SLD")], available_stk_shares=100.0)
        == "COVERED_CALL"
    )
    assert (
        classify_option_legs([_leg("20261218", 135, "P", "SLD"), _leg("20261218", 130, "P", "BOT")])
        == "BULL_PUT_SPREAD"
    )
    assert (
        classify_option_legs([_leg("20261218", 130, "P", "SLD"), _leg("20261218", 135, "P", "BOT")])
        == "BEAR_PUT_SPREAD"
    )
    assert (
        classify_option_legs([_leg("20261218", 130, "C", "BOT"), _leg("20261218", 135, "C", "SLD")])
        == "BULL_CALL_SPREAD"
    )
    assert (
        classify_option_legs([_leg("20261218", 130, "C", "SLD"), _leg("20261218", 135, "C", "BOT")])
        == "BEAR_CALL_SPREAD"
    )
    assert (
        classify_option_legs([_leg("20261218", 140, "C", "SLD"), _leg("20270115", 140, "C", "BOT")])
        == "CALENDAR_SPREAD"
    )
    assert (
        classify_option_legs([_leg("20261218", 140, "C", "SLD"), _leg("20270115", 135, "C", "BOT")])
        == "DIAGONAL_SPREAD"
    )
    assert (
        classify_option_legs([_leg("20261218", 135, "P", "SLD"), _leg("20261218", 135, "C", "SLD")])
        == "STRADDLE"
    )
    assert (
        classify_option_legs([_leg("20261218", 125, "P", "SLD"), _leg("20261218", 145, "C", "SLD")])
        == "STRANGLE"
    )
    assert (
        classify_option_legs(
            [
                _leg("20261218", 120, "P", "BOT"),
                _leg("20261218", 125, "P", "SLD"),
                _leg("20261218", 145, "C", "SLD"),
                _leg("20261218", 150, "C", "BOT"),
            ]
        )
        == "IRON_CONDOR"
    )
    assert (
        classify_option_legs(
            [
                _leg("20261218", 125, "P", "BOT"),
                _leg("20261218", 135, "P", "SLD"),
                _leg("20261218", 135, "C", "SLD"),
                _leg("20261218", 145, "C", "BOT"),
            ]
        )
        == "IRON_BUTTERFLY"
    )
    assert (
        classify_option_legs(
            [
                _leg("20261218", 125, "P", "BOT", qty=1),
                _leg("20261218", 135, "P", "SLD", qty=2),
            ]
        )
        == "CUSTOM_COMBO"
    )


def test_bull_put_spread_shared_order_and_roll_down_out() -> None:
    execs = _exec_df(
        [
            # Open 2x 135/130 Bull Put Spread via shared ib_order_id ("ORD-1") 20d ago
            {
                "exec_id": "bps-1",
                "ib_order_id": "ORD-1",
                "underlying": "AVGO",
                "expiry": "20261120",
                "strike": 135.0,
                "right": "P",
                "side": "SLD",
                "quantity": 2.0,
                "price": 3.20,
                "commission": 1.30,
                "open_close": "O",
                "executed_at": _ts(20),
            },
            {
                "exec_id": "bps-2",
                "ib_order_id": "ORD-1",
                "underlying": "AVGO",
                "expiry": "20261120",
                "strike": 130.0,
                "right": "P",
                "side": "BOT",
                "quantity": 2.0,
                "price": 1.80,
                "commission": 1.30,
                "open_close": "O",
                "executed_at": _ts(20),
            },
            # Roll Down & Out to Dec18 125/120P via shared ib_order_id ("ORD-2") 10d ago
            {
                "exec_id": "bps-3",
                "ib_order_id": "ORD-2",
                "underlying": "AVGO",
                "expiry": "20261120",
                "strike": 135.0,
                "right": "P",
                "side": "BOT",
                "quantity": 2.0,
                "price": 4.50,
                "commission": 1.30,
                "open_close": "C",
                "executed_at": _ts(10),
            },
            {
                "exec_id": "bps-4",
                "ib_order_id": "ORD-2",
                "underlying": "AVGO",
                "expiry": "20261120",
                "strike": 130.0,
                "right": "P",
                "side": "SLD",
                "quantity": 2.0,
                "price": 2.60,
                "commission": 1.30,
                "open_close": "C",
                "executed_at": _ts(10),
            },
            {
                "exec_id": "bps-5",
                "ib_order_id": "ORD-2",
                "underlying": "AVGO",
                "expiry": "20261218",
                "strike": 125.0,
                "right": "P",
                "side": "SLD",
                "quantity": 2.0,
                "price": 3.80,
                "commission": 1.30,
                "open_close": "O",
                "executed_at": _ts(10),
            },
            {
                "exec_id": "bps-6",
                "ib_order_id": "ORD-2",
                "underlying": "AVGO",
                "expiry": "20261218",
                "strike": 120.0,
                "right": "P",
                "side": "BOT",
                "quantity": 2.0,
                "price": 1.90,
                "commission": 1.30,
                "open_close": "O",
                "executed_at": _ts(10),
            },
            # Close 125/120P spread 2d ago (<= 2s clustering without ib_order_id)
            {
                "exec_id": "bps-7",
                "ib_order_id": None,
                "underlying": "AVGO",
                "expiry": "20261218",
                "strike": 125.0,
                "right": "P",
                "side": "BOT",
                "quantity": 2.0,
                "price": 0.40,
                "commission": 1.30,
                "open_close": "C",
                "executed_at": _ts(2),
            },
            {
                "exec_id": "bps-8",
                "ib_order_id": None,
                "underlying": "AVGO",
                "expiry": "20261218",
                "strike": 120.0,
                "right": "P",
                "side": "SLD",
                "quantity": 2.0,
                "price": 0.10,
                "commission": 1.30,
                "open_close": "C",
                "executed_at": _ts(2, hours_offset=1.0 / 3600.0),
            },
        ]
    )

    report = build_ledger_report(execs, _empty_positions(), window="1M", as_of=AS_OF)
    assert len(report.campaigns) == 1
    camp = report.campaigns[0]
    assert camp.underlying == "AVGO"
    assert camp.strategy_type == "BULL_PUT_SPREAD"
    assert camp.status == "CLOSED"
    assert camp.roll_count == 1
    assert camp.contracts_total == pytest.approx(2.0)
    assert camp.contracts_open == pytest.approx(0.0)
    # Open net: +280; Roll: (-380 close + 380 open) = 0; Close: -60 => +220 ex-fee
    assert camp.net_premium_ex_fee == pytest.approx(220.0)
    assert camp.realized_pnl_ex_fee == pytest.approx(220.0)
    # Fees kept strictly sidecar: 8 legs * 1.30 = 10.40
    assert camp.fees == pytest.approx(10.40)
    assert [s.action for s in camp.steps] == ["OPEN", "ROLL", "CLOSE"]
    assert camp.steps[1].roll_label == "Roll Down & Out"
    assert camp.steps[1].realized_pnl_ex_fee == pytest.approx(-100.0)
    assert camp.steps[2].realized_pnl_ex_fee == pytest.approx(320.0)

    assert report.kpis.realized_pnl_ex_fee == pytest.approx(220.0)
    assert report.kpis.net_premium_ex_fee == pytest.approx(220.0)
    assert report.kpis.fees == pytest.approx(10.40)
    assert report.kpis.win_rate_pct == pytest.approx(100.0)
    assert report.kpis.roll_count == 1


def test_covered_call_3_hop_roll_chain_and_include_stock_gain_toggle() -> None:
    execs = _exec_df(
        [
            # Buy 100 NVDA @ $125.00 (40d ago)
            {
                "exec_id": "cc-stk-buy",
                "underlying": "NVDA",
                "asset_class": "STK",
                "side": "BOT",
                "quantity": 100.0,
                "price": 125.0,
                "commission": 1.00,
                "open_close": "O",
                "executed_at": _ts(40),
            },
            # Hop 1: STO 1 130C @ $3.00 (39d ago)
            {
                "exec_id": "cc-1",
                "underlying": "NVDA",
                "expiry": "20261030",
                "strike": 130.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 3.00,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(39),
            },
            # Roll 1 (1-day delayed roll, 4h gap <= 24h, missing ib_order_id): $130C -> $135C
            {
                "exec_id": "cc-2",
                "underlying": "NVDA",
                "expiry": "20261030",
                "strike": 130.0,
                "right": "C",
                "side": "BOT",
                "quantity": 1.0,
                "price": 4.00,
                "commission": 0.65,
                "open_close": "C",
                "executed_at": _ts(25),
            },
            {
                "exec_id": "cc-3",
                "underlying": "NVDA",
                "expiry": "20261106",
                "strike": 135.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 5.50,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(25, hours_offset=4.0),
            },
            # Roll 2 (same-order roll): $135C -> $140C
            {
                "exec_id": "cc-4",
                "ib_order_id": "ROLL-2",
                "underlying": "NVDA",
                "expiry": "20261106",
                "strike": 135.0,
                "right": "C",
                "side": "BOT",
                "quantity": 1.0,
                "price": 3.50,
                "commission": 0.65,
                "open_close": "C",
                "executed_at": _ts(15),
            },
            {
                "exec_id": "cc-5",
                "ib_order_id": "ROLL-2",
                "underlying": "NVDA",
                "expiry": "20261113",
                "strike": 140.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 4.50,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(15),
            },
            # Assignment at $140C (5d ago)
            {
                "exec_id": "cc-6",
                "underlying": "NVDA",
                "expiry": "20261113",
                "strike": 140.0,
                "right": "C",
                "side": "BOT",
                "quantity": 1.0,
                "price": 0.0,
                "proceeds": 0.0,
                "commission": 0.0,
                "open_close": "C",
                "event_code": "ASSIGNED",
                "executed_at": _ts(5),
            },
            {
                "exec_id": "cc-stk-assign",
                "underlying": "NVDA",
                "asset_class": "STK",
                "side": "SLD",
                "quantity": 100.0,
                "price": 140.0,
                "commission": 0.50,
                "open_close": "C",
                "event_code": "ASSIGNED",
                "executed_at": _ts(5),
            },
        ]
    )

    excl = build_ledger_report(
        execs, _empty_positions(), window="ALL", include_stock_gain=False, as_of=AS_OF
    )
    assert len(excl.campaigns) == 1
    c_excl = excl.campaigns[0]
    assert c_excl.strategy_type == "COVERED_CALL"
    assert c_excl.status == "ASSIGNED"
    assert c_excl.roll_count == 2
    assert c_excl.net_premium_ex_fee == pytest.approx(550.0)
    assert c_excl.realized_pnl_ex_fee == pytest.approx(550.0)
    assert c_excl.stock_realized_pnl_ex_fee == pytest.approx(0.0)
    assert c_excl.fees == pytest.approx(5 * 0.65)
    assert [s.roll_label for s in c_excl.steps if s.action == "ROLL"] == [
        "Roll Up & Out",
        "Roll Up & Out",
    ]
    assert excl.kpis.stock_pnl_ex_fee == pytest.approx(0.0)
    assert excl.kpis.total_pnl_ex_fee == pytest.approx(550.0)

    incl = build_ledger_report(
        execs, _empty_positions(), window="ALL", include_stock_gain=True, as_of=AS_OF
    )
    assert len(incl.campaigns) == 1
    c_incl = incl.campaigns[0]
    assert c_incl.strategy_type == "COVERED_CALL"
    assert c_incl.realized_pnl_ex_fee == pytest.approx(550.0)
    assert c_incl.stock_realized_pnl_ex_fee == pytest.approx(1500.0)
    assert c_incl.net_premium_ex_fee == pytest.approx(550.0)
    assert c_incl.fees == pytest.approx(5 * 0.65 + 1.00 + 0.50)
    assert incl.kpis.realized_pnl_ex_fee == pytest.approx(550.0)
    assert incl.kpis.stock_pnl_ex_fee == pytest.approx(1500.0)
    assert incl.kpis.total_pnl_ex_fee == pytest.approx(2050.0)
    assert incl.kpis.net_premium_ex_fee == pytest.approx(550.0)


def test_covered_call_reconstructs_shares_from_open_positions_anchor() -> None:
    # No STK execution in history (shares bought >365d ago), but `open_positions` holds 100 shs.
    execs = _exec_df(
        [
            {
                "exec_id": "opt-only-1",
                "underlying": "PLTR",
                "expiry": "20261218",
                "strike": 55.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 2.40,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(5),
            }
        ]
    )
    pos = _pos_df(
        [
            {
                "underlying": "PLTR",
                "asset_class": "STK",
                "position": 100.0,
                "avg_cost": 42.0,
                "mark_price": 52.0,
            },
            {
                "conid": 88001,
                "underlying": "PLTR",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 55.0,
                "right": "C",
                "position": -1.0,
                "avg_cost": 2.40,
                "mark_price": 1.90,
            },
        ]
    )
    rep = build_ledger_report(execs, pos, window="1M", as_of=AS_OF)
    assert len(rep.campaigns) == 1
    assert rep.campaigns[0].strategy_type == "COVERED_CALL"
    assert rep.campaigns[0].status == "OPEN"


def test_strict_1m_window_filtering_and_roll_gap_cutoff() -> None:
    execs = _exec_df(
        [
            # Opened 45d ago (outside 1M = 30d window): +$500 cr, $1.30 fee
            {
                "exec_id": "w-open",
                "underlying": "CRDO",
                "expiry": "20261120",
                "strike": 40.0,
                "right": "P",
                "side": "SLD",
                "quantity": 2.0,
                "price": 2.50,
                "commission": 1.30,
                "open_close": "O",
                "executed_at": _ts(45),
            },
            # Closed 10d ago (inside 1M window): -$120 db, $0.70 fee -> +$380 realized in 1M
            {
                "exec_id": "w-close",
                "underlying": "CRDO",
                "expiry": "20261120",
                "strike": 40.0,
                "right": "P",
                "side": "BOT",
                "quantity": 2.0,
                "price": 0.60,
                "commission": 0.70,
                "open_close": "C",
                "executed_at": _ts(10),
            },
            # New trade opened 36h after close (> 24h roll_max_gap_hours) -> separate campaign!
            {
                "exec_id": "w-new",
                "underlying": "CRDO",
                "expiry": "20261218",
                "strike": 42.0,
                "right": "P",
                "side": "SLD",
                "quantity": 2.0,
                "price": 1.50,
                "commission": 0.0,
                "open_close": "O",
                "executed_at": _ts(10, hours_offset=36.0),
            },
        ]
    )

    rep_1m = build_ledger_report(execs, _empty_positions(), window="1M", as_of=AS_OF)
    assert rep_1m.window_start == _ts(30)
    assert len(rep_1m.campaigns) == 2  # No false roll stitching across 36h > 24h!
    assert all(c.roll_count == 0 for c in rep_1m.campaigns)

    closed_camp = next(c for c in rep_1m.campaigns if c.status == "CLOSED")
    # Only the 10d-ago close step is inside 1M!
    assert len(closed_camp.steps) == 1
    assert closed_camp.net_premium_ex_fee == pytest.approx(-120.0)
    assert closed_camp.realized_pnl_ex_fee == pytest.approx(380.0)
    assert closed_camp.fees == pytest.approx(0.70)

    # KPIs for 1M: -120 (close) + 300 (new open) = +180 net premium; +380 realized; 0.70 fees
    assert rep_1m.kpis.net_premium_ex_fee == pytest.approx(180.0)
    assert rep_1m.kpis.realized_pnl_ex_fee == pytest.approx(380.0)
    assert rep_1m.kpis.fees == pytest.approx(0.70)
    assert rep_1m.kpis.closed_count == 1
    assert rep_1m.kpis.open_count == 1

    # Compare against ALL window: +500 - 120 + 300 = +680 net premium, 2.00 fees
    rep_all = build_ledger_report(execs, _empty_positions(), window="ALL", as_of=AS_OF)
    assert rep_all.kpis.net_premium_ex_fee == pytest.approx(680.0)
    assert rep_all.kpis.realized_pnl_ex_fee == pytest.approx(380.0)
    assert rep_all.kpis.fees == pytest.approx(2.00)
    assert compute_window_start("YTD", AS_OF) == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def test_open_mtm_separation_live_marks_flex_and_missing_marks() -> None:
    execs = _exec_df(
        [
            # Closed winning put (+200 realized)
            {
                "exec_id": "m-1",
                "underlying": "NVDA",
                "expiry": "20261106",
                "strike": 130.0,
                "right": "P",
                "side": "SLD",
                "quantity": 1.0,
                "price": 2.00,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(15),
            },
            {
                "exec_id": "m-2",
                "underlying": "NVDA",
                "expiry": "20261106",
                "strike": 130.0,
                "right": "P",
                "side": "BOT",
                "quantity": 1.0,
                "price": 0.0,
                "proceeds": 0.0,
                "commission": 0.0,
                "open_close": "C",
                "event_code": "EXPIRED",
                "executed_at": _ts(6),
            },
            # Open FIFO multi-lot calls: 1 @ $4.00, 1 @ $6.00; close 1 @ $3.00 -> FIFO realizes +100
            # Remaining open short call: -1 @ $6.00 (cost_basis = -600.0)
            {
                "exec_id": "m-3",
                "underlying": "NVDA",
                "conid": 700001,
                "expiry": "20261218",
                "strike": 145.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 4.00,
                "commission": 0.65,
                "open_close": None,  # GATEWAY_LIVE style
                "executed_at": _ts(8),
            },
            {
                "exec_id": "m-4",
                "underlying": "NVDA",
                "conid": 700001,
                "expiry": "20261218",
                "strike": 145.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 6.00,
                "commission": 0.65,
                "open_close": None,
                "executed_at": _ts(7),
            },
            {
                "exec_id": "m-5",
                "underlying": "NVDA",
                "conid": 700001,
                "expiry": "20261218",
                "strike": 145.0,
                "right": "C",
                "side": "BOT",
                "quantity": 1.0,
                "price": 3.00,
                "commission": 0.65,
                "open_close": None,
                "executed_at": _ts(4),
            },
            # Second open option WITHOUT mark anywhere -> NONE
            {
                "exec_id": "m-6",
                "underlying": "AVGO",
                "expiry": "20261218",
                "strike": 170.0,
                "right": "P",
                "side": "BOT",
                "quantity": 1.0,
                "price": 3.50,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(3),
            },
        ]
    )
    pos = _pos_df(
        [
            {
                "conid": 700001,
                "underlying": "NVDA",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 145.0,
                "right": "C",
                "position": -1.0,
                "avg_cost": 6.00,
                "mark_price": 5.20,
            }
        ]
    )

    # View 1: strictly realized, no unrealized contamination
    v1 = build_ledger_report(execs, pos, view="realized", live_marks={700001: 4.50}, as_of=AS_OF)
    assert v1.open_positions == []
    assert v1.kpis.unrealized_mtm_ex_fee is None
    assert v1.kpis.realized_pnl_ex_fee == pytest.approx(300.0)  # +200 (expired) + 100 (FIFO)

    # View 2 with live mark override (4.50 beats Flex 5.20)
    v2_live = build_ledger_report(
        execs, pos, view="open_mtm", live_marks={700001: 4.50}, as_of=AS_OF
    )
    assert v2_live.kpis.realized_pnl_ex_fee == pytest.approx(300.0)
    assert len(v2_live.open_positions) == 2

    avgo_pos = next(p for p in v2_live.open_positions if p.underlying == "AVGO")
    assert avgo_pos.mark_price is None
    assert avgo_pos.mark_source == "NONE"
    assert avgo_pos.market_value is None
    assert avgo_pos.unrealized_pnl_ex_fee is None
    assert avgo_pos.cost_basis_ex_fee == pytest.approx(350.0)

    nvda_pos = next(p for p in v2_live.open_positions if p.underlying == "NVDA")
    assert nvda_pos.quantity == pytest.approx(-1.0)
    assert nvda_pos.avg_cost_price == pytest.approx(6.00)
    assert nvda_pos.cost_basis_ex_fee == pytest.approx(-600.0)
    assert nvda_pos.mark_price == pytest.approx(4.50)
    assert nvda_pos.mark_source == "LIVE_GATEWAY"
    assert nvda_pos.market_value == pytest.approx(-450.0)
    assert nvda_pos.unrealized_pnl_ex_fee == pytest.approx(150.0)
    assert v2_live.kpis.unrealized_mtm_ex_fee == pytest.approx(150.0)

    # Without live_marks, falls back to FLEX_MARK (5.20 -> +80 MTM)
    v2_flex = build_ledger_report(execs, pos, view="open_mtm", live_marks=None, as_of=AS_OF)
    nvda_flex = next(p for p in v2_flex.open_positions if p.underlying == "NVDA")
    assert nvda_flex.mark_source == "FLEX_MARK"
    assert nvda_flex.unrealized_pnl_ex_fee == pytest.approx(80.0)

    # When ALL open positions lack marks, unrealized_mtm_ex_fee is None
    v2_nomark = build_ledger_report(
        execs.filter(pl.col("underlying") == "AVGO"),
        _empty_positions(),
        view="open_mtm",
        as_of=AS_OF,
    )
    assert v2_nomark.kpis.unrealized_mtm_ex_fee is None


def test_group_by_strategy_ticker_executions_and_chart_points() -> None:
    execs = _exec_df(
        [
            {
                "exec_id": "g-1",
                "underlying": "NVDA",
                "expiry": "20261106",
                "strike": 130.0,
                "right": "P",
                "side": "SLD",
                "quantity": 1.0,
                "price": 3.00,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(12),
            },
            {
                "exec_id": "g-2",
                "underlying": "NVDA",
                "expiry": "20261106",
                "strike": 130.0,
                "right": "P",
                "side": "BOT",
                "quantity": 1.0,
                "price": 1.00,
                "commission": 0.65,
                "open_close": "C",
                "executed_at": _ts(9),
            },
            {
                "exec_id": "g-3",
                "underlying": "PLTR",
                "expiry": "20261106",
                "strike": 40.0,
                "right": "P",
                "side": "BOT",
                "quantity": 1.0,
                "price": 2.00,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(8),
            },
            {
                "exec_id": "g-4",
                "underlying": "PLTR",
                "expiry": "20261106",
                "strike": 40.0,
                "right": "P",
                "side": "SLD",
                "quantity": 1.0,
                "price": 0.0,
                "proceeds": 0.0,
                "commission": 0.0,
                "open_close": "C",
                "event_code": "EXPIRED",
                "executed_at": _ts(6),
            },
        ]
    )

    by_strat = build_ledger_report(
        execs, _empty_positions(), window="1M", group_by="strategy", as_of=AS_OF
    )
    assert by_strat.available_tickers == ["NVDA", "PLTR"]
    strat_map = {b.key: b for b in by_strat.breakdowns}
    assert set(strat_map) == {"SHORT_PUT", "LONG_PUT"}
    assert strat_map["SHORT_PUT"].win_count == 1
    assert strat_map["SHORT_PUT"].loss_count == 0
    assert strat_map["SHORT_PUT"].realized_pnl_ex_fee == pytest.approx(200.0)
    assert strat_map["LONG_PUT"].win_count == 0
    assert strat_map["LONG_PUT"].loss_count == 1
    assert strat_map["LONG_PUT"].realized_pnl_ex_fee == pytest.approx(-200.0)

    by_ticker = build_ledger_report(
        execs, _empty_positions(), window="1M", group_by="ticker", as_of=AS_OF
    )
    tick_map = {b.key: b for b in by_ticker.breakdowns}
    assert set(tick_map) == {"NVDA", "PLTR"}
    assert tick_map["NVDA"].realized_pnl_ex_fee == pytest.approx(200.0)
    assert tick_map["PLTR"].realized_pnl_ex_fee == pytest.approx(-200.0)

    # Chart points accumulate to exact KPIs
    assert len(by_ticker.chart_points) == 4
    assert by_ticker.chart_points[-1].cumulative_realized_ex_fee == pytest.approx(0.0)
    assert by_ticker.chart_points[-1].cumulative_premium_ex_fee == pytest.approx(0.0)
    assert sum(p.fees for p in by_ticker.chart_points) == pytest.approx(1.95)

    # Single-ticker filter keeps `available_tickers` complete while filtering data
    nvda_execs = build_ledger_report(
        execs,
        _empty_positions(),
        window="1M",
        ticker="nvda",
        group_by="executions",
        as_of=AS_OF,
    )
    assert nvda_execs.ticker == "NVDA"
    assert nvda_execs.available_tickers == ["NVDA", "PLTR"]
    assert len(nvda_execs.executions) == 2
    assert nvda_execs.kpis.realized_pnl_ex_fee == pytest.approx(200.0)


def test_covered_stock_open_mtm_add_trim_exercise_and_orphan_close() -> None:
    execs = _exec_df(
        [
            # Buy 100 NVDA @ 120.00 + open 1 Covered Call 135C @ 3.00 (still OPEN)
            {
                "exec_id": "s-1",
                "underlying": "NVDA",
                "asset_class": "STK",
                "side": "BOT",
                "quantity": 100.0,
                "price": 120.0,
                "commission": 1.00,
                "open_close": "O",
                "executed_at": _ts(10),
            },
            {
                "exec_id": "o-1",
                "underlying": "NVDA",
                "conid": 701001,
                "expiry": "20261218",
                "strike": 135.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 3.00,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(10),
            },
            # Scale-in ADD + TRIM + EXERCISE on AVGO Long Call
            {
                "exec_id": "a-1",
                "underlying": "AVGO",
                "expiry": "20261120",
                "strike": 170.0,
                "right": "C",
                "side": "BOT",
                "quantity": 1.0,
                "price": 4.00,
                "commission": 0.0,
                "open_close": "O",
                "executed_at": _ts(9),
            },
            {
                "exec_id": "a-2",
                "underlying": "AVGO",
                "expiry": "20261120",
                "strike": 170.0,
                "right": "C",
                "side": "BOT",
                "quantity": 1.0,
                "price": 4.00,
                "commission": 0.0,
                "open_close": "O",
                "executed_at": _ts(8),
            },
            {
                "exec_id": "a-3",
                "underlying": "AVGO",
                "expiry": "20261120",
                "strike": 170.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 6.00,
                "commission": 0.0,
                "open_close": "C",
                "executed_at": _ts(5),
            },
            {
                "exec_id": "a-4",
                "underlying": "AVGO",
                "expiry": "20261120",
                "strike": 170.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 0.0,
                "proceeds": 0.0,
                "commission": 0.0,
                "open_close": "C",
                "event_code": "EXERCISED",
                "executed_at": _ts(2),
            },
            # Pre-window orphan close on PLTR (opened >365d ago, closed inside 1M)
            {
                "exec_id": "orph-1",
                "underlying": "PLTR",
                "expiry": "20261106",
                "strike": 45.0,
                "right": "P",
                "side": "BOT",
                "quantity": 1.0,
                "price": 0.50,
                "commission": 0.65,
                "realized_pnl_ib": 150.0,
                "open_close": "C",
                "executed_at": _ts(4),
            },
        ]
    )
    pos = _pos_df(
        [
            {
                "conid": 4815747,
                "underlying": "NVDA",
                "asset_class": "STK",
                "position": 100.0,
                "avg_cost": 120.0,
                "mark_price": 132.0,
            },
            {
                "conid": 701001,
                "underlying": "NVDA",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 135.0,
                "right": "C",
                "position": -1.0,
                "avg_cost": 3.00,
                "mark_price": 1.50,
            },
        ]
    )

    # Open MTM without stock gain -> only OPT position (+150)
    mtm_no_stk = build_ledger_report(
        execs, pos, window="1M", view="open_mtm", include_stock_gain=False, as_of=AS_OF
    )
    assert len(mtm_no_stk.open_positions) == 1
    assert mtm_no_stk.open_positions[0].asset_class == "OPT"
    assert mtm_no_stk.kpis.unrealized_mtm_ex_fee == pytest.approx(150.0)

    # Open MTM with stock gain -> OPT (+150) + covered STK (+1200) = +1350
    mtm_with_stk = build_ledger_report(
        execs, pos, window="1M", view="open_mtm", include_stock_gain=True, as_of=AS_OF
    )
    assert len(mtm_with_stk.open_positions) == 2
    stk_pos = next(p for p in mtm_with_stk.open_positions if p.asset_class == "STK")
    assert stk_pos.strategy_type == "COVERED_CALL"
    assert stk_pos.unrealized_pnl_ex_fee == pytest.approx(1200.0)
    assert mtm_with_stk.kpis.unrealized_mtm_ex_fee == pytest.approx(1350.0)

    # Verify AVGO ADD + TRIM + EXERCISE and PLTR orphan close (did NOT open phantom short!)
    avgo_camp = next(c for c in mtm_no_stk.campaigns if c.underlying == "AVGO")
    assert [s.action for s in avgo_camp.steps] == ["OPEN", "ADD", "TRIM", "EXERCISE"]
    assert avgo_camp.contracts_total == pytest.approx(2.0)
    assert avgo_camp.contracts_open == pytest.approx(0.0)
    assert avgo_camp.realized_pnl_ex_fee == pytest.approx(-200.0)  # (+200 trim) + (-400 exercised)
    pltr_camp = next(c for c in mtm_no_stk.campaigns if c.underlying == "PLTR")
    assert pltr_camp.strategy_type == "SHORT_PUT"
    assert pltr_camp.steps[0].summary.startswith("BTC ")
    assert mtm_no_stk.kpis.realized_pnl_ex_fee == pytest.approx(-50.0)  # -200 + 150 orphan
    assert compute_window_start("1W", AS_OF) == AS_OF - timedelta(days=7)
    assert compute_window_start("3M", AS_OF) == AS_OF - timedelta(days=90)
    assert compute_window_start("1Y", AS_OF) == AS_OF - timedelta(days=365)
    assert compute_window_start("ALL", AS_OF) is None


def _norm_opt_exec(
    exec_id: str,
    *,
    ib_order_id: str | None,
    strike: float,
    right: Literal["C", "P"],
    side: LedgerSide,
    dt: datetime,
    seq_index: int,
    event_code: LedgerEventCode = "TRADE",
) -> NormalizedExecution:
    return NormalizedExecution(
        exec_id=exec_id,
        ib_order_id=ib_order_id,
        conid=None,
        underlying_conid=None,
        symbol=f"NVDA 20261218 {strike:g}{right}",
        underlying="NVDA",
        asset_class="OPT",
        expiry="20261218",
        strike=strike,
        right=right,
        multiplier=100,
        side=side,
        quantity=1.0,
        price=2.0,
        proceeds_ex_fee=-200.0 if side == "BOT" else 200.0,
        commission_base=0.65,
        realized_pnl_ib=None,
        open_close="O" if event_code == "TRADE" else "C",
        event_code=event_code,
        executed_at=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        executed_at_dt=dt,
        source="FLEX_IMPORT",
        seq_index=seq_index,
    )


def test_cluster_option_episodes_merges_simultaneous_distinct_orders_and_keeps_partial_fills() -> (
    None
):
    # 1. Two simultaneous spread legs (1.0s apart <= 2.0s) with DISTINCT ib_order_ids
    #    -> cluster into 1 BULL_PUT_SPREAD campaign
    execs = _exec_df(
        [
            {
                "exec_id": "multi-1",
                "ib_order_id": "ORD-PUT-1",
                "underlying": "NVDA",
                "expiry": "20261218",
                "strike": 135.0,
                "right": "P",
                "side": "SLD",
                "quantity": 1.0,
                "price": 3.50,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(10),
            },
            {
                "exec_id": "multi-2",
                "ib_order_id": "ORD-PUT-2",
                "underlying": "NVDA",
                "expiry": "20261218",
                "strike": 130.0,
                "right": "P",
                "side": "BOT",
                "quantity": 1.0,
                "price": 2.00,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(10, hours_offset=1.0 / 3600.0),
            },
        ]
    )
    report = build_ledger_report(execs, _empty_positions(), window="1M", as_of=AS_OF)
    assert len(report.campaigns) == 1
    assert report.campaigns[0].strategy_type == "BULL_PUT_SPREAD"
    assert report.campaigns[0].contracts_total == pytest.approx(1.0)

    # 2. Single ib_order_id partial fills > 2.0s apart (10s) still stay in the same episode
    base_dt = AS_OF - timedelta(days=5)
    partial_same_order = [
        _norm_opt_exec(
            "p-1",
            ib_order_id="ORD-PARTIAL",
            strike=135.0,
            right="P",
            side="SLD",
            dt=base_dt,
            seq_index=0,
        ),
        _norm_opt_exec(
            "p-2",
            ib_order_id="ORD-PARTIAL",
            strike=135.0,
            right="P",
            side="SLD",
            dt=base_dt + timedelta(seconds=10),
            seq_index=1,
        ),
    ]
    partial_eps = cluster_option_episodes(partial_same_order)
    assert len(partial_eps) == 1
    assert [e.exec_id for e in partial_eps[0]] == ["p-1", "p-2"]

    # 3. Distinct ib_order_ids > 2.0s apart do NOT merge, and TRADE vs EXPIRED at 0s do NOT merge
    mixed_events = [
        _norm_opt_exec(
            "m-trade-1",
            ib_order_id="ORD-A",
            strike=135.0,
            right="P",
            side="SLD",
            dt=base_dt,
            seq_index=0,
            event_code="TRADE",
        ),
        _norm_opt_exec(
            "m-exp-1",
            ib_order_id="ORD-EXP",
            strike=130.0,
            right="P",
            side="BOT",
            dt=base_dt,
            seq_index=1,
            event_code="EXPIRED",
        ),
        _norm_opt_exec(
            "m-trade-2",
            ib_order_id="ORD-B",
            strike=140.0,
            right="C",
            side="BOT",
            dt=base_dt + timedelta(seconds=3),
            seq_index=2,
            event_code="TRADE",
        ),
    ]
    separated_eps = cluster_option_episodes(mixed_events)
    assert len(separated_eps) == 3
    assert [[e.exec_id for e in ep] for ep in separated_eps] == [
        ["m-trade-1"],
        ["m-exp-1"],
        ["m-trade-2"],
    ]


def test_orphan_close_strategy_classification_across_lifecycle_and_trade_closes() -> None:
    execs = _exec_df(
        [
            # 1) Expired Long Call (closing side="SLD", EXPIRED) -> LONG_CALL
            {
                "exec_id": "orph-lc",
                "underlying": "NVDA",
                "expiry": "20261106",
                "strike": 140.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 0.0,
                "proceeds": 0.0,
                "commission": 0.0,
                "realized_pnl_ib": -250.0,
                "open_close": "C",
                "event_code": "EXPIRED",
                "executed_at": _ts(6),
            },
            # 2) Assigned Short Put (closing side="BOT", ASSIGNED) -> SHORT_PUT
            {
                "exec_id": "orph-sp",
                "underlying": "AVGO",
                "expiry": "20261106",
                "strike": 160.0,
                "right": "P",
                "side": "BOT",
                "quantity": 1.0,
                "price": 0.0,
                "proceeds": 0.0,
                "commission": 0.0,
                "realized_pnl_ib": 180.0,
                "open_close": "C",
                "event_code": "ASSIGNED",
                "executed_at": _ts(5),
            },
            # 3) Expired Bull Call Spread (SLD lower + BOT upper, EXPIRED) -> BULL_CALL_SPREAD
            {
                "exec_id": "orph-bcs-low",
                "underlying": "CRDO",
                "expiry": "20261106",
                "strike": 40.0,
                "right": "C",
                "side": "SLD",
                "quantity": 2.0,
                "price": 0.0,
                "proceeds": 0.0,
                "commission": 0.0,
                "realized_pnl_ib": -300.0,
                "open_close": "C",
                "event_code": "EXPIRED",
                "executed_at": _ts(4),
            },
            {
                "exec_id": "orph-bcs-high",
                "underlying": "CRDO",
                "expiry": "20261106",
                "strike": 45.0,
                "right": "C",
                "side": "BOT",
                "quantity": 2.0,
                "price": 0.0,
                "proceeds": 0.0,
                "commission": 0.0,
                "realized_pnl_ib": 120.0,
                "open_close": "C",
                "event_code": "EXPIRED",
                "executed_at": _ts(4),
            },
            # 4) Assigned Short Call with >= 100 open shares (side="BOT", ASSIGNED) -> COVERED_CALL
            {
                "exec_id": "orph-cc",
                "underlying": "PLTR",
                "expiry": "20261106",
                "strike": 50.0,
                "right": "C",
                "side": "BOT",
                "quantity": 1.0,
                "price": 0.0,
                "proceeds": 0.0,
                "commission": 0.0,
                "realized_pnl_ib": 140.0,
                "open_close": "C",
                "event_code": "ASSIGNED",
                "executed_at": _ts(3),
            },
            # 5) Closing TRADE orphan on Long Call (side="SLD") -> LONG_CALL, verb stays "STC"
            {
                "exec_id": "orph-lc-trade",
                "underlying": "MSFT",
                "expiry": "20261120",
                "strike": 420.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 5.25,
                "commission": 0.65,
                "realized_pnl_ib": 125.0,
                "open_close": "C",
                "event_code": "TRADE",
                "executed_at": _ts(2),
            },
        ]
    )
    pos = _pos_df(
        [
            {
                "underlying": "PLTR",
                "asset_class": "STK",
                "position": 100.0,
                "avg_cost": 42.0,
                "mark_price": 51.0,
            }
        ]
    )

    report = build_ledger_report(execs, pos, window="1M", as_of=AS_OF)
    by_ticker = {c.underlying: c for c in report.campaigns}

    assert by_ticker["NVDA"].strategy_type == "LONG_CALL"
    assert by_ticker["NVDA"].status == "EXPIRED"
    assert by_ticker["NVDA"].steps[0].action == "EXPIRE"
    assert by_ticker["NVDA"].steps[0].summary.startswith("Expired ")
    assert by_ticker["NVDA"].realized_pnl_ex_fee == pytest.approx(-250.0)

    assert by_ticker["AVGO"].strategy_type == "SHORT_PUT"
    assert by_ticker["AVGO"].status == "ASSIGNED"
    assert by_ticker["AVGO"].steps[0].action == "ASSIGN"
    assert by_ticker["AVGO"].steps[0].summary.startswith("Assigned ")
    assert by_ticker["AVGO"].realized_pnl_ex_fee == pytest.approx(180.0)

    assert by_ticker["CRDO"].strategy_type == "BULL_CALL_SPREAD"
    assert by_ticker["CRDO"].status == "EXPIRED"
    assert by_ticker["CRDO"].contracts_total == pytest.approx(2.0)
    assert by_ticker["CRDO"].steps[0].action == "EXPIRE"
    assert by_ticker["CRDO"].steps[0].summary.startswith("Expired ")
    assert by_ticker["CRDO"].realized_pnl_ex_fee == pytest.approx(-180.0)

    assert by_ticker["PLTR"].strategy_type == "COVERED_CALL"
    assert by_ticker["PLTR"].status == "ASSIGNED"
    assert by_ticker["PLTR"].steps[0].action == "ASSIGN"
    assert by_ticker["PLTR"].steps[0].summary.startswith("Assigned ")
    assert by_ticker["PLTR"].realized_pnl_ex_fee == pytest.approx(140.0)

    assert by_ticker["MSFT"].strategy_type == "LONG_CALL"
    assert by_ticker["MSFT"].status == "CLOSED"
    assert by_ticker["MSFT"].steps[0].action == "CLOSE"
    assert by_ticker["MSFT"].steps[0].summary.startswith("STC ")
    assert by_ticker["MSFT"].steps[0].cashflow_ex_fee == pytest.approx(525.0)
    assert by_ticker["MSFT"].realized_pnl_ex_fee == pytest.approx(125.0)


def test_open_position_view_populates_conid_and_opened_at() -> None:
    execs = _exec_df(
        [
            # Two OPT lots for same NVDA contract at different timestamps -> earliest opened_at
            {
                "exec_id": "op-1",
                "underlying": "NVDA",
                "conid": 700111,
                "expiry": "20261218",
                "strike": 140.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 3.0,
                "open_close": "O",
                "executed_at": _ts(10),
            },
            {
                "exec_id": "op-2",
                "underlying": "NVDA",
                "conid": 700111,
                "expiry": "20261218",
                "strike": 140.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 3.5,
                "open_close": "O",
                "executed_at": _ts(4),
            },
            # Two STK lots for NVDA at different timestamps -> earliest executed_at
            {
                "exec_id": "stk-1",
                "underlying": "NVDA",
                "asset_class": "STK",
                "conid": 4815747,
                "side": "BOT",
                "quantity": 50.0,
                "price": 120.0,
                "open_close": "O",
                "executed_at": _ts(12),
            },
            {
                "exec_id": "stk-2",
                "underlying": "NVDA",
                "asset_class": "STK",
                "conid": 4815747,
                "side": "BOT",
                "quantity": 50.0,
                "price": 125.0,
                "open_close": "O",
                "executed_at": _ts(6),
            },
        ]
    )
    pos = _pos_df(
        [
            # Matched STK (overrides/confirms conid)
            {
                "conid": 4815747,
                "underlying": "NVDA",
                "asset_class": "STK",
                "position": 100.0,
                "avg_cost": 122.5,
                "mark_price": 130.0,
                "updated_at": _ts(1),
            },
            # Unmatched OPT position (no executions) -> uses pos_row.conid and pos_row.updated_at
            {
                "conid": 990001,
                "underlying": "AVGO",
                "asset_class": "OPT",
                "expiry": "20270115",
                "strike": 180.0,
                "right": "P",
                "position": -1.0,
                "avg_cost": 4.0,
                "mark_price": 3.2,
                "updated_at": "2026-11-11T18:30:00Z",
            },
            # Unmatched STK position with blank updated_at -> opened_at=None
            {
                "conid": 880002,
                "underlying": "PLTR",
                "asset_class": "STK",
                "position": 100.0,
                "avg_cost": 40.0,
                "mark_price": 45.0,
                "updated_at": "",
            },
        ]
    )

    rep = build_ledger_report(
        execs, pos, window="ALL", view="open_mtm", include_stock_gain=True, as_of=AS_OF
    )
    by_key = {p.position_key: p for p in rep.open_positions}

    nvda_opt = by_key["OPT:NVDA:20261218:140:C"]
    assert nvda_opt.conid == 700111
    assert nvda_opt.opened_at == _ts(10)

    nvda_stk = by_key["STK:NVDA"]
    assert nvda_stk.conid == 4815747
    assert nvda_stk.opened_at == _ts(12)

    avgo_opt = by_key["OPT:AVGO:20270115:180:P"]
    assert avgo_opt.conid == 990001
    assert avgo_opt.opened_at == "2026-11-11T18:30:00Z"

    pltr_stk = by_key["STK:PLTR"]
    assert pltr_stk.conid == 880002
    assert pltr_stk.opened_at is None


def test_closed_positions_not_resurrected_by_stale_flex_while_newer_gateway_kept() -> None:
    execs = _exec_df(
        [
            # NVDA OPT closed to zero at _ts(2)
            {
                "exec_id": "cl-nvda-opt-1",
                "underlying": "NVDA",
                "conid": 710001,
                "expiry": "20261218",
                "strike": 140.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 3.00,
                "open_close": "O",
                "executed_at": _ts(10),
            },
            {
                "exec_id": "cl-nvda-opt-2",
                "underlying": "NVDA",
                "conid": 710001,
                "expiry": "20261218",
                "strike": 140.0,
                "right": "C",
                "side": "BOT",
                "quantity": 1.0,
                "price": 1.00,
                "open_close": "C",
                "executed_at": _ts(2),
            },
            # NVDA STK closed to zero at _ts(2)
            {
                "exec_id": "cl-nvda-stk-1",
                "underlying": "NVDA",
                "asset_class": "STK",
                "conid": 4815747,
                "side": "BOT",
                "quantity": 100.0,
                "price": 120.0,
                "open_close": "O",
                "executed_at": _ts(10),
            },
            {
                "exec_id": "cl-nvda-stk-2",
                "underlying": "NVDA",
                "asset_class": "STK",
                "conid": 4815747,
                "side": "SLD",
                "quantity": 100.0,
                "price": 130.0,
                "open_close": "C",
                "executed_at": _ts(2),
            },
            # CRDO OPT & STK closed to zero at _ts(2) (stale GATEWAY_LIVE <= _ts(2))
            {
                "exec_id": "cl-crdo-opt-1",
                "underlying": "CRDO",
                "conid": 710002,
                "expiry": "20261218",
                "strike": 45.0,
                "right": "C",
                "side": "BOT",
                "quantity": 1.0,
                "price": 2.00,
                "open_close": "O",
                "executed_at": _ts(10),
            },
            {
                "exec_id": "cl-crdo-opt-2",
                "underlying": "CRDO",
                "conid": 710002,
                "expiry": "20261218",
                "strike": 45.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 3.00,
                "open_close": "C",
                "executed_at": _ts(2),
            },
            {
                "exec_id": "cl-crdo-stk-1",
                "underlying": "CRDO",
                "asset_class": "STK",
                "conid": 710003,
                "side": "BOT",
                "quantity": 50.0,
                "price": 40.0,
                "open_close": "O",
                "executed_at": _ts(10),
            },
            {
                "exec_id": "cl-crdo-stk-2",
                "underlying": "CRDO",
                "asset_class": "STK",
                "conid": 710003,
                "side": "SLD",
                "quantity": 50.0,
                "price": 44.0,
                "open_close": "C",
                "executed_at": _ts(2),
            },
            # AVGO OPT & STK closed at _ts(3), then reopened live via newer GATEWAY_LIVE (_ts(1))
            {
                "exec_id": "cl-avgo-opt-1",
                "underlying": "AVGO",
                "conid": 710004,
                "expiry": "20261218",
                "strike": 170.0,
                "right": "P",
                "side": "SLD",
                "quantity": 1.0,
                "price": 4.00,
                "open_close": "O",
                "executed_at": _ts(10),
            },
            {
                "exec_id": "cl-avgo-opt-2",
                "underlying": "AVGO",
                "conid": 710004,
                "expiry": "20261218",
                "strike": 170.0,
                "right": "P",
                "side": "BOT",
                "quantity": 1.0,
                "price": 1.50,
                "open_close": "C",
                "executed_at": _ts(3),
            },
            {
                "exec_id": "cl-avgo-stk-1",
                "underlying": "AVGO",
                "asset_class": "STK",
                "conid": 710005,
                "side": "BOT",
                "quantity": 50.0,
                "price": 150.0,
                "open_close": "O",
                "executed_at": _ts(10),
            },
            {
                "exec_id": "cl-avgo-stk-2",
                "underlying": "AVGO",
                "asset_class": "STK",
                "conid": 710005,
                "side": "SLD",
                "quantity": 50.0,
                "price": 160.0,
                "open_close": "C",
                "executed_at": _ts(3),
            },
        ]
    )
    pos = _pos_df(
        [
            # Stale FLEX_* rows (even with recent updated_at) must NOT resurrect closed contracts
            {
                "conid": 710001,
                "underlying": "NVDA",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 140.0,
                "right": "C",
                "position": -1.0,
                "avg_cost": 3.00,
                "mark_price": 2.00,
                "source": "FLEX_ACTIVITY_365",
                "updated_at": _ts(0),
            },
            {
                "conid": 4815747,
                "underlying": "NVDA",
                "asset_class": "STK",
                "position": 100.0,
                "avg_cost": 120.0,
                "mark_price": 135.0,
                "source": "FLEX_ACTIVITY_365",
                "updated_at": _ts(0),
            },
            # Stale GATEWAY_LIVE rows with updated_at <= latest_exec_at must NOT resurrect
            {
                "conid": 710002,
                "underlying": "CRDO",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 45.0,
                "right": "C",
                "position": 1.0,
                "avg_cost": 2.00,
                "mark_price": 2.50,
                "source": "GATEWAY_LIVE",
                "updated_at": _ts(2),
            },
            {
                "conid": 710003,
                "underlying": "CRDO",
                "asset_class": "STK",
                "position": 50.0,
                "avg_cost": 40.0,
                "mark_price": 45.0,
                "source": "GATEWAY_LIVE",
                "updated_at": _ts(2),
            },
            # Newer GATEWAY_LIVE rows (updated_at > latest_exec_at) ARE kept as live/reopened
            {
                "conid": 710004,
                "underlying": "AVGO",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 170.0,
                "right": "P",
                "position": -2.0,
                "avg_cost": 3.50,
                "mark_price": 3.00,
                "source": "GATEWAY_LIVE",
                "updated_at": _ts(1),
            },
            {
                "conid": 710005,
                "underlying": "AVGO",
                "asset_class": "STK",
                "position": 80.0,
                "avg_cost": 162.0,
                "mark_price": 165.0,
                "source": "GATEWAY_LIVE",
                "updated_at": _ts(1),
            },
        ]
    )

    rep_with_stk = build_ledger_report(
        execs, pos, window="ALL", view="open_mtm", include_stock_gain=True, as_of=AS_OF
    )
    by_key = {p.position_key: p for p in rep_with_stk.open_positions}
    assert set(by_key) == {"OPT:AVGO:20261218:170:P", "STK:AVGO"}

    avgo_opt = by_key["OPT:AVGO:20261218:170:P"]
    assert avgo_opt.quantity == pytest.approx(-2.0)
    assert avgo_opt.cost_basis_ex_fee == pytest.approx(-700.0)
    assert avgo_opt.unrealized_pnl_ex_fee == pytest.approx(100.0)

    avgo_stk = by_key["STK:AVGO"]
    assert avgo_stk.quantity == pytest.approx(80.0)
    assert avgo_stk.cost_basis_ex_fee == pytest.approx(12960.0)
    assert avgo_stk.unrealized_pnl_ex_fee == pytest.approx(240.0)

    rep_opt_only = build_ledger_report(
        execs, pos, window="ALL", view="open_mtm", include_stock_gain=False, as_of=AS_OF
    )
    assert [p.position_key for p in rep_opt_only.open_positions] == ["OPT:AVGO:20261218:170:P"]


def test_gateway_snapshot_suppresses_absent_older_lots_and_reconciles_resized_positions() -> None:
    execs = _exec_df(
        [
            # Absent older OPT & STK lots (opened _ts(8) < gw_*_snapshot_ts _ts(1)) -> suppressed
            {
                "exec_id": "gw-abs-opt",
                "underlying": "MSFT",
                "conid": 720001,
                "expiry": "20261218",
                "strike": 420.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 5.00,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(8),
            },
            {
                "exec_id": "gw-abs-stk",
                "underlying": "MSFT",
                "asset_class": "STK",
                "conid": 720002,
                "side": "BOT",
                "quantity": 100.0,
                "price": 400.0,
                "commission": 1.00,
                "open_close": "O",
                "executed_at": _ts(8),
            },
            # Absent newer OPT & STK lots (opened AFTER gw_*_snapshot_ts at _ts(1)) -> kept
            {
                "exec_id": "gw-new-opt",
                "underlying": "PLTR",
                "conid": 720003,
                "expiry": "20261218",
                "strike": 55.0,
                "right": "P",
                "side": "SLD",
                "quantity": 1.0,
                "price": 2.00,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(0, hours_offset=-1.0),
            },
            {
                "exec_id": "gw-new-stk",
                "underlying": "PLTR",
                "asset_class": "STK",
                "conid": 720004,
                "side": "BOT",
                "quantity": 25.0,
                "price": 50.0,
                "commission": 0.50,
                "open_close": "O",
                "executed_at": _ts(0, hours_offset=-1.0),
            },
            # Scaled-down same-direction OPT (-2 -> -1) & STK (+100 -> +40)
            {
                "exec_id": "gw-sd-opt",
                "underlying": "NVDA",
                "conid": 720005,
                "expiry": "20261218",
                "strike": 140.0,
                "right": "C",
                "side": "SLD",
                "quantity": 2.0,
                "price": 5.00,
                "commission": 1.30,
                "open_close": "O",
                "executed_at": _ts(5),
            },
            {
                "exec_id": "gw-sd-stk",
                "underlying": "NVDA",
                "asset_class": "STK",
                "conid": 720006,
                "side": "BOT",
                "quantity": 100.0,
                "price": 120.0,
                "commission": 1.00,
                "open_close": "O",
                "executed_at": _ts(5),
            },
            # Expanded same-direction OPT (-1 -> -3) & STK (+50 -> +120)
            {
                "exec_id": "gw-exp-opt",
                "underlying": "AVGO",
                "conid": 720007,
                "expiry": "20261218",
                "strike": 180.0,
                "right": "C",
                "side": "SLD",
                "quantity": 1.0,
                "price": 4.00,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(5),
            },
            {
                "exec_id": "gw-exp-stk",
                "underlying": "AVGO",
                "asset_class": "STK",
                "conid": 720008,
                "side": "BOT",
                "quantity": 50.0,
                "price": 160.0,
                "commission": 1.00,
                "open_close": "O",
                "executed_at": _ts(5),
            },
            # Flipped-direction OPT (-1 -> +2) & STK (+20 -> -30)
            {
                "exec_id": "gw-flip-opt",
                "underlying": "AMD",
                "conid": 720009,
                "expiry": "20261218",
                "strike": 150.0,
                "right": "P",
                "side": "SLD",
                "quantity": 1.0,
                "price": 3.00,
                "commission": 0.65,
                "open_close": "O",
                "executed_at": _ts(5),
            },
            {
                "exec_id": "gw-flip-stk",
                "underlying": "AMD",
                "asset_class": "STK",
                "conid": 720010,
                "side": "BOT",
                "quantity": 20.0,
                "price": 140.0,
                "commission": 1.00,
                "open_close": "O",
                "executed_at": _ts(5),
            },
        ]
    )
    pos = _pos_df(
        [
            {
                "conid": 720005,
                "underlying": "NVDA",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 140.0,
                "right": "C",
                "position": -1.0,
                "avg_cost": 5.00,
                "mark_price": 4.00,
                "source": "GATEWAY_LIVE",
                "updated_at": _ts(1),
            },
            {
                "conid": 720006,
                "underlying": "NVDA",
                "asset_class": "STK",
                "position": 40.0,
                "avg_cost": 120.0,
                "mark_price": 130.0,
                "source": "GATEWAY_LIVE",
                "updated_at": _ts(1),
            },
            {
                "conid": 720007,
                "underlying": "AVGO",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 180.0,
                "right": "C",
                "position": -3.0,
                "avg_cost": 4.50,
                "mark_price": 4.00,
                "source": "GATEWAY_LIVE",
                "updated_at": _ts(1),
            },
            {
                "conid": 720008,
                "underlying": "AVGO",
                "asset_class": "STK",
                "position": 120.0,
                "avg_cost": 165.0,
                "mark_price": 170.0,
                "source": "GATEWAY_LIVE",
                "updated_at": _ts(1),
            },
            {
                "conid": 720009,
                "underlying": "AMD",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 150.0,
                "right": "P",
                "position": 2.0,
                "avg_cost": 2.50,
                "mark_price": 3.50,
                "source": "GATEWAY_LIVE",
                "updated_at": _ts(1),
            },
            {
                "conid": 720010,
                "underlying": "AMD",
                "asset_class": "STK",
                "position": -30.0,
                "avg_cost": 145.0,
                "mark_price": 140.0,
                "source": "GATEWAY_LIVE",
                "updated_at": _ts(1),
            },
        ]
    )

    rep = build_ledger_report(
        execs, pos, window="ALL", view="open_mtm", include_stock_gain=True, as_of=AS_OF
    )
    by_key = {p.position_key: p for p in rep.open_positions}

    assert "OPT:MSFT:20261218:420:C" not in by_key
    assert "STK:MSFT" not in by_key
    assert "OPT:PLTR:20261218:55:P" in by_key
    assert "STK:PLTR" in by_key

    # Scaled-down same-direction resize
    nvda_opt = by_key["OPT:NVDA:20261218:140:C"]
    assert nvda_opt.quantity == pytest.approx(-1.0)
    assert nvda_opt.avg_cost_price == pytest.approx(5.00)
    assert nvda_opt.cost_basis_ex_fee == pytest.approx(-500.0)
    assert nvda_opt.fees == pytest.approx(0.65)
    assert nvda_opt.unrealized_pnl_ex_fee == pytest.approx(100.0)

    nvda_stk = by_key["STK:NVDA"]
    assert nvda_stk.quantity == pytest.approx(40.0)
    assert nvda_stk.avg_cost_price == pytest.approx(120.0)
    assert nvda_stk.cost_basis_ex_fee == pytest.approx(4800.0)
    assert nvda_stk.fees == pytest.approx(0.40)
    assert nvda_stk.unrealized_pnl_ex_fee == pytest.approx(400.0)

    # Expanded same-direction resize
    avgo_opt = by_key["OPT:AVGO:20261218:180:C"]
    assert avgo_opt.quantity == pytest.approx(-3.0)
    assert avgo_opt.avg_cost_price == pytest.approx(4.50)
    assert avgo_opt.cost_basis_ex_fee == pytest.approx(-1350.0)
    assert avgo_opt.unrealized_pnl_ex_fee == pytest.approx(150.0)

    avgo_stk = by_key["STK:AVGO"]
    assert avgo_stk.quantity == pytest.approx(120.0)
    assert avgo_stk.avg_cost_price == pytest.approx(165.0)
    assert avgo_stk.cost_basis_ex_fee == pytest.approx(19800.0)
    assert avgo_stk.unrealized_pnl_ex_fee == pytest.approx(600.0)

    # Flipped-direction resize (also unlinks campaign and reclassifies strategy)
    amd_opt = by_key["OPT:AMD:20261218:150:P"]
    assert amd_opt.quantity == pytest.approx(2.0)
    assert amd_opt.avg_cost_price == pytest.approx(2.50)
    assert amd_opt.cost_basis_ex_fee == pytest.approx(500.0)
    assert amd_opt.unrealized_pnl_ex_fee == pytest.approx(200.0)
    assert amd_opt.strategy_type == "LONG_PUT"
    assert amd_opt.campaign_id is None

    amd_stk = by_key["STK:AMD"]
    assert amd_stk.quantity == pytest.approx(-30.0)
    assert amd_stk.avg_cost_price == pytest.approx(145.0)
    assert amd_stk.cost_basis_ex_fee == pytest.approx(-4350.0)
    assert amd_stk.unrealized_pnl_ex_fee == pytest.approx(150.0)


def test_stale_gateway_snapshot_does_not_revert_later_partial_close() -> None:
    execs = _exec_df(
        [
            # T1: open -2 OPT and +100 STK for NVDA, plus -2 OPT and +100 STK for AVGO
            {
                "exec_id": "t1-nvda-opt-open",
                "underlying": "NVDA",
                "conid": 730001,
                "expiry": "20261218",
                "strike": 140.0,
                "right": "C",
                "side": "SLD",
                "quantity": 2.0,
                "price": 5.00,
                "commission": 1.30,
                "open_close": "O",
                "executed_at": _ts(5),
            },
            {
                "exec_id": "t1-nvda-stk-open",
                "underlying": "NVDA",
                "asset_class": "STK",
                "conid": 730002,
                "side": "BOT",
                "quantity": 100.0,
                "price": 120.0,
                "commission": 1.00,
                "open_close": "O",
                "executed_at": _ts(5),
            },
            {
                "exec_id": "t1-avgo-opt-open",
                "underlying": "AVGO",
                "conid": 730003,
                "expiry": "20261218",
                "strike": 180.0,
                "right": "C",
                "side": "SLD",
                "quantity": 2.0,
                "price": 4.00,
                "commission": 1.30,
                "open_close": "O",
                "executed_at": _ts(5),
            },
            {
                "exec_id": "t1-avgo-stk-open",
                "underlying": "AVGO",
                "asset_class": "STK",
                "conid": 730004,
                "side": "BOT",
                "quantity": 100.0,
                "price": 160.0,
                "commission": 1.00,
                "open_close": "O",
                "executed_at": _ts(5),
            },
            # T3 > T2: partial closes without a new Gateway snapshot after T2
            {
                "exec_id": "t3-nvda-opt-trim",
                "underlying": "NVDA",
                "conid": 730001,
                "expiry": "20261218",
                "strike": 140.0,
                "right": "C",
                "side": "BOT",
                "quantity": 1.0,
                "price": 4.50,
                "commission": 0.65,
                "open_close": "C",
                "executed_at": _ts(1),
            },
            {
                "exec_id": "t3-nvda-stk-trim",
                "underlying": "NVDA",
                "asset_class": "STK",
                "conid": 730002,
                "side": "SLD",
                "quantity": 50.0,
                "price": 128.0,
                "commission": 0.50,
                "open_close": "C",
                "executed_at": _ts(1),
            },
            {
                "exec_id": "t3-avgo-opt-trim",
                "underlying": "AVGO",
                "conid": 730003,
                "expiry": "20261218",
                "strike": 180.0,
                "right": "C",
                "side": "BOT",
                "quantity": 1.0,
                "price": 3.00,
                "commission": 0.65,
                "open_close": "C",
                "executed_at": _ts(1),
            },
            {
                "exec_id": "t3-avgo-stk-trim",
                "underlying": "AVGO",
                "asset_class": "STK",
                "conid": 730004,
                "side": "SLD",
                "quantity": 50.0,
                "price": 165.0,
                "commission": 0.50,
                "open_close": "C",
                "executed_at": _ts(1),
            },
        ]
    )
    # T2 snapshot (_ts(3)): has NVDA pre-trim quantities and omits AVGO
    pos = _pos_df(
        [
            {
                "conid": 730001,
                "underlying": "NVDA",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 140.0,
                "right": "C",
                "position": -2.0,
                "avg_cost": 5.00,
                "mark_price": 4.00,
                "source": "GATEWAY_LIVE",
                "updated_at": _ts(3),
            },
            {
                "conid": 730002,
                "underlying": "NVDA",
                "asset_class": "STK",
                "position": 100.0,
                "avg_cost": 120.0,
                "mark_price": 130.0,
                "source": "GATEWAY_LIVE",
                "updated_at": _ts(3),
            },
        ]
    )

    rep = build_ledger_report(
        execs, pos, window="ALL", view="open_mtm", include_stock_gain=True, as_of=AS_OF
    )
    by_key = {p.position_key: p for p in rep.open_positions}

    nvda_opt = by_key["OPT:NVDA:20261218:140:C"]
    assert nvda_opt.quantity == pytest.approx(-1.0)
    assert nvda_opt.cost_basis_ex_fee == pytest.approx(-500.0)
    assert nvda_opt.fees == pytest.approx(0.65)
    assert nvda_opt.unrealized_pnl_ex_fee == pytest.approx(100.0)

    nvda_stk = by_key["STK:NVDA"]
    assert nvda_stk.quantity == pytest.approx(50.0)
    assert nvda_stk.cost_basis_ex_fee == pytest.approx(6000.0)
    assert nvda_stk.fees == pytest.approx(0.50)
    assert nvda_stk.unrealized_pnl_ex_fee == pytest.approx(500.0)

    # Contracts active at T3 > T2 also remain visible even if absent from T2 Gateway snapshot
    assert by_key["OPT:AVGO:20261218:180:C"].quantity == pytest.approx(-1.0)
    assert by_key["STK:AVGO"].quantity == pytest.approx(50.0)


def test_already_aggregated_fast_paths_and_format_utc_iso() -> None:
    naive = datetime(2026, 11, 20, 9, 5, 7)
    utc_dt = datetime(2026, 11, 20, 9, 5, 7, tzinfo=UTC)
    plus_two = datetime(2026, 11, 20, 11, 5, 7, tzinfo=timezone(timedelta(hours=2)))
    assert format_utc_iso(naive) == "2026-11-20T09:05:07Z"
    assert format_utc_iso(utc_dt) == "2026-11-20T09:05:07Z"
    assert format_utc_iso(plus_two) == "2026-11-20T09:05:07Z"

    raw_legs = [
        AggregatedLeg(
            underlying="NVDA",
            expiry="20261218",
            strike=140.0,
            right="P",
            side="SLD",
            quantity=1.0,
            price=4.0,
            cashflow_ex_fee=400.0,
            fees=0.65,
            multiplier=100,
            conid=700001,
            symbol="NVDA  261218P00140000",
        ),
        AggregatedLeg(
            underlying="NVDA",
            expiry="20261218",
            strike=140.0,
            right="P",
            side="SLD",
            quantity=1.0,
            price=4.2,
            cashflow_ex_fee=420.0,
            fees=0.65,
            multiplier=100,
            conid=None,
            symbol="NVDA  261218P00140000",
        ),
        AggregatedLeg(
            underlying="NVDA",
            expiry="20261218",
            strike=135.0,
            right="P",
            side="BOT",
            quantity=2.0,
            price=2.1,
            cashflow_ex_fee=-420.0,
            fees=1.30,
            multiplier=100,
            conid=700002,
            symbol="NVDA  261218P00135000",
        ),
    ]
    old_legs = [
        AggregatedLeg(
            underlying="NVDA",
            expiry="20261120",
            strike=130.0,
            right="P",
            side="SLD",
            quantity=2.0,
            price=3.0,
            cashflow_ex_fee=600.0,
            fees=1.30,
            multiplier=100,
            conid=700003,
            symbol="NVDA  261120P00130000",
        ),
    ]
    distinct = aggregate_legs(raw_legs)
    assert len(distinct) == 2
    assert distinct[1].conid == 700001
    assert distinct[1].quantity == pytest.approx(2.0)

    assert classify_option_legs(raw_legs) == classify_option_legs(distinct, already_aggregated=True)
    assert format_legs_strike_summary(raw_legs) == format_legs_strike_summary(
        distinct, already_aggregated=True
    )
    assert format_legs_position_label(raw_legs) == format_legs_position_label(
        distinct, already_aggregated=True
    )
    assert format_step_summary(
        "ROLL",
        raw_legs,
        cashflow_ex_fee=120.0,
        contracts=2.0,
        roll_old_legs=old_legs,
    ) == format_step_summary(
        "ROLL",
        distinct,
        cashflow_ex_fee=120.0,
        contracts=2.0,
        roll_old_legs=aggregate_legs(old_legs),
        already_aggregated=True,
    )


def test_compile_ledger_data_equivalence_and_as_of_fallback() -> None:
    execs = _exec_df(
        [
            {
                "exec_id": "comp-1",
                "underlying": "NVDA",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 140.0,
                "right": "P",
                "side": "SLD",
                "quantity": 1.0,
                "price": 5.0,
                "proceeds": 500.0,
                "commission": 1.0,
                "open_close": "O",
                "executed_at": _ts(1),
            },
            {
                "exec_id": "comp-2",
                "underlying": "AVGO",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 180.0,
                "right": "P",
                "side": "SLD",
                "quantity": 1.0,
                "price": 6.0,
                "proceeds": 600.0,
                "commission": 1.0,
                "open_close": "O",
                "executed_at": _ts(5),
            },
        ]
    )
    pos = _pos_df(
        [
            {
                "conid": 740001,
                "underlying": "NVDA",
                "asset_class": "OPT",
                "expiry": "20261218",
                "strike": 140.0,
                "right": "P",
                "position": -1.0,
                "avg_cost": 5.0,
                "mark_price": 4.0,
                "source": "FLEX_IMPORT",
                "updated_at": _ts(5),
            }
        ]
    )
    compiled = compile_ledger_data(execs, pos)
    assert isinstance(compiled, CompiledLedgerData)

    later = AS_OF + timedelta(days=2)
    views: tuple[Literal["realized", "open_mtm"], ...] = ("realized", "open_mtm")
    for ticker_arg in ("ALL", "NVDA"):
        for view_arg in views:
            uncompiled = build_ledger_report(
                execs,
                pos,
                window="ALL",
                ticker=ticker_arg,
                view=view_arg,
                as_of=later,
            )
            fast = build_ledger_report(
                execs,
                pos,
                window="ALL",
                ticker=ticker_arg,
                view=view_arg,
                as_of=later,
                compiled=compiled,
            )
            assert fast.model_dump() == uncompiled.model_dump()

    # Safely ignores compiled cache when caller requests historical as_of < max_exec_dt
    early_as_of = datetime.fromisoformat(_ts(2).replace("Z", "+00:00"))
    expected_early = build_ledger_report(execs, pos, window="ALL", as_of=early_as_of)
    actual_early = build_ledger_report(
        execs, pos, window="ALL", as_of=early_as_of, compiled=compiled
    )
    assert actual_early.model_dump() == expected_early.model_dump()
