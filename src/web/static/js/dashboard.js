/* ==========================================================================
   IBKR Options Analyzer — Interactive Multi-DTE Dashboard Controller
   ========================================================================== */

(function () {
  "use strict";

  var CHART_IDS = [
    "chart-walls",
    "chart-iv-smile",
    "chart-anomalies",
    "chart-greeks",
    "chart-corridor-ladder",
    "chart-strategy-payoff",
  ];

  var PANEL_VISIBILITY_KEY = "ibkr_panel_visibility_v1";
  var TABLE_SORT_KEY = "ibkr_chain_table_sort_v1";
  var STRATEGY_AUTO_POPULATE_KEY = "ibkr_strategy_auto_populate_v1";
  var PULL_PREFS_KEY = "ibkr_pull_prefs_v1";
  var DEFAULT_PANEL_VISIBILITY = {
    walls: true,
    iv_smile: false,
    anomalies: true,
    greeks: false,
    corridor: true,
    strategy: true,
  };

  function cloneDefaultPanelVisibility() {
    return {
      walls: DEFAULT_PANEL_VISIBILITY.walls,
      iv_smile: DEFAULT_PANEL_VISIBILITY.iv_smile,
      anomalies: DEFAULT_PANEL_VISIBILITY.anomalies,
      greeks: DEFAULT_PANEL_VISIBILITY.greeks,
      corridor: DEFAULT_PANEL_VISIBILITY.corridor,
      strategy: DEFAULT_PANEL_VISIBILITY.strategy,
    };
  }

  var bootstrapEl = document.getElementById("app-bootstrap");
  var bootstrap = bootstrapEl ? JSON.parse(bootstrapEl.textContent || "{}") : {};

  var state = {
    ticker: bootstrap.defaultTicker || "NVDA",
    snapshotId: "",
    watchlist: bootstrap.watchlist || ["NVDA", "AVGO", "CRDO", "PLTR"],
    chainData: null,
    snapshots: [],
    status: null,
    loading: false,
    panelVisibility: cloneDefaultPanelVisibility(),
    corridorExpiry: "",
    corridorWindow: 5,
    corridorFrame: null,
    corridorBusy: false,
    corridorStreaming: false,
    corridorStreamSource: null,
    corridorPrevMids: {},
    corridorLastRepriceAt: 0,
    corridorAutoRepriceTimer: null,
    strategyKind: "BULL_PUT_SPREAD",
    strategyLegs: [],
    strategyEvaluation: null,
    strategyEvaluationDirty: false,
    strategyPendingEvaluate: null,
    strategyBusy: false,
    strategyLoadedTicker: "",
    bannerSeq: 0,
    activeBannerKey: "",
    activeBannerCount: 0,
    bannerFreshTimer: null,
    tableSort: { col: null, dir: null },
    tableFilters: {},
  };

  var dom = {
    gatewayBadge: document.getElementById("gateway-badge"),
    mdTypeBadge: document.getElementById("md-type-badge"),
    greeksBadge: document.getElementById("greeks-source-badge"),
    themeToggle: document.getElementById("theme-toggle"),
    editorialBannerCopy: document.getElementById("editorial-banner-copy"),
    watchlistPills: document.getElementById("watchlist-pills"),
    customForm: document.getElementById("custom-ticker-form"),
    customInput: document.getElementById("custom-ticker-input"),
    rangeSelect: document.getElementById("range-pct-select"),
    stepSelect: document.getElementById("strike-step-select"),
    dteMaxSelect: document.getElementById("dte-max-select"),
    snapshotSelect: document.getElementById("snapshot-select"),
    refreshBtn: document.getElementById("refresh-btn"),
    exportCsvBtn: document.getElementById("export-csv-btn"),
    panelVisibilityControls: document.getElementById("panel-visibility-controls"),
    panelVisibilityReset: document.getElementById("panel-visibility-reset"),
    alertBanner: document.getElementById("alert-banner"),
    kpiSpot: document.getElementById("kpi-spot"),
    kpiCallWall: document.getElementById("kpi-call-wall"),
    kpiPutWall: document.getElementById("kpi-put-wall"),
    kpiAnomalies: document.getElementById("kpi-anomalies"),
    wallsBasisLabel: document.getElementById("walls-basis-label"),
    wallsViewSelect: document.getElementById("walls-view-select"),
    wallsMetricSelect: document.getElementById("walls-metric-select"),
    wallsDteSelect: document.getElementById("walls-dte-select"),
    wallsSplitDte: document.getElementById("walls-split-dte"),
    wallsSplitDteWrap: document.getElementById("walls-split-dte-wrap"),
    wallsShowAnomalies: document.getElementById("walls-show-anomalies"),
    wallsCopyAiBtn: document.getElementById("walls-copy-ai-btn"),
    greeksMetricSelect: document.getElementById("greeks-metric-select"),
    dteBucketTabs: document.getElementById("dte-bucket-tabs"),
    filterDte: document.getElementById("filter-dte"),
    filterRight: document.getElementById("filter-right"),
    filterUnusual: document.getElementById("filter-unusual"),
    tableResetFiltersBtn: document.getElementById("table-reset-filters-btn"),
    chainTableHead: document.querySelector("#chain-table thead"),
    thFilterExpiry: document.getElementById("th-filter-expiry"),
    thFilterRight: document.getElementById("th-filter-right"),
    thFilterSource: document.getElementById("th-filter-source"),
    tableBody: document.getElementById("chain-table-body"),
    tableRowCount: document.getElementById("table-row-count"),
    strategyBasisBadge: document.getElementById("strategy-basis-badge"),
    strategyAutoPopulate: document.getElementById("strategy-auto-populate"),
    strategyPresetPills: document.getElementById("strategy-preset-pills"),
    strategyExpirySelect: document.getElementById("strategy-expiry-select"),
    strategyQtyInput: document.getElementById("strategy-qty-input"),
    strategyAutoBuildBtn: document.getElementById("strategy-autobuild-btn"),
    strategyEvaluateBtn: document.getElementById("strategy-evaluate-btn"),
    strategyAddLegBtn: document.getElementById("strategy-add-leg-btn"),
    strategyResetBtn: document.getElementById("strategy-reset-btn"),
    strategyKpiPrice: document.getElementById("strategy-kpi-price"),
    strategyKpiPriceSub: document.getElementById("strategy-kpi-price-sub"),
    strategyKpiSlip: document.getElementById("strategy-kpi-slip"),
    strategyKpiSlipSub: document.getElementById("strategy-kpi-slip-sub"),
    strategyKpiRisk: document.getElementById("strategy-kpi-risk"),
    strategyKpiRiskSub: document.getElementById("strategy-kpi-risk-sub"),
    strategyKpiPop: document.getElementById("strategy-kpi-pop"),
    strategyKpiPopSub: document.getElementById("strategy-kpi-pop-sub"),
    strategyKpiGreeks: document.getElementById("strategy-kpi-greeks"),
    strategyKpiGreeksSub: document.getElementById("strategy-kpi-greeks-sub"),
    strategyRationale: document.getElementById("strategy-rationale"),
    strategyLegsTbody: document.getElementById("strategy-legs-tbody"),
    strategyDaysSlider: document.getElementById("strategy-days-slider"),
    strategyDaysReadout: document.getElementById("strategy-days-readout"),
    strategyIvSlider: document.getElementById("strategy-iv-slider"),
    strategyIvReadout: document.getElementById("strategy-iv-readout"),
    strategyChartEl: document.getElementById("chart-strategy-payoff"),
    corridorStatusBadge: document.getElementById("corridor-status-badge"),
    corridorSubsBadge: document.getElementById("corridor-subs-badge"),
    corridorExpirySelect: document.getElementById("corridor-expiry-select"),
    corridorWindowSelect: document.getElementById("corridor-window-select"),
    corridorRefreshBtn: document.getElementById("corridor-refresh-btn"),
    corridorStreamToggle: document.getElementById("corridor-stream-toggle"),
    corridorAutoReprice: document.getElementById("corridor-auto-reprice"),
    corridorKpiPw: document.getElementById("corridor-kpi-pw"),
    corridorKpiPwSub: document.getElementById("corridor-kpi-pw-sub"),
    corridorKpiCw: document.getElementById("corridor-kpi-cw"),
    corridorKpiCwSub: document.getElementById("corridor-kpi-cw-sub"),
    corridorKpiSpot: document.getElementById("corridor-kpi-spot"),
    corridorKpiSpotSub: document.getElementById("corridor-kpi-spot-sub"),
    corridorKpiSync: document.getElementById("corridor-kpi-sync"),
    corridorKpiSyncSub: document.getElementById("corridor-kpi-sync-sub"),
    corridorLadderTbody: document.getElementById("corridor-ladder-tbody"),
    corridorChartEl: document.getElementById("chart-corridor-ladder"),
  };

  // ------------------------------------------------------------------ Helpers
  function extractApiErrorMessage(body, fallback) {
    if (!body) return fallback;
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail;
    if (Array.isArray(body.detail) && body.detail.length) {
      var parts = body.detail
        .map(function (item) {
          if (typeof item === "string") return item;
          if (!item || typeof item !== "object") return "";
          var loc = Array.isArray(item.loc) ? item.loc.join(".") : "";
          var msg = item.msg || "";
          return loc && msg ? loc + ": " + msg : msg || loc;
        })
        .filter(Boolean);
      if (parts.length) return parts.join("; ");
    }
    if (typeof body.message === "string" && body.message.trim()) return body.message;
    return fallback;
  }

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "terminal"
      ? "terminal"
      : "print";
  }

  function themeVars() {
    var styles = window.getComputedStyle(document.documentElement);
    function cssVar(name, fallback) {
      var val = styles.getPropertyValue(name);
      return val ? val.trim() : fallback;
    }
    var isTerminal = currentTheme() === "terminal";
    return {
      isTerminal: isTerminal,
      bg: cssVar("--plotly-bg", isTerminal ? "#11161F" : "#FFFFFF"),
      grid: cssVar("--plotly-grid", isTerminal ? "#1E2636" : "#E8E4DA"),
      ink: cssVar("--plotly-ink", isTerminal ? "#EAECEF" : "#111111"),
      muted: cssVar("--text-muted", isTerminal ? "#6C7687" : "#727272"),
      call: cssVar("--accent-call", isTerminal ? "#10B981" : "#0F6E4F"),
      put: cssVar("--accent-put", isTerminal ? "#EF4444" : "#A82020"),
      anomaly: cssVar("--accent-anomaly", isTerminal ? "#FB923C" : "#C2410C"),
      warn: cssVar("--accent-warn", isTerminal ? "#F59E0B" : "#B45309"),
      fontMono: cssVar("--font-mono", "monospace"),
      dtePalette: isTerminal
        ? ["#38BDF8", "#10B981", "#F59E0B", "#A78BFA", "#F472B6"]
        : ["#1D4ED8", "#0F6E4F", "#B45309", "#6D28D9", "#A82020"],
      callDteShades: isTerminal
        ? ["#059669", "#10B981", "#34D399", "#6EE7B7", "#A7F3D0"]
        : ["#0B4F38", "#0F6E4F", "#1B8A64", "#3FA682", "#72C0A4"],
      putDteShades: isTerminal
        ? ["#DC2626", "#EF4444", "#F87171", "#FCA5A5", "#FECACA"]
        : ["#7F1616", "#A82020", "#C53B3B", "#DC6363", "#EA9292"],
    };
  }

  function readPref(key, fallback) {
    try {
      var val = window.localStorage.getItem(key);
      return val !== null ? val : fallback;
    } catch (_err) {
      return fallback;
    }
  }

  function writePref(key, value) {
    try {
      window.localStorage.setItem(key, String(value));
    } catch (_err) {
      /* ignore */
    }
  }

  function isStrategyAutoPopulateEnabled() {
    if (dom.strategyAutoPopulate) {
      return Boolean(dom.strategyAutoPopulate.checked);
    }
    return readPref(STRATEGY_AUTO_POPULATE_KEY, "true") !== "false";
  }

  function syncStrategyAutoPopulateUI() {
    if (!dom.strategyAutoPopulate) return;
    dom.strategyAutoPopulate.checked = readPref(STRATEGY_AUTO_POPULATE_KEY, "true") !== "false";
  }

  function readPullPrefsMap() {
    try {
      var raw = window.localStorage.getItem(PULL_PREFS_KEY);
      if (!raw) return {};
      var parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_err) {
      return {};
    }
  }

  function readTickerPullPrefs(ticker) {
    var key = String(ticker || "").trim().toUpperCase();
    if (!key) return null;
    var map = readPullPrefsMap();
    return map[key] && typeof map[key] === "object" ? map[key] : null;
  }

  function writeTickerPullPrefs(ticker) {
    var key = String(ticker || "").trim().toUpperCase();
    if (!key) return;
    var map = readPullPrefsMap();
    map[key] = {
      range_pct: dom.rangeSelect ? String(dom.rangeSelect.value || "0.20") : "0.20",
      strike_step: dom.stepSelect ? String(dom.stepSelect.value || "auto") : "auto",
      dte_max: dom.dteMaxSelect ? String(dom.dteMaxSelect.value || "120") : "120",
    };
    try {
      window.localStorage.setItem(PULL_PREFS_KEY, JSON.stringify(map));
    } catch (_err) {
      /* ignore */
    }
  }

  function selectHasEnabledOption(selectEl, targetVal) {
    if (!selectEl || !targetVal) return false;
    for (var i = 0; i < selectEl.options.length; i++) {
      var opt = selectEl.options[i];
      if (opt.value === String(targetVal) && !opt.disabled) {
        return true;
      }
    }
    return false;
  }

  function applyTickerPullPrefsToControls(ticker) {
    var prefs = readTickerPullPrefs(ticker);
    if (!prefs) return false;
    var applied = false;
    if (dom.rangeSelect && prefs.range_pct && selectHasEnabledOption(dom.rangeSelect, prefs.range_pct)) {
      dom.rangeSelect.value = String(prefs.range_pct);
      applied = true;
    }
    if (dom.stepSelect && prefs.strike_step && selectHasEnabledOption(dom.stepSelect, prefs.strike_step)) {
      dom.stepSelect.value = String(prefs.strike_step);
      applied = true;
    }
    if (dom.dteMaxSelect && prefs.dte_max && selectHasEnabledOption(dom.dteMaxSelect, prefs.dte_max)) {
      dom.dteMaxSelect.value = String(prefs.dte_max);
      applied = true;
    }
    return applied;
  }

  function parseExpiryDayMs(rawExpiry) {
    var s = String(rawExpiry || "").trim();
    if (!/^\d{8}$/.test(s)) return null;
    var y = parseInt(s.slice(0, 4), 10);
    var m = parseInt(s.slice(4, 6), 10) - 1;
    var d = parseInt(s.slice(6, 8), 10);
    return Date.UTC(y, m, d);
  }

  function snapDteValueToExpiries(previousVal, expiries, options) {
    var opts = options || {};
    var emptyValue = Object.prototype.hasOwnProperty.call(opts, "emptyValue")
      ? opts.emptyValue
      : "ALL";
    var prev = String(previousVal || "");
    if (!expiries || !expiries.length) return emptyValue;
    if (!prev || prev === "ALL") return emptyValue;

    if (opts.allowBuckets && prev.indexOf("BUCKET:") === 0) {
      for (var b = 0; b < expiries.length; b++) {
        if (matchesDteFilterValue(expiries[b].dte, expiries[b].expiry, prev)) {
          return prev;
        }
      }
      return emptyValue;
    }

    for (var i = 0; i < expiries.length; i++) {
      if (String(expiries[i].expiry) === prev) {
        return prev;
      }
    }

    var targetMs = parseExpiryDayMs(prev);
    if (targetMs === null) {
      return emptyValue;
    }
    var bestExpiry = expiries[0].expiry;
    var bestDiff = Infinity;
    for (var j = 0; j < expiries.length; j++) {
      var candMs = parseExpiryDayMs(expiries[j].expiry);
      if (candMs === null) continue;
      var diff = Math.abs(candMs - targetMs);
      if (diff < bestDiff) {
        bestDiff = diff;
        bestExpiry = expiries[j].expiry;
      }
    }
    return bestExpiry || emptyValue;
  }

  function buildActivePullScopeText() {
    var stepTxt =
      dom.stepSelect && dom.stepSelect.value === "auto"
        ? "Auto"
        : dom.stepSelect
          ? formatStepShort(Number(dom.stepSelect.value))
          : "Auto";
    var rangeTxt = dom.rangeSelect ? formatRangeShort(Number(dom.rangeSelect.value || 0.2)) : "\u00B120%";
    var dteTxt = dom.dteMaxSelect ? "\u2264" + dom.dteMaxSelect.value + "d" : "\u2264120d";
    return "Step " + stepTxt + " | " + rangeTxt + " | " + dteTxt;
  }

  function formatExpiryIso(rawExpiry) {
    var s = String(rawExpiry || "").trim();
    if (/^\d{8}$/.test(s)) {
      return s.slice(0, 4) + "-" + s.slice(4, 6) + "-" + s.slice(6, 8);
    }
    return s;
  }

  function formatStrikeShort(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return "—";
    }
    var n = Number(val);
    if (Math.abs(n - Math.round(n)) < 1e-6) {
      return "$" + Math.round(n).toString();
    }
    return "$" + n.toFixed(2);
  }

  function fmtNum(val, decimals) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return "—";
    }
    return Number(val).toFixed(decimals);
  }

  function fmtInt(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return "—";
    }
    return Math.round(Number(val)).toLocaleString("en-US");
  }

  function fmtMoney(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return "—";
    }
    return "$" + Number(val).toFixed(2);
  }

  function fmtPct(val, decimals) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return "—";
    }
    return (Number(val) * 100).toFixed(decimals === undefined ? 1 : decimals) + "%";
  }

  function formatStepShort(stepVal) {
    if (stepVal === null || stepVal === undefined || Number.isNaN(Number(stepVal))) {
      return "—";
    }
    var n = Number(stepVal);
    return Math.abs(n - Math.round(n)) < 1e-6 ? "$" + Math.round(n) : "$" + n;
  }

  function formatRangeShort(rangeVal) {
    if (rangeVal === null || rangeVal === undefined || Number.isNaN(Number(rangeVal))) {
      return "—";
    }
    return "\u00B1" + Math.round(Number(rangeVal) * 100) + "%";
  }

  function formatPullScopeSummary(meta) {
    if (!meta) return "Range \u2014 \u2022 Step \u2014";
    var rangeTxt =
      meta.range_pct !== null && meta.range_pct !== undefined
        ? formatRangeShort(meta.range_pct)
        : "\u2014";
    var stepTxt =
      meta.strike_step !== null && meta.strike_step !== undefined
        ? formatStepShort(meta.strike_step) + " step"
        : "\u2014 step";
    if (
      meta.requested_strike_step !== null &&
      meta.requested_strike_step !== undefined &&
      meta.strike_step !== null &&
      meta.strike_step !== undefined &&
      Math.abs(Number(meta.requested_strike_step) - Number(meta.strike_step)) > 1e-4
    ) {
      stepTxt += " (req " + formatStepShort(meta.requested_strike_step) + ")";
    }
    return rangeTxt + " \u2022 " + stepTxt;
  }

  function resetSelectCapabilities(selectEl) {
    if (!selectEl) return;
    var stale = selectEl.querySelectorAll(
      'option[data-pulled-temp="true"], option[data-exchange-native="true"]'
    );
    for (var s = 0; s < stale.length; s++) {
      stale[s].parentNode.removeChild(stale[s]);
    }
    for (var i = 0; i < selectEl.options.length; i++) {
      var opt = selectEl.options[i];
      if (!opt.dataset.baseLabel) {
        opt.dataset.baseLabel = opt.textContent.trim();
      }
      opt.disabled = false;
      opt.textContent = opt.dataset.baseLabel;
    }
  }

  function applySelectCapabilityList(selectEl, availableVals, labelFn) {
    if (!selectEl) return;
    var prevVal = selectEl.value;
    resetSelectCapabilities(selectEl);
    if (!Array.isArray(availableVals) || !availableVals.length) {
      if (prevVal && selectEl.querySelector('option[value="' + prevVal + '"]')) {
        selectEl.value = prevVal;
      }
      return;
    }

    var supported = [];
    for (var a = 0; a < availableVals.length; a++) {
      var num = Number(availableVals[a]);
      if (!Number.isNaN(num) && num > 0) {
        supported.push(num);
      }
    }
    if (!supported.length) return;

    // Ensure every exchange-discovered value exists as a selectable option.
    for (var k = 0; k < supported.length; k++) {
      var val = supported[k];
      var found = false;
      for (var j = 0; j < selectEl.options.length; j++) {
        var existing = selectEl.options[j];
        if (existing.value === "auto") continue;
        if (Math.abs(Number(existing.value) - val) < 1e-4) {
          found = true;
          break;
        }
      }
      if (!found) {
        var nativeOpt = document.createElement("option");
        nativeOpt.value = String(val);
        var nativeLabel = labelFn(val);
        nativeOpt.textContent = nativeLabel;
        nativeOpt.dataset.baseLabel = nativeLabel;
        nativeOpt.setAttribute("data-exchange-native", "true");
        selectEl.appendChild(nativeOpt);
      }
    }

    var firstEnabledVal = null;
    var hasAuto = false;
    for (var i = 0; i < selectEl.options.length; i++) {
      var opt = selectEl.options[i];
      if (opt.value === "auto") {
        hasAuto = true;
        continue;
      }
      var optNum = Number(opt.value);
      var isSupported = false;
      for (var m = 0; m < supported.length; m++) {
        if (Math.abs(supported[m] - optNum) < 1e-4) {
          isSupported = true;
          break;
        }
      }
      opt.disabled = !isSupported;
      opt.textContent = isSupported
        ? opt.dataset.baseLabel
        : opt.dataset.baseLabel + " (N/A)";
      if (isSupported && firstEnabledVal === null) {
        firstEnabledVal = opt.value;
      }
    }

    if (prevVal) {
      selectEl.value = prevVal;
    }
    var activeOpt = selectEl.options[selectEl.selectedIndex];
    if (!activeOpt || activeOpt.disabled) {
      selectEl.value = hasAuto ? "auto" : firstEnabledVal || "";
    }
  }

  function resetControlCapabilities() {
    resetSelectCapabilities(dom.rangeSelect);
    resetSelectCapabilities(dom.stepSelect);
    if (dom.stepSelect && !dom.stepSelect.value) {
      dom.stepSelect.value = "auto";
    }
  }

  function syncControlCapabilities(caps) {
    if (!caps) {
      resetControlCapabilities();
      return;
    }
    applySelectCapabilityList(dom.rangeSelect, caps.available_range_pcts, formatRangeShort);
    applySelectCapabilityList(dom.stepSelect, caps.available_strike_steps, formatStepShort);
  }

  function syncSelectToNumeric(selectEl, targetVal, labelFn) {
    if (!selectEl) return;
    var stale = selectEl.querySelectorAll('option[data-pulled-temp="true"]');
    for (var s = 0; s < stale.length; s++) {
      stale[s].parentNode.removeChild(stale[s]);
    }
    if (targetVal === null || targetVal === undefined || Number.isNaN(Number(targetVal))) {
      return;
    }
    var numTarget = Number(targetVal);
    for (var i = 0; i < selectEl.options.length; i++) {
      var opt = selectEl.options[i];
      if (opt.value === "auto") continue;
      if (Math.abs(Number(opt.value) - numTarget) < 1e-4) {
        if (opt.disabled) {
          opt.disabled = false;
          opt.textContent = opt.dataset.baseLabel || opt.textContent;
        }
        selectEl.value = opt.value;
        return;
      }
    }
    var customOpt = document.createElement("option");
    customOpt.value = String(numTarget);
    customOpt.textContent = labelFn(numTarget) + " (pulled)";
    customOpt.setAttribute("data-pulled-temp", "true");
    customOpt.selected = true;
    selectEl.appendChild(customOpt);
  }

  function syncControlsFromMeta(meta, options) {
    var opts = options || {};
    if (!meta) {
      resetControlCapabilities();
      return;
    }
    syncControlCapabilities(meta);
    var tickerKey = meta.underlying || state.ticker;
    var savedPrefs = readTickerPullPrefs(tickerKey);
    if (opts.fromRefresh || !savedPrefs || !savedPrefs.range_pct) {
      syncSelectToNumeric(dom.rangeSelect, meta.range_pct, formatRangeShort);
    } else {
      applyTickerPullPrefsToControls(tickerKey);
    }
    // Keep #strike-step-select on Auto unless the user explicitly selected a manual numeric step.
  }

  function escapeHtml(raw) {
    return String(raw === null || raw === undefined ? "" : raw)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatBannerClock(dateObj) {
    var d = dateObj || new Date();
    var hh = String(d.getHours()).padStart(2, "0");
    var mm = String(d.getMinutes()).padStart(2, "0");
    var ss = String(d.getSeconds()).padStart(2, "0");
    return hh + ":" + mm + ":" + ss;
  }

  function clearAlertBanner() {
    if (state.bannerFreshTimer) {
      window.clearTimeout(state.bannerFreshTimer);
      state.bannerFreshTimer = null;
    }
    state.activeBannerKey = "";
    state.activeBannerCount = 0;
    if (!dom.alertBanner) return;
    dom.alertBanner.hidden = true;
    dom.alertBanner.className = "alert-banner";
    dom.alertBanner.innerHTML = "";
  }

  function showBanner(kind, message, meta) {
    if (!dom.alertBanner) return;
    if (!message) {
      clearAlertBanner();
      return;
    }
    var tone = kind || "info";
    var m = meta && typeof meta === "object" ? meta : {};
    state.bannerSeq += 1;
    var clockStr = formatBannerClock(new Date());
    var dedupKey = [
      tone,
      String(message),
      m.phase || "",
      m.status !== undefined && m.status !== null ? String(m.status) : "",
      m.ticker || "",
    ].join("|");

    var wasVisible = !dom.alertBanner.hidden;
    if (wasVisible && state.activeBannerKey === dedupKey) {
      state.activeBannerCount += 1;
    } else {
      state.activeBannerKey = dedupKey;
      state.activeBannerCount = 1;
    }

    var chips = [];
    var tickerLabel = m.ticker || (tone === "error" || tone === "warn" ? state.ticker : "");
    if (tickerLabel) chips.push(String(tickerLabel));
    if (m.pullScope) chips.push(String(m.pullScope));
    if (m.phase) chips.push(String(m.phase));
    if (m.status !== undefined && m.status !== null && m.status !== "") {
      chips.push("HTTP " + String(m.status));
    }
    if (m.errorCode) chips.push(String(m.errorCode));
    if (m.elapsedMs !== undefined && m.elapsedMs !== null && !Number.isNaN(Number(m.elapsedMs))) {
      chips.push(Math.round(Number(m.elapsedMs)) + " ms");
    }

    var chipsHtml = "";
    for (var i = 0; i < chips.length; i++) {
      chipsHtml += '<span class="alert-banner-chip">' + escapeHtml(chips[i]) + "</span>";
    }
    var repeatHtml =
      state.activeBannerCount > 1
        ? '<span class="alert-banner-badge">\u00D7' +
          state.activeBannerCount +
          " \u2022 last " +
          clockStr +
          "</span>"
        : "";

    var techParts = [];
    if (m.detail) techParts.push(String(m.detail));
    if (m.rawBody && String(m.rawBody) !== String(m.detail || "")) {
      techParts.push("Response snippet: " + String(m.rawBody));
    }
    var techText = techParts.join("\n").trim();

    dom.alertBanner.setAttribute("role", tone === "error" || tone === "warn" ? "alert" : "status");
    dom.alertBanner.className = "alert-banner alert-banner--" + tone;
    dom.alertBanner.innerHTML =
      '<div class="alert-banner-main">' +
      '<div class="alert-banner-head">' +
      '<span class="alert-banner-badge">#' +
      state.bannerSeq +
      " \u2022 " +
      clockStr +
      "</span>" +
      repeatHtml +
      chipsHtml +
      "</div>" +
      '<div class="alert-banner-msg">' +
      escapeHtml(message) +
      "</div>" +
      (m.hint ? '<div class="alert-banner-hint">' + escapeHtml(m.hint) + "</div>" : "") +
      (techText
        ? '<details class="alert-banner-details"><summary>Technical details</summary><pre class="alert-banner-tech">' +
          escapeHtml(techText) +
          "</pre></details>"
        : "") +
      "</div>" +
      '<div class="alert-banner-actions">' +
      '<button type="button" id="alert-banner-close" class="alert-banner-close" aria-label="Dismiss alert" title="Dismiss alert">\u00D7</button>' +
      "</div>";

    dom.alertBanner.hidden = false;
    dom.alertBanner.classList.remove("is-fresh");
    try {
      void dom.alertBanner.offsetWidth;
    } catch (_err) {
      /* ignore layout read errors in non-DOM test envs */
    }
    dom.alertBanner.classList.add("is-fresh");
    if (state.bannerFreshTimer) {
      window.clearTimeout(state.bannerFreshTimer);
    }
    state.bannerFreshTimer = window.setTimeout(function () {
      state.bannerFreshTimer = null;
      if (dom.alertBanner) {
        dom.alertBanner.classList.remove("is-fresh");
      }
    }, 750);
  }

  function setActiveTickerUI(ticker) {
    if (dom.watchlistPills) {
      var pills = dom.watchlistPills.querySelectorAll(".pill-btn");
      for (var i = 0; i < pills.length; i++) {
        var btn = pills[i];
        btn.classList.toggle("active", btn.getAttribute("data-ticker") === ticker);
      }
    }
    if (dom.customInput && state.watchlist.indexOf(ticker) === -1) {
      dom.customInput.value = ticker;
    }
    updateExportLink();
  }

  function updateExportLink() {
    if (!dom.exportCsvBtn) return;
    var url = "/api/csv/" + encodeURIComponent(state.ticker);
    if (state.snapshotId) {
      url += "?snapshot_id=" + encodeURIComponent(state.snapshotId);
    }
    dom.exportCsvBtn.setAttribute("href", url);
  }

  function marketDataBadgeInfo(mdType) {
    var code = Number(mdType);
    if (code === 1) {
      return { label: "TYPE 1: LIVE OPRA", cls: "badge badge--live" };
    }
    if (code === 2) {
      return { label: "TYPE 2: FROZEN", cls: "badge badge--amber" };
    }
    if (code === 4) {
      return { label: "TYPE 4: DELAYED-FROZEN", cls: "badge badge--amber" };
    }
    if (code === 3) {
      return { label: "TYPE 3: 15M DELAYED", cls: "badge badge--delayed" };
    }
    return { label: "TYPE " + (mdType || "3"), cls: "badge badge--info" };
  }

  function marketDataLabel(mdType) {
    return marketDataBadgeInfo(mdType).label;
  }

  function applyMdTypeBadge(mdType) {
    if (!dom.mdTypeBadge) return;
    var info = marketDataBadgeInfo(mdType);
    dom.mdTypeBadge.className = info.cls;
    dom.mdTypeBadge.textContent = info.label;
  }

  function updateThemeToggleLabel() {
    if (!dom.themeToggle) return;
    dom.themeToggle.textContent =
      currentTheme() === "terminal" ? "PRINT EDITION" : "TERMINAL THEME";
  }

  // ------------------------------------------------------------------ Badges
  function renderStatusBadges(statusPayload) {
    state.status = statusPayload;
    if (dom.gatewayBadge) {
      var connected = Boolean(statusPayload && statusPayload.connected);
      dom.gatewayBadge.className = "badge " + (connected ? "badge--online" : "badge--offline");
      dom.gatewayBadge.innerHTML =
        '<span class="badge-dot" aria-hidden="true"></span><span>' +
        (connected ? "GATEWAY ONLINE" : "GATEWAY OFFLINE (CACHE ONLY)") +
        "</span>";
    }
    if (dom.mdTypeBadge && statusPayload) {
      var md =
        statusPayload.active_market_data_type !== null &&
        statusPayload.active_market_data_type !== undefined
          ? statusPayload.active_market_data_type
          : statusPayload.default_market_data_type || 3;
      applyMdTypeBadge(md);
    }
    if (statusPayload && !state.chainData) {
      syncControlCapabilities(statusPayload);
    }
  }

  function renderChainBadges(meta) {
    if (
      dom.mdTypeBadge &&
      meta &&
      (!state.status ||
        state.status.active_market_data_type === null ||
        state.status.active_market_data_type === undefined)
    ) {
      applyMdTypeBadge(meta.market_data_type);
    }
    if (dom.editorialBannerCopy) {
      if (!meta) {
        dom.editorialBannerCopy.textContent =
          "Immutable CSV Archive \u2022 Delayed Type 3/4 Feed \u2022 Weekly Expirations (30\u2013120 DTE)";
      } else {
        var dteSpanTxt =
          meta.dtes && meta.dtes.length
            ? meta.dtes[0] +
              "\u2013" +
              meta.dtes[meta.dtes.length - 1] +
              "d (" +
              meta.dtes.length +
              " wk exp)"
            : "30\u2013120 DTE";
        var verifiedSuffix =
          meta.exchange_native_verified && meta.exchange_validated_strike_count
            ? " \u2022 " + meta.exchange_validated_strike_count + " IBKR strikes"
            : "";
        dom.editorialBannerCopy.textContent =
          meta.underlying +
          " (" +
          meta.market_date +
          ") \u2022 Pulled Scope: " +
          formatPullScopeSummary(meta) +
          " \u2022 Expirations: " +
          dteSpanTxt +
          verifiedSuffix;
      }
    }
    if (dom.greeksBadge) {
      if (!meta || !meta.greeks_source_counts) {
        dom.greeksBadge.textContent = "GREEKS: —";
        return;
      }
      var counts = meta.greeks_source_counts;
      var parts = [];
      if (counts.IBKR_MODEL) parts.push("IBKR:" + counts.IBKR_MODEL);
      if (counts.BSM_FALLBACK) parts.push("BSM:" + counts.BSM_FALLBACK);
      if (counts.NONE) parts.push("NONE:" + counts.NONE);
      dom.greeksBadge.textContent = "GREEKS: " + (parts.length ? parts.join(" / ") : "—");
    }
  }

  // ------------------------------------------------------------------ KPI Strip
  function setKpi(cardEl, valueText, subText) {
    if (!cardEl) return;
    var valEl = cardEl.querySelector(".kpi-value");
    var subEl = cardEl.querySelector(".kpi-sub");
    if (valEl) valEl.textContent = valueText;
    if (subEl) subEl.textContent = subText;
  }

  function renderKpis(data) {
    if (!data) {
      setKpi(dom.kpiSpot, "—", "No cached snapshot for " + state.ticker);
      setKpi(dom.kpiCallWall, "—", "Click REFRESH FROM IBKR");
      setKpi(dom.kpiPutWall, "—", "Click REFRESH FROM IBKR");
      setKpi(dom.kpiAnomalies, "—", "0 flagged contracts");
      return;
    }

    var meta = data.meta;
    var walls = data.walls;
    var anomalies = data.anomalies;

    setKpi(
      dom.kpiSpot,
      fmtMoney(meta.underlying_price),
      meta.underlying +
        " \u2022 " +
        meta.source +
        " \u2022 " +
        meta.contract_count +
        "c \u2022 " +
        formatPullScopeSummary(meta) +
        " \u2022 " +
        meta.market_date
    );

    var cw = walls.aggregate_primary_call_wall;
    if (cw) {
      var cwSign = cw.distance_from_spot_pct >= 0 ? "+" : "";
      setKpi(
        dom.kpiCallWall,
        "$" + fmtNum(cw.strike, 2),
        walls.primary_basis +
          ": " +
          fmtInt(cw.metric_value) +
          " (" +
          cwSign +
          fmtNum(cw.distance_from_spot_pct, 1) +
          "% vs spot)"
      );
    } else {
      setKpi(dom.kpiCallWall, "—", "Insufficient Call " + walls.primary_basis);
    }

    var pw = walls.aggregate_primary_put_wall;
    if (pw) {
      var pwSign = pw.distance_from_spot_pct >= 0 ? "+" : "";
      setKpi(
        dom.kpiPutWall,
        "$" + fmtNum(pw.strike, 2),
        walls.primary_basis +
          ": " +
          fmtInt(pw.metric_value) +
          " (" +
          pwSign +
          fmtNum(pw.distance_from_spot_pct, 1) +
          "% vs spot)"
      );
    } else {
      setKpi(dom.kpiPutWall, "—", "Insufficient Put " + walls.primary_basis);
    }

    setKpi(
      dom.kpiAnomalies,
      String(anomalies.total_flagged),
      anomalies.vol_gt_oi_count +
        " Vol>OI \u2022 " +
        anomalies.high_zscore_count +
        " High Z-score"
    );
  }

  // ------------------------------------------------------------------ Plotly Charts
  function renderCorridorEmptyPlaceholder() {
    if (!dom.corridorChartEl) return;
    if (typeof window.Plotly !== "undefined" && dom.corridorChartEl.data) {
      window.Plotly.purge(dom.corridorChartEl);
    }
    dom.corridorChartEl.classList.add("is-idle");
    dom.corridorChartEl.innerHTML =
      '<div class="strategy-empty-state">Load an option snapshot to preview the Put/Call Wall \u00B15 strike corridor, or click <strong>Refresh Corridor Quotes</strong>.</div>';
  }

  function renderStrategyEmptyPlaceholder() {
    if (!dom.strategyChartEl) return;
    if (typeof window.Plotly !== "undefined" && dom.strategyChartEl.data) {
      window.Plotly.purge(dom.strategyChartEl);
    }
    dom.strategyChartEl.classList.add("is-idle");
    dom.strategyChartEl.innerHTML =
      '<div class="strategy-empty-state">Click a preset or <strong>Auto-Build from Walls</strong> / <strong>Evaluate / Re-Price</strong> to plot T+0, Midpoint, Expiration &amp; Simulated P&amp;L curves.</div>';
  }

  function purgeCharts() {
    if (typeof window.Plotly !== "undefined") {
      for (var i = 0; i < CHART_IDS.length; i++) {
        var el = document.getElementById(CHART_IDS[i]);
        if (el) {
          window.Plotly.purge(el);
          el.innerHTML = "";
        }
      }
    }
    renderCorridorEmptyPlaceholder();
    renderStrategyEmptyPlaceholder();
    if (dom.wallsBasisLabel) {
      dom.wallsBasisLabel.textContent = "NO DATA";
    }
  }

  function makeVerticalMarker(xVal, label, color, yPos, dash, theme) {
    if (xVal === null || xVal === undefined || Number.isNaN(Number(xVal))) {
      return { shape: null, annotation: null };
    }
    var bgFill = theme && theme.bg ? theme.bg : "rgba(255,255,255,0.92)";
    var fontFam = theme && theme.fontMono ? theme.fontMono : "monospace";
    return {
      shape: {
        type: "line",
        x0: xVal,
        x1: xVal,
        y0: 0,
        y1: 1,
        yref: "paper",
        line: { color: color, width: 2, dash: dash || "dash" },
      },
      annotation: {
        x: xVal,
        y: yPos !== undefined ? yPos : 0.99,
        yref: "paper",
        text: "<b>" + label + "</b>",
        showarrow: false,
        font: { size: 11, color: color, family: fontFam },
        bgcolor: bgFill,
        bordercolor: color,
        borderwidth: 1.5,
        borderpad: 3,
        yanchor: "top",
      },
    };
  }

  function makeWallReferenceMarker(
    orientation,
    strikeVal,
    label,
    color,
    posOffset,
    dash,
    placeAbove,
    theme
  ) {
    if (strikeVal === null || strikeVal === undefined || Number.isNaN(Number(strikeVal))) {
      return { shape: null, annotation: null };
    }
    var bgFill = theme && theme.bg ? theme.bg : "rgba(255,255,255,0.92)";
    var fontFam = theme && theme.fontMono ? theme.fontMono : "monospace";
    if (orientation === "ladder") {
      var xAnchorPos = posOffset !== undefined ? posOffset : 0.99;
      return {
        shape: {
          type: "line",
          x0: 0,
          x1: 1,
          xref: "paper",
          y0: strikeVal,
          y1: strikeVal,
          line: { color: color, width: 2, dash: dash || "dash" },
        },
        annotation: {
          x: xAnchorPos,
          xref: "paper",
          xanchor: xAnchorPos < 0.5 ? "left" : "right",
          y: strikeVal,
          yanchor: placeAbove === false ? "top" : "bottom",
          yshift: placeAbove === false ? -3 : 3,
          text: "<b>" + label + "</b>",
          showarrow: false,
          font: { size: 11, color: color, family: fontFam },
          bgcolor: bgFill,
          bordercolor: color,
          borderwidth: 1.5,
          borderpad: 3,
        },
      };
    }
    return makeVerticalMarker(strikeVal, label, color, posOffset, dash, theme);
  }

  function baseLayout(t, xTitle, yTitle, markers) {
    var shapes = [];
    var annotations = [];
    if (markers && markers.length) {
      for (var i = 0; i < markers.length; i++) {
        if (markers[i] && markers[i].shape) shapes.push(markers[i].shape);
        if (markers[i] && markers[i].annotation) annotations.push(markers[i].annotation);
      }
    }
    return {
      autosize: true,
      paper_bgcolor: t.bg,
      plot_bgcolor: t.bg,
      font: { family: t.fontMono, size: 11, color: t.ink },
      margin: { t: 28, r: 22, b: 44, l: 54 },
      xaxis: {
        title: { text: xTitle, font: { size: 11, color: t.muted } },
        gridcolor: t.grid,
        zerolinecolor: t.grid,
        tickfont: { color: t.ink },
      },
      yaxis: {
        title: { text: yTitle, font: { size: 11, color: t.muted } },
        gridcolor: t.grid,
        zerolinecolor: t.grid,
        tickfont: { color: t.ink },
      },
      legend: {
        orientation: "h",
        y: 1.12,
        x: 0,
        font: { size: 10, color: t.ink },
      },
      shapes: shapes,
      annotations: annotations,
      hovermode: "x unified",
      hoverlabel: {
        align: "left",
        font: { family: t.fontMono, size: 11 },
      },
    };
  }

  var DTE_BUCKET_DEFS = [
    { key: "BUCKET:30-60", label: "30\u201360d", min: 30, max: 60 },
    { key: "BUCKET:61-90", label: "61\u201390d", min: 61, max: 90 },
    { key: "BUCKET:91-120", label: "91\u2013120d", min: 91, max: 120 },
  ];

  function matchesDteFilterValue(dteVal, expiryStr, filterVal) {
    if (!filterVal || filterVal === "ALL") return true;
    for (var b = 0; b < DTE_BUCKET_DEFS.length; b++) {
      if (DTE_BUCKET_DEFS[b].key === filterVal) {
        var nDte = Number(dteVal);
        return !isNaN(nDte) && nDte >= DTE_BUCKET_DEFS[b].min && nDte <= DTE_BUCKET_DEFS[b].max;
      }
    }
    return String(expiryStr) === String(filterVal);
  }

  function formatDteBucketLabel(filterVal) {
    for (var b = 0; b < DTE_BUCKET_DEFS.length; b++) {
      if (DTE_BUCKET_DEFS[b].key === filterVal) return DTE_BUCKET_DEFS[b].label;
    }
    return String(filterVal || "");
  }

  function appendDteBucketOptions(selectEl, expiries, previous) {
    var matchingBuckets = [];
    for (var b = 0; b < DTE_BUCKET_DEFS.length; b++) {
      var def = DTE_BUCKET_DEFS[b];
      var cnt = 0;
      for (var e = 0; e < expiries.length; e++) {
        if (expiries[e].dte >= def.min && expiries[e].dte <= def.max) cnt += 1;
      }
      if (cnt > 0) {
        matchingBuckets.push({ def: def, cnt: cnt });
      }
    }
    if (!matchingBuckets.length) return;
    var group = document.createElement("optgroup");
    group.label = "DTE Buckets (30\u2013120d)";
    for (var m = 0; m < matchingBuckets.length; m++) {
      var item = matchingBuckets[m];
      var bOpt = document.createElement("option");
      bOpt.value = item.def.key;
      bOpt.textContent = item.def.label + " (" + item.cnt + " exp)";
      if (bOpt.value === previous) bOpt.selected = true;
      group.appendChild(bOpt);
    }
    selectEl.appendChild(group);
  }

  function syncWallsSplitCheckboxState() {
    if (!dom.wallsSplitDte) return;
    var dteVal = dom.wallsDteSelect ? dom.wallsDteSelect.value : "ALL";
    var metricVal = dom.wallsMetricSelect ? dom.wallsMetricSelect.value : "auto";
    var canSplit = (dteVal === "ALL" || dteVal.indexOf("BUCKET:") === 0) && metricVal !== "overlay";
    dom.wallsSplitDte.disabled = !canSplit;
    if (dom.wallsSplitDteWrap) {
      dom.wallsSplitDteWrap.classList.toggle("is-disabled", !canSplit);
    }
  }

  function populateWallsDteFilter(data) {
    if (!dom.wallsDteSelect) return;
    var rawPrev = dom.wallsDteSelect.value || "ALL";
    dom.wallsDteSelect.innerHTML = "";

    var defaultOpt = document.createElement("option");
    defaultOpt.value = "ALL";

    var expiries =
      data && data.profiles && data.profiles.expiries ? data.profiles.expiries.slice() : [];
    expiries.sort(function (a, b) {
      return a.dte - b.dte;
    });

    if (!expiries.length) {
      defaultOpt.textContent = "All DTEs";
      dom.wallsDteSelect.appendChild(defaultOpt);
      syncWallsSplitCheckboxState();
      return;
    }

    var previous = snapDteValueToExpiries(rawPrev, expiries, {
      allowBuckets: true,
      emptyValue: "ALL",
    });

    var minDte = expiries[0].dte;
    var maxDte = expiries[expiries.length - 1].dte;
    var dteSpan = minDte === maxDte ? minDte + "d" : minDte + "\u2013" + maxDte + "d";
    defaultOpt.textContent =
      "All DTEs (" + dteSpan + " \u2022 " + expiries.length + " exp)";
    dom.wallsDteSelect.appendChild(defaultOpt);

    appendDteBucketOptions(dom.wallsDteSelect, expiries, previous);

    var expGroup = document.createElement("optgroup");
    expGroup.label = "Expirations";
    for (var i = 0; i < expiries.length; i++) {
      var exp = expiries[i];
      var opt = document.createElement("option");
      opt.value = exp.expiry;
      opt.textContent = exp.dte + "d (" + formatExpiryIso(exp.expiry) + ")";
      if (opt.value === previous) {
        opt.selected = true;
      }
      expGroup.appendChild(opt);
    }
    dom.wallsDteSelect.appendChild(expGroup);
    if (previous) {
      dom.wallsDteSelect.value = previous;
    }
    syncWallsSplitCheckboxState();
  }

  function buildSymmetricPositiveTicks(maxLeft, maxRight) {
    var absMax = Math.max(maxLeft || 0, maxRight || 0, 1);
    var rawStep = absMax / 3;
    var mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
    var norm = rawStep / mag;
    var niceNorm = 1;
    if (norm > 5) niceNorm = 10;
    else if (norm > 2.5) niceNorm = 5;
    else if (norm > 2) niceNorm = 2.5;
    else if (norm > 1) niceNorm = 2;
    var step = Math.max(1, Math.round(niceNorm * mag));

    var leftSteps = maxLeft > 0 ? Math.ceil(maxLeft / step) : 0;
    var rightSteps = maxRight > 0 ? Math.ceil(maxRight / step) : 1;
    var vals = [];
    for (var l = leftSteps; l >= 1; l--) {
      vals.push(-l * step);
    }
    vals.push(0);
    for (var r = 1; r <= rightSteps; r++) {
      vals.push(r * step);
    }
    var labels = vals.map(function (v) {
      return Math.round(Math.abs(v)).toLocaleString("en-US");
    });
    var leftBound = maxLeft > 0 ? Math.max(maxLeft * 1.14, step * 0.6) : step * 0.25;
    var rightBound = maxRight > 0 ? Math.max(maxRight * 1.14, step * 0.6) : step * 0.25;
    return {
      tickvals: vals,
      ticktext: labels,
      range: [-leftBound, rightBound],
    };
  }

  function resolveWallCandidate(data, activeExpiries, dteSelection, basisKey, side) {
    var isCall = side === "C";
    var dataMatchesPrimary =
      (basisKey === "oi" && data.walls.primary_basis === "OPEN_INTEREST") ||
      (basisKey === "volume" && data.walls.primary_basis === "VOLUME");

    var wallStrike = null;
    if (dataMatchesPrimary) {
      if (dteSelection === "ALL") {
        var agg = isCall
          ? data.walls.aggregate_primary_call_wall
          : data.walls.aggregate_primary_put_wall;
        wallStrike = agg ? agg.strike : null;
      } else {
        var byDte = data.walls.by_dte || [];
        for (var i = 0; i < byDte.length; i++) {
          if (byDte[i].expiry === dteSelection) {
            var bw = isCall ? byDte[i].primary_call_wall : byDte[i].primary_put_wall;
            wallStrike = bw ? bw.strike : null;
            break;
          }
        }
      }
    }

    if (wallStrike === null) {
      var totalsByStrike = {};
      for (var bIdx = 0; bIdx < activeExpiries.length; bIdx++) {
        var rows = activeExpiries[bIdx].strikes || [];
        for (var rIdx = 0; rIdx < rows.length; rIdx++) {
          var row = rows[rIdx];
          var k = Number(row.strike).toFixed(4);
          var field = isCall
            ? basisKey === "volume"
              ? "call_volume"
              : "call_oi"
            : basisKey === "volume"
            ? "put_volume"
            : "put_oi";
          totalsByStrike[k] = {
            strike: Number(row.strike),
            val: (totalsByStrike[k] ? totalsByStrike[k].val : 0) + (row[field] || 0),
          };
        }
      }
      var bestVal = 0;
      Object.keys(totalsByStrike).forEach(function (k) {
        if (totalsByStrike[k].val > bestVal) {
          bestVal = totalsByStrike[k].val;
          wallStrike = totalsByStrike[k].strike;
        }
      });
    }

    if (wallStrike === null) {
      return null;
    }

    var prefix = isCall ? "CALL WALL " : "PUT WALL ";
    if (dteSelection !== "ALL" && activeExpiries.length === 1) {
      return {
        strike: wallStrike,
        label: prefix + formatStrikeShort(wallStrike) + " (" + activeExpiries[0].dte + "d)",
      };
    }

    var fieldName = isCall
      ? basisKey === "volume"
        ? "call_volume"
        : "call_oi"
      : basisKey === "volume"
      ? "put_volume"
      : "put_oi";
    var totalAtWall = 0;
    var domDte = null;
    var domVal = -1;
    var contributingExpiries = 0;
    for (var j = 0; j < activeExpiries.length; j++) {
      var expItem = activeExpiries[j];
      var expRows = expItem.strikes || [];
      for (var m = 0; m < expRows.length; m++) {
        if (Math.abs(Number(expRows[m].strike) - wallStrike) < 1e-6) {
          var v = expRows[m][fieldName] || 0;
          if (v > 0) contributingExpiries += 1;
          totalAtWall += v;
          if (v > domVal) {
            domVal = v;
            domDte = expItem.dte;
          }
        }
      }
    }

    if (domDte === null || totalAtWall <= 0) {
      return { strike: wallStrike, label: prefix + formatStrikeShort(wallStrike) };
    }
    var sharePct = Math.round((domVal / totalAtWall) * 100);
    var dteSuffix =
      contributingExpiries <= 1 || activeExpiries.length === 1
        ? "(" + domDte + "d)"
        : "(dom. " + domDte + "d \u2022 " + sharePct + "%)";
    return {
      strike: wallStrike,
      label: prefix + formatStrikeShort(wallStrike) + " " + dteSuffix,
    };
  }

  /**
   * Derive true strike spacing, per-bar widths/offsets, array ticks, and padded range
   * from active plotted strikes so `$0.5/$1/$2.5/$5/$10/$25+` and mixed grids scale natively.
   */
  function computeStrikeAxisGeometry(strikes, metaStrikeStep, spot, callWall, putWall) {
    var count = Array.isArray(strikes) ? strikes.length : 0;
    var fallbackStep = Number(metaStrikeStep) > 0 ? Number(metaStrikeStep) : 5;
    if (count === 0) {
      return {
        minStep: fallbackStep,
        maxStep: fallbackStep,
        modalStep: fallbackStep,
        distinctSteps: [],
        localSteps: [],
        localStepByKey: {},
        tickvals: [],
        ticktext: [],
        axisRange: null,
        stepBadgeText: formatStepShort(metaStrikeStep) + " STEP",
      };
    }

    var diffs = [];
    var minStep = Infinity;
    var maxStep = 0;
    var diffCounts = {};
    for (var i = 1; i < count; i++) {
      var rawDiff = strikes[i] - strikes[i - 1];
      if (rawDiff > 1e-6) {
        var d = Math.round(rawDiff * 10000) / 10000;
        diffs.push(d);
        if (d < minStep) minStep = d;
        if (d > maxStep) maxStep = d;
        var dKey = d.toFixed(4);
        if (!diffCounts[dKey]) {
          diffCounts[dKey] = { step: d, count: 0 };
        }
        diffCounts[dKey].count += 1;
      }
    }

    var modalStep = fallbackStep;
    var distinctSteps = [];
    if (diffs.length > 0) {
      var bestEntry = null;
      Object.keys(diffCounts).forEach(function (k) {
        var entry = diffCounts[k];
        distinctSteps.push(entry.step);
        if (
          !bestEntry ||
          entry.count > bestEntry.count ||
          (entry.count === bestEntry.count && entry.step < bestEntry.step)
        ) {
          bestEntry = entry;
        }
      });
      distinctSteps.sort(function (a, b) {
        return a - b;
      });
      if (bestEntry) modalStep = bestEntry.step;
    } else {
      minStep = fallbackStep;
      maxStep = fallbackStep;
    }

    var localSteps = [];
    var localStepByKey = {};
    if (count === 1 || diffs.length === 0) {
      localSteps.push(fallbackStep);
      localStepByKey[Number(strikes[0]).toFixed(4)] = fallbackStep;
    } else {
      var hasMixedSteps = distinctSteps.length > 1;
      var mixedCap = modalStep * 1.5;
      for (var sIdx = 0; sIdx < count; sIdx++) {
        var prevDiff = sIdx > 0 ? diffs[sIdx - 1] : diffs[0];
        var nextDiff = sIdx < count - 1 ? diffs[sIdx] : diffs[diffs.length - 1];
        var localGap = Math.min(prevDiff, nextDiff);
        var effStep = hasMixedSteps ? Math.min(localGap, mixedCap) : localGap;
        localSteps.push(effStep);
        localStepByKey[Number(strikes[sIdx]).toFixed(4)] = effStep;
      }
    }

    var tickvals = [];
    var ticktext = [];
    if (count <= 22) {
      for (var tIdx = 0; tIdx < count; tIdx++) {
        tickvals.push(strikes[tIdx]);
        ticktext.push(formatStrikeShort(strikes[tIdx]));
      }
    } else {
      var stride = Math.ceil(count / 18);
      for (var kIdx = 0; kIdx < count; kIdx += stride) {
        tickvals.push(strikes[kIdx]);
        ticktext.push(formatStrikeShort(strikes[kIdx]));
      }
    }

    var coords = [strikes[0], strikes[count - 1]];
    if (spot > 0 && !Number.isNaN(Number(spot))) coords.push(Number(spot));
    if (callWall && !Number.isNaN(Number(callWall.strike))) coords.push(Number(callWall.strike));
    if (putWall && !Number.isNaN(Number(putWall.strike))) coords.push(Number(putWall.strike));
    var minCoord = Math.min.apply(null, coords);
    var maxCoord = Math.max.apply(null, coords);
    var edgeFirst = localSteps[0] || modalStep || fallbackStep;
    var edgeLast = localSteps[localSteps.length - 1] || modalStep || fallbackStep;
    var axisRange = [minCoord - 0.6 * edgeFirst, maxCoord + 0.6 * edgeLast];

    var stepBadgeText =
      distinctSteps.length === 1
        ? formatStepShort(distinctSteps[0]) + " STEP"
        : distinctSteps.length > 1
        ? formatStepShort(minStep) + "\u2013" + formatStepShort(maxStep) + " STEP"
        : formatStepShort(metaStrikeStep) + " STEP";

    return {
      minStep: minStep,
      maxStep: maxStep,
      modalStep: modalStep,
      distinctSteps: distinctSteps,
      localSteps: localSteps,
      localStepByKey: localStepByKey,
      tickvals: tickvals,
      ticktext: ticktext,
      axisRange: axisRange,
      stepBadgeText: stepBadgeText,
    };
  }

  function renderWallsChart(data, t) {
    var el = document.getElementById("chart-walls");
    if (!el || typeof window.Plotly === "undefined") return;

    var allExpiries =
      data && data.profiles && data.profiles.expiries ? data.profiles.expiries.slice() : [];
    allExpiries.sort(function (a, b) {
      return a.dte - b.dte;
    });

    var viewMode = dom.wallsViewSelect ? dom.wallsViewSelect.value : "ladder";
    var metricSel = dom.wallsMetricSelect ? dom.wallsMetricSelect.value : "auto";
    var dteSel = dom.wallsDteSelect ? dom.wallsDteSelect.value : "ALL";
    syncWallsSplitCheckboxState();

    var activeExpiries = allExpiries.filter(function (exp) {
      return matchesDteFilterValue(exp.dte, exp.expiry, dteSel);
    });

    if (!activeExpiries.length) {
      window.Plotly.purge(el);
      el.innerHTML = "";
      if (dom.wallsBasisLabel) {
        dom.wallsBasisLabel.textContent = "NO DATA";
      }
      return;
    }

    var effectiveMetric = metricSel;
    if (metricSel === "auto") {
      effectiveMetric = data.walls.primary_basis === "VOLUME" ? "volume" : "oi";
    }
    var splitByDte = Boolean(
      dom.wallsSplitDte &&
        dom.wallsSplitDte.checked &&
        (dteSel === "ALL" || dteSel.indexOf("BUCKET:") === 0) &&
        effectiveMetric !== "overlay"
    );

    var spot = Number(data.meta.underlying_price || 0);
    var strikeMap = {};
    for (var bIdx = 0; bIdx < activeExpiries.length; bIdx++) {
      var expObj = activeExpiries[bIdx];
      var bRows = expObj.strikes || [];
      for (var rIdx = 0; rIdx < bRows.length; rIdx++) {
        var row = bRows[rIdx];
        var key = Number(row.strike).toFixed(4);
        if (!strikeMap[key]) {
          strikeMap[key] = {
            strike: Number(row.strike),
            callOi: 0,
            putOi: 0,
            callVol: 0,
            putVol: 0,
            byExpiry: {},
          };
        }
        var entry = strikeMap[key];
        var cOi = row.call_oi || 0;
        var pOi = row.put_oi || 0;
        var cVol = row.call_volume || 0;
        var pVol = row.put_volume || 0;
        entry.callOi += cOi;
        entry.putOi += pOi;
        entry.callVol += cVol;
        entry.putVol += pVol;
        entry.byExpiry[expObj.expiry] = {
          dte: expObj.dte,
          expiryIso: formatExpiryIso(expObj.expiry),
          callOi: cOi,
          putOi: pOi,
          callVol: cVol,
          putVol: pVol,
        };
      }
    }

    var sorted = Object.keys(strikeMap)
      .map(function (k) {
        return strikeMap[k];
      })
      .sort(function (a, b) {
        return a.strike - b.strike;
      });

    var strikes = sorted.map(function (s) {
      return s.strike;
    });

    var wallMetricKey =
      effectiveMetric === "overlay"
        ? data.walls.primary_basis === "VOLUME"
          ? "volume"
          : "oi"
        : effectiveMetric;
    var callWall = resolveWallCandidate(data, activeExpiries, dteSel, wallMetricKey, "C");
    var putWall = resolveWallCandidate(data, activeExpiries, dteSel, wallMetricKey, "P");
    var sameWallStrike = Boolean(
      callWall && putWall && Math.abs(callWall.strike - putWall.strike) < 1e-6
    );
    var axisGeo = computeStrikeAxisGeometry(
      strikes,
      data.meta ? data.meta.strike_step : null,
      spot,
      callWall,
      putWall
    );

    var metricBadgeText =
      effectiveMetric === "overlay"
        ? "OI + VOLUME"
        : effectiveMetric === "volume"
        ? metricSel === "auto"
          ? "VOLUME [FALLBACK]"
          : "VOLUME"
        : "OPEN INTEREST";
    var viewBadgeText = viewMode === "grouped" ? "GROUPED" : "LADDER";
    var dteBadgeText = "";
    if (dteSel === "ALL" || dteSel.indexOf("BUCKET:") === 0) {
      var dteList = activeExpiries
        .map(function (exp) {
          return exp.dte + "d";
        })
        .join(", ");
      var firstExp = formatExpiryIso(activeExpiries[0].expiry);
      var lastExp = formatExpiryIso(activeExpiries[activeExpiries.length - 1].expiry);
      var expSpan = firstExp === lastExp ? firstExp : firstExp + ".." + lastExp;
      var bucketPrefix =
        dteSel.indexOf("BUCKET:") === 0 ? formatDteBucketLabel(dteSel) + ": " : "DTEs: ";
      dteBadgeText = bucketPrefix + dteList + " (" + expSpan + ")";
    } else {
      var onlyExp = activeExpiries[0];
      dteBadgeText = "DTE: " + onlyExp.dte + "d (EXP " + formatExpiryIso(onlyExp.expiry) + ")";
    }
    var rangeBadgePart =
      data.meta && data.meta.range_pct !== null && data.meta.range_pct !== undefined
        ? formatRangeShort(data.meta.range_pct)
        : "";
    var stepBadgePart = axisGeo.stepBadgeText || "";
    var scopePieces = [];
    if (rangeBadgePart) scopePieces.push(rangeBadgePart);
    if (stepBadgePart && stepBadgePart !== "\u2014 STEP") scopePieces.push(stepBadgePart);
    var pullScopeLabel = scopePieces.length ? " \u2022 " + scopePieces.join(" / ") : "";
    if (dom.wallsBasisLabel) {
      dom.wallsBasisLabel.textContent =
        viewBadgeText + " \u2022 " + metricBadgeText + pullScopeLabel + " \u2022 " + dteBadgeText;
    }

    var strikeMeta = sorted.map(function (s) {
      var distPct = spot > 0 ? ((s.strike - spot) / spot) * 100 : 0;
      var pctStr = (distPct >= 0 ? "+" : "") + distPct.toFixed(1) + "%";
      var lines = [];
      for (var bi = 0; bi < activeExpiries.length; bi++) {
        var ab = activeExpiries[bi];
        var bData = s.byExpiry[ab.expiry] || {
          dte: ab.dte,
          expiryIso: formatExpiryIso(ab.expiry),
          callOi: 0,
          putOi: 0,
          callVol: 0,
          putVol: 0,
        };
        var metricSummary =
          effectiveMetric === "volume"
            ? "C " + fmtInt(bData.callVol) + " / P " + fmtInt(bData.putVol) + " Vol"
            : effectiveMetric === "overlay"
            ? "OI C " +
              fmtInt(bData.callOi) +
              "/P " +
              fmtInt(bData.putOi) +
              " | Vol C " +
              fmtInt(bData.callVol) +
              "/P " +
              fmtInt(bData.putVol)
            : "C " + fmtInt(bData.callOi) + " / P " + fmtInt(bData.putOi) + " OI";
        lines.push("\u2022 " + bData.dte + "d (" + bData.expiryIso + "): " + metricSummary);
      }
      return {
        strikeStr: formatStrikeShort(s.strike),
        pctStr: pctStr,
        callOiStr: fmtInt(s.callOi),
        putOiStr: fmtInt(s.putOi),
        callVolStr: fmtInt(s.callVol),
        putVolStr: fmtInt(s.putVol),
        dteHtml: "<br><b>By DTE:</b><br>" + lines.join("<br>"),
      };
    });

    var isLadder = viewMode === "ladder";
    var traces = [];
    var barmode = "relative";
    var maxLeft = 0;
    var maxRight = 0;

    function buildCombinedHover(sideLabel, primaryLabel, metricKind) {
      return sorted.map(function (s, idx) {
        var m = strikeMeta[idx];
        var primaryVal =
          metricKind === "volume"
            ? sideLabel === "CALLS"
              ? m.callVolStr
              : m.putVolStr
            : sideLabel === "CALLS"
            ? m.callOiStr
            : m.putOiStr;
        var oppSide = sideLabel === "CALLS" ? "Puts" : "Calls";
        var oppVal =
          metricKind === "volume"
            ? sideLabel === "CALLS"
              ? m.putVolStr
              : m.callVolStr
            : sideLabel === "CALLS"
            ? m.putOiStr
            : m.callOiStr;
        var auxLine =
          metricKind === "volume"
            ? "Open Interest: C " + m.callOiStr + " / P " + m.putOiStr
            : "Session Volume: C " + m.callVolStr + " / P " + m.putVolStr;
        return [m.strikeStr, m.pctStr, primaryLabel, primaryVal, oppSide, oppVal, auxLine, m.dteHtml];
      });
    }

    var combinedHoverTemplate =
      "<b>%{fullData.name} \u2022 Strike %{customdata[0]} (%{customdata[1]} vs Spot)</b><br>" +
      "%{customdata[2]}: %{customdata[3]}<br>" +
      "Opposite %{customdata[4]}: %{customdata[5]}<br>" +
      "%{customdata[6]}" +
      "%{customdata[7]}<extra></extra>";

    function scaleSteps(factor) {
      return axisGeo.localSteps.map(function (stepVal) {
        return stepVal * factor;
      });
    }
    var ladderBarWidths = scaleSteps(0.78);
    var groupedSideWidths = scaleSteps(0.36);
    var groupedCallOffsets = scaleSteps(-0.38);
    var groupedPutOffsets = scaleSteps(0.02);

    if (effectiveMetric === "overlay") {
      barmode = "overlay";
      var callOiVals = sorted.map(function (s) {
        if (s.callOi > maxRight) maxRight = s.callOi;
        return s.callOi;
      });
      var callVolVals = sorted.map(function (s) {
        if (s.callVol > maxRight) maxRight = s.callVol;
        return s.callVol;
      });
      var putOiVals = sorted.map(function (s) {
        if (s.putOi > maxLeft) maxLeft = s.putOi;
        return isLadder ? -s.putOi : s.putOi;
      });
      var putVolVals = sorted.map(function (s) {
        if (s.putVol > maxLeft) maxLeft = s.putVol;
        return isLadder ? -s.putVol : s.putVol;
      });

      var overlayTemplate =
        "<b>%{fullData.name} \u2022 Strike %{customdata[0]} (%{customdata[1]} vs Spot)</b><br>" +
        "Calls \u2014 OI: %{customdata[2]} | Vol: %{customdata[3]}<br>" +
        "Puts \u2014 OI: %{customdata[4]} | Vol: %{customdata[5]}" +
        "%{customdata[6]}<extra></extra>";
      var overlayAllCustom = sorted.map(function (_s, idx) {
        var m = strikeMeta[idx];
        return [m.strikeStr, m.pctStr, m.callOiStr, m.callVolStr, m.putOiStr, m.putVolStr, m.dteHtml];
      });

      var groupedVolWidths = scaleSteps(0.2);
      var oiOptsCall = isLadder
        ? { width: ladderBarWidths, opacity: 0.34 }
        : { offset: groupedCallOffsets, width: groupedSideWidths, opacity: 0.34 };
      var volOptsCall = isLadder
        ? { width: scaleSteps(0.42), opacity: 1 }
        : { offset: scaleSteps(-0.3), width: groupedVolWidths, opacity: 1 };
      var oiOptsPut = isLadder
        ? { width: ladderBarWidths, opacity: 0.34 }
        : { offset: groupedPutOffsets, width: groupedSideWidths, opacity: 0.34 };
      var volOptsPut = isLadder
        ? { width: scaleSteps(0.42), opacity: 1 }
        : { offset: scaleSteps(0.1), width: groupedVolWidths, opacity: 1 };

      function makeOverlayTrace(name, vals, color, opts) {
        var tr = Object.assign(
          {
            type: "bar",
            name: name,
            marker: { color: color },
            customdata: overlayAllCustom,
            hovertemplate: overlayTemplate,
          },
          opts
        );
        if (isLadder) {
          tr.orientation = "h";
          tr.y = strikes;
          tr.x = vals;
        } else {
          tr.x = strikes;
          tr.y = vals;
        }
        return tr;
      }

      traces.push(
        makeOverlayTrace("Calls OI", callOiVals, t.call, oiOptsCall),
        makeOverlayTrace("Calls Vol", callVolVals, t.call, volOptsCall),
        makeOverlayTrace("Puts OI", putOiVals, t.put, oiOptsPut),
        makeOverlayTrace("Puts Vol", putVolVals, t.put, volOptsPut)
      );
    } else if (splitByDte) {
      barmode = "relative";
      var metricFieldPrefix = effectiveMetric === "volume" ? "Vol" : "OI";
      var splitHoverTemplate =
        "<b>%{fullData.name} \u2022 Strike %{customdata[0]} (%{customdata[1]} vs Spot)</b><br>" +
        "%{customdata[2]}: %{customdata[3]}<br>" +
        "All DTEs Total: %{customdata[4]}" +
        "%{customdata[5]}<extra></extra>";

      sorted.forEach(function (s) {
        var cTotal = effectiveMetric === "volume" ? s.callVol : s.callOi;
        var pTotal = effectiveMetric === "volume" ? s.putVol : s.putOi;
        if (cTotal > maxRight) maxRight = cTotal;
        if (pTotal > maxLeft) maxLeft = pTotal;
      });

      for (var bi = 0; bi < activeExpiries.length; bi++) {
        var bObj = activeExpiries[bi];
        var bKey = bObj.expiry;
        var expLabel = formatExpiryIso(bObj.expiry);
        var callColor = t.callDteShades[bi % t.callDteShades.length];
        var putColor = t.putDteShades[bi % t.putDteShades.length];

        var callSlice = [];
        var putSlice = [];
        var callCustom = [];
        var putCustom = [];

        for (var si = 0; si < sorted.length; si++) {
          var sRow = sorted[si];
          var sm = strikeMeta[si];
          var bInfo = sRow.byExpiry[bKey] || { callOi: 0, putOi: 0, callVol: 0, putVol: 0 };
          var cVal = effectiveMetric === "volume" ? bInfo.callVol : bInfo.callOi;
          var pVal = effectiveMetric === "volume" ? bInfo.putVol : bInfo.putOi;
          var totalLine =
            effectiveMetric === "volume"
              ? "Call Vol " + sm.callVolStr + " / Put Vol " + sm.putVolStr
              : "Call OI " + sm.callOiStr + " / Put OI " + sm.putOiStr;

          callSlice.push(cVal);
          putSlice.push(isLadder ? -pVal : pVal);
          callCustom.push([
            sm.strikeStr,
            sm.pctStr,
            bObj.dte + "d (" + expLabel + ") Call " + metricFieldPrefix,
            fmtInt(cVal),
            totalLine,
            sm.dteHtml,
          ]);
          putCustom.push([
            sm.strikeStr,
            sm.pctStr,
            bObj.dte + "d (" + expLabel + ") Put " + metricFieldPrefix,
            fmtInt(pVal),
            totalLine,
            sm.dteHtml,
          ]);
        }

        var callSplitTrace = {
          type: "bar",
          orientation: isLadder ? "h" : "v",
          name: "C " + bObj.dte + "d",
          x: isLadder ? callSlice : strikes,
          y: isLadder ? strikes : callSlice,
          offsetgroup: isLadder ? undefined : "calls",
          width: isLadder ? ladderBarWidths : groupedSideWidths,
          marker: { color: callColor },
          customdata: callCustom,
          hovertemplate: splitHoverTemplate,
        };
        var putSplitTrace = {
          type: "bar",
          orientation: isLadder ? "h" : "v",
          name: "P " + bObj.dte + "d",
          x: isLadder ? putSlice : strikes,
          y: isLadder ? strikes : putSlice,
          offsetgroup: isLadder ? undefined : "puts",
          width: isLadder ? ladderBarWidths : groupedSideWidths,
          marker: { color: putColor },
          customdata: putCustom,
          hovertemplate: splitHoverTemplate,
        };
        if (!isLadder) {
          callSplitTrace.offset = groupedCallOffsets;
          putSplitTrace.offset = groupedPutOffsets;
        }
        traces.push(callSplitTrace, putSplitTrace);
      }
    } else {
      barmode = isLadder ? "relative" : "group";
      var callTitle = effectiveMetric === "volume" ? "Call Volume" : "Call Open Interest";
      var putTitle = effectiveMetric === "volume" ? "Put Volume" : "Put Open Interest";
      var callVals = sorted.map(function (s) {
        var v = effectiveMetric === "volume" ? s.callVol : s.callOi;
        if (v > maxRight) maxRight = v;
        return v;
      });
      var putVals = sorted.map(function (s) {
        var v = effectiveMetric === "volume" ? s.putVol : s.putOi;
        if (v > maxLeft) maxLeft = v;
        return isLadder ? -v : v;
      });
      var customC = buildCombinedHover("CALLS", callTitle, effectiveMetric);
      var customP = buildCombinedHover("PUTS", putTitle, effectiveMetric);

      var callStdTrace = {
        type: "bar",
        orientation: isLadder ? "h" : "v",
        name: "Calls",
        x: isLadder ? callVals : strikes,
        y: isLadder ? strikes : callVals,
        width: isLadder ? ladderBarWidths : groupedSideWidths,
        marker: { color: t.call },
        customdata: customC,
        hovertemplate: combinedHoverTemplate,
      };
      var putStdTrace = {
        type: "bar",
        orientation: isLadder ? "h" : "v",
        name: "Puts",
        x: isLadder ? putVals : strikes,
        y: isLadder ? strikes : putVals,
        width: isLadder ? ladderBarWidths : groupedSideWidths,
        marker: { color: t.put },
        customdata: customP,
        hovertemplate: combinedHoverTemplate,
      };
      if (!isLadder) {
        callStdTrace.offset = groupedCallOffsets;
        putStdTrace.offset = groupedPutOffsets;
      }
      traces.push(callStdTrace, putStdTrace);
    }

    var showAnomalies = Boolean(dom.wallsShowAnomalies && dom.wallsShowAnomalies.checked);
    var anomTotalCount = 0;
    if (showAnomalies && data.anomalies && data.anomalies.anomalies) {
      var scopedAnoms = data.anomalies.anomalies.filter(function (a) {
        return dteSel === "ALL" || a.expiry === dteSel;
      });
      anomTotalCount = scopedAnoms.length;

      if (scopedAnoms.length > 0) {
        var strikeLookup = {};
        sorted.forEach(function (s) {
          var callLen =
            effectiveMetric === "overlay"
              ? Math.max(s.callOi, s.callVol)
              : effectiveMetric === "volume"
              ? s.callVol
              : s.callOi;
          var putLen =
            effectiveMetric === "overlay"
              ? Math.max(s.putOi, s.putVol)
              : effectiveMetric === "volume"
              ? s.putVol
              : s.putOi;
          strikeLookup[Number(s.strike).toFixed(4)] = { callLen: callLen, putLen: putLen };
        });

        var groupedAnoms = {};
        for (var ai = 0; ai < scopedAnoms.length; ai++) {
          var a = scopedAnoms[ai];
          var gKey = Number(a.strike).toFixed(4) + "|" + a.right;
          if (!groupedAnoms[gKey]) {
            groupedAnoms[gKey] = { strike: Number(a.strike), right: a.right, items: [] };
          }
          groupedAnoms[gKey].items.push(a);
        }

        var tipPad = Math.max(maxRight, maxLeft, 10) * 0.055;
        var anomX = [];
        var anomY = [];
        var anomText = [];

        Object.keys(groupedAnoms).forEach(function (gKey) {
          var grp = groupedAnoms[gKey];
          var sInfo = strikeLookup[Number(grp.strike).toFixed(4)];
          if (!sInfo) return;
          var isCall = grp.right === "C";
          var barBase = isCall ? sInfo.callLen : sInfo.putLen;
          var tipVal = barBase + tipPad;

          var lines = grp.items.map(function (item) {
            var flags = [];
            if (item.is_vol_gt_oi) flags.push("VOL>OI");
            if (item.is_high_vol_zscore) flags.push("Z-SCORE");
            var ratioTxt =
              item.vol_oi_ratio !== null && item.vol_oi_ratio !== undefined
                ? Number(item.vol_oi_ratio).toFixed(2) + "x"
                : "—";
            return (
              "\u2022 " +
              item.dte +
              "d (" +
              formatExpiryIso(item.expiry) +
              "): Vol " +
              fmtInt(item.volume) +
              " / OI " +
              fmtInt(item.open_interest) +
              " (" +
              ratioTxt +
              ") | Z=" +
              fmtNum(item.volume_zscore, 2) +
              " [" +
              flags.join(", ") +
              "]"
            );
          });

          anomText.push(
            "<b>UNUSUAL " +
              (isCall ? "CALL" : "PUT") +
              " ACTIVITY \u2022 Strike " +
              formatStrikeShort(grp.strike) +
              "</b><br>" +
              lines.join("<br>")
          );

          if (isLadder) {
            anomY.push(grp.strike);
            if (isCall) {
              anomX.push(tipVal);
              if (tipVal > maxRight) maxRight = tipVal;
            } else {
              anomX.push(-tipVal);
              if (tipVal > maxLeft) maxLeft = tipVal;
            }
          } else {
            var anomStrikeKey = Number(grp.strike).toFixed(4);
            var strikeLocalStep =
              axisGeo.localStepByKey[anomStrikeKey] !== undefined
                ? axisGeo.localStepByKey[anomStrikeKey]
                : axisGeo.modalStep;
            var sideOffset = strikeLocalStep * 0.2;
            anomX.push(isCall ? grp.strike - sideOffset : grp.strike + sideOffset);
            anomY.push(tipVal);
          }
        });

        if (anomX.length > 0) {
          traces.push({
            type: "scatter",
            mode: "markers",
            name: "Anomalies (Vol>OI / Z)",
            x: anomX,
            y: anomY,
            text: anomText,
            hoverinfo: "text",
            marker: {
              color: t.anomaly,
              size: 10,
              symbol: "diamond",
              line: { width: 1.5, color: t.ink },
            },
          });
        }
      }
    }

    if (dom.wallsBasisLabel && showAnomalies) {
      dom.wallsBasisLabel.textContent += " \u2022 ANOMALIES: " + anomTotalCount;
    }

    var markers = [
      makeWallReferenceMarker(
        viewMode,
        spot,
        "SPOT " + formatStrikeShort(spot),
        t.ink,
        isLadder ? 0.01 : 0.99,
        "dash",
        true,
        t
      ),
    ];
    if (callWall) {
      markers.push(
        makeWallReferenceMarker(
          viewMode,
          callWall.strike,
          callWall.label,
          t.call,
          isLadder ? 0.99 : 0.88,
          "dot",
          true,
          t
        )
      );
    }
    if (putWall) {
      markers.push(
        makeWallReferenceMarker(
          viewMode,
          putWall.strike,
          putWall.label,
          t.put,
          isLadder ? 0.99 : 0.14,
          "dot",
          sameWallStrike ? false : true,
          t
        )
      );
    }

    var countAxisTitle =
      effectiveMetric === "overlay"
        ? "Contracts (OI Backdrop + Session Vol)"
        : effectiveMetric === "volume"
        ? "Session Volume (Contracts)"
        : "Open Interest (Contracts)";
    var layout = isLadder
      ? baseLayout(t, "\u2190 Puts  |  " + countAxisTitle + "  |  Calls \u2192", "Strike ($)", markers)
      : baseLayout(t, "Strike ($)", countAxisTitle, markers);
    layout.barmode = barmode;
    layout.hovermode = "closest";

    var hasDecimalStrikeTicks = axisGeo.ticktext.some(function (lbl) {
      return lbl.indexOf(".") !== -1;
    });
    if (isLadder) {
      var tickCfg = buildSymmetricPositiveTicks(maxLeft, maxRight);
      layout.margin.l = hasDecimalStrikeTicks ? 68 : 58;
      layout.xaxis.tickmode = "array";
      layout.xaxis.tickvals = tickCfg.tickvals;
      layout.xaxis.ticktext = tickCfg.ticktext;
      layout.xaxis.range = tickCfg.range;
      layout.xaxis.zeroline = true;
      layout.xaxis.zerolinewidth = 1.5;
      layout.xaxis.zerolinecolor = t.ink;
      layout.yaxis.tickmode = "array";
      layout.yaxis.tickvals = axisGeo.tickvals;
      layout.yaxis.ticktext = axisGeo.ticktext;
      layout.yaxis.tickprefix = "";
      if (axisGeo.axisRange) {
        layout.yaxis.range = axisGeo.axisRange;
      }
    } else {
      layout.xaxis.tickmode = "array";
      layout.xaxis.tickvals = axisGeo.tickvals;
      layout.xaxis.ticktext = axisGeo.ticktext;
      layout.xaxis.tickprefix = "";
      if (axisGeo.axisRange) {
        layout.xaxis.range = axisGeo.axisRange;
      }
      layout.yaxis.rangemode = "nonnegative";
    }

    window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
  }

  function renderIvSmileChart(data, t) {
    var el = document.getElementById("chart-iv-smile");
    if (!el || typeof window.Plotly === "undefined") return;

    var expiries = data.profiles.expiries || [];
    var traces = [];

    for (var i = 0; i < expiries.length; i++) {
      var b = expiries[i];
      var xs = [];
      var ys = [];
      var rows = b.strikes || [];
      for (var j = 0; j < rows.length; j++) {
        var r = rows[j];
        var ivVals = [];
        if (r.call_iv !== null && r.call_iv !== undefined) ivVals.push(r.call_iv);
        if (r.put_iv !== null && r.put_iv !== undefined) ivVals.push(r.put_iv);
        if (ivVals.length > 0) {
          var meanIv =
            ivVals.reduce(function (acc, v) {
              return acc + v;
            }, 0) / ivVals.length;
          xs.push(r.strike);
          ys.push(Number((meanIv * 100).toFixed(2)));
        }
      }
      traces.push({
        type: "scatter",
        mode: "lines+markers",
        name: b.dte + "d (" + formatExpiryIso(b.expiry) + ")",
        x: xs,
        y: ys,
        line: { color: t.dtePalette[i % t.dtePalette.length], width: 2 },
        marker: { size: 5 },
      });
    }

    var markers = [
      makeVerticalMarker(data.meta.underlying_price, "SPOT", t.ink, 0.99, "dash", t),
    ];
    var layout = baseLayout(t, "Strike ($)", "Implied Volatility (%)", markers);
    window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
  }

  function renderAnomaliesChart(data, t) {
    var el = document.getElementById("chart-anomalies");
    if (!el || typeof window.Plotly === "undefined") return;

    var anomalyMap = {};
    var anoms = data.anomalies.anomalies || [];
    for (var i = 0; i < anoms.length; i++) {
      var a = anoms[i];
      anomalyMap[a.option_conid + "|" + a.expiry + "|" + a.strike + "|" + a.right] = a;
    }

    var normalX = [];
    var normalY = [];
    var normalText = [];
    var anomX = [];
    var anomY = [];
    var anomText = [];
    var anomSizes = [];

    var contracts = data.contracts || [];
    for (var j = 0; j < contracts.length; j++) {
      var c = contracts[j];
      var vol = c.volume !== null && c.volume !== undefined ? Number(c.volume) : 0;
      var oi = c.open_interest !== null && c.open_interest !== undefined ? Number(c.open_interest) : 0;
      var ratio = oi > 0 ? vol / oi : vol > 0 ? vol : 0;
      var key = c.option_conid + "|" + c.expiry + "|" + c.strike + "|" + c.right;
      var flag = anomalyMap[key];
      var label =
        c.expiry +
        " $" +
        c.strike +
        " " +
        c.right +
        " | Vol: " +
        vol +
        " / OI: " +
        oi +
        " (" +
        ratio.toFixed(2) +
        "x)";
      if (flag) {
        anomX.push(c.strike);
        anomY.push(vol);
        anomText.push(label + " | Z=" + fmtNum(flag.volume_zscore, 2));
        anomSizes.push(Math.min(22, Math.max(9, 8 + ratio * 2)));
      } else if (vol > 0) {
        normalX.push(c.strike);
        normalY.push(vol);
        normalText.push(label);
      }
    }

    var traces = [
      {
        type: "scatter",
        mode: "markers",
        name: "Standard Volume",
        x: normalX,
        y: normalY,
        text: normalText,
        hoverinfo: "text",
        marker: { color: t.muted, size: 6, opacity: 0.55 },
      },
      {
        type: "scatter",
        mode: "markers",
        name: "Unusual (Vol>OI / Z-Score)",
        x: anomX,
        y: anomY,
        text: anomText,
        hoverinfo: "text",
        marker: {
          color: t.anomaly,
          size: anomSizes.length ? anomSizes : 10,
          symbol: "diamond",
          line: { width: 1, color: t.ink },
        },
      },
    ];

    var markers = [
      makeVerticalMarker(data.meta.underlying_price, "SPOT", t.ink, 0.99, "dash", t),
    ];
    var layout = baseLayout(t, "Strike ($)", "Contract Volume", markers);
    layout.hovermode = "closest";
    window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
  }

  function renderGreeksChart(data, t) {
    var el = document.getElementById("chart-greeks");
    if (!el || typeof window.Plotly === "undefined") return;

    var metric = dom.greeksMetricSelect ? dom.greeksMetricSelect.value : "gamma";
    var expiries = data.profiles.expiries || [];
    var traces = [];

    for (var i = 0; i < expiries.length; i++) {
      var b = expiries[i];
      var xs = [];
      var ys = [];
      var rows = b.strikes || [];
      for (var j = 0; j < rows.length; j++) {
        var r = rows[j];
        var callVal = r["call_" + metric];
        var putVal = r["put_" + metric];
        var combined = null;
        if (callVal !== null && callVal !== undefined && putVal !== null && putVal !== undefined) {
          combined = metric === "delta" ? (callVal + putVal) / 2 : (callVal + putVal) / 2;
        } else if (callVal !== null && callVal !== undefined) {
          combined = callVal;
        } else if (putVal !== null && putVal !== undefined) {
          combined = putVal;
        }
        if (combined !== null) {
          xs.push(r.strike);
          ys.push(combined);
        }
      }
      traces.push({
        type: "scatter",
        mode: "lines+markers",
        name: b.dte + "d (" + formatExpiryIso(b.expiry) + ")",
        x: xs,
        y: ys,
        line: { color: t.dtePalette[i % t.dtePalette.length], width: 2 },
        marker: { size: 4 },
      });
    }

    var markers = [
      makeVerticalMarker(data.meta.underlying_price, "SPOT", t.ink, 0.99, "dash", t),
    ];
    var yTitle = "Avg Call/Put " + metric.toUpperCase();
    var layout = baseLayout(t, "Strike ($)", yTitle, markers);
    window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
  }

  // ------------------------------------------------------------------ Panel Visibility
  function readPanelVisibility() {
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

  function isPanelVisible(panelId) {
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

  function applyPanelVisibility(nextVis, options) {
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

  function renderAllCharts(data) {
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

  // ------------------------------------------------------------------ Option Chain Table
  var TABLE_COLUMNS = [
    {
      key: "expiry",
      type: "string",
      defaultDir: "asc",
      filterType: "select",
      getValue: function (r) {
        return r.expiry;
      },
    },
    {
      key: "dte",
      type: "number",
      defaultDir: "asc",
      filterType: "expr",
      getValue: function (r) {
        return r.dte;
      },
    },
    {
      key: "right",
      type: "string",
      defaultDir: "asc",
      filterType: "select",
      getValue: function (r) {
        return r.right;
      },
    },
    {
      key: "strike",
      type: "number",
      defaultDir: "asc",
      filterType: "expr",
      getValue: function (r) {
        return r.strike;
      },
    },
    {
      key: "moneyness_pct",
      type: "number",
      defaultDir: "asc",
      filterType: "expr",
      getValue: function (r) {
        return r.moneyness_pct;
      },
    },
    {
      key: "bid",
      type: "number",
      defaultDir: "desc",
      filterType: "expr",
      getValue: function (r) {
        return r.bid;
      },
    },
    {
      key: "ask",
      type: "number",
      defaultDir: "desc",
      filterType: "expr",
      getValue: function (r) {
        return r.ask;
      },
    },
    {
      key: "mark_price",
      type: "number",
      defaultDir: "desc",
      filterType: "expr",
      getValue: function (r) {
        return r.mark_price;
      },
    },
    {
      key: "volume",
      type: "number",
      defaultDir: "desc",
      filterType: "expr",
      getValue: function (r) {
        return r.volume;
      },
    },
    {
      key: "open_interest",
      type: "number",
      defaultDir: "desc",
      filterType: "expr",
      getValue: function (r) {
        return r.open_interest;
      },
    },
    {
      key: "vol_oi",
      type: "number",
      defaultDir: "desc",
      filterType: "expr",
      getValue: function (_r, volOi) {
        return volOi;
      },
    },
    {
      key: "iv",
      type: "number",
      defaultDir: "desc",
      filterType: "expr",
      getValue: function (r) {
        return r.iv !== null && r.iv !== undefined ? Number(r.iv) * 100 : null;
      },
    },
    {
      key: "delta",
      type: "number",
      defaultDir: "desc",
      filterType: "expr",
      getValue: function (r) {
        return r.delta;
      },
    },
    {
      key: "gamma",
      type: "number",
      defaultDir: "desc",
      filterType: "expr",
      getValue: function (r) {
        return r.gamma;
      },
    },
    {
      key: "theta",
      type: "number",
      defaultDir: "asc",
      filterType: "expr",
      getValue: function (r) {
        return r.theta;
      },
    },
    {
      key: "vega",
      type: "number",
      defaultDir: "desc",
      filterType: "expr",
      getValue: function (r) {
        return r.vega;
      },
    },
    {
      key: "greeks_source",
      type: "string",
      defaultDir: "asc",
      filterType: "select",
      getValue: function (r) {
        return r.greeks_source;
      },
    },
  ];

  var TABLE_COLUMN_BY_KEY = {};
  for (var tci = 0; tci < TABLE_COLUMNS.length; tci++) {
    TABLE_COLUMN_BY_KEY[TABLE_COLUMNS[tci].key] = TABLE_COLUMNS[tci];
  }

  function computeContractVolOi(cRow) {
    var vol = cRow.volume !== null && cRow.volume !== undefined ? Number(cRow.volume) : null;
    var oi =
      cRow.open_interest !== null && cRow.open_interest !== undefined
        ? Number(cRow.open_interest)
        : null;
    if (vol !== null && oi !== null && isFinite(vol) && isFinite(oi) && oi > 0) {
      return vol / oi;
    }
    return null;
  }

  function readTableSortPref() {
    try {
      var raw = window.localStorage.getItem(TABLE_SORT_KEY);
      if (raw) {
        var parsed = JSON.parse(raw);
        if (
          parsed &&
          TABLE_COLUMN_BY_KEY[parsed.col] &&
          (parsed.dir === "asc" || parsed.dir === "desc")
        ) {
          return { col: parsed.col, dir: parsed.dir };
        }
      }
    } catch (_err) {
      /* ignore storage failure */
    }
    return { col: null, dir: null };
  }

  function writeTableSortPref(sortObj) {
    try {
      if (sortObj && sortObj.col && sortObj.dir) {
        window.localStorage.setItem(TABLE_SORT_KEY, JSON.stringify(sortObj));
      } else {
        window.localStorage.removeItem(TABLE_SORT_KEY);
      }
    } catch (_err) {
      /* ignore storage failure */
    }
  }

  function syncTableHeaderSortUI() {
    if (!dom.chainTableHead) return;
    var buttons = dom.chainTableHead.querySelectorAll(".th-sort-btn[data-sort-col]");
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      var col = btn.getAttribute("data-sort-col");
      var th = btn.parentElement;
      var ind = btn.querySelector(".th-sort-ind");
      var ariaVal = "none";
      var glyph = "\u21C5";
      if (state.tableSort && state.tableSort.col === col && state.tableSort.dir) {
        ariaVal = state.tableSort.dir === "asc" ? "ascending" : "descending";
        glyph = state.tableSort.dir === "asc" ? "\u25B2" : "\u25BC";
      }
      if (th) th.setAttribute("aria-sort", ariaVal);
      if (ind) ind.textContent = glyph;
    }
  }

  function matchesNumericFilterExpr(value, rawExpr) {
    var trimmed = String(rawExpr || "").trim();
    if (!trimmed) return true;
    var clean = trimmed.replace(/%/g, "").trim();
    if (!clean) return true;

    if (value === null || value === undefined || value === "") return false;
    var num = Number(value);
    if (!isFinite(num)) return false;

    var cmpMatch = clean.match(/^(>=|<=|!=|>|<|=)\s*(-?\d+(?:\.\d+)?)$/);
    if (cmpMatch) {
      var op = cmpMatch[1];
      var rhs = Number(cmpMatch[2]);
      if (!isFinite(rhs)) return true;
      if (op === ">=") return num >= rhs;
      if (op === "<=") return num <= rhs;
      if (op === ">") return num > rhs;
      if (op === "<") return num < rhs;
      if (op === "!=") return Math.abs(num - rhs) >= 1e-6;
      return Math.abs(num - rhs) < 1e-6;
    }

    var rangeDots = clean.match(/^(-?\d+(?:\.\d+)?)\s*\.\.\s*(-?\d+(?:\.\d+)?)$/);
    var rangeDash = clean.match(/^(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)$/);
    var rangeMatch = rangeDots || rangeDash;
    if (rangeMatch) {
      var lo = Number(rangeMatch[1]);
      var hi = Number(rangeMatch[2]);
      if (!isFinite(lo) || !isFinite(hi)) return true;
      var minVal = Math.min(lo, hi);
      var maxVal = Math.max(lo, hi);
      return num >= minVal - 1e-9 && num <= maxVal + 1e-9;
    }

    var bareMatch = clean.match(/^(-?\d+(?:\.\d+)?)$/);
    if (bareMatch) {
      var target = Number(bareMatch[1]);
      if (Math.abs(num - target) < 1e-6) return true;
      return String(num).indexOf(clean) !== -1;
    }

    // Incomplete operator expression mid-keystroke (e.g. ">", "-", "30.."): do not blank table.
    return true;
  }

  function applyTableSort(entries) {
    var sortCfg = state.tableSort || {};
    if (!sortCfg.col || !sortCfg.dir) return entries;
    var colDef = TABLE_COLUMN_BY_KEY[sortCfg.col];
    if (!colDef) return entries;
    var dirMult = sortCfg.dir === "desc" ? -1 : 1;

    var copy = entries.slice();
    copy.sort(function (a, b) {
      var va = colDef.getValue(a.row, a.volOi);
      var vb = colDef.getValue(b.row, b.volOi);
      var aMissing =
        va === null ||
        va === undefined ||
        va === "" ||
        (colDef.type === "number" && !isFinite(Number(va)));
      var bMissing =
        vb === null ||
        vb === undefined ||
        vb === "" ||
        (colDef.type === "number" && !isFinite(Number(vb)));

      // Missing values always sink to the bottom regardless of asc/desc.
      if (aMissing && bMissing) return 0;
      if (aMissing) return 1;
      if (bMissing) return -1;

      var cmp = 0;
      if (colDef.type === "number") {
        cmp = Number(va) - Number(vb);
      } else {
        var sa = String(va);
        var sb = String(vb);
        cmp = sa < sb ? -1 : sa > sb ? 1 : 0;
      }
      if (cmp !== 0) return cmp * dirMult;

      // Deterministic tie-breaker: (dte asc, strike asc, right asc)
      var dteDiff = (Number(a.row.dte) || 0) - (Number(b.row.dte) || 0);
      if (dteDiff !== 0) return dteDiff;
      var strikeDiff = (Number(a.row.strike) || 0) - (Number(b.row.strike) || 0);
      if (strikeDiff !== 0) return strikeDiff;
      return a.row.right < b.row.right ? -1 : a.row.right > b.row.right ? 1 : 0;
    });
    return copy;
  }

  function updateHeaderFilterHighlights() {
    if (!dom.chainTableHead) return;
    var controls = dom.chainTableHead.querySelectorAll("[data-filter-col]");
    for (var i = 0; i < controls.length; i++) {
      var ctrl = controls[i];
      var col = ctrl.getAttribute("data-filter-col");
      var active = Boolean(col && state.tableFilters[col]);
      ctrl.classList.toggle("is-filtered", active);
    }
  }

  function populateTableHeaderFilters(data) {
    if (dom.thFilterExpiry) {
      var curExp = state.tableFilters.expiry || "";
      dom.thFilterExpiry.innerHTML = '<option value="">All</option>';
      var exps =
        data && data.profiles && data.profiles.expiries ? data.profiles.expiries.slice() : [];
      exps.sort(function (a, b) {
        return a.dte - b.dte;
      });
      var expMatched = false;
      for (var i = 0; i < exps.length; i++) {
        var opt = document.createElement("option");
        opt.value = exps[i].expiry;
        opt.textContent = formatExpiryIso(exps[i].expiry) + " (" + exps[i].dte + "d)";
        if (curExp && curExp === exps[i].expiry) {
          opt.selected = true;
          expMatched = true;
        }
        dom.thFilterExpiry.appendChild(opt);
      }
      if (curExp && !expMatched) {
        state.tableFilters.expiry = "";
      }
    }

    if (dom.thFilterSource) {
      var curSrc = state.tableFilters.greeks_source || "";
      var seenSrc = { IBKR_MODEL: true, BSM_FALLBACK: true, NONE: true };
      if (data && data.contracts) {
        for (var j = 0; j < data.contracts.length; j++) {
          var src = data.contracts[j].greeks_source;
          if (src) seenSrc[src] = true;
        }
      }
      dom.thFilterSource.innerHTML = '<option value="">All</option>';
      var orderedSources = ["IBKR_MODEL", "BSM_FALLBACK", "NONE"];
      Object.keys(seenSrc).forEach(function (k) {
        if (orderedSources.indexOf(k) === -1) orderedSources.push(k);
      });
      for (var s = 0; s < orderedSources.length; s++) {
        var sOpt = document.createElement("option");
        sOpt.value = orderedSources[s];
        sOpt.textContent = orderedSources[s];
        if (curSrc === orderedSources[s]) sOpt.selected = true;
        dom.thFilterSource.appendChild(sOpt);
      }
    }
    updateHeaderFilterHighlights();
  }

  function resetAllTableFilters() {
    state.tableFilters = {};
    state.tableSort = { col: null, dir: null };
    writeTableSortPref(state.tableSort);
    if (dom.chainTableHead) {
      var ctrls = dom.chainTableHead.querySelectorAll("[data-filter-col]");
      for (var i = 0; i < ctrls.length; i++) {
        ctrls[i].value = "";
        ctrls[i].classList.remove("is-filtered");
      }
    }
    if (dom.filterDte) dom.filterDte.value = "ALL";
    if (dom.filterRight) dom.filterRight.value = "ALL";
    if (dom.filterUnusual) dom.filterUnusual.checked = false;
    syncDteBucketTabs(state.chainData);
    syncTableHeaderSortUI();
    renderTable(state.chainData);
  }

  function syncDteBucketTabs(data) {
    if (!dom.dteBucketTabs) return;
    var activeVal = dom.filterDte ? dom.filterDte.value : "ALL";
    var expiries =
      data && data.profiles && data.profiles.expiries ? data.profiles.expiries : [];
    var buttons = dom.dteBucketTabs.querySelectorAll("[data-dte-bucket]");
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      var bucketVal = btn.getAttribute("data-dte-bucket") || "ALL";
      var hasRows = bucketVal === "ALL";
      if (!hasRows) {
        for (var e = 0; e < expiries.length; e++) {
          if (matchesDteFilterValue(expiries[e].dte, expiries[e].expiry, bucketVal)) {
            hasRows = true;
            break;
          }
        }
      }
      btn.disabled = expiries.length > 0 && !hasRows;
      var isActive = activeVal === bucketVal;
      btn.classList.toggle("active", isActive);
      btn.setAttribute("aria-pressed", isActive ? "true" : "false");
    }
  }

  function populateDteFilter(data) {
    if (!dom.filterDte) return;
    var rawPrev = dom.filterDte.value || "ALL";
    dom.filterDte.innerHTML = '<option value="ALL">All DTEs</option>';
    var expiries =
      data && data.profiles && data.profiles.expiries ? data.profiles.expiries.slice() : [];
    expiries.sort(function (a, b) {
      return a.dte - b.dte;
    });
    var previous = snapDteValueToExpiries(rawPrev, expiries, {
      allowBuckets: true,
      emptyValue: "ALL",
    });
    if (expiries.length) {
      appendDteBucketOptions(dom.filterDte, expiries, previous);
      var expGroup = document.createElement("optgroup");
      expGroup.label = "Expirations";
      for (var i = 0; i < expiries.length; i++) {
        var exp = expiries[i];
        var opt = document.createElement("option");
        opt.value = exp.expiry;
        opt.textContent = exp.dte + "d (" + formatExpiryIso(exp.expiry) + ")";
        if (opt.value === previous) opt.selected = true;
        expGroup.appendChild(opt);
      }
      dom.filterDte.appendChild(expGroup);
    }
    dom.filterDte.value = previous || "ALL";
    if (dom.thFilterExpiry && previous !== rawPrev) {
      var syncedExp = previous === "ALL" || previous.indexOf("BUCKET:") === 0 ? "" : previous;
      dom.thFilterExpiry.value = syncedExp;
      state.tableFilters.expiry = syncedExp;
      updateHeaderFilterHighlights();
    }
    syncDteBucketTabs(data);
  }

  function renderTable(data) {
    syncTableHeaderSortUI();
    if (!dom.tableBody) return;
    if (!data || !data.contracts || !data.contracts.length) {
      dom.tableBody.innerHTML =
        '<tr><td colspan="18" class="text-left">No cached contracts available. Click REFRESH FROM IBKR.</td></tr>';
      if (dom.tableRowCount) dom.tableRowCount.textContent = "0 CONTRACTS";
      return;
    }

    var anomalyMap = {};
    var anoms = (data.anomalies && data.anomalies.anomalies) || [];
    for (var a = 0; a < anoms.length; a++) {
      var item = anoms[a];
      anomalyMap[item.expiry + "|" + item.strike + "|" + item.right] = item;
    }

    var dteFilter = dom.filterDte && dom.filterDte.value ? dom.filterDte.value : "ALL";
    var rightFilter = dom.filterRight && dom.filterRight.value ? dom.filterRight.value : "ALL";
    var unusualOnly = Boolean(dom.filterUnusual && dom.filterUnusual.checked);
    var activeFilterKeys = Object.keys(state.tableFilters).filter(function (k) {
      return Boolean(state.tableFilters[k]);
    });
    var spot = data.meta.underlying_price;

    var filtered = [];
    for (var i = 0; i < data.contracts.length; i++) {
      var c = data.contracts[i];
      if (!matchesDteFilterValue(c.dte, c.expiry, dteFilter)) continue;
      if (rightFilter !== "ALL" && c.right !== rightFilter) continue;
      var aKey = c.expiry + "|" + c.strike + "|" + c.right;
      var flagged = anomalyMap[aKey];
      if (unusualOnly && !flagged) continue;

      var volOi = computeContractVolOi(c);
      var headerPass = true;
      for (var fk = 0; fk < activeFilterKeys.length; fk++) {
        var fKey = activeFilterKeys[fk];
        var colDef = TABLE_COLUMN_BY_KEY[fKey];
        if (!colDef) continue;
        var fExpr = state.tableFilters[fKey];
        var cellVal = colDef.getValue(c, volOi);
        if (colDef.filterType === "select") {
          if (String(cellVal || "") !== String(fExpr)) {
            headerPass = false;
            break;
          }
        } else if (!matchesNumericFilterExpr(cellVal, fExpr)) {
          headerPass = false;
          break;
        }
      }
      if (!headerPass) continue;
      filtered.push({ row: c, anomaly: flagged || null, volOi: volOi });
    }

    filtered = applyTableSort(filtered);

    if (dom.tableRowCount) {
      dom.tableRowCount.textContent =
        filtered.length + " OF " + data.contracts.length + " CONTRACTS";
    }

    if (!filtered.length) {
      dom.tableBody.innerHTML =
        '<tr><td colspan="18" class="text-left">No contracts match the active table filters.</td></tr>';
      return;
    }

    var html = [];
    for (var r = 0; r < filtered.length; r++) {
      var cRow = filtered[r].row;
      var flag = filtered[r].anomaly;
      var ratio = filtered[r].volOi;
      var isAtm = spot > 0 && Math.abs((cRow.strike - spot) / spot) <= 0.015;
      var classes = [];
      if (isAtm) classes.push("row-atm");
      if (flag) classes.push("row-anomaly");

      var vol = cRow.volume !== null && cRow.volume !== undefined ? Number(cRow.volume) : null;
      var oi =
        cRow.open_interest !== null && cRow.open_interest !== undefined
          ? Number(cRow.open_interest)
          : null;
      var ratioWidth = ratio !== null ? Math.min(100, Math.round(ratio * 40)) : 0;
      var ratioLabel = ratio !== null ? ratio.toFixed(2) + "x" : "—";
      var flagTags = "";
      if (flag) {
        if (flag.is_vol_gt_oi) flagTags += '<span class="anomaly-tag">V&gt;OI</span>';
        if (flag.is_high_vol_zscore) flagTags += '<span class="anomaly-tag">Z</span>';
      }

      html.push(
        '<tr class="' +
          classes.join(" ") +
          '">' +
          '<td class="text-left">' +
          escapeHtml(cRow.expiry) +
          "</td>" +
          "<td>" +
          escapeHtml(cRow.dte) +
          "d</td>" +
          '<td class="text-left"><span class="right-pill right-pill--' +
          escapeHtml(cRow.right) +
          '">' +
          escapeHtml(cRow.right) +
          "</span>" +
          flagTags +
          "</td>" +
          "<td>$" +
          fmtNum(cRow.strike, 2) +
          "</td>" +
          "<td>" +
          (cRow.moneyness_pct >= 0 ? "+" : "") +
          fmtNum(cRow.moneyness_pct, 1) +
          "%</td>" +
          "<td>" +
          fmtNum(cRow.bid, 2) +
          "</td>" +
          "<td>" +
          fmtNum(cRow.ask, 2) +
          "</td>" +
          "<td>" +
          fmtNum(cRow.mark_price, 2) +
          "</td>" +
          "<td>" +
          fmtInt(vol) +
          "</td>" +
          "<td>" +
          fmtInt(oi) +
          "</td>" +
          '<td><span class="vol-oi-cell"><span>' +
          ratioLabel +
          '</span><span class="vol-oi-bar"><span class="vol-oi-fill" style="width:' +
          ratioWidth +
          '%"></span></span></span></td>' +
          "<td>" +
          (cRow.iv !== null && cRow.iv !== undefined ? (cRow.iv * 100).toFixed(1) + "%" : "—") +
          "</td>" +
          "<td>" +
          fmtNum(cRow.delta, 3) +
          "</td>" +
          "<td>" +
          fmtNum(cRow.gamma, 4) +
          "</td>" +
          "<td>" +
          fmtNum(cRow.theta, 3) +
          "</td>" +
          "<td>" +
          fmtNum(cRow.vega, 3) +
          "</td>" +
          '<td class="text-left"><span class="source-tag">' +
          escapeHtml(cRow.greeks_source) +
          "</span></td>" +
          '<td class="chain-leg-col"><button type="button" class="btn btn-xs chain-add-leg-btn" data-leg-expiry="' +
          escapeHtml(cRow.expiry) +
          '" data-leg-right="' +
          escapeHtml(cRow.right) +
          '" data-leg-strike="' +
          escapeHtml(cRow.strike) +
          '" title="Add this contract to Strategy Lab">+Leg</button></td>' +
          "</tr>"
      );
    }
    dom.tableBody.innerHTML = html.join("");
  }

  // ------------------------------------------------------------------ Snapshot Dropdown
  function renderSnapshotSelect(payload) {
    if (!dom.snapshotSelect) return;
    var items = (payload && payload.snapshots) || [];
    state.snapshots = items;
    dom.snapshotSelect.innerHTML = "";
    if (!items.length) {
      var emptyOpt = document.createElement("option");
      emptyOpt.value = "";
      emptyOpt.textContent = "No snapshots archived";
      dom.snapshotSelect.appendChild(emptyOpt);
      state.snapshotId = "";
      updateExportLink();
      return;
    }

    for (var i = 0; i < items.length; i++) {
      var snap = items[i];
      var opt = document.createElement("option");
      opt.value = snap.snapshot_id;
      var metaBits = [
        snap.market_date,
        "T" + snap.market_data_type,
      ];
      if (snap.range_pct !== null && snap.range_pct !== undefined) {
        metaBits.push(formatRangeShort(snap.range_pct));
      }
      if (snap.strike_step !== null && snap.strike_step !== undefined) {
        metaBits.push(formatStepShort(snap.strike_step));
      }
      metaBits.push(snap.contract_count + "c");
      opt.textContent = snap.snapshot_id + " (" + metaBits.join(" \u2022 ") + ")";
      if (state.snapshotId ? snap.snapshot_id === state.snapshotId : i === 0) {
        opt.selected = true;
      }
      dom.snapshotSelect.appendChild(opt);
    }
    if (!state.snapshotId && items.length) {
      state.snapshotId = items[0].snapshot_id;
    }
    updateExportLink();
  }

  // ------------------------------------------------------------------ Strategy Lab & T+n Payoff
  function strategySortedExpiries(data) {
    if (!data || !data.contracts || !data.contracts.length) return [];
    var seen = {};
    var out = [];
    for (var i = 0; i < data.contracts.length; i++) {
      var c = data.contracts[i];
      if (!seen[c.expiry]) {
        seen[c.expiry] = true;
        out.push({ expiry: c.expiry, dte: Number(c.dte) });
      }
    }
    out.sort(function (a, b) {
      return a.dte - b.dte || String(a.expiry).localeCompare(String(b.expiry));
    });
    return out;
  }

  function strategyStrikesFor(data, expiry, right) {
    if (!data || !data.contracts) return [];
    var seen = {};
    var strikes = [];
    for (var i = 0; i < data.contracts.length; i++) {
      var c = data.contracts[i];
      if (String(c.expiry) === String(expiry) && String(c.right) === String(right)) {
        var k = Number(c.strike);
        if (!seen[k]) {
          seen[k] = true;
          strikes.push(k);
        }
      }
    }
    strikes.sort(function (a, b) {
      return a - b;
    });
    return strikes;
  }

  function findLocalChainContract(data, expiry, right, strike) {
    if (!data || !data.contracts) return null;
    var targetK = Number(strike);
    for (var i = 0; i < data.contracts.length; i++) {
      var c = data.contracts[i];
      if (
        String(c.expiry) === String(expiry) &&
        String(c.right) === String(right) &&
        Math.abs(Number(c.strike) - targetK) < 1e-4
      ) {
        return c;
      }
    }
    return null;
  }

  function nearestStrikeFromList(strikes, targetStrike) {
    if (!strikes || !strikes.length) return null;
    var best = strikes[0];
    var bestDist = Math.abs(best - targetStrike);
    for (var i = 1; i < strikes.length; i++) {
      var d = Math.abs(strikes[i] - targetStrike);
      if (d < bestDist) {
        best = strikes[i];
        bestDist = d;
      }
    }
    return best;
  }

  function setActiveStrategyPresetUI(kind) {
    state.strategyKind = kind;
    if (!dom.strategyPresetPills) return;
    var btns = dom.strategyPresetPills.querySelectorAll("[data-strategy-preset]");
    for (var i = 0; i < btns.length; i++) {
      var btn = btns[i];
      var isMatch = btn.getAttribute("data-strategy-preset") === kind;
      btn.classList.toggle("active", isMatch);
      btn.setAttribute("aria-pressed", isMatch ? "true" : "false");
    }
  }

  function buildSeedLegsFromChain(data) {
    var exps = strategySortedExpiries(data);
    if (!exps.length) return [];
    var chosenExp = exps[0].expiry;
    if (dom.strategyExpirySelect && dom.strategyExpirySelect.value) {
      for (var i = 0; i < exps.length; i++) {
        if (exps[i].expiry === dom.strategyExpirySelect.value) {
          chosenExp = exps[i].expiry;
          break;
        }
      }
    }
    var puts = strategyStrikesFor(data, chosenExp, "P");
    if (!puts.length) {
      var calls = strategyStrikesFor(data, chosenExp, "C");
      if (!calls.length) return [];
      return [
        { side: "BUY", right: "C", expiry: chosenExp, strike: calls[0], qty: 1 },
      ];
    }
    var spot = Number(data.meta.underlying_price || 100);
    var pw =
      data.walls && data.walls.aggregate_primary_put_wall
        ? Number(data.walls.aggregate_primary_put_wall.strike)
        : spot * 0.95;
    var shortK = nearestStrikeFromList(puts, pw);
    var lowerPuts = puts.filter(function (k) {
      return k < shortK - 1e-6;
    });
    var longK = lowerPuts.length ? lowerPuts[lowerPuts.length - 1] : shortK;
    if (Math.abs(longK - shortK) < 1e-6) {
      return [{ side: "SELL", right: "P", expiry: chosenExp, strike: shortK, qty: 1 }];
    }
    return [
      { side: "SELL", right: "P", expiry: chosenExp, strike: shortK, qty: 1 },
      { side: "BUY", right: "P", expiry: chosenExp, strike: longK, qty: 1 },
    ];
  }

  function renderStrategySlidersMax(maxDays) {
    if (dom.strategyDaysSlider) {
      var cap = Math.max(1, Number(maxDays || 120));
      dom.strategyDaysSlider.max = String(cap);
      if (Number(dom.strategyDaysSlider.value) > cap) {
        dom.strategyDaysSlider.value = String(cap);
      }
      if (dom.strategyDaysReadout) {
        dom.strategyDaysReadout.textContent = "T+" + dom.strategyDaysSlider.value + "d";
      }
    }
    if (dom.strategyIvSlider && dom.strategyIvReadout) {
      var ivVal = Number(dom.strategyIvSlider.value || 0);
      dom.strategyIvReadout.textContent = (ivVal >= 0 ? "+" : "") + ivVal + "%";
    }
  }

  function resolveLocalContractMid(contract) {
    if (!contract) return null;
    var bidVal = contract.bid;
    var askVal = contract.ask;
    if (
      bidVal !== null &&
      bidVal !== undefined &&
      askVal !== null &&
      askVal !== undefined &&
      Number(bidVal) > 0 &&
      Number(askVal) >= Number(bidVal)
    ) {
      return (Number(bidVal) + Number(askVal)) / 2.0;
    }
    var keys = ["mid", "mark_price", "last", "close"];
    for (var i = 0; i < keys.length; i++) {
      var val = contract[keys[i]];
      if (val !== null && val !== undefined && Number(val) > 0) {
        return Number(val);
      }
    }
    if (contract.mid !== null && contract.mid !== undefined) {
      return Number(contract.mid);
    }
    return null;
  }

  function findExactEvaluatedLeg(leg, evaluatedLegs) {
    if (!leg || !Array.isArray(evaluatedLegs)) return null;
    for (var i = 0; i < evaluatedLegs.length; i++) {
      var evLeg = evaluatedLegs[i];
      if (
        String(evLeg.expiry) === String(leg.expiry) &&
        String(evLeg.right) === String(leg.right) &&
        Math.abs(Number(evLeg.strike) - Number(leg.strike)) < 1e-4
      ) {
        return evLeg;
      }
    }
    return null;
  }

  function formatLegQuoteCells(localMatch, evalFallback) {
    var srcBid =
      localMatch && localMatch.bid !== null && localMatch.bid !== undefined
        ? localMatch.bid
        : evalFallback
          ? evalFallback.bid
          : null;
    var srcAsk =
      localMatch && localMatch.ask !== null && localMatch.ask !== undefined
        ? localMatch.ask
        : evalFallback
          ? evalFallback.ask
          : null;
    var localMid = resolveLocalContractMid(localMatch);
    var srcMid =
      localMid !== null && localMid !== undefined
        ? localMid
        : evalFallback
          ? evalFallback.mid
          : null;
    var srcDelta =
      localMatch && localMatch.delta !== null && localMatch.delta !== undefined
        ? localMatch.delta
        : evalFallback
          ? evalFallback.delta
          : null;
    var srcIv =
      localMatch && localMatch.iv !== null && localMatch.iv !== undefined
        ? localMatch.iv
        : evalFallback
          ? evalFallback.iv
          : null;

    var bidAskText =
      (srcBid !== null && srcBid !== undefined ? fmtNum(srcBid, 2) : "\u2014") +
      " / " +
      (srcAsk !== null && srcAsk !== undefined ? fmtNum(srcAsk, 2) : "\u2014");
    var midText = srcMid !== null && srcMid !== undefined ? fmtMoney(srcMid) : "\u2014";
    var dTxt = srcDelta !== null && srcDelta !== undefined ? fmtNum(srcDelta, 2) : "\u2014";
    var ivTxt =
      srcIv !== null && srcIv !== undefined ? fmtNum(Number(srcIv) * 100, 1) + "%" : "\u2014";
    return {
      bidAskText: bidAskText,
      midText: midText,
      greeksText: dTxt + " \u2022 " + ivTxt,
    };
  }

  function snapLegToValidContract(data, leg, preferAxis) {
    if (!data || !leg) return true;
    var directStrikes = strategyStrikesFor(data, leg.expiry, leg.right);
    if (directStrikes.length) {
      leg.strike = nearestStrikeFromList(directStrikes, leg.strike);
      return true;
    }
    if (preferAxis === "expiry") {
      var oppRight = leg.right === "C" ? "P" : "C";
      var oppStrikes = strategyStrikesFor(data, leg.expiry, oppRight);
      if (oppStrikes.length) {
        leg.right = oppRight;
        leg.strike = nearestStrikeFromList(oppStrikes, leg.strike);
        return true;
      }
    }
    var exps = strategySortedExpiries(data);
    for (var i = 0; i < exps.length; i++) {
      var candStrikes = strategyStrikesFor(data, exps[i].expiry, leg.right);
      if (candStrikes.length) {
        leg.expiry = exps[i].expiry;
        leg.strike = nearestStrikeFromList(candStrikes, leg.strike);
        return true;
      }
    }
    return false;
  }

  function syncStrategyActionButtons() {
    var hasChain = Boolean(state.chainData);
    var hasLegs = Boolean(state.strategyLegs && state.strategyLegs.length > 0);
    if (dom.strategyAutoBuildBtn) {
      dom.strategyAutoBuildBtn.disabled = Boolean(state.strategyBusy || !hasChain);
    }
    if (dom.strategyEvaluateBtn) {
      dom.strategyEvaluateBtn.disabled = Boolean(state.strategyBusy || !hasChain || !hasLegs);
      dom.strategyEvaluateBtn.title = !hasLegs
        ? "Add at least one option leg first (+ Leg or Auto-Build)"
        : "Evaluate or re-price exact current legs table using cached or live Corridor quotes";
    }
    if (dom.strategyAddLegBtn) {
      dom.strategyAddLegBtn.disabled = Boolean(!hasChain);
    }
  }

  function clearStrategyKpis(subHint, basisText, rationaleText) {
    if (dom.strategyBasisBadge && basisText) {
      dom.strategyBasisBadge.textContent = basisText;
    }
    if (dom.strategyKpiPrice) {
      dom.strategyKpiPrice.textContent = "\u2014";
      dom.strategyKpiPrice.classList.remove("is-credit", "is-debit");
    }
    if (dom.strategyKpiPriceSub) {
      dom.strategyKpiPriceSub.textContent = subHint || "Click a preset or Auto-Build";
    }
    if (dom.strategyKpiSlip) dom.strategyKpiSlip.textContent = "\u2014";
    if (dom.strategyKpiSlipSub) dom.strategyKpiSlipSub.textContent = "Nat \u2192 Fair \u2192 Mid";
    if (dom.strategyKpiRisk) dom.strategyKpiRisk.textContent = "\u2014";
    if (dom.strategyKpiRiskSub) dom.strategyKpiRiskSub.textContent = "RoR %";
    if (dom.strategyKpiPop) dom.strategyKpiPop.textContent = "\u2014";
    if (dom.strategyKpiPopSub) dom.strategyKpiPopSub.textContent = "1\u03c3 expected move";
    if (dom.strategyKpiGreeks) dom.strategyKpiGreeks.textContent = "\u2014";
    if (dom.strategyKpiGreeksSub) {
      dom.strategyKpiGreeksSub.textContent = "\u0394 / \u0393 / \u0398 / V";
    }
    if (dom.strategyRationale && rationaleText !== undefined) {
      dom.strategyRationale.textContent = rationaleText;
    }
  }

  function markStrategyEvaluationDirty() {
    state.strategyEvaluationDirty = true;
    if (dom.strategyBasisBadge && state.chainData && state.strategyLegs.length) {
      dom.strategyBasisBadge.textContent = state.strategyEvaluation
        ? "CUSTOM EDITS \u2022 CLICK EVALUATE / RE-PRICE"
        : "READY \u2022 CLICK EVALUATE / RE-PRICE";
    }
    syncStrategyActionButtons();
  }

  function resetStrategyEvaluationUI(keepChainLegs) {
    state.strategyEvaluation = null;
    state.strategyEvaluationDirty = false;
    state.strategyPendingEvaluate = null;
    if (dom.strategyDaysSlider) dom.strategyDaysSlider.value = "0";
    if (dom.strategyIvSlider) dom.strategyIvSlider.value = "0";
    renderStrategySlidersMax(120);

    var basisTxt = state.chainData
      ? "IDLE \u2022 CLICK TO BUILD"
      : "NO SNAPSHOT LOADED";
    var ratTxt = state.chainData
      ? "Cached snapshot ready. Click any strategy preset or Auto-Build from Walls, or edit legs below and click Evaluate / Re-Price."
      : "Load or refresh a ticker snapshot first to build option strategies.";
    clearStrategyKpis("Click a preset or Auto-Build", basisTxt, ratTxt);

    if (!state.chainData) {
      state.strategyLegs = [];
    } else if (!keepChainLegs) {
      state.strategyLegs = buildSeedLegsFromChain(state.chainData);
    }
    if (keepChainLegs && state.chainData && !state.strategyLegs.length) {
      clearStrategyKpis(
        "Add a leg or click Auto-Build",
        "NO LEGS \u2022 CLICK + LEG",
        "No legs configured. Click + Leg or Auto-Build from Walls."
      );
    }
    renderStrategyLegsTable();
    renderStrategyEmptyPlaceholder();
  }

  function syncStrategySelectors(data) {
    var nextTicker = data && data.meta ? String(data.meta.underlying || "") : "";
    var sameTicker = Boolean(nextTicker && state.strategyLoadedTicker === nextTicker);
    state.strategyLoadedTicker = nextTicker;

    if (dom.strategyExpirySelect) {
      var prev = dom.strategyExpirySelect.value;
      dom.strategyExpirySelect.innerHTML = "";
      var autoOpt = document.createElement("option");
      autoOpt.value = "";
      autoOpt.textContent = "Auto (Front)";
      dom.strategyExpirySelect.appendChild(autoOpt);
      var exps = strategySortedExpiries(data);
      var snappedExp = snapDteValueToExpiries(prev, exps, {
        allowBuckets: false,
        emptyValue: "",
      });
      for (var i = 0; i < exps.length; i++) {
        var opt = document.createElement("option");
        opt.value = exps[i].expiry;
        opt.textContent = formatExpiryIso(exps[i].expiry) + " (" + exps[i].dte + "d)";
        if (snappedExp && snappedExp === exps[i].expiry) {
          opt.selected = true;
        }
        dom.strategyExpirySelect.appendChild(opt);
      }
      if (snappedExp) {
        dom.strategyExpirySelect.value = snappedExp;
      }
    }

    if (sameTicker && data && state.strategyKind === "CUSTOM" && state.strategyLegs.length) {
      var kept = [];
      for (var j = 0; j < state.strategyLegs.length; j++) {
        var leg = state.strategyLegs[j];
        if (
          findLocalChainContract(data, leg.expiry, leg.right, leg.strike) ||
          snapLegToValidContract(data, leg, "expiry")
        ) {
          kept.push(leg);
        }
      }
      state.strategyLegs = kept;
      if (state.strategyLegs.length) {
        markStrategyEvaluationDirty();
        renderStrategyLegsTable();
        renderStrategyEmptyPlaceholder();
        return;
      }
    }

    if (!sameTicker && state.strategyKind === "CUSTOM") {
      setActiveStrategyPresetUI("BULL_PUT_SPREAD");
    }
    resetStrategyEvaluationUI(false);
  }

  function renderStrategyLegsTable() {
    var tbody = dom.strategyLegsTbody;
    if (!tbody) return;
    tbody.innerHTML = "";
    syncStrategyActionButtons();
    if (!state.strategyLegs.length) {
      var emptyTr = document.createElement("tr");
      var emptyTd = document.createElement("td");
      emptyTd.colSpan = 9;
      emptyTd.textContent = state.chainData
        ? "No legs configured. Click + Leg or Auto-Build from Walls."
        : "Waiting for cached option snapshot.";
      emptyTr.appendChild(emptyTd);
      tbody.appendChild(emptyTr);
      return;
    }

    var exps = strategySortedExpiries(state.chainData);
    var evaluatedLegs =
      state.strategyEvaluation && state.strategyEvaluation.legs
        ? state.strategyEvaluation.legs
        : null;

    for (var i = 0; i < state.strategyLegs.length; i++) {
      (function (idx) {
        var leg = state.strategyLegs[idx];
        var localMatch = findLocalChainContract(
          state.chainData,
          leg.expiry,
          leg.right,
          leg.strike
        );
        var evalMatch = findExactEvaluatedLeg(leg, evaluatedLegs);
        var tr = document.createElement("tr");

        // 1. Side
        var tdSide = document.createElement("td");
        var selSide = document.createElement("select");
        selSide.className =
          "select-control strategy-leg-side " + (leg.side === "SELL" ? "is-sell" : "is-buy");
        selSide.setAttribute("aria-label", "Leg " + (idx + 1) + " side");
        ["BUY", "SELL"].forEach(function (sv) {
          var o = document.createElement("option");
          o.value = sv;
          o.textContent = sv;
          o.selected = leg.side === sv;
          selSide.appendChild(o);
        });
        selSide.addEventListener("change", function () {
          state.strategyLegs[idx].side = selSide.value;
          setActiveStrategyPresetUI("CUSTOM");
          markStrategyEvaluationDirty();
          renderStrategyLegsTable();
        });
        tdSide.appendChild(selSide);
        tr.appendChild(tdSide);

        // 2. Right
        var tdRight = document.createElement("td");
        var selRight = document.createElement("select");
        selRight.className = "select-control";
        selRight.setAttribute("aria-label", "Leg " + (idx + 1) + " right");
        ["P", "C"].forEach(function (rv) {
          var o = document.createElement("option");
          o.value = rv;
          o.textContent = rv === "C" ? "CALL" : "PUT";
          o.selected = leg.right === rv;
          selRight.appendChild(o);
        });
        selRight.addEventListener("change", function () {
          var prevRight = state.strategyLegs[idx].right;
          state.strategyLegs[idx].right = selRight.value;
          if (!snapLegToValidContract(state.chainData, state.strategyLegs[idx], "right")) {
            state.strategyLegs[idx].right = prevRight;
            showBanner("warn", "No " + (selRight.value === "C" ? "CALL" : "PUT") + " contracts found in snapshot.");
          }
          setActiveStrategyPresetUI("CUSTOM");
          markStrategyEvaluationDirty();
          renderStrategyLegsTable();
        });
        tdRight.appendChild(selRight);
        tr.appendChild(tdRight);

        // 3. Expiry
        var tdExp = document.createElement("td");
        var selExp = document.createElement("select");
        selExp.className = "select-control";
        selExp.setAttribute("aria-label", "Leg " + (idx + 1) + " expiry");
        var expList = exps.length ? exps : [{ expiry: leg.expiry, dte: 0 }];
        for (var e = 0; e < expList.length; e++) {
          var oe = document.createElement("option");
          oe.value = expList[e].expiry;
          oe.textContent = formatExpiryIso(expList[e].expiry) + " (" + expList[e].dte + "d)";
          oe.selected = expList[e].expiry === leg.expiry;
          selExp.appendChild(oe);
        }
        selExp.addEventListener("change", function () {
          state.strategyLegs[idx].expiry = selExp.value;
          snapLegToValidContract(state.chainData, state.strategyLegs[idx], "expiry");
          setActiveStrategyPresetUI("CUSTOM");
          markStrategyEvaluationDirty();
          renderStrategyLegsTable();
        });
        tdExp.appendChild(selExp);
        tr.appendChild(tdExp);

        // 4. Strike
        var tdStrike = document.createElement("td");
        var selStrike = document.createElement("select");
        selStrike.className = "select-control";
        selStrike.setAttribute("aria-label", "Leg " + (idx + 1) + " strike");
        var strikes = strategyStrikesFor(state.chainData, leg.expiry, leg.right);
        if (!strikes.length && leg.strike) {
          strikes = [Number(leg.strike)];
        }
        for (var k = 0; k < strikes.length; k++) {
          var ok = document.createElement("option");
          ok.value = String(strikes[k]);
          ok.textContent = formatStrikeShort(strikes[k]);
          ok.selected = Math.abs(strikes[k] - Number(leg.strike)) < 1e-4;
          selStrike.appendChild(ok);
        }
        selStrike.addEventListener("change", function () {
          state.strategyLegs[idx].strike = Number(selStrike.value);
          setActiveStrategyPresetUI("CUSTOM");
          markStrategyEvaluationDirty();
          renderStrategyLegsTable();
        });
        tdStrike.appendChild(selStrike);
        tr.appendChild(tdStrike);

        // 5. Qty
        var tdQty = document.createElement("td");
        tdQty.className = "num";
        var inpQty = document.createElement("input");
        inpQty.type = "number";
        inpQty.min = "1";
        inpQty.max = "100";
        inpQty.value = String(leg.qty || 1);
        inpQty.className = "select-control strategy-leg-qty";
        inpQty.setAttribute("aria-label", "Leg " + (idx + 1) + " qty");
        inpQty.addEventListener("input", function () {
          var parsed = parseInt(inpQty.value, 10);
          if (!Number.isNaN(parsed) && parsed >= 1) {
            state.strategyLegs[idx].qty = Math.min(100, parsed);
            setActiveStrategyPresetUI("CUSTOM");
            markStrategyEvaluationDirty();
          }
        });
        inpQty.addEventListener("change", function () {
          var v = Math.max(1, Math.min(100, parseInt(inpQty.value, 10) || 1));
          inpQty.value = String(v);
          state.strategyLegs[idx].qty = v;
          setActiveStrategyPresetUI("CUSTOM");
          markStrategyEvaluationDirty();
        });
        tdQty.appendChild(inpQty);
        tr.appendChild(tdQty);

        // 6-8. Live/Local-Preferred Quote Cells
        var qCells = formatLegQuoteCells(localMatch, evalMatch);

        var tdBa = document.createElement("td");
        tdBa.className = "num";
        tdBa.setAttribute("data-leg-idx", String(idx));
        tdBa.setAttribute("data-leg-cell", "bidask");
        tdBa.textContent = qCells.bidAskText;
        tr.appendChild(tdBa);

        var tdMid = document.createElement("td");
        tdMid.className = "num";
        tdMid.setAttribute("data-leg-idx", String(idx));
        tdMid.setAttribute("data-leg-cell", "mid");
        tdMid.textContent = qCells.midText;
        tr.appendChild(tdMid);

        var tdGreeks = document.createElement("td");
        tdGreeks.className = "num";
        tdGreeks.setAttribute("data-leg-idx", String(idx));
        tdGreeks.setAttribute("data-leg-cell", "greeks");
        tdGreeks.textContent = qCells.greeksText;
        tr.appendChild(tdGreeks);

        // 9. Remove (allowed down to 0 legs)
        var tdDel = document.createElement("td");
        var delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "btn strategy-leg-del-btn";
        delBtn.textContent = "\u00D7";
        delBtn.title = "Remove leg";
        delBtn.addEventListener("click", function () {
          state.strategyLegs.splice(idx, 1);
          setActiveStrategyPresetUI("CUSTOM");
          if (!state.strategyLegs.length) {
            state.strategyEvaluation = null;
            state.strategyEvaluationDirty = false;
            clearStrategyKpis(
              "Add a leg or click Auto-Build",
              "NO LEGS \u2022 CLICK + LEG",
              "No legs configured. Click + Leg or Auto-Build from Walls."
            );
            renderStrategyEmptyPlaceholder();
          } else {
            markStrategyEvaluationDirty();
          }
          renderStrategyLegsTable();
        });
        tdDel.appendChild(delBtn);
        tr.appendChild(tdDel);

        tbody.appendChild(tr);
      })(i);
    }
  }

  function refreshStrategyLegsQuotesOnly() {
    if (!dom.strategyLegsTbody || !state.chainData || !state.strategyLegs.length) return;
    var evaluatedLegs =
      state.strategyEvaluation && state.strategyEvaluation.legs
        ? state.strategyEvaluation.legs
        : null;
    for (var i = 0; i < state.strategyLegs.length; i++) {
      var leg = state.strategyLegs[i];
      var match = findLocalChainContract(state.chainData, leg.expiry, leg.right, leg.strike);
      if (!match) continue;
      var qCells = formatLegQuoteCells(match, findExactEvaluatedLeg(leg, evaluatedLegs));
      var baCell = dom.strategyLegsTbody.querySelector(
        '[data-leg-idx="' + i + '"][data-leg-cell="bidask"]'
      );
      var midCell = dom.strategyLegsTbody.querySelector(
        '[data-leg-idx="' + i + '"][data-leg-cell="mid"]'
      );
      var greeksCell = dom.strategyLegsTbody.querySelector(
        '[data-leg-idx="' + i + '"][data-leg-cell="greeks"]'
      );
      if (baCell) baCell.textContent = qCells.bidAskText;
      if (midCell) midCell.textContent = qCells.midText;
      if (greeksCell) greeksCell.textContent = qCells.greeksText;
    }
  }

  function renderStrategyEvaluation(ev) {
    if (!ev) return;
    state.strategyEvaluation = ev;
    state.strategyEvaluationDirty = false;
    setActiveStrategyPresetUI(ev.strategy_kind);
    state.strategyLegs = ev.legs.map(function (l) {
      var perLegQty = ev.qty_multiplier > 0 ? Math.max(1, Math.round(l.qty / ev.qty_multiplier)) : l.qty;
      return {
        side: l.side,
        right: l.right,
        expiry: l.expiry,
        strike: l.strike,
        qty: perLegQty,
      };
    });
    renderStrategySlidersMax(ev.short_dte);
    renderStrategyLegsTable();

    if (dom.strategyBasisBadge) {
      dom.strategyBasisBadge.textContent =
        ev.strategy_kind.replace(/_/g, " ") + " \u2022 " + ev.short_dte + "d SHORT DTE";
    }

    var pr = ev.pricing;
    if (dom.strategyKpiPrice) {
      dom.strategyKpiPrice.classList.remove("is-credit", "is-debit");
      dom.strategyKpiPrice.classList.add(pr.is_credit ? "is-credit" : "is-debit");
      var cashAbs = Math.abs(pr.mid_cash_total);
      dom.strategyKpiPrice.textContent =
        (pr.is_credit ? "+" : "-") +
        fmtMoney(cashAbs) +
        (pr.is_credit ? " CR" : " DB") +
        " (" +
        fmtMoney(Math.abs(pr.net_mid_per_share)) +
        "/sh)";
    }
    if (dom.strategyKpiPriceSub) {
      dom.strategyKpiPriceSub.textContent =
        "Natural fill: " +
        (pr.natural_cash_total >= 0 ? "+" : "-") +
        fmtMoney(Math.abs(pr.natural_cash_total)) +
        " (" +
        fmtMoney(Math.abs(pr.net_natural_per_share)) +
        "/sh)";
    }

    if (dom.strategyKpiSlip) {
      var slipPctStr =
        pr.slippage_pct !== null && pr.slippage_pct !== undefined
          ? " (" + fmtNum(pr.slippage_pct, 1) + "%)"
          : "";
      dom.strategyKpiSlip.textContent = fmtMoney(pr.slippage_dollars) + slipPctStr;
    }
    if (dom.strategyKpiSlipSub) {
      dom.strategyKpiSlipSub.textContent =
        "Ladder: " +
        (pr.limit_ladder && pr.limit_ladder.length
          ? pr.limit_ladder
              .map(function (v) {
                return (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toFixed(2);
              })
              .join(" \u2192 ")
          : "\u2014");
    }

    var rk = ev.risk;
    if (dom.strategyKpiRisk) {
      var lossTxt = rk.max_loss_unbounded ? "UNBOUNDED" : fmtMoney(rk.max_loss);
      dom.strategyKpiRisk.textContent = "+" + fmtMoney(rk.max_profit) + " / -" + lossTxt;
    }
    if (dom.strategyKpiRiskSub) {
      dom.strategyKpiRiskSub.textContent =
        rk.return_on_risk_pct !== null && rk.return_on_risk_pct !== undefined
          ? "RoR: " + fmtNum(rk.return_on_risk_pct, 1) + "% on max risk"
          : rk.max_loss_unbounded
            ? "Defined upside cap / Naked risk"
            : "Risk envelope computed";
    }

    if (dom.strategyKpiPop) {
      var beTxt =
        rk.breakevens && rk.breakevens.length
          ? rk.breakevens
              .map(function (b) {
                return "$" + fmtNum(b, 2);
              })
              .join(" & ")
          : "None";
      var popTxt =
        rk.prob_of_profit_pct !== null && rk.prob_of_profit_pct !== undefined
          ? fmtNum(rk.prob_of_profit_pct, 1) + "% POP"
          : "\u2014";
      dom.strategyKpiPop.textContent = "BE " + beTxt + " \u2022 " + popTxt;
    }
    if (dom.strategyKpiPopSub) {
      dom.strategyKpiPopSub.textContent =
        rk.expected_move_1sigma_low !== null && rk.expected_move_1sigma_high !== null
          ? "\u00B11\u03c3 Move (" +
            fmtNum(rk.expected_move_1sigma_pct, 1) +
            "%): $" +
            fmtNum(rk.expected_move_1sigma_low, 2) +
            " \u2013 $" +
            fmtNum(rk.expected_move_1sigma_high, 2)
          : "\u00B11\u03c3 expected move unavailable";
    }

    var gr = ev.net_greeks;
    if (dom.strategyKpiGreeks) {
      dom.strategyKpiGreeks.textContent =
        "\u0394 " +
        fmtNum(gr.delta_shares, 1) +
        "sh \u2022 \u0398 " +
        (gr.theta_daily_dollars >= 0 ? "+" : "-") +
        fmtMoney(Math.abs(gr.theta_daily_dollars)) +
        "/d";
    }
    if (dom.strategyKpiGreeksSub) {
      dom.strategyKpiGreeksSub.textContent =
        "\u0393 " +
        fmtNum(gr.gamma_shares, 2) +
        " \u2022 Vega " +
        (gr.vega_per_1pct_dollars >= 0 ? "+" : "-") +
        fmtMoney(Math.abs(gr.vega_per_1pct_dollars)) +
        "/1%IV";
    }

    if (dom.strategyRationale) {
      dom.strategyRationale.textContent =
        ev.rationale && ev.rationale.length
          ? ev.rationale.join(" ")
          : "Strategy evaluated.";
    }

    if (isPanelVisible("strategy")) {
      renderStrategyPayoffChart(ev, themeVars());
    }
  }

  function renderStrategyPayoffChart(ev, t) {
    var el = dom.strategyChartEl;
    if (!el || typeof window.Plotly === "undefined") return;
    if (!ev || !ev.curves || !ev.curves.length || !ev.price_grid || !ev.price_grid.length) {
      renderStrategyEmptyPlaceholder();
      return;
    }

    el.classList.remove("is-idle");
    if (!el.data) {
      el.innerHTML = "";
    }
    var xs = ev.price_grid;
    var traces = [
      {
        type: "scatter",
        mode: "lines",
        name: "Zero P&L",
        x: [xs[0], xs[xs.length - 1]],
        y: [0, 0],
        line: { color: t.muted, width: 1, dash: "dot" },
        hoverinfo: "skip",
        showlegend: false,
      },
    ];

    for (var i = 0; i < ev.curves.length; i++) {
      var curve = ev.curves[i];
      var isExp = curve.label.indexOf("At ") === 0;
      var isSim = curve.label.indexOf("Simulated") === 0;
      var isMid = curve.label.indexOf("Midpoint") === 0;
      var lineColor = isExp
        ? t.call
        : isSim
          ? t.anomaly
          : isMid
            ? t.warn
            : t.dtePalette[0];
      traces.push({
        type: "scatter",
        mode: "lines",
        name: curve.label,
        x: xs,
        y: curve.pnl_values,
        line: {
          color: lineColor,
          width: isExp || isSim ? 2.5 : 1.75,
          dash: isMid ? "dot" : curve.days_elapsed === 0 && !isSim ? "dash" : "solid",
        },
        hovertemplate: curve.label + ": $%{y:,.2f}<extra></extra>",
      });
    }

    var markers = [
      makeVerticalMarker(ev.underlying_price, "SPOT $" + fmtNum(ev.underlying_price, 2), t.ink, 0.99, "dash", t),
    ];
    if (ev.primary_call_wall) {
      markers.push(
        makeVerticalMarker(
          ev.primary_call_wall,
          "CW $" + fmtNum(ev.primary_call_wall, 0),
          t.call,
          0.90,
          "dot",
          t
        )
      );
    }
    if (ev.primary_put_wall) {
      markers.push(
        makeVerticalMarker(
          ev.primary_put_wall,
          "PW $" + fmtNum(ev.primary_put_wall, 0),
          t.put,
          0.80,
          "dot",
          t
        )
      );
    }
    if (ev.risk && ev.risk.breakevens) {
      for (var b = 0; b < ev.risk.breakevens.length; b++) {
        markers.push(
          makeVerticalMarker(
            ev.risk.breakevens[b],
            "BE $" + fmtNum(ev.risk.breakevens[b], 2),
            t.warn,
            0.10 + b * 0.09,
            "dashdot",
            t
          )
        );
      }
    }

    var layout = baseLayout(t, "Underlying Price at Horizon ($)", "Net Strategy P&L ($)", markers);
    if (
      ev.risk &&
      ev.risk.expected_move_1sigma_low !== null &&
      ev.risk.expected_move_1sigma_high !== null
    ) {
      layout.shapes.unshift({
        type: "rect",
        xref: "x",
        yref: "paper",
        x0: ev.risk.expected_move_1sigma_low,
        x1: ev.risk.expected_move_1sigma_high,
        y0: 0,
        y1: 1,
        fillcolor: t.isTerminal ? "rgba(56, 189, 248, 0.06)" : "rgba(29, 78, 216, 0.05)",
        line: { width: 0 },
        layer: "below",
      });
    }
    layout.legend = {
      orientation: "h",
      y: 1.14,
      x: 0.5,
      xanchor: "center",
      font: { size: 10, color: t.ink },
    };
    layout.xaxis.tickprefix = "$";
    layout.yaxis.tickprefix = "$";
    layout.yaxis.zeroline = true;
    layout.yaxis.zerolinecolor = t.grid;
    layout.margin = { t: 48, r: 22, b: 44, l: 64 };
    window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
    window.requestAnimationFrame(function () {
      if (window.Plotly && window.Plotly.Plots && el.data) {
        window.Plotly.Plots.resize(el);
      }
    });
    window.setTimeout(function () {
      if (window.Plotly && window.Plotly.Plots && el.data) {
        window.Plotly.Plots.resize(el);
      }
    }, 60);
  }

  function strategyControlParams() {
    var qty = dom.strategyQtyInput
      ? Math.max(1, Math.min(100, parseInt(dom.strategyQtyInput.value, 10) || 1))
      : 1;
    var days = dom.strategyDaysSlider ? Math.max(0, parseInt(dom.strategyDaysSlider.value, 10) || 0) : 0;
    var ivPct = dom.strategyIvSlider ? parseInt(dom.strategyIvSlider.value, 10) || 0 : 0;
    var targetExp = dom.strategyExpirySelect && dom.strategyExpirySelect.value
      ? dom.strategyExpirySelect.value
      : null;
    return {
      qty: qty,
      daysForward: days,
      ivShift: ivPct / 100.0,
      targetExpiry: targetExp,
    };
  }

  function setStrategyButtonsBusy(busy) {
    state.strategyBusy = Boolean(busy);
    syncStrategyActionButtons();
  }

  async function autoBuildStrategy(presetKind, callOptions) {
    var cOpts = callOptions || {};
    var silent = Boolean(cOpts.silent);
    if (!state.chainData || state.strategyBusy) {
      if (!state.chainData && !silent) {
        showBanner("warn", "Load or refresh an option snapshot before auto-building strategies.", {
          phase: "STRATEGY_BUILD",
        });
      }
      return;
    }
    var kind = presetKind && presetKind !== "CUSTOM" ? presetKind : "BULL_PUT_SPREAD";
    setActiveStrategyPresetUI(kind);
    var opts = strategyControlParams();
    setStrategyButtonsBusy(true);
    try {
      var res = await apiJson("/api/strategy/suggest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ticker: state.ticker,
          snapshot_id: state.snapshotId || null,
          strategy_kind: kind,
          target_expiry: opts.targetExpiry,
          days_forward: opts.daysForward,
          iv_shift: opts.ivShift,
          qty_multiplier: opts.qty,
        }),
      });
      if (res.ok && res.body && res.body.evaluations && res.body.evaluations.length) {
        renderStrategyEvaluation(res.body.evaluations[0]);
      } else if (!silent) {
        showBanner(
          "warn",
          extractApiErrorMessage(res.body, res.textSnippet || "Unable to auto-build strategy."),
          {
            ticker: state.ticker,
            phase: "STRATEGY_BUILD",
            status: res.status,
            elapsedMs: res.elapsedMs,
            rawBody: res.textSnippet,
          }
        );
      }
    } catch (err) {
      if (!silent) {
        showBanner("error", "Strategy auto-build request failed.", {
          ticker: state.ticker,
          phase: "STRATEGY_BUILD",
          elapsedMs: err && err.elapsedMs,
          detail: err && err.message ? err.message : String(err || ""),
        });
      }
    } finally {
      setStrategyButtonsBusy(false);
    }
  }

  async function evaluateStrategyRequest(options) {
    var silent = Boolean(options && options.silent);
    if (!state.chainData) {
      if (!silent) {
        showBanner("warn", "Load or refresh an option snapshot before evaluating legs.");
      }
      return;
    }
    if (state.strategyBusy) {
      if (!state.strategyPendingEvaluate || !silent) {
        state.strategyPendingEvaluate = { silent: silent };
      }
      return;
    }
    if (!state.strategyLegs.length) {
      if (!silent) {
        showBanner("warn", "Add at least one option leg first.");
      }
      return;
    }

    var snappedAny = false;
    for (var i = 0; i < state.strategyLegs.length; i++) {
      var leg = state.strategyLegs[i];
      if (!findLocalChainContract(state.chainData, leg.expiry, leg.right, leg.strike)) {
        if (snapLegToValidContract(state.chainData, leg, "expiry")) {
          snappedAny = true;
        } else {
          if (!silent) {
            showBanner("warn", "Leg " + (i + 1) + " does not match any contract in the loaded snapshot.");
          }
          return;
        }
      }
    }
    if (snappedAny) {
      renderStrategyLegsTable();
    }

    var opts = strategyControlParams();
    setStrategyButtonsBusy(true);
    try {
      var res = await apiJson("/api/strategy/evaluate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ticker: state.ticker,
          snapshot_id: state.snapshotId || null,
          strategy_kind: state.strategyKind || "CUSTOM",
          target_expiry: opts.targetExpiry,
          legs: state.strategyLegs.map(function (l) {
            return {
              side: l.side,
              right: l.right,
              expiry: l.expiry,
              strike: Number(l.strike),
              qty: Math.max(1, Math.min(100, parseInt(l.qty, 10) || 1)),
            };
          }),
          days_forward: opts.daysForward,
          iv_shift: opts.ivShift,
          qty_multiplier: opts.qty,
        }),
      });
      if (res.ok && res.body) {
        state.corridorLastRepriceAt = Date.now();
        renderStrategyEvaluation(res.body);
      } else if (!silent) {
        showBanner(
          "warn",
          extractApiErrorMessage(res.body, res.textSnippet || "Strategy evaluation failed."),
          {
            ticker: state.ticker,
            phase: "STRATEGY_EVAL",
            status: res.status,
            elapsedMs: res.elapsedMs,
            rawBody: res.textSnippet,
          }
        );
      }
    } catch (err) {
      if (!silent) {
        showBanner("error", "Strategy evaluation request failed.", {
          ticker: state.ticker,
          phase: "STRATEGY_EVAL",
          elapsedMs: err && err.elapsedMs,
          detail: err && err.message ? err.message : String(err || ""),
        });
      }
    } finally {
      setStrategyButtonsBusy(false);
      if (state.strategyPendingEvaluate) {
        var pending = state.strategyPendingEvaluate;
        state.strategyPendingEvaluate = null;
        evaluateStrategyRequest(pending);
      }
    }
  }

  function addCustomStrategyLegRow() {
    if (!state.chainData) return;
    if (state.strategyLegs.length >= 6) {
      showBanner("warn", "Maximum 6 option legs supported per strategy.");
      return;
    }
    var exps = strategySortedExpiries(state.chainData);
    var exp =
      (dom.strategyExpirySelect && dom.strategyExpirySelect.value) ||
      (state.strategyLegs.length ? state.strategyLegs[0].expiry : exps.length ? exps[0].expiry : "");
    var chosenRight = "C";
    var strikes = strategyStrikesFor(state.chainData, exp, chosenRight);
    if (!strikes.length) {
      chosenRight = "P";
      strikes = strategyStrikesFor(state.chainData, exp, chosenRight);
    }
    if (!strikes.length && state.chainData.contracts && state.chainData.contracts.length) {
      var fallbackC = state.chainData.contracts[0];
      exp = fallbackC.expiry;
      chosenRight = fallbackC.right;
      strikes = strategyStrikesFor(state.chainData, exp, chosenRight);
    }
    if (!strikes.length) {
      showBanner("warn", "No eligible option contracts found to add a leg.");
      return;
    }
    var spot = Number(state.chainData.meta.underlying_price || 100);
    var strike = nearestStrikeFromList(strikes, spot);
    state.strategyLegs.push({ side: "BUY", right: chosenRight, expiry: exp, strike: strike, qty: 1 });
    setActiveStrategyPresetUI("CUSTOM");
    markStrategyEvaluationDirty();
    renderStrategyLegsTable();
  }

  function addSpecificStrategyLeg(expiry, right, strike) {
    if (!state.chainData) return;
    if (state.strategyLegs.length >= 6) {
      showBanner("warn", "Maximum 6 option legs supported per strategy.", {
        ticker: state.ticker,
        phase: "STRATEGY_LEG",
      });
      return;
    }
    var expStr = String(expiry || "");
    var rightNorm = String(right || "C").toUpperCase();
    var numStrike = Number(strike);
    if (!expStr || Number.isNaN(numStrike)) return;

    if (!isPanelVisible("strategy")) {
      var nextVis = Object.assign({}, state.panelVisibility, { strategy: true });
      applyPanelVisibility(nextVis, { persist: true, skipRender: true });
    }
    state.strategyLegs.push({
      side: "BUY",
      right: rightNorm,
      expiry: expStr,
      strike: numStrike,
      qty: 1,
    });
    setActiveStrategyPresetUI("CUSTOM");
    markStrategyEvaluationDirty();
    renderStrategyLegsTable();
    if (state.corridorFrame && state.corridorFrame.source === "CACHED_PREVIEW") {
      applyCorridorFrame(buildLocalCorridorPreview(state.chainData), { mergeIntoChain: false });
    }
    if (state.strategyEvaluation || isStrategyAutoPopulateEnabled()) {
      evaluateStrategyRequest({ silent: true });
    }
  }

  function maybeAutoPopulateStrategy(data) {
    if (!data || !data.contracts || !data.contracts.length) return;
    if (!isStrategyAutoPopulateEnabled() || !isPanelVisible("strategy")) return;
    if (state.strategyKind === "CUSTOM" && state.strategyLegs.length) {
      evaluateStrategyRequest({ silent: true });
      return;
    }
    var preset = state.strategyKind && state.strategyKind !== "CUSTOM"
      ? state.strategyKind
      : "BULL_PUT_SPREAD";
    autoBuildStrategy(preset, { silent: true, autoTriggered: true });
  }

  // ------------------------------------------------------------------ Wall Strike Corridor (+-5 Strikes & Live Streaming)
  function corridorKey(expiry, strike, right) {
    return String(expiry) + "|" + Number(strike) + "|" + String(right).toUpperCase();
  }

  function corridorPriorityLegsList() {
    if (!state.strategyLegs || !state.strategyLegs.length) return [];
    return state.strategyLegs.map(function (l) {
      return {
        expiry: String(l.expiry),
        strike: Number(l.strike),
        right: String(l.right).toUpperCase(),
      };
    });
  }

  function corridorPriorityLegsParam() {
    return corridorPriorityLegsList()
      .map(function (l) {
        return l.expiry + "|" + l.strike + "|" + l.right;
      })
      .join(",");
  }

  function corridorWallsForExpiry(data, targetExp) {
    if (!data || !data.walls) return { putWall: null, callWall: null, basis: "VOLUME" };
    var basis = data.walls.primary_basis || "VOLUME";
    var byDte = data.walls.by_dte || [];
    for (var i = 0; i < byDte.length; i++) {
      if (String(byDte[i].expiry) === String(targetExp)) {
        return {
          putWall: byDte[i].primary_put_wall ? Number(byDte[i].primary_put_wall.strike) : null,
          callWall: byDte[i].primary_call_wall ? Number(byDte[i].primary_call_wall.strike) : null,
          basis: basis,
        };
      }
    }
    return {
      putWall: data.walls.aggregate_primary_put_wall
        ? Number(data.walls.aggregate_primary_put_wall.strike)
        : null,
      callWall: data.walls.aggregate_primary_call_wall
        ? Number(data.walls.aggregate_primary_call_wall.strike)
        : null,
      basis: basis,
    };
  }

  function buildLocalCorridorPreview(data) {
    if (!data || !data.contracts || !data.contracts.length) return null;
    var exps = strategySortedExpiries(data);
    if (!exps.length) return null;
    var win = dom.corridorWindowSelect ? Math.max(1, parseInt(dom.corridorWindowSelect.value, 10) || 5) : 5;
    state.corridorWindow = win;
    var maxCap = 56;
    var chosenExp =
      dom.corridorExpirySelect && dom.corridorExpirySelect.value
        ? dom.corridorExpirySelect.value
        : exps[0].expiry;
    var chosenDte = exps[0].dte;
    var foundExp = false;
    for (var i = 0; i < exps.length; i++) {
      if (exps[i].expiry === chosenExp) {
        chosenDte = exps[i].dte;
        foundExp = true;
        break;
      }
    }
    if (!foundExp) {
      chosenExp = exps[0].expiry;
      chosenDte = exps[0].dte;
    }
    state.corridorExpiry = chosenExp;

    var spot = Number(data.meta.underlying_price || 0);
    var wInfo = corridorWallsForExpiry(data, chosenExp);
    var expRows = data.contracts.filter(function (c) {
      return String(c.expiry) === String(chosenExp);
    });
    if (!expRows.length) return null;

    var strikeMap = {};
    for (var r = 0; r < expRows.length; r++) {
      strikeMap[Number(expRows[r].strike)] = true;
    }
    var strikes = Object.keys(strikeMap)
      .map(function (s) {
        return Number(s);
      })
      .sort(function (a, b) {
        return a - b;
      });
    if (!strikes.length) return null;

    function nearestIdx(targetVal) {
      var bestI = 0;
      var bestD = Math.abs(strikes[0] - targetVal);
      for (var k = 1; k < strikes.length; k++) {
        var d = Math.abs(strikes[k] - targetVal);
        if (d < bestD) {
          bestI = k;
          bestD = d;
        }
      }
      return bestI;
    }

    var pwIdx = nearestIdx(wInfo.putWall !== null ? wInfo.putWall : spot);
    var cwIdx = nearestIdx(wInfo.callWall !== null ? wInfo.callWall : spot);
    var atmIdx = nearestIdx(spot);
    var idxLookup = {};
    for (var si = 0; si < strikes.length; si++) {
      idxLookup[strikes[si].toFixed(6)] = si;
    }

    var legKeys = {};
    var legs = corridorPriorityLegsList();
    for (var l = 0; l < legs.length; l++) {
      legKeys[corridorKey(legs[l].expiry, legs[l].strike, legs[l].right)] = true;
    }

    function inBand(idx, anchorIdx) {
      return idx >= anchorIdx - win && idx <= anchorIdx + win;
    }

    var candidates = [];
    var seen = {};
    for (var a = 0; a < data.contracts.length; a++) {
      var row = data.contracts[a];
      var rk = corridorKey(row.expiry, row.strike, row.right);
      var isExpMatch = String(row.expiry) === String(chosenExp);
      var sIdx = isExpMatch ? idxLookup[Number(row.strike).toFixed(6)] : undefined;
      var inPw = sIdx !== undefined && inBand(sIdx, pwIdx);
      var inCw = sIdx !== undefined && inBand(sIdx, cwIdx);
      var inAtm = sIdx !== undefined && inBand(sIdx, atmIdx);
      var isLeg = Boolean(legKeys[rk]);
      if (!isLeg && !(inPw || inCw || inAtm)) continue;
      if (seen[rk]) continue;
      seen[rk] = true;

      var tier = 3;
      var dist = sIdx !== undefined ? Math.abs(sIdx - atmIdx) : 0;
      if (isLeg) {
        tier = 0;
        dist = 0;
      } else if ((inPw && row.right === "P") || (inCw && row.right === "C")) {
        tier = 1;
        dist = inPw && row.right === "P" ? Math.abs(sIdx - pwIdx) : Math.abs(sIdx - cwIdx);
      } else if (inPw || inCw) {
        tier = 2;
        dist = Math.min(
          inPw ? Math.abs(sIdx - pwIdx) : 999,
          inCw ? Math.abs(sIdx - cwIdx) : 999
        );
      }

      var zone = "ATM";
      if (sIdx === undefined) zone = "STRATEGY_LEG";
      else if (inPw && inCw) zone = "BOTH_WALLS";
      else if (inPw) zone = "PUT_WALL";
      else if (inCw) zone = "CALL_WALL";
      else if (isLeg) zone = "STRATEGY_LEG";

      var bid = row.bid !== null && row.bid !== undefined ? Number(row.bid) : null;
      var ask = row.ask !== null && row.ask !== undefined ? Number(row.ask) : null;
      var mid =
        bid !== null && ask !== null && bid > 0 && ask >= bid
          ? (bid + ask) / 2.0
          : row.mid !== null && row.mid !== undefined
            ? Number(row.mid)
            : row.last !== null && row.last !== undefined
              ? Number(row.last)
              : row.close !== null && row.close !== undefined
                ? Number(row.close)
                : null;
      var spreadPct =
        bid !== null && ask !== null && mid && bid > 0 && ask >= bid
          ? ((ask - bid) / mid) * 100.0
          : null;

      candidates.push({
        sortRank: [tier, dist, Math.abs(Number(row.strike) - spot), Number(row.strike)],
        quote: {
          option_conid: row.option_conid || 0,
          expiry: String(row.expiry),
          dte: Number(row.dte),
          strike: Number(row.strike),
          right: String(row.right),
          zone: zone,
          is_put_wall:
            wInfo.putWall !== null &&
            Math.abs(Number(row.strike) - wInfo.putWall) < 1e-4 &&
            row.right === "P",
          is_call_wall:
            wInfo.callWall !== null &&
            Math.abs(Number(row.strike) - wInfo.callWall) < 1e-4 &&
            row.right === "C",
          is_strategy_leg: isLeg,
          bid: bid,
          ask: ask,
          mid: mid,
          last: row.last !== null && row.last !== undefined ? Number(row.last) : null,
          close: row.close !== null && row.close !== undefined ? Number(row.close) : null,
          spread_pct: spreadPct,
          volume: row.volume,
          open_interest: row.open_interest,
          iv: row.iv,
          delta: row.delta,
          gamma: row.gamma,
          theta: row.theta,
          vega: row.vega,
          greeks_source: row.greeks_source || "NONE",
          updated_ts_utc: data.meta.snapshot_ts_utc || "",
        },
      });
    }

    candidates.sort(function (a, b) {
      for (var tIdx = 0; tIdx < a.sortRank.length; tIdx++) {
        if (a.sortRank[tIdx] !== b.sortRank[tIdx]) return a.sortRank[tIdx] - b.sortRank[tIdx];
      }
      return 0;
    });
    var selected = candidates.slice(0, maxCap).map(function (x) {
      return x.quote;
    });
    selected.sort(function (a, b) {
      if (a.dte !== b.dte) return a.dte - b.dte;
      if (a.strike !== b.strike) return a.strike - b.strike;
      return a.right < b.right ? -1 : a.right > b.right ? 1 : 0;
    });

    return {
      ticker: data.meta.underlying,
      snapshot_id: data.meta.snapshot_id,
      target_expiry: chosenExp,
      target_dte: chosenDte,
      window_strikes: win,
      max_contracts: maxCap,
      subscribed_count: Math.min(60, selected.length + (selected.length < 60 ? 1 : 0)),
      subscription_cap: 60,
      underlying_price: spot,
      market_data_type: data.meta.market_data_type || 3,
      primary_call_wall: wInfo.callWall,
      primary_put_wall: wInfo.putWall,
      wall_basis: wInfo.basis,
      updated_ts_utc: data.meta.snapshot_ts_utc || "",
      sequence: 0,
      mode: "PREVIEW",
      elapsed_ms: null,
      changed_keys: [],
      quotes: selected,
      notices: [],
    };
  }

  function updateCorridorStatusUI(frame, modeLabel) {
    var count = frame ? frame.subscribed_count : 0;
    var cap = frame && frame.subscription_cap ? frame.subscription_cap : 60;
    if (dom.corridorSubsBadge) {
      dom.corridorSubsBadge.textContent = count + " / " + cap + " SUBS";
      dom.corridorSubsBadge.className =
        "badge " + (state.corridorStreaming ? "badge--live" : "badge--info");
    }
    if (dom.corridorStatusBadge) {
      if (modeLabel) {
        dom.corridorStatusBadge.textContent = modeLabel;
      } else if (!frame) {
        dom.corridorStatusBadge.textContent = "IDLE \u2022 MANUAL";
      } else if (frame.mode === "STREAM") {
        dom.corridorStatusBadge.textContent =
          "STREAMING LIVE \u2022 #" + frame.sequence + " \u2022 " + frame.target_dte + "d EXP";
      } else if (frame.mode === "ONE_SHOT") {
        dom.corridorStatusBadge.textContent =
          "ONE-SHOT UPDATED \u2022 " + frame.target_dte + "d EXP";
      } else {
        dom.corridorStatusBadge.textContent =
          "IDLE \u2022 PREVIEW (" + frame.quotes.length + " OPT \u2022 " + frame.target_dte + "d)";
      }
    }
  }

  function renderCorridorKpis(frame) {
    if (!frame || !frame.quotes || !frame.quotes.length) {
      if (dom.corridorKpiPw) dom.corridorKpiPw.textContent = "\u2014";
      if (dom.corridorKpiPwSub) dom.corridorKpiPwSub.textContent = "Put corridor avg mid / spread";
      if (dom.corridorKpiCw) dom.corridorKpiCw.textContent = "\u2014";
      if (dom.corridorKpiCwSub) dom.corridorKpiCwSub.textContent = "Call corridor avg mid / spread";
      if (dom.corridorKpiSpot) dom.corridorKpiSpot.textContent = "\u2014";
      if (dom.corridorKpiSpotSub) dom.corridorKpiSpotSub.textContent = "Cached baseline (manual)";
      if (dom.corridorKpiSync) dom.corridorKpiSync.textContent = "IDLE";
      if (dom.corridorKpiSyncSub) dom.corridorKpiSyncSub.textContent = "Click Refresh or toggle Streaming ON";
      return;
    }

    var pwQ = null;
    var cwQ = null;
    var spreadSum = 0;
    var spreadCnt = 0;
    for (var i = 0; i < frame.quotes.length; i++) {
      var q = frame.quotes[i];
      if (q.is_put_wall && !pwQ) pwQ = q;
      if (q.is_call_wall && !cwQ) cwQ = q;
      if (q.spread_pct !== null && q.spread_pct !== undefined) {
        spreadSum += Number(q.spread_pct);
        spreadCnt += 1;
      }
    }

    if (dom.corridorKpiPw) {
      dom.corridorKpiPw.textContent =
        frame.primary_put_wall !== null && frame.primary_put_wall !== undefined
          ? "$" + fmtNum(frame.primary_put_wall, 2) + (pwQ && pwQ.mid ? " \u2022 " + fmtMoney(pwQ.mid) : "")
          : "\u2014";
    }
    if (dom.corridorKpiPwSub) {
      dom.corridorKpiPwSub.textContent = pwQ
        ? "Bid/Ask " +
          (pwQ.bid !== null ? fmtNum(pwQ.bid, 2) : "\u2014") +
          "/" +
          (pwQ.ask !== null ? fmtNum(pwQ.ask, 2) : "\u2014") +
          (pwQ.delta !== null ? " \u2022 \u0394 " + fmtNum(pwQ.delta, 2) : "")
        : "Put Wall window \u00B1" + frame.window_strikes;
    }

    if (dom.corridorKpiCw) {
      dom.corridorKpiCw.textContent =
        frame.primary_call_wall !== null && frame.primary_call_wall !== undefined
          ? "$" + fmtNum(frame.primary_call_wall, 2) + (cwQ && cwQ.mid ? " \u2022 " + fmtMoney(cwQ.mid) : "")
          : "\u2014";
    }
    if (dom.corridorKpiCwSub) {
      dom.corridorKpiCwSub.textContent = cwQ
        ? "Bid/Ask " +
          (cwQ.bid !== null ? fmtNum(cwQ.bid, 2) : "\u2014") +
          "/" +
          (cwQ.ask !== null ? fmtNum(cwQ.ask, 2) : "\u2014") +
          (cwQ.delta !== null ? " \u2022 \u0394 " + fmtNum(cwQ.delta, 2) : "")
        : "Call Wall window \u00B1" + frame.window_strikes;
    }

    if (dom.corridorKpiSpot) {
      dom.corridorKpiSpot.textContent =
        "$" + fmtNum(frame.underlying_price, 2) + " \u2022 " + frame.target_dte + "d";
    }
    if (dom.corridorKpiSpotSub) {
      var avgSpreadTxt = spreadCnt ? "Avg Sprd " + fmtNum(spreadSum / spreadCnt, 1) + "%" : "Spread n/a";
      dom.corridorKpiSpotSub.textContent = "Type " + frame.market_data_type + " \u2022 " + avgSpreadTxt;
    }

    if (dom.corridorKpiSync) {
      dom.corridorKpiSync.textContent =
        frame.mode === "STREAM"
          ? "LIVE #" + frame.sequence
          : frame.mode === "ONE_SHOT"
            ? (frame.elapsed_ms ? Math.round(frame.elapsed_ms) + " ms" : "UPDATED")
            : "PREVIEW";
    }
    if (dom.corridorKpiSyncSub) {
      var tsShort = frame.updated_ts_utc ? frame.updated_ts_utc.replace("T", " ").replace("Z", " UTC") : "Cached snapshot";
      dom.corridorKpiSyncSub.textContent = tsShort;
    }
  }

  function renderCorridorLadder(frame) {
    var tbody = dom.corridorLadderTbody;
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!frame || !frame.quotes || !frame.quotes.length) {
      var trEmpty = document.createElement("tr");
      var tdEmpty = document.createElement("td");
      tdEmpty.colSpan = 9;
      tdEmpty.className = "text-left";
      tdEmpty.textContent = "Load or refresh a ticker snapshot to view the Wall Corridor.";
      trEmpty.appendChild(tdEmpty);
      tbody.appendChild(trEmpty);
      return;
    }

    var changedSet = {};
    if (frame.changed_keys) {
      for (var c = 0; c < frame.changed_keys.length; c++) {
        changedSet[frame.changed_keys[c]] = true;
      }
    }
    var nextPrevMids = {};

    for (var i = 0; i < frame.quotes.length; i++) {
      var q = frame.quotes[i];
      var qKey = corridorKey(q.expiry, q.strike, q.right);
      var prevMid = state.corridorPrevMids[qKey];
      var curMid = q.mid !== null && q.mid !== undefined ? Number(q.mid) : null;
      if (curMid !== null) nextPrevMids[qKey] = curMid;

      var flashClass = "";
      if (
        frame.mode === "STREAM" &&
        changedSet[qKey] &&
        prevMid !== undefined &&
        curMid !== null
      ) {
        if (curMid > prevMid + 1e-4) flashClass = " flash-up";
        else if (curMid < prevMid - 1e-4) flashClass = " flash-down";
      }

      var tr = document.createElement("tr");
      var classes = [];
      if (q.is_put_wall) classes.push("row-wall-put");
      if (q.is_call_wall) classes.push("row-wall-call");
      if (q.is_strategy_leg) classes.push("row-strategy-leg");
      tr.className = classes.join(" ");

      var tdZone = document.createElement("td");
      tdZone.className = "text-left";
      var zSpan = document.createElement("span");
      var zLabel =
        q.zone === "PUT_WALL"
          ? "PW"
          : q.zone === "CALL_WALL"
            ? "CW"
            : q.zone === "BOTH_WALLS"
              ? "PW/CW"
              : q.zone === "STRATEGY_LEG"
                ? "LEG"
                : "ATM";
      zSpan.className =
        "corridor-zone-pill" +
        (q.is_put_wall
          ? " corridor-zone-pill--pw"
          : q.is_call_wall
            ? " corridor-zone-pill--cw"
            : "");
      zSpan.textContent = zLabel;
      tdZone.appendChild(zSpan);
      if (q.is_strategy_leg && zLabel !== "LEG") {
        var legSpan = document.createElement("span");
        legSpan.className = "corridor-zone-pill corridor-zone-pill--leg";
        legSpan.textContent = "LEG";
        tdZone.appendChild(legSpan);
      }
      tr.appendChild(tdZone);

      var tdRight = document.createElement("td");
      tdRight.className = "text-left";
      var rPill = document.createElement("span");
      rPill.className = "right-pill right-pill--" + q.right;
      rPill.textContent = q.right;
      tdRight.appendChild(rPill);
      tr.appendChild(tdRight);

      var tdStrike = document.createElement("td");
      tdStrike.className = "num";
      tdStrike.textContent =
        formatStrikeShort(q.strike) +
        (q.expiry !== frame.target_expiry ? " (" + q.dte + "d)" : "");
      tr.appendChild(tdStrike);

      var tdBid = document.createElement("td");
      tdBid.className = "num" + flashClass;
      tdBid.textContent = q.bid !== null && q.bid !== undefined ? fmtNum(q.bid, 2) : "\u2014";
      tr.appendChild(tdBid);

      var tdAsk = document.createElement("td");
      tdAsk.className = "num" + flashClass;
      tdAsk.textContent = q.ask !== null && q.ask !== undefined ? fmtNum(q.ask, 2) : "\u2014";
      tr.appendChild(tdAsk);

      var tdMid = document.createElement("td");
      tdMid.className = "num" + flashClass;
      tdMid.textContent = q.mid !== null && q.mid !== undefined ? fmtMoney(q.mid) : "\u2014";
      tr.appendChild(tdMid);

      var tdSpread = document.createElement("td");
      tdSpread.className = "num";
      tdSpread.textContent =
        q.spread_pct !== null && q.spread_pct !== undefined
          ? fmtNum(q.spread_pct, 1) + "%"
          : "\u2014";
      tr.appendChild(tdSpread);

      var tdVolOi = document.createElement("td");
      tdVolOi.className = "num";
      var vTxt = q.volume !== null && q.volume !== undefined ? fmtInt(q.volume) : "\u2014";
      var oiTxt =
        q.open_interest !== null && q.open_interest !== undefined
          ? fmtInt(q.open_interest)
          : "\u2014";
      tdVolOi.textContent = vTxt + " / " + oiTxt;
      tr.appendChild(tdVolOi);

      var tdGreeks = document.createElement("td");
      tdGreeks.className = "num";
      var gD = q.delta !== null && q.delta !== undefined ? fmtNum(q.delta, 2) : "\u2014";
      var gIv =
        q.iv !== null && q.iv !== undefined ? fmtNum(Number(q.iv) * 100, 1) + "%" : "\u2014";
      tdGreeks.textContent = gD + " \u2022 " + gIv;
      tr.appendChild(tdGreeks);

      tbody.appendChild(tr);
    }

    state.corridorPrevMids = nextPrevMids;
  }

  function renderCorridorChart(frame, t) {
    var el = dom.corridorChartEl;
    if (!el || typeof window.Plotly === "undefined") return;
    if (!frame || !frame.quotes || !frame.quotes.length) {
      renderCorridorEmptyPlaceholder();
      return;
    }

    var callX = [];
    var callY = [];
    var callUp = [];
    var callDn = [];
    var callHover = [];
    var putX = [];
    var putY = [];
    var putUp = [];
    var putDn = [];
    var putHover = [];

    for (var i = 0; i < frame.quotes.length; i++) {
      var q = frame.quotes[i];
      if (q.expiry !== frame.target_expiry) continue;
      if (q.mid === null || q.mid === undefined) continue;
      var midVal = Number(q.mid);
      var bidVal = q.bid !== null && q.bid !== undefined ? Number(q.bid) : midVal;
      var askVal = q.ask !== null && q.ask !== undefined ? Number(q.ask) : midVal;
      var upErr = Math.max(0, askVal - midVal);
      var dnErr = Math.max(0, midVal - bidVal);
      var label =
        "$" +
        fmtNum(q.strike, 2) +
        " " +
        q.right +
        "<br>Bid/Ask: " +
        fmtNum(bidVal, 2) +
        " / " +
        fmtNum(askVal, 2) +
        "<br>Mid: $" +
        fmtNum(midVal, 2) +
        (q.delta !== null && q.delta !== undefined ? "<br>\u0394: " + fmtNum(q.delta, 2) : "");
      if (q.right === "C") {
        callX.push(q.strike);
        callY.push(midVal);
        callUp.push(upErr);
        callDn.push(dnErr);
        callHover.push(label);
      } else {
        putX.push(q.strike);
        putY.push(midVal);
        putUp.push(upErr);
        putDn.push(dnErr);
        putHover.push(label);
      }
    }

    if (!callX.length && !putX.length) {
      renderCorridorEmptyPlaceholder();
      return;
    }

    el.classList.remove("is-idle");
    if (!el.data) {
      el.innerHTML = "";
    }

    var traces = [];
    if (callX.length) {
      traces.push({
        type: "scatter",
        mode: "lines+markers",
        name: "Call Mid (Bid-Ask)",
        x: callX,
        y: callY,
        error_y: {
          type: "data",
          symmetric: false,
          array: callUp,
          arrayminus: callDn,
          color: t.call,
          thickness: 1.5,
          width: 4,
        },
        line: { color: t.call, width: 2 },
        marker: { color: t.call, size: 7 },
        text: callHover,
        hovertemplate: "%{text}<extra></extra>",
      });
    }
    if (putX.length) {
      traces.push({
        type: "scatter",
        mode: "lines+markers",
        name: "Put Mid (Bid-Ask)",
        x: putX,
        y: putY,
        error_y: {
          type: "data",
          symmetric: false,
          array: putUp,
          arrayminus: putDn,
          color: t.put,
          thickness: 1.5,
          width: 4,
        },
        line: { color: t.put, width: 2 },
        marker: { color: t.put, size: 7, symbol: "diamond" },
        text: putHover,
        hovertemplate: "%{text}<extra></extra>",
      });
    }

    var markers = [
      makeVerticalMarker(frame.underlying_price, "SPOT $" + fmtNum(frame.underlying_price, 2), t.ink, 0.99, "dash", t),
    ];
    if (frame.primary_put_wall) {
      markers.push(
        makeVerticalMarker(frame.primary_put_wall, "PW $" + fmtNum(frame.primary_put_wall, 0), t.put, 0.87, "dot", t)
      );
    }
    if (frame.primary_call_wall) {
      markers.push(
        makeVerticalMarker(frame.primary_call_wall, "CW $" + fmtNum(frame.primary_call_wall, 0), t.call, 0.75, "dot", t)
      );
    }

    var layout = baseLayout(t, "Corridor Strike ($)", "Option Mid & Bid-Ask ($)", markers);
    layout.xaxis.tickprefix = "$";
    layout.yaxis.tickprefix = "$";
    layout.legend = {
      orientation: "h",
      y: 1.13,
      x: 0.5,
      xanchor: "center",
      font: { size: 10, color: t.ink },
    };
    layout.margin = { t: 46, r: 20, b: 44, l: 58 };
    window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
    schedulePlotlyResize(el);
    window.setTimeout(function () {
      schedulePlotlyResize(el);
    }, 60);
  }

  function mergeCorridorQuotesIntoChain(frame, options) {
    if (!state.chainData || !state.chainData.contracts || !frame || !frame.quotes) return false;
    var opts = options || {};
    var bypassThrottle = Boolean(opts.bypassAutoThrottle);
    if (frame.underlying_price && frame.underlying_price > 0) {
      state.chainData.meta.underlying_price = frame.underlying_price;
      if (dom.kpiSpot) {
        var spotValEl = dom.kpiSpot.querySelector(".kpi-value");
        if (spotValEl) spotValEl.textContent = fmtMoney(frame.underlying_price);
      }
    }

    var byKey = {};
    for (var i = 0; i < frame.quotes.length; i++) {
      var q = frame.quotes[i];
      byKey[corridorKey(q.expiry, q.strike, q.right)] = q;
    }

    var touched = false;
    for (var c = 0; c < state.chainData.contracts.length; c++) {
      var row = state.chainData.contracts[c];
      var match = byKey[corridorKey(row.expiry, row.strike, row.right)];
      if (!match) continue;
      touched = true;
      row.bid = match.bid;
      row.ask = match.ask;
      row.mid = match.mid;
      if (match.mid !== null && match.mid !== undefined) row.mark_price = match.mid;
      row.last = match.last;
      row.close = match.close;
      if (match.volume !== null && match.volume !== undefined) row.volume = match.volume;
      if (match.open_interest !== null && match.open_interest !== undefined) {
        row.open_interest = match.open_interest;
      }
      if (match.iv !== null && match.iv !== undefined) row.iv = match.iv;
      if (match.delta !== null && match.delta !== undefined) row.delta = match.delta;
      if (match.gamma !== null && match.gamma !== undefined) row.gamma = match.gamma;
      if (match.theta !== null && match.theta !== undefined) row.theta = match.theta;
      if (match.vega !== null && match.vega !== undefined) row.vega = match.vega;
      if (match.greeks_source && match.greeks_source !== "NONE") {
        row.greeks_source = match.greeks_source;
      }
    }

    if (touched) {
      refreshStrategyLegsQuotesOnly();
      renderTable(state.chainData);
    }

    if (
      dom.corridorAutoReprice &&
      dom.corridorAutoReprice.checked &&
      state.strategyLegs.length &&
      (bypassThrottle || (!state.strategyBusy && Date.now() - state.corridorLastRepriceAt >= 2000))
    ) {
      evaluateStrategyRequest({ silent: true });
    }
    return touched;
  }

  function applyCorridorFrame(frame, options) {
    var opts = options || {};
    state.corridorFrame = frame;
    updateCorridorStatusUI(frame, opts.modeLabel);
    renderCorridorKpis(frame);
    renderCorridorLadder(frame);
    if (isPanelVisible("corridor")) {
      renderCorridorChart(frame, themeVars());
    }
    if (opts.mergeIntoChain) {
      mergeCorridorQuotesIntoChain(frame, opts);
    }
  }

  function syncCorridorFromChain(data) {
    if (state.corridorStreaming || state.corridorStreamSource) {
      stopCorridorStream({ silent: true });
    }
    state.corridorPrevMids = {};
    if (dom.corridorStreamToggle) dom.corridorStreamToggle.checked = false;

    if (dom.corridorExpirySelect) {
      var prev = dom.corridorExpirySelect.value;
      dom.corridorExpirySelect.innerHTML = "";
      var exps = strategySortedExpiries(data);
      if (!exps.length) {
        var opt0 = document.createElement("option");
        opt0.value = "";
        opt0.textContent = "Front DTE";
        dom.corridorExpirySelect.appendChild(opt0);
      } else {
        var snappedCorridorExp = snapDteValueToExpiries(prev, exps, {
          allowBuckets: false,
          emptyValue: exps[0].expiry,
        });
        for (var i = 0; i < exps.length; i++) {
          var opt = document.createElement("option");
          opt.value = exps[i].expiry;
          opt.textContent = formatExpiryIso(exps[i].expiry) + " (" + exps[i].dte + "d)";
          if (snappedCorridorExp && snappedCorridorExp === exps[i].expiry) opt.selected = true;
          dom.corridorExpirySelect.appendChild(opt);
        }
        if (snappedCorridorExp) {
          dom.corridorExpirySelect.value = snappedCorridorExp;
        }
      }
    }

    var preview = buildLocalCorridorPreview(data);
    applyCorridorFrame(preview, { mergeIntoChain: false });
  }

  async function refreshCorridorQuotesOneShot() {
    if (!state.chainData || state.corridorBusy) {
      if (!state.chainData) {
        showBanner("warn", "Load or refresh an option snapshot before fetching Corridor quotes.");
      }
      return;
    }
    state.corridorBusy = true;
    if (dom.corridorRefreshBtn) {
      dom.corridorRefreshBtn.disabled = true;
      dom.corridorRefreshBtn.textContent = "FETCHING CORRIDOR...";
    }
    var winVal = dom.corridorWindowSelect ? parseInt(dom.corridorWindowSelect.value, 10) || 5 : 5;
    var expVal = dom.corridorExpirySelect && dom.corridorExpirySelect.value ? dom.corridorExpirySelect.value : null;
    try {
      var res = await apiJson("/api/corridor/quotes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ticker: state.ticker,
          snapshot_id: state.snapshotId || null,
          target_expiry: expVal,
          window_strikes: winVal,
          max_contracts: 56,
          priority_legs: corridorPriorityLegsList(),
        }),
      });
      if (res.ok && res.body) {
        applyCorridorFrame(res.body, { mergeIntoChain: true, bypassAutoThrottle: true });
        await fetchStatus();
        if (res.body.notices && res.body.notices.length) {
          showBanner("info", res.body.notices.join(" "));
        }
      } else if (res.status === 503) {
        await fetchStatus();
        showBanner(
          "warn",
          extractApiErrorMessage(
            res.body,
            res.textSnippet || "IBKR Gateway is offline. Preserving cached corridor view."
          ),
          {
            ticker: state.ticker,
            phase: "CORRIDOR",
            status: res.status,
            elapsedMs: res.elapsedMs,
            rawBody: res.textSnippet,
          }
        );
      } else {
        showBanner(
          "warn",
          extractApiErrorMessage(res.body, res.textSnippet || "Corridor quotes pull failed."),
          {
            ticker: state.ticker,
            phase: "CORRIDOR",
            status: res.status,
            elapsedMs: res.elapsedMs,
            rawBody: res.textSnippet,
          }
        );
      }
    } catch (err) {
      showBanner("error", "Corridor quote request failed.", {
        ticker: state.ticker,
        phase: "CORRIDOR",
        elapsedMs: err && err.elapsedMs,
        detail: err && err.message ? err.message : String(err || ""),
      });
    } finally {
      state.corridorBusy = false;
      if (dom.corridorRefreshBtn) {
        dom.corridorRefreshBtn.disabled = false;
        dom.corridorRefreshBtn.textContent = "Refresh Corridor Quotes";
      }
    }
  }

  function stopCorridorStream(options) {
    var opts = options || {};
    var wasActive = Boolean(state.corridorStreaming || state.corridorStreamSource);
    state.corridorStreaming = false;
    if (dom.corridorStreamToggle) dom.corridorStreamToggle.checked = false;
    if (state.corridorStreamSource) {
      try {
        state.corridorStreamSource.close();
      } catch (_err) {
        /* ignore */
      }
      state.corridorStreamSource = null;
    }
    if (wasActive && !opts.skipRemoteStop) {
      try {
        window.fetch("/api/corridor/stop", { method: "POST", keepalive: true }).catch(function () {});
      } catch (_err) {
        /* ignore */
      }
    }
    updateCorridorStatusUI(state.corridorFrame, opts.modeLabel || null);
    if (!opts.silent && wasActive) {
      showBanner("info", "Live Wall Corridor streaming stopped; IBKR subscriptions cancelled.");
    }
  }

  function startCorridorStream() {
    if (!state.chainData) {
      if (dom.corridorStreamToggle) dom.corridorStreamToggle.checked = false;
      showBanner("warn", "Load or refresh an option snapshot before enabling Live Streaming.");
      return;
    }
    stopCorridorStream({ silent: true });

    var winVal = dom.corridorWindowSelect ? parseInt(dom.corridorWindowSelect.value, 10) || 5 : 5;
    var expVal = dom.corridorExpirySelect && dom.corridorExpirySelect.value ? dom.corridorExpirySelect.value : "";
    var params = new URLSearchParams();
    if (state.snapshotId) params.set("snapshot_id", state.snapshotId);
    if (expVal) params.set("target_expiry", expVal);
    params.set("window_strikes", String(winVal));
    params.set("max_contracts", "56");
    var legParam = corridorPriorityLegsParam();
    if (legParam) params.set("priority_legs", legParam);

    var url = "/api/corridor/stream/" + encodeURIComponent(state.ticker) + "?" + params.toString();
    state.corridorStreaming = true;
    if (dom.corridorStreamToggle) dom.corridorStreamToggle.checked = true;
    updateCorridorStatusUI(state.corridorFrame, "CONNECTING STREAM...");

    var es = new window.EventSource(url);
    state.corridorStreamSource = es;

    es.addEventListener("frame", function (ev) {
      if (!state.corridorStreaming) return;
      try {
        var frame = JSON.parse(ev.data);
        applyCorridorFrame(frame, { mergeIntoChain: true });
      } catch (_err) {
        /* ignore malformed tick */
      }
    });

    es.addEventListener("error", function (ev) {
      if (ev && ev.data) {
        var msg = "IBKR Gateway error during live corridor stream.";
        try {
          var errObj = JSON.parse(ev.data);
          if (errObj && errObj.message) msg = errObj.message;
        } catch (_err) {
          /* ignore */
        }
        stopCorridorStream({ silent: true, skipRemoteStop: true, modeLabel: "STREAM OFFLINE" });
        fetchStatus();
        showBanner("warn", msg);
      }
    });

    es.onerror = function () {
      if (!state.corridorStreaming) return;
      stopCorridorStream({ silent: true, modeLabel: "STREAM DISCONNECTED" });
      fetchStatus();
      showBanner(
        "warn",
        "Live corridor stream interrupted or IBKR Gateway unreachable (Port 4001). Subscriptions cancelled."
      );
    };
  }

  // ------------------------------------------------------------------ Network Requests
  async function apiJson(url, options) {
    var opts = options ? Object.assign({}, options) : {};
    var timeoutMs = Number(opts.timeoutMs || 0);
    delete opts.timeoutMs;

    var timerId = null;
    var timedOut = false;
    var controller = null;
    if (timeoutMs > 0 && typeof AbortController !== "undefined" && !opts.signal) {
      controller = new AbortController();
      opts.signal = controller.signal;
      timerId = window.setTimeout(function () {
        timedOut = true;
        try {
          controller.abort();
        } catch (_abortErr) {
          /* ignore */
        }
      }, timeoutMs);
    }

    var startedAt = Date.now();
    try {
      var resp = await window.fetch(url, opts);
      var elapsedMs = Date.now() - startedAt;
      var rawText = "";
      var body = null;
      if (typeof resp.text === "function") {
        try {
          rawText = await resp.text();
          if (rawText && rawText.trim()) {
            body = JSON.parse(rawText);
          }
        } catch (_parseErr) {
          body = null;
        }
      } else if (typeof resp.json === "function") {
        try {
          body = await resp.json();
        } catch (_jsonErr) {
          body = null;
        }
      }
      var snippet = rawText && body === null ? rawText.trim().slice(0, 280) : "";
      return {
        ok: resp.ok,
        status: resp.status,
        body: body,
        textSnippet: snippet,
        elapsedMs: elapsedMs,
        timedOut: timedOut,
      };
    } catch (err) {
      if (err && typeof err === "object") {
        err.elapsedMs = Date.now() - startedAt;
        err.timedOut = timedOut || err.name === "AbortError";
      }
      throw err;
    } finally {
      if (timerId !== null) {
        window.clearTimeout(timerId);
      }
    }
  }

  async function fetchStatus(tickerOverride) {
    var targetTicker = tickerOverride || state.ticker;
    var url = "/api/status";
    if (targetTicker) {
      url += "?ticker=" + encodeURIComponent(targetTicker);
    }
    try {
      var res = await apiJson(url);
      if (res.ok && res.body) {
        renderStatusBadges(res.body);
      }
    } catch (_err) {
      renderStatusBadges({ connected: false, default_market_data_type: 3 });
    }
  }

  async function fetchSnapshots(ticker) {
    var res = await apiJson("/api/snapshots/" + encodeURIComponent(ticker));
    if (res.ok && res.body) {
      renderSnapshotSelect(res.body);
      return;
    }
    renderSnapshotSelect({ snapshots: [] });
  }

  async function loadChain(ticker, snapshotId) {
    if (state.corridorStreaming || state.corridorStreamSource) {
      stopCorridorStream({ silent: true });
    }
    var prevTicker = state.ticker;
    state.ticker = ticker;
    state.snapshotId = snapshotId || "";
    setActiveTickerUI(ticker);
    if (ticker !== prevTicker) {
      applyTickerPullPrefsToControls(ticker);
    }

    await fetchSnapshots(ticker);
    var url = "/api/chain/" + encodeURIComponent(ticker);
    if (snapshotId) {
      url += "?snapshot_id=" + encodeURIComponent(snapshotId);
    }

    var res;
    try {
      res = await apiJson(url);
    } catch (err) {
      showBanner("error", "Unable to load option chain for " + ticker + ".", {
        ticker: ticker,
        phase: "CHAIN_LOAD",
        elapsedMs: err && err.elapsedMs,
        detail: err && err.message ? err.message : String(err || ""),
      });
      return;
    }

    if (res.ok && res.body) {
      try {
        state.chainData = res.body;
        state.snapshotId = res.body.meta.snapshot_id;
        updateExportLink();
        syncControlsFromMeta(res.body.meta);
        renderChainBadges(res.body.meta);
        renderKpis(res.body);
        populateDteFilter(res.body);
        populateTableHeaderFilters(res.body);
        populateWallsDteFilter(res.body);
        syncStrategySelectors(res.body);
        syncCorridorFromChain(res.body);
        renderAllCharts(res.body);
        renderTable(res.body);
        maybeAutoPopulateStrategy(res.body);

        if (res.body.meta.notices && res.body.meta.notices.length) {
          showBanner("info", res.body.meta.notices.join(" "), {
            ticker: ticker,
            phase: "CHAIN_LOAD",
            elapsedMs: res.elapsedMs,
          });
        } else {
          showBanner("", "");
        }
      } catch (renderErr) {
        if (window.console && window.console.error) {
          window.console.error(renderErr);
        }
        var renderMsg =
          renderErr && renderErr.message ? renderErr.message : String(renderErr || "Render error");
        showBanner("warn", "Snapshot loaded, UI render warning: " + renderMsg, {
          ticker: ticker,
          phase: "UI_RENDER",
          elapsedMs: res.elapsedMs,
          detail: renderErr && renderErr.stack ? String(renderErr.stack).slice(0, 400) : renderMsg,
        });
      }
      return;
    }

    if (res.status === 404) {
      state.chainData = null;
      state.strategyLoadedTicker = "";
      resetControlCapabilities();
      applyTickerPullPrefsToControls(ticker);
      await fetchStatus(ticker);
      purgeCharts();
      renderChainBadges(null);
      renderKpis(null);
      populateDteFilter(null);
      populateTableHeaderFilters(null);
      populateWallsDteFilter(null);
      syncStrategySelectors(null);
      syncCorridorFromChain(null);
      renderTable(null);
      showBanner(
        "info",
        "No cached snapshot exists for " +
          ticker +
          ". Click REFRESH FROM IBKR to pull and archive live/delayed chain data.",
        { ticker: ticker, phase: "CHAIN_LOAD", status: 404, elapsedMs: res.elapsedMs }
      );
      return;
    }

    var errMsg = extractApiErrorMessage(
      res.body,
      res.textSnippet || "Unable to load option chain for " + ticker + "."
    );
    showBanner("error", errMsg, {
      ticker: ticker,
      phase: "CHAIN_LOAD",
      status: res.status,
      errorCode: res.body && res.body.error_code,
      elapsedMs: res.elapsedMs,
      detail: res.body && res.body.detail ? String(res.body.detail) : "",
      rawBody: res.textSnippet,
    });
  }

  async function refreshFromIBKR() {
    if (state.loading) return;
    if (state.corridorStreaming || state.corridorStreamSource) {
      stopCorridorStream({ silent: true });
    }
    state.loading = true;
    if (dom.refreshBtn) {
      dom.refreshBtn.disabled = true;
      dom.refreshBtn.textContent = "REFRESHING...";
    }

    var rangeVal = dom.rangeSelect ? Number(dom.rangeSelect.value) : 0.2;
    var stepRaw = dom.stepSelect ? dom.stepSelect.value : "auto";
    var stepVal = stepRaw === "auto" ? null : Number(stepRaw);
    var dteMaxVal = dom.dteMaxSelect ? Number(dom.dteMaxSelect.value) : 120;
    var pullScope = buildActivePullScopeText();
    writeTickerPullPrefs(state.ticker);

    try {
      // Stage 1: HTTP / IBKR Gateway Pull
      var res;
      try {
        res = await apiJson("/api/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          timeoutMs: 120000,
          body: JSON.stringify({
            ticker: state.ticker,
            range_pct: rangeVal,
            strike_step: stepVal,
            dte_min: 30,
            dte_max: dteMaxVal,
          }),
        });
      } catch (httpErr) {
        await fetchStatus();
        var rawErrMsg =
          httpErr && httpErr.message ? httpErr.message : String(httpErr || "Request aborted");
        var isTimeout = Boolean(httpErr && httpErr.timedOut);
        var bannerTitle = isTimeout
          ? "IBKR refresh timed out after 120s. Preserving cached snapshot view."
          : "Network or gateway interruption during refresh (" +
            rawErrMsg +
            "). Preserving cached snapshot view.";
        showBanner("warn", bannerTitle, {
          ticker: state.ticker,
          pullScope: pullScope,
          phase: isTimeout ? "TIMEOUT" : "REFRESH_HTTP",
          elapsedMs: httpErr && httpErr.elapsedMs,
          detail: rawErrMsg,
          hint: isTimeout
            ? "Try narrowing Range %, raising Strike Step ($5/$10), or lowering Max DTE before retrying."
            : "Verify IBKR Gateway Port 4001 and FastAPI server availability; cached snapshot remains active.",
        });
        return;
      }

      if (!res.ok || !res.body) {
        await fetchStatus();
        var fallbackMsg =
          res.status === 503
            ? "IBKR Gateway is offline or unreachable. Preserving cached snapshot view."
            : "Refresh failed (HTTP " + res.status + "). Preserving cached snapshot view.";
        var apiMsg = extractApiErrorMessage(res.body, res.textSnippet || fallbackMsg);
        showBanner(res.status === 503 ? "warn" : "error", apiMsg, {
          ticker: state.ticker,
          pullScope: pullScope,
          phase: res.status === 503 ? "GATEWAY" : "REFRESH_HTTP",
          status: res.status,
          errorCode: res.body && res.body.error_code,
          elapsedMs: res.elapsedMs,
          detail: res.body && res.body.detail ? String(res.body.detail) : "",
          rawBody: res.textSnippet,
          hint:
            res.status === 503
              ? "Check IBKR Gateway connection on Port 4001; cached view preserved."
              : "Preserving previous cached snapshot view.",
        });
        return;
      }

      // Stage 2: Post-Refresh DOM / Plotly Render
      try {
        state.chainData = res.body;
        state.snapshotId = res.body.meta.snapshot_id;
        updateExportLink();
        await fetchSnapshots(state.ticker);
        await fetchStatus();
        syncControlsFromMeta(res.body.meta, { fromRefresh: true });
        renderChainBadges(res.body.meta);
        renderKpis(res.body);
        populateDteFilter(res.body);
        populateTableHeaderFilters(res.body);
        populateWallsDteFilter(res.body);
        syncStrategySelectors(res.body);
        syncCorridorFromChain(res.body);
        renderAllCharts(res.body);
        renderTable(res.body);
        maybeAutoPopulateStrategy(res.body);
        var msg =
          res.body.meta.notices && res.body.meta.notices.length
            ? res.body.meta.notices.join(" ")
            : res.body.meta.exchange_native_verified &&
                res.body.meta.exchange_validated_strike_count
              ? "Archived IBKR snapshot " +
                res.body.meta.snapshot_id +
                " (" +
                res.body.meta.exchange_validated_strike_count +
                " exchange-validated strikes)."
              : "Archived new IBKR snapshot " + res.body.meta.snapshot_id + ".";
        showBanner("info", msg, {
          ticker: state.ticker,
          pullScope: pullScope,
          phase: "REFRESH_OK",
          elapsedMs: res.elapsedMs,
        });
      } catch (renderErr) {
        if (window.console && window.console.error) {
          window.console.error(renderErr);
        }
        var renderErrMsg =
          renderErr && renderErr.message ? renderErr.message : String(renderErr || "Render error");
        showBanner("warn", "Snapshot saved, UI render warning: " + renderErrMsg, {
          ticker: state.ticker,
          pullScope: pullScope,
          phase: "UI_RENDER",
          elapsedMs: res.elapsedMs,
          detail:
            renderErr && renderErr.stack ? String(renderErr.stack).slice(0, 400) : renderErrMsg,
          hint: "The CSV snapshot was archived on disk; reload or adjust panel view if charts look stale.",
        });
      }
    } finally {
      state.loading = false;
      if (dom.refreshBtn) {
        dom.refreshBtn.disabled = false;
        dom.refreshBtn.textContent = "REFRESH FROM IBKR";
      }
    }
  }

  // ------------------------------------------------------------------ Event Bindings
  function bindEvents() {
    if (dom.themeToggle) {
      dom.themeToggle.addEventListener("click", function () {
        var next = currentTheme() === "terminal" ? "print" : "terminal";
        document.documentElement.setAttribute("data-theme", next);
        try {
          window.localStorage.setItem("ibkr_options_theme", next);
        } catch (_err) {
          /* ignore */
        }
        updateThemeToggleLabel();
        if (state.chainData) {
          renderAllCharts(state.chainData);
        }
      });
    }

    if (dom.watchlistPills) {
      dom.watchlistPills.addEventListener("click", function (ev) {
        var target = ev.target;
        if (target && target.matches && target.matches(".pill-btn")) {
          var sym = target.getAttribute("data-ticker");
          if (sym) {
            if (dom.customInput) dom.customInput.value = "";
            loadChain(sym, "");
          }
        }
      });
    }

    if (dom.customForm) {
      dom.customForm.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var raw = dom.customInput ? dom.customInput.value.trim().toUpperCase() : "";
        if (!raw) return;
        loadChain(raw, "");
      });
    }

    if (dom.snapshotSelect) {
      dom.snapshotSelect.addEventListener("change", function () {
        var snapId = dom.snapshotSelect.value;
        loadChain(state.ticker, snapId);
      });
    }

    if (dom.refreshBtn) {
      dom.refreshBtn.addEventListener("click", refreshFromIBKR);
    }

    if (dom.alertBanner) {
      dom.alertBanner.addEventListener("click", function (ev) {
        var closeBtn =
          ev.target && ev.target.closest ? ev.target.closest("#alert-banner-close, .alert-banner-close") : null;
        if (closeBtn) {
          clearAlertBanner();
        }
      });
    }

    [dom.rangeSelect, dom.stepSelect, dom.dteMaxSelect].forEach(function (ctrl) {
      if (ctrl) {
        ctrl.addEventListener("change", function () {
          writeTickerPullPrefs(state.ticker);
        });
      }
    });

    if (dom.wallsCopyAiBtn) {
      dom.wallsCopyAiBtn.addEventListener("click", copyWallsAiPrompt);
    }

    if (dom.greeksMetricSelect) {
      dom.greeksMetricSelect.addEventListener("change", function () {
        if (state.chainData && isPanelVisible("greeks")) {
          renderGreeksChart(state.chainData, themeVars());
        }
      });
    }

    if (dom.panelVisibilityControls) {
      dom.panelVisibilityControls.addEventListener("click", function (ev) {
        var target =
          ev.target && ev.target.closest ? ev.target.closest("[data-panel-target]") : null;
        if (!target) return;
        var panelId = target.getAttribute("data-panel-target");
        if (!panelId || !Object.prototype.hasOwnProperty.call(DEFAULT_PANEL_VISIBILITY, panelId)) {
          return;
        }
        var nextVis = Object.assign({}, state.panelVisibility);
        nextVis[panelId] = !isPanelVisible(panelId);
        applyPanelVisibility(nextVis, { persist: true });
      });
    }

    if (dom.panelVisibilityReset) {
      dom.panelVisibilityReset.addEventListener("click", function () {
        applyPanelVisibility(cloneDefaultPanelVisibility(), { clearOverrides: true });
      });
    }

    function onWallsControlChange() {
      if (dom.wallsViewSelect) writePref("ibkr_walls_view", dom.wallsViewSelect.value);
      if (dom.wallsMetricSelect) writePref("ibkr_walls_metric", dom.wallsMetricSelect.value);
      if (dom.wallsSplitDte) writePref("ibkr_walls_split_dte", dom.wallsSplitDte.checked);
      if (dom.wallsShowAnomalies) {
        writePref("ibkr_walls_show_anomalies", dom.wallsShowAnomalies.checked);
      }
      syncWallsSplitCheckboxState();
      if (state.chainData && isPanelVisible("walls")) {
        renderWallsChart(state.chainData, themeVars());
      }
    }

    [
      dom.wallsViewSelect,
      dom.wallsMetricSelect,
      dom.wallsDteSelect,
      dom.wallsSplitDte,
      dom.wallsShowAnomalies,
    ].forEach(function (ctrl) {
      if (ctrl) {
        ctrl.addEventListener("change", onWallsControlChange);
      }
    });

    [dom.filterDte, dom.filterRight, dom.filterUnusual].forEach(function (ctrl) {
      if (ctrl) {
        ctrl.addEventListener("change", function () {
          if (ctrl === dom.filterDte && dom.thFilterExpiry) {
            var dVal = dom.filterDte.value || "ALL";
            var expVal = dVal === "ALL" || dVal.indexOf("BUCKET:") === 0 ? "" : dVal;
            dom.thFilterExpiry.value = expVal;
            state.tableFilters.expiry = expVal;
          }
          if (ctrl === dom.filterRight && dom.thFilterRight) {
            var rVal = dom.filterRight.value === "ALL" ? "" : dom.filterRight.value;
            dom.thFilterRight.value = rVal;
            state.tableFilters.right = rVal;
          }
          updateHeaderFilterHighlights();
          syncDteBucketTabs(state.chainData);
          renderTable(state.chainData);
        });
      }
    });

    if (dom.dteBucketTabs) {
      dom.dteBucketTabs.addEventListener("click", function (ev) {
        var btn =
          ev.target && ev.target.closest ? ev.target.closest("[data-dte-bucket]") : null;
        if (!btn || btn.disabled) return;
        var bucketVal = btn.getAttribute("data-dte-bucket") || "ALL";
        if (dom.filterDte) {
          dom.filterDte.value = bucketVal;
        }
        if (dom.thFilterExpiry) {
          dom.thFilterExpiry.value = "";
          state.tableFilters.expiry = "";
          updateHeaderFilterHighlights();
        }
        syncDteBucketTabs(state.chainData);
        renderTable(state.chainData);
      });
    }

    if (dom.tableResetFiltersBtn) {
      dom.tableResetFiltersBtn.addEventListener("click", resetAllTableFilters);
    }

    if (dom.tableBody) {
      dom.tableBody.addEventListener("click", function (ev) {
        var legBtn =
          ev.target && ev.target.closest ? ev.target.closest(".chain-add-leg-btn") : null;
        if (!legBtn) return;
        addSpecificStrategyLeg(
          legBtn.getAttribute("data-leg-expiry"),
          legBtn.getAttribute("data-leg-right"),
          legBtn.getAttribute("data-leg-strike")
        );
      });
    }

    if (dom.chainTableHead) {
      dom.chainTableHead.addEventListener("click", function (ev) {
        var sortBtn =
          ev.target && ev.target.closest ? ev.target.closest(".th-sort-btn[data-sort-col]") : null;
        if (!sortBtn) return;
        var col = sortBtn.getAttribute("data-sort-col");
        var colDef = col ? TABLE_COLUMN_BY_KEY[col] : null;
        if (!colDef) return;

        var firstDir = colDef.defaultDir || "asc";
        var secondDir = firstDir === "asc" ? "desc" : "asc";
        if (!state.tableSort || state.tableSort.col !== col) {
          state.tableSort = { col: col, dir: firstDir };
        } else if (state.tableSort.dir === firstDir) {
          state.tableSort = { col: col, dir: secondDir };
        } else {
          state.tableSort = { col: null, dir: null };
        }
        writeTableSortPref(state.tableSort);
        syncTableHeaderSortUI();
        renderTable(state.chainData);
      });

      function onHeaderFilterChange(ev) {
        var target = ev.target;
        if (!target || !target.getAttribute) return;
        var col = target.getAttribute("data-filter-col");
        if (!col || !TABLE_COLUMN_BY_KEY[col]) return;

        var val = String(target.value || "").trim();
        state.tableFilters[col] = val;

        if (col === "expiry" && dom.filterDte) {
          dom.filterDte.value = val || "ALL";
          syncDteBucketTabs(state.chainData);
        } else if (col === "right" && dom.filterRight) {
          dom.filterRight.value = val || "ALL";
        }

        updateHeaderFilterHighlights();
        renderTable(state.chainData);
      }

      dom.chainTableHead.addEventListener("input", function (ev) {
        if (ev.target && ev.target.classList && ev.target.classList.contains("th-filter-input")) {
          onHeaderFilterChange(ev);
        }
      });

      dom.chainTableHead.addEventListener("change", function (ev) {
        if (ev.target && ev.target.classList && ev.target.classList.contains("th-filter-select")) {
          onHeaderFilterChange(ev);
        }
      });
    }

    if (dom.strategyPresetPills) {
      dom.strategyPresetPills.addEventListener("click", function (ev) {
        var btn =
          ev.target && ev.target.closest ? ev.target.closest("[data-strategy-preset]") : null;
        if (!btn) return;
        var preset = btn.getAttribute("data-strategy-preset") || "BULL_PUT_SPREAD";
        setActiveStrategyPresetUI(preset);
        if (preset === "CUSTOM") {
          if (!state.strategyLegs.length && state.chainData) {
            state.strategyLegs = buildSeedLegsFromChain(state.chainData);
            renderStrategyLegsTable();
          }
          if (dom.strategyRationale) {
            dom.strategyRationale.textContent =
              "Custom leg builder active. Edit strikes/rights below and click Evaluate / Re-Price.";
          }
          return;
        }
        autoBuildStrategy(preset);
      });
    }

    if (dom.strategyAutoBuildBtn) {
      dom.strategyAutoBuildBtn.addEventListener("click", function () {
        autoBuildStrategy(state.strategyKind);
      });
    }

    if (dom.strategyEvaluateBtn) {
      dom.strategyEvaluateBtn.addEventListener("click", function () {
        evaluateStrategyRequest();
      });
    }

    if (dom.strategyAddLegBtn) {
      dom.strategyAddLegBtn.addEventListener("click", function () {
        addCustomStrategyLegRow();
      });
    }

    if (dom.strategyAutoPopulate) {
      dom.strategyAutoPopulate.addEventListener("change", function () {
        writePref(
          STRATEGY_AUTO_POPULATE_KEY,
          dom.strategyAutoPopulate.checked ? "true" : "false"
        );
        if (
          dom.strategyAutoPopulate.checked &&
          state.chainData &&
          !state.strategyEvaluation &&
          !state.strategyBusy
        ) {
          maybeAutoPopulateStrategy(state.chainData);
        }
      });
    }

    if (dom.strategyResetBtn) {
      dom.strategyResetBtn.addEventListener("click", function () {
        resetStrategyEvaluationUI(true);
      });
    }

    if (dom.strategyDaysSlider) {
      dom.strategyDaysSlider.addEventListener("input", function () {
        renderStrategySlidersMax(dom.strategyDaysSlider.max);
      });
      dom.strategyDaysSlider.addEventListener("change", function () {
        renderStrategySlidersMax(dom.strategyDaysSlider.max);
        if (state.chainData && state.strategyLegs.length) {
          evaluateStrategyRequest();
        }
      });
    }

    if (dom.strategyIvSlider) {
      dom.strategyIvSlider.addEventListener("input", function () {
        renderStrategySlidersMax(dom.strategyDaysSlider ? dom.strategyDaysSlider.max : 120);
      });
      dom.strategyIvSlider.addEventListener("change", function () {
        renderStrategySlidersMax(dom.strategyDaysSlider ? dom.strategyDaysSlider.max : 120);
        if (state.chainData && state.strategyLegs.length) {
          evaluateStrategyRequest();
        }
      });
    }

    function onCorridorControlChange() {
      if (state.corridorStreaming || state.corridorStreamSource) {
        stopCorridorStream({ silent: true });
      }
      if (state.chainData) {
        applyCorridorFrame(buildLocalCorridorPreview(state.chainData), { mergeIntoChain: false });
      }
    }

    if (dom.corridorExpirySelect) {
      dom.corridorExpirySelect.addEventListener("change", onCorridorControlChange);
    }
    if (dom.corridorWindowSelect) {
      dom.corridorWindowSelect.addEventListener("change", onCorridorControlChange);
    }
    if (dom.corridorRefreshBtn) {
      dom.corridorRefreshBtn.addEventListener("click", refreshCorridorQuotesOneShot);
    }
    if (dom.corridorStreamToggle) {
      dom.corridorStreamToggle.addEventListener("change", function () {
        if (dom.corridorStreamToggle.checked) {
          startCorridorStream();
        } else {
          stopCorridorStream();
        }
      });
    }
    if (dom.corridorAutoReprice) {
      dom.corridorAutoReprice.addEventListener("change", function () {
        if (
          dom.corridorAutoReprice.checked &&
          state.chainData &&
          state.strategyLegs.length &&
          state.corridorFrame &&
          (state.corridorStreaming || state.corridorFrame.source !== "CACHED_PREVIEW")
        ) {
          evaluateStrategyRequest({ silent: true });
        }
      });
    }

    window.addEventListener("pagehide", function () {
      if (state.corridorStreaming || state.corridorStreamSource) {
        if (navigator.sendBeacon) {
          navigator.sendBeacon("/api/corridor/stop");
        }
        stopCorridorStream({ silent: true, skipRemoteStop: true });
      }
    });
  }

  function promptNum(val, digits) {
    if (val === null || val === undefined || isNaN(Number(val))) return "n/a";
    return Number(val).toFixed(digits === undefined ? 2 : digits);
  }

  function promptInt(val) {
    if (val === null || val === undefined || isNaN(Number(val))) return "0";
    return Math.round(Number(val)).toLocaleString("en-US");
  }

  function promptExpiryIso(raw) {
    var s = String(raw || "");
    if (/^\d{8}$/.test(s)) {
      return s.slice(0, 4) + "-" + s.slice(4, 6) + "-" + s.slice(6, 8);
    }
    return s || "n/a";
  }

  function promptGreekVal(val, digits) {
    if (val === null || val === undefined || isNaN(Number(val))) return "n/a";
    return Number(val).toFixed(digits);
  }

  function promptGammaVal(val) {
    if (val === null || val === undefined || isNaN(Number(val))) return "n/a";
    var num = Number(val);
    var abs = Math.abs(num);
    var digits = abs > 0 && abs < 0.005 ? 4 : 3;
    return num.toFixed(digits);
  }

  function promptThetaVal(val) {
    if (val === null || val === undefined || isNaN(Number(val))) return "n/a";
    var num = Number(val);
    var abs = Math.abs(num);
    var digits = abs > 0 && abs < 0.02 ? 3 : 2;
    return num.toFixed(digits);
  }

  function firstValidNum(primary, fallback) {
    if (primary !== null && primary !== undefined && !isNaN(Number(primary))) {
      return Number(primary);
    }
    if (fallback !== null && fallback !== undefined && !isNaN(Number(fallback))) {
      return Number(fallback);
    }
    return null;
  }

  function formatPromptGreeksInline(ivVal, deltaVal, gammaVal, thetaVal) {
    var ivStr =
      ivVal !== null && ivVal !== undefined && !isNaN(Number(ivVal))
        ? promptNum(Number(ivVal) * 100, 1) + "%"
        : "n/a";
    return (
      "IV " +
      ivStr +
      ", Δ " +
      promptGreekVal(deltaVal, 2) +
      ", Γ " +
      promptGammaVal(gammaVal) +
      ", Θ " +
      promptThetaVal(thetaVal)
    );
  }

  function computeScopedWallsForPrompt(activeExpiries, spot, contractByKey) {
    var byStrike = {};
    var cMap = contractByKey || {};
    var totalCallOi = 0;
    var totalPutOi = 0;
    var totalCallVol = 0;
    var totalPutVol = 0;

    function addMetric(row, prefix, metric, val) {
      if (val === null || val === undefined || isNaN(Number(val))) return;
      row[prefix + "_" + metric + "_sum"] += Number(val);
      row[prefix + "_" + metric + "_cnt"] += 1;
    }

    for (var i = 0; i < activeExpiries.length; i++) {
      var expObj = activeExpiries[i];
      var expKey = String(expObj.expiry || "");
      var strikes = expObj && expObj.strikes ? expObj.strikes : [];
      for (var j = 0; j < strikes.length; j++) {
        var pt = strikes[j];
        var k = Number(pt.strike);
        if (isNaN(k)) continue;
        var key = String(k);
        if (!byStrike[key]) {
          byStrike[key] = {
            strike: k,
            call_oi: 0,
            put_oi: 0,
            call_volume: 0,
            put_volume: 0,
            call_iv_sum: 0,
            call_iv_cnt: 0,
            put_iv_sum: 0,
            put_iv_cnt: 0,
            call_delta_sum: 0,
            call_delta_cnt: 0,
            put_delta_sum: 0,
            put_delta_cnt: 0,
            call_gamma_sum: 0,
            call_gamma_cnt: 0,
            put_gamma_sum: 0,
            put_gamma_cnt: 0,
            call_theta_sum: 0,
            call_theta_cnt: 0,
            put_theta_sum: 0,
            put_theta_cnt: 0,
          };
        }
        var row = byStrike[key];
        var coi = Number(pt.call_oi || 0);
        var poi = Number(pt.put_oi || 0);
        var cvol = Number(pt.call_volume || 0);
        var pvol = Number(pt.put_volume || 0);
        row.call_oi += coi;
        row.put_oi += poi;
        row.call_volume += cvol;
        row.put_volume += pvol;
        totalCallOi += coi;
        totalPutOi += poi;
        totalCallVol += cvol;
        totalPutVol += pvol;

        var cRow = cMap[expKey + "|" + k + "|C"] || null;
        var pRow = cMap[expKey + "|" + k + "|P"] || null;
        addMetric(row, "call", "iv", firstValidNum(pt.call_iv, cRow ? cRow.iv : null));
        addMetric(row, "put", "iv", firstValidNum(pt.put_iv, pRow ? pRow.iv : null));
        addMetric(row, "call", "delta", firstValidNum(pt.call_delta, cRow ? cRow.delta : null));
        addMetric(row, "put", "delta", firstValidNum(pt.put_delta, pRow ? pRow.delta : null));
        addMetric(row, "call", "gamma", firstValidNum(pt.call_gamma, cRow ? cRow.gamma : null));
        addMetric(row, "put", "gamma", firstValidNum(pt.put_gamma, pRow ? pRow.gamma : null));
        addMetric(row, "call", "theta", firstValidNum(pt.call_theta, cRow ? cRow.theta : null));
        addMetric(row, "put", "theta", firstValidNum(pt.put_theta, pRow ? pRow.theta : null));
      }
    }

    function avgMetric(row, prefix, metric) {
      var cnt = row[prefix + "_" + metric + "_cnt"];
      return cnt > 0 ? row[prefix + "_" + metric + "_sum"] / cnt : null;
    }

    var ladder = Object.keys(byStrike)
      .map(function (k) {
        var r = byStrike[k];
        r.call_iv = avgMetric(r, "call", "iv");
        r.put_iv = avgMetric(r, "put", "iv");
        r.call_delta = avgMetric(r, "call", "delta");
        r.put_delta = avgMetric(r, "put", "delta");
        r.call_gamma = avgMetric(r, "call", "gamma");
        r.put_gamma = avgMetric(r, "put", "gamma");
        r.call_theta = avgMetric(r, "call", "theta");
        r.put_theta = avgMetric(r, "put", "theta");
        return r;
      })
      .sort(function (a, b) {
        return a.strike - b.strike;
      });

    function topN(field, n, side) {
      return ladder
        .filter(function (r) {
          return Number(r[field] || 0) > 0;
        })
        .slice()
        .sort(function (a, b) {
          if (b[field] !== a[field]) return b[field] - a[field];
          return Math.abs(a.strike - spot) - Math.abs(b.strike - spot);
        })
        .slice(0, n)
        .map(function (r) {
          var distPct = spot > 0 ? ((r.strike - spot) / spot) * 100 : 0;
          return {
            strike: r.strike,
            value: r[field],
            dist_pct: distPct,
            iv: r[side + "_iv"],
            delta: r[side + "_delta"],
            gamma: r[side + "_gamma"],
            theta: r[side + "_theta"],
          };
        });
    }

    return {
      ladder: ladder,
      totalCallOi: totalCallOi,
      totalPutOi: totalPutOi,
      totalCallVol: totalCallVol,
      totalPutVol: totalPutVol,
      topCallOi: topN("call_oi", 3, "call"),
      topPutOi: topN("put_oi", 3, "put"),
      topCallVol: topN("call_volume", 3, "call"),
      topPutVol: topN("put_volume", 3, "put"),
    };
  }

  function buildWallsAiPrompt(data) {
    if (!data || !data.meta || !data.profiles) return "";
    var meta = data.meta;
    var walls = data.walls || {};
    var spot = Number(meta.underlying_price || 0);
    var allExpiries = Array.isArray(data.profiles.expiries) ? data.profiles.expiries : [];
    // Multi-expiration mode: the prompt always covers every weekly expiry in the loaded
    // chain (independent of the #walls-dte-select chart scope).
    var isSingleDte = false;
    var activeExpiries = allExpiries;
    if (!activeExpiries.length) return "";

    var activeExpirySet = {};
    for (var ae = 0; ae < activeExpiries.length; ae++) {
      activeExpirySet[String(activeExpiries[ae].expiry)] = true;
    }

    var contracts = Array.isArray(data.contracts) ? data.contracts : [];
    var contractByConid = {};
    var contractByKey = {};
    var scopedSourceCounts = {};
    for (var ci = 0; ci < contracts.length; ci++) {
      var cItem = contracts[ci];
      if (!cItem) continue;
      if (cItem.option_conid !== null && cItem.option_conid !== undefined) {
        contractByConid[String(cItem.option_conid)] = cItem;
      }
      var cExp = String(cItem.expiry || "");
      var cKey = cExp + "|" + Number(cItem.strike) + "|" + String(cItem.right || "");
      contractByKey[cKey] = cItem;
      if (activeExpirySet[cExp] && cItem.greeks_source) {
        scopedSourceCounts[cItem.greeks_source] =
          (scopedSourceCounts[cItem.greeks_source] || 0) + 1;
      }
    }

    var scoped = computeScopedWallsForPrompt(activeExpiries, spot, contractByKey);
    var hasOi = scoped.totalCallOi + scoped.totalPutOi > 0;
    var selectedMetric = dom.wallsMetricSelect ? dom.wallsMetricSelect.value : "auto";
    var effectiveBasis =
      selectedMetric === "volume"
        ? "VOLUME"
        : selectedMetric === "oi"
        ? "OPEN_INTEREST"
        : hasOi
        ? walls.primary_basis || "OPEN_INTEREST"
        : "VOLUME";

    var rangeLabel =
      meta.range_pct !== null && meta.range_pct !== undefined
        ? formatRangeShort(meta.range_pct)
        : "n/a";
    var stepLabel =
      meta.strike_step !== null && meta.strike_step !== undefined
        ? "$" + promptNum(meta.strike_step, Number(meta.strike_step) % 1 === 0 ? 0 : 2)
        : "Auto";
    if (
      meta.requested_strike_step !== null &&
      meta.requested_strike_step !== undefined &&
      meta.strike_step !== null &&
      meta.strike_step !== undefined &&
      Math.abs(Number(meta.requested_strike_step) - Number(meta.strike_step)) > 1e-6
    ) {
      stepLabel +=
        " (żądano $" +
        promptNum(
          meta.requested_strike_step,
          Number(meta.requested_strike_step) % 1 === 0 ? 0 : 2
        ) +
        ")";
    }

    var ivSum = 0;
    var ivCnt = 0;
    for (var vi = 0; vi < activeExpiries.length; vi++) {
      var exIv = activeExpiries[vi].atm_iv;
      if (exIv !== null && exIv !== undefined && !isNaN(Number(exIv))) {
        ivSum += Number(exIv);
        ivCnt += 1;
      }
    }
    var avgAtmIvLabel = ivCnt > 0 ? promptNum((ivSum / ivCnt) * 100, 1) + "%" : "n/a";
    var greeksCounts = Object.keys(scopedSourceCounts).length
      ? scopedSourceCounts
      : meta.greeks_source_counts || {};
    var greeksSourceNames = Object.keys(greeksCounts);
    var greeksLabel = greeksSourceNames.length
      ? greeksSourceNames
          .map(function (k) {
            return k + ":" + greeksCounts[k];
          })
          .join(", ")
      : "n/a";
    var greeksSourceSummary = greeksSourceNames.length
      ? greeksSourceNames.join(" / ")
      : "n/a";

    var mdNames = {
      1: "TYPE 1 (Live OPRA)",
      2: "TYPE 2 (Frozen)",
      3: "TYPE 3 (15-min Delayed)",
      4: "TYPE 4 (Delayed-Frozen)",
    };
    var mdLabel = mdNames[meta.market_data_type] || "TYPE " + (meta.market_data_type || "?");

    var scopeTitle = isSingleDte
      ? "WYBRANE WYGAŚNIĘCIE (SINGLE DTE): " +
        activeExpiries[0].dte +
        " DTE (" +
        promptExpiryIso(activeExpiries[0].expiry) +
        ")"
      : "WSZYSTKIE TYGODNIOWE WYGAŚNIĘCIA (30\u2013120 DTE): " +
        activeExpiries
          .map(function (ex) {
            return ex.dte + "d (" + promptExpiryIso(ex.expiry) + ")";
          })
          .join(", ");

    var atmStats = {
      cDeltaSum: 0,
      cDeltaCnt: 0,
      pDeltaSum: 0,
      pDeltaCnt: 0,
      cGammaSum: 0,
      cGammaCnt: 0,
      pGammaSum: 0,
      pGammaCnt: 0,
      cThetaSum: 0,
      cThetaCnt: 0,
      pThetaSum: 0,
      pThetaCnt: 0,
    };

    var byDteList = Array.isArray(walls.by_dte) ? walls.by_dte : [];
    var dteSummaryLines = activeExpiries.map(function (ex) {
      var wMatch = null;
      for (var k = 0; k < byDteList.length; k++) {
        if (String(byDteList[k].expiry) === String(ex.expiry)) {
          wMatch = byDteList[k];
          break;
        }
      }
      var cw =
        wMatch && wMatch.primary_call_wall
          ? "$" +
            promptNum(wMatch.primary_call_wall.strike, 2) +
            " (" +
            promptInt(wMatch.primary_call_wall.value) +
            ")"
          : "n/a";
      var pw =
        wMatch && wMatch.primary_put_wall
          ? "$" +
            promptNum(wMatch.primary_put_wall.strike, 2) +
            " (" +
            promptInt(wMatch.primary_put_wall.value) +
            ")"
          : "n/a";
      var atmIv =
        ex.atm_iv !== null && ex.atm_iv !== undefined ? promptNum(Number(ex.atm_iv) * 100, 1) + "%" : "n/a";

      var targetAtm =
        ex.atm_strike !== null && ex.atm_strike !== undefined && !isNaN(Number(ex.atm_strike))
          ? Number(ex.atm_strike)
          : spot;
      var exStrikes = Array.isArray(ex.strikes) ? ex.strikes : [];
      var atmPt = null;
      var bestAtmDist = Infinity;
      for (var si = 0; si < exStrikes.length; si++) {
        var stCand = exStrikes[si];
        var d = Math.abs(Number(stCand.strike) - targetAtm);
        if (d < bestAtmDist) {
          bestAtmDist = d;
          atmPt = stCand;
        }
      }
      var atmCallRow = atmPt
        ? contractByKey[String(ex.expiry) + "|" + Number(atmPt.strike) + "|C"] || null
        : null;
      var atmPutRow = atmPt
        ? contractByKey[String(ex.expiry) + "|" + Number(atmPt.strike) + "|P"] || null
        : null;
      var acDelta = atmPt ? firstValidNum(atmPt.call_delta, atmCallRow ? atmCallRow.delta : null) : null;
      var apDelta = atmPt ? firstValidNum(atmPt.put_delta, atmPutRow ? atmPutRow.delta : null) : null;
      var acGamma = atmPt ? firstValidNum(atmPt.call_gamma, atmCallRow ? atmCallRow.gamma : null) : null;
      var apGamma = atmPt ? firstValidNum(atmPt.put_gamma, atmPutRow ? atmPutRow.gamma : null) : null;
      var acTheta = atmPt ? firstValidNum(atmPt.call_theta, atmCallRow ? atmCallRow.theta : null) : null;
      var apTheta = atmPt ? firstValidNum(atmPt.put_theta, atmPutRow ? atmPutRow.theta : null) : null;

      if (acDelta !== null) {
        atmStats.cDeltaSum += acDelta;
        atmStats.cDeltaCnt += 1;
      }
      if (apDelta !== null) {
        atmStats.pDeltaSum += apDelta;
        atmStats.pDeltaCnt += 1;
      }
      if (acGamma !== null) {
        atmStats.cGammaSum += acGamma;
        atmStats.cGammaCnt += 1;
      }
      if (apGamma !== null) {
        atmStats.pGammaSum += apGamma;
        atmStats.pGammaCnt += 1;
      }
      if (acTheta !== null) {
        atmStats.cThetaSum += acTheta;
        atmStats.cThetaCnt += 1;
      }
      if (apTheta !== null) {
        atmStats.pThetaSum += apTheta;
        atmStats.pThetaCnt += 1;
      }

      return (
        "- " +
        ex.dte +
        " DTE | Exp " +
        promptExpiryIso(ex.expiry) +
        " | ATM Strike $" +
        promptNum(ex.atm_strike, 2) +
        " | ATM IV " +
        atmIv +
        " | Call Wall: " +
        cw +
        " | Put Wall: " +
        pw +
        " | ATM Greki C[Δ " +
        promptGreekVal(acDelta, 2) +
        ", Γ " +
        promptGammaVal(acGamma) +
        ", Θ " +
        promptThetaVal(acTheta) +
        "] P[Δ " +
        promptGreekVal(apDelta, 2) +
        ", Γ " +
        promptGammaVal(apGamma) +
        ", Θ " +
        promptThetaVal(apTheta) +
        "]"
      );
    });

    var avgAtmCallDelta = atmStats.cDeltaCnt ? atmStats.cDeltaSum / atmStats.cDeltaCnt : null;
    var avgAtmPutDelta = atmStats.pDeltaCnt ? atmStats.pDeltaSum / atmStats.pDeltaCnt : null;
    var avgAtmCallGamma = atmStats.cGammaCnt ? atmStats.cGammaSum / atmStats.cGammaCnt : null;
    var avgAtmPutGamma = atmStats.pGammaCnt ? atmStats.pGammaSum / atmStats.pGammaCnt : null;
    var avgAtmCallTheta = atmStats.cThetaCnt ? atmStats.cThetaSum / atmStats.cThetaCnt : null;
    var avgAtmPutTheta = atmStats.pThetaCnt ? atmStats.pThetaSum / atmStats.pThetaCnt : null;
    var atmGreeksSummary =
      "Call [Δ " +
      promptGreekVal(avgAtmCallDelta, 2) +
      ", Γ " +
      promptGammaVal(avgAtmCallGamma) +
      ", Θ " +
      promptThetaVal(avgAtmCallTheta) +
      "] | Put [Δ " +
      promptGreekVal(avgAtmPutDelta, 2) +
      ", Γ " +
      promptGammaVal(avgAtmPutGamma) +
      ", Θ " +
      promptThetaVal(avgAtmPutTheta) +
      "]";

    function fmtTopList(items) {
      if (!items.length) return "brak niezerowych pozycji (0)";
      return items
        .map(function (item, idx) {
          var sign = item.dist_pct >= 0 ? "+" : "";
          return (
            "#" +
            (idx + 1) +
            " $" +
            promptNum(item.strike, 2) +
            " (" +
            sign +
            promptNum(item.dist_pct, 1) +
            "% od Spot, kontr.: " +
            promptInt(item.value) +
            ", " +
            formatPromptGreeksInline(item.iv, item.delta, item.gamma, item.theta) +
            ")"
          );
        })
        .join("; ");
    }

    var pcOiRatio =
      scoped.totalCallOi > 0 ? promptNum(scoped.totalPutOi / scoped.totalCallOi, 2) : "n/a";
    var pcVolRatio =
      scoped.totalCallVol > 0 ? promptNum(scoped.totalPutVol / scoped.totalCallVol, 2) : "n/a";

    var allAnomalies =
      data.anomalies && Array.isArray(data.anomalies.anomalies) ? data.anomalies.anomalies : [];
    var formatAnomalyLine = function (a) {
      var volOi =
        a.vol_oi_ratio !== null && a.vol_oi_ratio !== undefined
          ? promptNum(a.vol_oi_ratio, 2) + "x"
          : "n/a";
      var zsc =
        a.volume_zscore !== null && a.volume_zscore !== undefined
          ? "z=" + promptNum(a.volume_zscore, 2)
          : "z=n/a";
      var matchedContract =
        (a.option_conid !== null && a.option_conid !== undefined
          ? contractByConid[String(a.option_conid)]
          : null) ||
        contractByKey[
          String(a.expiry || "") + "|" + Number(a.strike) + "|" + String(a.right || "")
        ] ||
        null;
      var aIv = firstValidNum(a.iv, matchedContract ? matchedContract.iv : null);
      var aDelta = firstValidNum(a.delta, matchedContract ? matchedContract.delta : null);
      var aGamma = firstValidNum(a.gamma, matchedContract ? matchedContract.gamma : null);
      var aTheta = firstValidNum(a.theta, matchedContract ? matchedContract.theta : null);
      var aSrc =
        a.greeks_source ||
        (matchedContract && matchedContract.greeks_source) ||
        greeksSourceSummary;
      return (
        "- " +
        a.dte +
        "d (" +
        promptExpiryIso(a.expiry) +
        ") " +
        a.right +
        " $" +
        promptNum(a.strike, 2) +
        " | Vol: " +
        promptInt(a.volume) +
        " vs OI: " +
        promptInt(a.open_interest) +
        " (Vol/OI=" +
        volOi +
        ", " +
        zsc +
        ") | " +
        formatPromptGreeksInline(aIv, aDelta, aGamma, aTheta) +
        " [" +
        aSrc +
        "] | Flagi: " +
        (a.reasons && a.reasons.length ? a.reasons.join(", ") : "VOLUME")
      );
    };

    function formatLadderLines(sc) {
      var out = [
        "Strike | OdSpot% | Call_OI | Put_OI | Net_OI(C-P) | Call_Vol | Put_Vol | Call_IV% | Put_IV% | C_Δ | P_Δ | C_Γ | P_Γ | C_Θ | P_Θ",
      ];
      for (var m = 0; m < sc.ladder.length; m++) {
        out.push(formatLadderRow(sc.ladder[m]));
      }
      return out;
    }

    function formatLadderRow(st) {
      var dist = spot > 0 ? ((st.strike - spot) / spot) * 100 : 0;
      var cIv =
        st.call_iv !== null && st.call_iv !== undefined
          ? promptNum(st.call_iv * 100, 1) + "%"
          : "n/a";
      var pIv =
        st.put_iv !== null && st.put_iv !== undefined
          ? promptNum(st.put_iv * 100, 1) + "%"
          : "n/a";
      return (
        "$" +
          promptNum(st.strike, 2) +
          " | " +
          (dist >= 0 ? "+" : "") +
          promptNum(dist, 1) +
          "% | " +
          promptInt(st.call_oi) +
          " | " +
          promptInt(st.put_oi) +
          " | " +
          promptInt(st.call_oi - st.put_oi) +
          " | " +
          promptInt(st.call_volume) +
          " | " +
          promptInt(st.put_volume) +
          " | " +
          cIv +
          " | " +
          pIv +
          " | " +
          promptGreekVal(st.call_delta, 2) +
          " | " +
          promptGreekVal(st.put_delta, 2) +
          " | " +
          promptGammaVal(st.call_gamma) +
          " | " +
          promptGammaVal(st.put_gamma) +
          " | " +
          promptThetaVal(st.call_theta) +
          " | " +
          promptThetaVal(st.put_theta)
      );
    }

    var todayIso = meta.market_date || "n/a";
    var undName = meta.underlying || "n/a";

    // Per-expiry detail blocks (Section 3).
    var expiryBlocks = [];
    var totalAnomalyCount = 0;
    for (var ei = 0; ei < activeExpiries.length; ei++) {
      var exp = activeExpiries[ei];
      var expSc = computeScopedWallsForPrompt([exp], spot, contractByKey);
      var expAnoms = allAnomalies.filter(function (a) {
        return String(a.expiry) === String(exp.expiry);
      });
      totalAnomalyCount += expAnoms.length;
      var expAtmIv =
        exp.atm_iv !== null && exp.atm_iv !== undefined && !isNaN(Number(exp.atm_iv))
          ? promptNum(Number(exp.atm_iv) * 100, 1) + "%"
          : "n/a";
      var expPcOi =
        expSc.totalCallOi > 0 ? promptNum(expSc.totalPutOi / expSc.totalCallOi, 2) : "n/a";
      var expPcVol =
        expSc.totalCallVol > 0 ? promptNum(expSc.totalPutVol / expSc.totalCallVol, 2) : "n/a";
      var expCallShare =
        expSc.totalCallOi + expSc.totalPutOi > 0
          ? promptNum((expSc.totalCallOi / (expSc.totalCallOi + expSc.totalPutOi)) * 100, 0) + "%"
          : "n/a";
      var block = [
        "--- EXPIRY " +
          (ei + 1) +
          "/" +
          activeExpiries.length +
          ": " +
          exp.dte +
          " DTE (" +
          promptExpiryIso(exp.expiry) +
          ") | ATM IV " +
          expAtmIv +
          " | ATM Strike $" +
          promptNum(exp.atm_strike, 2) +
          " ---",
        "- Call OI: " +
          promptInt(expSc.totalCallOi) +
          " | Put OI: " +
          promptInt(expSc.totalPutOi) +
          " | P/C (OI): " +
          expPcOi +
          " | udział Call w OI: " +
          expCallShare,
        "- Call Vol: " +
          promptInt(expSc.totalCallVol) +
          " | Put Vol: " +
          promptInt(expSc.totalPutVol) +
          " | P/C (Vol): " +
          expPcVol,
        "- Top 3 Call Walls (OI): " + fmtTopList(expSc.topCallOi),
        "- Top 3 Put Walls (OI): " + fmtTopList(expSc.topPutOi),
        "- Top 3 Call Walls (Vol): " + fmtTopList(expSc.topCallVol),
        "- Top 3 Put Walls (Vol): " + fmtTopList(expSc.topPutVol),
        "- Anomalie (Vol > OI / Z-Score), " + exp.dte + "d:",
        expAnoms.length
          ? expAnoms.slice(0, 6).map(formatAnomalyLine).join("\n")
          : "  brak wykrytych anomalii dla tego wygaśnięcia.",
        "- Drabina strike'ów (Greki: " + greeksSourceSummary + "):",
        formatLadderLines(expSc).join("\n"),
      ];
      expiryBlocks.push(block.join("\n"));
    }

    var lines = [
      "ROLA: Jesteś praktycznym analitykiem rynku opcji (struktura terminowa, wielu wygaśnięć naraz).",
      "ZADANIE WEWNĘTRZNE (zrób przed napisaniem odpowiedzi):",
      "1) Przeanalizuj głęboko WSZYSTKIE poniższe wygaśnięcia razem: porównaj ATM IV między datami (kształt struktury terminowej: contango / backwardation), migrację ścian Put/Call między datami (czy ściany rosną, przesuwają się wyżej/niżej, czy się kumulują), rozkład Gamma i Theta w czasie, układ P/C OI i Volume oraz anomalie Vol > OI i Z-Score.",
      "2) Uwzględnij kontekst dnia dzisiejszego (" + todayIso + ", UTC: " + (meta.snapshot_ts_utc || "n/a") + ") i aktualnego otoczenia rynkowego/makro/sektorowego dla " + undName + " (jeśli nie masz dostępu do bieżących wiadomości lub daty wyników, zaznacz to w 1 zdaniu zamiast zgadywać).",
      "3) Szukaj zależności między wygaśnięciami: czy ta sama ściana powtarza się w kilku datach (silny poziom), czy anomalie koncentrują się w jednej dacie, gdzie Theta jest najdroższa, a gdzie Gamma najbardziej wrażliwa.",
      "",
      "ŚCISŁE ZASADY ODPOWIEDZI:",
      "- JĘZYK: wyłącznie PROSTY, CODZIENNY POLSKI (zero ciężkiego żargonu; każde pojęcie typu Call Wall/Put Wall/IV/Delta/Gamma/Theta/struktura terminowa wyjaśnij od razu w prostych, ludzkich słowach).",
      "- FORMAT: BEZ ŚCIANY TEKSTU. Tylko krótkie wypunktowania dokładnie według 5-punktowego szablonu poniżej; w każdej sekcji porównuj wygaśnięcia (np. 'Expiry 1 vs Expiry 3'), nie opisuj każdego osobno od zera.",
      "- OBOWIĄZKOWO WYJAŚNIJ GREKI (SEKCJA 3): dokładnie 3 krótkie punkty (Delta, Gamma, Theta; max 2 zdania na każdy) z odniesieniem do różnic między wygaśnięciami.",
      "- LIMIT DŁUGOŚCI: MAKSYMALNIE 450-600 słów łącznie. Dokładnie, konkretnie, zero ogólników i powtórzeń danych.",
      "",
      "=== SEKCJA 1: KONTEKST OGÓLNY ===",
      "- Ticker: " + undName + " | Cena Spot: $" + promptNum(spot, 2),
      "- Data rynkowa: " + todayIso + " | Timestamp UTC: " + (meta.snapshot_ts_utc || "n/a") + " | Snapshot ID: " + (meta.snapshot_id || "n/a"),
      "- Tryb danych IBKR: " + mdLabel + " | Źródło Greeks/IV: " + greeksLabel + " | Śr. ATM IV (wszystkie daty): " + avgAtmIvLabel,
      "- Śr. Greeks ATM (" + greeksSourceSummary + "): " + atmGreeksSummary,
      "- Zakres pobrania: Range " + rangeLabel + " | Step $ " + stepLabel + " | Baza ścian: " + effectiveBasis,
      "- Zakres DTE: " + scopeTitle,
      "- Łącznie: Call OI " + promptInt(scoped.totalCallOi) + " | Put OI " + promptInt(scoped.totalPutOi) + " | P/C (OI) " + pcOiRatio + " | Call Vol " + promptInt(scoped.totalCallVol) + " | Put Vol " + promptInt(scoped.totalPutVol) + " | P/C (Vol) " + pcVolRatio + " | Anomalie: " + totalAnomalyCount,
      "",
      "=== SEKCJA 2: STRUKTURA TERMINOWA - PORÓWNANIE WYGAŚNIĘĆ (Expiry 1-" + activeExpiries.length + ") ===",
      dteSummaryLines.join("\n"),
      "",
      "=== SEKCJA 3: SZCZEGÓŁY WG WYGAŚNIĘCIA (ściany, Greki, anomalie, drabina) ===",
      expiryBlocks.join("\n\n"),
      "",
      "=== WYMAGANA STRUKTURA ODPOWIEDZI (SKOPIUJ DOKŁADNIE TE 5 NAGŁÓWKÓW, BEZ DŁUGICH AKAPITÓW) ===",
      "### 1. Kontekst i Struktura Terminowa",
      "- **Obecna pozycja ($" + promptNum(spot, 2) + "):** 1 krótkie zdanie - gdzie stoi kurs względem ścian z różnych dat i jaki widać klimat (spokój / presja wzrostowa / defensywa).",
      "- **Struktura terminowa IV:** 1-2 punkty - ATM IV rośnie czy maleje z datą (Expiry 1 vs ostatnie), co to mówi o oczekiwaniach rynku (np. strach krótkoterminowy vs spokój dalej).",
      "- **Kontekst dnia (" + todayIso + ") i świata:** 1-2 krótkie punkty o sytuacji rynkowej/sektorowej dla " + undName + " (lub wzmianka, jeśli brak live kalendarza, np. daty wyników).",
      "- **Jakość danych:** 1 punkt - źródło Greków (" + greeksSourceSummary + "), baza ścian (" + effectiveBasis + ", " + mdLabel + "), czy " + stepLabel + " i " + rangeLabel + " dają pełny obraz.",
      "",
      "### 2. Porównanie Walli (Expirations 1-" + activeExpiries.length + ")",
      "- **Call Wall (opór) w czasie:** główne poziomy $ w kolejnych datach i dystans %; czy ściana migruje w górę/dół, czy powtarza się (silny opór).",
      "- **Put Wall (wsparcie) w czasie:** analogicznie dla wsparcia; gdzie leży najmocniejsze wsparcie wspólne dla kilku dat.",
      "- **Przewaga stron (P/C):** 1 wniosek porównujący P/C OI i Volume między datami (która data jest najbardziej defensywna / agresywna).",
      "",
      "### 3. Analiza Greków (Delta, Gamma, Theta)",
      "- **Delta (wpływ ruchu kursu):** max 2 zdania - jak mocno reagują opcje przy ścianach i czy różni się to między bliską a dalszą datą.",
      "- **Gamma (przyspieszenie):** max 2 zdania - w której dacie Gamma jest największa przy kursie i co to znaczy dla gwałtownych ruchów.",
      "- **Theta (ucieczka czasu):** max 2 zdania - gdzie czas ucieka najszybciej i kto zyskuje (kupujący vs sprzedający opcje).",
      "",
      "### 4. Wykryte Anomalie",
      "- Maksymalnie 3 punkty o najsilniejszych sygnałach (data, C/P, strike $, Vol vs OI, Delta/Gamma/Theta - ostrożny zakład przy kursie czy tani los na loterię) i prosty wniosek, czy anomalie układają się w jedną spójną tezę między datami (albo wprost: brak istotnych anomalii).",
      "",
      "### 5. Top 3 strategie opcyjne pod ten układ",
      "Wybierz 3 najlepiej pasujące do struktury terminowej, ścian i IV (możesz użyć kalendarza/diagonali łączących różne daty). Dla każdej napisz TYLKO te podpunkty:",
      "1. **[Nazwa strategii - konkretne strike'i $ i daty YYYY-MM-DD z danych]**",
      "   - *Dlaczego pasuje:* 1 krótkie zdanie (jak wykorzystuje ściany / IV / różnicę między datami).",
      "   - *Scenariusz i ryzyko:* kiedy zarabia vs konkretny poziom $, gdzie teza się psuje.",
      "2. **[Nazwa strategii - konkretne strike'i $ i daty YYYY-MM-DD z danych]**",
      "   - *Dlaczego pasuje:* 1 krótkie zdanie.",
      "   - *Scenariusz i ryzyko:* kiedy zarabia vs konkretny poziom $, gdzie teza się psuje.",
      "3. **[Nazwa strategii - konkretne strike'i $ i daty YYYY-MM-DD z danych]**",
      "   - *Dlaczego pasuje:* 1 krótkie zdanie.",
      "   - *Scenariusz i ryzyko:* kiedy zarabia vs konkretny poziom $, gdzie teza się psuje.",
    ];

    return lines.join("\n");
  }

  function copyTextToClipboard(text) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      return navigator.clipboard.writeText(text).catch(function () {
        return fallbackCopyText(text);
      });
    }
    return fallbackCopyText(text);
  }

  function fallbackCopyText(text) {
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.top = "-9999px";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        ta.setSelectionRange(0, text.length);
        var ok = document.execCommand("copy");
        document.body.removeChild(ta);
        if (ok) resolve();
        else reject(new Error("Clipboard copy failed"));
      } catch (err) {
        reject(err);
      }
    });
  }

  function copyWallsAiPrompt() {
    if (!state.chainData || !state.chainData.profiles) {
      showBanner("warn", "Load an option chain snapshot before copying the AI analysis prompt.");
      return;
    }
    var promptText = buildWallsAiPrompt(state.chainData);
    if (!promptText) {
      showBanner("warn", "No option strikes available in the active DTE scope.");
      return;
    }
    var dteScope = "all weekly expirations (30\u2013120 DTE)";
    copyTextToClipboard(promptText)
      .then(function () {
        if (dom.wallsCopyAiBtn) {
          dom.wallsCopyAiBtn.classList.add("is-copied");
          window.setTimeout(function () {
            if (dom.wallsCopyAiBtn) dom.wallsCopyAiBtn.classList.remove("is-copied");
          }, 1800);
        }
        showBanner(
          "success",
          "Copied Polish AI Put/Call Wall prompt to clipboard (" + dteScope + ")."
        );
      })
      .catch(function (err) {
        showBanner("error", "Could not copy prompt to clipboard: " + (err && err.message ? err.message : err));
      });
  }

  function restoreWallsPreferences() {
    var savedView = readPref("ibkr_walls_view", "ladder");
    if (dom.wallsViewSelect && (savedView === "ladder" || savedView === "grouped")) {
      dom.wallsViewSelect.value = savedView;
    }
    var savedMetric = readPref("ibkr_walls_metric", "auto");
    if (
      dom.wallsMetricSelect &&
      (savedMetric === "auto" ||
        savedMetric === "oi" ||
        savedMetric === "volume" ||
        savedMetric === "overlay")
    ) {
      dom.wallsMetricSelect.value = savedMetric;
    }
    var savedSplit = readPref("ibkr_walls_split_dte", "false");
    if (dom.wallsSplitDte) {
      dom.wallsSplitDte.checked = savedSplit === "true";
    }
    var savedAnoms = readPref("ibkr_walls_show_anomalies", "true");
    if (dom.wallsShowAnomalies) {
      dom.wallsShowAnomalies.checked = savedAnoms !== "false";
    }
    syncWallsSplitCheckboxState();
  }

  var SNAP_COLS = [4, 6, 8, 12];
  var SNAP_HEIGHTS = [290, 350, 410, 470, 530, 590, 650, 720, 800, 880, 960, 1080];
  var GRID_LAYOUT_KEY = "ibkr_chart_grid_layout_v1";

  function readGridLayoutPrefs() {
    try {
      var raw = window.localStorage.getItem(GRID_LAYOUT_KEY);
      if (!raw) return {};
      var parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_err) {
      return {};
    }
  }

  function writeGridLayoutPrefs(layoutMap) {
    try {
      window.localStorage.setItem(GRID_LAYOUT_KEY, JSON.stringify(layoutMap));
    } catch (_err) {
      /* ignore */
    }
  }

  function nearestSnapValue(value, candidates) {
    var best = candidates[0];
    var bestDist = Math.abs(value - best);
    for (var i = 1; i < candidates.length; i++) {
      var d = Math.abs(value - candidates[i]);
      if (d < bestDist) {
        best = candidates[i];
        bestDist = d;
      }
    }
    return best;
  }

  function applyCardSpanClass(card, span) {
    SNAP_COLS.forEach(function (col) {
      card.classList.remove("panel-card--span-" + col);
    });
    if (span) {
      card.classList.add("panel-card--span-" + span);
    }
  }

  function computeTargetSnap(gridEl, measuredWidth, measuredHeight) {
    var gridWidth = gridEl ? gridEl.getBoundingClientRect().width : 0;
    var gapPx = 16;
    if (gridEl) {
      var parsedGap = parseFloat(window.getComputedStyle(gridEl).columnGap);
      if (!isNaN(parsedGap)) gapPx = parsedGap;
    }
    var trackWidth = gridWidth > 0 ? Math.max(20, (gridWidth - gapPx * 11) / 12) : 80;
    var bestSpan = 6;
    var bestSpanDist = Infinity;
    for (var i = 0; i < SNAP_COLS.length; i++) {
      var cols = SNAP_COLS[i];
      var targetW = cols * trackWidth + (cols - 1) * gapPx;
      var dist = Math.abs(measuredWidth - targetW);
      if (dist < bestSpanDist) {
        bestSpanDist = dist;
        bestSpan = cols;
      }
    }
    var bestHeight = nearestSnapValue(measuredHeight, SNAP_HEIGHTS);
    return { span: bestSpan, height: bestHeight };
  }

  var rafByCanvasId = {};
  function schedulePlotlyResize(canvasEl) {
    if (!canvasEl || typeof window.Plotly === "undefined" || !canvasEl.data) return;
    var cardEl = canvasEl.closest ? canvasEl.closest(".panel-card") : null;
    if (cardEl && cardEl.classList.contains("is-hidden")) return;
    var cid = canvasEl.id || "chart";
    if (rafByCanvasId[cid]) {
      window.cancelAnimationFrame(rafByCanvasId[cid]);
    }
    rafByCanvasId[cid] = window.requestAnimationFrame(function () {
      var cardNow = canvasEl.closest ? canvasEl.closest(".panel-card") : null;
      if (cardNow && cardNow.classList.contains("is-hidden")) return;
      var rect = canvasEl.getBoundingClientRect ? canvasEl.getBoundingClientRect() : null;
      if (rect && (rect.width <= 0 || rect.height <= 0)) return;
      if (window.Plotly && window.Plotly.Plots && canvasEl.data) {
        window.Plotly.Plots.resize(canvasEl);
      }
    });
  }

  function initChartResizeHandling() {
    var gridEl = document.querySelector(".chart-grid");
    var cards = document.querySelectorAll(".chart-grid .panel-card");
    if (!cards.length) return;

    function cardKey(card, idx) {
      if (card.id) return card.id;
      var canvas = card.querySelector(".chart-canvas");
      return canvas && canvas.id ? canvas.id : "panel-" + idx;
    }

    var savedLayout = readGridLayoutPrefs();
    var activeDrag = null;

    function cardSpanValue(card) {
      for (var s = 0; s < SNAP_COLS.length; s++) {
        if (card.classList.contains("panel-card--span-" + SNAP_COLS[s])) {
          return SNAP_COLS[s];
        }
      }
      return 6;
    }

    function updateCardSpanButton(card) {
      var btn = card.querySelector(".panel-span-btn");
      if (btn) {
        btn.textContent = cardSpanValue(card) + "/12";
      }
    }

    function snapCardToGrid(card, key, measuredWidth, measuredHeight) {
      var snap = computeTargetSnap(gridEl, measuredWidth, measuredHeight);
      card.style.width = "";
      card.style.maxWidth = "";
      card.style.height = snap.height + "px";
      applyCardSpanClass(card, snap.span);
      updateCardSpanButton(card);
      card.classList.remove("is-snapping-drag");
      card.removeAttribute("data-snap-hint");

      savedLayout[key] = { span: snap.span, height: snap.height };
      writeGridLayoutPrefs(savedLayout);

      var canvasEl = card.querySelector(".chart-canvas");
      if (canvasEl) {
        schedulePlotlyResize(canvasEl);
        window.setTimeout(function () {
          schedulePlotlyResize(canvasEl);
        }, 60);
      }
    }

    var observer =
      typeof window.ResizeObserver !== "undefined"
        ? new window.ResizeObserver(function (entries) {
            for (var i = 0; i < entries.length; i++) {
              var canvas = entries[i].target.querySelector(".chart-canvas");
              if (canvas) schedulePlotlyResize(canvas);
            }
          })
        : null;

    for (var i = 0; i < cards.length; i++) {
      (function (card, idx) {
        var key = cardKey(card, idx);
        var defaultSpan = "";
        for (var s = 0; s < SNAP_COLS.length; s++) {
          if (card.classList.contains("panel-card--span-" + SNAP_COLS[s])) {
            defaultSpan = String(SNAP_COLS[s]);
            break;
          }
        }
        card.dataset.defaultSpan = defaultSpan;

        var pref = savedLayout[key];
        if (pref && typeof pref === "object") {
          if (pref.span && SNAP_COLS.indexOf(Number(pref.span)) !== -1) {
            applyCardSpanClass(card, Number(pref.span));
          }
          if (pref.height && !isNaN(Number(pref.height))) {
            var restoredH = Number(pref.height);
            var minGuard =
              key === "strategy-lab-card"
                ? 530
                : key === "wall-corridor-card"
                  ? 460
                  : 0;
            if (restoredH >= minGuard) {
              card.style.height = restoredH + "px";
            }
          }
        }

        if (observer) {
          observer.observe(card);
        }

        var canvasEl = card.querySelector(".chart-canvas");
        var titleEl = card.querySelector(".panel-title");

        // Clickable grid width cycle badge ([6/12] -> 8/12 -> 12/12 -> 4/12 -> 6/12)
        if (titleEl && !titleEl.querySelector(".panel-span-btn")) {
          var spanBtn = document.createElement("button");
          spanBtn.type = "button";
          spanBtn.className = "panel-span-btn";
          spanBtn.title = "Click to cycle panel width (4/6/8/12 columns)";
          spanBtn.setAttribute("aria-label", "Cycle panel grid width");
          spanBtn.addEventListener("dblclick", function (ev) {
            ev.stopPropagation();
          });
          spanBtn.addEventListener("click", function (ev) {
            ev.stopPropagation();
            var curSpan = cardSpanValue(card);
            var cycle = [6, 8, 12, 4];
            var nextSpan = cycle[(cycle.indexOf(curSpan) + 1) % cycle.length];
            var curHeight = Math.round(card.getBoundingClientRect().height) || 410;
            card.style.width = "";
            card.style.maxWidth = "";
            if (key === "strategy-lab-card") {
              card.style.height = "";
              savedLayout[key] = { span: nextSpan };
            } else {
              card.style.height = curHeight + "px";
              savedLayout[key] = { span: nextSpan, height: curHeight };
            }
            applyCardSpanClass(card, nextSpan);
            updateCardSpanButton(card);
            writeGridLayoutPrefs(savedLayout);
            if (canvasEl) {
              schedulePlotlyResize(canvasEl);
              window.setTimeout(function () {
                schedulePlotlyResize(canvasEl);
              }, 60);
            }
          });
          titleEl.appendChild(spanBtn);
          updateCardSpanButton(card);
        }

        // Dedicated PointerEvents corner handle (never blocked by CSS Grid max-width or Plotly)
        var handle = card.querySelector(".panel-resize-handle");
        if (!handle) {
          handle = document.createElement("button");
          handle.type = "button";
          handle.className = "panel-resize-handle";
          handle.setAttribute("aria-label", "Drag corner to resize panel");
          handle.title = "Drag corner to resize and snap to 12-column grid";
          card.appendChild(handle);
        }

        var dragState = null;

        handle.addEventListener("pointerdown", function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          var startRect = card.getBoundingClientRect();
          var gridRect = gridEl ? gridEl.getBoundingClientRect() : startRect;
          dragState = {
            pointerId: ev.pointerId,
            startX: ev.clientX,
            startY: ev.clientY,
            startW: startRect.width,
            startH: startRect.height,
            maxW: Math.max(startRect.width, gridRect.width),
            mobileOnlyY: window.innerWidth <= 1120,
          };
          card.classList.add("is-snapping-drag");
          card.style.maxWidth = "none";
          if (!dragState.mobileOnlyY) {
            card.style.width = Math.round(startRect.width) + "px";
          }
          card.style.height = Math.round(startRect.height) + "px";
          var initSnap = computeTargetSnap(gridEl, startRect.width, startRect.height);
          card.setAttribute(
            "data-snap-hint",
            "Snap " + initSnap.span + "/12 col • " + initSnap.height + "px"
          );
          if (handle.setPointerCapture) {
            try {
              handle.setPointerCapture(ev.pointerId);
            } catch (_err) {
              /* ignore synthetic pointer events */
            }
          }
        });

        handle.addEventListener("pointermove", function (ev) {
          if (!dragState || ev.pointerId !== dragState.pointerId) return;
          var dx = ev.clientX - dragState.startX;
          var dy = ev.clientY - dragState.startY;
          var nextW = dragState.startW;
          if (!dragState.mobileOnlyY) {
            nextW = Math.max(260, Math.min(dragState.maxW, dragState.startW + dx));
            card.style.width = Math.round(nextW) + "px";
          }
          var nextH = Math.max(290, Math.min(1200, dragState.startH + dy));
          card.style.height = Math.round(nextH) + "px";
          var preview = computeTargetSnap(gridEl, nextW, nextH);
          card.setAttribute(
            "data-snap-hint",
            "Snap " + preview.span + "/12 col • " + preview.height + "px"
          );
          if (canvasEl) schedulePlotlyResize(canvasEl);
        });

        function endHandleDrag(ev) {
          if (!dragState) return;
          if (ev && ev.pointerId !== undefined && ev.pointerId !== dragState.pointerId) return;
          var st = dragState;
          dragState = null;
          if (handle.releasePointerCapture && ev && ev.pointerId !== undefined) {
            try {
              if (!handle.hasPointerCapture || handle.hasPointerCapture(ev.pointerId)) {
                handle.releasePointerCapture(ev.pointerId);
              }
            } catch (_err) {
              /* ignore */
            }
          }
          var measuredW = st.mobileOnlyY
            ? card.getBoundingClientRect().width
            : parseFloat(card.style.width) || card.getBoundingClientRect().width;
          var measuredH = parseFloat(card.style.height) || card.getBoundingClientRect().height;
          if (st.mobileOnlyY) {
            var heightSnap = nearestSnapValue(measuredH, SNAP_HEIGHTS);
            card.style.width = "";
            card.style.maxWidth = "";
            card.style.height = heightSnap + "px";
            card.classList.remove("is-snapping-drag");
            card.removeAttribute("data-snap-hint");
            savedLayout[key] = { span: cardSpanValue(card), height: heightSnap };
            writeGridLayoutPrefs(savedLayout);
            if (canvasEl) schedulePlotlyResize(canvasEl);
          } else {
            snapCardToGrid(card, key, measuredW, measuredH);
          }
        }

        handle.addEventListener("pointerup", endHandleDrag);
        handle.addEventListener("pointercancel", endHandleDrag);

        if (titleEl) {
          titleEl.title =
            "Drag bottom-right corner to snap to 12-col grid; click [6/12] to resize width; double-click title to reset";
          titleEl.addEventListener("dblclick", function () {
            card.style.width = "";
            card.style.maxWidth = "";
            card.style.height = "";
            card.classList.remove("is-snapping-drag");
            card.removeAttribute("data-snap-hint");
            applyCardSpanClass(
              card,
              card.dataset.defaultSpan ? Number(card.dataset.defaultSpan) : null
            );
            updateCardSpanButton(card);
            delete savedLayout[key];
            writeGridLayoutPrefs(savedLayout);
            if (canvasEl) {
              schedulePlotlyResize(canvasEl);
              window.setTimeout(function () {
                schedulePlotlyResize(canvasEl);
              }, 60);
            }
          });
        }
      })(cards[i], i);
    }
  }

  // ------------------------------------------------------------------ Init
  function init() {
    updateThemeToggleLabel();
    restoreWallsPreferences();
    syncStrategyAutoPopulateUI();
    applyTickerPullPrefsToControls(state.ticker);
    state.tableSort = readTableSortPref();
    syncTableHeaderSortUI();
    applyPanelVisibility(readPanelVisibility(), { skipRender: true });
    bindEvents();
    initChartResizeHandling();
    fetchStatus();
    loadChain(state.ticker, "");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
