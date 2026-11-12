/* IBKR Options Analyzer dashboard — Panel visibility bar (`.panel-toggle-pill`) and persistence. */

import {
  DEFAULT_PANEL_VISIBILITY,
  PANEL_VISIBILITY_KEY,
  cloneDefaultPanelVisibility,
  state,
} from "./state.js";
import {
  isStrategyAutoPopulateEnabled,
  themeVars,
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

// ------------------------------------------------------------------ Panel Visibility
export function readPanelVisibility() {
  var next = cloneDefaultPanelVisibility();
  try {
    var raw = window.localStorage.getItem(PANEL_VISIBILITY_KEY);
    if (!raw) return next;
    var parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return next;
    Object.keys(DEFAULT_PANEL_VISIBILITY).forEach(function (panelId) {
      if (typeof parsed[panelId] === "boolean") {
        next[panelId] = parsed[panelId];
      }
    });
  } catch (_err) {
    return cloneDefaultPanelVisibility();
  }
  return next;
}

function writePanelVisibility(vis) {
  try {
    window.localStorage.setItem(PANEL_VISIBILITY_KEY, JSON.stringify(vis));
  } catch (_err) {
    /* ignore */
  }
}

function clearPanelVisibilityOverrides() {
  try {
    window.localStorage.removeItem(PANEL_VISIBILITY_KEY);
  } catch (_err) {
    /* ignore */
  }
}

export function isPanelVisible(panelId) {
  if (!state.panelVisibility || typeof state.panelVisibility[panelId] !== "boolean") {
    return Boolean(DEFAULT_PANEL_VISIBILITY[panelId]);
  }
  return state.panelVisibility[panelId];
}

function syncPanelVisibilityUI(vis) {
  var buttons = document.querySelectorAll(
    "#panel-visibility-controls [data-panel-target]"
  );
  for (var i = 0; i < buttons.length; i++) {
    var btn = buttons[i];
    var target = btn.getAttribute("data-panel-target");
    if (!target) continue;
    var on = Boolean(vis[target]);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.classList.toggle("active", on);
  }
  var cards = document.querySelectorAll(".chart-grid .panel-card[data-panel-id]");
  for (var j = 0; j < cards.length; j++) {
    var card = cards[j];
    var pid = card.getAttribute("data-panel-id");
    if (!pid) continue;
    card.classList.toggle("is-hidden", !vis[pid]);
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

  state.panelVisibility = normalized;
  syncPanelVisibilityUI(normalized);

  if (opts.clearOverrides) {
    clearPanelVisibilityOverrides();
  } else if (opts.persist) {
    writePanelVisibility(normalized);
  }

  if (revealed.length && !opts.skipRender) {
    window.requestAnimationFrame(function () {
      var t = themeVars();
      for (var i = 0; i < revealed.length; i++) {
        var pid = revealed[i];
        if (!isPanelVisible(pid)) continue;
        var card = document.querySelector(
          '.chart-grid .panel-card[data-panel-id="' + pid + '"]'
        );
        if (card && card.classList.contains("is-hidden")) continue;
        if (state.chainData) {
          renderPanelChartById(pid, state.chainData, t);
        }
        var canvasEl = card ? card.querySelector(".chart-canvas") : null;
        if (canvasEl && window.Plotly && window.Plotly.Plots && canvasEl.data) {
          window.Plotly.Plots.resize(canvasEl);
        }
      }
    });
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
