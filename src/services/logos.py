"""Watchlist company logo lookup and disk-backed caching service (non-IBKR)."""

from __future__ import annotations

import asyncio
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from src.core.security import TickerValidator
from src.services._concurrency import BlockingRunner
from src.storage.logo_store import (
    MAX_LOGO_BYTES,
    LogoExt,
    LogoReadResult,
    LogoStore,
    validate_logo_payload,
)

logger = logging.getLogger(__name__)

ELBSTREAM_LOGO_URL: Final = "https://api.elbstream.com/logos/symbol/{ticker}?format={fmt}"
DEFAULT_LOGO_TIMEOUT_SEC: Final = 3.0


@dataclass(frozen=True, slots=True)
class UpstreamFetchResult:
    """Result of one upstream symbol-logo HTTP fetch."""

    status: Literal["ok", "not_found", "error"]
    data: bytes = b""
    ext: LogoExt = "svg"


@dataclass(frozen=True, slots=True)
class LogoPayload:
    """Resolved ticker logo served to `GET /api/logo/{ticker}`."""

    ticker: str
    data: bytes
    media_type: str
    source: Literal["cache", "upstream", "stale_cache"]


class LogoFetcher(Protocol):
    """Fetch a single validated uppercase ticker symbol from an upstream logo provider."""

    def __call__(self, symbol: str, /) -> UpstreamFetchResult: ...


def _infer_logo_ext(content_type: str, data: bytes, preferred: LogoExt) -> LogoExt:
    ct = content_type.split(";", 1)[0].strip().lower()
    if "svg" in ct:
        return "svg"
    if "png" in ct:
        return "png"
    if "webp" in ct:
        return "webp"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if b"<svg" in data[:512].lower():
        return "svg"
    return preferred


def fetch_elbstream_logo(
    symbol: str,
    *,
    timeout_sec: float = DEFAULT_LOGO_TIMEOUT_SEC,
) -> UpstreamFetchResult:
    """Fetch `symbol` SVG (fallback PNG) from Elbstream with bounded size and timeout."""
    saw_not_found = False
    for fmt in ("svg", "png"):
        ext_hint: LogoExt = "svg" if fmt == "svg" else "png"
        url = ELBSTREAM_LOGO_URL.format(ticker=symbol, fmt=fmt)
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "ibkr-options-analyzer/1.0",
                "Accept": "image/svg+xml,image/png,image/webp",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:  # noqa: S310 - HTTPS URL constant
                raw = resp.read(MAX_LOGO_BYTES + 1)
                if len(raw) > MAX_LOGO_BYTES:
                    logger.warning("upstream logo too large for %s (%s)", symbol, fmt)
                    return UpstreamFetchResult(status="error")
                ct = str(resp.headers.get("Content-Type", ""))
                ext = _infer_logo_ext(ct, raw, ext_hint)
                validated = validate_logo_payload(raw, ext)
                return UpstreamFetchResult(status="ok", data=validated, ext=ext)
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404, 410, 415):
                saw_not_found = True
                continue
            logger.debug("upstream logo HTTP %s for %s", exc.code, symbol)
            return UpstreamFetchResult(status="error")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            logger.debug("upstream logo fetch error for %s (%s): %s", symbol, fmt, exc)
            return UpstreamFetchResult(status="error")
    return UpstreamFetchResult(status="not_found" if saw_not_found else "error")


class LogoService:
    """Disk-cached logo resolution with single-flight per-ticker upstream deduplication."""

    def __init__(
        self,
        store: LogoStore,
        *,
        fetcher: LogoFetcher | None = None,
        runner: BlockingRunner | None = None,
    ) -> None:
        self.store = store
        self.fetcher: LogoFetcher = fetcher or fetch_elbstream_logo
        self.runner: BlockingRunner = runner or asyncio.to_thread
        self._inflight: dict[str, asyncio.Lock] = {}

    def ensure_layout(self) -> None:
        """Create the local logo cache directory if absent."""
        self.store.ensure_layout()

    async def get_logo(self, raw_ticker: str, *, refresh: bool = False) -> LogoPayload | None:
        """Return cached or freshly fetched logo bytes, falling back to stale cache if offline."""
        symbol = TickerValidator.validate(raw_ticker)

        if not refresh:
            fast: LogoReadResult = await self.runner(lambda: self.store.read(symbol))
            if fast.logo is not None and not fast.logo.is_stale:
                return LogoPayload(
                    ticker=symbol,
                    data=fast.logo.data,
                    media_type=fast.logo.media_type,
                    source="cache",
                )
            if fast.logo is None and fast.miss_active:
                return None

        lock = self._inflight.setdefault(symbol, asyncio.Lock())
        async with lock:
            current: LogoReadResult = await self.runner(lambda: self.store.read(symbol))
            if not refresh:
                if current.logo is not None and not current.logo.is_stale:
                    return LogoPayload(
                        ticker=symbol,
                        data=current.logo.data,
                        media_type=current.logo.media_type,
                        source="cache",
                    )
                if current.logo is None and current.miss_active:
                    return None

            fetched = await self.runner(lambda: self.fetcher(symbol))
            if fetched.status == "ok":
                saved = await self.runner(
                    lambda: self.store.save(symbol, fetched.data, fetched.ext)
                )
                return LogoPayload(
                    ticker=symbol,
                    data=saved.data,
                    media_type=saved.media_type,
                    source="upstream",
                )

            if current.logo is not None:
                return LogoPayload(
                    ticker=symbol,
                    data=current.logo.data,
                    media_type=current.logo.media_type,
                    source="stale_cache",
                )

            if fetched.status == "not_found":
                await self.runner(lambda: self.store.mark_missing(symbol))
            return None
