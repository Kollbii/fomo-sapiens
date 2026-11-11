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


class UnderlyingPriceUnavailableError(AnalyzerError):
    """Raised when no usable underlying spot price can be obtained."""


class OptionChainFetchError(AnalyzerError):
    """Raised when option chain parameters or market data cannot be fetched."""


class PacingViolationError(AnalyzerError):
    """Raised when IBKR pacing / market-data line limits would be violated."""


class StorageSchemaError(AnalyzerError):
    """Raised when CSV/row data violates the storage schema contract."""


class SnapshotNotFoundError(AnalyzerError):
    """Raised when a requested snapshot / latest CSV does not exist on disk."""
