"""Typed domain exceptions for the IBKR Options Analyzer."""

from __future__ import annotations


class AnalyzerError(Exception):
    """Base class for every domain-specific error raised by the analyzer."""


class SecurityViolationError(AnalyzerError):
    """Raised when a blocked IBKR order/mutation method is invoked."""


class InvalidTickerError(AnalyzerError):
    """Raised on ticker regex failure or IBKR contract qualification failure."""


class ConfigurationError(AnalyzerError):
    """Raised when configuration (YAML / environment) is missing or invalid."""


class GatewayConnectionError(AnalyzerError):
    """Raised when the IBKR Gateway cannot be reached or the connection drops."""


class GatewayTimeoutError(GatewayConnectionError):
    """Raised when an IBKR request is not answered within `ibkr.request_timeout_sec`.

    The local API socket may still be open (e.g. the Gateway lost its IBKR upstream), so the
    message is fixed and sanitized: it names only the stage and the limit, never raw IBKR text.
    """


class UnderlyingPriceUnavailableError(AnalyzerError):
    """Raised when no usable underlying spot price can be obtained."""


class OptionChainFetchError(AnalyzerError):
    """Raised when option chain parameters or market data cannot be fetched."""


class CorridorBusyError(OptionChainFetchError):
    """Raised when a Corridor stream would subscribe while another IBKR pull holds the lock.

    Subclasses `OptionChainFetchError` so the SSE route reports it through its existing
    `event: error` (`CORRIDOR_STREAM_ERROR`) path; no market-data line is opened.
    """


class ScreenerLiveBusyError(OptionChainFetchError):
    """Raised when the live screener stream would subscribe while `refresh_lock` is held.

    Sibling of `CorridorBusyError` (fixed `SCREENER_LIVE_BUSY_MESSAGE`); raised at subscribe time
    by `LiveLegQuoteService.stream_live_legs(..., is_busy)` with zero market-data lines opened.
    """


class PacingViolationError(AnalyzerError):
    """Raised when IBKR pacing / market-data line limits would be violated."""


class StorageSchemaError(AnalyzerError):
    """Raised when CSV/row data violates the storage schema contract."""


class StorageUnavailableError(AnalyzerError):
    """Raised when an existing storage file cannot be opened/read (e.g. locked); nothing written."""


class InvalidRequestError(AnalyzerError):
    """Request is well-formed but semantically invalid for the current data (HTTP 422)."""


class LiveStreamNotFoundError(InvalidRequestError):
    """Raised when a live screener stream id is unknown or the stream already stopped.

    Subclasses `InvalidRequestError` (422 by default); the client re-opens the stream.
    """


class SnapshotNotFoundError(AnalyzerError):
    """Raised when a requested snapshot / latest CSV does not exist on disk."""


class FlexServiceError(AnalyzerError):
    """Raised when IBKR Flex Web Service requests or statement downloads fail."""


class RefreshBusyError(AnalyzerError):
    """Raised when a chain refresh cannot start because another refresh holds the lock."""

    def __init__(self, message: str, *, owner: str, retry_after_sec: int) -> None:
        super().__init__(message)
        self.owner = owner
        self.retry_after_sec = retry_after_sec


class BatchNotFoundError(AnalyzerError):
    """Raised when a batch refresh id is unknown (only the latest batch is kept in memory)."""


class DuplicatePaperTradeError(InvalidRequestError):
    """Raised when registering a paper trade whose active legs already exist."""


class PaperTradeCapError(InvalidRequestError):
    """Raised when registering a paper trade would exceed `max_active_trades`."""


class WatchlistCapError(InvalidRequestError):
    """Raised when adding a ticker would exceed `watchlist.max_size`."""


class PaperTradeStateError(InvalidRequestError):
    """Raised on an invalid paper trade status transition."""


class BacktestValidationError(InvalidRequestError):
    """Raised when a backtest request or strategy specification fails validation."""
