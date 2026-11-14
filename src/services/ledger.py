"""Application service for the Hybrid IBKR Options Trade Ledger."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from src.analytics.ledger import (
    LedgerAnalyticsReport,
    LedgerGroupBy,
    LedgerView,
    LedgerWindow,
    build_ledger_report,
)
from src.broker.client import IBKRBrokerClient, SleepFn
from src.broker.flex import (
    FlexWebServiceClient,
    parse_activity_csv,
    parse_flex_xml,
    select_flex_query_ids,
)
from src.broker.ledger_sync import (
    MAX_MARK_CONTRACTS,
    fetch_gateway_ledger,
    refresh_open_option_marks,
)
from src.core.config import Settings, get_settings
from src.core.exceptions import (
    ConfigurationError,
    GatewayConnectionError,
    InvalidRequestError,
    RefreshBusyError,
)
from src.core.security import TickerValidator
from src.services.schemas import (
    MAX_LEDGER_IMPORT_BYTES,
    LedgerDashboardResponse,
    LedgerImportResponse,
    LedgerMTMRefreshResponse,
    LedgerStatusSummary,
    LedgerSyncMode,
    LedgerSyncResponse,
)
from src.storage.ledger_repository import LedgerRepository
from src.storage.ledger_schema import LedgerExecutionRow, LedgerPositionRow

logger = logging.getLogger(__name__)

MAX_IMPORT_BYTES = MAX_LEDGER_IMPORT_BYTES

_VALID_WINDOWS: dict[str, LedgerWindow] = {
    "1W": "1W",
    "1M": "1M",
    "3M": "3M",
    "YTD": "YTD",
    "1Y": "1Y",
    "ALL": "ALL",
}
_VALID_GROUP_BYS: dict[str, LedgerGroupBy] = {
    "campaigns": "campaigns",
    "strategy": "strategy",
    "ticker": "ticker",
    "executions": "executions",
}
_VALID_VIEWS: dict[str, LedgerView] = {
    "realized": "realized",
    "open_mtm": "open_mtm",
}
_VALID_SYNC_MODES: dict[str, LedgerSyncMode] = {
    "auto": "auto",
    "monthly": "monthly",
    "full": "full",
}


def _position_merge_key(pos: LedgerPositionRow) -> tuple[str, str, str, float, str]:
    strike_key = round(pos.strike, 6) if pos.strike is not None else 0.0
    return (
        pos.underlying,
        pos.asset_class,
        pos.expiry or "",
        strike_key,
        pos.right or "",
    )


def _merge_positions(
    flex_positions: list[LedgerPositionRow] | None,
    gw_positions: Sequence[LedgerPositionRow],
    *,
    gateway_synced: bool,
    existing_has_flex: bool,
) -> list[LedgerPositionRow] | None:
    """Merge Flex and live Gateway open positions without wiping Flex on empty Gateway."""
    if not gateway_synced or not gw_positions:
        if flex_positions is not None:
            return list(flex_positions)
        if gateway_synced and not existing_has_flex:
            return []
        return None

    merged: dict[tuple[str, str, str, float, str], LedgerPositionRow] = {}
    if flex_positions:
        for f_pos in flex_positions:
            merged[_position_merge_key(f_pos)] = f_pos

    for g_pos in gw_positions:
        key = _position_merge_key(g_pos)
        existing = merged.get(key)
        if existing is not None:
            updates: dict[str, float | None] = {}
            if g_pos.mark_price is None and existing.mark_price is not None:
                updates["mark_price"] = existing.mark_price
            if g_pos.market_value is None and existing.market_value is not None:
                updates["market_value"] = existing.market_value
            if g_pos.unrealized_pnl is None and existing.unrealized_pnl is not None:
                updates["unrealized_pnl"] = existing.unrealized_pnl
            merged[key] = g_pos.model_copy(update=updates) if updates else g_pos
        else:
            merged[key] = g_pos

    return list(merged.values())


def _has_valid_mark_spec(pos: LedgerPositionRow) -> bool:
    if pos.conid is None or pos.conid <= 0:
        return False
    if pos.asset_class == "OPT":
        return (
            pos.expiry is not None
            and pos.strike is not None
            and pos.strike > 0.0
            and pos.right in ("C", "P")
        )
    return bool(pos.underlying)


CorridorStopper = Callable[[], Awaitable[int]]
RefreshAcquireFn = Callable[[str, float], Awaitable[bool]]
RefreshReleaseFn = Callable[[], None]


class LedgerService:
    """Coordinates Flex Web sync, manual XML/CSV import, Gateway sync, MTM, and analytics."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        repo: LedgerRepository | None = None,
        broker_client: IBKRBrokerClient | None = None,
        flex_client: FlexWebServiceClient | None = None,
        now_fn: Callable[[], datetime] | None = None,
        sleep: SleepFn = asyncio.sleep,
        corridor_stopper: CorridorStopper | None = None,
        refresh_acquire: RefreshAcquireFn | None = None,
        refresh_release: RefreshReleaseFn | None = None,
        refresh_owner_getter: Callable[[], str | None] | None = None,
        on_ledger_changed: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repo = repo or LedgerRepository(self.settings.ledger.ledger_dir)
        self.broker_client = broker_client or IBKRBrokerClient(self.settings)
        self.sleep = sleep
        self.flex_client = flex_client or FlexWebServiceClient(
            token=self.settings.ledger.flex_token,
            sleep=sleep,
        )
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.corridor_stopper = corridor_stopper
        self._refresh_acquire = refresh_acquire
        self._refresh_release = refresh_release
        self._refresh_owner_getter = refresh_owner_getter
        self._on_ledger_changed = on_ledger_changed
        self._live_marks: dict[int, float] = {}

    def _notify_ledger_changed(self) -> None:
        if self._on_ledger_changed is None:
            return
        try:
            self._on_ledger_changed()
        except Exception as exc:
            logger.warning("on_ledger_changed callback failed: %s", exc)

    async def _acquire_ib_lock(self, owner: str) -> bool:
        if self._refresh_acquire is None:
            return False
        if not await self._refresh_acquire(owner, 0.0):
            holder = (
                self._refresh_owner_getter() if self._refresh_owner_getter is not None else None
            ) or "another operation"
            raise RefreshBusyError(
                f"Another IBKR refresh ({holder}) is currently in progress; retry shortly.",
                owner=holder,
                retry_after_sec=10,
            )
        return True

    def _utc_now_iso(self) -> str:
        dt = self.now_fn()
        dt_utc = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    def ensure_layout(self) -> None:
        """Create `data/ledger/` and `imports/` directories idempotently."""
        self.repo.ensure_layout()

    def get_ledger_status(self) -> LedgerStatusSummary:
        """Return safe non-secret configuration and sync status flags."""
        cfg = self.settings.ledger
        state = self.repo.load_sync_state()
        has_any_query = bool(
            cfg.flex_activity_query_id or cfg.flex_monthly_query_id or cfg.flex_trade_conf_query_id
        )
        rec_raw = str(state.get("recommended_incremental_mode", "full")).lower()
        recommended: Literal["monthly", "full"] = "monthly" if rec_raw == "monthly" else "full"
        return LedgerStatusSummary(
            flex_configured=bool(cfg.flex_token and has_any_query),
            flex_monthly_configured=bool(cfg.flex_monthly_query_id),
            flex_trade_conf_configured=bool(cfg.flex_trade_conf_query_id),
            gateway_sync_enabled=cfg.gateway_sync_enabled,
            recommended_sync_mode=recommended,
            last_synced_at=state.get("last_sync_at"),
            last_sync_mode=state.get("last_sync_mode"),
            execution_count=int(state.get("total_executions", 0)),
            open_position_count=int(state.get("open_positions_count", 0)),
        )

    def get_ledger_dashboard(
        self,
        *,
        window: str = "1M",
        ticker: str = "ALL",
        group_by: str = "campaigns",
        view: str = "realized",
        include_stock_gain: bool = False,
    ) -> LedgerDashboardResponse:
        """Validate filter controls, run pure ledger analytics, and attach sync status."""
        win_norm = _VALID_WINDOWS.get(window.strip().upper())
        if win_norm is None:
            raise InvalidRequestError(
                f"Invalid ledger window {window!r}; expected one of {sorted(_VALID_WINDOWS)}"
            )
        gb_norm = _VALID_GROUP_BYS.get(group_by.strip().lower())
        if gb_norm is None:
            raise InvalidRequestError(
                f"Invalid group_by {group_by!r}; expected one of {sorted(_VALID_GROUP_BYS)}"
            )
        view_norm = _VALID_VIEWS.get(view.strip().lower())
        if view_norm is None:
            raise InvalidRequestError(
                f"Invalid ledger view {view!r}; expected one of {sorted(_VALID_VIEWS)}"
            )

        raw_ticker = ticker.strip().upper()
        ticker_norm = "ALL" if raw_ticker in ("", "ALL") else TickerValidator.validate(raw_ticker)

        executions_df = self.repo.load_executions()
        positions_df = self.repo.load_positions()
        report = build_ledger_report(
            executions_df,
            positions_df,
            window=win_norm,
            ticker=ticker_norm,
            group_by=gb_norm,
            view=view_norm,
            include_stock_gain=include_stock_gain,
            roll_max_gap_hours=self.settings.ledger.roll_max_gap_hours,
            live_marks=dict(self._live_marks),
            as_of=self.now_fn(),
        )
        return LedgerDashboardResponse(report=report, status=self.get_ledger_status())

    def build_full_open_report(self) -> LedgerAnalyticsReport:
        """Build an unfiltered `open_mtm` report across all tickers and campaigns."""
        return build_ledger_report(
            self.repo.load_executions(),
            self.repo.load_positions(),
            window="ALL",
            ticker="ALL",
            group_by="campaigns",
            view="open_mtm",
            include_stock_gain=True,
            roll_max_gap_hours=self.settings.ledger.roll_max_gap_hours,
            live_marks=dict(self._live_marks) if self._live_marks else None,
            as_of=self.now_fn(),
        )

    async def sync_ledger(
        self,
        *,
        mode: str = "auto",
        include_gateway: bool | None = None,
    ) -> LedgerSyncResponse:
        """Sync trades/positions from IBKR Flex Web Service and/or opt-in Gateway."""
        resolved_mode = _VALID_SYNC_MODES.get(mode.strip().lower())
        if resolved_mode is None:
            raise InvalidRequestError(
                f"Invalid sync mode {mode!r}; expected one of {sorted(_VALID_SYNC_MODES)}"
            )

        cfg = self.settings.ledger
        if resolved_mode == "monthly" and not cfg.flex_monthly_query_id:
            raise ConfigurationError(
                "IBKR_FLEX_MONTHLY_QUERY_ID is not configured; configure "
                "IBKR_FLEX_MONTHLY_QUERY_ID for incremental monthly sync or use 'full' (365d) mode."
            )
        if include_gateway is True and not cfg.gateway_sync_enabled:
            raise ConfigurationError(
                "Gateway ledger sync is disabled; set ENABLE_GATEWAY_LEDGER_SYNC=true to opt in."
            )

        has_any_query = bool(
            cfg.flex_activity_query_id or cfg.flex_monthly_query_id or cfg.flex_trade_conf_query_id
        )
        can_run_flex = bool(cfg.flex_token and has_any_query)
        run_gateway = cfg.gateway_sync_enabled and include_gateway is not False

        if not can_run_flex and not run_gateway:
            raise ConfigurationError(
                "Ledger sync is not configured: set IBKR_FLEX_TOKEN + "
                "IBKR_FLEX_ACTIVITY_QUERY_ID, or opt in via ENABLE_GATEWAY_LEDGER_SYNC=true."
            )

        queries_run: list[str] = []
        notices: list[str] = []
        flex_execs: list[LedgerExecutionRow] = []
        flex_positions: list[LedgerPositionRow] | None = None
        flex_primary_label = ""
        gw_execs: list[LedgerExecutionRow] = []
        gw_positions: list[LedgerPositionRow] = []
        gateway_synced = False
        gateway_note: str | None = None

        acquired = await self._acquire_ib_lock("ledger:sync") if run_gateway else False
        try:
            if can_run_flex:
                has_history = self.repo.has_existing_history()
                query_plan = select_flex_query_ids(
                    cfg, mode=resolved_mode, has_existing_history=has_history
                )
                for query_id, source in query_plan:
                    raw_bytes = await self.flex_client.fetch_statement(
                        query_id, token=cfg.flex_token
                    )
                    if raw_bytes.lstrip().startswith(b"<"):
                        q_execs, q_positions = parse_flex_xml(raw_bytes, default_source=source)
                    else:
                        q_execs, q_positions = parse_activity_csv(raw_bytes, default_source=source)
                    queries_run.append(f"{source}:{query_id}")
                    flex_execs.extend(q_execs)
                    if source in ("FLEX_ACTIVITY_365", "FLEX_MONTHLY"):
                        flex_positions = list(q_positions)
                        flex_primary_label = "monthly" if source == "FLEX_MONTHLY" else "full"
                    elif q_positions and flex_positions is None:
                        flex_positions = list(q_positions)
                if not flex_primary_label and queries_run:
                    flex_primary_label = "trade_conf"

            if run_gateway:
                if self.corridor_stopper is not None:
                    stopped_lines = await self.corridor_stopper()
                    if stopped_lines > 0:
                        notices.append(
                            f"Stopped {stopped_lines} active Wall Corridor subscription(s) "
                            "before Gateway ledger sync."
                        )
                try:
                    gw_execs, gw_positions = await fetch_gateway_ledger(
                        self.broker_client,
                        include_stock=cfg.gateway_include_stock,
                        clock=self.now_fn,
                    )
                    gateway_synced = True
                    gateway_note = (
                        f"Gateway synced {len(gw_execs)} fills and {len(gw_positions)} positions."
                    )
                except GatewayConnectionError as exc:
                    if not can_run_flex:
                        raise
                    gateway_note = f"Gateway offline ({exc}); Flex results preserved."
                    notices.append(gateway_note)
        finally:
            if acquired and self._refresh_release is not None:
                self._refresh_release()

        # Upsert Flex first, then Gateway so Gateway fills never overwrite settled Flex rows
        # and intra-run Flex+Gateway duplicates settle cleanly without double-counting.
        combined_execs = [*flex_execs, *gw_execs]
        inserted, updated = self.repo.upsert_executions(combined_execs)

        existing_pos_df = self.repo.load_positions()
        existing_has_flex = (
            existing_pos_df.height > 0
            and existing_pos_df.filter(existing_pos_df["source"].str.starts_with("FLEX_")).height
            > 0
        )
        final_positions = _merge_positions(
            flex_positions,
            gw_positions,
            gateway_synced=gateway_synced,
            existing_has_flex=existing_has_flex,
        )
        if final_positions is not None:
            open_count = self.repo.replace_positions(final_positions)
        else:
            open_count = existing_pos_df.height

        if can_run_flex and gateway_synced:
            mode_used = f"{flex_primary_label}+gateway"
        elif can_run_flex:
            mode_used = flex_primary_label
        else:
            mode_used = "gateway"

        synced_at = self._utc_now_iso()
        self.repo.save_sync_state(last_sync_at=synced_at, last_sync_mode=mode_used)
        self._notify_ledger_changed()
        total_executions = self.repo.load_executions().height

        return LedgerSyncResponse(
            mode_used=mode_used,
            queries_run=queries_run,
            gateway_synced=gateway_synced,
            gateway_note=gateway_note,
            executions_inserted=inserted,
            executions_updated=updated,
            total_executions=total_executions,
            open_positions_count=open_count,
            synced_at=synced_at,
            notices=notices,
        )

    def import_ledger_file(self, filename: str, raw_bytes: bytes) -> LedgerImportResponse:
        """Import a manual IBKR Flex XML, Flex CSV, or Activity Statement CSV upload."""
        stripped_input = (filename or "").strip()
        raw_name = stripped_input.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if stripped_input and not raw_name:
            raise InvalidRequestError("Unsupported ledger file extension; expected .xml or .csv")
        safe_name = raw_name or "import.csv"
        if Path(safe_name).suffix.lower() not in {".xml", ".csv"}:
            raise InvalidRequestError("Unsupported ledger file extension; expected .xml or .csv")
        if not raw_bytes or not raw_bytes.strip():
            raise InvalidRequestError("Uploaded ledger file is empty.")
        if len(raw_bytes) > MAX_IMPORT_BYTES:
            raise InvalidRequestError("Uploaded ledger file exceeds 20 MB limit.")

        is_xml = raw_bytes.lstrip().startswith(b"<") or safe_name.lower().endswith(".xml")
        file_format: Literal["XML", "CSV"]
        try:
            if is_xml:
                file_format = "XML"
                execs, positions = parse_flex_xml(raw_bytes, default_source="FLEX_IMPORT")
            else:
                file_format = "CSV"
                execs, positions = parse_activity_csv(raw_bytes, default_source="FLEX_IMPORT")
        except UnicodeDecodeError as exc:
            raise InvalidRequestError("Uploaded ledger file is not valid UTF-8 text.") from exc

        notices: list[str] = []
        if not execs and not positions:
            notices.append(f"No OPT or STK trades/positions found in {safe_name}.")

        inserted, updated = self.repo.upsert_executions(execs)
        positions_imported = 0
        if positions:
            positions_imported = self.repo.replace_positions(positions)

        self.repo.save_sync_state(
            last_sync_at=self._utc_now_iso(),
            last_sync_mode=f"import:{file_format.lower()}",
        )
        self._notify_ledger_changed()
        total_executions = self.repo.load_executions().height
        return LedgerImportResponse(
            filename=safe_name,
            file_format=file_format,
            executions_inserted=inserted,
            executions_updated=updated,
            open_positions_imported=positions_imported,
            total_executions=total_executions,
            notices=notices,
        )

    async def refresh_open_mtm(self) -> LedgerMTMRefreshResponse:
        """Refresh live mark prices for open ledger positions (read-only, in-memory only)."""
        positions_df = self.repo.load_positions()
        if positions_df.is_empty():
            return LedgerMTMRefreshResponse(
                refreshed_count=0,
                skipped_count=0,
                marks_by_conid={},
                updated_at=self._utc_now_iso(),
                notices=["No open positions available to refresh."],
            )

        pos_models = [LedgerPositionRow.model_validate(r) for r in positions_df.to_dicts()]
        all_valid_conids: list[int] = []
        seen: set[int] = set()
        for pos in pos_models:
            if _has_valid_mark_spec(pos) and pos.conid is not None and pos.conid not in seen:
                seen.add(pos.conid)
                all_valid_conids.append(pos.conid)

        eligible_conids = all_valid_conids[:MAX_MARK_CONTRACTS]
        skipped_count = max(0, len(pos_models) - len(eligible_conids))
        notices: list[str] = []
        if len(all_valid_conids) > MAX_MARK_CONTRACTS:
            notices.append(f"Capped live MTM refresh to {MAX_MARK_CONTRACTS} contracts.")
        if not eligible_conids:
            notices.append("No open positions with valid IBKR conId available for mark refresh.")
            return LedgerMTMRefreshResponse(
                refreshed_count=0,
                skipped_count=skipped_count,
                marks_by_conid={},
                updated_at=self._utc_now_iso(),
                notices=notices,
            )

        acquired = await self._acquire_ib_lock("ledger:mtm")
        try:
            if self.corridor_stopper is not None:
                stopped_lines = await self.corridor_stopper()
                if stopped_lines > 0:
                    notices.append(
                        f"Stopped {stopped_lines} active Wall Corridor subscription(s) before "
                        "live MTM refresh to stay under IBKR's 100-line market data ceiling."
                    )
            marks = await refresh_open_option_marks(
                self.broker_client, pos_models, sleep=self.sleep
            )
        finally:
            if acquired and self._refresh_release is not None:
                self._refresh_release()
        self._live_marks.update(marks)
        unquoted = len(eligible_conids) - len(marks)
        if unquoted > 0:
            notices.append(f"{unquoted} contract(s) did not return a live/delayed quote.")

        return LedgerMTMRefreshResponse(
            refreshed_count=len(marks),
            skipped_count=skipped_count,
            marks_by_conid=marks,
            updated_at=self._utc_now_iso(),
            notices=notices,
        )
