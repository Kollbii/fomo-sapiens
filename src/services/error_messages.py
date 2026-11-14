"""Client-safe `(error_code, message)` for domain exceptions, outside of an HTTP response.

Used where an error must be reported inside a payload instead of being raised (e.g. one failed
ticker of a batch refresh). The codes and wording mirror `src/web/errors.py`: domain exceptions
(`src/core/exceptions.py`) carry messages that are already safe for the dashboard; anything
else (including the bare `AnalyzerError` base, which the web layer does not map either) gets
the fixed `GENERIC_ERROR_MESSAGE` so raw exception text and stack traces never reach a client.
`GatewayTimeoutError` (a `GatewayConnectionError` subclass) gets its own `GATEWAY_TIMEOUT` code.
Callers log the original exception server-side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from src.core.config import IBKRSettings
from src.core.exceptions import (
    AnalyzerError,
    BacktestValidationError,
    BatchNotFoundError,
    ConfigurationError,
    DuplicatePaperTradeError,
    FlexServiceError,
    GatewayConnectionError,
    GatewayTimeoutError,
    InvalidRequestError,
    InvalidTickerError,
    OptionChainFetchError,
    PacingViolationError,
    PaperTradeCapError,
    PaperTradeStateError,
    RefreshBusyError,
    SecurityViolationError,
    SnapshotNotFoundError,
    StorageSchemaError,
    StorageUnavailableError,
    UnderlyingPriceUnavailableError,
    WatchlistCapError,
)

GENERIC_ERROR_CODE: Final = "INTERNAL_ERROR"
GATEWAY_TIMEOUT_CODE: Final = "GATEWAY_TIMEOUT"
"""`GatewayTimeoutError`: the local socket may be up while the Gateway lost its IBKR upstream."""
GENERIC_ERROR_MESSAGE: Final = "Unexpected internal error; details were logged on the server."
MAX_SAFE_MESSAGE_CHARS: Final = 300

# Most specific first: the first `isinstance` match wins.
_DOMAIN_CODES: Final[tuple[tuple[type[AnalyzerError], str], ...]] = (
    (InvalidTickerError, "INVALID_TICKER"),
    (DuplicatePaperTradeError, "DUPLICATE_PAPER_TRADE"),
    (PaperTradeCapError, "PAPER_TRADE_CAP"),
    (PaperTradeStateError, "PAPER_TRADE_STATE"),
    (BacktestValidationError, "BACKTEST_VALIDATION_ERROR"),
    (WatchlistCapError, "WATCHLIST_FULL"),
    (InvalidRequestError, "INVALID_REQUEST"),
    (SnapshotNotFoundError, "SNAPSHOT_NOT_FOUND"),
    (BatchNotFoundError, "BATCH_NOT_FOUND"),
    (GatewayTimeoutError, GATEWAY_TIMEOUT_CODE),
    (GatewayConnectionError, "GATEWAY_OFFLINE"),
    (UnderlyingPriceUnavailableError, "UNDERLYING_PRICE_UNAVAILABLE"),
    (OptionChainFetchError, "OPTION_CHAIN_FETCH_ERROR"),
    (PacingViolationError, "PACING_VIOLATION"),
    (StorageSchemaError, "STORAGE_SCHEMA_ERROR"),
    (StorageUnavailableError, "STORAGE_UNAVAILABLE"),
    (ConfigurationError, "CONFIGURATION_ERROR"),
    (FlexServiceError, "FLEX_SERVICE_ERROR"),
    (SecurityViolationError, "SECURITY_VIOLATION"),
    (RefreshBusyError, "REFRESH_BUSY"),
)

_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE: Final = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class SafeError:
    """Client-safe error: stable `code` plus a single-line, length-capped `message`."""

    code: str
    message: str


def _single_line(text: str) -> str:
    flat = _SPACE_RE.sub(" ", _CONTROL_RE.sub(" ", text)).strip()
    if len(flat) > MAX_SAFE_MESSAGE_CHARS:
        flat = flat[: MAX_SAFE_MESSAGE_CHARS - 3].rstrip() + "..."
    return flat


def describe_error(exc: BaseException, *, ibkr: IBKRSettings | None = None) -> SafeError:
    """Map `exc` to the same `error_code` / wording the web error layer shows for it."""
    for exc_type, code in _DOMAIN_CODES:
        if isinstance(exc, exc_type):
            message = str(exc)
            if isinstance(exc, GatewayTimeoutError):
                host = ibkr.host if ibkr is not None else "127.0.0.1"
                port = ibkr.port if ibkr is not None else 4001
                message = (
                    f"{message} — IBKR Gateway on {host}:{port} is not answering "
                    "(it may have lost its IBKR connection). "
                    "Serving cached CSV snapshots remains available."
                )
            elif isinstance(exc, GatewayConnectionError):
                host = ibkr.host if ibkr is not None else "127.0.0.1"
                port = ibkr.port if ibkr is not None else 4001
                message = (
                    f"{message} — IBKR Gateway on {host}:{port} is unreachable. "
                    "Serving cached CSV snapshots remains available."
                )
            return SafeError(code, _single_line(message) or GENERIC_ERROR_MESSAGE)
    return SafeError(GENERIC_ERROR_CODE, GENERIC_ERROR_MESSAGE)
