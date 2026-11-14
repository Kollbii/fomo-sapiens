"""ScreenerService: Best Option screener over the watchlist (cached CSVs only, zero IBKR calls).

Real-engine tests run `AnalyzerService` (with IBKR stand-ins that fail on any use) over the golden
NVDA fixture plus BSM-priced synthetic chains; ranking / status edge cases use a crafted
`ScreenerCandidateSource`.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

import numpy as np
import polars as pl
import pytest
from pydantic import ValidationError

import src.services.analyzer as analyzer_module
from src.analytics.bsm_fallback import bsm_price
from src.analytics.strategies import (
    PRESET_STRATEGIES,
    EvaluatedLeg,
    LegSide,
    PositionGreeks,
    PresetStrategyKind,
    StrategyCandidateFilters,
    StrategyEvaluationResponse,
    StrategyFilterError,
    StrategyPricing,
    StrategyRiskMetrics,
    candidate_passes_filters,
)
from src.core.config import LedgerSettings, PaperTrackerSettings, Settings, WatchlistSettings
from src.core.exceptions import InvalidRequestError, SnapshotNotFoundError, StorageSchemaError
from src.services.analyzer import AnalyzerService, StrategyCandidateOutcome
from src.services.error_messages import GENERIC_ERROR_MESSAGE
from src.services.schemas import (
    ScreenerParams,
    ScreenerRow,
    ScreenerRunRequest,
    ScreenerRunResponse,
    StrategyEvaluateRequest,
    StrategySuggestRequest,
)
from src.services.screener import (
    CREDIT_STRATEGY_KINDS,
    INDEX_UNAVAILABLE_NOTICE,
    INDEX_UNAVAILABLE_REASON,
    NO_SNAPSHOT_REASON,
    PARTIAL_ONLY_REASON,
    ScreenerService,
    candidate_filters_for,
    failed_filters_for,
    format_legs_label,
    leg_inputs,
    min_leg_open_interest,
)
from src.services.watchlist import WatchlistService
from src.storage.csv_repository import CSVRepository, make_snapshot_id
from src.storage.schema import OptionRight
from src.storage.watchlist_store import WatchlistStore, resolve_watchlist_path
from tests.support.aio import noop_sleep, run_coro

_T = TypeVar("_T")

_FIXTURE_ID = "NVDA_20261111_155004"
_TS = datetime(2026, 11, 11, 15, 30, tzinfo=UTC)  # 10:30 ET on 2026-11-11
_NOW = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)  # 15:00 ET, same ET day
# ticker -> (spot, IV, strike step) of a BSM-priced synthetic chain
_SYNTH: dict[str, tuple[float, float, float]] = {
    "AVGO": (172.4, 0.42, 5.0),
    "CRDO": (61.3, 0.65, 2.5),
    "PLTR": (33.8, 0.55, 1.0),
    "MRVL": (88.2, 0.50, 2.5),
}


async def _inline(func: Callable[[], _T]) -> _T:
    return func()


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Untouchable:
    """IBKR-facing stand-in (broker / fetcher / corridor): any attribute not listed fails."""

    def __init__(self, name: str, **allowed: object) -> None:
        self._name = name
        self._allowed = allowed

    def __getattr__(self, attr: str) -> object:
        allowed: dict[str, object] = self.__dict__["_allowed"]
        if attr in allowed:
            return allowed[attr]
        raise AssertionError(f"screener touched IBKR object {self.__dict__['_name']}.{attr}")


def _chain_rows(
    ticker: str,
    ts: datetime,
    *,
    dtes: tuple[int, ...] = (37, 65),
    puts_only: bool = False,
    strikes_each_side: int = 8,
) -> list[dict[str, Any]]:
    """BSM-priced chain (bid/ask +-3% around the model price, OI peaking near spot)."""
    spot, iv, step = _SYNTH[ticker]
    snap_id = make_snapshot_id(ticker, ts)
    center = round(spot / step) * step
    strikes = [center + step * i for i in range(-strikes_each_side, strikes_each_side + 1)]
    rights: tuple[OptionRight, ...] = ("P",) if puts_only else ("C", "P")
    rows: list[dict[str, Any]] = []
    conid = 70_000
    for dte in dtes:
        expiry = (ts.date() + timedelta(days=dte)).isoformat()
        for strike in strikes:
            for right in rights:
                conid += 1
                px = float(
                    bsm_price(
                        np.array([spot]),
                        np.array([strike]),
                        np.array([dte / 365.0]),
                        0.0425,
                        np.array([iv]),
                        np.array([right == "C"]),
                    )[0]
                )
                px = max(px, 0.05)
                half = max(0.02, px * 0.03)
                oi = int(2000 * math.exp(-abs(strike - spot) / (spot * 0.08))) + 10
                rows.append(
                    {
                        "snapshot_id": snap_id,
                        "snapshot_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "market_date": ts.date().isoformat(),
                        "market_data_type": 3,
                        "underlying": ticker,
                        "underlying_conid": 4242,
                        "underlying_price": spot,
                        "option_conid": conid,
                        "expiry": expiry,
                        "dte": dte,
                        "strike": strike,
                        "moneyness_pct": (strike - spot) / spot * 100.0,
                        "right": right,
                        "bid": round(px - half, 2),
                        "ask": round(px + half, 2),
                        "mid": None,
                        "last": round(px, 2),
                        "close": round(px, 2),
                        "mark_price": None,
                        "volume": 100 + oi // 10,
                        "open_interest": oi,
                        "iv": None,
                        "delta": None,
                        "gamma": None,
                        "theta": None,
                        "vega": None,
                        "greeks_source": "NONE",
                    }
                )
    return rows


def _save(repo: CSVRepository, ticker: str, ts: datetime = _TS, **kwargs: Any) -> str:
    partial = kwargs.pop("partial_reason", None)
    meta = repo.save_snapshot_result(
        ticker, _chain_rows(ticker, ts, **kwargs), spot_source="MID", partial_reason=partial
    ).meta
    return meta.snapshot_id


def _analyzer(repo: CSVRepository, tickers: Sequence[str], clock: _Clock) -> AnalyzerService:
    settings = Settings(
        watchlist=WatchlistSettings(hardcoded_tickers=tuple(tickers), max_size=20),
        tracker=PaperTrackerSettings(tracked_dir="data/tracked"),
        ledger=LedgerSettings(ledger_dir="data/ledger"),
    )
    svc = AnalyzerService(
        settings=settings,
        repo=repo,
        broker_client=_Untouchable("broker"),  # type: ignore[arg-type]
        fetcher=_Untouchable("fetcher", clock=clock, sleep=noop_sleep),  # type: ignore[arg-type]
        corridor=_Untouchable("corridor", active_stream_count=0),  # type: ignore[arg-type]
    )
    svc.ensure_layout()
    svc.watchlist.runner = _inline
    svc.screener.runner = _inline
    return svc


def _run(svc: AnalyzerService | ScreenerService, **request: Any) -> ScreenerRunResponse:
    screener = svc.screener if isinstance(svc, AnalyzerService) else svc
    return run_coro(screener.run(ScreenerRunRequest(**request)))


def _by_id(resp: ScreenerRunResponse) -> dict[str, ScreenerRow]:
    return {row.row_id: row for row in resp.rows}


BuildCalls = list[tuple[str, tuple[PresetStrategyKind, ...]]]


def _spy_builds(svc: AnalyzerService, monkeypatch: pytest.MonkeyPatch) -> BuildCalls:
    calls: BuildCalls = []
    original = svc.build_strategy_candidates

    def _spy(
        symbol: str,
        enriched: pl.DataFrame,
        kinds: Sequence[PresetStrategyKind],
        **kwargs: Any,
    ) -> list[StrategyCandidateOutcome]:
        calls.append((symbol, tuple(kinds)))
        return original(symbol, enriched, kinds, **kwargs)

    monkeypatch.setattr(svc, "build_strategy_candidates", _spy)
    return calls


# ------------------------------------------------------------------ Strategy Lab parity
@pytest.mark.parametrize(
    ("floors", "expect_all_presets"),
    [
        ({}, True),
        ({"min_net_credit_dollars": 150.0, "min_ror_pct": 60.0}, False),
    ],
)
def test_rows_equal_strategy_lab_autobuild_for_every_preset(
    fixture_repo: CSVRepository, floors: dict[str, float], expect_all_presets: bool
) -> None:
    avgo_id = _save(fixture_repo, "AVGO")
    svc = _analyzer(fixture_repo, ("NVDA", "AVGO"), _Clock(_NOW))
    resp = _run(svc, mode="all", **floors)
    rows = _by_id(resp)
    assert resp.params.dte_min == 30 and resp.params.dte_max == 120
    for ticker, snap_id in (("NVDA", _FIXTURE_ID), ("AVGO", avgo_id)):
        # What the Strategy Lab card sends by default: explicit 30-120 window, Auto expiry.
        lab = svc.suggest_strategies(
            StrategySuggestRequest(
                ticker=ticker,
                snapshot_id=snap_id,
                dte_min=30,
                dte_max=120,
                min_net_credit_dollars=floors.get("min_net_credit_dollars"),
                min_ror_pct=floors.get("min_ror_pct"),
            )
        )
        lab_kinds = {ev.strategy_kind for ev in lab.evaluations}
        row_kinds = {r.strategy_kind for r in resp.rows if r.ticker == ticker}
        if expect_all_presets:
            assert lab_kinds == row_kinds == set(PRESET_STRATEGIES)
        assert row_kinds <= lab_kinds
        assert row_kinds or ticker == "NVDA"  # flat-priced fixture fails a 60% RoR floor
        for ev in lab.evaluations:
            row = rows.get(f"{ticker}:{ev.strategy_kind}")
            if row is None:
                assert not expect_all_presets
                continue
            assert row.status == "ok"
            assert row.snapshot_id == snap_id
            assert row.legs == ev.legs
            assert row.pricing == ev.pricing
            assert row.risk == ev.risk
            assert row.net_greeks == ev.net_greeks
            assert row.rationale == ev.rationale
            assert (row.primary_call_wall, row.primary_put_wall) == (
                ev.primary_call_wall,
                ev.primary_put_wall,
            )
            assert (row.short_expiry, row.short_dte, row.long_expiry, row.long_dte) == (
                ev.short_expiry,
                ev.short_dte,
                ev.long_expiry,
                ev.long_dte,
            )
            assert row.underlying_price == ev.underlying_price
            assert "curves" not in row.model_dump() and "price_grid" not in row.model_dump()
            # `selected` builds the same candidate on its own (cold cache).
            svc.screener.clear_cache()
            one = _run(svc, mode="selected", strategy_kind=ev.strategy_kind, **floors)
            assert _by_id(one)[row.row_id] == row


def test_row_legs_reload_identically_in_strategy_lab_evaluate(tmp_path: Path) -> None:
    """Open in Strategy Lab: the row's exact legs re-evaluate to the same numbers."""
    repo = CSVRepository(root=tmp_path)
    snap_id = _save(repo, "CRDO")
    svc = _analyzer(repo, ("CRDO",), _Clock(_NOW))
    rows = _run(svc, mode="all").rows
    assert len(rows) == 6
    for row in rows:
        assert row.strategy_kind is not None
        legs = leg_inputs(row.legs)
        assert [(leg.side, leg.right, leg.expiry, leg.strike, leg.qty) for leg in legs] == [
            (leg.side, leg.right, leg.expiry, leg.strike, leg.qty) for leg in row.legs
        ]
        lab = svc.evaluate_strategy(
            StrategyEvaluateRequest(
                ticker="CRDO",
                snapshot_id=snap_id,
                strategy_kind=row.strategy_kind,
                legs=legs,
                dte_min=30,  # the screener's resolved default window
                dte_max=120,
            )
        )
        assert (lab.legs, lab.pricing, lab.risk, lab.net_greeks) == (
            row.legs,
            row.pricing,
            row.risk,
            row.net_greeks,
        )


def test_suggest_strategies_runs_through_build_strategy_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = CSVRepository(root=tmp_path)
    _save(repo, "AVGO")
    svc = _analyzer(repo, ("AVGO",), _Clock(_NOW))
    calls = _spy_builds(svc, monkeypatch)
    resp = svc.suggest_strategies(StrategySuggestRequest(ticker="AVGO"))
    assert [ev.strategy_kind for ev in resp.evaluations] == list(PRESET_STRATEGIES)
    assert calls == [("AVGO", PRESET_STRATEGIES)]
    svc.suggest_strategies(StrategySuggestRequest(ticker="AVGO", strategy_kind="IRON_CONDOR"))
    assert calls[-1] == ("AVGO", ("IRON_CONDOR",))


def test_suggest_strategies_error_contract_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = CSVRepository(root=tmp_path)
    _save(repo, "AVGO")
    svc = _analyzer(repo, ("AVGO",), _Clock(_NOW))

    # Single preset: the builder's own message, chained to the engine error.
    with pytest.raises(InvalidRequestError) as filt:
        svc.suggest_strategies(
            StrategySuggestRequest(
                ticker="AVGO", strategy_kind="BULL_PUT_SPREAD", min_net_credit_dollars=100000.0
            )
        )
    assert str(filt.value) == (
        "No OTM Bull Put Spread met entry quality thresholds (min_credit=$100000, min_ror=0%)."
    )
    assert isinstance(filt.value.__cause__, StrategyFilterError)
    assert filt.value.__cause__.skip_reason == "BELOW_MIN_CREDIT"

    with pytest.raises(InvalidRequestError, match="No contracts found in requested DTE range"):
        svc.suggest_strategies(StrategySuggestRequest(ticker="AVGO", dte_min=100, dte_max=120))
    with pytest.raises(InvalidRequestError, match="min_ror_pct must be finite"):
        svc.suggest_strategies(
            StrategySuggestRequest.model_construct(ticker="AVGO", min_ror_pct=-1)
        )

    def _boom(_df: pl.DataFrame, *, strategy_kind: str, **_: Any) -> StrategyEvaluationResponse:
        raise ValueError(f"boom {strategy_kind}")

    monkeypatch.setattr(analyzer_module, "evaluate_strategy_engine", _boom)
    with pytest.raises(InvalidRequestError) as every:
        svc.suggest_strategies(StrategySuggestRequest(ticker="AVGO"))
    assert str(every.value) == "; ".join(f"{k}: boom {k}" for k in PRESET_STRATEGIES)
    with pytest.raises(InvalidRequestError, match=r"^boom IRON_CONDOR$") as one:
        svc.suggest_strategies(StrategySuggestRequest(ticker="AVGO", strategy_kind="IRON_CONDOR"))
    assert isinstance(one.value.__cause__, ValueError)


def _validation_error() -> ValidationError:
    try:
        StrategyCandidateFilters(min_pop_pct=500.0)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a ValidationError")


def test_candidate_outcome_requires_exactly_one_result() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        StrategyCandidateOutcome(kind="BULL_PUT_SPREAD")
    outcome = StrategyCandidateOutcome(
        kind="BULL_PUT_SPREAD", error=StrategyFilterError("INSUFFICIENT_WINGS", "a\n  b")
    )
    assert (outcome.skip_reason, outcome.reason) == ("INSUFFICIENT_WINGS", "a b")
    internal = StrategyCandidateOutcome(kind="IRON_CONDOR", error=_validation_error())
    assert (internal.skip_reason, internal.reason) == (None, GENERIC_ERROR_MESSAGE)


# ------------------------------------------------------------------ modes & filters (real engine)
def test_default_mode_is_best_and_picks_highest_ror_per_ticker(tmp_path: Path) -> None:
    repo = CSVRepository(root=tmp_path)
    for ticker in ("AVGO", "CRDO", "PLTR"):
        _save(repo, ticker)
    svc = _analyzer(repo, ("AVGO", "CRDO", "PLTR"), _Clock(_NOW))
    best = _run(svc)
    assert best.mode == "best" and best.params.mode == "best"
    assert best.params.strategy_kind is None
    assert sorted(r.ticker for r in best.rows) == ["AVGO", "CRDO", "PLTR"]
    assert best.counts.model_dump() == {"ok": 3, "filtered": 0, "not_buildable": 0, "no_data": 0}
    every = _run(svc, mode="all")
    assert len(every.rows) == 18 and every.counts.ok == 18

    def _rank(r: ScreenerRow) -> tuple[float, float, int]:
        assert r.risk is not None and r.strategy_kind is not None
        ror, pop = r.risk.return_on_risk_pct, r.risk.prob_of_profit_pct
        return (
            ror if ror is not None else -math.inf,
            pop if pop is not None else -math.inf,
            -PRESET_STRATEGIES.index(r.strategy_kind),
        )

    for row in best.rows:
        assert row == max((r for r in every.rows if r.ticker == row.ticker), key=_rank)
    for resp in (best, every):
        rors = [r.risk.return_on_risk_pct for r in resp.rows if r.risk is not None]
        assert rors == sorted(rors, key=lambda v: -(v if v is not None else -math.inf))


def test_filters_min_credit_spares_debit_presets_and_min_ror_pop_hit_every_preset(
    tmp_path: Path,
) -> None:
    repo = CSVRepository(root=tmp_path)
    _save(repo, "AVGO")
    svc = _analyzer(repo, ("AVGO",), _Clock(_NOW))
    base = _by_id(_run(svc, mode="all"))
    assert len(base) == 6

    # Min credit removes only Bull Put / Bear Call (while building) and Iron Condor (after).
    rich = _run(svc, mode="all", min_net_credit_dollars=100000.0)
    kept = {r.strategy_kind for r in rich.rows}
    assert kept == {"BULL_CALL_SPREAD", "BEAR_PUT_SPREAD", "DIAGONAL_SPREAD"}
    assert kept.isdisjoint(CREDIT_STRATEGY_KINDS)
    assert all(r == base[r.row_id] and r.pricing and not r.pricing.is_credit for r in rich.rows)
    assert rich.counts.model_dump() == {"ok": 3, "filtered": 3, "not_buildable": 0, "no_data": 0}
    condor = _by_id(
        _run(svc, mode="selected", strategy_kind="IRON_CONDOR", min_net_credit_dollars=100000.0)
    )["AVGO:IRON_CONDOR"]
    assert (condor.status, condor.failed_filters) == ("filtered", ["min_net_credit_dollars"])
    assert condor.status_reason == "min credit $100,000.00"
    assert condor.skip_reason is None and condor.legs == [] and condor.risk is None
    bull_put = _by_id(
        _run(svc, mode="selected", strategy_kind="BULL_PUT_SPREAD", min_net_credit_dollars=1e5)
    )["AVGO:BULL_PUT_SPREAD"]
    assert (bull_put.status, bull_put.skip_reason) == ("filtered", "BELOW_MIN_CREDIT")
    assert bull_put.failed_filters == ["min_net_credit_dollars"]

    # Min RoR removes a debit candidate too (Bull Call is built without floors).
    bull_call = base["AVGO:BULL_CALL_SPREAD"]
    assert bull_call.risk is not None and bull_call.risk.return_on_risk_pct is not None
    floor = bull_call.risk.return_on_risk_pct + 0.1
    above = _by_id(_run(svc, mode="all", min_ror_pct=floor))
    assert "AVGO:BULL_CALL_SPREAD" not in above
    assert all(r.risk and (r.risk.return_on_risk_pct or 0.0) >= floor for r in above.values())
    sel = _by_id(_run(svc, mode="selected", strategy_kind="BULL_CALL_SPREAD", min_ror_pct=floor))
    assert sel["AVGO:BULL_CALL_SPREAD"].failed_filters == ["min_ror_pct"]
    assert sel["AVGO:BULL_CALL_SPREAD"].status_reason == f"min RoR {floor:g}%"

    # Min POP (never part of the build) removes exactly the candidates below it.
    pop_rows = _by_id(_run(svc, mode="all", min_pop_pct=50.0))
    expected = {
        rid for rid, r in base.items() if r.risk and (r.risk.prob_of_profit_pct or 0.0) >= 50.0
    }
    assert set(pop_rows) == expected and 0 < len(expected) < 6
    assert all(pop_rows[rid] == base[rid] for rid in expected)
    pop_params = _run(svc, mode="all", min_pop_pct=50.0).params
    for rid, row in base.items():
        assert row.pricing is not None and row.risk is not None and row.strategy_kind
        combined = candidate_filters_for(row.strategy_kind, pop_params)
        assert candidate_passes_filters(row.pricing, row.risk, combined) is (rid in expected)


def test_status_rows_per_mode(tmp_path: Path) -> None:
    repo = CSVRepository(root=tmp_path)
    avgo_id = _save(repo, "AVGO")
    mrvl_id = _save(repo, "MRVL", puts_only=True)
    _save(repo, "PLTR", partial_reason="cancelled")  # only a partial snapshot
    svc = _analyzer(repo, ("AVGO", "MRVL", "MU", "PLTR"), _Clock(_NOW))

    sel = _run(svc, mode="selected", strategy_kind="BULL_CALL_SPREAD")
    assert [r.row_id for r in sel.rows] == [
        "AVGO:BULL_CALL_SPREAD",
        "MRVL:BULL_CALL_SPREAD",
        "MU:BULL_CALL_SPREAD",
        "PLTR:BULL_CALL_SPREAD",
    ]
    avgo, mrvl, mu, pltr = sel.rows
    assert avgo.status == "ok" and avgo.snapshot_id == avgo_id
    assert mrvl.status == "not_buildable" and mrvl.snapshot_id == mrvl_id
    assert (
        mrvl.status_reason == "No eligible option contracts found in snapshot for strategy preset"
    )
    assert mrvl.underlying_price == _SYNTH["MRVL"][0] and mrvl.legs == []
    assert (mu.status, mu.status_reason, mu.snapshot_id) == ("no_data", NO_SNAPSHOT_REASON, None)
    assert (pltr.status, pltr.status_reason) == ("no_data", PARTIAL_ONLY_REASON)
    assert sel.counts.model_dump() == {"ok": 1, "filtered": 0, "not_buildable": 1, "no_data": 2}
    assert sel.tickers == ["AVGO", "MRVL", "MU", "PLTR"]

    strict = _by_id(_run(svc, mode="selected", strategy_kind="BULL_CALL_SPREAD", min_pop_pct=99))
    assert strict["AVGO:BULL_CALL_SPREAD"].status == "filtered"
    assert strict["AVGO:BULL_CALL_SPREAD"].status_reason == "min POP 99%"

    best = _run(svc)
    assert [(r.ticker, r.status) for r in best.rows] == [
        ("AVGO", "ok"),
        ("MRVL", "ok"),
        ("MU", "no_data"),
        ("PLTR", "no_data"),
    ]
    avgo_best, mrvl_best, mu_best, pltr_best = best.rows
    assert avgo_best.row_id == f"AVGO:{avgo_best.strategy_kind}"
    assert mrvl_best.strategy_kind in ("BULL_PUT_SPREAD", "BEAR_PUT_SPREAD")
    assert (mu_best.row_id, mu_best.strategy_kind, pltr_best.row_id) == ("MU", None, "PLTR")
    assert best.counts.model_dump() == {"ok": 2, "filtered": 0, "not_buildable": 0, "no_data": 2}

    best_strict = _run(svc, min_pop_pct=99)
    assert [(r.row_id, r.status) for r in best_strict.rows] == [
        ("AVGO", "filtered"),
        ("MRVL", "filtered"),  # 2 presets filtered by POP, 4 not buildable -> filtered
        ("MU", "no_data"),
        ("PLTR", "no_data"),
    ]
    assert best_strict.rows[0].failed_filters == ["min_pop_pct"]

    empty_window = _run(svc, dte_min=100, dte_max=120)
    assert [r.status for r in empty_window.rows[:2]] == ["not_buildable", "not_buildable"]
    assert empty_window.rows[0].status_reason == (
        "No contracts found in requested DTE range for AVGO"
    )

    every = _run(svc, mode="all")
    assert {r.status for r in every.rows} == {"ok"}
    assert {(r.ticker, r.strategy_kind) for r in every.rows if r.ticker == "MRVL"} == {
        ("MRVL", "BULL_PUT_SPREAD"),
        ("MRVL", "BEAR_PUT_SPREAD"),
    }
    assert every.counts.model_dump() == {"ok": 8, "filtered": 0, "not_buildable": 4, "no_data": 2}


def test_partial_snapshot_skipped_and_new_complete_snapshot_invalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = CSVRepository(root=tmp_path)
    first = _save(repo, "AVGO", _TS)
    _save(repo, "AVGO", _TS + timedelta(hours=1), partial_reason="timeout")  # newer, partial
    _save(repo, "CRDO", _TS)
    svc = _analyzer(repo, ("AVGO", "CRDO"), _Clock(_NOW))
    calls = _spy_builds(svc, monkeypatch)

    resp = _run(svc)
    assert {r.ticker: r.snapshot_id for r in resp.rows}["AVGO"] == first
    assert sorted(c[0] for c in calls) == ["AVGO", "CRDO"]
    assert svc.screener.cache_size == 2

    newer = _save(repo, "AVGO", _TS + timedelta(hours=2))
    calls.clear()
    resp2 = _run(svc)
    assert {r.ticker: r.snapshot_id for r in resp2.rows}["AVGO"] == newer
    assert calls == [("AVGO", PRESET_STRATEGIES)]  # CRDO came from the cache
    assert svc.screener.cache_size == 2  # the old AVGO entry was pruned


@pytest.mark.parametrize(
    ("now", "today_et", "fresh"),
    [
        (datetime(2026, 11, 11, 20, 0, tzinfo=UTC), "2026-11-11", True),
        (datetime(2026, 11, 12, 3, 0, tzinfo=UTC), "2026-11-11", True),  # 22:00 ET Nov 11
        (datetime(2026, 11, 12, 14, 0, tzinfo=UTC), "2026-11-12", False),
    ],
)
def test_is_today_et_uses_injected_clock_and_is_never_cached(
    fixture_repo: CSVRepository,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    today_et: str,
    fresh: bool,
) -> None:
    clock = _Clock(datetime(2026, 11, 20, 15, 0, tzinfo=UTC))
    svc = _analyzer(fixture_repo, ("NVDA",), clock)
    stale = _run(svc)
    assert stale.rows[0].is_today_et is False
    calls = _spy_builds(svc, monkeypatch)
    clock.now = now
    resp = _run(svc)
    assert calls == []  # cache hit, yet freshness follows the clock
    row = resp.rows[0]
    assert (resp.today_et, row.is_today_et) == (today_et, fresh)
    assert (row.snapshot_id, row.snapshot_ts_utc) == (_FIXTURE_ID, "2026-11-11T15:50:04Z")
    assert row.market_data_type == 3
    assert resp.generated_at_utc == now.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_cache_reuses_builds_across_modes_and_pop_and_rebuilds_on_build_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = CSVRepository(root=tmp_path)
    _save(repo, "AVGO")
    _save(repo, "CRDO")
    svc = _analyzer(repo, ("AVGO", "CRDO"), _Clock(_NOW))
    calls = _spy_builds(svc, monkeypatch)

    _run(svc, mode="selected", strategy_kind="IRON_CONDOR")
    assert calls == [("AVGO", ("IRON_CONDOR",)), ("CRDO", ("IRON_CONDOR",))]
    calls.clear()
    first = _run(svc)  # best: only the 5 missing presets are built
    rest = tuple(k for k in PRESET_STRATEGIES if k != "IRON_CONDOR")
    assert calls == [("AVGO", rest), ("CRDO", rest)]
    calls.clear()
    assert _run(svc) == first
    _run(svc, mode="all")
    _run(svc, min_pop_pct=55.0)
    _run(svc, mode="selected", strategy_kind="BEAR_PUT_SPREAD")
    _run(svc, dte_min=30, dte_max=120, min_ror_pct=0.0, min_net_credit_dollars=0.0)  # = defaults
    assert calls == []
    assert svc.screener.cache_size == 2

    _run(svc, min_ror_pct=25.0)
    _run(svc, dte_min=35, dte_max=70)
    assert len(calls) == 4 and svc.screener.cache_size == 6

    # A ticker leaving the watchlist is pruned on the next run.
    run_coro(svc.watchlist.remove_ticker("CRDO"))
    _run(svc)
    assert svc.screener.cache_size == 3
    svc.screener.clear_cache()
    assert svc.screener.cache_size == 0


def test_cache_is_lru_bounded(tmp_path: Path) -> None:
    repo = CSVRepository(root=tmp_path)
    for ticker in ("AAA", "BBB"):
        repo.save_snapshot(ticker, _relabel(_chain_rows("PLTR", _TS), ticker), spot_source="MID")
    source = _FakeSource({})
    screener = _screener(repo, source, ("AAA", "BBB"), cache_max_entries=1)
    _run(screener)
    assert screener.cache_size == 1
    source.build_calls.clear()
    _run(screener)
    assert [c[0] for c in source.build_calls] == ["AAA", "BBB"]  # AAA was evicted by BBB
    with pytest.raises(ValueError, match="cache_max_entries"):
        _screener(repo, source, ("AAA",), cache_max_entries=0)


def test_zero_ibkr_and_never_takes_refresh_lock(fixture_repo: CSVRepository) -> None:
    svc = _analyzer(fixture_repo, ("NVDA",), _Clock(_NOW))
    assert run_coro(svc.acquire_refresh("batch:AVGO"))
    try:
        resp = _run(svc, mode="all")
        assert resp.counts.ok == 6
        assert svc.refresh_lock.locked() and svc.refresh_owner == "batch:AVGO"
    finally:
        svc.release_refresh()
    # The golden fixture has no open interest: the lowest-leg OI must stay unknown.
    assert all(r.min_leg_open_interest is None for r in resp.rows)


def test_index_unavailable_degrades_to_no_data_rows(
    fixture_repo: CSVRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = _analyzer(fixture_repo, ("NVDA", "AVGO"), _Clock(_NOW))

    def _broken(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise StorageSchemaError("index exploded")

    monkeypatch.setattr(svc.repo, "latest_snapshots", _broken)
    resp = _run(svc)
    assert [(r.ticker, r.status, r.status_reason) for r in resp.rows] == [
        ("NVDA", "no_data", INDEX_UNAVAILABLE_REASON),
        ("AVGO", "no_data", INDEX_UNAVAILABLE_REASON),
    ]
    assert resp.notices == [INDEX_UNAVAILABLE_NOTICE]


# ------------------------------------------------------------------ crafted source
def _leg(
    side: LegSide,
    right: OptionRight,
    strike: float,
    *,
    expiry: str = "2026-12-18",
    oi: int | None = 500,
    qty: int = 1,
) -> EvaluatedLeg:
    return EvaluatedLeg(
        side=side,
        right=right,
        expiry=expiry,
        dte=37,
        strike=strike,
        qty=qty,
        bid=1.0,
        ask=1.2,
        mid=1.1,
        natural_fill=1.0,
        spread_pct=18.0,
        iv=0.4,
        delta=0.3,
        gamma=0.01,
        theta=-0.02,
        vega=0.1,
        open_interest=oi,
        volume=10,
        option_conid=1,
    )


def _crafted(
    ticker: str,
    kind: PresetStrategyKind,
    *,
    ror: float | None,
    pop: float | None,
    credit: float = 120.0,
) -> StrategyEvaluationResponse:
    return StrategyEvaluationResponse(
        underlying=ticker,
        underlying_price=100.0,
        strategy_kind=kind,
        short_expiry="2026-12-18",
        short_dte=37,
        long_expiry="2026-12-18",
        long_dte=37,
        days_forward=0,
        iv_shift=0.0,
        qty_multiplier=1,
        risk_free_rate=0.0425,
        primary_call_wall=110.0,
        primary_put_wall=90.0,
        legs=[_leg("SELL", "P", 95.0), _leg("BUY", "P", 90.0, oi=120)],
        pricing=StrategyPricing(
            is_credit=credit >= 0.0,
            net_mid_per_share=credit / 100.0,
            net_natural_per_share=credit / 100.0,
            mid_cash_total=credit,
            natural_cash_total=credit,
            slippage_dollars=0.0,
            slippage_pct=None,
            limit_ladder=[],
        ),
        risk=StrategyRiskMetrics(
            max_profit=120.0,
            max_loss=380.0,
            max_loss_unbounded=False,
            return_on_risk_pct=ror,
            breakevens=[93.8],
            prob_of_profit_pct=pop,
            expected_move_1sigma_pct=10.0,
            expected_move_1sigma_low=90.0,
            expected_move_1sigma_high=110.0,
        ),
        net_greeks=PositionGreeks(
            delta_shares=5.0, gamma_shares=0.1, theta_daily_dollars=2.0, vega_per_1pct_dollars=-3.0
        ),
        price_grid=[90.0, 100.0, 110.0],
        curves=[],
        rationale=[f"crafted {kind}"],
    )


_Crafted = StrategyEvaluationResponse | ValueError


class _FakeSource:
    """`ScreenerCandidateSource` serving crafted outcomes per (ticker, preset)."""

    def __init__(
        self,
        table: dict[tuple[str, PresetStrategyKind], _Crafted],
        *,
        load_errors: dict[str, Exception] | None = None,
        build_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.table = table
        self.load_errors = load_errors or {}
        self.build_errors = build_errors or {}
        self.build_calls: BuildCalls = []

    def load_enriched_snapshot(self, raw_ticker: str, snapshot_id: str) -> pl.DataFrame:
        exc = self.load_errors.get(raw_ticker)
        if exc is not None:
            raise exc
        return pl.DataFrame({"snapshot_id": [snapshot_id]})

    def build_strategy_candidates(
        self,
        symbol: str,
        enriched: pl.DataFrame,
        kinds: Sequence[PresetStrategyKind],
        *,
        target_expiry: str | None = None,
        dte_min: int | None = None,
        dte_max: int | None = None,
        days_forward: int = 0,
        iv_shift: float = 0.0,
        qty_multiplier: int = 1,
        min_net_credit_dollars: float | None = None,
        min_ror_pct: float | None = None,
    ) -> list[StrategyCandidateOutcome]:
        assert (target_expiry, days_forward, iv_shift, qty_multiplier) == (None, 0, 0.0, 1)
        self.build_calls.append((symbol, tuple(kinds)))
        exc = self.build_errors.get(symbol)
        if exc is not None:
            raise exc
        out: list[StrategyCandidateOutcome] = []
        for kind in kinds:
            item = self.table.get((symbol, kind), ValueError(f"{kind} not crafted"))
            if isinstance(item, ValueError):
                out.append(StrategyCandidateOutcome(kind=kind, error=item))
            else:
                out.append(StrategyCandidateOutcome(kind=kind, evaluation=item))
        return out


def _relabel(rows: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    snap_id = make_snapshot_id(ticker, _TS)
    return [{**r, "underlying": ticker, "snapshot_id": snap_id} for r in rows]


def _screener(
    repo: CSVRepository,
    source: _FakeSource,
    tickers: Sequence[str],
    *,
    cache_max_entries: int = 240,
) -> ScreenerService:
    clock = _Clock(_NOW)
    store = WatchlistStore(
        resolve_watchlist_path(repo.storage, root=repo.root),
        WatchlistSettings(hardcoded_tickers=tuple(tickers), max_size=20),
        clock=clock,
    )
    watchlist = WatchlistService(store, repo, clock=clock, runner=_inline)
    return ScreenerService(
        source,
        watchlist,
        repo,
        settings=Settings(),
        clock=clock,
        runner=_inline,
        cache_max_entries=cache_max_entries,
    )


def _crafted_repo(tmp_path: Path, tickers: Sequence[str]) -> CSVRepository:
    repo = CSVRepository(root=tmp_path)
    for ticker in tickers:
        rows = _relabel(_chain_rows("PLTR", _TS, strikes_each_side=1, dtes=(37,)), ticker)
        repo.save_snapshot(ticker, rows, spot_source="MID")
    return repo


def test_best_tie_breaks_on_pop_then_preset_order(tmp_path: Path) -> None:
    tickers = ("AAA", "BBB", "CCC", "DDD")
    table: dict[tuple[str, PresetStrategyKind], _Crafted] = {
        ("AAA", "BULL_PUT_SPREAD"): _crafted("AAA", "BULL_PUT_SPREAD", ror=50.0, pop=60.0),
        ("AAA", "IRON_CONDOR"): _crafted("AAA", "IRON_CONDOR", ror=50.0, pop=70.0),
        ("AAA", "BEAR_PUT_SPREAD"): _crafted("AAA", "BEAR_PUT_SPREAD", ror=49.9, pop=99.0),
        ("BBB", "BULL_CALL_SPREAD"): _crafted("BBB", "BULL_CALL_SPREAD", ror=40.0, pop=55.0),
        ("BBB", "BEAR_PUT_SPREAD"): _crafted("BBB", "BEAR_PUT_SPREAD", ror=40.0, pop=55.0),
        ("CCC", "BULL_PUT_SPREAD"): _crafted("CCC", "BULL_PUT_SPREAD", ror=None, pop=99.0),
        ("CCC", "BEAR_CALL_SPREAD"): _crafted("CCC", "BEAR_CALL_SPREAD", ror=5.0, pop=10.0),
        ("DDD", "DIAGONAL_SPREAD"): _crafted("DDD", "DIAGONAL_SPREAD", ror=None, pop=40.0),
    }
    screener = _screener(_crafted_repo(tmp_path, tickers), _FakeSource(table), tickers)
    resp = _run(screener)
    assert [(r.ticker, r.strategy_kind) for r in resp.rows] == [
        ("AAA", "IRON_CONDOR"),  # RoR tie -> higher POP
        ("BBB", "BULL_CALL_SPREAD"),  # RoR + POP tie -> preset order
        ("CCC", "BEAR_CALL_SPREAD"),  # any RoR beats an undefined one
        ("DDD", "DIAGONAL_SPREAD"),  # the only buildable preset, RoR None -> last
    ]
    assert [r.status for r in resp.rows] == ["ok"] * 4
    aaa = resp.rows[0]
    assert aaa.row_id == "AAA:IRON_CONDOR" and aaa.legs_label == "\u2212P95 / +P90"
    assert aaa.min_leg_open_interest == 120 and aaa.rationale == ["crafted IRON_CONDOR"]
    assert aaa.is_today_et is True and aaa.snapshot_id == make_snapshot_id("AAA", _TS)

    # Default order in `all`: RoR desc, None last, ties by watchlist then preset order.
    every = _run(screener, mode="all")
    assert [r.row_id for r in every.rows] == [
        "AAA:BULL_PUT_SPREAD",
        "AAA:IRON_CONDOR",
        "AAA:BEAR_PUT_SPREAD",
        "BBB:BULL_CALL_SPREAD",
        "BBB:BEAR_PUT_SPREAD",
        "CCC:BEAR_CALL_SPREAD",
        "CCC:BULL_PUT_SPREAD",
        "DDD:DIAGONAL_SPREAD",
    ]
    assert every.counts.model_dump() == {"ok": 8, "filtered": 0, "not_buildable": 16, "no_data": 0}


def test_crafted_filters_and_status_reasons(tmp_path: Path) -> None:
    tickers = ("AAA", "BBB", "CCC", "DDD", "EEE")
    table: dict[tuple[str, PresetStrategyKind], _Crafted] = {
        # Debit preset below a min credit stays; credit Iron Condor below it is removed.
        ("AAA", "BULL_CALL_SPREAD"): _crafted(
            "AAA", "BULL_CALL_SPREAD", ror=150.0, pop=40.0, credit=-600.0
        ),
        ("AAA", "IRON_CONDOR"): _crafted("AAA", "IRON_CONDOR", ror=200.0, pop=35.0, credit=90.0),
        # Builder-level floor rejection vs plain not-buildable.
        ("BBB", "BULL_PUT_SPREAD"): StrategyFilterError("BELOW_MIN_ROR", "No OTM Bull Put met..."),
        ("BBB", "BEAR_CALL_SPREAD"): StrategyFilterError(
            "INSUFFICIENT_WINGS", "Insufficient higher-strike Call wings\nfor Bear Call Spread."
        ),
        # RoR / POP undefined fail an active floor.
        ("CCC", "BEAR_PUT_SPREAD"): _crafted("CCC", "BEAR_PUT_SPREAD", ror=None, pop=None),
        # An engine-internal validation error never leaks its raw text.
        ("CCC", "IRON_CONDOR"): _validation_error(),
    }
    source = _FakeSource(
        table,
        load_errors={"DDD": SnapshotNotFoundError("Snapshot not found: DDD_20261111_153000")},
        build_errors={"EEE": RuntimeError("secret C:\\Users\\x\\token")},
    )
    screener = _screener(_crafted_repo(tmp_path, tickers), source, tickers)
    floors: dict[str, Any] = {"min_net_credit_dollars": 100.0, "min_ror_pct": 20.0}

    every = _run(screener, mode="all", **floors)
    assert [r.row_id for r in every.rows] == ["AAA:BULL_CALL_SPREAD"]

    best = _by_id(_run(screener, **floors, min_pop_pct=30.0))
    assert best["AAA:BULL_CALL_SPREAD"].status == "ok"
    assert (best["BBB"].status, best["BBB"].failed_filters) == ("filtered", ["min_ror_pct"])
    assert best["BBB"].status_reason == "min RoR 20%"
    assert (best["CCC"].status, best["CCC"].failed_filters) == (
        "filtered",
        ["min_ror_pct", "min_pop_pct"],
    )
    assert best["CCC"].status_reason == "min RoR 20%, min POP 30%"
    assert (best["DDD"].status, best["DDD"].status_reason) == (
        "no_data",
        "Snapshot not found: DDD_20261111_153000",
    )
    assert best["DDD"].snapshot_id is None
    assert (best["EEE"].status, best["EEE"].status_reason) == (
        "not_buildable",
        GENERIC_ERROR_MESSAGE,
    )
    assert best["EEE"].snapshot_id == make_snapshot_id("EEE", _TS)
    assert "secret" not in _run(screener, **floors).model_dump_json()

    condor = _by_id(_run(screener, mode="selected", strategy_kind="IRON_CONDOR", **floors))
    assert condor["AAA:IRON_CONDOR"].failed_filters == ["min_net_credit_dollars"]
    assert condor["AAA:IRON_CONDOR"].status_reason == "min credit $100.00"
    internal = _run(screener, mode="selected", strategy_kind="IRON_CONDOR")
    assert _by_id(internal)["CCC:IRON_CONDOR"].status_reason == GENERIC_ERROR_MESSAGE
    assert "pydantic" not in internal.model_dump_json()
    bear_call = _by_id(_run(screener, mode="selected", strategy_kind="BEAR_CALL_SPREAD"))
    wings = bear_call["BBB:BEAR_CALL_SPREAD"]
    assert (wings.status, wings.skip_reason) == ("not_buildable", "INSUFFICIENT_WINGS")
    assert wings.status_reason == "Insufficient higher-strike Call wings for Bear Call Spread."

    # Failures are never cached; deterministic outcomes are.
    source.build_calls.clear()
    _run(screener, **floors, min_pop_pct=30.0)
    assert [c[0] for c in source.build_calls] == ["EEE"]


def test_best_merges_not_buildable_reasons(tmp_path: Path) -> None:
    tickers = ("AAA", "BBB", "CCC")
    table: dict[tuple[str, PresetStrategyKind], _Crafted] = {}
    for kind in PRESET_STRATEGIES:
        table[("AAA", kind)] = ValueError("Not enough strikes")
        table[("CCC", kind)] = ValueError(f"{kind} " + "x" * 80)
    screener = _screener(_crafted_repo(tmp_path, tickers), _FakeSource(table), tickers)
    rows = _by_id(_run(screener))
    assert rows["AAA"].status_reason == "Not enough strikes"
    merged = rows["BBB"].status_reason or ""
    assert merged == "; ".join(f"{k}: {k} not crafted" for k in PRESET_STRATEGIES)
    capped = rows["CCC"].status_reason or ""
    assert len(capped) == 300 and capped.endswith("...")
    assert all(rows[t].status == "not_buildable" and rows[t].skip_reason is None for t in tickers)


# ------------------------------------------------------------------ request & helpers
def test_request_validation_and_param_normalization(fixture_repo: CSVRepository) -> None:
    with pytest.raises(ValidationError, match="strategy_kind is required"):
        ScreenerRunRequest(mode="selected")
    with pytest.raises(ValidationError):
        ScreenerRunRequest.model_validate({"mode": "best", "ticker": "NVDA"})
    with pytest.raises(ValidationError, match="dte_min must be <= dte_max"):
        ScreenerRunRequest(dte_min=60, dte_max=30)
    for bad in (
        {"min_pop_pct": 100.5},
        {"min_ror_pct": -1.0},
        {"min_net_credit_dollars": float("inf")},
        {"dte_max": 121},
        {"mode": "top"},
        {"strategy_kind": "CUSTOM", "mode": "selected"},
    ):
        with pytest.raises(ValidationError):
            ScreenerRunRequest.model_validate(bad)

    svc = _analyzer(fixture_repo, ("NVDA",), _Clock(_NOW))
    params = svc.screener.resolve_params(
        ScreenerRunRequest(
            strategy_kind="IRON_CONDOR",  # ignored outside `selected`
            min_net_credit_dollars=0.0,
            min_ror_pct=12.5,
            min_pop_pct=0.0,
        )
    )
    assert params == ScreenerParams(
        mode="best",
        strategy_kind=None,
        dte_min=30,
        dte_max=120,
        min_net_credit_dollars=None,
        min_ror_pct=12.5,
        min_pop_pct=None,
    )
    assert (params.qty_multiplier, params.days_forward, params.iv_shift) == (1, 0, 0.0)
    with pytest.raises(InvalidRequestError, match="dte_min must be <= dte_max"):
        svc.screener.resolve_params(ScreenerRunRequest(dte_max=20))  # config dte_min is 30
    with pytest.raises(InvalidRequestError, match="strategy_kind is required"):
        svc.screener.resolve_params(ScreenerRunRequest.model_construct(mode="selected"))
    with pytest.raises(InvalidRequestError, match="min_pop_pct must be finite"):
        svc.screener.resolve_params(ScreenerRunRequest.model_construct(min_pop_pct=101.0))


def test_legs_label_format() -> None:
    assert format_legs_label([_leg("SELL", "P", 150.0), _leg("BUY", "P", 145.0)]) == (
        "\u2212P150 / +P145"
    )
    condor = [
        _leg("BUY", "P", 135.0),
        _leg("SELL", "P", 140.0),
        _leg("SELL", "C", 150.0),
        _leg("BUY", "C", 155.0),
    ]
    assert format_legs_label(condor) == "+P135 / \u2212P140 / \u2212C150 / +C155"
    diagonal = [
        _leg("BUY", "C", 130.0, expiry="2027-01-08"),
        _leg("SELL", "C", 147.5, expiry="2026-12-18"),
    ]
    assert format_legs_label(diagonal) == "+C130 2027-01-08 / \u2212C147.5 2026-12-18"
    assert format_legs_label([_leg("BUY", "C", 12.25, qty=2)]) == "+2xC12.25"
    assert format_legs_label([]) == ""


def test_min_leg_open_interest_is_unknown_when_any_leg_is_unknown() -> None:
    assert min_leg_open_interest([_leg("SELL", "P", 95.0, oi=700), _leg("BUY", "P", 90.0)]) == 500
    assert min_leg_open_interest([_leg("SELL", "P", 95.0, oi=None), _leg("BUY", "P", 90.0)]) is None
    assert min_leg_open_interest([_leg("SELL", "P", 95.0, oi=0)]) == 0
    assert min_leg_open_interest([]) is None


def test_failed_filters_follow_candidate_passes_filters() -> None:
    params = ScreenerParams(
        mode="all",
        dte_min=30,
        dte_max=120,
        min_net_credit_dollars=100.0,
        min_ror_pct=20.0,
        min_pop_pct=50.0,
    )
    debit = _crafted("AAA", "BEAR_PUT_SPREAD", ror=30.0, pop=55.0, credit=-250.0)
    credit = _crafted("AAA", "BULL_PUT_SPREAD", ror=30.0, pop=55.0, credit=99.99)
    undefined = _crafted("AAA", "DIAGONAL_SPREAD", ror=None, pop=None, credit=-10.0)
    assert candidate_filters_for("BEAR_PUT_SPREAD", params) == StrategyCandidateFilters(
        min_ror_pct=20.0, min_pop_pct=50.0
    )
    assert candidate_filters_for("IRON_CONDOR", params).min_net_credit_dollars == 100.0
    assert failed_filters_for("BEAR_PUT_SPREAD", debit.pricing, debit.risk, params) == []
    assert failed_filters_for("BULL_PUT_SPREAD", credit.pricing, credit.risk, params) == [
        "min_net_credit_dollars"
    ]
    assert failed_filters_for("DIAGONAL_SPREAD", undefined.pricing, undefined.risk, params) == [
        "min_ror_pct",
        "min_pop_pct",
    ]
    cases: tuple[tuple[PresetStrategyKind, StrategyEvaluationResponse], ...] = (
        ("BEAR_PUT_SPREAD", debit),
        ("BULL_PUT_SPREAD", credit),
        ("DIAGONAL_SPREAD", undefined),
    )
    for kind, ev in cases:
        combined = candidate_passes_filters(
            ev.pricing, ev.risk, candidate_filters_for(kind, params)
        )
        assert combined is (failed_filters_for(kind, ev.pricing, ev.risk, params) == [])


def test_empty_watchlist_returns_no_rows(tmp_path: Path) -> None:
    screener = _screener(CSVRepository(root=tmp_path), _FakeSource({}), ())
    resp = _run(screener, mode="all")
    assert (resp.rows, resp.tickers) == ([], [])
    assert resp.counts.model_dump() == {"ok": 0, "filtered": 0, "not_buildable": 0, "no_data": 0}
