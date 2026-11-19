"""Local disk cache for company/ETF ticker logos (`{storage.data_dir}/cache/logos/`).

Stores fetched `.svg` / `.png` / `.webp` icons atomically alongside short-lived `.miss`
markers so missing or offline tickers do not hammer upstream providers. Users can also drop
custom `{TICKER}.{svg,png,webp}` files directly into the directory for offline use; image
files always take precedence over `.miss` markers.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from src.core.config import StorageSettings, resolve_project_path
from src.core.security import TickerValidator

logger = logging.getLogger(__name__)

LogoExt = Literal["svg", "png", "webp"]

LOGO_CACHE_SUBDIR: Final = Path("cache") / "logos"
SUPPORTED_LOGO_EXTS: tuple[LogoExt, ...] = ("svg", "png", "webp")
MEDIA_TYPE_BY_EXT: dict[LogoExt, str] = {
    "svg": "image/svg+xml",
    "png": "image/png",
    "webp": "image/webp",
}
MAX_LOGO_BYTES: Final = 256 * 1024
DEFAULT_LOGO_TTL_SEC: Final = 30.0 * 86_400.0
DEFAULT_MISS_TTL_SEC: Final = 7.0 * 86_400.0

_PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"
_UNSAFE_SVG_RE: Final = re.compile(
    r"(?:<\s*script\b|<\s*foreignobject\b|\bon[a-z]+\s*=|(?:javascript|vbscript)\s*:)",
    re.IGNORECASE,
)


def resolve_logo_cache_dir(storage: StorageSettings, root: Path | None = None) -> Path:
    """Resolve `{storage.data_dir}/cache/logos` against `root` (or project root)."""
    if root is None:
        return resolve_project_path(storage.data_dir) / LOGO_CACHE_SUBDIR
    data_dir = Path(storage.data_dir)
    base = data_dir if data_dir.is_absolute() else Path(root) / data_dir
    return base.resolve() / LOGO_CACHE_SUBDIR


def validate_logo_payload(data: bytes, ext: LogoExt) -> bytes:
    """Validate image size, magic header, and SVG active-content safety."""
    if not data or len(data) > MAX_LOGO_BYTES:
        raise ValueError(f"Logo payload size {len(data)} outside 1..{MAX_LOGO_BYTES} bytes")
    if ext == "png":
        if not data.startswith(_PNG_MAGIC):
            raise ValueError("Invalid PNG header")
        return data
    if ext == "webp":
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            raise ValueError("Invalid WebP header")
        return data
    if ext == "svg":
        text = data.decode("utf-8", errors="strict").lstrip("\ufeff \t\r\n")
        lower = text.lower()
        if "<svg" not in lower or ("</svg>" not in lower and "/>" not in lower):
            raise ValueError("Invalid SVG document")
        if _UNSAFE_SVG_RE.search(text):
            raise ValueError("SVG contains disallowed active content")
        return data
    raise ValueError(f"Unsupported logo extension: {ext!r}")


@dataclass(frozen=True, slots=True)
class CachedLogo:
    """Validated logo bytes read from or written to disk."""

    ticker: str
    data: bytes
    ext: LogoExt
    media_type: str
    is_stale: bool = False


@dataclass(frozen=True, slots=True)
class LogoReadResult:
    """Cache lookup outcome distinguishing hit/stale image from negative `.miss` cache."""

    logo: CachedLogo | None = None
    miss_active: bool = False


class LogoStore:
    """Thread-safe atomic disk store for cached and manual offline ticker logos."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        logo_ttl_sec: float = DEFAULT_LOGO_TTL_SEC,
        miss_ttl_sec: float = DEFAULT_MISS_TTL_SEC,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.logo_ttl_sec = max(0.0, float(logo_ttl_sec))
        self.miss_ttl_sec = max(0.0, float(miss_ttl_sec))
        self._clock = clock or time.time
        self._lock = threading.RLock()

    def ensure_layout(self) -> None:
        """Idempotently create the local logo cache directory."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def read(self, raw_ticker: str) -> LogoReadResult:
        """Read a validated cached logo or active `.miss` marker for `raw_ticker`."""
        symbol = TickerValidator.validate(raw_ticker)
        now = float(self._clock())
        with self._lock:
            for ext in SUPPORTED_LOGO_EXTS:
                path = self.cache_dir / f"{symbol}.{ext}"
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                    if stat.st_size <= 0 or stat.st_size > MAX_LOGO_BYTES:
                        continue
                    raw = path.read_bytes()
                    validated = validate_logo_payload(raw, ext)
                except (OSError, UnicodeDecodeError, ValueError) as exc:
                    logger.warning("ignoring unusable cached logo %s: %s", path.name, exc)
                    continue
                age = max(0.0, now - stat.st_mtime)
                return LogoReadResult(
                    logo=CachedLogo(
                        ticker=symbol,
                        data=validated,
                        ext=ext,
                        media_type=MEDIA_TYPE_BY_EXT[ext],
                        is_stale=age > self.logo_ttl_sec,
                    ),
                    miss_active=False,
                )

            miss_path = self.cache_dir / f"{symbol}.miss"
            if miss_path.is_file():
                try:
                    age = max(0.0, now - miss_path.stat().st_mtime)
                    if age <= self.miss_ttl_sec:
                        return LogoReadResult(logo=None, miss_active=True)
                except OSError:
                    pass
        return LogoReadResult(logo=None, miss_active=False)

    def save(self, raw_ticker: str, data: bytes, ext: LogoExt) -> CachedLogo:
        """Atomically persist a validated logo and clear any stale extensions or `.miss` marker."""
        symbol = TickerValidator.validate(raw_ticker)
        validated = validate_logo_payload(data, ext)
        with self._lock:
            self.ensure_layout()
            target = self.cache_dir / f"{symbol}.{ext}"
            tmp = self.cache_dir / f"{symbol}.{ext}.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                with tmp.open("wb") as handle:
                    handle.write(validated)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, target)
            except BaseException:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            for other_ext in SUPPORTED_LOGO_EXTS:
                if other_ext != ext:
                    with contextlib.suppress(OSError):
                        (self.cache_dir / f"{symbol}.{other_ext}").unlink()
            with contextlib.suppress(OSError):
                (self.cache_dir / f"{symbol}.miss").unlink()
        return CachedLogo(
            ticker=symbol,
            data=validated,
            ext=ext,
            media_type=MEDIA_TYPE_BY_EXT[ext],
            is_stale=False,
        )

    def mark_missing(self, raw_ticker: str) -> None:
        """Write a short `.miss` marker if no logo image exists on disk for `raw_ticker`."""
        symbol = TickerValidator.validate(raw_ticker)
        with self._lock:
            if any((self.cache_dir / f"{symbol}.{ext}").is_file() for ext in SUPPORTED_LOGO_EXTS):
                return
            self.ensure_layout()
            target = self.cache_dir / f"{symbol}.miss"
            tmp = self.cache_dir / f"{symbol}.miss.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                tmp.write_text("miss\n", encoding="utf-8")
                os.replace(tmp, target)
            except BaseException:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
