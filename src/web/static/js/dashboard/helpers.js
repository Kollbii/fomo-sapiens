/* IBKR Options Analyzer dashboard — Formatting, parsing, DOM and API-error helpers shared by every panel. */

import {
  PULL_PREFS_KEY,
  STRATEGY_AUTO_POPULATE_KEY,
  bootstrap,
  dom,
  state,
} from "./state.js";
import {
  matchesDteFilterValue,
} from "./charts.js";

// ------------------------------------------------------------------ Helpers
export function extractApiErrorMessage(body, fallback) {
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

export function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "terminal"
    ? "terminal"
    : "print";
}

export function themeVars() {
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

export function readPref(key, fallback) {
  try {
    var val = window.localStorage.getItem(key);
    return val !== null ? val : fallback;
  } catch (_err) {
    return fallback;
  }
}

export function writePref(key, value) {
  try {
    window.localStorage.setItem(key, String(value));
  } catch (_err) {
    /* ignore */
  }
}

export function isStrategyAutoPopulateEnabled() {
  if (dom.strategyAutoPopulate) {
    return Boolean(dom.strategyAutoPopulate.checked);
  }
  return readPref(STRATEGY_AUTO_POPULATE_KEY, "true") !== "false";
}

export function syncStrategyAutoPopulateUI() {
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

export function writeTickerPullPrefs(ticker) {
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

export function applyTickerPullPrefsToControls(ticker) {
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

export function snapDteValueToExpiries(previousVal, expiries, options) {
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

export function buildActivePullScopeText() {
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

export function formatExpiryIso(rawExpiry) {
  var s = String(rawExpiry || "").trim();
  if (/^\d{8}$/.test(s)) {
    return s.slice(0, 4) + "-" + s.slice(4, 6) + "-" + s.slice(6, 8);
  }
  return s;
}

export function formatStrikeShort(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "—";
  }
  var n = Number(val);
  if (Math.abs(n - Math.round(n)) < 1e-6) {
    return "$" + Math.round(n).toString();
  }
  return "$" + n.toFixed(2);
}

export function fmtNum(val, decimals) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "—";
  }
  return Number(val).toFixed(decimals);
}

export function fmtInt(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "—";
  }
  return Math.round(Number(val)).toLocaleString("en-US");
}

export function fmtMoney(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "—";
  }
  return "$" + Number(val).toFixed(2);
}

export function formatStepShort(stepVal) {
  if (stepVal === null || stepVal === undefined || Number.isNaN(Number(stepVal))) {
    return "—";
  }
  var n = Number(stepVal);
  return Math.abs(n - Math.round(n)) < 1e-6 ? "$" + Math.round(n) : "$" + n;
}

export function formatRangeShort(rangeVal) {
  if (rangeVal === null || rangeVal === undefined || Number.isNaN(Number(rangeVal))) {
    return "—";
  }
  return "\u00B1" + Math.round(Number(rangeVal) * 100) + "%";
}

export function formatPullScopeSummary(meta) {
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

export function resetControlCapabilities() {
  resetSelectCapabilities(dom.rangeSelect);
  resetSelectCapabilities(dom.stepSelect);
  if (dom.stepSelect && !dom.stepSelect.value) {
    dom.stepSelect.value = "auto";
  }
}

export function syncControlCapabilities(caps) {
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

export function syncControlsFromMeta(meta, options) {
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

export function escapeHtml(raw) {
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

export function clearAlertBanner() {
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

export function showBanner(kind, message, meta) {
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

export function setActiveTickerUI(ticker) {
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

export function updateExportLink() {
  if (!dom.exportCsvBtn) return;
  var url = "/api/csv/" + encodeURIComponent(state.ticker);
  if (state.snapshotId) {
    url += "?snapshot_id=" + encodeURIComponent(state.snapshotId);
  }
  dom.exportCsvBtn.setAttribute("href", url);
}

export function configuredDefaultMdType() {
  if (
    state.status &&
    state.status.default_market_data_type !== null &&
    state.status.default_market_data_type !== undefined
  ) {
    return Number(state.status.default_market_data_type);
  }
  if (
    bootstrap.defaultMarketDataType !== null &&
    bootstrap.defaultMarketDataType !== undefined
  ) {
    return Number(bootstrap.defaultMarketDataType);
  }
  return 3;
}

export function configuredFallbackMdType() {
  if (
    state.status &&
    state.status.fallback_market_data_type !== null &&
    state.status.fallback_market_data_type !== undefined
  ) {
    return Number(state.status.fallback_market_data_type);
  }
  if (
    bootstrap.fallbackMarketDataType !== null &&
    bootstrap.fallbackMarketDataType !== undefined
  ) {
    return Number(bootstrap.fallbackMarketDataType);
  }
  return configuredDefaultMdType() <= 2 ? 2 : 4;
}

export function configuredMarketDataMode() {
  if (state.status && state.status.market_data_mode) {
    return String(state.status.market_data_mode).toUpperCase();
  }
  if (bootstrap.marketDataMode) {
    return String(bootstrap.marketDataMode).toUpperCase();
  }
  return configuredDefaultMdType() <= 2 ? "LIVE" : "DELAYED";
}

export function activeStatusMdType() {
  if (
    state.status &&
    state.status.active_market_data_type !== null &&
    state.status.active_market_data_type !== undefined
  ) {
    return Number(state.status.active_market_data_type);
  }
  return configuredDefaultMdType();
}

export function configuredFeedBannerSummary() {
  var modeLbl = configuredMarketDataMode() === "LIVE" ? "Live" : "Delayed";
  var defT = configuredDefaultMdType();
  var fbT = configuredFallbackMdType();
  if (defT !== fbT) {
    return modeLbl + " Type " + defT + "/" + fbT + " Feed";
  }
  return modeLbl + " Type " + defT + " Feed";
}

export function configuredDteWindowLabel() {
  var lo =
    state.status && state.status.dte_min !== null && state.status.dte_min !== undefined
      ? Number(state.status.dte_min)
      : bootstrap.dteMin !== null && bootstrap.dteMin !== undefined
        ? Number(bootstrap.dteMin)
        : 30;
  var hi =
    state.status && state.status.dte_max !== null && state.status.dte_max !== undefined
      ? Number(state.status.dte_max)
      : bootstrap.dteMax !== null && bootstrap.dteMax !== undefined
        ? Number(bootstrap.dteMax)
        : 120;
  return lo + "\u2013" + hi;
}

export function gatewayAddressLabel() {
  var host = (state.status && state.status.host) || bootstrap.ibkrHost || "127.0.0.1";
  var port = (state.status && state.status.port) || bootstrap.ibkrPort || 4001;
  return host + ":" + port;
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
  var fallbackCode =
    mdType !== null && mdType !== undefined && mdType !== ""
      ? mdType
      : configuredDefaultMdType();
  return { label: "TYPE " + fallbackCode, cls: "badge badge--info" };
}

export function applyMdTypeBadge(mdType) {
  if (!dom.mdTypeBadge) return;
  var info = marketDataBadgeInfo(mdType);
  dom.mdTypeBadge.className = info.cls;
  dom.mdTypeBadge.textContent = info.label;
}

export function updateThemeToggleLabel() {
  if (!dom.themeToggle) return;
  dom.themeToggle.textContent =
    currentTheme() === "terminal" ? "PRINT EDITION" : "TERMINAL THEME";
}
