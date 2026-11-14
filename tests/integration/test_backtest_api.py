"""Integration tests for Backtest API routes, Paper Trades lifecycle, and error mapping."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import anyio.to_thread
import httpx
import pytest

from src.broker.client import IBKRBrokerClient
from src.broker.fetcher import PagedOptionChainFetcher
from src.core.config import PaperTrackerSettings, Settings
from src.main import create_app
from src.storage.csv_repository import CSVRepository
from src.storage.schema import OptionContractRow, SpotSource
from src.web import backtest_api
from tests.support.aio import noop_sleep, run_coro
from tests.support.ib_fakes import FakeEvent

_EXPIRY = "2026-12-18"
_PAPER_ID_RE = re.compile(r"^[A-Z]{1,5}-\d{8}-[0-9a-f]{8}$")


class _OfflineIB:
    def __init__(self) -> None:
        self.errorEvent = FakeEvent()
        self.wrapper = SimpleNamespace(clientId=0)
        self.client = SimpleNamespace(connectAsync=self._connect_async)

    async def _connect_async(self, *_args: Any, **_kwargs: Any) -> None:
        raise ConnectionRefusedError("Gateway offline in backtest API tests")

    def isConnected(self) -> bool:  # noqa: N802
        return False

    def disconnect(self) -> None:
        return None


def _patch_inline_threads(monkeypatch: pytest.MonkeyPatch, app: Any) -> Any:
    """Run AnyIO and backtest threadpool adapters inline inside `run_coro`."""

    async def _run_sync_inline(func: Callable[..., Any], *args: Any, **_kwargs: Any) -> Any:
        return func(*args)

    async def _in_thread_inline(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    monkeypatch.setattr(anyio.to_thread, "run_sync", _run_sync_inline)
    monkeypatch.setattr(backtest_api, "_in_thread", _in_thread_inline)
    monkeypatch.setattr(
        anyio.CancelScope,
        "__new__",
        lambda _cls, **_kw: contextlib.nullcontext(),
    )

    async def _asgi_24(scope: Any, receive: Any, send: Any) -> None:
        patched_scope = {**scope, "asgi": {"version": "3.0", "spec_version": "2.4"}}
        await app(patched_scope, receive, send)

    return _asgi_24


def _chain_rows(
    *,
    snapshot_id: str,
    ts_utc: str,
    market_date: str,
    spot: float,
    short_put_mid: float,
    long_put_mid: float,
    market_data_type: int = 3,
    include_quotes: bool = True,
) -> list[dict[str, Any]]:
    contracts = [
        (
            9001,
            130.0,
            "P",
            long_put_mid * 0.65,
            long_put_mid * 0.65 - 0.10,
            long_put_mid * 0.65 + 0.10,
        ),
        (9002, 135.0, "P", long_put_mid, long_put_mid - 0.10, long_put_mid + 0.10),
        (9003, 140.0, "P", short_put_mid, short_put_mid - 0.10, short_put_mid + 0.10),
        (9004, 145.0, "C", 5.20, 5.10, 5.30),
        (9005, 150.0, "C", 3.10, 3.00, 3.20),
    ]
    dte = (date.fromisoformat(_EXPIRY) - date.fromisoformat(market_date)).days
    rows: list[dict[str, Any]] = []
    for conid, strike, right, mid_val, bid_raw, ask_raw in contracts:
        bid = round(max(0.05, bid_raw), 2) if include_quotes else None
        ask = round(max(0.10, ask_raw), 2) if include_quotes else None
        mid = round(mid_val, 2) if include_quotes else None
        rows.append(
            OptionContractRow(
                snapshot_id=snapshot_id,
                snapshot_ts_utc=ts_utc,
                market_date=market_date,
                market_data_type=market_data_type,
                underlying="NVDA",
                underlying_conid=4815747,
                underlying_price=spot,
                option_conid=conid,
                expiry=_EXPIRY,
                dte=dte,
                strike=strike,
                moneyness_pct=(strike - spot) / spot,
                right=right,  # type: ignore[arg-type]
                bid=bid,
                ask=ask,
                mid=mid,
                last=mid,
                close=round(mid_val, 2) if include_quotes else None,
                mark_price=mid,
                volume=100,
                open_interest=500,
                iv=0.42,
                delta=-0.22 if right == "P" else 0.25,
                gamma=0.02,
                theta=-0.04,
                vega=0.11,
                greeks_source="IBKR_MODEL",
            ).model_dump()
        )
    return rows


def _build_app(
    tmp_path: Path,
    *,
    max_active_trades: int = 20,
    seed_history: bool = True,
) -> Any:
    settings = Settings(
        tracker=PaperTrackerSettings(
            max_active_trades=max_active_trades,
            tracked_dir="data/tracked",
        )
    )
    repo = CSVRepository(settings.storage, root=tmp_path)
    broker = IBKRBrokerClient(settings, ib_factory=_OfflineIB)
    fixed_now = datetime(2026, 11, 12, 19, 0, tzinfo=UTC)
    fetcher = PagedOptionChainFetcher(
        broker,
        settings,
        clock=lambda: fixed_now,
        sleep=noop_sleep,
    )
    repo.ensure_layout()
    if seed_history:
        schedule: list[tuple[str, str, str, float, float, float, int, SpotSource, bool]] = [
            (
                "NVDA_20261105_190000",
                "2026-11-05T19:00:00Z",
                "2026-11-05",
                145.0,
                3.20,
                2.00,
                3,
                "MID",
                True,
            ),
            (
                "NVDA_20261106_190000",
                "2026-11-06T19:00:00Z",
                "2026-11-06",
                146.0,
                2.80,
                1.80,
                3,
                "MID",
                True,
            ),
            (
                "NVDA_20261109_150000",
                "2026-11-09T15:00:00Z",
                "2026-11-09",
                147.0,
                2.40,
                1.60,
                4,
                "CLOSE",
                False,
            ),
            (
                "NVDA_20261110_190000",
                "2026-11-10T19:00:00Z",
                "2026-11-10",
                148.0,
                1.90,
                1.25,
                3,
                "LAST",
                True,
            ),
            (
                "NVDA_20261112_190000",
                "2026-11-12T19:00:00Z",
                "2026-11-12",
                150.0,
                1.20,
                0.80,
                3,
                "MID",
                True,
            ),
        ]
        for (
            snap_id,
            ts_utc,
            mkt_date,
            spot,
            sp_mid,
            lp_mid,
            md_type,
            spot_src,
            has_quotes,
        ) in schedule:
            repo.save_snapshot(
                "NVDA",
                _chain_rows(
                    snapshot_id=snap_id,
                    ts_utc=ts_utc,
                    market_date=mkt_date,
                    spot=spot,
                    short_put_mid=sp_mid,
                    long_put_mid=lp_mid,
                    market_data_type=md_type,
                    include_quotes=has_quotes,
                ),
                spot_source=spot_src,
            )
    app = create_app(
        settings=settings,
        repo=repo,
        broker_client=broker,
        fetcher=fetcher,
    )
    return app


def test_backtest_entries_and_invalid_ticker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_app(tmp_path)
    asgi_app = _patch_inline_threads(monkeypatch, app)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r = await client.get("/api/backtest/entries/nvda")
                assert r.status_code == 200
                body = r.json()
                assert body["ticker"] == "NVDA"
                entries = body["entries"]
                assert len(entries) == 5
                assert entries[0]["snapshot_id"] == "NVDA_20261112_190000"
                assert entries[0]["usable"] is True
                flagged = next(e for e in entries if e["snapshot_id"] == "NVDA_20261109_150000")
                assert flagged["usable"] is False
                assert flagged["quality_reasons"]

                r_empty = await client.get("/api/backtest/entries/MSFT")
                assert r_empty.status_code == 200
                assert r_empty.json()["entries"] == []

                r_bad = await client.get("/api/backtest/entries/BAD!")
                assert r_bad.status_code == 422
                assert r_bad.json()["error_code"] == "INVALID_TICKER"

    run_coro(_exercise())


def test_backtest_run_modes_switches_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_app(tmp_path)
    asgi_app = _patch_inline_threads(monkeypatch, app)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r_sys = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SYSTEMATIC",
                        "ticker": "NVDA",
                        "config": {
                            "strategy_kind": "BULL_PUT_SPREAD",
                            "entry_cadence": "EVERY_N_DAYS",
                            "entry_every_n_days": 1,
                            "price_basis": "AUTO_MID_LAST_CLOSE",
                            "close_price_mode": "MID",
                            "tp_sl_basis": "CREDIT_DEBIT_PCT",
                            "profit_target_pct": 50.0,
                            "stop_loss_pct": 100.0,
                        },
                    },
                )
                assert r_sys.status_code == 200
                sys_body = r_sys.json()
                assert sys_body["mode"] == "SYSTEMATIC"
                assert sys_body["daily_bar_count"] >= 4
                assert sys_body["simulation"]["config"]["tp_sl_basis"] == "CREDIT_DEBIT_PCT"
                assert sys_body["simulation"]["summary"]["trade_count"] >= 1
                assert "realized_pnl_dollars" in sys_body["simulation"]["summary"]
                assert "unrealized_pnl_dollars" in sys_body["simulation"]["summary"]
                assert sys_body["simulation"]["equity_curve"]

                first_trade = sys_body["simulation"]["trades"][0]
                assert "selection_notes" in first_trade
                assert isinstance(first_trade["selection_notes"], list)
                assert first_trade["profit_target_dollars"] is not None
                assert first_trade["stop_loss_dollars"] is not None
                assert first_trade["stop_loss_dollars"] < 0
                missing_steps = [s for s in first_trade["steps"] if s["quote_missing"]]
                valid_steps = [s for s in first_trade["steps"] if not s["quote_missing"]]
                assert missing_steps and missing_steps[0]["mark_source"] == "MISSING"
                assert valid_steps and valid_steps[0]["mark_source"] in {"MID", "LAST", "CLOSE"}

                r_alt = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SYSTEMATIC",
                        "ticker": "NVDA",
                        "config": {
                            "strategy_kind": "BULL_PUT_SPREAD",
                            "entry_cadence": "WHEN_FLAT",
                            "price_basis": "LAST_CLOSE_ONLY",
                            "close_price_mode": "NATURAL_SPREAD",
                            "tp_sl_basis": "MAX_RISK_PCT",
                            "profit_target_pct": 25.0,
                            "stop_loss_pct": 50.0,
                        },
                    },
                )
                assert r_alt.status_code == 200
                alt_cfg = r_alt.json()["simulation"]["config"]
                assert alt_cfg["price_basis"] == "LAST_CLOSE_ONLY"
                assert alt_cfg["close_price_mode"] == "NATURAL_SPREAD"
                assert alt_cfg["tp_sl_basis"] == "MAX_RISK_PCT"

                r_single_auto = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SINGLE_TRADE",
                        "ticker": "NVDA",
                        "source": "AUTO_BUILD",
                        "entry_snapshot_id": "NVDA_20261105_190000",
                    },
                )
                assert r_single_auto.status_code == 200
                auto_body = r_single_auto.json()
                assert auto_body["mode"] == "SINGLE_TRADE"
                assert auto_body["source"] == "AUTO_BUILD"
                assert len(auto_body["simulation"]["trades"]) == 1
                assert len(auto_body["simulation"]["trades"][0]["steps"]) >= 3

                r_single_legs = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SINGLE_TRADE",
                        "ticker": "NVDA",
                        "source": "LEGS",
                        "entry_snapshot_id": "NVDA_20261105_190000",
                        "legs": [
                            {
                                "side": "SELL",
                                "right": "P",
                                "expiry": _EXPIRY,
                                "strike": 140.0,
                                "qty": 1,
                            },
                            {
                                "side": "BUY",
                                "right": "P",
                                "expiry": _EXPIRY,
                                "strike": 135.0,
                                "qty": 1,
                            },
                        ],
                    },
                )
                assert r_single_legs.status_code == 200
                assert r_single_legs.json()["source"] == "LEGS"

                r_val = await client.post(
                    "/api/backtest/run",
                    json={"mode": "SINGLE_TRADE", "ticker": "NVDA", "source": "AUTO_BUILD"},
                )
                assert r_val.status_code == 422
                assert r_val.json()["error_code"] == "BACKTEST_VALIDATION_ERROR"

                r_404 = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SINGLE_TRADE",
                        "ticker": "NVDA",
                        "source": "AUTO_BUILD",
                        "entry_snapshot_id": "NVDA_20990101_000000",
                    },
                )
                assert r_404.status_code == 404
                assert r_404.json()["error_code"] == "SNAPSHOT_NOT_FOUND"

    run_coro(_exercise())


def test_paper_trades_lifecycle_and_conflict_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_app(tmp_path, max_active_trades=1)
    asgi_app = _patch_inline_threads(monkeypatch, app)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                legs_bps = [
                    {"side": "SELL", "right": "P", "expiry": _EXPIRY, "strike": 140.0, "qty": 1},
                    {"side": "BUY", "right": "P", "expiry": _EXPIRY, "strike": 135.0, "qty": 1},
                ]
                r_create = await client.post(
                    "/api/backtest/paper-trades",
                    json={
                        "ticker": "NVDA",
                        "snapshot_id": "NVDA_20261105_190000",
                        "strategy_kind": "BULL_PUT_SPREAD",
                        "legs": legs_bps,
                        "qty": 1,
                    },
                )
                assert r_create.status_code == 201
                created = r_create.json()
                trade_id = created["id"]
                assert _PAPER_ID_RE.fullmatch(trade_id) is not None
                assert created["status"] == "ACTIVE"

                r_dup = await client.post(
                    "/api/backtest/paper-trades",
                    json={
                        "ticker": "NVDA",
                        "snapshot_id": "NVDA_20261105_190000",
                        "strategy_kind": "BULL_PUT_SPREAD",
                        "legs": legs_bps,
                        "qty": 1,
                    },
                )
                assert r_dup.status_code == 409
                assert r_dup.json()["error_code"] == "DUPLICATE_PAPER_TRADE"

                r_cap = await client.post(
                    "/api/backtest/paper-trades",
                    json={
                        "ticker": "NVDA",
                        "snapshot_id": "NVDA_20261105_190000",
                        "strategy_kind": "CUSTOM",
                        "legs": [
                            {
                                "side": "SELL",
                                "right": "C",
                                "expiry": _EXPIRY,
                                "strike": 145.0,
                                "qty": 1,
                            }
                        ],
                        "qty": 1,
                    },
                )
                assert r_cap.status_code == 409
                assert r_cap.json()["error_code"] == "PAPER_TRADE_CAP"

                r_list = await client.get(
                    "/api/backtest/paper-trades",
                    params={"status": "ACTIVE", "ticker": "NVDA"},
                )
                assert r_list.status_code == 200
                listed = r_list.json()
                assert listed["active_count"] == 1
                assert listed["max_active_trades"] == 1
                assert len(listed["trades"]) == 1

                r_replay = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SINGLE_TRADE",
                        "source": "PAPER_TRADE",
                        "paper_trade_id": trade_id,
                    },
                )
                assert r_replay.status_code == 200
                replay_body = r_replay.json()
                assert replay_body["paper_trade_id"] == trade_id
                assert replay_body["matching_paper_trade"]["id"] == trade_id

                r_stop = await client.post(f"/api/backtest/paper-trades/{trade_id}/stop")
                assert r_stop.status_code == 200
                assert r_stop.json()["status"] == "STOPPED"

                r_stop_again = await client.post(f"/api/backtest/paper-trades/{trade_id}/stop")
                assert r_stop_again.status_code == 409
                assert r_stop_again.json()["error_code"] == "PAPER_TRADE_STATE"

                r_missing = await client.post(
                    "/api/backtest/paper-trades/NVDA-20261112-deadbeef/stop"
                )
                assert r_missing.status_code == 404
                assert r_missing.json()["error_code"] == "SNAPSHOT_NOT_FOUND"

                r_bad_id = await client.post("/api/backtest/paper-trades/bad-id/stop")
                assert r_bad_id.status_code == 422

    run_coro(_exercise())


def test_refresh_busy_returns_409_with_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_app(tmp_path, seed_history=False)
    asgi_app = _patch_inline_threads(monkeypatch, app)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            svc = app.state.service
            acquired = await svc.acquire_refresh("collector:NVDA")
            assert acquired is True
            try:
                transport = httpx.ASGITransport(app=asgi_app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    res = await client.post("/api/refresh", json={"ticker": "NVDA"})
                    assert res.status_code == 409
                    assert res.headers["retry-after"] == "10"
                    body = res.json()
                    assert body["error_code"] == "REFRESH_BUSY"
                    assert body["retry_after_sec"] == 10
                    assert body["detail"] == body["message"]
            finally:
                svc.release_refresh()

    run_coro(_exercise())


def test_backtest_entry_filters_skipped_kpis_and_ui_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_app(tmp_path)
    asgi_app = _patch_inline_threads(monkeypatch, app)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r_page = await client.get("/")
                assert r_page.status_code == 200
                html = r_page.text
                assert 'id="backtest-card"' in html
                assert 'id="backtest-min-credit"' in html
                assert 'id="backtest-min-ror"' in html
                assert "data-bt-entry-filters" in html
                assert 'id="backtest-chart-toolbar"' in html
                assert 'id="backtest-chart-caption"' in html
                assert 'id="backtest-chart-view-group"' in html
                assert 'data-bt-chart-view="EQUITY"' in html
                assert 'data-bt-chart-view="TRADE"' in html
                assert "Trade &amp; Legs History" in html
                assert "Daily Mark-to-Market Step History" in html
                assert 'value="CLOSED"' in html
                assert 'id="backtest-paper-sync-ledger-btn"' in html
                assert "?v=20261114c" in html

                r_css = await client.get("/static/css/backtest.css")
                assert r_css.status_code == 200
                assert ".backtest-drawer" in r_css.text
                assert ".backtest-pill--missing" in r_css.text
                assert ".backtest-source-badge--ledger" in r_css.text
                assert ".backtest-status--CLOSED" in r_css.text

                r_ledger_css = await client.get("/static/css/ledger.css")
                assert r_ledger_css.status_code == 200
                assert ".ledger-replay-btn" in r_ledger_css.text

                r_js_charts = await client.get("/static/js/dashboard/backtest_charts.js")
                assert r_js_charts.status_code == 200
                assert "if (!dom.backtestChartEl.data)" in r_js_charts.text
                assert "window.Plotly.purge(dom.backtestChartEl)" in r_js_charts.text
                assert "&bull;" not in r_js_charts.text
                assert "&mdash;" not in r_js_charts.text

                r_js_tables = await client.get("/static/js/dashboard/backtest_tables.js")
                assert r_js_tables.status_code == 200
                assert "toggleBacktestTradeExpand" in r_js_tables.text
                assert "MISSING QUOTE" in r_js_tables.text
                assert "&bull;" not in r_js_tables.text
                assert "&mdash;" not in r_js_tables.text

                r_js_paper = await client.get("/static/js/dashboard/backtest_paper.js")
                assert r_js_paper.status_code == 200
                assert "syncPaperTradesFromLedger" in r_js_paper.text
                assert "formatLegsCompact" in r_js_paper.text
                assert "&bull;" not in r_js_paper.text
                assert "&mdash;" not in r_js_paper.text

                r_js_bt = await client.get("/static/js/dashboard/backtest.js")
                assert r_js_bt.status_code == 200
                assert "replayLedgerTradeInBacktester" in r_js_bt.text
                assert "syncPaperTradeQtyInput" in r_js_bt.text
                assert "clearBacktestDateAndEndFilters" in r_js_bt.text
                assert "replayPaperTradeFromList" in r_js_bt.text
                assert "&bull;" not in r_js_bt.text
                assert "&mdash;" not in r_js_bt.text

                r_js_ledger = await client.get("/static/js/dashboard/ledger.js")
                assert r_js_ledger.status_code == 200
                assert "triggerLedgerReplay" in r_js_ledger.text
                assert "ledger-replay-btn" in r_js_ledger.text
                assert "&bull;" not in r_js_ledger.text
                assert "&mdash;" not in r_js_ledger.text

                # min_entry_credit_dollars=150 allows $190/$163 spreads and skips lower-credit bars.
                r_credit_skip = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SYSTEMATIC",
                        "ticker": "NVDA",
                        "config": {
                            "strategy_kind": "BULL_PUT_SPREAD",
                            "entry_cadence": "EVERY_N_DAYS",
                            "entry_every_n_days": 1,
                            "max_open_trades": 5,
                            "min_entry_credit_dollars": 150.0,
                            "min_entry_ror_pct": None,
                        },
                    },
                )
                assert r_credit_skip.status_code == 200
                skip_sim = r_credit_skip.json()["simulation"]
                assert skip_sim["config"]["min_entry_credit_dollars"] == 150.0
                assert skip_sim["summary"]["trade_count"] == 2
                assert skip_sim["summary"]["skipped_entries_count"] == 3
                assert skip_sim["summary"]["skipped_by_reason"].get("BELOW_MIN_CREDIT") == 2

                # Null min_entry_credit_dollars disables $100 default; min_entry_ror_pct=28 keeps 1.
                r_ror = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SYSTEMATIC",
                        "ticker": "NVDA",
                        "config": {
                            "strategy_kind": "BULL_PUT_SPREAD",
                            "entry_cadence": "EVERY_N_DAYS",
                            "entry_every_n_days": 1,
                            "max_open_trades": 5,
                            "min_entry_credit_dollars": None,
                            "min_entry_ror_pct": 28.0,
                        },
                    },
                )
                assert r_ror.status_code == 200
                ror_sim = r_ror.json()["simulation"]
                assert ror_sim["config"]["min_entry_credit_dollars"] is None
                assert ror_sim["config"]["min_entry_ror_pct"] == 28.0
                assert ror_sim["summary"]["trade_count"] == 1
                assert ror_sim["summary"]["skipped_entries_count"] == 4
                assert ror_sim["summary"]["skipped_by_reason"].get("BELOW_MIN_ROR") == 3

                # Negative filter bounds fail validation with 422.
                for bad_cfg in (
                    {"min_entry_credit_dollars": -10.0},
                    {"min_entry_ror_pct": -1.0},
                    {"min_entry_ror_pct": 1001.0},
                ):
                    r_bad = await client.post(
                        "/api/backtest/run",
                        json={
                            "mode": "SYSTEMATIC",
                            "ticker": "NVDA",
                            "config": bad_cfg,
                        },
                    )
                    assert r_bad.status_code == 422

    run_coro(_exercise())


def _flex_nvda_open_spread_xml(*, include_open_positions: bool = True) -> bytes:
    """Flex XML with an open NVDA 140/135 Bull Put Spread entered on 2026-11-05."""
    open_pos_xml = (
        """
      <OpenPositions>
        <OpenPosition accountId="U1234567" assetCategory="OPT" levelOfDetail="SUMMARY"
                      symbol="NVDA  261218P00140000" underlyingSymbol="NVDA" conid="9003"
                      multiplier="100" strike="140" expiry="20261218" putCall="P"
                      position="-1" costBasisPrice="3.20" markPrice="1.20"
                      positionValue="-120.0" fifoPnlUnrealized="200.0" reportDate="20261112" />
        <OpenPosition accountId="U1234567" assetCategory="OPT" levelOfDetail="SUMMARY"
                      symbol="NVDA  261218P00135000" underlyingSymbol="NVDA" conid="9002"
                      multiplier="100" strike="135" expiry="20261218" putCall="P"
                      position="1" costBasisPrice="2.00" markPrice="0.80"
                      positionValue="80.0" fifoPnlUnrealized="-120.0" reportDate="20261112" />
      </OpenPositions>"""
        if include_open_positions
        else """
      <OpenPositions />"""
    )
    closing_trades_xml = (
        ""
        if include_open_positions
        else """
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="NVDA  261218P00140000" underlyingSymbol="NVDA" conid="9003"
               underlyingConid="4815747" multiplier="100" strike="140" expiry="20261218"
               putCall="P" dateTime="20261112;155900" quantity="1" tradePrice="1.20"
               proceeds="-120.0" ibCommission="-1.00" fifoPnlRealized="198.0"
               openCloseIndicator="C" buySell="BUY" ibOrderID="9201"
               ibExecID="nvda-bps-close-short" levelOfDetail="EXECUTION" />
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="NVDA  261218P00135000" underlyingSymbol="NVDA" conid="9002"
               underlyingConid="4815747" multiplier="100" strike="135" expiry="20261218"
               putCall="P" dateTime="20261112;155905" quantity="-1" tradePrice="0.80"
               proceeds="80.0" ibCommission="-1.00" fifoPnlRealized="-122.0"
               openCloseIndicator="C" buySell="SELL" ibOrderID="9202"
               ibExecID="nvda-bps-close-long" levelOfDetail="EXECUTION" />"""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Ledger" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567" toDate="20261112">
      <Trades>
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="NVDA  261218P00140000" underlyingSymbol="NVDA" conid="9003"
               underlyingConid="4815747" multiplier="100" strike="140" expiry="20261218"
               putCall="P" dateTime="20261105;143000" quantity="-1" tradePrice="3.20"
               proceeds="320.0" ibCommission="-1.00" fifoPnlRealized="0"
               openCloseIndicator="O" buySell="SELL" ibOrderID="9101"
               ibExecID="nvda-bps-open-short" levelOfDetail="EXECUTION" />
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="NVDA  261218P00135000" underlyingSymbol="NVDA" conid="9002"
               underlyingConid="4815747" multiplier="100" strike="135" expiry="20261218"
               putCall="P" dateTime="20261105;143000" quantity="1" tradePrice="2.00"
               proceeds="-200.0" ibCommission="-1.00" fifoPnlRealized="0"
               openCloseIndicator="O" buySell="BUY" ibOrderID="9101"
               ibExecID="nvda-bps-open-long" levelOfDetail="EXECUTION" />{closing_trades_xml}
      </Trades>{open_pos_xml}
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
""".encode()


def test_paper_trades_sync_ledger_and_replay_with_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_app(tmp_path, seed_history=True)
    asgi_app = _patch_inline_threads(monkeypatch, app)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r_imp = await client.post(
                    "/api/ledger/import",
                    files={
                        "file": (
                            "nvda_open.xml",
                            _flex_nvda_open_spread_xml(include_open_positions=True),
                            "application/xml",
                        )
                    },
                )
                assert r_imp.status_code == 200

                # Clear PaperTrade store to exercise explicit POST /paper-trades/sync-ledger
                app.state.service.paper_trades.store.save_trades([])

                r_sync1 = await client.post("/api/backtest/paper-trades/sync-ledger")
                assert r_sync1.status_code == 200
                sync1 = r_sync1.json()
                assert sync1["created_count"] == 1
                assert sync1["updated_count"] == 0
                assert sync1["closed_count"] == 0
                assert sync1["active_ledger_count"] == 1
                assert sync1["active_count"] == 1
                assert len(sync1["trades"]) == 1
                ledger_pt = sync1["trades"][0]
                assert ledger_pt["source"] == "LEDGER"
                assert ledger_pt["status"] == "ACTIVE"
                assert ledger_pt["ticker"] == "NVDA"
                assert ledger_pt["ledger_campaign_id"]
                assert ledger_pt["ledger_strategy_label"]
                assert ledger_pt["entry_net_cash"] == pytest.approx(120.0)
                assert len(ledger_pt["legs"]) == 2
                assert all(leg["entry_price"] is not None for leg in ledger_pt["legs"])

                # Idempotent re-sync preserves existing trade without creating duplicates
                r_sync2 = await client.post("/api/backtest/paper-trades/sync-ledger")
                assert r_sync2.status_code == 200
                sync2 = r_sync2.json()
                assert sync2["created_count"] == 0
                assert sync2["updated_count"] == 1
                assert sync2["closed_count"] == 0
                assert sync2["active_ledger_count"] == 1
                assert len(sync2["trades"]) == 1
                assert sync2["trades"][0]["id"] == ledger_pt["id"]

                # Replay synced Ledger trade in Single-Trade Time Replay against snapshots
                r_replay = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SINGLE_TRADE",
                        "ticker": "NVDA",
                        "source": "PAPER_TRADE",
                        "paper_trade_id": ledger_pt["id"],
                    },
                )
                assert r_replay.status_code == 200
                replay_body = r_replay.json()
                assert replay_body["mode"] == "SINGLE_TRADE"
                assert replay_body["source"] == "PAPER_TRADE"
                sim = replay_body["simulation"]
                assert len(sim["trades"]) == 1
                trade = sim["trades"][0]
                assert trade["strategy_kind"] == "BULL_PUT_SPREAD"
                assert trade["entry_cash_mid_dollars"] == pytest.approx(120.0)
                assert len(trade["steps"]) >= 3

                # Close positions in Ledger via closing Flex XML + empty positions;
                # verify POST /paper-trades/sync-ledger transitions PaperTrade to CLOSED
                app.state.service.ledger.repo.replace_positions([])
                r_imp_close = await client.post(
                    "/api/ledger/import",
                    files={
                        "file": (
                            "nvda_close.xml",
                            _flex_nvda_open_spread_xml(include_open_positions=False),
                            "application/xml",
                        )
                    },
                )
                assert r_imp_close.status_code == 200

                # Restore ACTIVE status to verify POST /paper-trades/sync-ledger closes it
                stored = app.state.service.paper_trades.store.load_trades()
                app.state.service.paper_trades.store.save_trades(
                    [t.model_copy(update={"status": "ACTIVE"}) for t in stored]
                )
                r_sync_close = await client.post("/api/backtest/paper-trades/sync-ledger")
                assert r_sync_close.status_code == 200
                sync_close = r_sync_close.json()
                assert sync_close["closed_count"] == 1
                assert sync_close["active_ledger_count"] == 0

                r_closed = await client.get(
                    "/api/backtest/paper-trades",
                    params={"status": "CLOSED"},
                )
                assert r_closed.status_code == 200
                closed_trades = r_closed.json()["trades"]
                assert len(closed_trades) == 1
                assert closed_trades[0]["id"] == ledger_pt["id"]
                assert closed_trades[0]["status"] == "CLOSED"

    run_coro(_exercise())


def test_paper_trades_ledger_replay_synthetic_mtm_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_app(tmp_path, seed_history=False)
    asgi_app = _patch_inline_threads(monkeypatch, app)

    msft_flex = b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Ledger" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567" toDate="20261112">
      <Trades>
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="MSFT  261218P00400000" underlyingSymbol="MSFT" conid="880001"
               underlyingConid="272093" multiplier="100" strike="400" expiry="20261218"
               putCall="P" dateTime="20261108;150000" quantity="-1" tradePrice="4.10"
               proceeds="410.0" ibCommission="-1.05" fifoPnlRealized="0"
               openCloseIndicator="O" buySell="SELL" ibOrderID="9301"
               ibExecID="msft-open-put" levelOfDetail="EXECUTION" />
      </Trades>
      <OpenPositions>
        <OpenPosition accountId="U1234567" assetCategory="OPT" levelOfDetail="SUMMARY"
                      symbol="MSFT  261218P00400000" underlyingSymbol="MSFT" conid="880001"
                      multiplier="100" strike="400" expiry="20261218" putCall="P"
                      position="-1" costBasisPrice="4.10" markPrice="2.10"
                      positionValue="-210.0" fifoPnlUnrealized="200.0" reportDate="20261112" />
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r_imp = await client.post(
                    "/api/ledger/import",
                    files={"file": ("msft_open.xml", msft_flex, "application/xml")},
                )
                assert r_imp.status_code == 200

                r_list = await client.get("/api/backtest/paper-trades")
                assert r_list.status_code == 200
                trades = r_list.json()["trades"]
                assert len(trades) == 1
                msft_pt = trades[0]
                assert msft_pt["source"] == "LEDGER"
                assert msft_pt["ticker"] == "MSFT"

                r_replay = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SINGLE_TRADE",
                        "ticker": "MSFT",
                        "source": "PAPER_TRADE",
                        "paper_trade_id": msft_pt["id"],
                    },
                )
                assert r_replay.status_code == 200
                replay_body = r_replay.json()
                assert replay_body["daily_bar_count"] == 1
                sim = replay_body["simulation"]
                assert len(sim["trades"]) == 1
                trade = sim["trades"][0]
                assert len(trade["steps"]) == 2
                assert trade["steps"][0]["snapshot_id"] == "MSFT_20261108_LEDGER"
                assert "LEDGER_MTM" in trade["steps"][-1]["snapshot_id"]
                assert trade["entry_cash_mid_dollars"] == pytest.approx(410.0)
                assert trade["gross_pnl_dollars"] == pytest.approx(200.0)

    run_coro(_exercise())


def test_paper_trades_ledger_multilot_qty_override_scales_entry_and_pnl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-lot (qty=2) Ledger replay with config.qty=1 scales entry cash & P&L cleanly."""
    app = _build_app(tmp_path, seed_history=True)
    asgi_app = _patch_inline_threads(monkeypatch, app)

    multilot_flex = b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Ledger" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567" toDate="20261112">
      <Trades>
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="PLTR  261218P00050000" underlyingSymbol="PLTR" conid="770001"
               underlyingConid="444857009" multiplier="100" strike="50" expiry="20261218"
               putCall="P" dateTime="20261108;150000" quantity="-2" tradePrice="2.50"
               proceeds="500.0" ibCommission="-2.10" fifoPnlRealized="0"
               openCloseIndicator="O" buySell="SELL" ibOrderID="9401"
               ibExecID="pltr-open-short-2" levelOfDetail="EXECUTION" />
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="NVDA  261218P00140000" underlyingSymbol="NVDA" conid="9003"
               underlyingConid="4815747" multiplier="100" strike="140" expiry="20261218"
               putCall="P" dateTime="20261105;143000" quantity="-2" tradePrice="2.50"
               proceeds="500.0" ibCommission="-2.10" fifoPnlRealized="0"
               openCloseIndicator="O" buySell="SELL" ibOrderID="9402"
               ibExecID="nvda-open-short-2" levelOfDetail="EXECUTION" />
      </Trades>
      <OpenPositions>
        <OpenPosition accountId="U1234567" assetCategory="OPT" levelOfDetail="SUMMARY"
                      symbol="PLTR  261218P00050000" underlyingSymbol="PLTR" conid="770001"
                      multiplier="100" strike="50" expiry="20261218" putCall="P"
                      position="-2" costBasisPrice="2.50" markPrice="1.80"
                      positionValue="-360.0" fifoPnlUnrealized="140.0" reportDate="20261112" />
        <OpenPosition accountId="U1234567" assetCategory="OPT" levelOfDetail="SUMMARY"
                      symbol="NVDA  261218P00140000" underlyingSymbol="NVDA" conid="9003"
                      multiplier="100" strike="140" expiry="20261218" putCall="P"
                      position="-2" costBasisPrice="2.50" markPrice="1.20"
                      positionValue="-240.0" fifoPnlUnrealized="260.0" reportDate="20261112" />
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r_imp = await client.post(
                    "/api/ledger/import",
                    files={"file": ("multilot_open.xml", multilot_flex, "application/xml")},
                )
                assert r_imp.status_code == 200

                r_sync = await client.post("/api/backtest/paper-trades/sync-ledger")
                assert r_sync.status_code == 200
                trades = r_sync.json()["trades"]
                pltr_pt = next(t for t in trades if t["ticker"] == "PLTR")
                nvda_pt = next(t for t in trades if t["ticker"] == "NVDA")
                assert pltr_pt["qty"] == 2
                assert pltr_pt["entry_net_cash"] == pytest.approx(500.0)
                assert nvda_pt["qty"] == 2
                assert nvda_pt["entry_net_cash"] == pytest.approx(500.0)

                # 1) Synthetic MTM replay with config.qty=1 -> $250 entry cash, $0 Day-0, +$70 MTM.
                r_pltr_one = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SINGLE_TRADE",
                        "source": "PAPER_TRADE",
                        "paper_trade_id": pltr_pt["id"],
                        "config": {"qty": 1},
                    },
                )
                assert r_pltr_one.status_code == 200
                pltr_one_trade = r_pltr_one.json()["simulation"]["trades"][0]
                assert pltr_one_trade["qty"] == 1
                assert pltr_one_trade["entry_cash_mid_dollars"] == pytest.approx(250.0)
                assert pltr_one_trade["steps"][0]["mark_source"] == "BROKER_FILL"
                assert pltr_one_trade["steps"][0]["gross_pnl_dollars"] == pytest.approx(0.0)
                assert pltr_one_trade["steps"][-1]["gross_pnl_dollars"] == pytest.approx(70.0)
                assert pltr_one_trade["gross_pnl_dollars"] == pytest.approx(70.0)

                # 2) Omitting config.qty defaults to full Ledger position (qty=2, +$140 MTM).
                r_pltr_full = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SINGLE_TRADE",
                        "source": "PAPER_TRADE",
                        "paper_trade_id": pltr_pt["id"],
                    },
                )
                assert r_pltr_full.status_code == 200
                pltr_full_trade = r_pltr_full.json()["simulation"]["trades"][0]
                assert pltr_full_trade["qty"] == 2
                assert pltr_full_trade["entry_cash_mid_dollars"] == pytest.approx(500.0)
                assert pltr_full_trade["steps"][0]["gross_pnl_dollars"] == pytest.approx(0.0)
                assert pltr_full_trade["gross_pnl_dollars"] == pytest.approx(140.0)

                # 3) Archived snapshot replay with config.qty=1 -> $250 entry cash, -$70 Day-0 snap.
                r_nvda_one = await client.post(
                    "/api/backtest/run",
                    json={
                        "mode": "SINGLE_TRADE",
                        "source": "PAPER_TRADE",
                        "paper_trade_id": nvda_pt["id"],
                        "config": {"qty": 1},
                    },
                )
                assert r_nvda_one.status_code == 200
                nvda_one_trade = r_nvda_one.json()["simulation"]["trades"][0]
                assert nvda_one_trade["qty"] == 1
                assert nvda_one_trade["entry_cash_mid_dollars"] == pytest.approx(250.0)
                assert nvda_one_trade["steps"][0]["mark_source"] == "BROKER_FILL"
                assert nvda_one_trade["steps"][0]["gross_pnl_dollars"] == pytest.approx(0.0)
                assert nvda_one_trade["steps"][1]["gross_pnl_dollars"] == pytest.approx(-70.0)
                assert nvda_one_trade["steps"][-1]["gross_pnl_dollars"] == pytest.approx(130.0)
                assert nvda_one_trade["gross_pnl_dollars"] == pytest.approx(130.0)

    run_coro(_exercise())
