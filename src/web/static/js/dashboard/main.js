/* IBKR Options Analyzer dashboard — Entry point: boots the dashboard once the DOM is ready. */

import {
  state,
} from "./state.js";
import {
  applyMdTypeBadge,
  applyTickerPullPrefsToControls,
  configuredDefaultMdType,
  syncPullScopeSummary,
  syncStrategyAutoPopulateUI,
  updateThemeToggleLabel,
} from "./helpers.js";
import {
  restoreWallsPreferences,
} from "./charts.js";
import {
  applyPanelVisibility,
  applyWorkspaceMode,
  readPanelVisibility,
  readWorkspaceMode,
} from "./panels.js";
import {
  readTableSortPref,
  syncTableHeaderSortUI,
} from "./chain_table.js";
import {
  fetchStatus,
  loadChain,
} from "./network.js";
import {
  loadLedger,
  restoreLedgerPreferences,
} from "./ledger.js";
import {
  bindEvents,
} from "./events.js";
import {
  initChartResizeHandling,
} from "./grid_layout.js";
import {
  initBacktestPanel,
} from "./backtest.js";
import {
  initWatchlist,
} from "./watchlist.js";
import {
  initScreener,
} from "./screener.js";

// ------------------------------------------------------------------ Init
function init() {
  updateThemeToggleLabel();
  restoreWallsPreferences();
  restoreLedgerPreferences();
  syncStrategyAutoPopulateUI();
  applyTickerPullPrefsToControls(state.ticker);
  syncPullScopeSummary();
  state.tableSort = readTableSortPref();
  syncTableHeaderSortUI();
  applyMdTypeBadge(configuredDefaultMdType());
  state.workspaceMode = readWorkspaceMode();
  applyPanelVisibility(readPanelVisibility(), { skipRender: true });
  applyWorkspaceMode(state.workspaceMode, { persist: false, skipRender: true });
  bindEvents();
  initChartResizeHandling();
  fetchStatus();
  loadChain(state.ticker, "");
  loadLedger();
  initBacktestPanel();
  initWatchlist();
  initScreener();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
