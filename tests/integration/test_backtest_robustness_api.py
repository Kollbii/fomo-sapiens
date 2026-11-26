"""Integration tests for `/api/backtest/robustness` route, validation, and UI assets."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
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
from src.storage.schema import OptionContractRow
from src.web import api as web_api
from src.web import backtest_api
from tests.support.aio import noop_sleep, run_coro
from tests.support.ib_fakes import FakeEvent


class _ForbidIBKR:
    """Offline IB stub that fails immediately if any IBKR network call is attempted."""

    def __init__(self) -> None:
        self.errorEvent = FakeEvent()
        self.wrapper = SimpleNamespace(clientId=0)
        self.client = SimpleNamespace(connectAsync=self._connect_async)

    async def _connect_async(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("IBKR must never be called by /api/backtest/robustness")

    def isConnected(self) -> bool:  # noqa: N802
        return False

    def disconnect(self) -> None:
        return None


def _patch_inline_threads(monkeypatch: pytest.MonkeyPatch, app: Any) -> Any:
    async def _run_sync_inline(func: Callable[..., Any], *args: Any, **_kwargs: Any) -> Any:
        return func(*args)

    async def _in_thread_inline(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    monkeypatch.setattr(anyio.to_thread, "run_sync", _run_sync_inline)
    monkeypatch.setattr(backtest_api, "_in_thread", _in_thread_inline)
    monkeypatch.setattr(web_api, "_in_thread", _in_thread_inline)
    monkeypatch.setattr(
        anyio.CancelScope,
        "__new__",
        lambda _cls, **_kw: contextlib.nullcontext(),
    )

    async def _asgi_24(scope: Any, receive: Any, send: Any) -> None:
        patched_scope = {**scope, "asgi": {"version": "3.0", "spec_version": "2.4"}}
        await app(patched_scope, receive, send)

    return _asgi_24


def _build_chain_rows(
    *,
    snapshot_id: str,
    ts_utc: str,
    market_date: str,
    expiry: str,
    spot: float,
    short_put_mid: float,
    long_put_mid: float,
) -> list[dict[str, Any]]:
    dte = (date.fromisoformat(expiry) - date.fromisoformat(market_date)).days
    specs = [
        (9001, 130.0, "P", round(long_put_mid * 0.65, 2)),
        (9002, 135.0, "P", round(long_put_mid, 2)),
        (9003, 140.0, "P", round(short_put_mid, 2)),
        (9004, 150.0, "C", 2.80),
        (9005, 155.0, "C", 1.50),
    ]
    rows: list[dict[str, Any]] = []
    for conid, strike, right, mid_val in specs:
        bid = round(max(0.05, mid_val - 0.10), 2)
        ask = round(mid_val + 0.10, 2)
        rows.append(
            OptionContractRow(
                snapshot_id=snapshot_id,
                snapshot_ts_utc=ts_utc,
                market_date=market_date,
                market_data_type=3,
                underlying="NVDA",
                underlying_conid=4815747,
                underlying_price=spot,
                option_conid=conid,
                expiry=expiry,
                dte=dte,
                strike=strike,
                moneyness_pct=(strike - spot) / spot,
                right=right,  # type: ignore[arg-type]
                bid=bid,
                ask=ask,
                mid=mid_val,
                last=mid_val,
                close=mid_val,
                mark_price=mid_val,
                volume=120,
                open_interest=500,
                iv=0.40,
                delta=-0.25 if right == "P" else 0.25,
                gamma=0.02,
                theta=-0.04,
                vega=0.11,
                greeks_source="IBKR_MODEL",
            ).model_dump()
        )
    return rows


def _build_app(tmp_path: Path, *, num_days: int = 12) -> Any:
    settings = Settings(
        tracker=PaperTrackerSettings(
            max_active_trades=20,
            tracked_dir="data/tracked",
        )
    )
    repo = CSVRepository(settings.storage, root=tmp_path)
    repo.ensure_layout()
    broker = IBKRBrokerClient(settings, ib_factory=_ForbidIBKR)
    fixed_now = datetime(2026, 11, 20, 19, 0, tzinfo=UTC)
    fetcher = PagedOptionChainFetcher(
        broker,
        settings,
        clock=lambda: fixed_now,
        sleep=noop_sleep,
    )

    cur = date(2026, 11, 2)
    expiry = "2026-12-18"
    saved = 0
    while saved < num_days:
        if cur.weekday() < 5:
            mkt = cur.isoformat()
            snap_id = f"NVDA_{cur.strftime('%Y%m%d')}_190000"
            ts_utc = f"{mkt}T19:00:00Z"
            decay = max(0.35, 1.0 - saved * 0.045)
            repo.save_snapshot(
                "NVDA",
                _build_chain_rows(
                    snapshot_id=snap_id,
                    ts_utc=ts_utc,
                    market_date=mkt,
                    expiry=expiry,
                    spot=145.0 + saved * 0.4,
                    short_put_mid=3.40 * decay,
                    long_put_mid=2.00 * decay,
                ),
                spot_source="MID",
            )
            saved += 1
        cur += timedelta(days=1)

    return create_app(
        settings=settings,
        repo=repo,
        broker_client=broker,
        fetcher=fetcher,
    )


def test_backtest_robustness_full_selective_and_short_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_app(tmp_path, num_days=12)
    asgi_app = _patch_inline_threads(monkeypatch, app)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                # 1. Full 4-section audit over < 60 bars returns LIMITED tier cleanly (no 500s)
                r_full = await client.post(
                    "/api/backtest/robustness",
                    json={
                        "ticker": "nvda",
                        "config": {
                            "strategy_kind": "BULL_PUT_SPREAD",
                            "profit_target_pct": 50.0,
                            "stop_loss_pct": 100.0,
                        },
                        "robustness_config": {
                            "monte_carlo": {"num_paths": 200, "seed": 7},
                        },
                    },
                )
                assert r_full.status_code == 200
                body = r_full.json()
                assert body["daily_bar_count"] == 12
                assert body["history_snapshot_count"] == 12
                assert body["cache_hit"] is False
                res = body["result"]
                assert res["ticker"] == "NVDA"
                assert res["data_sufficiency_tier"] == "LIMITED"
                assert res["total_bars"] == 12
                assert res["cohorts"] is not None
                assert res["sensitivity"] is not None
                assert res["walk_forward"] is not None
                assert res["monte_carlo"] is not None
                assert len(res["verdicts"]) == 4
                assert {v["section"] for v in res["verdicts"]} == {
                    "COHORTS",
                    "SENSITIVITY",
                    "WALK_FORWARD",
                    "MONTE_CARLO",
                }
                assert res["walk_forward"]["folds"] == []
                assert res["walk_forward"]["sample_depth_warning"] is True

                # 2. Repeat selective single-section audit hits cached daily bars
                r_sel = await client.post(
                    "/api/backtest/robustness",
                    json={
                        "ticker": "NVDA",
                        "sections": ["SENSITIVITY"],
                        "config": {
                            "strategy_kind": "BULL_PUT_SPREAD",
                            "profit_target_pct": 50.0,
                            "stop_loss_pct": 100.0,
                        },
                        "robustness_config": {
                            "sensitivity": {
                                "x_param": "profit_target_pct",
                                "y_param": "exit_dte",
                                "grid_density": 5,
                                "objective": "expectancy_dollars",
                            }
                        },
                    },
                )
                assert r_sel.status_code == 200
                sel_body = r_sel.json()
                assert sel_body["cache_hit"] is True
                sel_res = sel_body["result"]
                assert sel_res["requested_sections"] == ["SENSITIVITY"]
                assert sel_res["sensitivity"] is not None
                assert sel_res["cohorts"] is None
                assert sel_res["walk_forward"] is None
                assert sel_res["monte_carlo"] is None
                verdicts_by_sec = {v["section"]: v["status"] for v in sel_res["verdicts"]}
                assert verdicts_by_sec["COHORTS"] == "SKIPPED"
                assert verdicts_by_sec["WALK_FORWARD"] == "SKIPPED"
                assert verdicts_by_sec["MONTE_CARLO"] == "SKIPPED"

    run_coro(_exercise())


def test_backtest_robustness_validation_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_app(tmp_path, num_days=5)
    asgi_app = _patch_inline_threads(monkeypatch, app)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r_paths = await client.post(
                    "/api/backtest/robustness",
                    json={
                        "ticker": "NVDA",
                        "robustness_config": {"monte_carlo": {"num_paths": 5001}},
                    },
                )
                assert r_paths.status_code == 422

                r_axes = await client.post(
                    "/api/backtest/robustness",
                    json={
                        "ticker": "NVDA",
                        "robustness_config": {
                            "sensitivity": {
                                "x_param": "profit_target_pct",
                                "y_param": "profit_target_pct",
                            }
                        },
                    },
                )
                assert r_axes.status_code == 422

                r_custom = await client.post(
                    "/api/backtest/robustness",
                    json={
                        "ticker": "NVDA",
                        "config": {"strategy_kind": "CUSTOM"},
                    },
                )
                assert r_custom.status_code == 422
                assert r_custom.json()["error_code"] == "BACKTEST_VALIDATION_ERROR"

                r_ticker = await client.post(
                    "/api/backtest/robustness",
                    json={"ticker": "BAD!TICKER"},
                )
                assert r_ticker.status_code == 422

    run_coro(_exercise())


def test_backtest_robustness_ui_markup_and_assets_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _build_app(tmp_path, num_days=3)
    asgi_app = _patch_inline_threads(monkeypatch, app)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r_html = await client.get("/")
                assert r_html.status_code == 200
                html = r_html.text
                assert 'data-bt-mode="ROBUSTNESS"' in html
                assert 'id="backtest-cadence-legacy-hint"' in html
                assert 'id="backtest-robustness-panel"' in html
                for elem_id in (
                    "bt-rob-depth-badge",
                    "bt-rob-timing",
                    "bt-rob-run-all-btn",
                    "bt-rob-verdicts",
                    "bt-rob-tabs",
                    "bt-rob-pane-cohorts",
                    "bt-rob-pane-sensitivity",
                    "bt-rob-pane-walkforward",
                    "bt-rob-pane-montecarlo",
                    "bt-rob-sens-x",
                    "bt-rob-sens-y",
                    "bt-rob-apply-cell-btn",
                    "bt-rob-wfo-mode",
                    "bt-rob-mc-fan-chart",
                    "bt-rob-mc-dd-chart",
                ):
                    assert f'id="{elem_id}"' in html

                r_rob_js = await client.get("/static/js/dashboard/backtest_robustness.js")
                assert r_rob_js.status_code == 200
                assert "/api/backtest/robustness" in r_rob_js.text
                assert "&bull;" not in r_rob_js.text
                assert "&mdash;" not in r_rob_js.text

                r_css = await client.get("/static/css/backtest.css")
                assert r_css.status_code == 200
                assert ".bt-rob-verdict--pass" in r_css.text
                assert ".bt-rob-mc-charts" in r_css.text

    run_coro(_exercise())
