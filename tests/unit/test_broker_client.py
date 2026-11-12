"""IBKRBrokerClient: read-only lock, connection errors, error-code handling, market data types."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ib_async import Option

from src.broker.client import IBKRBrokerClient
from src.broker.fetcher import (
    PagedOptionChainFetcher,
)
from src.core.config import IBKRSettings, Settings
from src.core.exceptions import (
    GatewayConnectionError,
    SecurityViolationError,
)
from src.core.security import ReadOnlyIBGuard
from tests.support.aio import noop_sleep, run_coro
from tests.support.ib_fakes import FakeChainIB


def test_client_locks_ib_and_connects_type3_default() -> None:
    fake_ib = FakeChainIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    assert ReadOnlyIBGuard.verify_locked(client.ib)
    with pytest.raises(SecurityViolationError):
        client.ib.placeOrder("bad")

    run_coro(client.ensure_connected())
    assert client.is_connected
    assert client.current_type == 3
    assert client.get_active_market_data_type() == 3
    assert fake_ib.market_data_types == [3]

    # Switching to fallback cascades 3 -> 4
    assert client.can_fallback()
    assert client.switch_to_fallback_data_type() == 4
    assert client.current_type == 4
    assert client.get_active_market_data_type() == 4
    assert not client.can_fallback()

    # If client.marketDataType is exposed as a valid int, get_active_market_data_type reads it
    fake_ib.client.marketDataType = 2
    assert client.get_active_market_data_type() == 2
    delattr(fake_ib.client, "marketDataType")
    assert client.get_active_market_data_type() == 4

    client.disconnect()
    assert not client.is_connected


def test_client_connection_errors_raise_gateway_error() -> None:
    fake_ib = FakeChainIB(connect_exc=ConnectionRefusedError("refused"))
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    with pytest.raises(GatewayConnectionError):
        run_coro(client.ensure_connected())
    assert not client.is_connected

    fake_ib2 = FakeChainIB(connect_ok=False)
    client2 = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib2)
    with pytest.raises(GatewayConnectionError):
        run_coro(client2.ensure_connected())


def test_client_error_event_absorbs_10089_354_and_flags_pacing() -> None:
    live_settings = Settings(
        ibkr=IBKRSettings(
            market_data_mode="LIVE",
            default_market_data_type=1,
            fallback_market_data_type=2,
        )
    )
    fake_ib = FakeChainIB()
    client = IBKRBrokerClient(live_settings, ib_factory=lambda: fake_ib)
    assert client.current_type == 1

    # Info codes are ignored
    for code in (2104, 2106, 2107, 2108, 2158):
        fake_ib.errorEvent.emit(-1, code, "farm ok", None)
    assert client.subscription_warning_count == 0

    # Subscription warnings (10089 / 354) do not raise and cascade LIVE -> Type 3 Delayed
    fake_ib.errorEvent.emit(10, 10089, "Requested market data requires subscription", None)
    fake_ib.errorEvent.emit(11, 354, "Not subscribed", None)
    assert client.subscription_warning_count == 2
    assert client.live_subscription_missing is True
    assert client.current_type == 3

    # Pacing warnings (100 / 162) set the pacing flag
    assert not client.consume_pacing_warning()
    fake_ib.errorEvent.emit(-1, 100, "Max rate of messages exceeded", None)
    assert client.consume_pacing_warning() is True
    assert client.consume_pacing_warning() is False


def test_client_configured_types_ticker_observation_and_live_midpage_downgrade() -> None:
    # 1. Explicit primary=2, fallback=2 starts directly on Frozen without false Type 1 reset
    frozen_cfg = Settings(
        ibkr=IBKRSettings(
            market_data_mode="LIVE",
            default_market_data_type=2,
            fallback_market_data_type=2,
        )
    )
    fake_frozen_ib = FakeChainIB()
    frozen_client = IBKRBrokerClient(frozen_cfg, ib_factory=lambda: fake_frozen_ib)
    assert frozen_client.primary_type == 2
    assert frozen_client.fallback_type == 2
    assert frozen_client.can_fallback() is False

    # Empty Ticker(marketDataType=1) default must NOT overwrite Type 3 delayed session
    delayed_client = IBKRBrokerClient(Settings(), ib_factory=lambda: FakeChainIB())
    empty_delayed_tk = SimpleNamespace(
        marketDataType=1, bid=float("nan"), ask=float("nan"), last=float("nan"), close=float("nan")
    )
    assert delayed_client.observe_tickers_market_data_type([empty_delayed_tk]) == 3

    # Real Type 4 callback on delayed ticker updates active market data type dynamically
    tk4 = SimpleNamespace(marketDataType=4, bid=-1.0, ask=-1.0, last=-1.0, close=4.2)
    assert delayed_client.observe_tickers_market_data_type([tk4]) == 4
    assert delayed_client.get_active_market_data_type() == 4

    # 2. Live fetcher catches Error 10089 mid-page-1, logs notice, and re-collects under Type 3
    live_cfg = Settings(
        ibkr=IBKRSettings(
            market_data_mode="LIVE",
            default_market_data_type=1,
            fallback_market_data_type=2,
        )
    )
    fake_live_ib = FakeChainIB()
    live_client = IBKRBrokerClient(live_cfg, ib_factory=lambda: fake_live_ib)
    run_coro(live_client.ensure_connected())
    assert live_client.get_active_market_data_type() == 1

    def _downgrade_factory(contract: Any, _ticks: str) -> Any:
        if live_client.current_type == 1:
            fake_live_ib.errorEvent.emit(
                contract.conId or 1,
                10089,
                "Requested market data requires additional subscription for API",
                contract,
            )
            return SimpleNamespace(contract=contract, bid=-1.0, ask=-1.0, last=-1.0, close=-1.0)
        return SimpleNamespace(
            contract=contract,
            marketDataType=3,
            # ib_async maps delayed ticks 66/67/68 onto Ticker.bid / ask / last.
            bid=4.1,
            ask=4.3,
            last=4.2,
            delayedBid=4.1,
            delayedAsk=4.3,
            delayedLast=4.2,
            close=4.0,
        )

    fake_live_ib.ticker_factory = _downgrade_factory
    fetcher = PagedOptionChainFetcher(live_client, live_cfg, sleep=noop_sleep)
    contracts = [Option("NVDA", "20261211", 170.0, "C", "SMART", conId=501)]
    tickers, pages = run_coro(fetcher.fetch_pages(contracts))
    assert pages == 1
    assert len(tickers) == 1
    assert live_client.live_subscription_missing is True
    assert live_client.get_active_market_data_type() == 3
    assert any("MarketDataType(3)" in n for n in fetcher._notices)
    # After 10089 downgrade to Type 3, delayed-frozen fallback (3 -> 4) remains enabled
    assert live_client.can_fallback() is True
    assert live_client.switch_to_fallback_data_type() == 4
