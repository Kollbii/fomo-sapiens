"""Shared live market-data line primitives (Wall Corridor stream + live screener leg stream).

- `MarketDataSession`: one registered set of streaming lines; `release()` cancels each line once.
- `BurstPacer` / `pace_and_guard` / `subscribe_lines`: `reqMktData` (and re-allocation
  `cancelMktData`) in bursts of `PAGE_PACING_CHUNK_SIZE` with a `PAGE_PACING_PAUSE_SEC` pause
  before every burst after the first; the stop flag and the Gateway socket are checked before and
  after each pause.
- `poll_initial_quotes`: initial tick wait with the `DELAYED 3->4` / `LIVE 1->2` cascade and the
  `10089` / `354` live-subscription downgrade notice.
- `build_quote_rows`: IBKR tickers -> `CorridorQuoteRow` (cached snapshot values when IBKR sent no
  price, injected `GreeksFallback` for rows without `modelGreeks`).
- `contract_from_spec`, `is_client_disconnected`.

Every line opened by `subscribe_lines` is recorded on its session right after its `reqMktData`,
before the next await, so the `finally:` `release()` of every stream cancels 100% of them.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from ib_async import Option

from src.broker.client import IBKRBrokerClient, SleepFn, market_data_type_label, poll_until
from src.broker.corridor.models import (
    CorridorContractSpec,
    CorridorQuoteRow,
    FloatArray,
    GreeksFallback,
)
from src.broker.corridor.quotes import compute_mid, compute_spread_pct
from src.broker.fetcher import (
    EMPTY_QUOTE_FALLBACK_THRESHOLD,
    PAGE_PACING_CHUNK_SIZE,
    PAGE_PACING_PAUSE_SEC,
    empty_quote_ratio,
)
from src.broker.parser import extract_model_greeks, extract_open_interest, has_usable_quote
from src.core.config import Settings
from src.core.exceptions import GatewayConnectionError
from src.storage.schema import sanitize_count, sanitize_price

logger = logging.getLogger(__name__)

#: Raises `GatewayConnectionError` when the Gateway reported an upstream loss (optional guard).
LinkGuard = Callable[[], None]


@dataclass(eq=False)
class MarketDataSession:
    """One registered set of streaming market-data lines (`subscribed_contracts`)."""

    session_id: str
    ib: Any
    subscribed_contracts: list[Any] = field(default_factory=list)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    cleaned_up: bool = False

    def release(self, *, label: str) -> int:
        """Signal stop and cancel every subscribed line exactly once (synchronous, never raises).

        Returns the number of `cancelMktData` calls made (0 when already released).
        """
        self.stop_event.set()
        if self.cleaned_up:
            return 0
        self.cleaned_up = True
        for contract in self.subscribed_contracts:
            try:
                self.ib.cancelMktData(contract)
            except Exception:  # pragma: no cover - defensive cleanup
                logger.exception("%s cancelMktData failed for %s", label, contract)
        return len(self.subscribed_contracts)

    def cancel_line(self, contract: Any, *, label: str) -> bool:
        """Cancel ONE subscribed line now (re-allocation) and stop tracking it.

        `False` when the line is not tracked or the session was already released (its
        `release()` cancelled it); a line is never cancelled twice.
        """
        if self.cleaned_up:
            return False
        for idx, item in enumerate(self.subscribed_contracts):
            if item is contract:
                del self.subscribed_contracts[idx]
                try:
                    self.ib.cancelMktData(contract)
                except Exception:  # pragma: no cover - defensive cleanup
                    logger.exception("%s cancelMktData failed for %s", label, contract)
                return True
        return False


@dataclass
class BurstPacer:
    """Counts market-data messages of one operation; pauses before each burst after the first."""

    sleep: SleepFn
    sent: int = 0

    def pause_due(self) -> bool:
        return self.sent > 0 and self.sent % PAGE_PACING_CHUNK_SIZE == 0


def _ensure_line_link(
    session: MarketDataSession, lost_message: str, link_guard: LinkGuard | None
) -> None:
    if not session.ib.isConnected():
        raise GatewayConnectionError(lost_message)
    if link_guard is not None:
        link_guard()


async def pace_and_guard(
    session: MarketDataSession,
    pacer: BurstPacer,
    *,
    lost_message: str,
    link_guard: LinkGuard | None = None,
) -> bool:
    """Gate the next market-data message: `False` once stopped, raise on a lost Gateway link.

    Sleeps `PAGE_PACING_PAUSE_SEC` when a burst of `PAGE_PACING_CHUNK_SIZE` messages completed
    and re-checks the stop flag and the link afterwards.
    """
    if session.stop_event.is_set():
        return False
    _ensure_line_link(session, lost_message, link_guard)
    if pacer.pause_due():
        await pacer.sleep(PAGE_PACING_PAUSE_SEC)
        if session.stop_event.is_set():
            return False
        _ensure_line_link(session, lost_message, link_guard)
    return True


async def subscribe_lines(
    session: MarketDataSession,
    requests: Iterable[tuple[Any, str]],
    *,
    pacer: BurstPacer,
    lost_message: str,
    link_guard: LinkGuard | None = None,
) -> list[Any]:
    """Paced streaming `reqMktData` for `(contract, genericTickList)` pairs, in order.

    Returns the tickers opened (shorter than `requests` when the session stopped part-way).
    """
    tickers: list[Any] = []
    for contract, generic_ticks in requests:
        if not await pace_and_guard(
            session, pacer, lost_message=lost_message, link_guard=link_guard
        ):
            break
        ticker = session.ib.reqMktData(
            contract,
            genericTickList=generic_ticks,
            snapshot=False,
            regulatorySnapshot=False,
        )
        session.subscribed_contracts.append(contract)
        tickers.append(ticker)
        pacer.sent += 1
    return tickers


async def poll_initial_quotes(
    client: IBKRBrokerClient,
    settings: Settings,
    *,
    sleep: SleepFn,
    option_tickers: Callable[[], Sequence[Any]],
    stopped: Callable[[], bool],
    label: str,
    resubscribe: Callable[[str], Awaitable[None]] | None = None,
) -> list[str]:
    """Wait for the first ticks, then apply the market-data cascade; returns its notices.

    `option_tickers` / `stopped` are re-read after every `resubscribe` (one-shot pulls re-open
    their lines on a type change; streams pass `None` and only poll again). Returns early, without
    observing the market-data type, as soon as `stopped()` is true.
    """
    oc = settings.options_chain
    timeout_sec = settings.corridor.oneshot_collect_timeout_sec
    notices: list[str] = []

    def _ready() -> bool:
        return stopped() or all(has_usable_quote(tk) for tk in option_tickers())

    await poll_until(_ready, timeout_sec, oc.page_poll_interval_sec, sleep)
    if stopped():
        return notices

    if client.live_subscription_missing:
        sub_notice = client.consume_subscription_fallback_notice()
        if sub_notice:
            notices.append(sub_notice)
        if resubscribe is not None:
            await resubscribe("dl")
            if stopped():
                return notices
        await poll_until(_ready, timeout_sec, oc.page_poll_interval_sec, sleep)
        if stopped():
            return notices

    if (
        client.can_fallback()
        and empty_quote_ratio(option_tickers()) > EMPTY_QUOTE_FALLBACK_THRESHOLD
    ):
        prev_type = client.current_type
        new_type = client.switch_to_fallback_data_type()
        notices.append(
            f"{label} Type {prev_type} ({market_data_type_label(prev_type)}) quotes were "
            f">80% empty; cascaded to Type {new_type} ({market_data_type_label(new_type)})."
        )
        if resubscribe is not None:
            await resubscribe("fb")
            if stopped():
                return notices
            fallback_timeout = timeout_sec
        else:
            fallback_timeout = min(1.5, timeout_sec)
        await poll_until(_ready, fallback_timeout, oc.page_poll_interval_sec, sleep)
        if stopped():
            return notices

    client.observe_tickers_market_data_type(option_tickers())
    return notices


async def is_client_disconnected(
    callback: Callable[[], bool | Awaitable[bool]] | None,
) -> bool:
    if callback is None:
        return False
    res = callback()
    if inspect.isawaitable(res):
        return bool(await res)
    return bool(res)


def contract_from_spec(spec: CorridorContractSpec) -> Option:
    """Build an IBKR `Option` reusing the pre-qualified snapshot `conId` when available."""
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


def build_quote_rows(
    specs: Sequence[CorridorContractSpec],
    tickers: Sequence[Any],
    *,
    spots: Sequence[float],
    updated_ts_utc: str,
    greeks_fallback: GreeksFallback | None,
) -> tuple[list[CorridorQuoteRow], list[bool]]:
    """Convert raw IBKR `Ticker`s to `CorridorQuoteRow`s plus a per-line "IBKR priced" flag.

    `spots[i]` is the underlying price for line `i` (the Greeks fallback runs only where it is
    `> 0`). A line without any IBKR bid / ask / last / close keeps its cached snapshot quote and
    gets `False` in the flag list. Rows without IBKR `modelGreeks` get the injected fallback
    (vectorized BSM, <= 90 rows) when usable, else their cached snapshot Greeks.
    """
    if len(spots) != len(specs):
        raise ValueError(f"spots has {len(spots)} entries for {len(specs)} quote lines")
    interim: list[tuple[CorridorContractSpec, dict[str, Any]]] = []
    priced: list[bool] = []
    missing_indices: list[int] = []

    for idx, (spec, tk) in enumerate(zip(specs, tickers, strict=True)):
        bid = sanitize_price(getattr(tk, "bid", None))
        ask = sanitize_price(getattr(tk, "ask", None))
        last = sanitize_price(getattr(tk, "last", None))
        close = sanitize_price(getattr(tk, "close", None))
        has_price = not (bid is None and ask is None and last is None and close is None)
        if not has_price:
            bid = spec.cached_bid
            ask = spec.cached_ask
            last = spec.cached_last
            close = spec.cached_close
        priced.append(has_price)
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

    # Rows without IBKR modelGreeks: injected fallback where the spot is known, otherwise / when
    # unusable the cached snapshot Greeks are kept.
    eligible = [i for i in missing_indices if spots[i] > 0.0]
    usable = np.zeros(len(eligible), dtype=np.bool_)
    computed: tuple[FloatArray, FloatArray, FloatArray, FloatArray, FloatArray] | None = None
    if eligible and greeks_fallback is not None:
        el_specs = [interim[i][0] for i in eligible]
        iv_arr, d_arr, g_arr, th_arr, v_arr, usable = greeks_fallback(
            mark_price=np.array(
                [float(interim[i][1]["mid"] or 0.0) for i in eligible], dtype=np.float64
            ),
            spot=np.array([float(spots[i]) for i in eligible], dtype=np.float64),
            strike=np.array([s.strike for s in el_specs], dtype=np.float64),
            dte=np.array([float(s.dte) for s in el_specs], dtype=np.float64),
            is_call=np.array([s.right == "C" for s in el_specs], dtype=np.bool_),
            fallback_iv=np.array(
                [s.cached_iv if s.cached_iv is not None else np.nan for s in el_specs],
                dtype=np.float64,
            ),
        )
        computed = (iv_arr, d_arr, g_arr, th_arr, v_arr)
    position = {row_idx: pos for pos, row_idx in enumerate(eligible)}
    for row_idx in missing_indices:
        spec_i, data_i = interim[row_idx]
        pos = position.get(row_idx)
        if computed is not None and pos is not None and bool(usable[pos]):
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
    return rows, priced
