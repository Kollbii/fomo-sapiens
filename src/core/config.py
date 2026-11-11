"""Strict Pydantic v2 configuration: YAML defaults + `.env` / environment overrides."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_YAML_PATH = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
LOG_FILE_NAME = "analyzer.log"
_HANDLER_NAME = "analyzer"
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_VALID_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSettings(_StrictModel):
    name: str = "IBKR Options Analyzer & Dashboard"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    default_theme: Literal["print", "terminal"] = "print"
    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        upper = value.upper()
        if upper not in _VALID_LOG_LEVELS:
            raise ValueError(f"log_level must be one of {_VALID_LOG_LEVELS}, got {value!r}")
        return upper


class IBKRSettings(_StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=4001, ge=1, le=65535)
    client_id: int = Field(default=77, ge=0)
    connect_timeout_sec: float = Field(default=3.0, gt=0)
    read_only_enforced: bool = True
    market_data_mode: Literal["DELAYED", "LIVE"] = "DELAYED"
    default_market_data_type: int = Field(default=3, ge=1, le=4)
    fallback_market_data_type: int = Field(default=4, ge=1, le=4)

    @field_validator("market_data_mode", mode="before")
    @classmethod
    def _upper_mode(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("read_only_enforced")
    @classmethod
    def _must_be_read_only(cls, value: bool) -> bool:
        if not value:
            raise ValueError("read_only_enforced must remain true; the app is strictly read-only")
        return value


class WatchlistSettings(_StrictModel):
    default_ticker: str = "NVDA"
    hardcoded_tickers: tuple[str, ...] = ("NVDA", "AVGO", "CRDO", "PLTR")
    allow_custom_ticker: bool = True
    custom_ticker_regex: str = "^[A-Z]{1,5}$"


class OptionsChainSettings(_StrictModel):
    dte_min: int = Field(default=30, ge=0)
    dte_max: int = Field(default=60, ge=1)
    default_range_pct: float = Field(default=0.20, gt=0, le=1)
    allowed_range_pcts: tuple[float, ...] = (0.10, 0.15, 0.20, 0.25, 0.35, 0.50)
    default_strike_step: float = Field(default=5.0, gt=0)
    allowed_strike_steps: tuple[float, ...] = (1.0, 2.5, 5.0, 10.0, 25.0)
    max_contracts_per_page: int = Field(default=80, ge=1, le=100)
    max_pages_per_refresh: int = Field(default=5, ge=1)
    page_collect_timeout_sec: float = Field(default=2.25, gt=0)
    page_poll_interval_sec: float = Field(default=0.25, gt=0)
    inter_page_cooldown_sec: float = Field(default=0.35, ge=0)


class AnalyticsSettings(_StrictModel):
    risk_free_rate: float = Field(default=0.0425, ge=0, le=1)
    unusual_volume_zscore_threshold: float = Field(default=2.0, gt=0)
    top_walls_count: int = Field(default=3, ge=1)


class StorageSettings(_StrictModel):
    mode: str = "ARCHIVE_MODE_1"
    data_dir: str = "data"
    snapshots_dir: str = "data/snapshots"
    latest_dir: str = "data/latest"
    metadata_index_file: str = "data/metadata/snapshot_index.json"
    logs_dir: str = "data/logs"


class Settings(_StrictModel):
    app: AppSettings = AppSettings()
    ibkr: IBKRSettings = IBKRSettings()
    watchlist: WatchlistSettings = WatchlistSettings()
    options_chain: OptionsChainSettings = OptionsChainSettings()
    analytics: AnalyticsSettings = AnalyticsSettings()
    storage: StorageSettings = StorageSettings()


class _EnvOverrides(BaseSettings):
    """Flat environment variables (process env wins over `.env`) that override YAML."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    ibkr_host: str | None = None
    ibkr_port: int | None = None
    ibkr_client_id: int | None = None
    market_data_mode: str | None = None
    default_market_data_type: int | None = None
    risk_free_rate: float | None = None
    log_level: str | None = None
    app_host: str | None = None
    app_port: int | None = None


# (env field, section, key)
_ENV_MAP: tuple[tuple[str, str, str], ...] = (
    ("ibkr_host", "ibkr", "host"),
    ("ibkr_port", "ibkr", "port"),
    ("ibkr_client_id", "ibkr", "client_id"),
    ("market_data_mode", "ibkr", "market_data_mode"),
    ("default_market_data_type", "ibkr", "default_market_data_type"),
    ("risk_free_rate", "analytics", "risk_free_rate"),
    ("log_level", "app", "log_level"),
    ("app_host", "app", "host"),
    ("app_port", "app", "port"),
)


def load_settings(
    yaml_path: Path | str | None = None,
    env_file: Path | str | None = None,
) -> Settings:
    """Load YAML defaults, then apply `.env` / environment overrides, and validate."""
    path = Path(yaml_path) if yaml_path is not None else DEFAULT_YAML_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"Invalid YAML in {path}: {exc}") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise ConfigurationError(f"{path} must contain a YAML mapping at top level")
        raw = loaded

    try:
        env_path = Path(env_file) if env_file is not None else DEFAULT_ENV_PATH
        overrides = _EnvOverrides(_env_file=env_path)  # type: ignore[call-arg]
        for field, section, key in _ENV_MAP:
            value = getattr(overrides, field)
            if value is not None:
                section_data = raw.get(section)
                if not isinstance(section_data, dict):
                    section_data = {}
                    raw[section] = section_data
                section_data[key] = value
        return Settings.model_validate(raw)
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings dependency (FastAPI `Depends`-friendly)."""
    return load_settings()


def configure_logging(settings: Settings | None = None, log_dir: Path | str | None = None) -> Path:
    """Configure root logging to stdout + rotating file. Idempotent. Returns log file path."""
    cfg = settings or get_settings()
    directory = Path(log_dir) if log_dir is not None else Path(cfg.storage.logs_dir)
    if not directory.is_absolute():
        directory = PROJECT_ROOT / directory
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / LOG_FILE_NAME

    root = logging.getLogger()
    root.setLevel(cfg.app.log_level)
    for handler in list(root.handlers):
        if handler.get_name() == _HANDLER_NAME:
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(_LOG_FORMAT)
    stream = logging.StreamHandler(sys.stdout)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    for handler in (stream, file_handler):
        handler.setFormatter(formatter)
        handler.set_name(_HANDLER_NAME)
        root.addHandler(handler)
    return log_path
