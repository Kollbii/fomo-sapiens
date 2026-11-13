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
                assert sys_body["simulation"]["equity_curve"]

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
