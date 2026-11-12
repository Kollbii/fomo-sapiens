"""`CorridorQuoteService`: bounded IBKR subscriptions, one-shot pulls and SSE-ready streams.

Every `reqMktData` is paired with `cancelMktData` in `finally:` (stop, disconnect,
`aclose()`, Gateway drop, lifespan exit)."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
from ib_async import Option, Stock

from src.broker.client import IBKRBrokerClient, SleepFn, market_data_type_label, poll_until
from src.broker.contracts import extract_spot
from src.broker.corridor.models import (
    CORRIDOR_MAX_CONTRACTS,
    CorridorContractSpec,
    CorridorQuoteFrame,
    CorridorQuoteRow,
    CorridorSelection,
    FloatArray,
    GreeksFallback,
    corridor_quote_key,
)
from src.broker.corridor.quotes import compute_mid, compute_spread_pct
from src.broker.fetcher import (
    DELAYED_GENERIC_TICKS,
    EMPTY_QUOTE_FALLBACK_THRESHOLD,
    LIVE_OPRA_GENERIC_TICKS,
    empty_quote_ratio,
)
from src.broker.parser import extract_model_greeks, extract_open_interest, has_usable_quote
from src.core.config import Settings
from src.core.exceptions import (
    GatewayConnectionError,
    PacingViolationError,
)
from src.storage.schema import (
    sanitize_count,
    sanitize_price,
)

logger = logging.getLogger(__name__)


@dataclass
class _ActiveCorridorSession:
    session_id: str
    ib: Any
    subscribed_contracts: list[Any] = field(default_factory=list)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    cleaned_up: bool = False


async def _is_client_disconnected(
    callback: Callable[[], bool | Awaitable[bool]] | None,
) -> bool:
    if callback is None:
        return False
    res = callback()
    if inspect.isawaitable(res):
        return bool(await res)
    return bool(res)


def _contract_from_spec(spec: CorridorContractSpec) -> Option:
    """Build an IBKR `Option` reusing pre-qualified `conId` when available."""
    return Option(
        conId=spec.option_conid if spec.option_conid > 0 else 0,
        symbol=spec.symbol,
        lastTradeDateOrContractMonth=spec.ib_expiry,
        strike=spec.strike,
        right=spec.right,
        exchange="SMART",
        multiplier="100",
        currency="USD",
    )


def _build_frame_quotes(
    specs: Sequence[CorridorContractSpec],
    tickers: Sequence[Any],
    *,
    spot: float,
    updated_ts_utc: str,
    greeks_fallback: GreeksFallback | None,
) -> list[CorridorQuoteRow]:
    """Convert raw IBKR `Ticker` objects to `CorridorQuoteRow` items (Greeks fallback injected)."""
    interim: list[tuple[CorridorContractSpec, dict[str, Any]]] = []
    missing_indices: list[int] = []

    for idx, (spec, tk) in enumerate(zip(specs, tickers, strict=True)):
        bid = sanitize_price(getattr(tk, "bid", None))
        ask = sanitize_price(getattr(tk, "ask", None))
        last = sanitize_price(getattr(tk, "last", None))
        close = sanitize_price(getattr(tk, "close", None))
        if bid is None and ask is None and last is None and close is None:
            bid = spec.cached_bid
            ask = spec.cached_ask
            last = spec.cached_last
            close = spec.cached_close
        mid = compute_mid(bid, ask, last, close, spec.cached_mid)
        spread_pct = compute_spread_pct(bid, ask, mid)
        vol = sanitize_count(getattr(tk, "volume", None))
        if vol is None:
            vol = spec.cached_volume
        oi = extract_open_interest(tk, spec.right)
        if oi is None:
            oi = spec.cached_open_interest

        iv, delta, gamma, theta, vega, g_source = extract_model_greeks(tk)
        if g_source != "IBKR_MODEL":
            missing_indices.append(idx)

        interim.append(
            (
                spec,
                {
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "last": last,
                    "close": close,
                    "spread_pct": spread_pct,
                    "volume": vol,
                    "open_interest": oi,
                    "iv": iv,
                    "delta": delta,
                    "gamma": gamma,
                    "theta": theta,
                    "vega": vega,
                    "greeks_source": g_source,
                },
            )
        )

    # Rows without IBKR modelGreeks: injected fallback (vectorized BSM, <= 60 rows) when
    # available, otherwise / when unusable the cached snapshot Greeks are kept.
    usable = np.zeros(len(missing_indices), dtype=np.bool_)
    computed: tuple[FloatArray, FloatArray, FloatArray, FloatArray, FloatArray] | None = None
    if missing_indices and greeks_fallback is not None and spot > 0.0:
        miss_specs = [interim[i][0] for i in missing_indices]
        iv_arr, d_arr, g_arr, th_arr, v_arr, usable = greeks_fallback(
            mark_price=np.array(
                [float(interim[i][1]["mid"] or 0.0) for i in missing_indices], dtype=np.float64
            ),
            spot=np.full(len(missing_indices), spot, dtype=np.float64),
            strike=np.array([s.strike for s in miss_specs], dtype=np.float64),
            dte=np.array([float(s.dte) for s in miss_specs], dtype=np.float64),
            is_call=np.array([s.right == "C" for s in miss_specs], dtype=np.bool_),
            fallback_iv=np.array(
                [s.cached_iv if s.cached_iv is not None else np.nan for s in miss_specs],
                dtype=np.float64,
            ),
        )
        computed = (iv_arr, d_arr, g_arr, th_arr, v_arr)
    for pos, row_idx in enumerate(missing_indices):
        spec_i, data_i = interim[row_idx]
        if computed is not None and bool(usable[pos]):
            iv_arr, d_arr, g_arr, th_arr, v_arr = computed
            data_i["iv"] = round(float(iv_arr[pos]), 4)
            data_i["delta"] = round(float(d_arr[pos]), 4)
            data_i["gamma"] = round(float(g_arr[pos]), 5)
            data_i["theta"] = round(float(th_arr[pos]), 4)
            data_i["vega"] = round(float(v_arr[pos]), 4)
            data_i["greeks_source"] = "BSM_FALLBACK"
        else:
            data_i["iv"] = spec_i.cached_iv
            data_i["delta"] = spec_i.cached_delta
            data_i["gamma"] = spec_i.cached_gamma
            data_i["theta"] = spec_i.cached_theta
            data_i["vega"] = spec_i.cached_vega
            data_i["greeks_source"] = spec_i.cached_greeks_source

    rows: list[CorridorQuoteRow] = []
    for spec, d in interim:
        rows.append(
            CorridorQuoteRow(
                option_conid=spec.option_conid,
                expiry=spec.expiry,
                dte=spec.dte,
                strike=spec.strike,
                right=spec.right,
                zone=spec.zone,
                is_put_wall=spec.is_put_wall,
                is_call_wall=spec.is_call_wall,
                is_strategy_leg=spec.is_strategy_leg,
                bid=round(float(d["bid"]), 4) if d["bid"] is not None else None,
                ask=round(float(d["ask"]), 4) if d["ask"] is not None else None,
                mid=round(float(d["mid"]), 4) if d["mid"] is not None else None,
                last=round(float(d["last"]), 4) if d["last"] is not None else None,
                close=round(float(d["close"]), 4) if d["close"] is not None else None,
                spread_pct=d["spread_pct"],
                volume=d["volume"],
                open_interest=d["open_interest"],
                iv=d["iv"],
                delta=d["delta"],
                gamma=d["gamma"],
                theta=d["theta"],
                vega=d["vega"],
                greeks_source=d["greeks_source"],
                updated_ts_utc=updated_ts_utc,
            )
        )
    return rows


def _quote_signature(row: CorridorQuoteRow) -> tuple[Any, ...]:
    return (
        row.bid,
        row.ask,
        row.mid,
        row.last,
        row.volume,
        row.open_interest,
        row.iv,
        row.delta,
    )


class CorridorQuoteService:
    """Manages bounded `<= 60`-contract Wall Corridor snapshots and constant streaming."""

    def __init__(
        self,
        client: IBKRBrokerClient,
        settings: Settings | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: SleepFn = asyncio.sleep,
        greeks_fallback: GreeksFallback | None = None,
    ) -> None:
        self.client = client
        self.settings = settings or client.settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.greeks_fallback = greeks_fallback
        self._active_sessions: dict[str, _ActiveCorridorSession] = {}

    @property
    def active_stream_count(self) -> int:
        return len(self._active_sessions)

    def _generic_ticks(self) -> str:
        if self.client.mode == "LIVE" and not self.client.live_subscription_missing:
            return LIVE_OPRA_GENERIC_TICKS
        return DELAYED_GENERIC_TICKS

    def _cleanup_session(self, session: _ActiveCorridorSession) -> None:
        session.stop_event.set()
        if not session.cleaned_up:
            session.cleaned_up = True
            for contract in session.subscribed_contracts:
                try:
                    session.ib.cancelMktData(contract)
                except Exception:  # pragma: no cover - defensive cleanup
                    logger.exception("corridor cancelMktData failed for %s", contract)
        self._active_sessions.pop(session.session_id, None)

    async def stop_all_streams(self) -> int:
        """Signal and cancel 100% of active corridor market-data subscriptions."""
        sessions = list(self._active_sessions.values())
        count = 0
        for sess in sessions:
            count += len(sess.subscribed_contracts)
            self._cleanup_session(sess)
        return count

    async def _prepare_contracts(
        self,
        selection: CorridorSelection,
    ) -> tuple[list[Any], Any | None]:
        """Build option & optional spot contracts reusing cached `conId` when present."""
        if len(selection.specs) > CORRIDOR_MAX_CONTRACTS:
            raise PacingViolationError(
                f"Corridor selection {len(selection.specs)} exceeds {CORRIDOR_MAX_CONTRACTS} limit"
            )

        candidates = [_contract_from_spec(spec) for spec in selection.specs]
        unqualified = [c for c in candidates if int(getattr(c, "conId", 0) or 0) <= 0]
        if unqualified:
            res = list(await self.client.ib.qualifyContractsAsync(*unqualified) or [])
            if res and len(res) == len(unqualified):
                res_iter = iter(res)
                candidates = [
                    next(res_iter) if int(getattr(c, "conId", 0) or 0) <= 0 else c
                    for c in candidates
                ]

        spot_contract: Any | None = None
        if len(candidates) < CORRIDOR_MAX_CONTRACTS:
            if selection.underlying_conid > 0:
                spot_contract = Stock(
                    conId=selection.underlying_conid,
                    symbol=selection.ticker,
                    exchange="SMART",
                    currency="USD",
                )
            else:
                qual_spot = list(
                    await self.client.ib.qualifyContractsAsync(
                        Stock(selection.ticker, "SMART", "USD")
                    )
                    or []
                )
                if qual_spot:
                    spot_contract = qual_spot[0]

        total_subs = len(candidates) + (1 if spot_contract is not None else 0)
        if total_subs > CORRIDOR_MAX_CONTRACTS:
            raise PacingViolationError(
                f"Corridor total subscriptions {total_subs} exceed hard cap "
                f"{CORRIDOR_MAX_CONTRACTS}"
            )
        return candidates, spot_contract

    async def _subscribe_corridor(
        self,
        session: _ActiveCorridorSession,
        option_contracts: Sequence[Any],
        spot_contract: Any | None,
        *,
        generic_ticks: str,
    ) -> tuple[list[Any], Any | None]:
        ib = self.client.ib
        spot_ticker: Any | None = None
        if spot_contract is not None:
            spot_ticker = ib.reqMktData(
                spot_contract,
                genericTickList="",
                snapshot=False,
                regulatorySnapshot=False,
            )
            session.subscribed_contracts.append(spot_contract)

        option_tickers: list[Any] = []
        for contract in option_contracts:
            tk = ib.reqMktData(
                contract,
                genericTickList=generic_ticks,
                snapshot=False,
                regulatorySnapshot=False,
            )
            session.subscribed_contracts.append(contract)
            option_tickers.append(tk)

        return option_tickers, spot_ticker

    async def _poll_initial_corridor(
        self,
        session: _ActiveCorridorSession,
        option_contracts: Sequence[Any],
        spot_contract: Any | None,
        *,
        resubscribe_on_fallback: bool,
    ) -> tuple[_ActiveCorridorSession, list[Any], Any | None, list[str]]:
        """Subscribe corridor, wait for initial ticks, and apply dynamic Type fallback if needed."""
        oc = self.settings.options_chain
        corridor_cfg = self.settings.corridor
        notices: list[str] = []

        async def _resubscribe(suffix: str) -> tuple[list[Any], Any | None]:
            nonlocal session
            self._cleanup_session(session)
            session = _ActiveCorridorSession(
                session_id=f"{session.session_id}-{suffix}",
                ib=self.client.ib,
            )
            self._active_sessions[session.session_id] = session
            return await self._subscribe_corridor(
                session,
                option_contracts,
                spot_contract,
                generic_ticks=self._generic_ticks(),
            )

        try:
            option_tickers, spot_ticker = await self._subscribe_corridor(
                session,
                option_contracts,
                spot_contract,
                generic_ticks=self._generic_ticks(),
            )
            await poll_until(
                lambda: all(has_usable_quote(tk) for tk in option_tickers),
                corridor_cfg.oneshot_collect_timeout_sec,
                oc.page_poll_interval_sec,
                self.sleep,
            )

            if self.client.live_subscription_missing:
                sub_notice = self.client.consume_subscription_fallback_notice()
                if sub_notice:
                    notices.append(sub_notice)
                if resubscribe_on_fallback:
                    option_tickers, spot_ticker = await _resubscribe("dl")
                await poll_until(
                    lambda: all(has_usable_quote(tk) for tk in option_tickers),
                    corridor_cfg.oneshot_collect_timeout_sec,
                    oc.page_poll_interval_sec,
                    self.sleep,
                )

            if (
                self.client.can_fallback()
                and empty_quote_ratio(option_tickers) > EMPTY_QUOTE_FALLBACK_THRESHOLD
            ):
                prev_type = self.client.current_type
                new_type = self.client.switch_to_fallback_data_type()
                notices.append(
                    f"Corridor Type {prev_type} ({market_data_type_label(prev_type)}) quotes were "
                    f">80% empty; cascaded to Type {new_type} ({market_data_type_label(new_type)})."
                )
                if resubscribe_on_fallback:
                    option_tickers, spot_ticker = await _resubscribe("fb")
                    timeout_sec = corridor_cfg.oneshot_collect_timeout_sec
                else:
                    timeout_sec = min(1.5, corridor_cfg.oneshot_collect_timeout_sec)
                await poll_until(
                    lambda: all(has_usable_quote(tk) for tk in option_tickers),
                    timeout_sec,
                    oc.page_poll_interval_sec,
                    self.sleep,
                )

            self.client.observe_tickers_market_data_type(option_tickers)
            return session, option_tickers, spot_ticker, notices
        except BaseException:
            self._cleanup_session(session)
            raise

    async def fetch_corridor_quotes(
        self,
        selection: CorridorSelection,
        *,
        snapshot_id: str,
        on_frame: Callable[[CorridorQuoteFrame], None] | None = None,
    ) -> CorridorQuoteFrame:
        """One-shot targeted Wall Strike Corridor pull (`<= 60` contracts) with cleanup."""
        t0 = time.perf_counter()

        await self.client.ensure_connected()
        self.client.reset_market_data_type()
        option_contracts, spot_contract = await self._prepare_contracts(selection)

        session = _ActiveCorridorSession(
            session_id=f"oneshot-{uuid.uuid4().hex[:8]}",
            ib=self.client.ib,
        )
        self._active_sessions[session.session_id] = session

        try:
            session, option_tickers, spot_ticker, notices = await self._poll_initial_corridor(
                session,
                option_contracts,
                spot_contract,
                resubscribe_on_fallback=True,
            )

            live_spot = (
                extract_spot(spot_ticker) if spot_ticker is not None else None
            ) or selection.underlying_price
            ts_utc = self.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            quotes = _build_frame_quotes(
                selection.specs,
                option_tickers,
                spot=float(live_spot),
                updated_ts_utc=ts_utc,
                greeks_fallback=self.greeks_fallback,
            )
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            frame = CorridorQuoteFrame(
                ticker=selection.ticker,
                snapshot_id=snapshot_id,
                target_expiry=selection.target_expiry,
                target_dte=selection.target_dte,
                window_strikes=selection.window_strikes,
                max_contracts=selection.max_contracts,
                subscribed_count=len(session.subscribed_contracts),
                subscription_cap=CORRIDOR_MAX_CONTRACTS,
                underlying_price=round(float(live_spot), 4),
                market_data_type=self.client.get_active_market_data_type(),
                primary_call_wall=selection.primary_call_wall,
                primary_put_wall=selection.primary_put_wall,
                wall_basis=selection.wall_basis,
                updated_ts_utc=ts_utc,
                sequence=1,
                mode="ONE_SHOT",
                elapsed_ms=elapsed_ms,
                changed_keys=[corridor_quote_key(q.expiry, q.strike, q.right) for q in quotes],
                quotes=quotes,
                notices=notices,
            )
            if on_frame is not None:
                on_frame(frame)
            return frame
        finally:
            self._cleanup_session(session)

    async def stream_corridor_quotes(
        self,
        selection: CorridorSelection,
        *,
        snapshot_id: str,
        poll_interval_sec: float | None = None,
        is_disconnected: Callable[[], bool | Awaitable[bool]] | None = None,
        max_frames: int | None = None,
        on_frame: Callable[[CorridorQuoteFrame], None] | None = None,
    ) -> AsyncGenerator[CorridorQuoteFrame, None]:
        """Continuously stream throttled Wall Corridor quotes until stop/disconnect/error."""
        # Ensure zero overlapping live streams so IBKR line count stays <= 60
        await self.stop_all_streams()

        await self.client.ensure_connected()
        self.client.reset_market_data_type()
        option_contracts, spot_contract = await self._prepare_contracts(selection)

        corridor_cfg = self.settings.corridor
        eff_poll = (
            float(poll_interval_sec)
            if poll_interval_sec is not None
            else corridor_cfg.stream_poll_interval_sec
        )
        session = _ActiveCorridorSession(
            session_id=f"stream-{uuid.uuid4().hex[:8]}",
            ib=self.client.ib,
        )
        self._active_sessions[session.session_id] = session

        try:
            session, option_tickers, spot_ticker, notices = await self._poll_initial_corridor(
                session,
                option_contracts,
                spot_contract,
                resubscribe_on_fallback=False,
            )

            last_sigs: dict[str, tuple[Any, ...]] = {}
            last_spot: float | None = None
            seq = 0
            idle_elapsed = 0.0
            heartbeat_sec = corridor_cfg.heartbeat_interval_sec

            while not session.stop_event.is_set():
                if await _is_client_disconnected(is_disconnected):
                    break
                if not self.client.is_connected:
                    raise GatewayConnectionError(
                        "IBKR Gateway connection lost during live corridor stream"
                    )

                self.client.observe_tickers_market_data_type(option_tickers)
                raw_spot = extract_spot(spot_ticker) if spot_ticker is not None else None
                live_spot = float(raw_spot) if raw_spot is not None else selection.underlying_price
                ts_utc = self.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                quotes = _build_frame_quotes(
                    selection.specs,
                    option_tickers,
                    spot=live_spot,
                    updated_ts_utc=ts_utc,
                    greeks_fallback=self.greeks_fallback,
                )

                changed: list[str] = []
                for q in quotes:
                    qkey = corridor_quote_key(q.expiry, q.strike, q.right)
                    sig = _quote_signature(q)
                    if last_sigs.get(qkey) != sig:
                        changed.append(qkey)
                        last_sigs[qkey] = sig

                spot_changed = last_spot is None or abs(live_spot - last_spot) >= 0.005
                should_emit = (
                    seq == 0 or bool(changed) or spot_changed or idle_elapsed >= heartbeat_sec
                )

                if should_emit:
                    seq += 1
                    idle_elapsed = 0.0
                    last_spot = live_spot
                    frame = CorridorQuoteFrame(
                        ticker=selection.ticker,
                        snapshot_id=snapshot_id,
                        target_expiry=selection.target_expiry,
                        target_dte=selection.target_dte,
                        window_strikes=selection.window_strikes,
                        max_contracts=selection.max_contracts,
                        subscribed_count=len(session.subscribed_contracts),
                        subscription_cap=CORRIDOR_MAX_CONTRACTS,
                        underlying_price=round(live_spot, 4),
                        market_data_type=self.client.get_active_market_data_type(),
                        primary_call_wall=selection.primary_call_wall,
                        primary_put_wall=selection.primary_put_wall,
                        wall_basis=selection.wall_basis,
                        updated_ts_utc=ts_utc,
                        sequence=seq,
                        mode="STREAM",
                        changed_keys=changed,
                        quotes=quotes,
                        notices=notices if seq == 1 else [],
                    )
                    if on_frame is not None:
                        on_frame(frame)
                    yield frame
                    if max_frames is not None and seq >= max_frames:
                        break
                else:
                    idle_elapsed += eff_poll

                if session.stop_event.is_set() or await _is_client_disconnected(is_disconnected):
                    break
                await self.sleep(eff_poll)
        finally:
            self._cleanup_session(session)
