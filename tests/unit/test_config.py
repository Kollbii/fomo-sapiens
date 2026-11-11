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
    "FALLBACK_MARKET_DATA_TYPE",
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
    assert s.options_chain.dte_min == 30
    assert s.options_chain.dte_max == 120
    assert s.options_chain.default_strike_step is None
    assert s.options_chain.prefetch_exact_strikes is True
    assert s.options_chain.contract_details_timeout_sec == 5.0
    assert s.options_chain.contract_details_cooldown_sec == 0.10
    assert s.options_chain.contract_details_cache_ttl_sec == 900.0
    assert s.corridor.default_window_strikes == 5
    assert s.corridor.default_max_contracts == 56
    assert s.corridor.hard_max_contracts == 60
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


def test_env_market_data_reconciliation_and_mismatch(tmp_path: Path) -> None:
    # 1. Setting only DEFAULT_MARKET_DATA_TYPE=1 auto-derives LIVE mode & Type 2 fallback
    env_live_type = tmp_path / "live_type.env"
    env_live_type.write_text("DEFAULT_MARKET_DATA_TYPE=1\n", encoding="utf-8")
    s1 = load_settings(DEFAULT_YAML_PATH, env_live_type)
    assert s1.ibkr.market_data_mode == "LIVE"
    assert s1.ibkr.default_market_data_type == 1
    assert s1.ibkr.fallback_market_data_type == 2

    # 2. Setting only MARKET_DATA_MODE=LIVE aligns defaults to (1, 2) instead of YAML (3, 4)
    env_live_mode = tmp_path / "live_mode.env"
    env_live_mode.write_text("MARKET_DATA_MODE=LIVE\n", encoding="utf-8")
    s2 = load_settings(DEFAULT_YAML_PATH, env_live_mode)
    assert s2.ibkr.market_data_mode == "LIVE"
    assert s2.ibkr.default_market_data_type == 1
    assert s2.ibkr.fallback_market_data_type == 2

    # 3. Explicit FALLBACK_MARKET_DATA_TYPE override works alongside DEFAULT_MARKET_DATA_TYPE=1
    env_custom_fb = tmp_path / "custom_fb.env"
    env_custom_fb.write_text(
        "DEFAULT_MARKET_DATA_TYPE=1\nFALLBACK_MARKET_DATA_TYPE=4\n", encoding="utf-8"
    )
    s3 = load_settings(DEFAULT_YAML_PATH, env_custom_fb)
    assert s3.ibkr.market_data_mode == "LIVE"
    assert s3.ibkr.default_market_data_type == 1
    assert s3.ibkr.fallback_market_data_type == 4

    # 4. Contradictory explicit mode + type raises ConfigurationError
    env_bad = tmp_path / "bad.env"
    env_bad.write_text("MARKET_DATA_MODE=DELAYED\nDEFAULT_MARKET_DATA_TYPE=1\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_settings(DEFAULT_YAML_PATH, env_bad)
