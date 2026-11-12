/* IBKR Options Analyzer dashboard — Entry point: boots the dashboard once the DOM is ready. */

import {
  startCollectorPolling,
} from "./collector.js";
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
  bindEvents,
} from "./events.js";
import {
  initChartResizeHandling,
} from "./grid_layout.js";

// ------------------------------------------------------------------ Init
function init() {
  updateThemeToggleLabel();
  restoreWallsPreferences();
  syncStrategyAutoPopulateUI();
  applyTickerPullPrefsToControls(state.ticker);
  state.tableSort = readTableSortPref();
  syncTableHeaderSortUI();
  applyMdTypeBadge(configuredDefaultMdType());
  applyPanelVisibility(readPanelVisibility(), { skipRender: true });
  bindEvents();
  initChartResizeHandling();
  fetchStatus();
  startCollectorPolling();
  loadChain(state.ticker, "");
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
