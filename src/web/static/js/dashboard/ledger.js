/* IBKR Options Analyzer dashboard — Hybrid Options Trade Ledger, Roll Chains & Open MTM. */

import {
  LEDGER_PREFS_KEY,
  dom,
  state,
} from "./state.js";
import {
  escapeHtml,
  extractApiErrorMessage,
  fmtMoney,
  fmtNum,
  showBanner,
  themeVars,
} from "./helpers.js";
import {
  baseLayout,
} from "./charts.js";
import {
  isPanelVisible,
} from "./panels.js";
import {
  apiJson,
  fetchStatus,
} from "./network.js";
import {
  schedulePlotlyResize,
} from "./grid_layout.js";

var VALID_WINDOWS = ["1W", "1M", "3M", "YTD", "1Y", "ALL"];
var VALID_VIEWS = ["realized", "open_mtm"];
var VALID_GROUPS = ["campaigns", "strategy", "ticker", "executions"];
var VALID_SYNC_MODES = ["auto", "monthly", "full"];

var STRATEGY_LABELS = {
  BULL_PUT_SPREAD: "BULL PUT SPREAD",
  BEAR_CALL_SPREAD: "BEAR CALL SPREAD",
  BULL_CALL_SPREAD: "BULL CALL SPREAD",
  BEAR_PUT_SPREAD: "BEAR PUT SPREAD",
  IRON_CONDOR: "IRON CONDOR",
  IRON_BUTTERFLY: "IRON BUTTERFLY",
  DIAGONAL_SPREAD: "DIAGONAL SPREAD",
  CALENDAR_SPREAD: "CALENDAR SPREAD",
  STRANGLE: "STRANGLE",
  STRADDLE: "STRADDLE",
  COVERED_CALL: "COVERED CALL",
  NAKED_CALL: "NAKED CALL",
  SHORT_PUT: "SHORT PUT",
  LONG_CALL: "LONG CALL",
  LONG_PUT: "LONG PUT",
  CUSTOM_COMBO: "CUSTOM COMBO",
  STOCK_LOT: "STOCK LOT",
};

// ------------------------------------------------------------------ Formatting & Prefs
function formatStrategyLabel(rawKind) {
  var key = String(rawKind || "CUSTOM_COMBO").toUpperCase();
  return STRATEGY_LABELS[key] || key.replace(/_/g, " ");
}

function fmtSignedMoney(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "\u2014";
  }
  var n = Number(val);
  var absStr = "$" + Math.abs(n).toFixed(2);
  if (n > 1e-6) return "+" + absStr;
  if (n < -1e-6) return "-" + absStr;
  return "$0.00";
}

function fmtSignedQty(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "\u2014";
  }
  var n = Number(val);
  var numStr = Math.abs(n - Math.round(n)) < 1e-6 ? String(Math.round(n)) : n.toFixed(2);
  return n > 0 ? "+" + numStr : numStr;
}

function pnlToneClass(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "";
  var n = Number(val);
  if (n > 1e-6) return "ledger-num-pos";
  if (n < -1e-6) return "ledger-num-neg";
  return "";
}

function kpiToneClass(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "";
  var n = Number(val);
  if (n > 1e-6) return "is-positive";
  if (n < -1e-6) return "is-negative";
  return "";
}

function formatShortUtc(rawIso) {
  var s = String(rawIso || "").trim();
  if (!s) return "\u2014";
  return s.replace("T", " ").replace(/:00Z$/, "Z");
}

function formatCompactExpiry(rawExp) {
  var s = String(rawExp || "").trim();
  if (/^\d{8}$/.test(s)) {
    return s.slice(2, 4) + "-" + s.slice(4, 6) + "-" + s.slice(6, 8);
  }
  return s;
}

function formatStrikeBadge(strikeVal) {
  if (strikeVal === null || strikeVal === undefined || Number.isNaN(Number(strikeVal))) {
    return "";
  }
  var n = Number(strikeVal);
  return Math.abs(n - Math.round(n)) < 1e-6 ? "$" + Math.round(n) : "$" + n.toFixed(2);
}

export function restoreLedgerPreferences() {
  try {
    var raw = window.localStorage.getItem(LEDGER_PREFS_KEY);
    if (raw) {
      var parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object") {
        if (VALID_WINDOWS.indexOf(parsed.window) !== -1) state.ledgerWindow = parsed.window;
        if (VALID_VIEWS.indexOf(parsed.view) !== -1) state.ledgerView = parsed.view;
        if (VALID_GROUPS.indexOf(parsed.groupBy) !== -1) state.ledgerGroupBy = parsed.groupBy;
        if (typeof parsed.ticker === "string" && parsed.ticker.trim()) {
          state.ledgerTicker = parsed.ticker.trim().toUpperCase();
        }
        if (typeof parsed.includeStockGain === "boolean") {
          state.ledgerIncludeStockGain = parsed.includeStockGain;
        }
        if (VALID_SYNC_MODES.indexOf(parsed.syncMode) !== -1) {
          state.ledgerSyncMode = parsed.syncMode;
        }
      }
    }
  } catch (_err) {
    /* ignore storage restrictions */
  }
  syncLedgerControlStates(null);
}

export function saveLedgerPreferences() {
  try {
    window.localStorage.setItem(
      LEDGER_PREFS_KEY,
      JSON.stringify({
        window: state.ledgerWindow,
        view: state.ledgerView,
        groupBy: state.ledgerGroupBy,
        ticker: state.ledgerTicker,
        includeStockGain: state.ledgerIncludeStockGain,
        syncMode: state.ledgerSyncMode,
      })
    );
  } catch (_err) {
    /* ignore */
  }
}

// ------------------------------------------------------------------ Controls & Status
export function syncLedgerControlStates(report) {
  if (dom.ledgerWindowGroup) {
    var winBtns = dom.ledgerWindowGroup.querySelectorAll("[data-ledger-window]");
    for (var i = 0; i < winBtns.length; i++) {
      var wb = winBtns[i];
      var wMatch = wb.getAttribute("data-ledger-window") === state.ledgerWindow;
      wb.classList.toggle("active", wMatch);
      wb.setAttribute("aria-pressed", wMatch ? "true" : "false");
    }
  }

  if (dom.ledgerViewGroup) {
    var viewBtns = dom.ledgerViewGroup.querySelectorAll("[data-ledger-view]");
    for (var j = 0; j < viewBtns.length; j++) {
      var vb = viewBtns[j];
      var vMatch = vb.getAttribute("data-ledger-view") === state.ledgerView;
      vb.classList.toggle("active", vMatch);
      vb.setAttribute("aria-pressed", vMatch ? "true" : "false");
    }
  }

  var isOpenMtm = state.ledgerView === "open_mtm";
  if (dom.ledgerGroupGroup) {
    dom.ledgerGroupGroup.classList.toggle("is-disabled", isOpenMtm);
    dom.ledgerGroupGroup.setAttribute("aria-disabled", isOpenMtm ? "true" : "false");
    var grpBtns = dom.ledgerGroupGroup.querySelectorAll("[data-ledger-group]");
    for (var k = 0; k < grpBtns.length; k++) {
      var gb = grpBtns[k];
      var gMatch = gb.getAttribute("data-ledger-group") === state.ledgerGroupBy;
      gb.classList.toggle("active", gMatch);
      gb.setAttribute("aria-pressed", gMatch ? "true" : "false");
      gb.disabled = isOpenMtm;
    }
  }

  if (dom.ledgerStockGainToggle) {
    dom.ledgerStockGainToggle.checked = Boolean(state.ledgerIncludeStockGain);
  }

  if (dom.ledgerSyncMode) {
    dom.ledgerSyncMode.value = state.ledgerSyncMode;
  }

  if (dom.ledgerMtmBtn) {
    dom.ledgerMtmBtn.classList.toggle("btn-primary", isOpenMtm);
  }

  if (dom.ledgerTickerFilter) {
    var seen = { ALL: true };
    var tickers = [];
    var pushSym = function (sym) {
      var clean = String(sym || "").trim().toUpperCase();
      if (!clean || seen[clean]) return;
      seen[clean] = true;
      tickers.push(clean);
    };
    (state.watchlist || []).forEach(pushSym);
    if (report && Array.isArray(report.available_tickers)) {
      report.available_tickers.forEach(pushSym);
    }
    if (state.ledgerTicker && state.ledgerTicker !== "ALL") {
      pushSym(state.ledgerTicker);
    }
    tickers.sort();

    dom.ledgerTickerFilter.innerHTML = "";
    var allOpt = document.createElement("option");
    allOpt.value = "ALL";
    allOpt.textContent = "All Tickers";
    dom.ledgerTickerFilter.appendChild(allOpt);
    for (var t = 0; t < tickers.length; t++) {
      var opt = document.createElement("option");
      opt.value = tickers[t];
      opt.textContent = tickers[t];
      dom.ledgerTickerFilter.appendChild(opt);
    }
    dom.ledgerTickerFilter.value = seen[state.ledgerTicker] ? state.ledgerTicker : "ALL";
  }
}

function renderLedgerStatusBadge(status) {
  if (!dom.ledgerStatusBadge) return;
  if (!status) {
    dom.ledgerStatusBadge.textContent = "LEDGER IDLE \u2022 OFFLINE CACHE READY";
    return;
  }
  var flexLabel = status.flex_configured
    ? status.flex_monthly_configured
      ? "FLEX 365D/MONTHLY"
      : "FLEX 365D"
    : "CSV/XML READY";
  var gwLabel = status.gateway_sync_enabled ? "GATEWAY ON" : "GATEWAY OPT-OUT";
  var countsLabel =
    Number(status.execution_count || 0) +
    " EXECS \u2022 " +
    Number(status.open_position_count || 0) +
    " OPEN";
  var syncLabel = status.last_synced_at
    ? "SYNCED " +
      formatShortUtc(status.last_synced_at) +
      (status.last_sync_mode ? " (" + String(status.last_sync_mode).toUpperCase() + ")" : "")
    : "NO SYNC YET";
  dom.ledgerStatusBadge.textContent = [flexLabel, gwLabel, countsLabel, syncLabel].join(" \u2022 ");
}

function setLedgerBusy(busy, label) {
  state.ledgerBusy = Boolean(busy);
  if (dom.ledgerSyncBtn) {
    dom.ledgerSyncBtn.disabled = state.ledgerBusy;
    dom.ledgerSyncBtn.textContent = busy && label === "sync" ? "SYNCING..." : "Sync Flex / Gateway";
  }
  if (dom.ledgerImportBtn) {
    dom.ledgerImportBtn.disabled = state.ledgerBusy;
    dom.ledgerImportBtn.textContent = busy && label === "import" ? "IMPORTING..." : "Import XML / CSV";
  }
  if (dom.ledgerMtmBtn) {
    dom.ledgerMtmBtn.disabled = state.ledgerBusy;
    dom.ledgerMtmBtn.textContent = busy && label === "mtm" ? "REFRESHING..." : "Refresh Open MTM";
  }
}

// ------------------------------------------------------------------ KPI Strip
function buildKpiCardHtml(label, valueText, subText, cardModifier, valueModifier) {
  return (
    '<div class="ledger-kpi-card' +
    (cardModifier ? " " + cardModifier : "") +
    '">' +
    '<span class="ledger-kpi-label">' +
    escapeHtml(label) +
    "</span>" +
    '<strong class="ledger-kpi-value' +
    (valueModifier ? " " + valueModifier : "") +
    '">' +
    escapeHtml(valueText) +
    "</strong>" +
    '<span class="ledger-kpi-sub">' +
    escapeHtml(subText) +
    "</span>" +
    "</div>"
  );
}

function resolveOpenMarkSourceSummary(positions) {
  if (!positions || !positions.length) return "NONE";
  var hasLive = false;
  var hasFlex = false;
  for (var i = 0; i < positions.length; i++) {
    if (positions[i].mark_source === "LIVE_GATEWAY") hasLive = true;
    else if (positions[i].mark_source === "FLEX_MARK") hasFlex = true;
  }
  if (hasLive && hasFlex) return "LIVE_GATEWAY + FLEX_MARK";
  if (hasLive) return "LIVE_GATEWAY";
  if (hasFlex) return "FLEX_MARK";
  return "NONE";
}

function renderLedgerKpis(report) {
  if (!dom.ledgerKpiStrip || !report || !report.kpis) return;
  var kpis = report.kpis;

  if (report.view === "open_mtm") {
    var positions = report.open_positions || [];
    var optCount = 0;
    var stkCount = 0;
    var costBasis = 0;
    var openFees = 0;
    for (var i = 0; i < positions.length; i++) {
      var p = positions[i];
      if (p.asset_class === "STK") stkCount += 1;
      else optCount += 1;
      costBasis += Number(p.cost_basis_ex_fee || 0);
      openFees += Number(p.fees || 0);
    }
    var markSrc = resolveOpenMarkSourceSummary(positions);
    var mtmVal = kpis.unrealized_mtm_ex_fee;
    var mtmCardMod =
      mtmVal !== null && mtmVal !== undefined
        ? mtmVal > 1e-6
          ? "ledger-kpi-card--positive"
          : mtmVal < -1e-6
            ? "ledger-kpi-card--negative"
            : ""
        : "";
    var mtmSub =
      mtmVal !== null && mtmVal !== undefined
        ? "Ex-fee MTM \u2022 After Fees: " + fmtSignedMoney(mtmVal - openFees)
        : positions.length
          ? "Click Refresh Open MTM for live marks"
          : "No open positions";

    dom.ledgerKpiStrip.innerHTML =
      buildKpiCardHtml(
        "Total Unrealized MTM",
        fmtSignedMoney(mtmVal),
        mtmSub,
        mtmCardMod,
        kpiToneClass(mtmVal)
      ) +
      buildKpiCardHtml(
        "Open Positions",
        String(positions.length),
        optCount + " OPT / " + stkCount + " STK \u2022 " + kpis.open_count + " open groups",
        "",
        ""
      ) +
      buildKpiCardHtml(
        "Open Cost Basis",
        positions.length ? fmtSignedMoney(costBasis) : "\u2014",
        "Net cost basis (short credit < $0)",
        "",
        ""
      ) +
      buildKpiCardHtml(
        "Open Fees Paid",
        fmtMoney(openFees),
        "Mark Source: " + markSrc,
        "ledger-kpi-card--fees",
        ""
      );
    return;
  }

  var headlinePnl = Number(kpis.total_pnl_ex_fee || 0);
  var afterFees = headlinePnl - Number(kpis.fees || 0);
  var pnlSub = "After Fees: " + fmtSignedMoney(afterFees);
  if (report.include_stock_gain) {
    pnlSub +=
      " \u2022 Opt " +
      fmtSignedMoney(kpis.realized_pnl_ex_fee) +
      " / Stk " +
      fmtSignedMoney(kpis.stock_pnl_ex_fee);
  }
  var pnlCardMod =
    headlinePnl > 1e-6
      ? "ledger-kpi-card--positive"
      : headlinePnl < -1e-6
        ? "ledger-kpi-card--negative"
        : "";

  var premVal = Number(kpis.net_premium_ex_fee || 0);
  var premCardMod =
    premVal > 1e-6
      ? "ledger-kpi-card--positive"
      : premVal < -1e-6
        ? "ledger-kpi-card--negative"
        : "";

  var winRateText =
    kpis.win_rate_pct !== null && kpis.win_rate_pct !== undefined
      ? fmtNum(kpis.win_rate_pct, 1) + "%"
      : "\u2014";
  var activitySub =
    Number(kpis.closed_count || 0) +
    " closed \u2022 " +
    Number(kpis.open_count || 0) +
    " open \u2022 " +
    Number(kpis.roll_count || 0) +
    " rolls";

  dom.ledgerKpiStrip.innerHTML =
    buildKpiCardHtml(
      "Realized P&L (Ex-Fees)",
      fmtSignedMoney(headlinePnl),
      pnlSub,
      pnlCardMod,
      kpiToneClass(headlinePnl)
    ) +
    buildKpiCardHtml(
      "Total Fees Paid",
      fmtMoney(kpis.fees),
      "Sidecar fees (excluded from P&L above)",
      "ledger-kpi-card--fees",
      ""
    ) +
    buildKpiCardHtml(
      "Net Option Premium",
      fmtSignedMoney(premVal),
      "Credits minus closing/roll debits (" + report.window + ")",
      premCardMod,
      kpiToneClass(premVal)
    ) +
    buildKpiCardHtml(
      "Win Rate & Activity",
      winRateText,
      activitySub,
      "",
      ""
    );
}

// ------------------------------------------------------------------ Plotly Chart
var LEDGER_DAY_MS = 86400000;
var LEDGER_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function pad2(num) {
  return (num < 10 ? "0" : "") + String(num);
}

function parseIsoDateUtcMs(val) {
  if (typeof val === "number") {
    return isFinite(val) ? val : NaN;
  }
  if (!val || typeof val !== "string") return NaN;
  var m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/.exec(val.trim());
  if (!m) return NaN;
  var y = parseInt(m[1], 10);
  var mon = parseInt(m[2], 10);
  var day = parseInt(m[3], 10);
  var hh = m[4] ? parseInt(m[4], 10) : 0;
  var mm = m[5] ? parseInt(m[5], 10) : 0;
  var ss = m[6] ? parseInt(m[6], 10) : 0;
  return Date.UTC(y, mon - 1, day, hh, mm, ss);
}

function formatUtcMsIso(ms) {
  var dt = new Date(ms);
  return (
    dt.getUTCFullYear() +
    "-" +
    pad2(dt.getUTCMonth() + 1) +
    "-" +
    pad2(dt.getUTCDate())
  );
}

function formatLedgerDateTickLabel(ms, isMonthly, includeYear) {
  var dt = new Date(ms);
  var mon = LEDGER_MONTHS[dt.getUTCMonth()];
  var yrShort = "'" + String(dt.getUTCFullYear()).slice(-2);
  if (isMonthly) {
    return includeYear ? mon + " " + yrShort : mon;
  }
  var base = mon + " " + dt.getUTCDate();
  return includeYear ? base + " " + yrShort : base;
}

function measureLedgerChartWidth(el) {
  var w = el && el.clientWidth ? el.clientWidth : 0;
  if (!w && el && typeof el.getBoundingClientRect === "function") {
    var rect = el.getBoundingClientRect();
    w = rect ? rect.width : 0;
  }
  return w > 0 ? w : 440;
}

function generateDayStepTicks(viewMinMs, viewMaxMs, stepDays, alignMonday) {
  var cur = Math.ceil(viewMinMs / LEDGER_DAY_MS) * LEDGER_DAY_MS;
  if (alignMonday) {
    var dow = new Date(cur).getUTCDay();
    var delta = (8 - dow) % 7;
    cur += delta * LEDGER_DAY_MS;
  }
  var ticks = [];
  var stepMs = stepDays * LEDGER_DAY_MS;
  while (cur <= viewMaxMs) {
    ticks.push(cur);
    cur += stepMs;
  }
  return ticks;
}

function generateMonthStepTicks(viewMinMs, viewMaxMs, stepMonths) {
  var startDt = new Date(viewMinMs);
  var y = startDt.getUTCFullYear();
  var m = startDt.getUTCMonth();
  var guard = 0;
  var ticks = [];
  while (guard < 480) {
    guard += 1;
    var candidate = Date.UTC(y, m, 1);
    if (candidate > viewMaxMs) break;
    if (candidate >= viewMinMs && (stepMonths <= 1 || m % stepMonths === 0)) {
      ticks.push(candidate);
    }
    m += 1;
    if (m >= 12) {
      y += Math.floor(m / 12);
      m = m % 12;
    }
  }
  return ticks;
}

function ticksFitPixelSpacing(ticksMs, viewSpanMs, effectiveWidth, minGapPx, maxTicks) {
  if (!ticksMs || ticksMs.length < 2 || ticksMs.length > maxTicks || viewSpanMs <= 0) {
    return false;
  }
  for (var i = 1; i < ticksMs.length; i++) {
    var gapPx = ((ticksMs[i] - ticksMs[i - 1]) / viewSpanMs) * effectiveWidth;
    if (gapPx < minGapPx) return false;
  }
  return true;
}

/**
 * Compute cleanly spaced date x-axis ticks and marker density for the current chart width.
 * Dynamically squeezes or expands tick count as container width or zoom window changes.
 */
export function computeLedgerDateAxisSpec(dates, containerWidth, visibleRange) {
  if (!Array.isArray(dates) || !dates.length) return null;
  var ptsMs = [];
  for (var i = 0; i < dates.length; i++) {
    var ms = parseIsoDateUtcMs(dates[i]);
    if (isFinite(ms) && (!ptsMs.length || ms > ptsMs[ptsMs.length - 1])) {
      ptsMs.push(ms);
    }
  }
  if (!ptsMs.length) return null;

  var dataMinMs = ptsMs[0];
  var dataMaxMs = ptsMs[ptsMs.length - 1];
  var viewMinMs = dataMinMs;
  var viewMaxMs = dataMaxMs;
  if (Array.isArray(visibleRange) && visibleRange.length === 2) {
    var r0 = parseIsoDateUtcMs(visibleRange[0]);
    var r1 = parseIsoDateUtcMs(visibleRange[1]);
    if (isFinite(r0) && isFinite(r1) && r0 !== r1) {
      var rLow = Math.min(r0, r1);
      var rHigh = Math.max(r0, r1);
      if (rHigh >= dataMinMs && rLow <= dataMaxMs) {
        viewMinMs = Math.max(dataMinMs, rLow);
        viewMaxMs = Math.min(dataMaxMs, rHigh);
      }
    }
  }

  var safeWidth = isFinite(Number(containerWidth)) && Number(containerWidth) > 0 ? Number(containerWidth) : 440;
  var innerWidth = Math.max(160, safeWidth - 86);
  var effectiveWidth = innerWidth * 0.92;
  var minGapPx = innerWidth < 300 ? 62 : 68;
  var maxTicks = Math.max(2, Math.min(12, Math.floor(effectiveWidth / minGapPx) + 1));

  var visiblePts = [];
  for (var v = 0; v < ptsMs.length; v++) {
    if (ptsMs[v] >= viewMinMs && ptsMs[v] <= viewMaxMs) {
      visiblePts.push(ptsMs[v]);
    }
  }
  var visibleCount = Math.max(1, visiblePts.length);
  var pxPerPoint = visibleCount > 1 ? effectiveWidth / (visibleCount - 1) : effectiveWidth;
  var traceMode = "lines+markers";
  var primaryMarkerSize = 6;
  var secondaryMarkerSize = 5;
  if (visibleCount > 28 && pxPerPoint < 7.5) {
    traceMode = "lines";
    primaryMarkerSize = 3;
    secondaryMarkerSize = 3;
  } else if (visibleCount > 28 && pxPerPoint < 13) {
    traceMode = "lines+markers";
    primaryMarkerSize = 4;
    secondaryMarkerSize = 3.5;
  }

  var spanMs = Math.max(0, viewMaxMs - viewMinMs);
  if (spanMs <= 0) {
    var singleIso = formatUtcMsIso(viewMinMs);
    return {
      tickvals: [singleIso],
      ticktext: [formatLedgerDateTickLabel(viewMinMs, false, true)],
      range:
        ptsMs.length === 1
          ? [formatUtcMsIso(viewMinMs - LEDGER_DAY_MS), formatUtcMsIso(viewMinMs + LEDGER_DAY_MS)]
          : null,
      traceMode: "lines+markers",
      primaryMarkerSize: 6,
      secondaryMarkerSize: 5,
      pxPerPoint: effectiveWidth,
    };
  }

  var chosenTicks = null;
  var isMonthly = false;

  if (ticksFitPixelSpacing(visiblePts, spanMs, effectiveWidth, minGapPx, maxTicks)) {
    chosenTicks = visiblePts;
    isMonthly = false;
  }

  if (!chosenTicks) {
    var spanDays = spanMs / LEDGER_DAY_MS;
    if (spanDays <= 160) {
      var daySchedules = [
        [1, false],
        [2, false],
        [3, false],
        [5, false],
        [7, true],
        [10, false],
        [14, true],
        [21, true],
      ];
      for (var dIdx = 0; dIdx < daySchedules.length; dIdx++) {
        var dCand = generateDayStepTicks(viewMinMs, viewMaxMs, daySchedules[dIdx][0], daySchedules[dIdx][1]);
        if (ticksFitPixelSpacing(dCand, spanMs, effectiveWidth, minGapPx, maxTicks)) {
          chosenTicks = dCand;
          isMonthly = false;
          break;
        }
      }
    }
  }

  if (!chosenTicks) {
    var monthSteps = [1, 2, 3, 4, 6, 12, 24];
    for (var mIdx = 0; mIdx < monthSteps.length; mIdx++) {
      var mCand = generateMonthStepTicks(viewMinMs, viewMaxMs, monthSteps[mIdx]);
      if (ticksFitPixelSpacing(mCand, spanMs, effectiveWidth, minGapPx, maxTicks)) {
        chosenTicks = mCand;
        isMonthly = true;
        break;
      }
    }
  }

  if (!chosenTicks) {
    var fallbackTicks = [];
    for (var f = 0; f < maxTicks; f++) {
      var rawMs = viewMinMs + (spanMs * f) / (maxTicks - 1);
      var snappedMs = Math.round(rawMs / LEDGER_DAY_MS) * LEDGER_DAY_MS;
      if (!fallbackTicks.length || snappedMs > fallbackTicks[fallbackTicks.length - 1]) {
        fallbackTicks.push(snappedMs);
      }
    }
    chosenTicks = ticksFitPixelSpacing(fallbackTicks, spanMs, effectiveWidth, minGapPx, maxTicks)
      ? fallbackTicks
      : [viewMinMs, viewMaxMs];
    isMonthly = false;
  }

  var multiYear = new Date(viewMinMs).getUTCFullYear() !== new Date(viewMaxMs).getUTCFullYear();
  var tickvals = [];
  var ticktext = [];
  var prevTickYear = null;
  for (var tIdx = 0; tIdx < chosenTicks.length; tIdx++) {
    var tMs = chosenTicks[tIdx];
    var dtTick = new Date(tMs);
    var tickYear = dtTick.getUTCFullYear();
    var yearChanged = prevTickYear !== null && tickYear !== prevTickYear;
    var showYear = isMonthly
      ? tIdx === 0 || dtTick.getUTCMonth() === 0 || yearChanged
      : multiYear && (tIdx === 0 || yearChanged);
    tickvals.push(formatUtcMsIso(tMs));
    ticktext.push(formatLedgerDateTickLabel(tMs, isMonthly, showYear));
    prevTickYear = tickYear;
  }

  return {
    tickvals: tickvals,
    ticktext: ticktext,
    range: null,
    traceMode: traceMode,
    primaryMarkerSize: primaryMarkerSize,
    secondaryMarkerSize: secondaryMarkerSize,
    pxPerPoint: pxPerPoint,
  };
}

function buildLedgerAxisKey(spec) {
  return spec ? spec.tickvals.join(",") + "|" + spec.ticktext.join(",") : "";
}

function buildLedgerStyleKey(spec) {
  return spec ? spec.traceMode + "|" + spec.primaryMarkerSize + "|" + spec.secondaryMarkerSize : "";
}

function syncLedgerTimelineForCurrentWidth(el, explicitRange) {
  if (
    !el ||
    !el._ledgerTimelineCtx ||
    !el.data ||
    !el.layout ||
    !el.layout.xaxis ||
    el.layout.xaxis.type !== "date" ||
    typeof window.Plotly === "undefined"
  ) {
    return;
  }
  var cardEl = el.closest ? el.closest(".panel-card") : null;
  if (cardEl && cardEl.classList.contains("is-hidden")) return;

  var measuredW = el.clientWidth || (el.getBoundingClientRect ? el.getBoundingClientRect().width : 0);
  if (!measuredW || measuredW <= 0) return;

  var ctx = el._ledgerTimelineCtx;
  var visibleRange = explicitRange;
  if (explicitRange === undefined) {
    var xAxis = el._fullLayout && el._fullLayout.xaxis ? el._fullLayout.xaxis : el.layout.xaxis;
    visibleRange =
      xAxis && !xAxis.autorange && Array.isArray(xAxis.range) && xAxis.range.length === 2
        ? xAxis.range
        : null;
  }

  var spec = computeLedgerDateAxisSpec(ctx.dates, measuredW, visibleRange);
  if (!spec) return;

  var nextKey = buildLedgerAxisKey(spec);
  var nextStyleKey = buildLedgerStyleKey(spec);
  if (nextKey === ctx.lastKey && nextStyleKey === ctx.lastStyleKey) return;

  ctx.updating = true;
  var tasks = [];
  if (nextKey !== ctx.lastKey && typeof window.Plotly.relayout === "function") {
    ctx.lastKey = nextKey;
    tasks.push(
      window.Plotly.relayout(el, {
        "xaxis.tickmode": "array",
        "xaxis.tickvals": spec.tickvals,
        "xaxis.ticktext": spec.ticktext,
      })
    );
  }
  if (
    nextStyleKey !== ctx.lastStyleKey &&
    typeof window.Plotly.restyle === "function" &&
    el.data.length >= 2
  ) {
    ctx.lastStyleKey = nextStyleKey;
    tasks.push(
      window.Plotly.restyle(
        el,
        {
          mode: [spec.traceMode, spec.traceMode],
          "marker.size": [spec.primaryMarkerSize, spec.secondaryMarkerSize],
        },
        [0, 1]
      )
    );
  }
  var finish = function () {
    ctx.updating = false;
  };
  if (tasks.length && tasks[0] && typeof tasks[0].then === "function") {
    Promise.all(tasks).then(finish, finish);
  } else {
    finish();
  }
}

function attachLedgerTimelineHandlers(el) {
  if (!el || el._ledgerHandlersAttached) return;
  el._ledgerHandlersAttached = true;

  function scheduleSync(explicitRange) {
    if (el._ledgerSyncRaf) {
      window.cancelAnimationFrame(el._ledgerSyncRaf);
    }
    el._ledgerSyncRaf = window.requestAnimationFrame(function () {
      el._ledgerSyncRaf = 0;
      syncLedgerTimelineForCurrentWidth(el, explicitRange);
    });
  }

  if (typeof window.ResizeObserver !== "undefined") {
    el._ledgerResizeObserver = new window.ResizeObserver(function () {
      scheduleSync(undefined);
    });
    el._ledgerResizeObserver.observe(el);
  } else {
    window.addEventListener("resize", function () {
      scheduleSync(undefined);
    });
  }

  if (typeof el.on === "function") {
    el.on("plotly_relayout", function (evData) {
      var ctx = el._ledgerTimelineCtx;
      if (!ctx || ctx.updating || !evData) return;
      if (evData["xaxis.autorange"] === true) {
        scheduleSync(null);
        return;
      }
      var r0 =
        evData["xaxis.range[0]"] !== undefined
          ? evData["xaxis.range[0]"]
          : Array.isArray(evData["xaxis.range"])
            ? evData["xaxis.range"][0]
            : undefined;
      var r1 =
        evData["xaxis.range[1]"] !== undefined
          ? evData["xaxis.range[1]"]
          : Array.isArray(evData["xaxis.range"])
            ? evData["xaxis.range"][1]
            : undefined;
      if (r0 !== undefined && r1 !== undefined) {
        scheduleSync([r0, r1]);
      }
    });
  }
}

export function renderLedgerEmptyPlaceholder(message) {
  if (!dom.ledgerChartEl) return;
  dom.ledgerChartEl._ledgerTimelineCtx = null;
  if (typeof window.Plotly !== "undefined" && dom.ledgerChartEl.data) {
    window.Plotly.purge(dom.ledgerChartEl);
  }
  dom.ledgerChartEl.classList.add("is-idle");
  dom.ledgerChartEl.innerHTML =
    '<div class="ledger-empty-state">' +
    escapeHtml(
      message ||
        "No trade ledger data yet. Click Sync Flex / Gateway or Import XML / CSV to inspect P&L, roll chains, and open MTM."
    ) +
    "</div>";
}

function buildOpenPositionChartLabel(op) {
  if (op.asset_class === "OPT" && op.expiry && op.strike !== null && op.strike !== undefined) {
    return (
      op.underlying +
      " " +
      formatCompactExpiry(op.expiry) +
      " " +
      formatStrikeBadge(op.strike) +
      (op.right || "") +
      " (" +
      fmtSignedQty(op.quantity) +
      ")"
    );
  }
  return op.underlying + " STK (" + fmtSignedQty(op.quantity) + ")";
}

export function renderLedgerChart(report, theme) {
  var el = dom.ledgerChartEl;
  if (!el) return;
  if (!report) {
    renderLedgerEmptyPlaceholder();
    return;
  }
  var t = theme || themeVars();
  if (typeof window.Plotly === "undefined") {
    return;
  }

  // Case 1: Open & Unrealized MTM horizontal bar breakdown
  if (report.view === "open_mtm") {
    var valued = (report.open_positions || []).filter(function (op) {
      return op.unrealized_pnl_ex_fee !== null && op.unrealized_pnl_ex_fee !== undefined;
    });
    if (!valued.length) {
      var msg =
        report.open_positions && report.open_positions.length
          ? "Open positions loaded without mark prices yet. Click Refresh Open MTM to fetch live quotes."
          : "No open option or stock positions currently tracked in the ledger.";
      renderLedgerEmptyPlaceholder(msg);
      return;
    }

    var sortedOpen = valued.slice().sort(function (a, b) {
      return Number(a.unrealized_pnl_ex_fee) - Number(b.unrealized_pnl_ex_fee);
    });
    var yLabels = [];
    var xVals = [];
    var barColors = [];
    var hovers = [];
    for (var i = 0; i < sortedOpen.length; i++) {
      var op = sortedOpen[i];
      var mtm = Number(op.unrealized_pnl_ex_fee);
      var lbl = buildOpenPositionChartLabel(op);
      yLabels.push(lbl);
      xVals.push(mtm);
      barColors.push(mtm >= 0 ? t.call : t.put);
      hovers.push(
        "<b>" +
          escapeHtml(op.symbol) +
          "</b><br>Strategy: " +
          escapeHtml(formatStrategyLabel(op.strategy_type)) +
          "<br>Qty: " +
          fmtSignedQty(op.quantity) +
          " @ Avg " +
          fmtMoney(op.avg_cost_price) +
          "<br>Mark (" +
          escapeHtml(op.mark_source) +
          "): " +
          fmtMoney(op.mark_price) +
          "<br>Unrealized MTM: " +
          fmtSignedMoney(mtm) +
          "<br>Fees: " +
          fmtMoney(op.fees)
      );
    }

    el._ledgerTimelineCtx = null;
    if (!el.data) el.innerHTML = "";
    el.classList.remove("is-idle");
    var openLayout = baseLayout(t, "Unrealized MTM (Ex-Fee, $)", "");
    openLayout.hovermode = "closest";
    openLayout.xaxis.tickprefix = "$";
    openLayout.yaxis.automargin = true;
    openLayout.margin = { t: 26, r: 22, b: 44, l: 130 };
    openLayout.shapes = [
      {
        type: "line",
        x0: 0,
        x1: 0,
        y0: 0,
        y1: 1,
        yref: "paper",
        line: { color: t.muted, width: 1.5, dash: "dot" },
      },
    ];
    window.Plotly.react(
      el,
      [
        {
          type: "bar",
          orientation: "h",
          name: "Unrealized MTM",
          y: yLabels,
          x: xVals,
          marker: { color: barColors },
          text: hovers,
          hovertemplate: "%{text}<extra></extra>",
        },
      ],
      openLayout,
      { responsive: true, displayModeBar: false }
    );
    schedulePlotlyResize(el);
    window.setTimeout(function () {
      schedulePlotlyResize(el);
    }, 60);
    return;
  }

  // Case 2: By Strategy or By Ticker horizontal comparison
  if (report.group_by === "strategy" || report.group_by === "ticker") {
    var rows = report.breakdowns || [];
    if (!rows.length) {
      renderLedgerEmptyPlaceholder(
        "No closed or active ledger groups in window " +
          report.window +
          (report.ticker !== "ALL" ? " for " + report.ticker : "") +
          "."
      );
      return;
    }

    // Reverse sorted descending array so highest profit sits at the top of horizontal bars.
    var ascRows = rows.slice().reverse();
    var gLabels = [];
    var pnlVals = [];
    var pnlColors = [];
    var premVals = [];
    var pnlHovers = [];
    var premHovers = [];
    for (var b = 0; b < ascRows.length; b++) {
      var br = ascRows[b];
      var totalPnl =
        Number(br.realized_pnl_ex_fee || 0) +
        (report.include_stock_gain ? Number(br.stock_realized_pnl_ex_fee || 0) : 0);
      var netPrem = Number(br.net_premium_ex_fee || 0);
      var wrTxt =
        br.win_rate_pct !== null && br.win_rate_pct !== undefined
          ? fmtNum(br.win_rate_pct, 1) + "%"
          : "\u2014";
      gLabels.push(br.label);
      pnlVals.push(totalPnl);
      pnlColors.push(totalPnl >= 0 ? t.call : t.put);
      premVals.push(netPrem);

      var hoverCore =
        "<b>" +
        escapeHtml(br.label) +
        "</b><br>Trades: " +
        br.trade_count +
        " \u2022 Rolls: " +
        br.roll_count +
        " \u2022 Win: " +
        wrTxt +
        "<br>Realized P&L (Ex-Fee): " +
        fmtSignedMoney(totalPnl) +
        "<br>Net Premium: " +
        fmtSignedMoney(netPrem) +
        "<br>Fees: " +
        fmtMoney(br.fees);
      pnlHovers.push(hoverCore);
      premHovers.push(hoverCore);
    }

    el._ledgerTimelineCtx = null;
    if (!el.data) el.innerHTML = "";
    el.classList.remove("is-idle");
    var barLayout = baseLayout(t, "USD ($)", "");
    barLayout.barmode = "group";
    barLayout.hovermode = "closest";
    barLayout.xaxis.tickprefix = "$";
    barLayout.yaxis.automargin = true;
    barLayout.margin = { t: 40, r: 22, b: 44, l: 135 };
    barLayout.shapes = [
      {
        type: "line",
        x0: 0,
        x1: 0,
        y0: 0,
        y1: 1,
        yref: "paper",
        line: { color: t.muted, width: 1.5, dash: "dot" },
      },
    ];
    window.Plotly.react(
      el,
      [
        {
          type: "bar",
          orientation: "h",
          name: "Realized P&L (Ex-Fee)",
          y: gLabels,
          x: pnlVals,
          marker: { color: pnlColors },
          text: pnlHovers,
          hovertemplate: "%{text}<extra></extra>",
        },
        {
          type: "bar",
          orientation: "h",
          name: "Net Option Premium",
          y: gLabels,
          x: premVals,
          marker: { color: t.dtePalette[0], opacity: 0.72 },
          text: premHovers,
          hovertemplate: "%{text}<extra></extra>",
        },
      ],
      barLayout,
      { responsive: true, displayModeBar: false }
    );
    schedulePlotlyResize(el);
    window.setTimeout(function () {
      schedulePlotlyResize(el);
    }, 60);
    return;
  }

  // Case 3: Cumulative Ex-Fee Realized P&L + Net Option Premium curve (campaigns / executions)
  var pts = report.chart_points || [];
  if (!pts.length) {
    renderLedgerEmptyPlaceholder(
      report.status && report.status.execution_count === 0
        ? "No trade ledger executions yet. Click Sync Flex / Gateway or Import XML / CSV."
        : "No cumulative ledger activity in " +
            report.window +
            (report.ticker !== "ALL" ? " for " + report.ticker : "") +
            ". Try widening the timeframe to ALL."
    );
    return;
  }

  var dates = [];
  var cumPnl = [];
  var cumPrem = [];
  var pnlPointHover = [];
  var premPointHover = [];
  for (var c = 0; c < pts.length; c++) {
    var pt = pts[c];
    dates.push(pt.date);
    cumPnl.push(pt.cumulative_realized_ex_fee);
    cumPrem.push(pt.cumulative_premium_ex_fee);
    var detailHover =
      "<b>" +
      escapeHtml(pt.date) +
      "</b><br>Cumul. Realized (Ex-Fee): " +
      fmtSignedMoney(pt.cumulative_realized_ex_fee) +
      "<br>Cumul. Net Premium: " +
      fmtSignedMoney(pt.cumulative_premium_ex_fee) +
      "<br>Day P&L: " +
      fmtSignedMoney(pt.period_pnl_ex_fee) +
      " \u2022 Fees: " +
      fmtMoney(pt.fees);
    pnlPointHover.push(detailHover);
    premPointHover.push(detailHover);
  }

  var finalPnl = cumPnl[cumPnl.length - 1] || 0;
  var lineColor = finalPnl >= 0 ? t.call : t.put;

  var chartWidth = measureLedgerChartWidth(el);
  var axisSpec = computeLedgerDateAxisSpec(dates, chartWidth, null);
  var traceMode = axisSpec ? axisSpec.traceMode : "lines+markers";
  var primaryMarkerSize = axisSpec ? axisSpec.primaryMarkerSize : 6;
  var secondaryMarkerSize = axisSpec ? axisSpec.secondaryMarkerSize : 5;

  if (!el.data) el.innerHTML = "";
  el.classList.remove("is-idle");
  var lineLayout = baseLayout(t, "Activity Date (" + report.window + ")", "Cumulative USD ($)");
  lineLayout.margin = { t: 40, r: 24, b: 56, l: 62 };
  lineLayout.yaxis.tickprefix = "$";
  lineLayout.xaxis.type = "date";
  lineLayout.xaxis.title = {
    text: "Activity Date (" + report.window + ")",
    standoff: 10,
    font: { size: 11, color: t.muted },
  };
  lineLayout.xaxis.tickfont = { size: 10, color: t.ink };
  lineLayout.xaxis.ticks = "outside";
  lineLayout.xaxis.ticklen = 4;
  lineLayout.xaxis.tickcolor = t.grid;
  lineLayout.xaxis.automargin = false;
  if (axisSpec) {
    lineLayout.xaxis.tickmode = "array";
    lineLayout.xaxis.tickvals = axisSpec.tickvals;
    lineLayout.xaxis.ticktext = axisSpec.ticktext;
    if (axisSpec.range) {
      lineLayout.xaxis.range = axisSpec.range;
    }
  }
  lineLayout.shapes = [
    {
      type: "line",
      x0: 0,
      x1: 1,
      xref: "paper",
      y0: 0,
      y1: 0,
      line: { color: t.muted, width: 1.5, dash: "dot" },
    },
  ];
  window.Plotly.react(
    el,
    [
      {
        type: "scatter",
        mode: traceMode,
        name: "Cumul. Realized P&L (Ex-Fee)",
        x: dates,
        y: cumPnl,
        line: { color: lineColor, width: 2.5 },
        marker: { color: lineColor, size: primaryMarkerSize },
        fill: "tozeroy",
        fillcolor: finalPnl >= 0 ? "rgba(16, 185, 129, 0.12)" : "rgba(239, 68, 68, 0.12)",
        text: pnlPointHover,
        hovertemplate: "%{text}<extra></extra>",
      },
      {
        type: "scatter",
        mode: traceMode,
        name: "Cumul. Net Option Premium",
        x: dates,
        y: cumPrem,
        line: { color: t.dtePalette[0], width: 2, dash: "dot" },
        marker: { color: t.dtePalette[0], size: secondaryMarkerSize },
        text: premPointHover,
        hovertemplate: "%{text}<extra></extra>",
      },
    ],
    lineLayout,
    { responsive: true, displayModeBar: false }
  );
  el._ledgerTimelineCtx = {
    dates: dates,
    lastKey: buildLedgerAxisKey(axisSpec),
    lastStyleKey: buildLedgerStyleKey(axisSpec),
    updating: false,
  };
  attachLedgerTimelineHandlers(el);
  schedulePlotlyResize(el);
  window.setTimeout(function () {
    schedulePlotlyResize(el);
    syncLedgerTimelineForCurrentWidth(el);
  }, 60);
}

// ------------------------------------------------------------------ Table Rendering
function buildTimelineStepHtml(step) {
  var badgeText = step.roll_label || step.action || "STEP";
  var badgeClass =
    step.action === "ROLL"
      ? "ledger-pill--rolls"
      : step.action === "CLOSE" || step.action === "EXPIRE"
        ? "ledger-pill--closed"
        : step.action === "ASSIGN" || step.action === "EXERCISE"
          ? "ledger-pill--assigned"
          : "ledger-pill--open";
  return (
    '<div class="ledger-timeline-step">' +
    '<span class="ledger-timeline-step-idx">#' +
    escapeHtml(step.step_index) +
    "</span>" +
    '<span class="ledger-timeline-step-ts">' +
    escapeHtml(formatShortUtc(step.executed_at)) +
    "</span>" +
    '<span><span class="ledger-pill ' +
    badgeClass +
    '">' +
    escapeHtml(badgeText) +
    "</span></span>" +
    '<span title="' +
    escapeHtml(step.summary || "") +
    '">' +
    escapeHtml(step.strike_summary || step.summary || "\u2014") +
    "</span>" +
    '<span class="ledger-timeline-step-num">' +
    escapeHtml(fmtNum(step.contracts, 0)) +
    "x</span>" +
    '<span class="ledger-timeline-step-num ' +
    pnlToneClass(step.cashflow_ex_fee) +
    '">' +
    escapeHtml(fmtSignedMoney(step.cashflow_ex_fee)) +
    "</span>" +
    '<span class="ledger-timeline-step-num ' +
    pnlToneClass(step.realized_pnl_ex_fee) +
    '">' +
    escapeHtml(fmtSignedMoney(step.realized_pnl_ex_fee)) +
    "</span>" +
    '<span class="ledger-timeline-step-num ledger-num-fee">' +
    escapeHtml(fmtMoney(step.fees)) +
    "</span>" +
    "</div>"
  );
}

function buildCampaignTimelineRowHtml(camp) {
  var steps = camp.steps || [];
  var stepsHtml = steps.length
    ? steps.map(buildTimelineStepHtml).join("")
    : '<div class="ledger-timeline-empty">No individual option roll steps in this window.</div>';
  return (
    '<tr class="ledger-timeline-row" data-campaign-detail="' +
    escapeHtml(camp.campaign_id) +
    '">' +
    '<td colspan="9">' +
    '<div class="ledger-timeline">' +
    '<div class="ledger-timeline-title">Roll Chain Timeline \u2022 ' +
    escapeHtml(camp.underlying) +
    " \u2022 " +
    escapeHtml(formatStrategyLabel(camp.strategy_type)) +
    " (" +
    steps.length +
    " step" +
    (steps.length === 1 ? "" : "s") +
    ")</div>" +
    '<div class="ledger-timeline-steps">' +
    stepsHtml +
    "</div>" +
    "</div>" +
    "</td>" +
    "</tr>"
  );
}

function renderOnboardingRow(colspan, status) {
  var flexReady = Boolean(status && status.flex_configured);
  var text = flexReady
    ? "No ledger records match this window or ticker filter. Try switching timeframe to ALL or click Sync Flex / Gateway."
    : "No trade ledger data archived yet. Click Import XML / CSV to upload an IBKR Flex XML or Activity Statement CSV, or configure IBKR_FLEX_TOKEN in .env for one-click sync.";
  return '<tr><td colspan="' + colspan + '" class="ledger-table-empty">' + escapeHtml(text) + "</td></tr>";
}

function renderCampaignsTable(report, status) {
  dom.ledgerTableHead.innerHTML =
    "<tr>" +
    '<th class="text-left"></th>' +
    '<th class="text-left">Ticker</th>' +
    '<th class="text-left">Strategy</th>' +
    '<th class="text-left">Status</th>' +
    '<th class="text-left">Rolls</th>' +
    '<th class="text-left">Chain Summary</th>' +
    '<th class="num">Net Credit</th>' +
    '<th class="num">Realized P&amp;L</th>' +
    '<th class="num">Fees</th>' +
    "</tr>";

  var camps = report.campaigns || [];
  if (!camps.length) {
    dom.ledgerTableBody.innerHTML = renderOnboardingRow(9, status);
    return;
  }

  var html = "";
  for (var i = 0; i < camps.length; i++) {
    var c = camps[i];
    var expanded = Boolean(state.ledgerExpandedIds[c.campaign_id]);
    var statusMod = "ledger-pill--" + String(c.status || "OPEN").toLowerCase();
    var hasSteps = Boolean(c.steps && c.steps.length);
    var rollsHtml =
      c.roll_count > 0
        ? '<span class="ledger-pill ledger-pill--rolls">' +
          c.roll_count +
          " roll" +
          (c.roll_count === 1 ? "" : "s") +
          "</span>"
        : '<span class="ledger-num-fee">\u2014</span>';
    var chainSummary =
      c.current_summary && c.current_summary !== c.initial_summary
        ? c.initial_summary + " \u2192 " + c.current_summary
        : c.initial_summary;
    var totalPnl =
      Number(c.realized_pnl_ex_fee || 0) +
      (report.include_stock_gain ? Number(c.stock_realized_pnl_ex_fee || 0) : 0);
    var pnlSub =
      report.include_stock_gain && Math.abs(Number(c.stock_realized_pnl_ex_fee || 0)) > 1e-6
        ? '<span class="ledger-subnote">Opt ' +
          escapeHtml(fmtSignedMoney(c.realized_pnl_ex_fee)) +
          " / Stk " +
          escapeHtml(fmtSignedMoney(c.stock_realized_pnl_ex_fee)) +
          "</span>"
        : "";

    html +=
      '<tr class="ledger-data-row ledger-campaign-row' +
      (expanded ? " is-expanded" : "") +
      '" data-campaign-row="' +
      escapeHtml(c.campaign_id) +
      '">' +
      '<td class="text-left">' +
      '<button type="button" class="ledger-expand-btn" data-campaign-toggle="' +
      escapeHtml(c.campaign_id) +
      '" aria-expanded="' +
      (expanded ? "true" : "false") +
      '" aria-label="Toggle roll chain timeline"' +
      (hasSteps ? "" : " disabled") +
      ">" +
      (expanded ? "\u25BC" : "\u25B6") +
      "</button></td>" +
      '<td class="text-left"><strong>' +
      escapeHtml(c.underlying) +
      "</strong></td>" +
      '<td class="text-left"><span class="ledger-pill">' +
      escapeHtml(formatStrategyLabel(c.strategy_type)) +
      "</span></td>" +
      '<td class="text-left"><span class="ledger-pill ' +
      escapeHtml(statusMod) +
      '">' +
      escapeHtml(c.status) +
      "</span></td>" +
      '<td class="text-left">' +
      rollsHtml +
      "</td>" +
      '<td class="text-left ledger-chain-summary" title="' +
      escapeHtml(chainSummary) +
      '">' +
      escapeHtml(chainSummary) +
      "</td>" +
      '<td class="num ' +
      pnlToneClass(c.net_premium_ex_fee) +
      '">' +
      escapeHtml(fmtSignedMoney(c.net_premium_ex_fee)) +
      "</td>" +
      '<td class="num ' +
      pnlToneClass(totalPnl) +
      '">' +
      escapeHtml(fmtSignedMoney(totalPnl)) +
      pnlSub +
      "</td>" +
      '<td class="num ledger-num-fee">' +
      escapeHtml(fmtMoney(c.fees)) +
      "</td>" +
      "</tr>";

    if (expanded) {
      html += buildCampaignTimelineRowHtml(c);
    }
  }
  dom.ledgerTableBody.innerHTML = html;
}

function renderBreakdownTable(report, status) {
  var groupColLabel = report.group_by === "strategy" ? "Strategy" : "Ticker";
  dom.ledgerTableHead.innerHTML =
    "<tr>" +
    '<th class="text-left">' +
    groupColLabel +
    "</th>" +
    '<th class="num">Trades</th>' +
    '<th class="num">Rolls</th>' +
    '<th class="num">Win Rate</th>' +
    '<th class="num">Net Premium</th>' +
    '<th class="num">Realized P&amp;L</th>' +
    '<th class="num">Fees</th>' +
    "</tr>";

  var rows = report.breakdowns || [];
  if (!rows.length) {
    dom.ledgerTableBody.innerHTML = renderOnboardingRow(7, status);
    return;
  }

  var html = "";
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    var totalPnl =
      Number(r.realized_pnl_ex_fee || 0) +
      (report.include_stock_gain ? Number(r.stock_realized_pnl_ex_fee || 0) : 0);
    var wrTxt =
      r.win_rate_pct !== null && r.win_rate_pct !== undefined
        ? fmtNum(r.win_rate_pct, 1) + "% (" + r.win_count + "W/" + r.loss_count + "L)"
        : "\u2014";
    html +=
      '<tr class="ledger-data-row">' +
      '<td class="text-left"><strong>' +
      escapeHtml(r.label) +
      "</strong></td>" +
      '<td class="num">' +
      escapeHtml(String(r.trade_count)) +
      "</td>" +
      '<td class="num">' +
      escapeHtml(String(r.roll_count)) +
      "</td>" +
      '<td class="num">' +
      escapeHtml(wrTxt) +
      "</td>" +
      '<td class="num ' +
      pnlToneClass(r.net_premium_ex_fee) +
      '">' +
      escapeHtml(fmtSignedMoney(r.net_premium_ex_fee)) +
      "</td>" +
      '<td class="num ' +
      pnlToneClass(totalPnl) +
      '">' +
      escapeHtml(fmtSignedMoney(totalPnl)) +
      "</td>" +
      '<td class="num ledger-num-fee">' +
      escapeHtml(fmtMoney(r.fees)) +
      "</td>" +
      "</tr>";
  }
  dom.ledgerTableBody.innerHTML = html;
}

function renderExecutionsTable(report, status) {
  dom.ledgerTableHead.innerHTML =
    "<tr>" +
    '<th class="text-left">Executed UTC</th>' +
    '<th class="text-left">Ticker</th>' +
    '<th class="text-left">Strategy / Symbol</th>' +
    '<th class="text-left">Side</th>' +
    '<th class="num">Qty</th>' +
    '<th class="num">Price</th>' +
    '<th class="num">Proceeds (Ex-Fee)</th>' +
    '<th class="num">Fees</th>' +
    '<th class="text-left">O/C</th>' +
    '<th class="text-left">Event</th>' +
    '<th class="text-left">Source</th>' +
    "</tr>";

  var execs = report.executions || [];
  if (!execs.length) {
    dom.ledgerTableBody.innerHTML = renderOnboardingRow(11, status);
    return;
  }

  var html = "";
  for (var i = 0; i < execs.length; i++) {
    var ex = execs[i];
    var sideCls = ex.side === "SLD" ? "ledger-num-pos" : "ledger-num-neg";
    html +=
      '<tr class="ledger-data-row">' +
      '<td class="text-left">' +
      escapeHtml(formatShortUtc(ex.executed_at)) +
      "</td>" +
      '<td class="text-left"><strong>' +
      escapeHtml(ex.underlying) +
      "</strong></td>" +
      '<td class="text-left">' +
      escapeHtml(ex.symbol) +
      '<span class="ledger-subnote">' +
      escapeHtml(formatStrategyLabel(ex.strategy_type)) +
      "</span></td>" +
      '<td class="text-left ' +
      sideCls +
      '">' +
      escapeHtml(ex.side) +
      "</td>" +
      '<td class="num">' +
      escapeHtml(fmtNum(ex.quantity, Math.abs(ex.quantity - Math.round(ex.quantity)) < 1e-6 ? 0 : 2)) +
      "</td>" +
      '<td class="num">' +
      escapeHtml(fmtMoney(ex.price)) +
      "</td>" +
      '<td class="num ' +
      pnlToneClass(ex.proceeds_ex_fee) +
      '">' +
      escapeHtml(fmtSignedMoney(ex.proceeds_ex_fee)) +
      "</td>" +
      '<td class="num ledger-num-fee">' +
      escapeHtml(fmtMoney(ex.commission)) +
      "</td>" +
      '<td class="text-left">' +
      escapeHtml(ex.open_close || "\u2014") +
      "</td>" +
      '<td class="text-left"><span class="ledger-pill">' +
      escapeHtml(ex.event_code) +
      "</span></td>" +
      '<td class="text-left">' +
      escapeHtml(ex.source) +
      "</td>" +
      "</tr>";
  }
  dom.ledgerTableBody.innerHTML = html;
}

function renderOpenMtmTable(report, status) {
  dom.ledgerTableHead.innerHTML =
    "<tr>" +
    '<th class="text-left">Symbol</th>' +
    '<th class="text-left">Strategy</th>' +
    '<th class="num">Qty</th>' +
    '<th class="num">Avg Cost</th>' +
    '<th class="num">Mark Price</th>' +
    '<th class="text-left">Source</th>' +
    '<th class="num">Market Value</th>' +
    '<th class="num">Unrealized P&amp;L</th>' +
    '<th class="num">Fees</th>' +
    "</tr>";

  var positions = report.open_positions || [];
  if (!positions.length) {
    dom.ledgerTableBody.innerHTML = renderOnboardingRow(9, status);
    return;
  }

  var html = "";
  for (var i = 0; i < positions.length; i++) {
    var op = positions[i];
    var srcMod =
      op.mark_source === "LIVE_GATEWAY"
        ? "ledger-pill--live"
        : op.mark_source === "FLEX_MARK"
          ? "ledger-pill--flex"
          : "ledger-pill--none";
    var subDesc =
      op.asset_class === "OPT" && op.expiry
        ? formatCompactExpiry(op.expiry) +
          " " +
          formatStrikeBadge(op.strike) +
          (op.right || "")
        : op.asset_class;
    html +=
      '<tr class="ledger-data-row">' +
      '<td class="text-left"><strong>' +
      escapeHtml(op.symbol) +
      '</strong><span class="ledger-subnote">' +
      escapeHtml(subDesc) +
      "</span></td>" +
      '<td class="text-left"><span class="ledger-pill">' +
      escapeHtml(formatStrategyLabel(op.strategy_type)) +
      "</span></td>" +
      '<td class="num ' +
      pnlToneClass(op.quantity) +
      '">' +
      escapeHtml(fmtSignedQty(op.quantity)) +
      "</td>" +
      '<td class="num">' +
      escapeHtml(fmtMoney(op.avg_cost_price)) +
      "</td>" +
      '<td class="num">' +
      escapeHtml(op.mark_price !== null && op.mark_price !== undefined ? fmtMoney(op.mark_price) : "\u2014") +
      "</td>" +
      '<td class="text-left"><span class="ledger-pill ' +
      srcMod +
      '">' +
      escapeHtml(op.mark_source) +
      "</span></td>" +
      '<td class="num">' +
      escapeHtml(
        op.market_value !== null && op.market_value !== undefined
          ? fmtSignedMoney(op.market_value)
          : "\u2014"
      ) +
      "</td>" +
      '<td class="num ' +
      pnlToneClass(op.unrealized_pnl_ex_fee) +
      '">' +
      escapeHtml(fmtSignedMoney(op.unrealized_pnl_ex_fee)) +
      "</td>" +
      '<td class="num ledger-num-fee">' +
      escapeHtml(fmtMoney(op.fees)) +
      "</td>" +
      "</tr>";
  }
  dom.ledgerTableBody.innerHTML = html;
}

export function renderLedgerTable(payload) {
  if (!dom.ledgerTableHead || !dom.ledgerTableBody || !payload || !payload.report) return;
  var report = payload.report;
  var status = payload.status || null;
  if (report.view === "open_mtm") {
    renderOpenMtmTable(report, status);
  } else if (report.group_by === "strategy" || report.group_by === "ticker") {
    renderBreakdownTable(report, status);
  } else if (report.group_by === "executions") {
    renderExecutionsTable(report, status);
  } else {
    renderCampaignsTable(report, status);
  }
}

export function toggleCampaignExpand(campaignId) {
  if (!campaignId) return;
  if (state.ledgerExpandedIds[campaignId]) {
    delete state.ledgerExpandedIds[campaignId];
  } else {
    state.ledgerExpandedIds[campaignId] = true;
  }
  if (state.ledgerData) {
    renderLedgerTable(state.ledgerData);
  }
}

// ------------------------------------------------------------------ Rendering Bundle
export function renderLedgerDashboard(payload) {
  if (!payload || !payload.report) return;
  state.ledgerData = payload;
  var report = payload.report;
  renderLedgerStatusBadge(payload.status);
  syncLedgerControlStates(report);
  renderLedgerKpis(report);
  renderLedgerTable(payload);
  if (isPanelVisible("trade_ledger")) {
    renderLedgerChart(report, themeVars());
  }
}

// ------------------------------------------------------------------ Network Actions
export async function loadLedger() {
  var params = [
    "window=" + encodeURIComponent(state.ledgerWindow || "1M"),
    "ticker=" + encodeURIComponent(state.ledgerTicker || "ALL"),
    "group_by=" + encodeURIComponent(state.ledgerGroupBy || "campaigns"),
    "view=" + encodeURIComponent(state.ledgerView || "realized"),
    "include_stock_gain=" + (state.ledgerIncludeStockGain ? "true" : "false"),
  ];
  var url = "/api/ledger?" + params.join("&");
  try {
    var res = await apiJson(url);
    if (res.ok && res.body) {
      renderLedgerDashboard(res.body);
      return res.body;
    }
    var msg = extractApiErrorMessage(res.body, res.textSnippet || "Unable to load Trade Ledger.");
    showBanner("error", msg, {
      phase: "LEDGER_LOAD",
      status: res.status,
      errorCode: res.body && res.body.error_code,
      elapsedMs: res.elapsedMs,
      rawBody: res.textSnippet,
    });
  } catch (err) {
    showBanner("warn", "Unable to fetch Trade Ledger analytics.", {
      phase: "LEDGER_LOAD",
      detail: err && err.message ? err.message : String(err || ""),
    });
  }
  return null;
}

export async function syncLedgerFromUI() {
  if (state.ledgerBusy) return;
  setLedgerBusy(true, "sync");
  try {
    var res = await apiJson("/api/ledger/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      timeoutMs: 90000,
      body: JSON.stringify({
        mode: state.ledgerSyncMode || "auto",
        include_gateway: null,
      }),
    });
    if (res.ok && res.body) {
      var b = res.body;
      var msg =
        "Ledger synced (" +
        b.mode_used +
        "): " +
        b.executions_inserted +
        " inserted, " +
        b.executions_updated +
        " updated, " +
        b.open_positions_count +
        " open positions.";
      if (b.notices && b.notices.length) {
        msg += " " + b.notices.join(" ");
      }
      showBanner("info", msg, {
        phase: "LEDGER_SYNC",
        elapsedMs: res.elapsedMs,
      });
      await loadLedger();
      await fetchStatus();
      return;
    }
    var errTone = res.status === 503 || res.status === 422 ? "warn" : "error";
    var errMsg = extractApiErrorMessage(res.body, res.textSnippet || "Trade ledger sync failed.");
    showBanner(errTone, errMsg, {
      phase: "LEDGER_SYNC",
      status: res.status,
      errorCode: res.body && res.body.error_code,
      elapsedMs: res.elapsedMs,
      detail: res.body && res.body.detail ? String(res.body.detail) : "",
      rawBody: res.textSnippet,
    });
  } catch (err) {
    showBanner("error", "Trade ledger sync failed: " + (err && err.message ? err.message : String(err || "")), {
      phase: "LEDGER_SYNC",
      elapsedMs: err && err.elapsedMs,
    });
  } finally {
    setLedgerBusy(false, "");
  }
}

export async function importLedgerFile(file) {
  if (!file || state.ledgerBusy) return;
  setLedgerBusy(true, "import");
  try {
    var formData = new window.FormData();
    formData.append("file", file);
    var res = await apiJson("/api/ledger/import", {
      method: "POST",
      timeoutMs: 60000,
      body: formData,
    });
    if (res.ok && res.body) {
      var b = res.body;
      var msg =
        "Imported " +
        b.filename +
        " (" +
        b.file_format +
        "): " +
        b.executions_inserted +
        " inserted, " +
        b.executions_updated +
        " updated, " +
        b.open_positions_imported +
        " open positions.";
      if (b.notices && b.notices.length) {
        msg += " " + b.notices.join(" ");
      }
      showBanner("info", msg, {
        phase: "LEDGER_IMPORT",
        elapsedMs: res.elapsedMs,
      });
      await loadLedger();
      await fetchStatus();
      return;
    }
    var errMsg = extractApiErrorMessage(res.body, res.textSnippet || "Ledger file import failed.");
    showBanner("error", errMsg, {
      phase: "LEDGER_IMPORT",
      status: res.status,
      errorCode: res.body && res.body.error_code,
      elapsedMs: res.elapsedMs,
      detail: res.body && res.body.detail ? String(res.body.detail) : "",
      rawBody: res.textSnippet,
    });
  } catch (err) {
    showBanner("error", "Ledger file import error: " + (err && err.message ? err.message : String(err || "")), {
      phase: "LEDGER_IMPORT",
    });
  } finally {
    if (dom.ledgerImportFile) {
      dom.ledgerImportFile.value = "";
    }
    setLedgerBusy(false, "");
  }
}

export async function refreshOpenLedgerMtm() {
  if (state.ledgerBusy) return;
  setLedgerBusy(true, "mtm");
  try {
    var res = await apiJson("/api/ledger/mtm", {
      method: "POST",
      timeoutMs: 45000,
    });
    if (res.ok && res.body) {
      var b = res.body;
      var msg =
        "Refreshed live MTM marks for " +
        b.refreshed_count +
        " open position(s)" +
        (b.skipped_count ? " (" + b.skipped_count + " skipped)" : "") +
        ".";
      if (b.notices && b.notices.length) {
        msg += " " + b.notices.join(" ");
      }
      showBanner("info", msg, {
        phase: "LEDGER_MTM",
        elapsedMs: res.elapsedMs,
      });
      await loadLedger();
      return;
    }
    var errTone = res.status === 503 ? "warn" : "error";
    var errMsg = extractApiErrorMessage(res.body, res.textSnippet || "Open MTM refresh failed.");
    showBanner(errTone, errMsg, {
      phase: "LEDGER_MTM",
      status: res.status,
      errorCode: res.body && res.body.error_code,
      elapsedMs: res.elapsedMs,
      detail: res.body && res.body.detail ? String(res.body.detail) : "",
      rawBody: res.textSnippet,
    });
  } catch (err) {
    showBanner("warn", "Open MTM refresh error: " + (err && err.message ? err.message : String(err || "")), {
      phase: "LEDGER_MTM",
    });
  } finally {
    setLedgerBusy(false, "");
  }
}
