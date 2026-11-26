/* IBKR Options Analyzer dashboard — Backtest Robustness Suite controls, API, KPIs, tables & Plotly charts. */

import {
  dom,
  state,
} from "./state.js";
import {
  escapeHtml,
  extractApiErrorMessage,
  themeVars,
} from "./helpers.js";
import {
  apiJson,
} from "./network.js";
import {
  baseLayout,
} from "./charts.js";
import {
  fmtPlainMoney,
  fmtPct,
  signedMoney,
} from "./backtest_tables.js";
import {
  setBacktestNotice,
} from "./backtest_paper.js";
import {
  SENSITIVITY_PARAM_LABELS,
  applySensitivityCellToSystematic,
  buildSharedBacktestConfig,
} from "./backtest.js";

var ROBUSTNESS_SECTIONS = ["COHORTS", "SENSITIVITY", "WALK_FORWARD", "MONTE_CARLO"];

var SECTION_TITLES = {
  COHORTS: "All-Days & Weekday",
  SENSITIVITY: "Sensitivity Surface",
  WALK_FORWARD: "Walk-Forward",
  MONTE_CARLO: "Monte Carlo & Slippage",
};

var OBJECTIVE_LABELS = {
  expectancy_dollars: "Expectancy ($)",
  return_on_risk_pct: "RoR %",
  total_return_pct: "Total Return %",
  profit_factor: "Profit Factor",
  sharpe: "Sharpe",
  calmar: "Calmar",
  win_rate_pct: "Win Rate %",
};

var WFO_MODE_LABELS = {
  PURGED_WFO: "Purged Walk-Forward",
  FIXED_WINDOWS: "Fixed Windows",
};

var sensitivityClickBound = false;

function isWheelOrCcStrategy(strat) {
  return strat === "COVERED_CALL" || strat === "WHEEL";
}

function optionalInt(el) {
  if (!el) return null;
  var raw = String(el.value || "").trim();
  if (!raw) return null;
  var n = parseInt(raw, 10);
  return Number.isNaN(n) ? null : n;
}

function optionalFloat(el) {
  if (!el) return null;
  var raw = String(el.value || "").trim();
  if (!raw) return null;
  var n = Number(raw);
  return Number.isNaN(n) ? null : n;
}

function fmtNumOpt(val, digits) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "\u2014";
  return Number(val).toFixed(digits === undefined ? 2 : digits);
}

function fmtObjectiveValue(objective, val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "\u2014";
  var n = Number(val);
  if (objective === "expectancy_dollars") return signedMoney(n);
  if (
    objective === "return_on_risk_pct" ||
    objective === "total_return_pct" ||
    objective === "win_rate_pct"
  ) {
    return fmtPct(n, objective !== "win_rate_pct");
  }
  return n.toFixed(2);
}

function kpiCardHtml(label, value, sub, cls, tip) {
  var headHtml = tip
    ? '<div class="backtest-kpi__head">' +
      '<span class="backtest-kpi__label">' +
      escapeHtml(label) +
      "</span>" +
      '<button type="button" class="bt-info-btn" data-tip="' +
      escapeHtml(tip) +
      '" aria-label="Info: ' +
      escapeHtml(label) +
      '">i</button>' +
      "</div>"
    : '<span class="backtest-kpi__label">' + escapeHtml(label) + "</span>";
  return (
    '<div class="backtest-kpi">' +
    headHtml +
    '<span class="backtest-kpi__value ' +
    (cls || "") +
    '">' +
    escapeHtml(value) +
    "</span>" +
    (sub ? '<span class="backtest-kpi__sub">' + escapeHtml(sub) + "</span>" : "") +
    "</div>"
  );
}

var DEFAULT_CHART_TITLES = {
  cohorts: "All-Days Entry-Date Invariant Equity & Drawdown",
  sensitivity: "2D Sensitivity Heatmap",
  walkforward: "Stitched Out-of-Sample vs Baseline Equity",
  mcFan: "Stationary Block Bootstrap Fan (P5 / P25 / P50 / P75 / P95)",
  mcDd: "Simulated Max Drawdown Distribution",
};

function setChartTitleEl(el, text) {
  if (el) el.textContent = text;
}

function legendTopConfig(t) {
  return {
    orientation: "h",
    x: 0,
    xanchor: "left",
    y: 1.02,
    yanchor: "bottom",
    bgcolor: t.transparent,
    font: { size: 10, color: t.ink },
  };
}

export function isAxisSupportedForStrategy(param, strat) {
  if (param === "target_delta") {
    return strat === "COVERED_CALL" || strat === "WHEEL";
  }
  if (param === "min_entry_ror_pct") {
    return strat === "BULL_PUT_SPREAD";
  }
  if (param === "min_entry_credit_dollars") {
    return (
      strat === "BULL_PUT_SPREAD" ||
      strat === "COVERED_CALL" ||
      strat === "WHEEL"
    );
  }
  return true;
}

function pickSupportedAxisFallback(strat, excludeParam) {
  var order = isWheelOrCcStrategy(strat)
    ? [
        "target_delta",
        "profit_target_pct",
        "stop_loss_pct",
        "exit_dte",
        "dte_bucket",
        "min_entry_credit_dollars",
      ]
    : [
        "profit_target_pct",
        "stop_loss_pct",
        "exit_dte",
        "dte_bucket",
        "min_entry_credit_dollars",
        "min_entry_ror_pct",
      ];
  for (var i = 0; i < order.length; i++) {
    var cand = order[i];
    if (cand !== excludeParam && isAxisSupportedForStrategy(cand, strat)) {
      return cand;
    }
  }
  return "profit_target_pct";
}

function applyAxisOptionSupport(selectEl, strat) {
  if (!selectEl || !selectEl.options || !selectEl.options.length) return;
  for (var i = 0; i < selectEl.options.length; i++) {
    var opt = selectEl.options[i];
    if (!opt.dataset) opt.dataset = {};
    if (!opt.dataset.baseLabel) {
      opt.dataset.baseLabel = String(opt.textContent || "").trim();
    }
    var supported = isAxisSupportedForStrategy(opt.value, strat);
    opt.disabled = !supported;
    opt.textContent = supported ? opt.dataset.baseLabel : opt.dataset.baseLabel + " (N/A)";
  }
}

export function syncSensitivityAxisOptions(strat, changedAxis) {
  var s = strat || (dom.backtestStrategySelect ? dom.backtestStrategySelect.value : "BULL_PUT_SPREAD");
  applyAxisOptionSupport(dom.btRobSensX, s);
  applyAxisOptionSupport(dom.btRobSensY, s);

  if (dom.btRobSensX && !isAxisSupportedForStrategy(dom.btRobSensX.value, s)) {
    dom.btRobSensX.value = pickSupportedAxisFallback(s, dom.btRobSensY ? dom.btRobSensY.value : "");
  }
  if (dom.btRobSensY && !isAxisSupportedForStrategy(dom.btRobSensY.value, s)) {
    dom.btRobSensY.value = pickSupportedAxisFallback(s, dom.btRobSensX ? dom.btRobSensX.value : "");
  }

  if (dom.btRobSensX && dom.btRobSensY && dom.btRobSensX.value === dom.btRobSensY.value) {
    if (changedAxis === "Y") {
      dom.btRobSensX.value = pickSupportedAxisFallback(s, dom.btRobSensY.value);
    } else {
      dom.btRobSensY.value = pickSupportedAxisFallback(s, dom.btRobSensX.value);
    }
  }
}

function plotRobChart(el, traces, layout) {
  if (!el || typeof window.Plotly === "undefined") return;
  el.classList.remove("is-idle");
  if (!el.data) {
    el.innerHTML = "";
  }
  window.Plotly.react(el, traces, layout, {
    responsive: true,
    displayModeBar: false,
  });
}

function purgeRobPlaceholder(el, msgHtml) {
  if (!el) return;
  if (typeof window.Plotly !== "undefined" && el.data) {
    window.Plotly.purge(el);
  }
  el.classList.add("is-idle");
  el.innerHTML = '<div class="strategy-empty-state">' + msgHtml + "</div>";
}

function setRobustnessBusy(isBusy) {
  state.backtestRobustnessBusy = Boolean(isBusy);
  var btns = [
    dom.btRobRunAllBtn,
    dom.backtestRunBtn,
    dom.btRobRerunCohortsBtn,
    dom.btRobRerunSensBtn,
    dom.btRobRerunWfoBtn,
    dom.btRobRerunMcBtn,
  ];
  for (var i = 0; i < btns.length; i++) {
    if (btns[i]) btns[i].disabled = Boolean(isBusy);
  }
}

export function resizeVisibleRobustnessCharts() {
  if (
    state.backtestMode !== "ROBUSTNESS" ||
    typeof window.Plotly === "undefined" ||
    !window.Plotly.Plots
  ) {
    return;
  }
  var chartMap = {
    COHORTS: [dom.btRobCohortsChart],
    SENSITIVITY: [dom.btRobSensChart],
    WALK_FORWARD: [dom.btRobWfoChart],
    MONTE_CARLO: [dom.btRobMcFanChart, dom.btRobMcDdChart],
  };
  var list = chartMap[state.backtestRobustnessTab] || [];
  window.requestAnimationFrame(function () {
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].data) {
        window.Plotly.Plots.resize(list[i]);
      }
    }
  });
}

export function setActiveRobustnessTab(tabKey) {
  var nextTab = ROBUSTNESS_SECTIONS.indexOf(tabKey) >= 0 ? tabKey : "COHORTS";
  state.backtestRobustnessTab = nextTab;

  if (dom.btRobTabs) {
    var btns = dom.btRobTabs.querySelectorAll("[data-rob-tab]");
    for (var i = 0; i < btns.length; i++) {
      var btn = btns[i];
      var active = btn.getAttribute("data-rob-tab") === nextTab;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    }
  }

  if (dom.btRobVerdicts) {
    var cards = dom.btRobVerdicts.querySelectorAll("[data-rob-verdict]");
    for (var j = 0; j < cards.length; j++) {
      cards[j].classList.toggle("is-active", cards[j].getAttribute("data-rob-verdict") === nextTab);
    }
  }

  var panes = {
    COHORTS: dom.btRobPaneCohorts,
    SENSITIVITY: dom.btRobPaneSensitivity,
    WALK_FORWARD: dom.btRobPaneWalkforward,
    MONTE_CARLO: dom.btRobPaneMontecarlo,
  };
  Object.keys(panes).forEach(function (key) {
    if (panes[key]) panes[key].hidden = key !== nextTab;
  });

  resizeVisibleRobustnessCharts();
}

export function syncRobustnessControlState() {
  var strat = dom.backtestStrategySelect ? dom.backtestStrategySelect.value : "BULL_PUT_SPREAD";
  var nowWheel = isWheelOrCcStrategy(strat);
  var prevWheel = isWheelOrCcStrategy(state.backtestRobustnessLastStrategy);

  if (strat !== state.backtestRobustnessLastStrategy) {
    if (nowWheel && !prevWheel) {
      if (dom.btRobSensX) dom.btRobSensX.value = "target_delta";
      if (dom.btRobSensY) dom.btRobSensY.value = "profit_target_pct";
    } else if (!nowWheel && prevWheel) {
      if (dom.btRobSensX) dom.btRobSensX.value = "profit_target_pct";
      if (dom.btRobSensY) dom.btRobSensY.value = "stop_loss_pct";
    }
    state.backtestRobustnessLastStrategy = strat;
  }

  syncSensitivityAxisOptions(strat, null);

  var rollingWrap = document.querySelector("[data-rob-rolling-wrap]");
  if (rollingWrap) {
    rollingWrap.hidden = !nowWheel;
  }

  var wfoMode = dom.btRobWfoMode ? dom.btRobWfoMode.value : "PURGED_WFO";
  var isFixed = wfoMode === "FIXED_WINDOWS";
  var purgedEls = document.querySelectorAll("[data-rob-wfo-purged]");
  for (var i = 0; i < purgedEls.length; i++) {
    purgedEls[i].hidden = isFixed;
  }
  var fixedEls = document.querySelectorAll("[data-rob-wfo-fixed]");
  for (var j = 0; j < fixedEls.length; j++) {
    fixedEls[j].hidden = !isFixed;
  }

  setActiveRobustnessTab(state.backtestRobustnessTab);
}

export function buildRobustnessConfig() {
  var strat = dom.backtestStrategySelect ? dom.backtestStrategySelect.value : "BULL_PUT_SPREAD";
  var rollingHorizon = isWheelOrCcStrategy(strat) ? optionalInt(dom.btRobRollingHorizon) : null;
  var sensX = dom.btRobSensX ? dom.btRobSensX.value : "profit_target_pct";
  var sensY = dom.btRobSensY ? dom.btRobSensY.value : "stop_loss_pct";
  var sensObj = dom.btRobSensObj ? dom.btRobSensObj.value : "expectancy_dollars";
  var sensDensity = dom.btRobSensDensity ? parseInt(dom.btRobSensDensity.value, 10) : 5;
  var wfoMode = dom.btRobWfoMode ? dom.btRobWfoMode.value : "PURGED_WFO";
  var wfoWindowType = dom.btRobWfoWindowType ? dom.btRobWfoWindowType.value : "ROLLING";
  var wfoIsBars = optionalInt(dom.btRobWfoIsBars) || 120;
  var wfoOosBars = optionalInt(dom.btRobWfoOosBars) || 40;
  var wfoEmbargo = optionalInt(dom.btRobWfoEmbargo);
  var wfoFixedBars = optionalInt(dom.btRobWfoFixedBars) || 40;
  var wfoObj = dom.btRobWfoObj ? dom.btRobWfoObj.value : "expectancy_dollars";
  var mcPaths = optionalInt(dom.btRobMcPaths) || 2000;
  var mcBlock = optionalInt(dom.btRobMcBlock) || 5;
  var mcHorizon = optionalInt(dom.btRobMcHorizon);
  var mcRuin = optionalFloat(dom.btRobMcRuin);
  var mcSeed = optionalInt(dom.btRobMcSeed);

  return {
    cohorts: {
      rolling_horizon_bars: rollingHorizon,
    },
    sensitivity: {
      x_param: sensX || null,
      y_param: sensY || null,
      grid_density: sensDensity === 7 ? 7 : 5,
      objective: sensObj || "expectancy_dollars",
    },
    walk_forward: {
      mode: wfoMode || "PURGED_WFO",
      window_type: wfoWindowType || "ROLLING",
      fixed_window_bars: wfoFixedBars,
      is_bars: wfoIsBars,
      oos_bars: wfoOosBars,
      embargo_bars: wfoEmbargo !== null ? wfoEmbargo : 0,
      objective: wfoObj || "expectancy_dollars",
    },
    monte_carlo: {
      num_paths: mcPaths,
      block_size: mcBlock,
      horizon_bars: mcHorizon,
      ruin_drawdown_pct: mcRuin !== null ? mcRuin : 25.0,
      seed: mcSeed,
      include_trade_bootstrap: dom.btRobMcTradeBoot ? Boolean(dom.btRobMcTradeBoot.checked) : true,
      include_slippage_stress: dom.btRobMcSlippage ? Boolean(dom.btRobMcSlippage.checked) : true,
    },
  };
}

function mergeVerdicts(prevVerdicts, nextVerdicts, requestedSections) {
  var reqSet = {};
  for (var r = 0; r < requestedSections.length; r++) {
    reqSet[requestedSections[r]] = true;
  }
  var bySec = {};
  if (Array.isArray(prevVerdicts)) {
    for (var i = 0; i < prevVerdicts.length; i++) {
      if (prevVerdicts[i] && prevVerdicts[i].section) {
        bySec[prevVerdicts[i].section] = prevVerdicts[i];
      }
    }
  }
  if (Array.isArray(nextVerdicts)) {
    for (var j = 0; j < nextVerdicts.length; j++) {
      var item = nextVerdicts[j];
      if (!item || !item.section) continue;
      if (reqSet[item.section] || !bySec[item.section]) {
        bySec[item.section] = item;
      }
    }
  }
  var merged = [];
  for (var k = 0; k < ROBUSTNESS_SECTIONS.length; k++) {
    var sec = ROBUSTNESS_SECTIONS[k];
    if (bySec[sec]) merged.push(bySec[sec]);
  }
  return merged;
}

function computeBaseConfigSig(ticker, startDate, endDate, sharedCfg) {
  return JSON.stringify({
    ticker: String(ticker || "").trim().toUpperCase(),
    start_date: startDate || null,
    end_date: endDate || null,
    config: sharedCfg || {},
  });
}

function mergeRobustnessResponse(prevResult, apiBody, requestedSections, baseConfigSig) {
  var nextRes = apiBody.result;
  var sigMatches = !baseConfigSig || (prevResult && prevResult.base_config_sig === baseConfigSig);
  var canMerge =
    prevResult &&
    requestedSections &&
    requestedSections.length < ROBUSTNESS_SECTIONS.length &&
    prevResult.ticker === nextRes.ticker &&
    prevResult.strategy_kind === nextRes.strategy_kind &&
    sigMatches;

  if (!canMerge) {
    var reqOnly = {};
    if (Array.isArray(requestedSections)) {
      for (var r = 0; r < requestedSections.length; r++) {
        reqOnly[requestedSections[r]] = true;
      }
    }
    var partialReset =
      Array.isArray(requestedSections) && requestedSections.length < ROBUSTNESS_SECTIONS.length;
    var cleanVerdicts = Array.isArray(nextRes.verdicts) ? nextRes.verdicts.slice() : [];
    if (partialReset) {
      var byV = {};
      for (var vi = 0; vi < cleanVerdicts.length; vi++) {
        if (cleanVerdicts[vi] && cleanVerdicts[vi].section) {
          byV[cleanVerdicts[vi].section] = cleanVerdicts[vi];
        }
      }
      cleanVerdicts = ROBUSTNESS_SECTIONS.map(function (sec) {
        if (reqOnly[sec] && byV[sec]) return byV[sec];
        return {
          section: sec,
          status: "SKIPPED",
          headline: "Stale / not yet re-run",
          detail: "Base parameters changed \u2014 re-run this section or Full Audit.",
        };
      });
    }
    return Object.assign({}, nextRes, {
      cohorts: partialReset && !reqOnly.COHORTS ? null : nextRes.cohorts,
      sensitivity: partialReset && !reqOnly.SENSITIVITY ? null : nextRes.sensitivity,
      walk_forward: partialReset && !reqOnly.WALK_FORWARD ? null : nextRes.walk_forward,
      monte_carlo: partialReset && !reqOnly.MONTE_CARLO ? null : nextRes.monte_carlo,
      verdicts: cleanVerdicts,
      base_config_sig: baseConfigSig || null,
      cache_hit: Boolean(apiBody.cache_hit),
      daily_bar_count: apiBody.daily_bar_count,
      history_snapshot_count: apiBody.history_snapshot_count,
      api_warnings: Array.isArray(apiBody.warnings) ? apiBody.warnings : [],
    });
  }

  var reqSet = {};
  for (var i = 0; i < requestedSections.length; i++) {
    reqSet[requestedSections[i]] = true;
  }
  return Object.assign({}, prevResult, {
    base_config_sig: baseConfigSig || prevResult.base_config_sig || null,
    total_bars: nextRes.total_bars,
    data_sufficiency_tier: nextRes.data_sufficiency_tier,
    compute_ms: nextRes.compute_ms,
    notes: nextRes.notes,
    cohorts: reqSet.COHORTS ? nextRes.cohorts : prevResult.cohorts,
    sensitivity: reqSet.SENSITIVITY ? nextRes.sensitivity : prevResult.sensitivity,
    walk_forward: reqSet.WALK_FORWARD ? nextRes.walk_forward : prevResult.walk_forward,
    monte_carlo: reqSet.MONTE_CARLO ? nextRes.monte_carlo : prevResult.monte_carlo,
    verdicts: mergeVerdicts(prevResult.verdicts, nextRes.verdicts, requestedSections),
    cache_hit: Boolean(apiBody.cache_hit),
    daily_bar_count: apiBody.daily_bar_count,
    history_snapshot_count: apiBody.history_snapshot_count,
    api_warnings: Array.isArray(apiBody.warnings) ? apiBody.warnings : [],
  });
}

function verdictPillClass(status) {
  if (status === "PASS") return "bt-rob-verdict--pass";
  if (status === "WARN") return "bt-rob-verdict--warn";
  if (status === "FAIL") return "bt-rob-verdict--fail";
  return "bt-rob-verdict--skipped";
}

function renderVerdictStrip(result) {
  if (!dom.btRobVerdicts) return;
  var bySec = {};
  var verdicts = result && Array.isArray(result.verdicts) ? result.verdicts : [];
  for (var i = 0; i < verdicts.length; i++) {
    if (verdicts[i] && verdicts[i].section) {
      bySec[verdicts[i].section] = verdicts[i];
    }
  }
  var html = "";
  for (var j = 0; j < ROBUSTNESS_SECTIONS.length; j++) {
    var sec = ROBUSTNESS_SECTIONS[j];
    var v = bySec[sec] || {
      section: sec,
      status: "SKIPPED",
      headline: "Not yet evaluated",
      detail: "Run audit to evaluate this section.",
    };
    var activeCls = state.backtestRobustnessTab === sec ? " is-active" : "";
    html +=
      '<button type="button" class="bt-rob-verdict-card' +
      activeCls +
      '" data-rob-verdict="' +
      sec +
      '">' +
      '<span class="bt-rob-verdict-head">' +
      '<span class="bt-rob-verdict-name">' +
      escapeHtml(SECTION_TITLES[sec] || sec) +
      "</span>" +
      '<span class="bt-rob-verdict-pill ' +
      verdictPillClass(v.status) +
      '">' +
      escapeHtml(v.status) +
      "</span>" +
      "</span>" +
      '<span class="bt-rob-verdict-headline" title="' +
      escapeHtml(v.headline) +
      '">' +
      escapeHtml(v.headline) +
      "</span>" +
      '<span class="bt-rob-verdict-detail">' +
      escapeHtml(v.detail) +
      "</span>" +
      "</button>";
  }
  dom.btRobVerdicts.innerHTML = html;
}

function renderHeaderMeta(result) {
  if (dom.btRobDepthBadge) {
    if (!result) {
      dom.btRobDepthBadge.className = "badge badge--muted";
      dom.btRobDepthBadge.textContent = "READY";
    } else {
      var tier = result.data_sufficiency_tier || "LIMITED";
      var tierCls =
        tier === "HIGH" ? "badge--online" : tier === "MODERATE" ? "badge--info" : "badge--warn";
      dom.btRobDepthBadge.className = "badge " + tierCls;
      dom.btRobDepthBadge.textContent =
        tier + " DEPTH \u2022 " + String(result.total_bars || 0) + " BARS";
    }
  }
  if (dom.btRobTiming) {
    if (!result) {
      dom.btRobTiming.textContent = "Zero-IBKR cached daily-bar audit";
    } else {
      var cacheTag = result.cache_hit ? "Cache Hit" : "Fresh Archive Load";
      dom.btRobTiming.textContent =
        result.ticker +
        " \u2022 " +
        Math.round(Number(result.compute_ms || 0)) +
        " ms \u2022 " +
        cacheTag;
    }
  }
}

function renderCohortsSection(cohorts, t) {
  if (!cohorts) {
    setChartTitleEl(dom.btRobCohortsChartTitle, DEFAULT_CHART_TITLES.cohorts);
    if (dom.btRobCohortsKpis) dom.btRobCohortsKpis.innerHTML = "";
    purgeRobPlaceholder(
      dom.btRobCohortsChart,
      "Click <strong>Run Full Audit</strong> or <strong>Re-run Cohorts</strong> to evaluate daily cohort equity &amp; weekday invariance."
    );
    if (dom.btRobCohortsTable) {
      var emptyBody = dom.btRobCohortsTable.querySelector("tbody");
      if (emptyBody) {
        emptyBody.innerHTML =
          '<tr><td colspan="8" class="text-left">Run audit to inspect Mon\u2013Fri weekday stability and rolling start cohorts.</td></tr>';
      }
    }
    return;
  }

  if (dom.btRobCohortsKpis) {
    var expCls =
      cohorts.expectancy_dollars === null || cohorts.expectancy_dollars === undefined
        ? ""
        : cohorts.expectancy_dollars >= 0
          ? "is-pos"
          : "is-neg";
    var retCls = cohorts.portfolio_total_return_pct >= 0 ? "is-pos" : "is-neg";
    var kpis = [
      kpiCardHtml(
        "Closed Cohorts",
        String(cohorts.closed_cohorts) + " / " + String(cohorts.entered_cohorts),
        cohorts.open_cohorts + " open \u2022 " + cohorts.avg_concurrent_positions.toFixed(1) + " avg conc.",
        "",
        "Number of overlapping entry cohorts that reached exit vs. total cohorts entered across all trading days."
      ),
      kpiCardHtml(
        "Cohort Win Rate",
        fmtPct(cohorts.win_rate_pct, false),
        "PF " + fmtNumOpt(cohorts.profit_factor, 2),
        "",
        "Percentage of closed cohorts with positive net P&L; PF is gross wins divided by gross losses."
      ),
      kpiCardHtml(
        "Cohort Expectancy",
        signedMoney(cohorts.expectancy_dollars),
        "Avg RoR " + fmtPct(cohorts.avg_return_on_risk_pct, true),
        expCls,
        "Mean net P&L per closed cohort across all entry days, removing entry-weekday timing luck."
      ),
      kpiCardHtml(
        "All-Days Return",
        fmtPct(cohorts.portfolio_total_return_pct, true),
        signedMoney(cohorts.total_net_pnl_dollars),
        retCls,
        "Total return on initial capital when averaging/combining all daily entry cohorts."
      ),
      kpiCardHtml(
        "Portfolio MaxDD",
        fmtPct(-Math.abs(Number(cohorts.portfolio_max_drawdown_pct || 0)), false),
        "Sharpe " + fmtNumOpt(cohorts.portfolio_sharpe, 2),
        "is-neg",
        "Worst peak-to-trough drawdown across the combined all-days equity curve; Sharpe is annualized daily return / volatility."
      ),
    ];
    if (cohorts.mode === "ROLLING_START_STATEFUL" && cohorts.rolling_return_p50_pct !== null) {
      kpis.push(
        kpiCardHtml(
          "Rolling Median Ret",
          fmtPct(cohorts.rolling_return_p50_pct, true),
          "P10 " + fmtPct(cohorts.rolling_return_p10_pct, true) + " / P90 " + fmtPct(cohorts.rolling_return_p90_pct, true),
          cohorts.rolling_return_p50_pct >= 0 ? "is-pos" : "is-neg",
          "Median horizon return across staggered rolling start dates for stateful Covered Call / Wheel cycles."
        )
      );
    } else {
      kpis.push(
        kpiCardHtml(
          "Weekday Skew",
          cohorts.weekday_skew_detected ? "DETECTED" : "NONE",
          cohorts.weekday_skew_detected ? "Check Mon\u2013Fri table" : "Entry day invariant",
          cohorts.weekday_skew_detected ? "is-warn" : "is-pos",
          "Flags whether strategy performance differs sharply between Mon\u2013Fri entry weekdays (timing luck risk)."
        )
      );
    }
    dom.btRobCohortsKpis.innerHTML = kpis.join("");
  }

  if (dom.btRobCohortsChart) {
    var curve = Array.isArray(cohorts.equity_curve) ? cohorts.equity_curve : [];
    var rolling = Array.isArray(cohorts.rolling_cohorts) ? cohorts.rolling_cohorts : [];
    if (cohorts.mode === "ROLLING_START_STATEFUL" && rolling.length) {
      var rDates = [];
      var rRets = [];
      var rColors = [];
      var rHover = [];
      for (var i = 0; i < rolling.length; i++) {
        var rc = rolling[i];
        rDates.push(rc.start_date);
        rRets.push(Number(rc.total_return_pct));
        rColors.push(rc.total_return_pct >= 0 ? t.call : t.put);
        rHover.push(
          "<b>Start " +
            rc.start_date +
            " \u2192 " +
            rc.end_date +
            "</b><br>Return: " +
            fmtPct(rc.total_return_pct, true) +
            "<br>Net P&L: " +
            signedMoney(rc.net_pnl_dollars) +
            "<br>MaxDD: " +
            fmtPct(rc.max_drawdown_pct, false) +
            "<br>Trades: " +
            rc.trade_count
        );
      }
      setChartTitleEl(
        dom.btRobCohortsChartTitle,
        "Rolling Start-Date Cohort Returns (" + rolling.length + " starts)"
      );
      var rollingLayout = baseLayout(t, "Cohort Start Date", "Horizon Return (%)", []);
      rollingLayout.xaxis.type = "date";
      rollingLayout.showlegend = false;
      rollingLayout.margin = { t: 16, r: 24, b: 42, l: 58 };
      if (cohorts.rolling_return_p50_pct !== null && cohorts.rolling_return_p50_pct !== undefined) {
        rollingLayout.shapes.push({
          type: "line",
          xref: "paper",
          x0: 0,
          x1: 1,
          y0: Number(cohorts.rolling_return_p50_pct),
          y1: Number(cohorts.rolling_return_p50_pct),
          line: { color: t.accentFocus, width: 1.6, dash: "dash" },
        });
      }
      plotRobChart(
        dom.btRobCohortsChart,
        [
          {
            type: "bar",
            name: "Cohort Return %",
            x: rDates,
            y: rRets,
            marker: { color: rColors },
            text: rHover,
            hoverinfo: "text",
          },
        ],
        rollingLayout
      );
    } else if (curve.length) {
      var xDates = [];
      var eqY = [];
      var ddY = [];
      var maxAbsDd = 0;
      for (var c = 0; c < curve.length; c++) {
        xDates.push(curve[c].market_date);
        eqY.push(Number(curve[c].equity_dollars));
        var ddVal = -Math.abs(Number(curve[c].drawdown_pct || 0));
        ddY.push(ddVal);
        maxAbsDd = Math.max(maxAbsDd, Math.abs(ddVal));
      }
      setChartTitleEl(dom.btRobCohortsChartTitle, DEFAULT_CHART_TITLES.cohorts);
      var cLayout = baseLayout(t, "Trading Date", "All-Days Portfolio Equity ($)", []);
      cLayout.xaxis.type = "date";
      cLayout.margin = { t: 36, r: 58, b: 42, l: 64 };
      cLayout.legend = legendTopConfig(t);
      cLayout.yaxis2 = {
        title: { text: "Drawdown %", font: { size: 11, color: t.muted } },
        overlaying: "y",
        side: "right",
        showgrid: false,
        range: [-Math.max(5, maxAbsDd * 3.2), 0],
        tickfont: { color: t.muted },
      };
      plotRobChart(
        dom.btRobCohortsChart,
        [
          {
            type: "bar",
            name: "Drawdown %",
            x: xDates,
            y: ddY,
            marker: { color: t.put, opacity: 0.35 },
            hovertemplate: "<b>%{x}</b><br>Drawdown: %{y:.2f}%<extra></extra>",
            yaxis: "y2",
          },
          {
            type: "scatter",
            mode: "lines",
            name: "All-Days Equity ($)",
            x: xDates,
            y: eqY,
            line: { color: t.call, width: 2.3 },
            hovertemplate: "<b>%{x}</b><br>Equity: $%{y:,.2f}<extra></extra>",
            yaxis: "y",
          },
        ],
        cLayout
      );
    } else {
      setChartTitleEl(dom.btRobCohortsChartTitle, DEFAULT_CHART_TITLES.cohorts);
      purgeRobPlaceholder(dom.btRobCohortsChart, "No equity curve points available for the selected date window.");
    }
  }

  if (dom.btRobCohortsTable) {
    var tbody = dom.btRobCohortsTable.querySelector("tbody");
    var rows = Array.isArray(cohorts.weekday_stability) ? cohorts.weekday_stability : [];
    if (tbody) {
      if (!rows.length) {
        tbody.innerHTML =
          '<tr><td colspan="8" class="text-left">No weekday cohort trades closed in this sample.</td></tr>';
      } else {
        tbody.innerHTML = rows
          .map(function (r) {
            var pnlCls = r.total_net_pnl_dollars >= 0 ? "is-pos" : "is-neg";
            var eCls =
              r.expectancy_dollars === null || r.expectancy_dollars === undefined
                ? ""
                : r.expectancy_dollars >= 0
                  ? "is-pos"
                  : "is-neg";
            var warnCls = cohorts.weekday_skew_detected && r.expectancy_dollars !== null && r.expectancy_dollars < 0 ? ' class="bt-rob-row-warn"' : "";
            return (
              "<tr" +
              warnCls +
              ">" +
              '<td class="text-left">' +
              escapeHtml(r.weekday_name) +
              "</td>" +
              "<td>" +
              escapeHtml(r.attempted_entries) +
              "</td>" +
              "<td>" +
              escapeHtml(r.closed_trades) +
              "</td>" +
              "<td>" +
              escapeHtml(fmtPct(r.win_rate_pct, false)) +
              "</td>" +
              '<td class="backtest-pnl ' +
              eCls +
              '">' +
              escapeHtml(signedMoney(r.expectancy_dollars)) +
              "</td>" +
              "<td>" +
              escapeHtml(fmtPct(r.avg_return_on_risk_pct, true)) +
              "</td>" +
              "<td>" +
              escapeHtml(fmtNumOpt(r.profit_factor, 2)) +
              "</td>" +
              '<td class="backtest-pnl ' +
              pnlCls +
              '">' +
              escapeHtml(signedMoney(r.total_net_pnl_dollars)) +
              "</td>" +
              "</tr>"
            );
          })
          .join("");
      }
    }
  }
}

function updateSensitivityInspector(cell) {
  state.backtestRobustnessSelectedCell = cell || null;
  if (dom.btRobApplyCellBtn) {
    dom.btRobApplyCellBtn.disabled = !cell;
  }
  if (!dom.btRobSensCellInfo) return;
  if (!cell) {
    dom.btRobSensCellInfo.textContent =
      "Click any heatmap cell to inspect metrics and apply parameters to Systematic Study.";
    return;
  }
  var xLbl = SENSITIVITY_PARAM_LABELS[cell.x_param] || cell.x_param;
  var yLbl = SENSITIVITY_PARAM_LABELS[cell.y_param] || cell.y_param;
  dom.btRobSensCellInfo.textContent =
    xLbl +
    ": " +
    cell.x_label +
    " \u2022 " +
    yLbl +
    ": " +
    cell.y_label +
    " \u2022 Trades: " +
    cell.closed_trades +
    " \u2022 WR: " +
    fmtPct(cell.win_rate_pct, false) +
    " \u2022 Exp: " +
    signedMoney(cell.expectancy_dollars) +
    " \u2022 MaxDD: " +
    fmtPct(cell.max_drawdown_pct, false);
}

function parseCssColorRgb(colorStr) {
  var s = String(colorStr || "").trim();
  var hex3 = s.match(/^#([0-9a-f]{3})$/i);
  if (hex3) {
    var h = hex3[1];
    return [
      parseInt(h.charAt(0) + h.charAt(0), 16),
      parseInt(h.charAt(1) + h.charAt(1), 16),
      parseInt(h.charAt(2) + h.charAt(2), 16),
    ];
  }
  var hex6 = s.match(/^#([0-9a-f]{6})$/i);
  if (hex6) {
    var v = hex6[1];
    return [
      parseInt(v.slice(0, 2), 16),
      parseInt(v.slice(2, 4), 16),
      parseInt(v.slice(4, 6), 16),
    ];
  }
  var rgbM = s.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
  if (rgbM) {
    return [Number(rgbM[1]), Number(rgbM[2]), Number(rgbM[3])];
  }
  return null;
}

function relativeLuminance(rgb) {
  function lin(c) {
    var s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  }
  return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2]);
}

function contrastRatio(l1, l2) {
  var hi = Math.max(l1, l2);
  var lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

function interpolateRgb(rgbA, rgbB, frac) {
  var f = Math.max(0, Math.min(1, frac));
  return [
    Math.round(rgbA[0] + (rgbB[0] - rgbA[0]) * f),
    Math.round(rgbA[1] + (rgbB[1] - rgbA[1]) * f),
    Math.round(rgbA[2] + (rgbB[2] - rgbA[2]) * f),
  ];
}

export function pickHeatmapTextColor(val, zmin, zmax, t) {
  if (val === null || val === undefined || !Number.isFinite(Number(val))) {
    return t.ink;
  }
  var rgbLow = parseCssColorRgb(t.heatLow);
  var rgbMid = parseCssColorRgb(t.heatMid);
  var rgbHigh = parseCssColorRgb(t.heatHigh);
  var rgbInk = parseCssColorRgb(t.ink);
  var rgbBg = parseCssColorRgb(t.bg);
  if (!rgbLow || !rgbMid || !rgbHigh || !rgbInk || !rgbBg) {
    return t.ink;
  }
  var pos = zmax > zmin ? Math.max(0, Math.min(1, (Number(val) - zmin) / (zmax - zmin))) : 0.5;
  var cellRgb =
    pos <= 0.5
      ? interpolateRgb(rgbLow, rgbMid, pos / 0.5)
      : interpolateRgb(rgbMid, rgbHigh, (pos - 0.5) / 0.5);
  var lCell = relativeLuminance(cellRgb);
  var crInk = contrastRatio(lCell, relativeLuminance(rgbInk));
  var crBg = contrastRatio(lCell, relativeLuminance(rgbBg));
  return crBg > crInk ? t.bg : t.ink;
}

function renderSensitivitySection(sens, t) {
  if (!sens) {
    setChartTitleEl(dom.btRobSensChartTitle, DEFAULT_CHART_TITLES.sensitivity);
    if (dom.btRobSensKpis) dom.btRobSensKpis.innerHTML = "";
    purgeRobPlaceholder(
      dom.btRobSensChart,
      "Click <strong>Run Full Audit</strong> or <strong>Re-run Surface</strong> to render the 2D parameter plateau heatmap."
    );
    updateSensitivityInspector(null);
    return;
  }

  if (dom.btRobSensKpis) {
    var scoreCls =
      sens.plateau_stability_score >= 60
        ? "is-pos"
        : sens.plateau_stability_score >= 35
          ? "is-warn"
          : "is-neg";
    var optText = sens.optimal_cell
      ? sens.optimal_cell.x_label + " \u00D7 " + sens.optimal_cell.y_label
      : "\u2014";
    var optSub = sens.optimal_cell
      ? fmtObjectiveValue(sens.objective, sens.optimal_cell.objective_value)
      : "No valid optimal cell";
    var baseSub = sens.baseline_cell
      ? sens.baseline_cell.x_label + " \u00D7 " + sens.baseline_cell.y_label
      : "\u2014";
    dom.btRobSensKpis.innerHTML = [
      kpiCardHtml(
        "Plateau Score",
        sens.plateau_stability_score.toFixed(0) + " / 100",
        sens.valid_cells_count + " / " + sens.total_cells_count + " valid cells",
        scoreCls,
        "Measures neighborhood consistency around the top cell (0-100). Higher scores (>= 60) indicate a broad, resilient parameter plateau."
      ),
      kpiCardHtml(
        "Profitable Grid",
        fmtPct(sens.profitable_cells_pct, false),
        "Baseline: " + baseSub,
        sens.profitable_cells_pct >= 50 ? "is-pos" : "is-warn",
        "Percentage of valid parameter combinations that produced positive net expectancy."
      ),
      kpiCardHtml(
        "Deflated Sharpe (DSR)",
        fmtNumOpt(sens.deflated_sharpe_ratio, 2),
        sens.multiple_testing_warning ? "Multiple-testing warning" : "Haircut-adjusted",
        sens.multiple_testing_warning ? "is-warn" : "",
        "Bailey & Lopez de Prado Sharpe haircut accounting for multiple testing across the 25 or 49 grid trials."
      ),
      kpiCardHtml(
        "Optimal Cell",
        optText,
        optSub,
        "",
        "Best 2D parameter pair ranked by smoothed neighborhood score to avoid isolated single-cell spikes."
      ),
    ].join("");
  }

  if (dom.btRobSensChart) {
    var cells = Array.isArray(sens.cells) ? sens.cells : [];
    if (!cells.length) {
      setChartTitleEl(dom.btRobSensChartTitle, DEFAULT_CHART_TITLES.sensitivity);
      purgeRobPlaceholder(dom.btRobSensChart, "No sensitivity cells returned.");
      updateSensitivityInspector(null);
      return;
    }

    var nx = sens.x_values.length;
    var ny = sens.y_values.length;
    var xIndices = [];
    for (var xi = 0; xi < nx; xi++) xIndices.push(xi);
    var yIndices = [];
    for (var yi0 = 0; yi0 < ny; yi0++) yIndices.push(yi0);
    var xLabels = [];
    var yLabels = [];
    var zMatrix = [];
    var hoverMatrix = [];
    var cellLookup = {};
    var numericVals = [];

    for (var yi = 0; yi < ny; yi++) {
      zMatrix.push(new Array(nx).fill(null));
      hoverMatrix.push(new Array(nx).fill(""));
    }

    for (var i = 0; i < cells.length; i++) {
      var c = cells[i];
      if (!xLabels[c.x_index]) xLabels[c.x_index] = c.x_label;
      if (!yLabels[c.y_index]) yLabels[c.y_index] = c.y_label;
      zMatrix[c.y_index][c.x_index] = c.objective_value;
      if (
        c.objective_value !== null &&
        c.objective_value !== undefined &&
        Number.isFinite(Number(c.objective_value))
      ) {
        numericVals.push(Number(c.objective_value));
      }
      var valStr = fmtObjectiveValue(sens.objective, c.objective_value);
      var tag = c.is_baseline ? " [BASE]" : c.is_optimal ? " [OPT]" : "";
      hoverMatrix[c.y_index][c.x_index] =
        "<b>" +
        (SENSITIVITY_PARAM_LABELS[sens.x_param] || sens.x_param) +
        ": " +
        c.x_label +
        " \u2022 " +
        (SENSITIVITY_PARAM_LABELS[sens.y_param] || sens.y_param) +
        ": " +
        c.y_label +
        "</b>" +
        tag +
        "<br>Objective (" +
        (OBJECTIVE_LABELS[sens.objective] || sens.objective) +
        "): " +
        valStr +
        "<br>Smoothed Score: " +
        fmtNumOpt(c.smoothed_score, 2) +
        "<br>Closed Trades: " +
        c.closed_trades +
        "<br>Win Rate: " +
        fmtPct(c.win_rate_pct, false) +
        "<br>Expectancy: " +
        signedMoney(c.expectancy_dollars) +
        "<br>MaxDD: " +
        fmtPct(c.max_drawdown_pct, false);
      cellLookup[c.x_index + ":" + c.y_index] = c;
    }
    for (var lx = 0; lx < nx; lx++) {
      if (xLabels[lx] === undefined) xLabels[lx] = String(sens.x_values[lx]);
    }
    for (var ly = 0; ly < ny; ly++) {
      if (yLabels[ly] === undefined) yLabels[ly] = String(sens.y_values[ly]);
    }

    var signedObj =
      sens.objective !== "win_rate_pct" && sens.objective !== "profit_factor";
    var minZ = numericVals.length ? Math.min.apply(null, numericVals) : 0;
    var maxZ = numericVals.length ? Math.max.apply(null, numericVals) : 1;
    var zmin;
    var zmax;
    if (signedObj) {
      var maxAbs = numericVals.length ? Math.max(Math.abs(minZ), Math.abs(maxZ)) : 1;
      if (maxAbs === 0) maxAbs = 1;
      zmin = -maxAbs;
      zmax = maxAbs;
    } else {
      zmin = minZ;
      zmax = maxZ;
      if (zmin === zmax) {
        zmin = zmin - 1;
        zmax = zmax + 1;
      }
    }

    var annotations = [];
    for (var ai = 0; ai < cells.length; ai++) {
      var ac = cells[ai];
      var aValStr = fmtObjectiveValue(sens.objective, ac.objective_value);
      var aBadge = "";
      if (ac.is_optimal && ac.is_baseline) {
        aBadge = "<br><b>\u2605 OPT \u2022 BASE</b>";
      } else if (ac.is_optimal) {
        aBadge = "<br><b>\u2605 OPT</b>";
      } else if (ac.is_baseline) {
        aBadge = "<br><b>BASE</b>";
      }
      var annText = (ac.is_optimal || ac.is_baseline ? "<b>" + aValStr + "</b>" : aValStr) + aBadge;
      annotations.push({
        x: ac.x_index,
        y: ac.y_index,
        xref: "x",
        yref: "y",
        text: annText,
        showarrow: false,
        align: "center",
        captureevents: false,
        font: {
          family: t.fontMono,
          size: 10,
          color: pickHeatmapTextColor(ac.objective_value, zmin, zmax, t),
        },
      });
    }

    var heatmapTrace = {
      type: "heatmap",
      x: xIndices,
      y: yIndices,
      z: zMatrix,
      zmin: zmin,
      zmax: zmax,
      texttemplate: undefined,
      customdata: hoverMatrix,
      hovertemplate: "%{customdata}<extra></extra>",
      colorscale: [
        [0, t.heatLow],
        [0.5, t.heatMid],
        [1, t.heatHigh],
      ],
      colorbar: {
        title: { text: OBJECTIVE_LABELS[sens.objective] || sens.objective, font: { size: 10, color: t.ink } },
        tickfont: { size: 10, color: t.ink, family: t.fontMono },
      },
      xgap: 2,
      ygap: 2,
    };
    if (signedObj) {
      heatmapTrace.zmid = 0;
    }

    var traces = [heatmapTrace];
    if (sens.baseline_cell) {
      traces.push({
        type: "scatter",
        mode: "markers",
        name: "Baseline",
        x: [sens.baseline_cell.x_index],
        y: [sens.baseline_cell.y_index],
        marker: { symbol: "square-open", size: 18, color: t.ink, line: { width: 2, color: t.ink } },
        hoverinfo: "skip",
      });
    }
    if (sens.optimal_cell) {
      traces.push({
        type: "scatter",
        mode: "markers",
        name: "Optimal",
        x: [sens.optimal_cell.x_index],
        y: [sens.optimal_cell.y_index],
        marker: { symbol: "diamond-open", size: 18, color: t.accentFocus, line: { width: 2, color: t.accentFocus } },
        hoverinfo: "skip",
      });
    }

    setChartTitleEl(
      dom.btRobSensChartTitle,
      "2D Sensitivity Heatmap (" +
        (OBJECTIVE_LABELS[sens.objective] || sens.objective) +
        ")"
    );

    var hLayout = baseLayout(
      t,
      SENSITIVITY_PARAM_LABELS[sens.x_param] || sens.x_param,
      SENSITIVITY_PARAM_LABELS[sens.y_param] || sens.y_param,
      []
    );
    hLayout.hovermode = "closest";
    hLayout.xaxis = Object.assign({}, hLayout.xaxis, {
      type: "linear",
      tickmode: "array",
      tickvals: xIndices,
      ticktext: xLabels,
      range: [-0.5, nx - 0.5],
      showgrid: false,
      zeroline: false,
    });
    hLayout.yaxis = Object.assign({}, hLayout.yaxis, {
      type: "linear",
      tickmode: "array",
      tickvals: yIndices,
      ticktext: yLabels,
      range: [-0.5, ny - 0.5],
      showgrid: false,
      zeroline: false,
    });
    hLayout.margin = { t: 36, r: 92, b: 48, l: 74 };
    hLayout.legend = legendTopConfig(t);
    hLayout.annotations = annotations;

    plotRobChart(dom.btRobSensChart, traces, hLayout);
    dom.btRobSensChart._robSensitivityLookup = {
      x_param: sens.x_param,
      y_param: sens.y_param,
      byIdx: cellLookup,
    };
    if (!sensitivityClickBound && typeof dom.btRobSensChart.on === "function") {
      sensitivityClickBound = true;
      dom.btRobSensChart.on("plotly_click", function (ev) {
        if (!ev || !ev.points || !ev.points.length) return;
        var pt = ev.points[0];
        var meta = dom.btRobSensChart._robSensitivityLookup;
        if (!meta) return;
        var colIdx = null;
        var rowIdx = null;
        if (typeof pt.x === "number" && typeof pt.y === "number") {
          colIdx = Math.round(pt.x);
          rowIdx = Math.round(pt.y);
        } else if (Array.isArray(pt.pointNumber)) {
          rowIdx = pt.pointNumber[0];
          colIdx = pt.pointNumber[1];
        }
        if (rowIdx === null || colIdx === null) return;
        var picked = meta.byIdx[colIdx + ":" + rowIdx];
        if (!picked) return;
        updateSensitivityInspector(
          Object.assign({}, picked, { x_param: meta.x_param, y_param: meta.y_param })
        );
      });
    }
    if (sens.optimal_cell) {
      updateSensitivityInspector(
        Object.assign({}, sens.optimal_cell, { x_param: sens.x_param, y_param: sens.y_param })
      );
    } else {
      updateSensitivityInspector(null);
    }
  }
}

function renderWalkForwardSection(wf, t) {
  if (!wf) {
    setChartTitleEl(dom.btRobWfoChartTitle, DEFAULT_CHART_TITLES.walkforward);
    if (dom.btRobWfoKpis) dom.btRobWfoKpis.innerHTML = "";
    if (dom.btRobWfoWarn) {
      dom.btRobWfoWarn.hidden = true;
      dom.btRobWfoWarn.textContent = "";
    }
    purgeRobPlaceholder(
      dom.btRobWfoChart,
      "Click <strong>Run Full Audit</strong> or <strong>Re-run WFO</strong> to plot stitched out-of-sample vs baseline equity."
    );
    if (dom.btRobWfoTable) {
      var emptyBody = dom.btRobWfoTable.querySelector("tbody");
      if (emptyBody) {
        emptyBody.innerHTML =
          '<tr><td colspan="10" class="text-left">Run audit to inspect walk-forward folds and parameter drift.</td></tr>';
      }
    }
    return;
  }

  if (dom.btRobWfoKpis) {
    var oosCls = wf.oos_total_return_pct >= 0 ? "is-pos" : "is-neg";
    var baseCls = wf.baseline_oos_total_return_pct >= 0 ? "is-pos" : "is-neg";
    dom.btRobWfoKpis.innerHTML = [
      kpiCardHtml(
        "WFE %",
        fmtPct(wf.wfe_pct, false),
        "OOS / IS efficiency",
        wf.wfe_pct !== null && wf.wfe_pct >= 50 ? "is-pos" : "",
        "Walk-Forward Efficiency = annualized Out-of-Sample return divided by In-Sample return. >= 50% suggests parameters generalize."
      ),
      kpiCardHtml(
        "Stitched OOS Ret",
        fmtPct(wf.oos_total_return_pct, true),
        "OOS MaxDD " + fmtPct(-Math.abs(Number(wf.oos_max_drawdown_pct || 0)), false),
        oosCls,
        "Compounded return across unseen Out-of-Sample windows using each fold's IS-selected parameters."
      ),
      kpiCardHtml(
        "Baseline OOS Ret",
        fmtPct(wf.baseline_oos_total_return_pct, true),
        "Fixed config benchmark",
        baseCls,
        "Return of your fixed baseline parameters over the exact same OOS windows for comparison against re-optimized folds."
      ),
      kpiCardHtml(
        "Positive Windows",
        fmtPct(wf.positive_windows_pct, false),
        (Array.isArray(wf.folds) ? wf.folds.length : 0) + " fold(s)",
        wf.positive_windows_pct !== null && wf.positive_windows_pct >= 50 ? "is-pos" : "is-warn",
        "Share of OOS test folds that finished with positive net return."
      ),
      kpiCardHtml(
        "Param Drift",
        fmtNumOpt(wf.parameter_drift_ratio, 2),
        "Fold-to-fold stability",
        "",
        "Normalized jump in selected optimal parameters from fold to fold. Lower values indicate a stable regime."
      ),
    ].join("");
  }

  if (dom.btRobWfoWarn) {
    if (wf.warning_message) {
      dom.btRobWfoWarn.hidden = false;
      dom.btRobWfoWarn.textContent = wf.warning_message;
    } else {
      dom.btRobWfoWarn.hidden = true;
      dom.btRobWfoWarn.textContent = "";
    }
  }

  if (dom.btRobWfoChart) {
    var oosCurve = Array.isArray(wf.stitched_oos_curve) ? wf.stitched_oos_curve : [];
    var baseCurve = Array.isArray(wf.stitched_baseline_curve) ? wf.stitched_baseline_curve : [];
    if (!oosCurve.length) {
      setChartTitleEl(dom.btRobWfoChartTitle, DEFAULT_CHART_TITLES.walkforward);
      purgeRobPlaceholder(
        dom.btRobWfoChart,
        escapeHtml(wf.warning_message || "Insufficient daily bars for walk-forward stitching.")
      );
    } else {
      setChartTitleEl(
        dom.btRobWfoChartTitle,
        "Stitched Out-of-Sample vs Baseline Equity (" + (WFO_MODE_LABELS[wf.mode] || wf.mode) + ")"
      );
      var oosX = oosCurve.map(function (p) {
        return p.market_date;
      });
      var oosY = oosCurve.map(function (p) {
        return Number(p.equity_dollars);
      });
      var traces = [
        {
          type: "scatter",
          mode: "lines",
          name: "Stitched OOS ($)",
          x: oosX,
          y: oosY,
          line: { color: t.call, width: 2.5 },
          hovertemplate: "<b>%{x}</b><br>OOS Equity: $%{y:,.2f}<extra></extra>",
        },
      ];
      if (baseCurve.length) {
        traces.push({
          type: "scatter",
          mode: "lines",
          name: "Baseline OOS ($)",
          x: baseCurve.map(function (p) {
            return p.market_date;
          }),
          y: baseCurve.map(function (p) {
            return Number(p.equity_dollars);
          }),
          line: { color: t.muted, width: 1.8, dash: "dash" },
          hovertemplate: "<b>%{x}</b><br>Baseline Equity: $%{y:,.2f}<extra></extra>",
        });
      }

      var wLayout = baseLayout(t, "OOS Trading Date", "Stitched Equity ($)", []);
      wLayout.xaxis.type = "date";
      wLayout.margin = { t: 36, r: 24, b: 42, l: 64 };
      wLayout.legend = legendTopConfig(t);

      var folds = Array.isArray(wf.folds) ? wf.folds : [];
      for (var f = 0; f < folds.length; f++) {
        var fld = folds[f];
        if (f > 0) {
          wLayout.shapes.push({
            type: "line",
            x0: fld.oos_start_date,
            x1: fld.oos_start_date,
            y0: 0,
            y1: 1,
            yref: "paper",
            line: { color: t.heatZeroLine, width: 1, dash: "dot" },
          });
        }
        wLayout.annotations.push({
          x: fld.oos_start_date,
          y: f % 2 === 0 ? 0.98 : 0.88,
          yshift: 0,
          yref: "paper",
          xanchor: "left",
          yanchor: "top",
          text: "F" + (fld.fold_index + 1) + " " + fld.regime,
          showarrow: false,
          font: { size: 10, color: t.muted, family: t.fontMono },
          bgcolor: t.bg,
          borderpad: 2,
        });
      }
      plotRobChart(dom.btRobWfoChart, traces, wLayout);
    }
  }

  if (dom.btRobWfoTable) {
    var tbody = dom.btRobWfoTable.querySelector("tbody");
    var foldsRows = Array.isArray(wf.folds) ? wf.folds : [];
    if (tbody) {
      if (!foldsRows.length) {
        tbody.innerHTML =
          '<tr><td colspan="10" class="text-left">' +
          escapeHtml(wf.warning_message || "No walk-forward folds generated.") +
          "</td></tr>";
      } else {
        tbody.innerHTML = foldsRows
          .map(function (fld) {
            var pPairs = Object.keys(fld.selected_params || {}).map(function (k) {
              return (SENSITIVITY_PARAM_LABELS[k] || k) + "=" + fld.selected_params[k];
            });
            var oosCls = fld.oos_return_pct >= 0 ? "is-pos" : "is-neg";
            var isCls = fld.is_return_pct >= 0 ? "is-pos" : "is-neg";
            return (
              "<tr>" +
              "<td>#" +
              escapeHtml(fld.fold_index + 1) +
              "</td>" +
              '<td class="text-left">' +
              escapeHtml(fld.is_start_date + " \u2192 " + fld.is_end_date) +
              "</td>" +
              '<td class="text-left">' +
              escapeHtml(fld.oos_start_date + " \u2192 " + fld.oos_end_date) +
              "</td>" +
              '<td class="text-left">' +
              escapeHtml(fld.regime) +
              "</td>" +
              '<td class="text-left">' +
              escapeHtml(pPairs.length ? pPairs.join(", ") : "\u2014") +
              "</td>" +
              '<td class="backtest-pnl ' +
              isCls +
              '">' +
              escapeHtml(fmtPct(fld.is_return_pct, true)) +
              "</td>" +
              '<td class="backtest-pnl ' +
              oosCls +
              '">' +
              escapeHtml(fmtPct(fld.oos_return_pct, true)) +
              "</td>" +
              "<td>" +
              escapeHtml(fmtPct(fld.baseline_oos_return_pct, true)) +
              "</td>" +
              "<td>" +
              escapeHtml(fmtPct(-Math.abs(Number(fld.oos_max_drawdown_pct || 0)), false)) +
              "</td>" +
              "<td>" +
              escapeHtml(fld.oos_trades) +
              "</td>" +
              "</tr>"
            );
          })
          .join("");
      }
    }
  }
}

function renderMonteCarloSection(mc, t) {
  if (!mc) {
    setChartTitleEl(dom.btRobMcFanChartTitle, DEFAULT_CHART_TITLES.mcFan);
    setChartTitleEl(dom.btRobMcDdChartTitle, DEFAULT_CHART_TITLES.mcDd);
    if (dom.btRobMcKpis) dom.btRobMcKpis.innerHTML = "";
    purgeRobPlaceholder(
      dom.btRobMcFanChart,
      "Click <strong>Run Full Audit</strong> or <strong>Re-run MC</strong> to plot P5/P25/P50/P75/P95 equity fan."
    );
    purgeRobPlaceholder(dom.btRobMcDdChart, "Simulated max drawdown distribution will appear here.");
    if (dom.btRobMcSlippageTable) {
      var emptyBody = dom.btRobMcSlippageTable.querySelector("tbody");
      if (emptyBody) {
        emptyBody.innerHTML =
          '<tr><td colspan="8" class="text-left">Run audit to evaluate Mid \u2194 Natural spread fill stress.</td></tr>';
      }
    }
    return;
  }

  if (dom.btRobMcKpis) {
    var slip = mc.slippage_stress;
    var slipVal =
      slip && slip.edge_survives_50pct_slippage !== null && slip.edge_survives_50pct_slippage !== undefined
        ? slip.edge_survives_50pct_slippage
          ? "SURVIVES"
          : "FAILS"
        : "\u2014";
    var slipCls =
      slip && slip.edge_survives_50pct_slippage === true
        ? "is-pos"
        : slip && slip.edge_survives_50pct_slippage === false
          ? "is-neg"
          : "";
    var slipSub =
      slip && slip.blended_expectancy_dollars !== null && slip.blended_expectancy_dollars !== undefined
        ? "50% slip exp " + signedMoney(slip.blended_expectancy_dollars)
        : "No spread cohorts";
    var ciText =
      mc.trade_expectancy_ci_low_dollars !== null && mc.trade_expectancy_ci_high_dollars !== null
        ? signedMoney(mc.trade_expectancy_ci_low_dollars) +
          " .. " +
          signedMoney(mc.trade_expectancy_ci_high_dollars)
        : "\u2014";

    dom.btRobMcKpis.innerHTML = [
      kpiCardHtml(
        "Prob Profit",
        fmtPct(mc.prob_profit_pct, false),
        mc.num_paths + " paths \u2022 block " + mc.block_size,
        mc.prob_profit_pct >= 60 ? "is-pos" : mc.prob_profit_pct >= 45 ? "is-warn" : "is-neg",
        "Fraction of stationary block-bootstrap paths finishing above initial capital."
      ),
      kpiCardHtml(
        "Risk of Ruin",
        fmtPct(mc.risk_of_ruin_pct, false),
        "MaxDD >= " + mc.ruin_drawdown_pct.toFixed(0) + "%",
        mc.risk_of_ruin_pct <= 15 ? "is-pos" : mc.risk_of_ruin_pct <= 35 ? "is-warn" : "is-neg",
        "Share of simulated paths that suffer a drawdown >= your Ruin MaxDD % threshold."
      ),
      kpiCardHtml(
        "Median Return",
        fmtPct(mc.terminal_return_p50_pct, true),
        "P05 " + fmtPct(mc.terminal_return_p05_pct, true) + " / P95 " + fmtPct(mc.terminal_return_p95_pct, true),
        mc.terminal_return_p50_pct >= 0 ? "is-pos" : "is-neg",
        "50th-percentile terminal return across simulated paths, bounded by 5th and 95th percentiles."
      ),
      kpiCardHtml(
        "P95 MaxDD",
        fmtPct(-Math.abs(mc.max_dd_p95_pct), false),
        "P50 " + fmtPct(-Math.abs(mc.max_dd_p50_pct), false),
        "is-neg",
        "95th-percentile worst drawdown across simulated paths (only 5% of scenarios drew down further)."
      ),
      kpiCardHtml(
        "CVaR 5%",
        fmtPct(mc.cvar_5_return_pct, true),
        "Tail terminal return",
        mc.cvar_5_return_pct >= 0 ? "is-pos" : "is-neg",
        "Conditional Value at Risk: average return of the worst 5% tail of simulated paths."
      ),
      kpiCardHtml(
        "Trade Exp 95% CI",
        ciText,
        mc.trade_count + " closed trades",
        "",
        "95% bootstrap confidence interval on per-trade net expectancy."
      ),
      kpiCardHtml(
        "50% Slippage Edge",
        slipVal,
        slipSub,
        slipCls,
        "Checks whether closed-cohort expectancy stays positive when paying 50% of the bid-ask spread on entry and exit."
      ),
    ].join("");
  }

  if (dom.btRobMcFanChart) {
    var fan = mc.fan_series;
    if (fan && Array.isArray(fan.bar_offsets) && fan.bar_offsets.length) {
      setChartTitleEl(dom.btRobMcFanChartTitle, DEFAULT_CHART_TITLES.mcFan);
      var fLayout = baseLayout(t, "Forward Bar Offset", "Simulated Equity ($)", []);
      fLayout.margin = { t: 36, r: 24, b: 42, l: 64 };
      fLayout.legend = legendTopConfig(t);
      var fanTraces = [
        {
          type: "scatter",
          mode: "lines",
          name: "P5",
          x: fan.bar_offsets,
          y: fan.p05,
          line: { color: t.put, width: 1, dash: "dot" },
          hovertemplate: "Bar %{x}<br>P05: $%{y:,.2f}<extra></extra>",
        },
        {
          type: "scatter",
          mode: "lines",
          name: "P5\u2013P95 Band",
          x: fan.bar_offsets,
          y: fan.p95,
          fill: "tonexty",
          fillcolor: t.fanOuter,
          line: { color: t.call, width: 1, dash: "dot" },
          hovertemplate: "Bar %{x}<br>P95: $%{y:,.2f}<extra></extra>",
        },
        {
          type: "scatter",
          mode: "lines",
          name: "P25",
          x: fan.bar_offsets,
          y: fan.p25,
          line: { color: t.muted, width: 0 },
          showlegend: false,
          hoverinfo: "skip",
        },
        {
          type: "scatter",
          mode: "lines",
          name: "P25\u2013P75 Band",
          x: fan.bar_offsets,
          y: fan.p75,
          fill: "tonexty",
          fillcolor: t.fanInner,
          line: { color: t.muted, width: 0 },
          hovertemplate: "Bar %{x}<br>P75: $%{y:,.2f}<extra></extra>",
        },
        {
          type: "scatter",
          mode: "lines",
          name: "Median (P50)",
          x: fan.bar_offsets,
          y: fan.p50,
          line: { color: t.accentFocus, width: 2.5 },
          hovertemplate: "Bar %{x}<br>Median: $%{y:,.2f}<extra></extra>",
        },
      ];
      plotRobChart(dom.btRobMcFanChart, fanTraces, fLayout);
    } else {
      setChartTitleEl(dom.btRobMcFanChartTitle, DEFAULT_CHART_TITLES.mcFan);
      purgeRobPlaceholder(dom.btRobMcFanChart, "Insufficient bars for Monte Carlo fan chart.");
    }
  }

  if (dom.btRobMcDdChart) {
    var hist = Array.isArray(mc.max_dd_histogram) ? mc.max_dd_histogram : [];
    if (hist.length) {
      setChartTitleEl(
        dom.btRobMcDdChartTitle,
        "Simulated Max Drawdown Distribution (Ruin >= " + mc.ruin_drawdown_pct.toFixed(0) + "%)"
      );
      var labels = [];
      var freqs = [];
      var colors = [];
      var hovers = [];
      for (var h = 0; h < hist.length; h++) {
        var bin = hist[h];
        labels.push(bin.bin_start_pct.toFixed(1) + "\u2013" + bin.bin_end_pct.toFixed(1) + "%");
        freqs.push(Number(bin.frequency_pct));
        colors.push(bin.bin_end_pct >= mc.ruin_drawdown_pct ? t.put : t.accentFocus);
        hovers.push(
          "<b>MaxDD " +
            bin.bin_start_pct.toFixed(1) +
            "% \u2013 " +
            bin.bin_end_pct.toFixed(1) +
            "%</b><br>Paths: " +
            bin.count +
            " (" +
            bin.frequency_pct.toFixed(1) +
            "%)"
        );
      }
      var ddLayout = baseLayout(t, "Max Drawdown Bin (%)", "Frequency (%)", []);
      ddLayout.xaxis.type = "category";
      ddLayout.xaxis.tickangle = -30;
      ddLayout.showlegend = false;
      ddLayout.margin = { t: 16, r: 22, b: 64, l: 54 };
      plotRobChart(
        dom.btRobMcDdChart,
        [
          {
            type: "bar",
            name: "Path Frequency %",
            x: labels,
            y: freqs,
            marker: { color: colors },
            text: hovers,
            hoverinfo: "text",
          },
        ],
        ddLayout
      );
    } else {
      setChartTitleEl(dom.btRobMcDdChartTitle, DEFAULT_CHART_TITLES.mcDd);
      purgeRobPlaceholder(dom.btRobMcDdChart, "No drawdown distribution bins generated.");
    }
  }

  if (dom.btRobMcSlippageTable) {
    var tbody = dom.btRobMcSlippageTable.querySelector("tbody");
    var scenarios =
      mc.slippage_stress && Array.isArray(mc.slippage_stress.scenarios)
        ? mc.slippage_stress.scenarios
        : [];
    if (tbody) {
      if (!scenarios.length) {
        tbody.innerHTML =
          '<tr><td colspan="8" class="text-left">No spread slippage stress scenarios generated (requires closed spread cohorts).</td></tr>';
      } else {
        tbody.innerHTML = scenarios
          .map(function (sc) {
            var eCls =
              sc.expectancy_dollars === null || sc.expectancy_dollars === undefined
                ? ""
                : sc.expectancy_dollars >= 0
                  ? "is-pos"
                  : "is-neg";
            var pCls = sc.total_net_pnl_dollars >= 0 ? "is-pos" : "is-neg";
            return (
              "<tr>" +
              '<td class="text-left">' +
              escapeHtml(sc.label) +
              "</td>" +
              "<td>" +
              escapeHtml(Math.round(Number(sc.natural_fraction) * 100) + "%") +
              "</td>" +
              "<td>" +
              escapeHtml(sc.closed_trades) +
              "</td>" +
              "<td>" +
              escapeHtml(fmtPct(sc.win_rate_pct, false)) +
              "</td>" +
              '<td class="backtest-pnl ' +
              eCls +
              '">' +
              escapeHtml(signedMoney(sc.expectancy_dollars)) +
              "</td>" +
              "<td>" +
              escapeHtml(fmtNumOpt(sc.profit_factor, 2)) +
              "</td>" +
              "<td>" +
              escapeHtml(fmtPct(sc.total_return_pct, true)) +
              "</td>" +
              '<td class="backtest-pnl ' +
              pCls +
              '">' +
              escapeHtml(signedMoney(sc.total_net_pnl_dollars)) +
              "</td>" +
              "</tr>"
            );
          })
          .join("");
      }
    }
  }
}

export function renderRobustnessResult(result, theme) {
  var t = theme || themeVars();
  renderHeaderMeta(result);
  renderVerdictStrip(result);
  renderCohortsSection(result ? result.cohorts : null, t);
  renderSensitivitySection(result ? result.sensitivity : null, t);
  renderWalkForwardSection(result ? result.walk_forward : null, t);
  renderMonteCarloSection(result ? result.monte_carlo : null, t);
  resizeVisibleRobustnessCharts();
}

export function rerenderRobustnessChartsOnTheme() {
  if (state.backtestMode === "ROBUSTNESS" && state.backtestRobustnessResult) {
    renderRobustnessResult(state.backtestRobustnessResult, themeVars());
  }
}

export function markRobustnessStaleForTicker(ticker) {
  var sym = String(ticker || state.ticker || "").trim().toUpperCase();
  if (!state.backtestRobustnessResult || state.backtestRobustnessResult.ticker === sym) {
    return false;
  }
  var prevTicker = state.backtestRobustnessResult.ticker || "";
  state.backtestRobustnessResult = null;
  state.backtestRobustnessSelectedCell = null;
  renderRobustnessResult(null, themeVars());
  if (dom.btRobDepthBadge) {
    dom.btRobDepthBadge.className = "badge badge--warn";
    dom.btRobDepthBadge.textContent = "STALE";
  }
  if (dom.btRobTiming) {
    dom.btRobTiming.textContent =
      "Ticker changed (" + prevTicker + " \u2192 " + sym + ") \u2014 run audit";
  }
  return true;
}

export async function runRobustnessAudit(sections) {
  if (state.backtestRobustnessBusy || state.backtestBusy) return;
  var strat = dom.backtestStrategySelect ? dom.backtestStrategySelect.value : "BULL_PUT_SPREAD";
  if (strat === "CUSTOM") {
    setBacktestNotice(
      "warn",
      "Robustness Suite requires a preset strategy (Bull Put, Bull Call, Diagonal, Iron Condor, Covered Call, or Wheel) \u2014 Custom Legs is not supported."
    );
    return;
  }

  var robCfg = buildRobustnessConfig();
  var reqSections = Array.isArray(sections) && sections.length ? sections : ROBUSTNESS_SECTIONS.slice();
  if (
    reqSections.indexOf("SENSITIVITY") >= 0 &&
    robCfg.sensitivity.x_param &&
    robCfg.sensitivity.y_param &&
    robCfg.sensitivity.x_param === robCfg.sensitivity.y_param
  ) {
    setBacktestNotice("warn", "Sensitivity X Axis and Y Axis parameters must differ.");
    return;
  }

  setRobustnessBusy(true);
  setBacktestNotice("", "");
  if (dom.backtestStatusBadge) {
    dom.backtestStatusBadge.className = "badge badge--amber";
    dom.backtestStatusBadge.textContent = "RUNNING...";
  }

  try {
    var startDate =
      dom.backtestStartDate && dom.backtestStartDate.value ? dom.backtestStartDate.value : null;
    var endDate = dom.backtestEndDate && dom.backtestEndDate.value ? dom.backtestEndDate.value : null;
    var sharedCfg = buildSharedBacktestConfig();
    var baseConfigSig = computeBaseConfigSig(state.ticker, startDate, endDate, sharedCfg);
    var payload = {
      ticker: state.ticker,
      start_date: startDate,
      end_date: endDate,
      config: sharedCfg,
      sections: reqSections,
      robustness_config: robCfg,
    };
    var res = await apiJson("/api/backtest/robustness", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok || !res.body || !res.body.result) {
      var errMsg = extractApiErrorMessage(res.body, "Robustness audit failed.");
      setBacktestNotice("error", errMsg);
      if (dom.backtestStatusBadge) {
        dom.backtestStatusBadge.className = "badge badge--warn";
        dom.backtestStatusBadge.textContent = "ERROR";
      }
      return;
    }

    state.backtestRobustnessResult = mergeRobustnessResponse(
      state.backtestRobustnessResult,
      res.body,
      reqSections,
      baseConfigSig
    );

    var combinedWarnings = [];
    if (Array.isArray(res.body.warnings)) {
      combinedWarnings = combinedWarnings.concat(res.body.warnings);
    }
    if (Array.isArray(state.backtestRobustnessResult.notes)) {
      combinedWarnings = combinedWarnings.concat(state.backtestRobustnessResult.notes);
    }
    if (combinedWarnings.length) {
      setBacktestNotice("warn", combinedWarnings.join(" \u2022 "));
    }

    if (dom.backtestStatusBadge) {
      dom.backtestStatusBadge.className = "badge badge--online";
      dom.backtestStatusBadge.textContent = "AUDIT READY";
    }
    renderRobustnessResult(state.backtestRobustnessResult, themeVars());
  } finally {
    setRobustnessBusy(false);
  }
}

export function bindRobustnessEvents() {
  if (dom.btRobRunAllBtn) {
    dom.btRobRunAllBtn.addEventListener("click", function () {
      runRobustnessAudit(null);
    });
  }
  if (dom.btRobRerunCohortsBtn) {
    dom.btRobRerunCohortsBtn.addEventListener("click", function () {
      runRobustnessAudit(["COHORTS"]);
    });
  }
  if (dom.btRobRerunSensBtn) {
    dom.btRobRerunSensBtn.addEventListener("click", function () {
      runRobustnessAudit(["SENSITIVITY"]);
    });
  }
  if (dom.btRobRerunWfoBtn) {
    dom.btRobRerunWfoBtn.addEventListener("click", function () {
      runRobustnessAudit(["WALK_FORWARD"]);
    });
  }
  if (dom.btRobRerunMcBtn) {
    dom.btRobRerunMcBtn.addEventListener("click", function () {
      runRobustnessAudit(["MONTE_CARLO"]);
    });
  }
  if (dom.btRobTabs) {
    dom.btRobTabs.addEventListener("click", function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest("[data-rob-tab]") : null;
      if (!btn) return;
      setActiveRobustnessTab(btn.getAttribute("data-rob-tab") || "COHORTS");
    });
  }
  if (dom.btRobVerdicts) {
    dom.btRobVerdicts.addEventListener("click", function (ev) {
      var card = ev.target && ev.target.closest ? ev.target.closest("[data-rob-verdict]") : null;
      if (!card) return;
      setActiveRobustnessTab(card.getAttribute("data-rob-verdict") || "COHORTS");
    });
  }
  if (dom.btRobSensX) {
    dom.btRobSensX.addEventListener("change", function () {
      var curStrat = dom.backtestStrategySelect ? dom.backtestStrategySelect.value : "BULL_PUT_SPREAD";
      syncSensitivityAxisOptions(curStrat, "X");
    });
  }
  if (dom.btRobSensY) {
    dom.btRobSensY.addEventListener("change", function () {
      var curStrat = dom.backtestStrategySelect ? dom.backtestStrategySelect.value : "BULL_PUT_SPREAD";
      syncSensitivityAxisOptions(curStrat, "Y");
    });
  }
  if (dom.btRobWfoMode) {
    dom.btRobWfoMode.addEventListener("change", syncRobustnessControlState);
  }
  if (dom.btRobApplyCellBtn) {
    dom.btRobApplyCellBtn.addEventListener("click", function () {
      if (state.backtestRobustnessSelectedCell) {
        applySensitivityCellToSystematic(state.backtestRobustnessSelectedCell);
      }
    });
  }
  syncRobustnessControlState();
}
