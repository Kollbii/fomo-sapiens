"""ContractDiscoveryService: weekly DTE matching, strike selection, chain choice, exact strikes."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from ib_async import Option, Stock

from src.broker.client import IBKRBrokerClient
from src.broker.contracts import (
    EXACT_PREFETCH_MAX_CONCURRENCY,
    SPOT_LAST_MID_MAX_DEVIATION,
    ContractDiscoveryService,
    choose_auto_strike_step,
    detect_available_range_pcts,
    detect_available_strike_steps,
    detect_native_strike_step,
    extract_spot,
    match_expirations,
    select_chain,
    select_spot,
    select_strikes,
)
from src.broker.fetcher import (
    PagedOptionChainFetcher,
)
from src.core.config import OptionsChainSettings, Settings
from src.core.exceptions import (
    InvalidTickerError,
    OptionChainFetchError,
    UnderlyingPriceUnavailableError,
)
from tests.support.aio import noop_sleep, run_coro, run_virtual_loop
from tests.support.ib_fakes import FakeChainIB


def test_dte_strict_weekly_matching_and_iso_week_dedup() -> None:
    # 2026-11-11 is Wednesday.
    # Candidates inside [30, 120] inclusive: 30, 35/37, 43, 50, 56/58/60, 90/93, 120.
    # Boundaries 15, 121, 150 must be excluded by dte_min <= dte <= dte_max (30 and 120 included).
    market_date = date(2026, 11, 11)
    dtes = [15, 30, 35, 37, 43, 50, 56, 58, 60, 90, 93, 120, 121, 150]
    expirations = [(market_date + timedelta(days=d)).strftime("%Y%m%d") for d in dtes]

    matches = match_expirations(
        expirations,
        market_date=market_date,
        dte_min=30,
        dte_max=120,
    )
    # Within each ISO week, Friday overrides mid-week (37 beats 35, 58 beats 56/60, 93 beats 90).
    assert [m.dte for m in matches] == [30, 37, 43, 50, 58, 93, 120]
    assert all(30 <= m.dte <= 120 for m in matches)
    assert len({m.ib_expiry for m in matches}) == 7


def test_select_strikes_moneyness_atm_anchor_and_auto_widening() -> None:
    spot = 142.40
    # Dense $1.00 strikes from $50 to $250
    all_strikes = [float(x) for x in range(50, 251)]

    # +-20% around 142.40 -> [113.92, 170.88], grid-aligned $5 multiples anchored on 140.0
    sel = select_strikes(all_strikes, spot=spot, range_pct=0.20, strike_step=5.0)
    assert sel.atm_strike == 140.0
    assert 140.0 in sel.strikes
    assert all(s % 5.0 == 0.0 for s in sel.strikes)
    assert not sel.widened
    assert all(142.40 * 0.80 <= s <= 142.40 * 1.20 for s in sel.strikes)
    diffs = [round(b - a, 6) for a, b in zip(sel.strikes, sel.strikes[1:], strict=False)]
    assert all(d >= 5.0 for d in diffs)

    # Mixed $2.50 short-DTE strikes must NOT trap step=$5.0 onto .50 off-grid strikes (e.g. 347.5)
    aapl_spot = 347.40
    mixed_strikes = [320.0 + 2.5 * i for i in range(25)]  # 320.0 .. 380.0 in $2.50 steps
    aapl_sel = select_strikes(mixed_strikes, spot=aapl_spot, range_pct=0.10, strike_step=5.0)
    assert aapl_sel.atm_strike == 345.0
    assert all(s % 5.0 == 0.0 for s in aapl_sel.strikes)

    # Auto strike step skips non-native $2.5 on integer-only grids and picks $5.0;
    # when .50 increments are present, it picks $2.5.
    assert choose_auto_strike_step(all_strikes, spot=spot, range_pct=0.20) == 5.0
    assert choose_auto_strike_step(mixed_strikes, spot=aapl_spot, range_pct=0.10) == 2.5

    # Force auto-widening when max_strikes cap is exceeded (e.g. 4 expirations -> max 40 strikes)
    wide = select_strikes(
        all_strikes,
        spot=spot,
        range_pct=0.50,
        strike_step=1.0,
        max_strikes=40,
    )
    assert len(wide.strikes) <= 40
    assert wide.atm_strike == 140.0
    assert 140.0 in wide.strikes
    assert wide.widened
    assert wide.effective_step >= 5.0


def test_select_chain_prefers_smart_multiplier_100_largest_grid() -> None:
    chains = [
        SimpleNamespace(
            exchange="CBOE",
            tradingClass="NVDA",
            multiplier="100",
            expirations=["20261218"] * 10,
            strikes=[100.0] * 50,
        ),
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA2",
            multiplier="100",
            expirations=["20261218"],
            strikes=[100.0, 105.0],
        ),
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=["20261211", "20261218", "20261224"],
            strikes=[130.0, 135.0, 140.0, 145.0, 150.0],
        ),
    ]
    chosen = select_chain(chains, "NVDA")
    assert chosen.exchange == "SMART"
    assert chosen.tradingClass == "NVDA"


def test_discovery_spot_fallback_and_errors() -> None:
    fake_ib = FakeChainIB()
    # First call (Type 3) returns sentinels, second call (Type 4) returns close=140.5
    call_count = 0

    def _spot_ticker(contract: Any, _ticks: str) -> Any:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SimpleNamespace(
                contract=contract, bid=-1.0, ask=-1.0, last=float("nan"), close=0.0
            )
        return SimpleNamespace(contract=contract, bid=-1.0, ask=-1.0, last=-1.0, close=140.5)

    fake_ib.ticker_factory = _spot_ticker
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, sleep=noop_sleep)

    stock = run_coro(svc.qualify_underlying("nvda"))
    spot = run_coro(svc.resolve_spot(stock))
    assert spot == 140.5
    assert client.current_type == 4
    assert len(fake_ib.cancelled) == 2

    # Unqualified ticker raises InvalidTickerError
    fake_ib.qualify_handler = lambda *_c: [None]
    with pytest.raises(InvalidTickerError):
        run_coro(svc.qualify_underlying("ZZZZ"))

    # Spot unavailable even after fallback raises UnderlyingPriceUnavailableError
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=-1.0, ask=-1.0, last=-1.0, close=-1.0
    )
    with pytest.raises(UnderlyingPriceUnavailableError):
        run_coro(svc.resolve_spot(Stock("NVDA", "SMART", "USD", conId=1)))


def test_extract_spot_priority() -> None:
    assert extract_spot(SimpleNamespace(last=150.0, bid=148.0, ask=149.0, close=140.0)) == 150.0
    assert extract_spot(SimpleNamespace(last=-1.0, bid=148.0, ask=150.0, close=140.0)) == 149.0
    t_close = SimpleNamespace(last=float("nan"), bid=-1.0, ask=150.0, close=140.0)
    assert extract_spot(t_close) == 140.0
    assert extract_spot(SimpleNamespace(last=-1.0, bid=-1.0, ask=-1.0, close=0.0)) is None
    with pytest.raises(OptionChainFetchError):
        select_chain([], "NVDA")


def test_per_expiration_intersection_prevents_phantom_strikes() -> None:
    """Monthly/weekly SMART chains with distinct strike grids must not cross-pollinate."""
    fake_ib = FakeChainIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    market_date = date(2026, 11, 11)
    exp_weekly = (market_date + timedelta(days=37)).strftime("%Y%m%d")  # Friday weekly ($2.5)
    exp_monthly = (market_date + timedelta(days=43)).strftime("%Y%m%d")  # Next week ($5 only)
    weekly_strikes = [120.0 + 2.5 * i for i in range(17)]  # 120.0..160.0 in $2.5 steps
    monthly_strikes = [float(s) for s in range(120, 165, 5)]  # $5 steps only (no 137.5, 142.5)
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=[exp_weekly],
            strikes=weekly_strikes,
        ),
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=[exp_monthly],
            strikes=monthly_strikes,
        ),
    ]
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=139.9, ask=140.1, last=140.0, close=139.8
    )
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, clock=lambda: now, sleep=noop_sleep)

    plan = run_coro(svc.build_plan("NVDA", range_pct=0.20, strike_step=2.5))
    spec_by_exp = {spec.match.ib_expiry: spec for spec in plan.expiration_specs}
    assert set(spec_by_exp) == {exp_weekly, exp_monthly}
    assert 137.5 in spec_by_exp[exp_weekly].strikes
    assert 137.5 not in spec_by_exp[exp_monthly].strikes
    assert 142.5 not in spec_by_exp[exp_monthly].strikes
    assert spec_by_exp[exp_monthly].effective_step == pytest.approx(5.0)

    fetcher = PagedOptionChainFetcher(client, sleep=noop_sleep)
    built = fetcher.build_candidate_contracts(plan)
    monthly_built_strikes = {
        float(c.strike) for c in built if c.lastTradeDateOrContractMonth == exp_monthly
    }
    assert 137.5 not in monthly_built_strikes
    assert 142.5 not in monthly_built_strikes


def test_non_standard_10_dollar_step_adapts_with_notice() -> None:
    fake_ib = FakeChainIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    market_date = date(2026, 11, 11)
    exps = [(market_date + timedelta(days=d)).strftime("%Y%m%d") for d in (37, 43)]
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=exps,
            strikes=[float(s) for s in range(100, 190, 10)],
        )
    ]
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=139.9, ask=140.1, last=140.0, close=139.8
    )
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, clock=lambda: now, sleep=noop_sleep)
    plan = run_coro(svc.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert plan.exchange_native_verified is True
    assert plan.exchange_validated_strike_count > 0
    assert plan.strikes.effective_step == pytest.approx(10.0)
    assert 10.0 in plan.available_strike_steps
    assert any("adapted" in n.lower() or "$10" in n for n in plan.notices)
    assert any("exchange" in n.lower() or "validated" in n.lower() for n in plan.notices)


def test_detect_available_strike_steps_and_range_pcts() -> None:
    ten_dollar_chain = [110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0]
    assert detect_native_strike_step(ten_dollar_chain) == pytest.approx(10.0)
    avail_steps = detect_available_strike_steps(
        ten_dollar_chain,
        spot=140.0,
        range_pct=0.20,
        candidate_steps=[1.0, 2.5, 5.0, 10.0],
    )
    assert avail_steps == [10.0]

    narrow_chain = [130.0, 140.0, 150.0]
    avail_ranges = detect_available_range_pcts(
        narrow_chain,
        spot=140.0,
        candidate_ranges=[0.05, 0.10, 0.20, 0.30],
    )
    assert avail_ranges == [0.10, 0.20, 0.30]


def _make_cd_list(
    symbol: str,
    ib_expiry: str,
    strikes: list[float],
    *,
    conid_start: int,
    trading_class: str = "NVDA",
    asymmetric_call_only_strike: float | None = None,
) -> list[Any]:
    details: list[Any] = []
    cid = conid_start
    for strike in strikes:
        rights = ("C",) if strike == asymmetric_call_only_strike else ("C", "P")
        for right in rights:
            cid += 1
            opt = Option(
                symbol=symbol,
                lastTradeDateOrContractMonth=ib_expiry,
                strike=strike,
                right=right,
                exchange="SMART",
                multiplier="100",
                currency="USD",
                tradingClass=trading_class,
                conId=cid,
                localSymbol=f"{symbol} {ib_expiry}{right}{int(strike * 1000):08d}",
            )
            details.append(SimpleNamespace(contract=opt))
    return details


def test_prefetch_exact_per_expiration_strikes_eliminates_phantom_strikes_and_qualify_calls() -> (
    None
):
    """Single-tradingClass pooled SMART chain is narrowed per expiry via reqContractDetailsAsync."""
    fake_ib = FakeChainIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    market_date = date(2026, 11, 11)
    exp_weekly = (market_date + timedelta(days=30)).strftime("%Y%m%d")  # Friday weekly ($2.5)
    exp_monthly = "20261218"  # Monthly OPEX (37 DTE): $5 strikes only + 1 asymmetric outer call

    weekly_strikes = [165.0 + 2.5 * i for i in range(13)]  # 165.0..195.0 including .5
    monthly_strikes = [165.0, 170.0, 175.0, 180.0, 185.0, 190.0, 195.0, 200.0]
    pooled_strikes = sorted(set(weekly_strikes) | set(monthly_strikes))

    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=[exp_weekly, exp_monthly],
            strikes=pooled_strikes,
        )
    ]

    details_queries: list[str] = []

    def _cd_handler(contract: Any) -> list[Any]:
        exp = str(contract.lastTradeDateOrContractMonth)
        details_queries.append(exp)
        assert contract.strike == 0.0
        assert contract.right == ""
        if exp == exp_weekly:
            return _make_cd_list("NVDA", exp, weekly_strikes, conid_start=50_000)
        return _make_cd_list(
            "NVDA",
            exp,
            monthly_strikes,
            conid_start=60_000,
            asymmetric_call_only_strike=200.0,
        )

    fake_ib.contract_details_handler = _cd_handler

    qualify_batches: list[int] = []

    def _track_qualify(*contracts: Any) -> list[Any]:
        qualify_batches.append(len(contracts))
        for idx, c in enumerate(contracts, start=1):
            c.conId = getattr(c, "conId", 0) or (4800 + idx)
        return list(contracts)

    fake_ib.qualify_handler = _track_qualify
    fake_ib.ticker_factory = lambda c, _t: (
        SimpleNamespace(contract=c, bid=179.9, ask=180.1, last=180.0, close=179.8)
        if getattr(c, "secType", "") == "STK"
        else SimpleNamespace(
            contract=c,
            bid=6.0,
            ask=6.4,
            last=6.2,
            close=6.1,
            volume=40,
            callOpenInterest=200,
            putOpenInterest=200,
            modelGreeks=None,
        )
    )

    sleeps: list[float] = []

    async def _rec_sleep(sec: float) -> None:
        sleeps.append(sec)

    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(client, clock=lambda: now, sleep=_rec_sleep)

    res = run_coro(fetcher.fetch_option_chain("NVDA", range_pct=0.20, strike_step=None))

    assert details_queries == [exp_weekly, exp_monthly]
    # Cooldown slept strictly between the 2 sequential lookups (not on 1st or after 2nd)
    assert 0.10 in sleeps
    # Only the underlying Stock was sent to qualifyContractsAsync (zero Option batches!)
    assert qualify_batches == [1]

    spec_by_exp = {spec.match.ib_expiry: spec for spec in res.plan.expiration_specs}
    assert spec_by_exp[exp_weekly].exact_strikes_verified is True
    assert spec_by_exp[exp_monthly].exact_strikes_verified is True
    assert spec_by_exp[exp_weekly].effective_step == pytest.approx(2.5)
    assert spec_by_exp[exp_monthly].effective_step == pytest.approx(5.0)
    assert 172.5 in spec_by_exp[exp_weekly].strikes
    for half in (172.5, 177.5, 182.5, 187.5):
        assert half not in spec_by_exp[exp_monthly].strikes

    monthly_rows = [r for r in res.rows if r.expiry == "2026-12-18"]
    # Strike 200.0 only has Call in exact ContractDetails, so Put was never requested
    rights_at_200 = {r.right for r in monthly_rows if r.strike == 200.0}
    assert rights_at_200 == {"C"}
    assert any("reqcontractdetails" in n.lower() for n in res.notices)


def test_exact_strike_prefetch_ttl_cache_and_expiration() -> None:
    fake_ib = FakeChainIB()
    clock_state = [datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)]
    exp = (clock_state[0].date() + timedelta(days=37)).strftime("%Y%m%d")
    strikes = [135.0, 140.0, 145.0]
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=[exp],
            strikes=strikes,
        )
    ]
    calls: list[str] = []

    def _cd_handler(c: Any) -> list[Any]:
        calls.append(str(c.lastTradeDateOrContractMonth))
        return _make_cd_list("NVDA", exp, strikes, conid_start=70_000)

    fake_ib.contract_details_handler = _cd_handler
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=139.9, ask=140.1, last=140.0, close=139.8
    )

    settings = Settings(
        options_chain=OptionsChainSettings(
            contract_details_cache_ttl_sec=300.0,
            contract_details_cooldown_sec=0.05,
        )
    )
    client = IBKRBrokerClient(settings, ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, settings, clock=lambda: clock_state[0], sleep=noop_sleep)

    run_coro(svc.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert len(calls) == 1

    # Inside TTL: changing range_pct reuses exact contracts cache without hitting IBKR again
    clock_state[0] = clock_state[0] + timedelta(seconds=120)
    run_coro(svc.build_plan("NVDA", range_pct=0.10, strike_step=5.0))
    assert len(calls) == 1

    # Past TTL (301s > 300s): forces a fresh reqContractDetailsAsync query
    clock_state[0] = clock_state[0] + timedelta(seconds=301)
    run_coro(svc.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert len(calls) == 2


def test_exact_strike_prefetch_falls_back_on_timeout_and_disabled_flag() -> None:
    fake_ib = FakeChainIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    exp = (now.date() + timedelta(days=37)).strftime("%Y%m%d")
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=[exp],
            strikes=[135.0, 140.0, 145.0],
        )
    ]
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=139.9, ask=140.1, last=140.0, close=139.8
    )

    # Case 1: TimeoutError from reqContractDetailsAsync falls back cleanly to reqSecDefOptParams
    async def _timeout_handler(_c: Any) -> list[Any]:
        raise TimeoutError("reqContractDetails timed out")

    fake_ib.contract_details_handler = _timeout_handler
    timeout_settings = Settings(
        options_chain=OptionsChainSettings(
            prefetch_exact_strikes=True,
            contract_details_timeout_sec=0.02,
        )
    )
    client = IBKRBrokerClient(timeout_settings, ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, timeout_settings, clock=lambda: now, sleep=noop_sleep)
    plan = run_coro(svc.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert plan.expiration_specs[0].exact_strikes_verified is False
    assert plan.strikes.strikes == (135.0, 140.0, 145.0)
    assert any("reqsecdefoptparams" in n.lower() for n in plan.notices)

    # Case 2: disabled flag never invokes reqContractDetailsAsync
    called = [False]

    def _disabled_handler(_c: Any) -> list[Any]:
        called[0] = True
        return []

    fake_ib.contract_details_handler = _disabled_handler
    disabled_settings = Settings(options_chain=OptionsChainSettings(prefetch_exact_strikes=False))
    client2 = IBKRBrokerClient(disabled_settings, ib_factory=lambda: fake_ib)
    svc2 = ContractDiscoveryService(client2, disabled_settings, clock=lambda: now, sleep=noop_sleep)
    plan2 = run_coro(svc2.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert called[0] is False
    assert plan2.expiration_specs[0].exact_strikes_verified is False


def test_prefetch_exact_contracts_runs_bounded_concurrency_in_event_loop() -> None:
    fake_ib = FakeChainIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    market_date = date(2026, 11, 11)
    dtes = [30, 37, 44, 51, 58, 65]
    exps = [(market_date + timedelta(days=d)).strftime("%Y%m%d") for d in dtes]
    strikes = [135.0, 140.0, 145.0]
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=exps,
            strikes=strikes,
        )
    ]

    active = 0
    max_active = 0

    async def _cd_handler(contract: Any) -> list[Any]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.015)
            return _make_cd_list(
                "NVDA", str(contract.lastTradeDateOrContractMonth), strikes, conid_start=80_000
            )
        finally:
            active -= 1

    fake_ib.contract_details_handler = _cd_handler
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, bid=139.9, ask=140.1, last=140.0, close=139.8
    )
    settings = Settings(
        options_chain=OptionsChainSettings(
            prefetch_exact_strikes=True,
            contract_details_cooldown_sec=0.0,
        )
    )
    client = IBKRBrokerClient(settings, ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, settings, clock=lambda: now)

    plan = run_virtual_loop(svc.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert 2 <= max_active <= EXACT_PREFETCH_MAX_CONCURRENCY
    assert [spec.match.ib_expiry for spec in plan.expiration_specs] == exps
    assert all(spec.exact_strikes_verified for spec in plan.expiration_specs)


def test_select_spot_cross_check_and_resolve_spot_with_source() -> None:
    assert SPOT_LAST_MID_MAX_DEVIATION == 0.02
    assert select_spot(SimpleNamespace(last=140.0, bid=139.9, ask=140.1, close=138.0)) == (
        140.0,
        "LAST",
    )
    # Outlier `last` >2% from bid/ask midpoint resolves to `MID`
    assert select_spot(SimpleNamespace(last=160.0, bid=139.9, ask=140.1, close=138.0)) == (
        140.0,
        "MID",
    )
    assert select_spot(SimpleNamespace(last=None, bid=139.8, ask=140.2, close=138.0)) == (
        140.0,
        "MID",
    )
    assert select_spot(SimpleNamespace(last=None, bid=None, ask=None, close=138.5)) == (
        138.5,
        "CLOSE",
    )
    assert select_spot(SimpleNamespace(last=None, bid=None, ask=None, close=None)) is None

    fake_ib = FakeChainIB()
    now = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    market_date = date(2026, 11, 11)
    exps = [(market_date + timedelta(days=d)).strftime("%Y%m%d") for d in (37, 44, 51, 58)]
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=exps,
            strikes=[135.0, 140.0, 145.0],
        )
    ]
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, last=160.0, bid=139.9, ask=140.1, close=138.0
    )
    settings = Settings(options_chain=OptionsChainSettings(prefetch_exact_strikes=False))
    client = IBKRBrokerClient(settings, ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, settings, clock=lambda: now, sleep=noop_sleep)

    plan = run_coro(svc.build_plan("NVDA", range_pct=0.20, strike_step=5.0))
    assert plan.underlying.spot == pytest.approx(140.0)
    assert plan.underlying.spot_source == "MID"


def test_short_dte_zero_dte_discovery_and_reuse() -> None:
    now = datetime(2026, 11, 13, 15, 30, 0, tzinfo=UTC)  # Friday 2026-11-13
    mkt = date(2026, 11, 13)
    # 0 DTE (same-day Friday), 7, 14, 21, 28, plus 30 & 37 DTE (excluded from [0, 29])
    exps = [(mkt + timedelta(days=d)).strftime("%Y%m%d") for d in (0, 7, 14, 21, 28, 30, 37)]
    strikes = [130.0, 135.0, 140.0, 145.0, 150.0]

    matched = match_expirations(exps, mkt, dte_min=0, dte_max=29)
    assert [m.dte for m in matched] == [0, 7, 14, 21, 28]

    qualify_calls = 0
    secdef_calls = 0

    class _TrackingFakeIB(FakeChainIB):
        async def reqSecDefOptParamsAsync(  # noqa: N802
            self, symbol: str, fut_fop_exchange: str, sec_type: str, con_id: int
        ) -> list[Any]:
            nonlocal secdef_calls
            secdef_calls += 1
            return await super().reqSecDefOptParamsAsync(symbol, fut_fop_exchange, sec_type, con_id)

    fake_ib = _TrackingFakeIB()
    fake_ib.chains_result = [
        SimpleNamespace(
            exchange="SMART",
            tradingClass="NVDA",
            multiplier="100",
            expirations=exps,
            strikes=strikes,
        )
    ]

    def _track_qualify(*contracts: Any) -> list[Any]:
        nonlocal qualify_calls
        qualify_calls += 1
        for idx, c in enumerate(contracts, start=1):
            c.conId = 4815 + idx
        return list(contracts)

    fake_ib.qualify_handler = _track_qualify
    fake_ib.ticker_factory = lambda c, _t: SimpleNamespace(
        contract=c, last=140.0, bid=139.9, ask=140.1, close=139.5
    )

    settings = Settings(options_chain=OptionsChainSettings(prefetch_exact_strikes=False))
    client = IBKRBrokerClient(settings, ib_factory=lambda: fake_ib)
    svc = ContractDiscoveryService(client, settings, clock=lambda: now, sleep=noop_sleep)

    plan1 = run_coro(
        svc.build_plan("NVDA", range_pct=0.20, strike_step=5.0, dte_min=30, dte_max=120)
    )
    assert [m.dte for m in plan1.expirations] == [30, 37]
    assert qualify_calls == 1
    assert secdef_calls == 1

    # Pass 2 reuses underlying_info, smart_chains, and market_date: zero extra qualify/secdef calls.
    plan2 = run_coro(
        svc.build_plan(
            "NVDA",
            range_pct=0.20,
            strike_step=5.0,
            dte_min=0,
            dte_max=29,
            underlying_info=plan1.underlying,
            smart_chains=plan1.smart_chains,
            market_date=plan1.market_date,
        )
    )
    assert [m.dte for m in plan2.expirations] == [0, 7, 14, 21, 28]
    assert plan2.total_contracts <= svc.max_total_contracts
    assert qualify_calls == 1
    assert secdef_calls == 1
