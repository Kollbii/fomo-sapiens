from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core.config import (
    DEFAULT_YAML_PATH,
    PROJECT_ROOT,
    CorridorSettings,
    LedgerSettings,
    PaperTrackerSettings,
    Settings,
    StorageSettings,
    configure_logging,
    get_settings,
    load_settings,
    resolve_project_path,
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
    "IBKR_FLEX_TOKEN",
    "IBKR_FLEX_ACTIVITY_QUERY_ID",
    "IBKR_FLEX_MONTHLY_QUERY_ID",
    "IBKR_FLEX_TRADE_CONF_QUERY_ID",
    "ENABLE_GATEWAY_LEDGER_SYNC",
    "GATEWAY_LEDGER_INCLUDE_STOCK",
    "LEDGER_ROLL_MAX_GAP_HOURS",
    "LEDGER_DIR",
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
    assert s.corridor.default_window_strikes == 10
    assert s.corridor.default_max_contracts == 88
    assert s.corridor.hard_max_contracts == 90
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


def test_ledger_settings_defaults_env_overrides_and_secret_redaction(tmp_path: Path) -> None:
    defaults = load_settings(DEFAULT_YAML_PATH, tmp_path / "missing.env")
    assert defaults.ledger.flex_token is None
    assert defaults.ledger.flex_activity_query_id is None
    assert defaults.ledger.flex_monthly_query_id is None
    assert defaults.ledger.flex_trade_conf_query_id is None
    assert defaults.ledger.gateway_sync_enabled is False
    assert defaults.ledger.gateway_include_stock is True
    assert defaults.ledger.roll_max_gap_hours == 24.0
    assert defaults.ledger.ledger_dir == "data/ledger"

    # Blank placeholders in `.env` cleanly resolve to None / defaults
    blank_env = tmp_path / "blank.env"
    blank_env.write_text(
        "IBKR_FLEX_TOKEN=\n"
        "IBKR_FLEX_ACTIVITY_QUERY_ID=   \n"
        "IBKR_FLEX_MONTHLY_QUERY_ID=\n"
        "IBKR_FLEX_TRADE_CONF_QUERY_ID=\n"
        "ENABLE_GATEWAY_LEDGER_SYNC=\n"
        "LEDGER_DIR=\n",
        encoding="utf-8",
    )
    blank_loaded = load_settings(DEFAULT_YAML_PATH, blank_env)
    assert blank_loaded.ledger == LedgerSettings()

    # Full overrides (including monthly placeholder & opt-in Gateway sync)
    env = tmp_path / "ledger.env"
    env.write_text(
        "IBKR_FLEX_TOKEN=super-secret-flex-token\n"
        "IBKR_FLEX_ACTIVITY_QUERY_ID=111222\n"
        "IBKR_FLEX_MONTHLY_QUERY_ID=333444\n"
        "IBKR_FLEX_TRADE_CONF_QUERY_ID=555666\n"
        "ENABLE_GATEWAY_LEDGER_SYNC=true\n"
        "GATEWAY_LEDGER_INCLUDE_STOCK=false\n"
        "LEDGER_ROLL_MAX_GAP_HOURS=36.5\n"
        "LEDGER_DIR=data/custom_ledger\n",
        encoding="utf-8",
    )
    s = load_settings(DEFAULT_YAML_PATH, env)
    assert s.ledger.flex_token == "super-secret-flex-token"
    assert s.ledger.flex_activity_query_id == "111222"
    assert s.ledger.flex_monthly_query_id == "333444"
    assert s.ledger.flex_trade_conf_query_id == "555666"
    assert s.ledger.gateway_sync_enabled is True
    assert s.ledger.gateway_include_stock is False
    assert s.ledger.roll_max_gap_hours == 36.5
    assert s.ledger.ledger_dir == "data/custom_ledger"

    # Never expose raw Flex token via repr()
    assert "super-secret-flex-token" not in repr(s.ledger)
    assert "super-secret-flex-token" not in repr(s)

    # Frozen immutability & validation bounds
    with pytest.raises(ValidationError):
        s.ledger.gateway_sync_enabled = False

    bad_env = tmp_path / "bad_ledger.env"
    bad_env.write_text("LEDGER_ROLL_MAX_GAP_HOURS=0\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_settings(DEFAULT_YAML_PATH, bad_env)


def test_paper_tracker_settings_and_resolve_project_path(tmp_path: Path) -> None:
    defaults = load_settings(DEFAULT_YAML_PATH, tmp_path / "missing.env")
    assert defaults.tracker == PaperTrackerSettings(
        max_active_trades=120, tracked_dir="data/tracked"
    )
    assert resolve_project_path("data/tracked") == (PROJECT_ROOT / "data/tracked").resolve()
    assert resolve_project_path(str(tmp_path)) == tmp_path.resolve()

    with pytest.raises(ValidationError, match=r"tracker\.tracked_dir must be a folder inside"):
        Settings(tracker=PaperTrackerSettings(tracked_dir="outside/tracked"))

    with pytest.raises(ValidationError, match=r"data/snapshots"):
        Settings(
            storage=StorageSettings(),
            tracker=PaperTrackerSettings(tracked_dir="data/snapshots/sub"),
        )

    with pytest.raises(ValidationError):
        PaperTrackerSettings(max_active_trades=0)
    with pytest.raises(ValidationError):
        PaperTrackerSettings(max_active_trades=201)


def test_short_dte_backtest_options_chain_settings(tmp_path: Path) -> None:
    from src.core.config import OptionsChainSettings

    s = load_settings(DEFAULT_YAML_PATH, tmp_path / "missing.env")
    assert s.options_chain.dte_min == 30
    assert s.options_chain.dte_max == 120
    assert s.options_chain.include_short_dte_for_backtest is True
    assert s.options_chain.short_dte_min == 0
    assert s.options_chain.short_dte_max == 29

    valid = OptionsChainSettings(
        short_dte_min=0, short_dte_max=30, include_short_dte_for_backtest=False
    )
    assert valid.short_dte_max == 30
    assert valid.include_short_dte_for_backtest is False

    with pytest.raises(ValidationError, match="short_dte_min"):
        OptionsChainSettings(short_dte_min=15, short_dte_max=14)
    with pytest.raises(ValidationError):
        OptionsChainSettings(short_dte_min=-1)
    with pytest.raises(ValidationError):
        OptionsChainSettings(short_dte_max=31)
    with pytest.raises(ValidationError, match="dte_min"):
        OptionsChainSettings(dte_min=90, dte_max=60)


def test_corridor_settings_bounds_and_cap_validator() -> None:
    cfg = CorridorSettings(
        default_window_strikes=15,
        default_max_contracts=88,
        hard_max_contracts=90,
    )
    assert cfg.default_window_strikes == 15
    assert cfg.default_max_contracts == 88
    assert cfg.hard_max_contracts == 90

    with pytest.raises(ValidationError, match="default_max_contracts"):
        CorridorSettings(default_max_contracts=88, hard_max_contracts=80)
    with pytest.raises(ValidationError):
        CorridorSettings(default_window_strikes=16)
    with pytest.raises(ValidationError):
        CorridorSettings(hard_max_contracts=91)
