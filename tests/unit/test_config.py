from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.core.config import (
    DEFAULT_YAML_PATH,
    Settings,
    configure_logging,
    get_settings,
    load_settings,
)
from src.core.exceptions import ConfigurationError

ENV_VARS = (
    "IBKR_HOST",
    "IBKR_PORT",
    "IBKR_CLIENT_ID",
    "MARKET_DATA_MODE",
    "DEFAULT_MARKET_DATA_TYPE",
    "RISK_FREE_RATE",
    "LOG_LEVEL",
    "APP_HOST",
    "APP_PORT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_yaml_defaults_loaded(tmp_path: Path) -> None:
    s = load_settings(DEFAULT_YAML_PATH, tmp_path / "missing.env")
    assert s.ibkr.port == 4001
    assert s.ibkr.client_id == 77
    assert s.ibkr.default_market_data_type == 3
    assert s.ibkr.fallback_market_data_type == 4
    assert s.ibkr.market_data_mode == "DELAYED"
    assert s.watchlist.hardcoded_tickers == ("NVDA", "AVGO", "CRDO", "PLTR")
    assert s.options_chain.max_contracts_per_page == 80
    assert s.options_chain.target_dte_buckets == (30, 35, 40, 45, 60)
    assert s.analytics.risk_free_rate == 0.0425


def test_model_defaults_without_yaml(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "nope.yaml", tmp_path / "nope.env")
    assert s == Settings()
    assert s.ibkr.port == 4001


def test_dotenv_overrides(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "IBKR_HOST=host.docker.internal\nIBKR_PORT=4002\nIBKR_CLIENT_ID=5\n"
        "MARKET_DATA_MODE=live\nDEFAULT_MARKET_DATA_TYPE=1\nRISK_FREE_RATE=0.05\nLOG_LEVEL=debug\n"
    )
    s = load_settings(DEFAULT_YAML_PATH, env)
    assert s.ibkr.host == "host.docker.internal"
    assert s.ibkr.port == 4002
    assert s.ibkr.client_id == 5
    assert s.ibkr.market_data_mode == "LIVE"
    assert s.ibkr.default_market_data_type == 1
    assert s.analytics.risk_free_rate == 0.05
    assert s.app.log_level == "DEBUG"


def test_process_env_beats_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("IBKR_PORT=4002\n")
    monkeypatch.setenv("IBKR_PORT", "4001")
    assert load_settings(DEFAULT_YAML_PATH, env).ibkr.port == 4001


def test_invalid_values_raise_configuration_error(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("IBKR_PORT=99999\n")
    with pytest.raises(ConfigurationError):
        load_settings(DEFAULT_YAML_PATH, env)


def test_read_only_cannot_be_disabled(tmp_path: Path) -> None:
    y = tmp_path / "s.yaml"
    y.write_text("ibkr:\n  read_only_enforced: false\n")
    with pytest.raises(ConfigurationError):
        load_settings(y, tmp_path / "x.env")


def test_unknown_key_rejected(tmp_path: Path) -> None:
    y = tmp_path / "s.yaml"
    y.write_text("ibkr:\n  bogus: 1\n")
    with pytest.raises(ConfigurationError):
        load_settings(y, tmp_path / "x.env")


def test_get_settings_cached() -> None:
    assert get_settings() is get_settings()


def test_configure_logging_writes_file(tmp_path: Path) -> None:
    path = configure_logging(Settings(), log_dir=tmp_path)
    configure_logging(Settings(), log_dir=tmp_path)  # idempotent
    logging.getLogger("test").info("hello-phase-1")
    for h in logging.getLogger().handlers:
        h.flush()
    assert path == tmp_path / "analyzer.log"
    assert "hello-phase-1" in path.read_text(encoding="utf-8")
    managed = [h for h in logging.getLogger().handlers if h.get_name() == "analyzer"]
    assert len(managed) == 2
    for h in managed:
        logging.getLogger().removeHandler(h)
        h.close()
