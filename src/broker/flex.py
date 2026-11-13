"""IBKR Flex Web Service v3 client and XML/CSV trade & position parsers (read-only)."""

from __future__ import annotations

import asyncio
import csv
import http.client
import io
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, Literal

from src.broker.client import SleepFn
from src.core.config import LedgerSettings
from src.core.exceptions import (
    ConfigurationError,
    StorageSchemaError,
)
from src.core.exceptions import (
    FlexServiceError as FlexServiceError,
)
from src.storage.ledger_schema import (
    LedgerAssetClass,
    LedgerEventCode,
    LedgerExecutionRow,
    LedgerOpenClose,
    LedgerPositionRow,
    LedgerSide,
    LedgerSource,
    compute_signed_proceeds,
    normalize_expiry_yyyymmdd,
    normalize_right_optional,
    normalize_underlying,
    normalize_utc_timestamp,
    sanitize_optional_conid,
    sanitize_signed_float,
    synthesize_exec_id,
)

logger = logging.getLogger(__name__)

FLEX_SEND_REQUEST_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/SendRequest"
)
FLEX_GET_STATEMENT_URL = (
    "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement"
)
FLEX_ALLOWED_HOST_PREFIX = "https://ndcdyn.interactivebrokers.com/"
FLEX_API_VERSION = "3"
FLEX_RETRY_ERROR_CODES: frozenset[str] = frozenset({"1018", "1019"})

HttpFetchFn = Callable[[str], Awaitable[bytes]]
FlexQuerySource = Literal["FLEX_ACTIVITY_365", "FLEX_MONTHLY", "FLEX_TRADE_CONF"]

_MONTHS: dict[str, int] = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}

_OCC_RE = re.compile(r"^([A-Z0-9 .-]+?)\s+(\d{6})([CP])(\d{8})$")
_ACT_OPT_RE = re.compile(
    r"^([A-Z0-9 .-]+?)\s+(\d{1,2})([A-Z]{3})(\d{2,4})\s+(\d+(?:\.\d+)?)\s+([CP])$"
)
_ISO_OPT_RE = re.compile(
    r"^([A-Z0-9 .-]+?)\s+(\d{8}|\d{4}-\d{2}-\d{2})\s+(\d+(?:\.\d+)?)\s+([CP])$"
)
_FX_PAIR_RE = re.compile(r"^[A-Z]{3}\.[A-Z]{3}$", re.IGNORECASE)
_LIFECYCLE_EVENTS: frozenset[str] = frozenset({"EXPIRED", "ASSIGNED", "EXERCISED"})

_TOKEN_QUERY_RE = re.compile(r"([?&]t=)[^&\s\"'>]+", re.IGNORECASE)
_ACCT_TAG_RE = re.compile(
    r"(<(?:acctId|accountId|acctCode)>\s*)[^<]+(\s*</(?:acctId|accountId|acctCode)>)",
    re.IGNORECASE,
)
_ACCT_QUOTED_RE = re.compile(
    r"\b(acctId|accountId|acctCode|account|ClientAccountID)(\s*[:=]\s*)([\"'])[^\"']*\3",
    re.IGNORECASE,
)
_ACCT_ATTR_RE = re.compile(
    r"\b(acctId|accountId|acctCode|account|ClientAccountID)(\s*[:=]\s*)([^\s,;>\"']+)",
    re.IGNORECASE,
)

_SUMMARY_DETAIL_LEVELS: frozenset[str] = frozenset(
    {
        "SYMBOL_SUMMARY",
        "ASSET_SUMMARY",
        "BASE_SUMMARY",
        "CLOSED_LOT",
        "LOT",
        "TAX_LOT",
        "SUMMARY",
        "SUBTOTAL",
        "TOTAL",
    }
)


def redact_flex_secrets(text: str, token: str | None = None) -> str:
    """Redact Flex tokens (`t=...`) and account IDs from logs and error messages."""
    redacted = _TOKEN_QUERY_RE.sub(r"\1[REDACTED]", text)
    redacted = _ACCT_TAG_RE.sub(r"\1[REDACTED]\2", redacted)
    redacted = _ACCT_QUOTED_RE.sub(r"\1\2\3[REDACTED]\3", redacted)
    redacted = _ACCT_ATTR_RE.sub(r"\1\2[REDACTED]", redacted)
    if token:
        cleaned = token.strip()
        if cleaned:
            redacted = redacted.replace(cleaned, "[REDACTED]")
    return redacted


async def _default_http_fetch(url: str, *, timeout_sec: float = 20.0) -> bytes:
    """Non-blocking stdlib HTTPS GET executed via `asyncio.to_thread`."""
    if not url.startswith("https://"):
        raise FlexServiceError("Flex Web Service URL must use HTTPS")

    def _sync_get() -> bytes:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "IBKR-Options-Analyzer/1.0",
                "Accept": "application/xml,text/csv",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310
            data: bytes = resp.read()
            return data

    return await asyncio.to_thread(_sync_get)


def select_flex_query_ids(
    settings: LedgerSettings,
    *,
    mode: Literal["auto", "monthly", "full"],
    has_existing_history: bool,
) -> list[tuple[str, FlexQuerySource]]:
    """Select Flex Query `(query_id, source)` sequence for `auto`, `monthly`, or `full` sync."""
    selected: list[tuple[str, FlexQuerySource]] = []

    if mode == "full":
        if not settings.flex_activity_query_id:
            raise ConfigurationError(
                "IBKR_FLEX_ACTIVITY_QUERY_ID is required for 'full' (365-day) Flex sync."
            )
        selected.append((settings.flex_activity_query_id, "FLEX_ACTIVITY_365"))
    elif mode == "monthly":
        if not settings.flex_monthly_query_id:
            raise ConfigurationError(
                "IBKR_FLEX_MONTHLY_QUERY_ID is not configured; configure "
                "IBKR_FLEX_MONTHLY_QUERY_ID for incremental monthly sync or use 'full' (365d) mode."
            )
        selected.append((settings.flex_monthly_query_id, "FLEX_MONTHLY"))
    elif mode == "auto":
        if has_existing_history and settings.flex_monthly_query_id:
            selected.append((settings.flex_monthly_query_id, "FLEX_MONTHLY"))
        elif settings.flex_activity_query_id:
            selected.append((settings.flex_activity_query_id, "FLEX_ACTIVITY_365"))
        elif not settings.flex_trade_conf_query_id:
            raise ConfigurationError(
                "No Flex Query ID configured; set IBKR_FLEX_ACTIVITY_QUERY_ID (365d) "
                "or IBKR_FLEX_MONTHLY_QUERY_ID."
            )
    else:
        raise ConfigurationError(f"Unsupported Flex sync mode: {mode!r}")

    if settings.flex_trade_conf_query_id:
        pair: tuple[str, FlexQuerySource] = (settings.flex_trade_conf_query_id, "FLEX_TRADE_CONF")
        if pair not in selected:
            selected.append(pair)
    return selected


def _parse_statement_response(raw_bytes: bytes, token: str | None = None) -> ET.Element | None:
    stripped = raw_bytes.lstrip()
    if not stripped.startswith(b"<"):
        return None
    try:
        root = ET.fromstring(stripped)
    except ET.ParseError as exc:
        raise FlexServiceError(
            redact_flex_secrets(f"Invalid XML from IBKR Flex Web Service: {exc}", token)
        ) from exc
    return root if root.tag == "FlexStatementResponse" else None


def _tag_text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


class FlexWebServiceClient:
    """Async read-only client for IBKR Flex Web Service v3 (`SendRequest` -> `GetStatement`)."""

    def __init__(
        self,
        *,
        token: str | None = None,
        http_fetch: HttpFetchFn | None = None,
        timeout_sec: float = 20.0,
        poll_interval_sec: float = 1.5,
        max_poll_attempts: int = 8,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._token = token.strip() if isinstance(token, str) and token.strip() else None
        self._timeout_sec = timeout_sec
        self._http_fetch: HttpFetchFn = http_fetch or (
            lambda url: _default_http_fetch(url, timeout_sec=self._timeout_sec)
        )
        self.poll_interval_sec = poll_interval_sec
        self.max_poll_attempts = max(1, max_poll_attempts)
        self.sleep = sleep

    async def _get(self, url: str, token: str) -> bytes:
        try:
            return await self._http_fetch(url)
        except FlexServiceError:
            raise
        except (
            http.client.HTTPException,
            urllib.error.URLError,
            OSError,
            TimeoutError,
            ValueError,
        ) as exc:
            raise FlexServiceError(
                redact_flex_secrets(f"IBKR Flex HTTP request failed: {exc}", token)
            ) from exc

    async def fetch_statement(self, query_id: str, *, token: str | None = None) -> bytes:
        """Execute `SendRequest` then poll `GetStatement` until ready (`1018`/`1019` retries)."""
        resolved_token = (token or self._token or "").strip()
        if not resolved_token:
            raise ConfigurationError("IBKR_FLEX_TOKEN is not configured")
        qid = query_id.strip()
        if not qid:
            raise ConfigurationError("Flex Query ID must not be empty")

        send_url = (
            f"{FLEX_SEND_REQUEST_URL}?"
            f"{urllib.parse.urlencode({'t': resolved_token, 'q': qid, 'v': FLEX_API_VERSION})}"
        )

        ref_code = ""
        get_base_url = FLEX_GET_STATEMENT_URL
        for attempt in range(self.max_poll_attempts):
            raw_send = await self._get(send_url, resolved_token)
            resp = _parse_statement_response(raw_send, resolved_token)
            if resp is None:
                raise FlexServiceError("Unexpected non-FlexStatementResponse from SendRequest")

            status = _tag_text(resp, "Status").lower()
            if status == "success":
                ref_code = _tag_text(resp, "ReferenceCode")
                candidate_url = _tag_text(resp, "Url")
                if candidate_url.startswith(FLEX_ALLOWED_HOST_PREFIX):
                    get_base_url = candidate_url
                if not ref_code:
                    raise FlexServiceError("Flex SendRequest succeeded without ReferenceCode")
                break

            err_code = _tag_text(resp, "ErrorCode")
            err_msg = redact_flex_secrets(_tag_text(resp, "ErrorMessage"), resolved_token)
            if err_code in FLEX_RETRY_ERROR_CODES and attempt + 1 < self.max_poll_attempts:
                logger.info(
                    "IBKR Flex SendRequest code %s for query=%s; retrying (%d/%d)",
                    err_code,
                    qid,
                    attempt + 1,
                    self.max_poll_attempts,
                )
                await self.sleep(self.poll_interval_sec)
                continue
            raise FlexServiceError(
                f"IBKR Flex SendRequest failed (code {err_code or 'unknown'}): {err_msg}"
            )

        get_url = (
            f"{get_base_url}?"
            f"{urllib.parse.urlencode({'q': ref_code, 't': resolved_token, 'v': FLEX_API_VERSION})}"
        )
        for attempt in range(self.max_poll_attempts):
            raw_stmt = await self._get(get_url, resolved_token)
            status_resp = _parse_statement_response(raw_stmt, resolved_token)
            if status_resp is None:
                return raw_stmt

            err_code = _tag_text(status_resp, "ErrorCode")
            err_msg = redact_flex_secrets(_tag_text(status_resp, "ErrorMessage"), resolved_token)
            if err_code in FLEX_RETRY_ERROR_CODES and attempt + 1 < self.max_poll_attempts:
                logger.info(
                    "IBKR Flex GetStatement code %s (in progress/rate-limit); polling (%d/%d)",
                    err_code,
                    attempt + 1,
                    self.max_poll_attempts,
                )
                await self.sleep(self.poll_interval_sec)
                continue
            raise FlexServiceError(
                f"IBKR Flex GetStatement failed (code {err_code or 'unknown'}): {err_msg}"
            )

        raise FlexServiceError("IBKR Flex GetStatement timed out waiting for statement generation")


# --------------------------------------------------------------------------- symbol / field helpers
def parse_option_symbol(symbol: str) -> tuple[str, str, float, Literal["C", "P"]] | None:
    """Parse OCC (`NVDA  261218C00140000`) or Activity (`NVDA 18DEC26 140 C`) option symbols."""
    text = symbol.strip().upper()
    if not text:
        return None

    m_occ = _OCC_RE.match(text)
    if m_occ:
        und_raw, yymmdd, right, strike_raw = m_occ.groups()
        expiry = f"20{yymmdd}"
        try:
            datetime.strptime(expiry, "%Y%m%d")
        except ValueError:
            return None
        right_norm: Literal["C", "P"] = "C" if right == "C" else "P"
        return (normalize_underlying(und_raw), expiry, int(strike_raw) / 1000.0, right_norm)

    m_act = _ACT_OPT_RE.match(text)
    if m_act:
        und_raw, day_s, mon_s, yr_s, strike_s, right = m_act.groups()
        month = _MONTHS.get(mon_s)
        if month is None:
            return None
        year = int(yr_s) + 2000 if len(yr_s) == 2 else int(yr_s)
        try:
            expiry = date(year, month, int(day_s)).strftime("%Y%m%d")
        except ValueError:
            return None
        right_norm = "C" if right == "C" else "P"
        return (normalize_underlying(und_raw), expiry, float(strike_s), right_norm)

    m_iso = _ISO_OPT_RE.match(text)
    if m_iso:
        und_raw, exp_raw, strike_s, right = m_iso.groups()
        expiry_opt = normalize_expiry_yyyymmdd(exp_raw)
        if expiry_opt is None:
            return None
        right_norm = "C" if right == "C" else "P"
        return (normalize_underlying(und_raw), expiry_opt, float(strike_s), right_norm)

    return None


def _normalize_asset_class(raw: Any) -> LedgerAssetClass | None:
    text = str(raw or "").strip().upper()
    if not text:
        return None
    if text in {"OPT", "OPTION", "OPTIONS", "EQUITY AND INDEX OPTIONS"}:
        return "OPT"
    if text in {"STK", "STOCK", "STOCKS", "EQUITY", "EQUITIES"}:
        return "STK"
    return None


def _has_option_attrs(attrs: Mapping[str, str]) -> bool:
    if (
        _extract_attr(attrs, "putCall", "right", "expiry", "lastTradeDateOrContractMonth")
        is not None
    ):
        return True
    strike_val = sanitize_signed_float(_extract_attr(attrs, "strike"))
    return strike_val is not None and strike_val > 0.0


def _resolve_asset_class(attrs: Mapping[str, str], symbol: str) -> LedgerAssetClass | None:
    """Resolve explicit assetCategory, or infer OPT/STK while skipping FX pairs and AFX rows."""
    raw_asset = _extract_attr(attrs, "assetCategory", "assetClass", "Asset Category")
    if raw_asset is not None:
        return _normalize_asset_class(raw_asset)

    if _has_option_attrs(attrs) or parse_option_symbol(symbol) is not None:
        return "OPT"

    upper_sym = symbol.strip().upper()
    underlying_raw = _extract_attr(attrs, "underlyingSymbol")
    und_upper = (underlying_raw or "").strip().upper()

    if _FX_PAIR_RE.match(upper_sym):
        base_ccy, quote_ccy = upper_sym.split(".", 1)
        row_ccy = (_extract_attr(attrs, "currency", "currencyPrimary") or "").strip().upper()
        if und_upper in {"", "FX", base_ccy, quote_ccy} or (row_ccy and und_upper == row_ccy):
            return None

    raw_codes = _extract_attr(attrs, "code", "Code", "notes", "Notes", "Notes/Codes")
    if raw_codes:
        tokens = {tok.strip() for tok in re.split(r"[;,/\s]+", raw_codes.upper()) if tok.strip()}
        if "AFX" in tokens and und_upper in {"", "FX"}:
            return None

    return "STK"


def _normalize_side(raw_side: Any, raw_qty: float | None) -> LedgerSide | None:
    text = str(raw_side or "").strip().upper()
    if text in {"BOT", "BUY", "B"} or text.startswith("BUY "):
        return "BOT"
    if text in {"SLD", "SELL", "S"} or text.startswith("SELL "):
        return "SLD"
    if raw_qty is not None and raw_qty != 0.0:
        return "BOT" if raw_qty > 0.0 else "SLD"
    return None


def _normalize_event_code(raw: Any) -> LedgerEventCode:
    text = str(raw or "").strip().upper()
    if not text or text == "TRADE":
        return "TRADE"
    if "EXPIR" in text or text == "EP":
        return "EXPIRED"
    if "ASSIGN" in text or text == "A":
        return "ASSIGNED"
    if "EXERCIS" in text or text == "EX":
        return "EXERCISED"
    return "TRADE"


def _parse_open_close_and_codes(
    raw_oc: Any, raw_codes: Any = None
) -> tuple[LedgerOpenClose | None, LedgerEventCode]:
    """Parse Flex `openCloseIndicator` and Activity `Code` (`O`, `C`, `Ep`, `A`, `Ex`)."""
    event_code: LedgerEventCode = "TRADE"
    open_close: LedgerOpenClose | None = None

    for source_val in (raw_oc, raw_codes):
        text = str(source_val or "").strip().upper()
        if not text:
            continue
        tokens = [tok.strip() for tok in re.split(r"[;,/\s]+", text) if tok.strip()]
        if "EP" in tokens or any("EXPIR" in t for t in tokens):
            event_code = "EXPIRED"
        elif "A" in tokens or any("ASSIGN" in t for t in tokens):
            event_code = "ASSIGNED"
        elif "EX" in tokens or any("EXERCIS" in t for t in tokens):
            event_code = "EXERCISED"

        has_open = "O" in tokens or "OPEN" in tokens
        has_close = "C" in tokens or "CLOSE" in tokens
        if has_open and not has_close:
            open_close = "O"
        elif has_close and not has_open:
            open_close = "C"

    if event_code in {"EXPIRED", "ASSIGNED", "EXERCISED"} and open_close is None:
        open_close = "C"
    return open_close, event_code


def _extract_attr(attrs: Mapping[str, str], *names: str) -> str | None:
    """Case-tolerant lookup of XML/CSV fields (ignoring account-ID keys)."""
    lower_map: dict[str, str] | None = None
    for name in names:
        if name in attrs:
            val = attrs[name].strip()
            if val:
                return val
    lower_map = {k.strip().lower(): v.strip() for k, v in attrs.items()}
    for name in names:
        val = lower_map.get(name.lower(), "")
        if val:
            return val
    return None


def _build_execution_from_mapping(
    attrs: Mapping[str, str],
    *,
    default_source: LedgerSource,
    forced_event: LedgerEventCode | None = None,
    fallback_ts: str | None = None,
    seen_synthetic: Counter[str],
) -> LedgerExecutionRow | None:
    symbol = _extract_attr(attrs, "symbol", "localSymbol") or ""
    if not symbol:
        return None
    upper_sym = symbol.upper()
    if upper_sym.startswith(("TOTAL", "SUBTOTAL")):
        return None

    asset_class = _resolve_asset_class(attrs, symbol)
    if asset_class is None:
        return None

    parsed_opt = parse_option_symbol(symbol) if asset_class == "OPT" else None
    underlying_raw = _extract_attr(attrs, "underlyingSymbol")
    if not underlying_raw:
        underlying_raw = parsed_opt[0] if parsed_opt is not None else symbol.split()[0]
    underlying = normalize_underlying(underlying_raw)

    expiry = normalize_expiry_yyyymmdd(
        _extract_attr(attrs, "expiry", "lastTradeDateOrContractMonth")
    )
    strike = sanitize_signed_float(_extract_attr(attrs, "strike"))
    right = normalize_right_optional(_extract_attr(attrs, "putCall", "right"))

    if asset_class == "OPT" and parsed_opt is not None:
        if expiry is None:
            expiry = parsed_opt[1]
        if strike is None or strike <= 0.0:
            strike = parsed_opt[2]
        if right is None:
            right = parsed_opt[3]
    elif asset_class == "STK":
        expiry = None
        strike = None
        right = None

    mult_val = sanitize_signed_float(_extract_attr(attrs, "multiplier", "mult"))
    default_mult = 1 if asset_class == "STK" else 100
    multiplier = int(mult_val) if mult_val and mult_val > 0 else default_mult

    raw_qty = sanitize_signed_float(_extract_attr(attrs, "quantity"))
    if raw_qty is None or raw_qty == 0.0:
        return None
    quantity = abs(raw_qty)

    side = _normalize_side(_extract_attr(attrs, "buySell", "side"), raw_qty)
    if side is None:
        return None

    raw_price = sanitize_signed_float(_extract_attr(attrs, "tradePrice", "T. Price", "price"))
    price = max(0.0, raw_price) if raw_price is not None else 0.0

    proceeds = sanitize_signed_float(_extract_attr(attrs, "proceeds"))
    if proceeds is None:
        proceeds = compute_signed_proceeds(side, quantity, price, multiplier)

    commission_raw = sanitize_signed_float(
        _extract_attr(attrs, "ibCommission", "commission", "commisions", "Comm/Fee")
    )
    commission = abs(commission_raw) if commission_raw is not None else 0.0

    realized = sanitize_signed_float(
        _extract_attr(attrs, "fifoPnlRealized", "realizedPnl", "realizedPNL", "Realized P/L")
    )
    open_close, code_event = _parse_open_close_and_codes(
        _extract_attr(attrs, "openCloseIndicator", "openClose", "Open/CloseIndicator"),
        _extract_attr(attrs, "code", "Code", "notes", "Notes", "Notes/Codes", "transactionType"),
    )
    raw_tx_type = _extract_attr(attrs, "transactionType")
    tx_event = _normalize_event_code(raw_tx_type) if raw_tx_type else "TRADE"
    event_code = forced_event or (tx_event if tx_event != "TRADE" else code_event)
    if event_code in _LIFECYCLE_EVENTS and open_close is None:
        open_close = "C"

    raw_ts = _extract_attr(attrs, "dateTime", "Date/Time", "tradeDate", "date", "reportDate")
    if raw_ts is None:
        raw_ts = fallback_ts
    if raw_ts is None:
        return None
    executed_at = normalize_utc_timestamp(raw_ts)

    ib_order_id = _extract_attr(attrs, "ibOrderID", "orderID", "orderId")
    conid = sanitize_optional_conid(_extract_attr(attrs, "conid", "conId"))
    underlying_conid = sanitize_optional_conid(
        _extract_attr(attrs, "underlyingConid", "underlyingConId")
    )
    currency = (_extract_attr(attrs, "currency", "currencyPrimary") or "USD").upper()
    fx_val = sanitize_signed_float(_extract_attr(attrs, "fxRateToBase"))
    fx_rate = fx_val if fx_val is not None and fx_val > 0.0 else 1.0

    exec_id = _extract_attr(attrs, "ibExecID", "execID", "execId", "transactionID", "tradeID")
    if not exec_id:
        base_id = synthesize_exec_id(
            event_code=event_code,
            executed_at=executed_at,
            underlying=underlying,
            asset_class=asset_class,
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
        occ = seen_synthetic[base_id]
        seen_synthetic[base_id] += 1
        exec_id = (
            base_id
            if occ == 0
            else synthesize_exec_id(
                event_code=event_code,
                executed_at=executed_at,
                underlying=underlying,
                asset_class=asset_class,
                symbol=symbol,
                expiry=expiry,
                strike=strike,
                right=right,
                side=side,
                quantity=quantity,
                price=price,
                proceeds=proceeds,
                ib_order_id=ib_order_id,
                hint=f"occ:{occ}",
            )
        )

    return LedgerExecutionRow(
        exec_id=exec_id,
        ib_order_id=ib_order_id,
        conid=conid,
        underlying_conid=underlying_conid,
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
        open_close=open_close,
        event_code=event_code,
        currency=currency,
        fx_rate_to_base=fx_rate,
        executed_at=executed_at,
        source=default_source,
    )


def _build_position_from_mapping(
    attrs: Mapping[str, str],
    *,
    default_source: LedgerSource,
    fallback_ts: str,
) -> LedgerPositionRow | None:
    symbol = _extract_attr(attrs, "symbol", "localSymbol") or ""
    if not symbol or symbol.upper().startswith(("TOTAL", "SUBTOTAL")):
        return None

    asset_class = _resolve_asset_class(attrs, symbol)
    if asset_class is None:
        return None

    parsed_opt = parse_option_symbol(symbol) if asset_class == "OPT" else None
    underlying_raw = _extract_attr(attrs, "underlyingSymbol")
    if not underlying_raw:
        underlying_raw = parsed_opt[0] if parsed_opt is not None else symbol.split()[0]
    underlying = normalize_underlying(underlying_raw)

    expiry = normalize_expiry_yyyymmdd(
        _extract_attr(attrs, "expiry", "lastTradeDateOrContractMonth")
    )
    strike = sanitize_signed_float(_extract_attr(attrs, "strike"))
    right = normalize_right_optional(_extract_attr(attrs, "putCall", "right"))

    if asset_class == "OPT" and parsed_opt is not None:
        if expiry is None:
            expiry = parsed_opt[1]
        if strike is None or strike <= 0.0:
            strike = parsed_opt[2]
        if right is None:
            right = parsed_opt[3]
    elif asset_class == "STK":
        expiry = None
        strike = None
        right = None

    mult_val = sanitize_signed_float(_extract_attr(attrs, "multiplier", "mult"))
    default_mult = 1 if asset_class == "STK" else 100
    multiplier = int(mult_val) if mult_val and mult_val > 0 else default_mult

    mark_price = sanitize_signed_float(_extract_attr(attrs, "markPrice", "Close Price"))
    market_value = sanitize_signed_float(
        _extract_attr(attrs, "positionValue", "marketValue", "Value")
    )

    pos_qty = sanitize_signed_float(_extract_attr(attrs, "position", "quantity"))
    if pos_qty is None:
        if (
            market_value is not None
            and mark_price is not None
            and mark_price > 0.0
            and multiplier > 0
        ):
            pos_qty = round(market_value / (mark_price * multiplier), 6)
        else:
            return None
    pos_side = (_extract_attr(attrs, "side") or "").upper()
    if pos_side == "SHORT" and pos_qty > 0.0:
        pos_qty = -pos_qty
    if abs(pos_qty) <= 1e-6:
        return None

    avg_cost = sanitize_signed_float(
        _extract_attr(attrs, "costBasisPrice", "Cost Price", "openPrice", "avgCost")
    )
    if avg_cost is None:
        basis_money = sanitize_signed_float(_extract_attr(attrs, "costBasisMoney", "Cost Basis"))
        avg_cost = abs(basis_money / (pos_qty * multiplier)) if basis_money is not None else 0.0

    unrealized = sanitize_signed_float(
        _extract_attr(attrs, "fifoPnlUnrealized", "unrealizedPnl", "Unrealized P/L")
    )
    raw_updated = _extract_attr(attrs, "reportDate", "dateTime", "toDate") or fallback_ts
    updated_at = normalize_utc_timestamp(raw_updated)

    return LedgerPositionRow(
        conid=sanitize_optional_conid(_extract_attr(attrs, "conid", "conId")),
        symbol=symbol,
        underlying=underlying,
        asset_class=asset_class,
        expiry=expiry,
        strike=strike,
        right=right,
        multiplier=multiplier,
        position=pos_qty,
        avg_cost=avg_cost,
        mark_price=mark_price,
        market_value=market_value,
        unrealized_pnl=unrealized,
        source=default_source,
        updated_at=updated_at,
    )


_ACCOUNT_ATTR_KEYS: frozenset[str] = frozenset(
    {"accountid", "acctid", "acctcode", "account", "clientaccountid"}
)


def _strip_account_attrs(attrs: Mapping[str, str]) -> dict[str, str]:
    """Return copy of attributes excluding any account identifier keys."""
    return {k: v for k, v in attrs.items() if k.strip().lower() not in _ACCOUNT_ATTR_KEYS}


def _trade_lifecycle_sig(
    row: LedgerExecutionRow,
) -> tuple[str, str, str, str, float, str, str]:
    return (
        row.underlying,
        row.asset_class,
        row.symbol,
        row.side,
        round(row.quantity, 4),
        row.executed_at[:10],
        row.event_code,
    )


def parse_flex_xml(
    xml_bytes: bytes | str,
    *,
    default_source: LedgerSource = "FLEX_ACTIVITY_365",
) -> tuple[list[LedgerExecutionRow], list[LedgerPositionRow]]:
    """Parse IBKR Flex XML (`Trade`, `TradeConfirm`, `OptionEAE`, `OpenPosition`) for OPT/STK."""
    raw = xml_bytes.encode("utf-8") if isinstance(xml_bytes, str) else xml_bytes
    if not raw.strip():
        raise StorageSchemaError("Empty Flex XML payload")
    try:
        root = ET.fromstring(raw.lstrip())
    except ET.ParseError as exc:
        raise StorageSchemaError(redact_flex_secrets(f"Malformed Flex XML payload: {exc}")) from exc

    if root.tag == "FlexStatementResponse":
        status = _tag_text(root, "Status")
        code = _tag_text(root, "ErrorCode")
        msg = redact_flex_secrets(_tag_text(root, "ErrorMessage"))
        raise FlexServiceError(f"Flex statement response {status} (code {code}): {msg}")

    statements = root.findall(".//FlexStatement")
    if not statements:
        statements = [root]

    executions: list[LedgerExecutionRow] = []
    positions: list[LedgerPositionRow] = []
    seen_synthetic: Counter[str] = Counter()
    now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        for stmt in statements:
            stmt_date = stmt.attrib.get("toDate") or stmt.attrib.get("reportDate") or now_utc
            trade_elems = list(stmt.iter("Trade"))
            has_exec_detail = any(
                elem.attrib.get("levelOfDetail", "").strip().upper() == "EXECUTION"
                for elem in trade_elems
            )
            trade_lifecycle_keys: set[tuple[str, str, str, str, float, str, str]] = set()

            for elem in trade_elems:
                lod = elem.attrib.get("levelOfDetail", "").strip().upper()
                if lod in _SUMMARY_DETAIL_LEVELS:
                    continue
                if has_exec_detail and lod == "ORDER":
                    continue
                row = _build_execution_from_mapping(
                    _strip_account_attrs(elem.attrib),
                    default_source=default_source,
                    fallback_ts=stmt_date,
                    seen_synthetic=seen_synthetic,
                )
                if row is not None:
                    executions.append(row)
                    if row.event_code in _LIFECYCLE_EVENTS:
                        trade_lifecycle_keys.add(_trade_lifecycle_sig(row))

            for elem in stmt.iter("TradeConfirm"):
                lod = elem.attrib.get("levelOfDetail", "").strip().upper()
                if lod in _SUMMARY_DETAIL_LEVELS:
                    continue
                row = _build_execution_from_mapping(
                    _strip_account_attrs(elem.attrib),
                    default_source=default_source,
                    fallback_ts=stmt_date,
                    seen_synthetic=seen_synthetic,
                )
                if row is not None:
                    executions.append(row)
                    if row.event_code in _LIFECYCLE_EVENTS:
                        trade_lifecycle_keys.add(_trade_lifecycle_sig(row))

            for elem in stmt.iter("OptionEAE"):
                lod = elem.attrib.get("levelOfDetail", "").strip().upper()
                if lod in _SUMMARY_DETAIL_LEVELS:
                    continue
                raw_tt = elem.attrib.get("transactionType", "")
                forced_event = _normalize_event_code(raw_tt)
                if forced_event == "TRADE" and raw_tt:
                    continue
                if forced_event == "TRADE":
                    forced_event = "EXPIRED"
                row = _build_execution_from_mapping(
                    _strip_account_attrs(elem.attrib),
                    default_source=default_source,
                    forced_event=forced_event,
                    fallback_ts=stmt_date,
                    seen_synthetic=seen_synthetic,
                )
                if row is not None:
                    if _trade_lifecycle_sig(row) in trade_lifecycle_keys:
                        continue
                    executions.append(row)

            for elem in stmt.iter("OpenPosition"):
                lod = elem.attrib.get("levelOfDetail", "").strip().upper()
                if lod in {"LOT", "TAX_LOT", "CLOSED_LOT", "SUBTOTAL", "TOTAL"}:
                    continue
                pos = _build_position_from_mapping(
                    _strip_account_attrs(elem.attrib),
                    default_source=default_source,
                    fallback_ts=stmt_date,
                )
                if pos is not None:
                    positions.append(pos)
    except (StorageSchemaError, FlexServiceError):
        raise
    except ValueError as exc:
        raise StorageSchemaError(
            redact_flex_secrets(f"Malformed ledger row/payload: {exc}")
        ) from exc

    return executions, positions


def _parse_period_end_date(period_text: str) -> str | None:
    """Extract end date from IBKR Activity `Statement,Data,Period` strings if recognizable."""
    parts = [p.strip() for p in period_text.split("-") if p.strip()]
    candidate = parts[-1] if parts else period_text.strip()
    try:
        return normalize_utc_timestamp(candidate)
    except ValueError:
        pass
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            dt = datetime.strptime(candidate, fmt)
            return dt.strftime("%Y-%m-%dT00:00:00Z")
        except ValueError:
            continue
    return None


_ACTIVITY_SECTIONS: frozenset[str] = frozenset(
    {
        "Statement",
        "Account Information",
        "Trades",
        "Open Positions",
        "Net Asset Value",
        "Cash Report",
    }
)


def _is_activity_statement(rows: Sequence[list[str]]) -> bool:
    for row in rows[:20]:
        if (
            len(row) >= 3
            and row[1].strip() in {"Header", "Data", "SubTotal", "Total"}
            and row[0].strip() in _ACTIVITY_SECTIONS
        ):
            return True
    return False


def _parse_activity_statement_rows(
    rows: Sequence[list[str]],
    *,
    default_source: LedgerSource,
) -> tuple[list[LedgerExecutionRow], list[LedgerPositionRow]]:
    now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    stmt_ts = now_utc
    trades_header: list[str] = []
    positions_header: list[str] = []
    trade_dicts: list[dict[str, str]] = []
    position_dicts: list[dict[str, str]] = []

    for row in rows:
        if len(row) < 2:
            continue
        section = row[0].strip()
        row_type = row[1].strip()
        if section == "Statement" and row_type == "Data" and len(row) >= 4:
            if row[2].strip().lower() == "period":
                parsed_end = _parse_period_end_date(row[3])
                if parsed_end is not None:
                    stmt_ts = parsed_end
        elif section == "Trades":
            if row_type == "Header":
                trades_header = [col.strip() for col in row[2:]]
            elif row_type == "Data" and trades_header:
                mapped = {
                    col: (row[2 + idx].strip() if 2 + idx < len(row) else "")
                    for idx, col in enumerate(trades_header)
                }
                trade_dicts.append(_strip_account_attrs(mapped))
        elif section == "Open Positions":
            if row_type == "Header":
                positions_header = [col.strip() for col in row[2:]]
            elif row_type == "Data" and positions_header:
                mapped = {
                    col: (row[2 + idx].strip() if 2 + idx < len(row) else "")
                    for idx, col in enumerate(positions_header)
                }
                position_dicts.append(_strip_account_attrs(mapped))

    has_order_rows = any(
        d.get("DataDiscriminator", "").strip().upper() == "ORDER" for d in trade_dicts
    )
    executions: list[LedgerExecutionRow] = []
    seen_synthetic: Counter[str] = Counter()
    for d in trade_dicts:
        disc = d.get("DataDiscriminator", "").strip().upper()
        if disc in {"SUBTOTAL", "TOTAL", "CLOSEDLOT", "LOT", "WASHSALE"}:
            continue
        if has_order_rows and disc not in {"ORDER", ""}:
            continue
        ex = _build_execution_from_mapping(
            d,
            default_source=default_source,
            fallback_ts=stmt_ts,
            seen_synthetic=seen_synthetic,
        )
        if ex is not None:
            executions.append(ex)

    positions: list[LedgerPositionRow] = []
    for d in position_dicts:
        disc = d.get("DataDiscriminator", "").strip().upper()
        if disc in {"LOT", "TAXLOT", "CLOSEDLOT", "SUBTOTAL", "TOTAL"}:
            continue
        pos = _build_position_from_mapping(
            d,
            default_source=default_source,
            fallback_ts=stmt_ts,
        )
        if pos is not None:
            positions.append(pos)

    return executions, positions


def _parse_flat_flex_csv_rows(
    rows: Sequence[list[str]],
    *,
    default_source: LedgerSource,
) -> tuple[list[LedgerExecutionRow], list[LedgerPositionRow]]:
    now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    executions: list[LedgerExecutionRow] = []
    positions: list[LedgerPositionRow] = []
    seen_synthetic: Counter[str] = Counter()

    # Check if multi-section Flex CSV (`HEADER`/`DATA` prefix rows) vs plain flat CSV
    has_header_markers = any(
        len(r) >= 2 and r[0].strip().upper() in {"BOF", "HEADER", "DATA"} for r in rows[:10]
    )

    if has_header_markers:
        current_headers: dict[str, list[str]] = {}
        for row in rows:
            tag = row[0].strip().upper()
            if tag in {"BOF", "BOA", "BOS", "EOS", "EOA", "EOF"} or len(row) < 3:
                continue
            section_code = row[1].strip().upper()
            if tag == "HEADER":
                current_headers[section_code] = [c.strip() for c in row[2:]]
            elif tag == "DATA" and section_code in current_headers:
                hdr = current_headers[section_code]
                mapped = _strip_account_attrs(
                    {
                        col: (row[2 + idx].strip() if 2 + idx < len(row) else "")
                        for idx, col in enumerate(hdr)
                    }
                )
                if _extract_attr(mapped, "position") is not None and (
                    _extract_attr(mapped, "buySell", "tradePrice") is None
                ):
                    pos = _build_position_from_mapping(
                        mapped, default_source=default_source, fallback_ts=now_utc
                    )
                    if pos is not None:
                        positions.append(pos)
                else:
                    ex = _build_execution_from_mapping(
                        mapped,
                        default_source=default_source,
                        fallback_ts=now_utc,
                        seen_synthetic=seen_synthetic,
                    )
                    if ex is not None:
                        executions.append(ex)
        return executions, positions

    header = [c.strip() for c in rows[0]]
    header_lower = {c.lower() for c in header}
    is_position_table = "position" in header_lower and not (
        {"buy/sell", "buysell", "tradeprice", "t. price"} & header_lower
    )
    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        mapped = _strip_account_attrs(
            {col: (row[idx].strip() if idx < len(row) else "") for idx, col in enumerate(header)}
        )
        lod = (_extract_attr(mapped, "levelOfDetail") or "").upper()
        if lod in _SUMMARY_DETAIL_LEVELS and not (is_position_table and lod == "SUMMARY"):
            continue
        if is_position_table:
            pos = _build_position_from_mapping(
                mapped, default_source=default_source, fallback_ts=now_utc
            )
            if pos is not None:
                positions.append(pos)
        else:
            ex = _build_execution_from_mapping(
                mapped,
                default_source=default_source,
                fallback_ts=now_utc,
                seen_synthetic=seen_synthetic,
            )
            if ex is not None:
                executions.append(ex)

    return executions, positions


def parse_activity_csv(
    csv_text: str | bytes,
    *,
    default_source: LedgerSource = "FLEX_IMPORT",
) -> tuple[list[LedgerExecutionRow], list[LedgerPositionRow]]:
    """Parse either flat Flex CSV or multi-section IBKR Activity Statement CSV (`OPT`/`STK`)."""
    try:
        text = (
            csv_text.decode("utf-8-sig")
            if isinstance(csv_text, bytes)
            else csv_text.lstrip("\ufeff")
        )
        if not text.strip():
            return ([], [])
        if "\x00" in text:
            raise StorageSchemaError("Malformed ledger row/payload: unexpected NUL byte in CSV")

        rows = [row for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)]
        if not rows:
            return ([], [])

        if _is_activity_statement(rows):
            return _parse_activity_statement_rows(rows, default_source=default_source)
        return _parse_flat_flex_csv_rows(rows, default_source=default_source)
    except (StorageSchemaError, UnicodeDecodeError):
        raise
    except (ValueError, csv.Error) as exc:
        raise StorageSchemaError(
            redact_flex_secrets(f"Malformed ledger row/payload: {exc}")
        ) from exc
