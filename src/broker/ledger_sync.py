"""Opt-in read-only IBKR Gateway execution/position sync and bounded mark-price refresh."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from ib_async import Contract, ExecutionFilter, Option, Stock

from src.broker.client import IBKRBrokerClient, SleepFn, poll_until
from src.broker.corridor.quotes import compute_mid
from src.core.exceptions import ConfigurationError, PacingViolationError
from src.storage.ledger_schema import (
    LedgerExecutionRow,
    LedgerPositionRow,
    LedgerSide,
    compute_signed_proceeds,
    normalize_expiry_yyyymmdd,
    normalize_right_optional,
    normalize_underlying,
    normalize_utc_timestamp,
    sanitize_optional_conid,
    sanitize_signed_float,
    synthesize_exec_id,
)
from src.storage.schema import sanitize_price

logger = logging.getLogger(__name__)

MAX_MARK_CONTRACTS = 60
MARK_PACING_CHUNK_SIZE = 20
MARK_PACING_YIELD_SEC = 0.01
EMPTY_MARK_FALLBACK_THRESHOLD = 0.80
_ALLOWED_SEC_TYPES: frozenset[str] = frozenset({"OPT", "STK"})


def _normalize_gateway_side(raw: Any, quantity: float) -> LedgerSide:
    text = str(raw or "").strip().upper()
    if text in {"BOT", "BUY"}:
        return "BOT"
    if text in {"SLD", "SELL"}:
        return "SLD"
    return "BOT" if quantity >= 0.0 else "SLD"


def _fill_to_execution_row(fill: Any, *, include_stock: bool) -> LedgerExecutionRow | None:
    """Convert an `ib_async.Fill` to `LedgerExecutionRow`, discarding non-OPT/STK & accounts."""
    contract = getattr(fill, "contract", None)
    execution = getattr(fill, "execution", None)
    if contract is None or execution is None:
        return None

    sec_type = str(getattr(contract, "secType", "") or "").strip().upper()
    if sec_type not in _ALLOWED_SEC_TYPES:
        return None
    asset_class: Literal["OPT", "STK"] = "OPT" if sec_type == "OPT" else "STK"
    if asset_class == "STK" and not include_stock:
        return None

    raw_shares = sanitize_signed_float(getattr(execution, "shares", None))
    if raw_shares is None or raw_shares == 0.0:
        return None
    quantity = abs(raw_shares)
    side = _normalize_gateway_side(getattr(execution, "side", None), raw_shares)

    raw_price = sanitize_signed_float(getattr(execution, "price", None))
    price = max(0.0, raw_price) if raw_price is not None else 0.0

    raw_symbol = str(
        getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or ""
    ).strip()
    raw_und = str(getattr(contract, "symbol", "") or raw_symbol).strip()
    if not raw_und:
        return None
    underlying = normalize_underlying(raw_und)

    if sec_type == "OPT":
        expiry = normalize_expiry_yyyymmdd(getattr(contract, "lastTradeDateOrContractMonth", None))
        strike = sanitize_signed_float(getattr(contract, "strike", None))
        right = normalize_right_optional(getattr(contract, "right", None))
        if expiry is None or strike is None or strike <= 0.0 or right is None:
            return None
        mult_val = sanitize_signed_float(getattr(contract, "multiplier", None))
        multiplier = int(mult_val) if mult_val and mult_val > 0 else 100
        symbol = raw_symbol or f"{underlying} {expiry} {strike:g} {right}"
    else:
        expiry = None
        strike = None
        right = None
        multiplier = 1
        symbol = raw_symbol or underlying

    proceeds = compute_signed_proceeds(side, quantity, price, multiplier)

    comm_report = getattr(fill, "commissionReport", None)
    raw_comm = sanitize_signed_float(getattr(comm_report, "commission", None))
    commission = abs(raw_comm) if raw_comm is not None else 0.0
    realized = sanitize_signed_float(getattr(comm_report, "realizedPNL", None))
    currency = (
        str(getattr(comm_report, "currency", None) or getattr(contract, "currency", None) or "USD")
        .strip()
        .upper()
        or "USD"
    )

    raw_time = getattr(fill, "time", None) or getattr(execution, "time", None)
    if raw_time is None:
        return None
    executed_at = normalize_utc_timestamp(raw_time)

    order_id_num = int(getattr(execution, "orderId", 0) or 0)
    perm_id_num = int(getattr(execution, "permId", 0) or 0)
    ib_order_id = (
        str(order_id_num) if order_id_num > 0 else (str(perm_id_num) if perm_id_num > 0 else None)
    )

    exec_id = str(getattr(execution, "execId", "") or "").strip()
    if not exec_id:
        exec_id = synthesize_exec_id(
            event_code="TRADE",
            executed_at=executed_at,
            underlying=underlying,
            asset_class=sec_type,
            symbol=symbol,
            expiry=expiry,
            strike=strike,
            right=right,
            side=side,
            quantity=quantity,
            price=price,
            proceeds=proceeds,
            ib_order_id=ib_order_id,
        )

    return LedgerExecutionRow(
        exec_id=exec_id,
        ib_order_id=ib_order_id,
        conid=sanitize_optional_conid(getattr(contract, "conId", None)),
        underlying_conid=None,
        symbol=symbol,
        underlying=underlying,
        asset_class=asset_class,
        expiry=expiry,
        strike=strike,
        right=right,
        multiplier=multiplier,
        side=side,
        quantity=quantity,
        price=price,
        proceeds=proceeds,
        commission=commission,
        realized_pnl_ib=realized,
        open_close=None,
        event_code="TRADE",
        currency=currency,
        fx_rate_to_base=1.0,
        executed_at=executed_at,
        source="GATEWAY_LIVE",
    )


def _position_to_row(pos: Any, *, include_stock: bool, updated_at: str) -> LedgerPositionRow | None:
    """Convert an `ib_async.Position` to `LedgerPositionRow`, dropping `account` completely."""
    contract = getattr(pos, "contract", None)
    if contract is None:
        return None
    sec_type = str(getattr(contract, "secType", "") or "").strip().upper()
    if sec_type not in _ALLOWED_SEC_TYPES:
        return None
    asset_class: Literal["OPT", "STK"] = "OPT" if sec_type == "OPT" else "STK"
    if asset_class == "STK" and not include_stock:
        return None

    qty = sanitize_signed_float(getattr(pos, "position", None))
    if qty is None or qty == 0.0:
        return None

    raw_symbol = str(
        getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or ""
    ).strip()
    raw_und = str(getattr(contract, "symbol", "") or raw_symbol).strip()
    if not raw_und:
        return None
    underlying = normalize_underlying(raw_und)

    if sec_type == "OPT":
        expiry = normalize_expiry_yyyymmdd(getattr(contract, "lastTradeDateOrContractMonth", None))
        strike = sanitize_signed_float(getattr(contract, "strike", None))
        right = normalize_right_optional(getattr(contract, "right", None))
        if expiry is None or strike is None or strike <= 0.0 or right is None:
            return None
        mult_val = sanitize_signed_float(getattr(contract, "multiplier", None))
        multiplier = int(mult_val) if mult_val and mult_val > 0 else 100
        symbol = raw_symbol or f"{underlying} {expiry} {strike:g} {right}"
    else:
        expiry = None
        strike = None
        right = None
        multiplier = 1
        symbol = raw_symbol or underlying

    avg_cost = sanitize_signed_float(getattr(pos, "avgCost", None)) or 0.0
    return LedgerPositionRow(
        conid=sanitize_optional_conid(getattr(contract, "conId", None)),
        symbol=symbol,
        underlying=underlying,
        asset_class=asset_class,
        expiry=expiry,
        strike=strike,
        right=right,
        multiplier=multiplier,
        position=qty,
        avg_cost=avg_cost,
        mark_price=None,
        market_value=None,
        unrealized_pnl=None,
        source="GATEWAY_LIVE",
        updated_at=updated_at,
    )


async def fetch_gateway_ledger(
    broker: IBKRBrokerClient,
    *,
    include_stock: bool = True,
    clock: Callable[[], datetime] | None = None,
) -> tuple[list[LedgerExecutionRow], list[LedgerPositionRow]]:
    """Opt-in read-only sync of today's OPT/STK executions and open positions via IBKR Gateway."""
    if not broker.settings.ledger.gateway_sync_enabled:
        raise ConfigurationError(
            "Gateway ledger sync is disabled; set ENABLE_GATEWAY_LEDGER_SYNC=true to opt in."
        )

    await broker.ensure_connected()

    opt_fills = list(
        await broker.run_stage(
            "reqExecutions(OPT)",
            broker.ib.reqExecutionsAsync(ExecutionFilter(secType="OPT")),
        )
        or []
    )
    stk_fills: list[Any] = []
    if include_stock:
        stk_fills = list(
            await broker.run_stage(
                "reqExecutions(STK)",
                broker.ib.reqExecutionsAsync(ExecutionFilter(secType="STK")),
            )
            or []
        )
    raw_positions = list(
        await broker.run_stage("reqPositions", broker.ib.reqPositionsAsync()) or []
    )

    executions_by_id: dict[str, LedgerExecutionRow] = {}
    for fill in [*opt_fills, *stk_fills]:
        row = _fill_to_execution_row(fill, include_stock=include_stock)
        if row is not None:
            executions_by_id[row.exec_id] = row

    now_iso = normalize_utc_timestamp((clock or (lambda: datetime.now(UTC)))())
    positions: list[LedgerPositionRow] = []
    for pos in raw_positions:
        prow = _position_to_row(pos, include_stock=include_stock, updated_at=now_iso)
        if prow is not None:
            positions.append(prow)

    return list(executions_by_id.values()), positions


def _contract_for_mark(pos: LedgerPositionRow) -> Contract | None:
    if pos.conid is None or pos.conid <= 0:
        return None
    if pos.asset_class == "OPT":
        if pos.expiry is None or pos.strike is None or pos.strike <= 0.0 or pos.right is None:
            return None
        return Option(
            conId=pos.conid,
            symbol=pos.underlying,
            lastTradeDateOrContractMonth=pos.expiry,
            strike=pos.strike,
            right=pos.right,
            exchange="SMART",
            multiplier=str(pos.multiplier or 100),
            currency="USD",
        )
    return Stock(
        conId=pos.conid,
        symbol=pos.underlying,
        exchange="SMART",
        currency="USD",
    )


def _extract_ticker_mark(ticker: Any) -> float | None:
    bid = sanitize_price(getattr(ticker, "bid", None)) or sanitize_price(
        getattr(ticker, "delayedBid", None)
    )
    ask = sanitize_price(getattr(ticker, "ask", None)) or sanitize_price(
        getattr(ticker, "delayedAsk", None)
    )
    last = sanitize_price(getattr(ticker, "last", None)) or sanitize_price(
        getattr(ticker, "delayedLast", None)
    )
    close = sanitize_price(getattr(ticker, "close", None)) or sanitize_price(
        getattr(ticker, "delayedClose", None)
    )
    return compute_mid(bid, ask, last, close)


async def refresh_open_option_marks(
    broker: IBKRBrokerClient,
    positions: Sequence[LedgerPositionRow],
    *,
    max_contracts: int = MAX_MARK_CONTRACTS,
    collect_timeout_sec: float = 2.0,
    poll_interval_sec: float = 0.25,
    sleep: SleepFn = asyncio.sleep,
) -> dict[int, float]:
    """Read-only bounded mark-price refresh (`<= 60` contracts, `cancelMktData` in `finally:`)."""
    if max_contracts < 1 or max_contracts > MAX_MARK_CONTRACTS:
        raise PacingViolationError(
            f"max_contracts must be between 1 and {MAX_MARK_CONTRACTS}, got {max_contracts}"
        )

    contract_pairs: list[tuple[int, Contract]] = []
    seen_conids: set[int] = set()
    for pos in positions:
        if pos.conid is None or pos.conid <= 0 or pos.conid in seen_conids:
            continue
        contract = _contract_for_mark(pos)
        if contract is None:
            continue
        seen_conids.add(pos.conid)
        contract_pairs.append((pos.conid, contract))
        if len(contract_pairs) >= max_contracts:
            break

    if not contract_pairs:
        return {}

    await broker.ensure_connected()
    broker.reset_market_data_type()

    async def _collect_once() -> list[tuple[int, Any]]:
        ib = broker.ib
        subscribed: list[Contract] = []
        tickers: list[tuple[int, Any]] = []
        stage = f"ledger mark refresh ({len(contract_pairs)} contracts)"
        try:
            for idx, (conid, contract) in enumerate(contract_pairs):
                tk = ib.reqMktData(contract, "", False, False)
                subscribed.append(contract)
                tickers.append((conid, tk))
                if (idx + 1) % MARK_PACING_CHUNK_SIZE == 0 and idx + 1 < len(contract_pairs):
                    await sleep(MARK_PACING_YIELD_SEC)

            await poll_until(
                lambda: all(_extract_ticker_mark(tk) is not None for _, tk in tickers),
                collect_timeout_sec,
                poll_interval_sec,
                sleep,
            )
            broker.ensure_still_connected(stage=stage)
            return tickers
        except Exception as exc:
            broker.handle_stage_error(exc, stage=stage)
            raise
        finally:
            pending_cancel_exc: BaseException | None = None
            for idx, contract in enumerate(subscribed):
                try:
                    ib.cancelMktData(contract)
                except Exception:  # pragma: no cover - defensive cleanup
                    logger.exception("cancelMktData failed for conId=%s", contract.conId)
                if (
                    pending_cancel_exc is None
                    and (idx + 1) % MARK_PACING_CHUNK_SIZE == 0
                    and idx + 1 < len(subscribed)
                ):
                    try:
                        await sleep(MARK_PACING_YIELD_SEC)
                    except BaseException as cancel_exc:
                        pending_cancel_exc = cancel_exc
            if pending_cancel_exc is not None:
                raise pending_cancel_exc

    tickers = await _collect_once()
    if broker.live_subscription_missing:
        broker.consume_subscription_fallback_notice()
        tickers = await _collect_once()
    if broker.can_fallback():
        empty_count = sum(1 for _, tk in tickers if _extract_ticker_mark(tk) is None)
        if empty_count / len(tickers) > EMPTY_MARK_FALLBACK_THRESHOLD:
            broker.switch_to_fallback_data_type()
            tickers = await _collect_once()

    broker.observe_tickers_market_data_type([tk for _, tk in tickers])

    marks: dict[int, float] = {}
    for conid, tk in tickers:
        mark = _extract_ticker_mark(tk)
        if mark is not None:
            marks[conid] = round(mark, 4)
    return marks
