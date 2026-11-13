/* IBKR Options Analyzer dashboard — Entry point: boots the dashboard once the DOM is ready. */

import {
  state,
} from "./state.js";
import {
  applyMdTypeBadge,
  applyTickerPullPrefsToControls,
  configuredDefaultMdType,
  syncStrategyAutoPopulateUI,
  updateThemeToggleLabel,
} from "./helpers.js";
import {
  restoreWallsPreferences,
} from "./charts.js";
import {
  applyPanelVisibility,
  readPanelVisibility,
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

// ------------------------------------------------------------------ Init
function init() {
  updateThemeToggleLabel();
  restoreWallsPreferences();
  restoreLedgerPreferences();
  syncStrategyAutoPopulateUI();
  applyTickerPullPrefsToControls(state.ticker);
  state.tableSort = readTableSortPref();
  syncTableHeaderSortUI();
  applyMdTypeBadge(configuredDefaultMdType());
  applyPanelVisibility(readPanelVisibility(), { skipRender: true });
  bindEvents();
  initChartResizeHandling();
  fetchStatus();
  loadChain(state.ticker, "");
  loadLedger();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
