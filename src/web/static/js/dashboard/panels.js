/* IBKR Options Analyzer dashboard — Workspace modes, Panel visibility bar (`.panel-toggle-pill`), and persistence. */

import {
  DEFAULT_PANEL_VISIBILITY,
  DEFAULT_WORKSPACE_MODE,
  PANEL_VISIBILITY_KEY,
  WORKSPACE_MODE_KEY,
  WORKSPACE_PANEL_SETS,
  cloneDefaultPanelVisibility,
  dom,
  state,
} from "./state.js";
import {
  isStrategyAutoPopulateEnabled,
  readJsonPref,
  readPref,
  removePref,
  themeVars,
  writeJsonPref,
  writePref,
} from "./helpers.js";
import {
  purgeCharts,
  renderAnomaliesChart,
  renderGreeksChart,
  renderIvSmileChart,
  renderWallsChart,
} from "./charts.js";
import {
  maybeAutoPopulateStrategy,
  renderStrategyPayoffChart,
} from "./strategy_lab.js";
import {
  buildLocalCorridorPreview,
  renderCorridorChart,
  stopCorridorStream,
} from "./corridor.js";
import {
  renderLedgerChart,
} from "./ledger.js";
import {
  renderBacktestChart,
} from "./backtest_charts.js";

// ------------------------------------------------------------------ Panel Visibility
export function readPanelVisibility() {
  var next = cloneDefaultPanelVisibility();
  var parsed = readJsonPref(PANEL_VISIBILITY_KEY, null);
  if (!parsed || typeof parsed !== "object") return next;
  Object.keys(DEFAULT_PANEL_VISIBILITY).forEach(function (panelId) {
    if (typeof parsed[panelId] === "boolean") {
      next[panelId] = parsed[panelId];
    }
  });
  return next;
}

function writePanelVisibility(vis) {
  writeJsonPref(PANEL_VISIBILITY_KEY, vis);
}

function clearPanelVisibilityOverrides() {
  removePref(PANEL_VISIBILITY_KEY);
}

export var VALID_WORKSPACE_MODES = ["desk", "portfolio", "backtest", "all"];

export function normalizeWorkspaceMode(rawMode) {
  var val = String(rawMode || "").trim().toLowerCase();
  return VALID_WORKSPACE_MODES.indexOf(val) >= 0 ? val : DEFAULT_WORKSPACE_MODE;
}

export function isPanelInWorkspace(panelId, mode) {
  var m = normalizeWorkspaceMode(mode);
  var allowed = WORKSPACE_PANEL_SETS[m] || WORKSPACE_PANEL_SETS[DEFAULT_WORKSPACE_MODE];
  return allowed.indexOf(panelId) >= 0;
}

export function workspaceForPanel(panelId) {
  if (panelId === "trade_ledger") return "portfolio";
  if (panelId === "backtest") return "backtest";
  return "desk";
}

export function readWorkspaceMode() {
  return normalizeWorkspaceMode(readPref(WORKSPACE_MODE_KEY, null));
}

export function writeWorkspaceMode(mode) {
  writePref(WORKSPACE_MODE_KEY, normalizeWorkspaceMode(mode));
}

export function isPanelVisible(panelId) {
  var mode = normalizeWorkspaceMode(state.workspaceMode);
  if (!isPanelInWorkspace(panelId, mode)) {
    return false;
  }
  if (mode === "portfolio" || mode === "backtest") {
    return true;
  }
  if (!state.panelVisibility || typeof state.panelVisibility[panelId] !== "boolean") {
    return Boolean(DEFAULT_PANEL_VISIBILITY[panelId]);
  }
  return state.panelVisibility[panelId];
}

function syncWorkspaceTabsUI(mode) {
  if (typeof document !== "undefined" && document.documentElement) {
    document.documentElement.setAttribute("data-workspace-mode", mode);
  }
  var buttons = document.querySelectorAll("[data-workspace-tab]");
  for (var i = 0; i < buttons.length; i++) {
    var btn = buttons[i];
    var tabMode = btn.getAttribute("data-workspace-tab");
    var active = tabMode === mode;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  }
}

function syncPanelVisibilityUI(vis) {
  var mode = normalizeWorkspaceMode(state.workspaceMode);
  syncWorkspaceTabsUI(mode);
  var barEl = dom.panelVisibilityControls || document.getElementById("panel-visibility-controls");
  if (barEl) {
    barEl.hidden = mode === "portfolio" || mode === "backtest";
  }
  var buttons = document.querySelectorAll(
    "#panel-visibility-controls [data-panel-target]"
  );
  for (var i = 0; i < buttons.length; i++) {
    var btn = buttons[i];
    var target = btn.getAttribute("data-panel-target");
    if (!target) continue;
    btn.hidden = !isPanelInWorkspace(target, mode);
    var on = Boolean(vis[target]);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.classList.toggle("active", on);
  }
  var cards = document.querySelectorAll(".chart-grid .panel-card[data-panel-id]");
  for (var j = 0; j < cards.length; j++) {
    var card = cards[j];
    var pid = card.getAttribute("data-panel-id");
    if (!pid) continue;
    card.classList.toggle("is-hidden", !isPanelVisible(pid));
  }
}

function renderPanelChartById(panelId, data, t) {
  if (!data || !isPanelVisible(panelId)) return;
  var theme = t || themeVars();
  if (panelId === "walls") renderWallsChart(data, theme);
  else if (panelId === "iv_smile") renderIvSmileChart(data, theme);
  else if (panelId === "anomalies") renderAnomaliesChart(data, theme);
  else if (panelId === "greeks") renderGreeksChart(data, theme);
  else if (panelId === "corridor") {
    var preview = state.corridorFrame || buildLocalCorridorPreview(data);
    if (preview) renderCorridorChart(preview, theme);
  } else if (panelId === "strategy") {
    if (state.strategyEvaluation) {
      renderStrategyPayoffChart(state.strategyEvaluation, theme);
    } else if (isStrategyAutoPopulateEnabled() && data) {
      maybeAutoPopulateStrategy(data);
    }
  }
}

export function applyPanelVisibility(nextVis, options) {
  var opts = options || {};
  var normalized = cloneDefaultPanelVisibility();
  Object.keys(DEFAULT_PANEL_VISIBILITY).forEach(function (pid) {
    normalized[pid] =
      typeof nextVis[pid] === "boolean"
        ? nextVis[pid]
        : DEFAULT_PANEL_VISIBILITY[pid];
  });

  if (!normalized.corridor && state.corridorStreaming) {
    stopCorridorStream({ silent: true });
  }

  var revealed = [];
  Object.keys(DEFAULT_PANEL_VISIBILITY).forEach(function (pid) {
    if (!normalized[pid]) return;
    var card = document.querySelector(
      '.chart-grid .panel-card[data-panel-id="' + pid + '"]'
    );
    var wasHidden = card
      ? card.classList.contains("is-hidden")
      : !isPanelVisible(pid);
    if (wasHidden) {
      revealed.push(pid);
    }
  });

  var wasBestOption = isPanelVisible("best_option");
  state.panelVisibility = normalized;
  var nowBestOption = isPanelVisible("best_option");
  syncPanelVisibilityUI(normalized);
  if (wasBestOption !== nowBestOption) {
    // Decoupled signal (no import cycle): the screener stops/resumes its live stream.
    document.dispatchEvent(
      new CustomEvent("ibkr:panel-visibility", {
        detail: { panelId: "best_option", visible: nowBestOption },
      })
    );
  }

  if (opts.clearOverrides) {
    clearPanelVisibilityOverrides();
  } else if (opts.persist) {
    writePanelVisibility(normalized);
  }

  if (revealed.length && !opts.skipRender) {
    scheduleRevealedPanelRenders(revealed);
  }
}

function scheduleRevealedPanelRenders(revealed) {
  if (!revealed || !revealed.length) return;
  window.requestAnimationFrame(function () {
    var t = themeVars();
    for (var i = 0; i < revealed.length; i++) {
      var pid = revealed[i];
      if (!isPanelVisible(pid)) continue;
      var card = document.querySelector(
        '.chart-grid .panel-card[data-panel-id="' + pid + '"]'
      );
      if (card && card.classList.contains("is-hidden")) continue;
      if (pid === "trade_ledger") {
        if (state.ledgerData && state.ledgerData.report) {
          renderLedgerChart(state.ledgerData.report, t);
        }
      } else if (pid === "backtest") {
        if (state.backtestResult && state.backtestResult.simulation) {
          renderBacktestChart(state.backtestResult, t);
        }
      } else if (state.chainData) {
        renderPanelChartById(pid, state.chainData, t);
      }
      var canvasEl = card ? card.querySelector(".chart-canvas") : null;
      if (canvasEl && window.Plotly && window.Plotly.Plots && canvasEl.data) {
        window.Plotly.Plots.resize(canvasEl);
      }
    }
  });
}

export function applyWorkspaceMode(nextMode, options) {
  var opts = options || {};
  var mode = normalizeWorkspaceMode(nextMode);
  var wasVisible = {};
  Object.keys(DEFAULT_PANEL_VISIBILITY).forEach(function (pid) {
    wasVisible[pid] = isPanelVisible(pid);
  });

  state.workspaceMode = mode;
  if (opts.persist !== false) {
    writeWorkspaceMode(mode);
  }

  var corridorNowVisible = isPanelVisible("corridor");
  if (wasVisible.corridor && !corridorNowVisible && state.corridorStreaming) {
    stopCorridorStream({ silent: true });
  }

  syncPanelVisibilityUI(state.panelVisibility);

  var bestNowVisible = isPanelVisible("best_option");
  if (wasVisible.best_option !== bestNowVisible) {
    document.dispatchEvent(
      new CustomEvent("ibkr:panel-visibility", {
        detail: { panelId: "best_option", visible: bestNowVisible },
      })
    );
  }

  if (!opts.skipRender) {
    var revealed = Object.keys(DEFAULT_PANEL_VISIBILITY).filter(function (pid) {
      return !wasVisible[pid] && isPanelVisible(pid);
    });
    scheduleRevealedPanelRenders(revealed);
  }
}

export function ensurePanelVisible(panelId) {
  if (!isPanelInWorkspace(panelId, state.workspaceMode)) {
    applyWorkspaceMode(workspaceForPanel(panelId), { persist: true });
  }
  if (!isPanelVisible(panelId)) {
    var nextVis = Object.assign({}, state.panelVisibility);
    nextVis[panelId] = true;
    applyPanelVisibility(nextVis, { persist: true });
  }
}

export function renderAllCharts(data) {
  if (!data) {
    purgeCharts();
    return;
  }
  var t = themeVars();
  if (isPanelVisible("walls")) renderWallsChart(data, t);
  if (isPanelVisible("iv_smile")) renderIvSmileChart(data, t);
  if (isPanelVisible("anomalies")) renderAnomaliesChart(data, t);
  if (isPanelVisible("greeks")) renderGreeksChart(data, t);
  if (isPanelVisible("corridor")) {
    var cFrame = state.corridorFrame || buildLocalCorridorPreview(data);
    if (cFrame) renderCorridorChart(cFrame, t);
  }
  if (isPanelVisible("strategy") && state.strategyEvaluation) {
    renderStrategyPayoffChart(state.strategyEvaluation, t);
  }
}
