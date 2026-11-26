/* IBKR Options Analyzer dashboard — Backtest controls, execution, KPIs & step drill-down. */

import {
  dom,
  state,
} from "./state.js";
import {
  extractApiErrorMessage,
  formatExpiryIso,
  showBanner,
  themeVars,
} from "./helpers.js";
import {
  ensurePanelVisible,
  isPanelVisible,
} from "./panels.js";
import {
  apiJson,
  loadChain,
} from "./network.js";
import {
  renderBacktestChart,
  renderBacktestEmptyPlaceholder,
} from "./backtest_charts.js";
import {
  addCustomBacktestLeg,
  copyStrategyLabLegsToBacktest,
  loadPaperTrades,
  readCustomLegsFromUI,
  removeCustomBacktestLeg,
  renderCustomLegsEditor,
  renderPaperTradesList,
  saveCurrentPaperTrade,
  setBacktestNotice,
  setPaperTradesExpanded,
  stopPaperTrade,
  syncPaperTradesFromLedger,
  togglePaperTradesDrawer,
} from "./backtest_paper.js";
import {
  initBacktestTablesSplitter,
  renderBacktestResult,
  renderTradeLog,
  toggleBacktestTradeExpand,
} from "./backtest_tables.js";
import {
  bindRobustnessEvents,
  markRobustnessStaleForTicker,
  rerenderRobustnessChartsOnTheme,
  resizeVisibleRobustnessCharts,
  runRobustnessAudit,
  syncRobustnessControlState,
} from "./backtest_robustness.js";

function setSegActive(groupEl, attrName, value) {
  if (!groupEl) return;
  var buttons = groupEl.querySelectorAll("button[" + attrName + "]");
  for (var i = 0; i < buttons.length; i++) {
    var btn = buttons[i];
    var active = btn.getAttribute(attrName) === value;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  }
}

export function syncBacktestControlVisibility() {
  var isSingle = state.backtestMode === "SINGLE_TRADE";
  var isRobustness = state.backtestMode === "ROBUSTNESS";
  var source = state.backtestSource;
  var cadence = dom.backtestCadenceSelect ? dom.backtestCadenceSelect.value : "WHEN_FLAT";
  var strat = dom.backtestStrategySelect ? dom.backtestStrategySelect.value : "BULL_PUT_SPREAD";

  if (dom.backtestSingleControls) dom.backtestSingleControls.hidden = !isSingle;

  var sysEls = document.querySelectorAll("[data-bt-systematic]");
  for (var i = 0; i < sysEls.length; i++) {
    sysEls[i].hidden = isSingle;
  }

  var sysOnlyEls = document.querySelectorAll("[data-bt-systematic-only]");
  for (var k = 0; k < sysOnlyEls.length; k++) {
    sysOnlyEls[k].hidden = isSingle || isRobustness;
  }

  var weekdayEl = document.querySelector("[data-bt-weekday]");
  if (weekdayEl) weekdayEl.hidden = isSingle || isRobustness || cadence !== "WEEKLY_DAY";

  var everyNEl = document.querySelector("[data-bt-every-n]");
  if (everyNEl) everyNEl.hidden = isSingle || isRobustness || cadence !== "EVERY_N_DAYS";

  if (dom.backtestCadenceLegacyHint) {
    dom.backtestCadenceLegacyHint.hidden = isSingle || isRobustness || cadence !== "WEEKLY_DAY";
  }

  var entryFiltersEl = document.querySelector("[data-bt-entry-filters]");
  var isWheelOrCc = strat === "COVERED_CALL" || strat === "WHEEL";
  if (entryFiltersEl) {
    entryFiltersEl.hidden = isSingle || !(strat === "BULL_PUT_SPREAD" || isWheelOrCc);
  }
  if (dom.backtestEntryFiltersLabel) {
    dom.backtestEntryFiltersLabel.textContent = isWheelOrCc ? "Credit Entry Filter" : "BPS Entry Filters";
  }
  var minRorWrap = document.querySelector("[data-bt-min-ror-wrap]");
  if (minRorWrap) minRorWrap.hidden = isWheelOrCc;

  if (dom.backtestWheelControls) {
    dom.backtestWheelControls.hidden = !isWheelOrCc || (isSingle && source !== "AUTO_BUILD");
  }
  var strikeMode = dom.backtestStrikeModeSelect
    ? dom.backtestStrikeModeSelect.value
    : "TARGET_DELTA";
  var targetDeltaWrap = document.querySelector("[data-bt-target-delta-wrap]");
  if (targetDeltaWrap) targetDeltaWrap.hidden = strikeMode !== "TARGET_DELTA";
  var ccItmWrap = document.querySelector("[data-bt-cc-itm-wrap]");
  if (ccItmWrap) ccItmWrap.hidden = strat !== "COVERED_CALL";
  if (dom.backtestRollCreditSelect) {
    dom.backtestRollCreditSelect.disabled = !(
      dom.backtestRollToggle && dom.backtestRollToggle.checked
    );
  }

  var entryWrap = document.querySelector("[data-bt-entry-wrap]");
  if (entryWrap) entryWrap.hidden = !isSingle || source === "PAPER_TRADE";

  var endWrap = document.querySelector("[data-bt-end-wrap]");
  if (endWrap) endWrap.hidden = !isSingle;

  var paperWrap = document.querySelector("[data-bt-paper-wrap]");
  if (paperWrap) paperWrap.hidden = !isSingle || source !== "PAPER_TRADE";

  if (dom.backtestLegsBox) {
    dom.backtestLegsBox.hidden =
      isRobustness || !(strat === "CUSTOM" || (isSingle && source === "LEGS"));
  }

  if (dom.backtestKpis) dom.backtestKpis.hidden = isRobustness;
  if (dom.backtestChartToolbar) dom.backtestChartToolbar.hidden = isRobustness;
  if (dom.backtestChartEl) dom.backtestChartEl.hidden = isRobustness;
  if (dom.backtestTablesGrid) dom.backtestTablesGrid.hidden = isRobustness;
  if (dom.backtestAssumptions) dom.backtestAssumptions.hidden = isRobustness;
  if (dom.backtestRobustnessPanel) dom.backtestRobustnessPanel.hidden = !isRobustness;

  if (dom.backtestSavePaperBtn) {
    dom.backtestSavePaperBtn.hidden = isRobustness;
    dom.backtestSavePaperBtn.disabled = isRobustness;
  }
  if (dom.backtestRunBtn) {
    dom.backtestRunBtn.textContent = isRobustness ? "RUN FULL AUDIT" : "RUN BACKTEST";
  }

  var simMode =
    state.backtestResult && state.backtestResult.simulation
      ? state.backtestResult.simulation.mode
      : null;
  var showChartViewToggle = simMode ? simMode === "SYSTEMATIC" : !isSingle;
  if (dom.backtestChartViewGroup) {
    dom.backtestChartViewGroup.hidden = isRobustness || !showChartViewToggle;
  }

  setSegActive(dom.backtestModeGroup, "data-bt-mode", state.backtestMode);
  setSegActive(dom.backtestSourceGroup, "data-bt-source", state.backtestSource);
  setSegActive(dom.backtestPriceBasisGroup, "data-bt-price-basis", state.backtestPriceBasis);
  setSegActive(dom.backtestCloseModeGroup, "data-bt-close-mode", state.backtestCloseMode);
  setSegActive(dom.backtestTpSlBasisGroup, "data-bt-tpsl-basis", state.backtestTpSlBasis);
  setSegActive(dom.backtestChartViewGroup, "data-bt-chart-view", state.backtestChartView);

  syncRobustnessControlState();
  syncCapitalAdequacyHint();
  if (isRobustness) {
    resizeVisibleRobustnessCharts();
  }
}

export function syncCapitalAdequacyHint() {
  if (!dom.backtestCapitalAdequacy) return;
  var strat = dom.backtestStrategySelect ? dom.backtestStrategySelect.value : "BULL_PUT_SPREAD";
  var isSingleNonAuto = state.backtestMode === "SINGLE_TRADE" && state.backtestSource !== "AUTO_BUILD";
  var isWheelOrCc = (strat === "COVERED_CALL" || strat === "WHEEL") && !isSingleNonAuto;
  var spot =
    state.chainData && state.chainData.underlying_price
      ? Number(state.chainData.underlying_price)
      : 0;
  var qty = Math.max(1, parseInt(dom.backtestQty ? dom.backtestQty.value : "1", 10) || 1);
  var initialCap = Number(dom.backtestInitialCapital ? dom.backtestInitialCapital.value : "10000") || 0;
  var requiredNotional = 100 * qty * spot;

  if (!isWheelOrCc || !(spot > 0) || !(initialCap < requiredNotional)) {
    dom.backtestCapitalAdequacy.hidden = true;
    return;
  }

  var suggestedCap = Math.max(100, Math.ceil(requiredNotional / 100) * 100);
  var shareCount = 100 * qty;
  dom.backtestCapitalAdequacy.hidden = false;
  if (dom.backtestCapitalAdequacyText) {
    dom.backtestCapitalAdequacyText.textContent =
      (strat === "WHEEL" ? "Wheel" : "Covered Call") +
      " requires " +
      shareCount +
      " shares @ $" +
      spot.toFixed(2) +
      " (~$" +
      Math.round(requiredNotional).toLocaleString("en-US") +
      " notional), exceeding Initial Capital ($" +
      Math.round(initialCap).toLocaleString("en-US") +
      ").";
  }
  if (dom.backtestMatchCapitalBtn) {
    dom.backtestMatchCapitalBtn.textContent = "Size to $" + suggestedCap.toLocaleString("en-US");
    if (!dom.backtestMatchCapitalBtn.dataset) dom.backtestMatchCapitalBtn.dataset = {};
    dom.backtestMatchCapitalBtn.dataset.suggestedCapital = String(suggestedCap);
  }
}

function renderEntrySelectOptions(entries) {
  if (!dom.backtestEntrySelect) return;
  var prevEntry = dom.backtestEntrySelect.value;
  var prevEnd = dom.backtestEndSelect ? dom.backtestEndSelect.value : "";
  dom.backtestEntrySelect.innerHTML = "";
  if (dom.backtestEndSelect) {
    dom.backtestEndSelect.innerHTML = '<option value="">Latest Available</option>';
  }

  if (!entries || !entries.length) {
    var noneOpt = document.createElement("option");
    noneOpt.value = "";
    noneOpt.textContent = "No archived entries for " + state.ticker;
    dom.backtestEntrySelect.appendChild(noneOpt);
    return;
  }

  for (var i = 0; i < entries.length; i++) {
    var ent = entries[i];
    var covStr =
      ent.quote_coverage !== null && ent.quote_coverage !== undefined
        ? Math.round(Number(ent.quote_coverage) * 100) + "% cov"
        : "n/a cov";
    var opt = document.createElement("option");
    opt.value = ent.snapshot_id;
    opt.textContent =
      (ent.ts_et || ent.snapshot_id) +
      " \u2022 " +
      ent.contract_count +
      "c \u2022 " +
      covStr +
      (ent.usable ? "" : " [flagged]");
    if (ent.snapshot_id === prevEntry || (!prevEntry && ent.snapshot_id === state.snapshotId)) {
      opt.selected = true;
    }
    dom.backtestEntrySelect.appendChild(opt);

    if (dom.backtestEndSelect) {
      var endOpt = document.createElement("option");
      endOpt.value = ent.snapshot_id;
      endOpt.textContent = (ent.ts_et || ent.snapshot_id) + " (" + ent.snapshot_id + ")";
      if (ent.snapshot_id === prevEnd) endOpt.selected = true;
      dom.backtestEndSelect.appendChild(endOpt);
    }
  }
}

export async function backtestOnSnapshotsLoaded(ticker) {
  var sym = String(ticker || state.ticker || "").trim().toUpperCase();
  if (!sym) return;
  if (state.backtestRobustnessResult && state.backtestRobustnessResult.ticker !== sym) {
    markRobustnessStaleForTicker(sym);
  }
  var res = await apiJson("/api/backtest/entries/" + encodeURIComponent(sym));
  if (res.ok && res.body) {
    state.backtestEntries = Array.isArray(res.body.entries) ? res.body.entries : [];
    state.backtestEntriesTicker = sym;
    renderEntrySelectOptions(state.backtestEntries);
  }
  renderPaperTradesList();
  syncCapitalAdequacyHint();
}

function findPaperTradeById(tradeId) {
  var pid = String(tradeId || "").trim();
  if (!pid || !Array.isArray(state.backtestPaperTrades)) return null;
  for (var i = 0; i < state.backtestPaperTrades.length; i++) {
    if (state.backtestPaperTrades[i] && state.backtestPaperTrades[i].id === pid) {
      return state.backtestPaperTrades[i];
    }
  }
  return null;
}

function syncPaperTradeQtyInput(trade) {
  if (!trade || !dom.backtestQty) return;
  dom.backtestQty.value = String(trade.qty || 1);
}

function syncSelectedPaperTradeQty() {
  if (!dom.backtestPaperSelect) return;
  syncPaperTradeQtyInput(findPaperTradeById(dom.backtestPaperSelect.value));
}

function clearBacktestDateAndEndFilters() {
  if (dom.backtestStartDate) dom.backtestStartDate.value = "";
  if (dom.backtestEndDate) dom.backtestEndDate.value = "";
  if (dom.backtestEndSelect) dom.backtestEndSelect.value = "";
}

function optionalNumberInput(el, isInt) {
  if (!el) return null;
  var raw = String(el.value || "").trim();
  if (!raw) return null;
  var n = isInt ? parseInt(raw, 10) : Number(raw);
  return Number.isNaN(n) ? null : n;
}

function optionalFilterNumberInput(el) {
  var n = optionalNumberInput(el, false);
  return n === null || n === 0 ? null : n;
}

function serializeLegsForRequest() {
  return readCustomLegsFromUI().map(function (leg) {
    return {
      side: leg.side,
      right: leg.right,
      expiry: formatExpiryIso(leg.expiry),
      strike: Number(leg.strike),
      qty: Math.max(1, parseInt(leg.qty || 1, 10) || 1),
    };
  });
}

export function buildSharedBacktestConfig() {
  var mode = state.backtestMode;
  var strat = dom.backtestStrategySelect ? dom.backtestStrategySelect.value : "BULL_PUT_SPREAD";
  var dteMinParsed = parseInt(dom.backtestDteMin ? dom.backtestDteMin.value : "30", 10);
  var dteMaxParsed = parseInt(dom.backtestDteMax ? dom.backtestDteMax.value : "60", 10);
  var cfg = {
    strategy_kind: strat,
    target_dte_min: Number.isNaN(dteMinParsed) ? 30 : dteMinParsed,
    target_dte_max: Number.isNaN(dteMaxParsed) ? 60 : dteMaxParsed,
    entry_cadence: dom.backtestCadenceSelect ? dom.backtestCadenceSelect.value : "WHEN_FLAT",
    entry_weekday: parseInt(dom.backtestWeekdaySelect ? dom.backtestWeekdaySelect.value : "4", 10),
    entry_every_n_days: parseInt(dom.backtestEveryNDays ? dom.backtestEveryNDays.value : "5", 10) || 5,
    initial_capital: Number(dom.backtestInitialCapital ? dom.backtestInitialCapital.value : "10000") || 10000,
    max_open_trades: parseInt(dom.backtestMaxOpen ? dom.backtestMaxOpen.value : "1", 10) || 1,
    qty: parseInt(dom.backtestQty ? dom.backtestQty.value : "1", 10) || 1,
    commission_per_contract: Number(dom.backtestCommission ? dom.backtestCommission.value : "0.65"),
    price_basis: state.backtestPriceBasis,
    close_price_mode: state.backtestCloseMode,
    tp_sl_basis: state.backtestTpSlBasis,
    profit_target_pct: optionalNumberInput(dom.backtestTpPct, false),
    stop_loss_pct: optionalNumberInput(dom.backtestSlPct, false),
    exit_dte: optionalNumberInput(dom.backtestExitDte, true),
    min_entry_credit_dollars:
      mode !== "SINGLE_TRADE" ? optionalFilterNumberInput(dom.backtestMinCredit) : null,
    min_entry_ror_pct:
      mode !== "SINGLE_TRADE" && strat === "BULL_PUT_SPREAD"
        ? optionalFilterNumberInput(dom.backtestMinRor)
        : null,
    strike_selection_mode: dom.backtestStrikeModeSelect
      ? dom.backtestStrikeModeSelect.value
      : "TARGET_DELTA",
    target_delta: Number(dom.backtestTargetDelta ? dom.backtestTargetDelta.value : "0.30") || 0.3,
    cc_strike_floor: dom.backtestCcFloorSelect ? dom.backtestCcFloorSelect.value : "COST_BASIS",
    stock_stop_mode: dom.backtestStockStopSelect
      ? dom.backtestStockStopSelect.value
      : "OPTION_ONLY",
    cc_itm_mode: dom.backtestCcItmSelect ? dom.backtestCcItmSelect.value : "CALL_AWAY_AND_REBUY",
    roll_enabled: Boolean(dom.backtestRollToggle && dom.backtestRollToggle.checked),
    roll_credit_mode: dom.backtestRollCreditSelect
      ? dom.backtestRollCreditSelect.value
      : "CREDIT_ONLY",
  };
  if (Number.isNaN(cfg.entry_weekday)) cfg.entry_weekday = 4;
  if (Number.isNaN(cfg.commission_per_contract)) cfg.commission_per_contract = 0.65;
  return cfg;
}

function buildBacktestPayload() {
  var mode = state.backtestMode;
  var cfg = buildSharedBacktestConfig();
  var strat = cfg.strategy_kind;

  var payload = {
    mode: mode,
    ticker: state.ticker,
    start_date: dom.backtestStartDate && dom.backtestStartDate.value ? dom.backtestStartDate.value : null,
    end_date: dom.backtestEndDate && dom.backtestEndDate.value ? dom.backtestEndDate.value : null,
    config: cfg,
  };

  if (mode === "SINGLE_TRADE") {
    payload.source = state.backtestSource;
    if (state.backtestSource === "PAPER_TRADE") {
      var selectedPaperId =
        dom.backtestPaperSelect && dom.backtestPaperSelect.value ? dom.backtestPaperSelect.value : null;
      payload.paper_trade_id = selectedPaperId;
      var selectedPaper = findPaperTradeById(selectedPaperId);
      if (selectedPaper && selectedPaper.ticker) {
        payload.ticker = selectedPaper.ticker;
      }
      if (!selectedPaper) {
        delete cfg.qty;
      }
    } else {
      payload.entry_snapshot_id =
        (dom.backtestEntrySelect && dom.backtestEntrySelect.value) || state.snapshotId || null;
      if (dom.backtestEndSelect && dom.backtestEndSelect.value) {
        payload.end_snapshot_id = dom.backtestEndSelect.value;
      }
      if (state.backtestSource === "LEGS" || strat === "CUSTOM") {
        payload.legs = serializeLegsForRequest();
      }
    }
  } else if (strat === "CUSTOM") {
    payload.legs = serializeLegsForRequest();
  }
  return payload;
}

function applySingleSensitivityParam(paramName, rawValue) {
  if (!paramName) return;
  if (paramName === "profit_target_pct" && dom.backtestTpPct) {
    dom.backtestTpPct.value = Number(rawValue) <= 0 ? "" : String(rawValue);
  } else if (paramName === "stop_loss_pct" && dom.backtestSlPct) {
    dom.backtestSlPct.value = Number(rawValue) <= 0 ? "" : String(rawValue);
  } else if (paramName === "exit_dte" && dom.backtestExitDte) {
    dom.backtestExitDte.value = String(Math.round(Number(rawValue)));
  } else if (paramName === "dte_bucket") {
    var parts = String(rawValue || "").replace(":", "-").split("-");
    if (parts.length === 2) {
      if (dom.backtestDteMin) dom.backtestDteMin.value = String(parseInt(parts[0], 10));
      if (dom.backtestDteMax) dom.backtestDteMax.value = String(parseInt(parts[1], 10));
    }
  } else if (paramName === "target_delta" && dom.backtestTargetDelta) {
    dom.backtestTargetDelta.value = String(rawValue);
    if (dom.backtestStrikeModeSelect) {
      dom.backtestStrikeModeSelect.value = "TARGET_DELTA";
    }
  } else if (paramName === "min_entry_credit_dollars" && dom.backtestMinCredit) {
    dom.backtestMinCredit.value = Number(rawValue) <= 0 ? "" : String(rawValue);
  } else if (paramName === "min_entry_ror_pct" && dom.backtestMinRor) {
    dom.backtestMinRor.value = Number(rawValue) <= 0 ? "" : String(rawValue);
  }
}

export var SENSITIVITY_PARAM_LABELS = {
  profit_target_pct: "Profit Target %",
  stop_loss_pct: "Stop Loss %",
  exit_dte: "Exit DTE",
  dte_bucket: "DTE Window",
  target_delta: "Target Delta",
  min_entry_credit_dollars: "Min Credit ($)",
  min_entry_ror_pct: "Min RoR (%)",
};

export function applySensitivityCellToSystematic(cell) {
  if (!cell) return;
  applySingleSensitivityParam(cell.x_param, cell.x_value);
  applySingleSensitivityParam(cell.y_param, cell.y_value);
  state.backtestMode = "SYSTEMATIC";
  syncBacktestControlVisibility();
  var xLabelName = SENSITIVITY_PARAM_LABELS[cell.x_param] || cell.x_param;
  var yLabelName = SENSITIVITY_PARAM_LABELS[cell.y_param] || cell.y_param;
  setBacktestNotice(
    "info",
    "Applied cell (" +
      xLabelName +
      " = " +
      cell.x_label +
      ", " +
      yLabelName +
      " = " +
      cell.y_label +
      ") to Systematic Study controls. Click RUN BACKTEST to inspect trades."
  );
}

export async function runBacktestStudy() {
  if (state.backtestMode === "ROBUSTNESS") {
    return runRobustnessAudit(null);
  }
  if (state.backtestBusy) return;
  state.backtestBusy = true;
  if (dom.backtestRunBtn) dom.backtestRunBtn.disabled = true;
  if (dom.backtestStatusBadge) {
    dom.backtestStatusBadge.className = "badge badge--amber";
    dom.backtestStatusBadge.textContent = "RUNNING...";
  }
  try {
    var res = await apiJson("/api/backtest/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildBacktestPayload()),
    });
    if (!res.ok || !res.body) {
      var errMsg = extractApiErrorMessage(res.body, "Backtest execution failed.");
      setBacktestNotice("error", errMsg);
      if (dom.backtestStatusBadge) {
        dom.backtestStatusBadge.className = "badge badge--warn";
        dom.backtestStatusBadge.textContent = "ERROR";
      }
      return;
    }
    state.backtestSelectedTradeId = "";
    state.backtestExpandedTradeIds = {};
    renderBacktestResult(res.body);
    syncBacktestControlVisibility();
  } finally {
    state.backtestBusy = false;
    if (dom.backtestRunBtn) dom.backtestRunBtn.disabled = false;
  }
}

export function rerenderBacktestChartOnTheme() {
  if (!isPanelVisible("backtest")) return;
  if (state.backtestResult) {
    renderBacktestChart(state.backtestResult, themeVars());
  }
  rerenderRobustnessChartsOnTheme();
}

function findMatchedLedgerPaperTrade(trades, campaignId, positionKey, ticker) {
  if (!Array.isArray(trades) || !trades.length) return null;
  var cid = String(campaignId || "").trim();
  var pkey = String(positionKey || "").trim();
  var sym = String(ticker || "").trim().toUpperCase();

  function pickBest(candidates) {
    if (!candidates.length) return null;
    for (var i = 0; i < candidates.length; i++) {
      if (candidates[i].source === "LEDGER" && candidates[i].status === "ACTIVE") {
        return candidates[i];
      }
    }
    for (var j = 0; j < candidates.length; j++) {
      if (candidates[j].source === "LEDGER") return candidates[j];
    }
    for (var k = 0; k < candidates.length; k++) {
      if (candidates[k].status === "ACTIVE") return candidates[k];
    }
    return candidates[0];
  }

  if (cid) {
    var byCampaign = trades.filter(function (pt) {
      return pt.ledger_campaign_id === cid && (!sym || pt.ticker === sym);
    });
    var bestCamp = pickBest(byCampaign);
    if (bestCamp) return bestCamp;
  }
  if (pkey) {
    var byPos = trades.filter(function (pt) {
      return pt.ledger_campaign_id === pkey && (!sym || pt.ticker === sym);
    });
    var bestPos = pickBest(byPos);
    if (bestPos) return bestPos;
  }
  if (!cid && !pkey && sym) {
    var byTicker = trades.filter(function (pt) {
      return pt.source === "LEDGER" && pt.ticker === sym;
    });
    return pickBest(byTicker);
  }
  return null;
}

async function activateAndReplayPaperTrade(matched) {
  if (!matched) return;
  setPaperTradesExpanded(false);
  ensurePanelVisible("backtest");
  var filterVal = dom.backtestPaperFilter ? dom.backtestPaperFilter.value : "ACTIVE";
  var inVisibleList = (state.backtestPaperTrades || []).some(function (t) {
    return t.id === matched.id;
  });
  if (!inVisibleList || (filterVal !== "ALL" && filterVal !== matched.status)) {
    if (dom.backtestPaperFilter && filterVal !== "ALL" && filterVal !== matched.status) {
      dom.backtestPaperFilter.value = "ALL";
    }
    await loadPaperTrades();
  }
  if (dom.backtestCard && typeof dom.backtestCard.scrollIntoView === "function") {
    dom.backtestCard.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  clearBacktestDateAndEndFilters();
  var switchedTicker = Boolean(matched.ticker && matched.ticker !== state.ticker);
  if (switchedTicker) {
    await loadChain(matched.ticker, "");
  }
  state.backtestMode = "SINGLE_TRADE";
  state.backtestSource = "PAPER_TRADE";
  syncBacktestControlVisibility();
  if (dom.backtestPaperSelect) {
    dom.backtestPaperSelect.value = matched.id;
    if (dom.backtestPaperSelect.value !== matched.id && typeof document !== "undefined") {
      var fallbackOpt = document.createElement("option");
      fallbackOpt.value = matched.id;
      fallbackOpt.textContent = matched.id + (matched.ticker ? " \u2022 " + matched.ticker : "");
      fallbackOpt.selected = true;
      dom.backtestPaperSelect.appendChild(fallbackOpt);
      dom.backtestPaperSelect.value = matched.id;
    }
  }
  syncPaperTradeQtyInput(matched);
  clearBacktestDateAndEndFilters();
  setBacktestNotice("", "");
  await runBacktestStudy();
  if (switchedTicker && !state.chainData && state.backtestResult) {
    var stratLabel = matched.ledger_strategy_label || matched.strategy_kind;
    showBanner(
      "info",
      "Replaying Ledger trade " +
        matched.id +
        " (" +
        matched.ticker +
        " \u2022 " +
        stratLabel +
        ") using synthetic Ledger MTM fallback.",
      { ticker: matched.ticker, phase: "PAPER_TRADE" }
    );
  }
}

export async function replayLedgerTradeInBacktester(options) {
  var opts = options && typeof options === "object" ? options : {};
  var campaignId = opts.campaignId ? String(opts.campaignId).trim() : "";
  var positionKey = opts.positionKey ? String(opts.positionKey).trim() : "";
  var ticker = opts.ticker ? String(opts.ticker).trim().toUpperCase() : "";

  var matched = findMatchedLedgerPaperTrade(
    state.backtestPaperTrades,
    campaignId,
    positionKey,
    ticker
  );
  if (!matched) {
    var listRes = await apiJson("/api/backtest/paper-trades");
    if (listRes.ok && listRes.body && Array.isArray(listRes.body.trades)) {
      matched = findMatchedLedgerPaperTrade(
        listRes.body.trades,
        campaignId,
        positionKey,
        ticker
      );
    }
  }
  if (!matched) {
    var syncBody = await syncPaperTradesFromLedger();
    var syncedTrades =
      syncBody && Array.isArray(syncBody.trades)
        ? syncBody.trades
        : state.backtestPaperTrades;
    matched = findMatchedLedgerPaperTrade(syncedTrades, campaignId, positionKey, ticker);
  }
  if (!matched) {
    var label = ticker || campaignId || positionKey || "position";
    var warnMsg =
      "No replayable option Paper Trade found for Trade Ledger " +
      label +
      " (stock-only lots or groups > 8 legs are skipped).";
    showBanner("warn", warnMsg, { ticker: ticker || state.ticker, phase: "PAPER_TRADE" });
    setBacktestNotice("warn", warnMsg);
    return;
  }
  await activateAndReplayPaperTrade(matched);
}

async function replayPaperTradeFromList(pid) {
  var tradeId = String(pid || "").trim();
  if (!tradeId) return;
  var matched = findPaperTradeById(tradeId);
  if (!matched) {
    await loadPaperTrades();
    matched = findPaperTradeById(tradeId);
  }
  if (matched) {
    await activateAndReplayPaperTrade(matched);
    return;
  }
  setPaperTradesExpanded(false);
  state.backtestMode = "SINGLE_TRADE";
  state.backtestSource = "PAPER_TRADE";
  syncBacktestControlVisibility();
  if (dom.backtestPaperSelect) dom.backtestPaperSelect.value = tradeId;
  clearBacktestDateAndEndFilters();
  setBacktestNotice("", "");
  await runBacktestStudy();
}

async function reloadPaperTradesAndSyncQty(loaderFn, forceSync) {
  var prevId = dom.backtestPaperSelect ? dom.backtestPaperSelect.value : "";
  await loaderFn();
  var nextId = dom.backtestPaperSelect ? dom.backtestPaperSelect.value : "";
  if (
    state.backtestMode === "SINGLE_TRADE" &&
    state.backtestSource === "PAPER_TRADE" &&
    nextId &&
    (forceSync || nextId !== prevId)
  ) {
    syncSelectedPaperTradeQty();
  }
}

function wireSegGroup(groupEl, attrName, stateKey, fallback) {
  if (!groupEl) return;
  groupEl.addEventListener("click", function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest("[" + attrName + "]") : null;
    if (!btn) return;
    var prevMode = state.backtestMode;
    var prevSource = state.backtestSource;
    state[stateKey] = btn.getAttribute(attrName) || fallback;
    syncBacktestControlVisibility();
    if (
      (stateKey === "backtestMode" || stateKey === "backtestSource") &&
      (prevMode !== state.backtestMode || prevSource !== state.backtestSource) &&
      state.backtestMode === "SINGLE_TRADE" &&
      state.backtestSource === "PAPER_TRADE"
    ) {
      syncSelectedPaperTradeQty();
    }
  });
}

function positionInfoTooltip(btn) {
  if (!btn || !btn.classList || !btn.classList.contains("bt-info-btn")) return;
  btn.classList.remove("is-tip-left", "is-tip-right", "is-tip-below");
  if (typeof btn.getBoundingClientRect !== "function") return;
  var rect = btn.getBoundingClientRect();
  var cardRect =
    dom.backtestCard && typeof dom.backtestCard.getBoundingClientRect === "function"
      ? dom.backtestCard.getBoundingClientRect()
      : null;
  var vw = typeof window !== "undefined" && window.innerWidth ? window.innerWidth : 1280;
  var minLeft = cardRect ? Math.max(8, cardRect.left + 8) : 8;
  var maxRight = cardRect && cardRect.right ? Math.min(vw - 8, cardRect.right - 8) : vw - 8;
  var minTop = cardRect ? Math.max(8, cardRect.top + 8) : 8;
  var tipHalfW = 145;
  var tipH = 95;
  if (rect.left - tipHalfW < minLeft) {
    btn.classList.add("is-tip-left");
  } else if (rect.right + tipHalfW > maxRight) {
    btn.classList.add("is-tip-right");
  }
  if (rect.top - tipH < minTop) {
    btn.classList.add("is-tip-below");
  }
}

export function bindBacktestEvents() {
  if (dom.backtestCard) {
    dom.backtestCard.addEventListener("mouseover", function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest(".bt-info-btn") : null;
      if (btn) positionInfoTooltip(btn);
    });
    dom.backtestCard.addEventListener("focusin", function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest(".bt-info-btn") : null;
      if (btn) positionInfoTooltip(btn);
    });
  }

  wireSegGroup(dom.backtestModeGroup, "data-bt-mode", "backtestMode", "SYSTEMATIC");
  wireSegGroup(dom.backtestSourceGroup, "data-bt-source", "backtestSource", "AUTO_BUILD");
  wireSegGroup(dom.backtestPriceBasisGroup, "data-bt-price-basis", "backtestPriceBasis", "AUTO_MID_LAST_CLOSE");
  wireSegGroup(dom.backtestCloseModeGroup, "data-bt-close-mode", "backtestCloseMode", "MID");
  wireSegGroup(dom.backtestTpSlBasisGroup, "data-bt-tpsl-basis", "backtestTpSlBasis", "CREDIT_DEBIT_PCT");

  if (dom.backtestInitialCapital) {
    dom.backtestInitialCapital.addEventListener("input", syncCapitalAdequacyHint);
    dom.backtestInitialCapital.addEventListener("change", syncCapitalAdequacyHint);
  }
  if (dom.backtestQty) {
    dom.backtestQty.addEventListener("input", syncCapitalAdequacyHint);
    dom.backtestQty.addEventListener("change", syncCapitalAdequacyHint);
  }
  if (dom.backtestMatchCapitalBtn) {
    dom.backtestMatchCapitalBtn.addEventListener("click", function () {
      var spot =
        state.chainData && state.chainData.underlying_price
          ? Number(state.chainData.underlying_price)
          : 0;
      var qty = Math.max(1, parseInt(dom.backtestQty ? dom.backtestQty.value : "1", 10) || 1);
      var target =
        spot > 0
          ? Math.max(100, Math.ceil((100 * qty * spot) / 100) * 100)
          : Number(
              (dom.backtestMatchCapitalBtn.dataset &&
                dom.backtestMatchCapitalBtn.dataset.suggestedCapital) ||
                10000
            );
      if (dom.backtestInitialCapital && target >= 100) {
        dom.backtestInitialCapital.value = String(target);
      }
      syncCapitalAdequacyHint();
    });
  }

  if (dom.backtestCadenceSelect) dom.backtestCadenceSelect.addEventListener("change", syncBacktestControlVisibility);
  if (dom.backtestStrategySelect) {
    dom.backtestStrategySelect.addEventListener("change", function () {
      var strat = dom.backtestStrategySelect.value;
      if ((strat === "COVERED_CALL" || strat === "WHEEL") && dom.backtestCadenceSelect) {
        dom.backtestCadenceSelect.value = "WHEN_FLAT";
      }
      syncBacktestControlVisibility();
    });
  }
  if (dom.backtestStrikeModeSelect) {
    dom.backtestStrikeModeSelect.addEventListener("change", syncBacktestControlVisibility);
  }
  if (dom.backtestRollToggle) {
    dom.backtestRollToggle.addEventListener("change", syncBacktestControlVisibility);
  }
  if (dom.backtestForm) {
    dom.backtestForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      runBacktestStudy();
    });
  }
  if (dom.backtestRunBtn) dom.backtestRunBtn.addEventListener("click", runBacktestStudy);
  if (dom.backtestSavePaperBtn) dom.backtestSavePaperBtn.addEventListener("click", saveCurrentPaperTrade);
  if (dom.backtestImportLabBtn) dom.backtestImportLabBtn.addEventListener("click", copyStrategyLabLegsToBacktest);
  if (dom.backtestAddLegBtn) dom.backtestAddLegBtn.addEventListener("click", addCustomBacktestLeg);
  if (dom.backtestLegsBody) {
    dom.backtestLegsBody.addEventListener("click", function (ev) {
      var remBtn = ev.target && ev.target.closest ? ev.target.closest("[data-bt-leg-remove]") : null;
      if (!remBtn) return;
      var idx = parseInt(remBtn.getAttribute("data-bt-leg-remove") || "-1", 10);
      if (idx >= 0) removeCustomBacktestLeg(idx);
    });
  }
  if (dom.backtestPaperSelect) dom.backtestPaperSelect.addEventListener("change", syncSelectedPaperTradeQty);
  if (dom.backtestPaperToggle) {
    dom.backtestPaperToggle.addEventListener("click", togglePaperTradesDrawer);
  }
  if (dom.backtestPaperTickerFilter) {
    dom.backtestPaperTickerFilter.addEventListener("change", function () {
      state.backtestPaperTickerFilter = dom.backtestPaperTickerFilter.value || "ALL";
      renderPaperTradesList();
    });
  }
  if (dom.backtestPaperFilter) {
    dom.backtestPaperFilter.addEventListener("change", function () {
      reloadPaperTradesAndSyncQty(loadPaperTrades);
    });
  }
  if (dom.backtestPaperSyncLedgerBtn) {
    dom.backtestPaperSyncLedgerBtn.addEventListener("click", function () {
      reloadPaperTradesAndSyncQty(syncPaperTradesFromLedger, true);
    });
  }
  if (dom.backtestPaperRefreshBtn) {
    dom.backtestPaperRefreshBtn.addEventListener("click", function () {
      reloadPaperTradesAndSyncQty(loadPaperTrades);
    });
  }
  if (dom.backtestPaperList) {
    dom.backtestPaperList.addEventListener("click", function (ev) {
      var stopBtn = ev.target && ev.target.closest ? ev.target.closest("[data-paper-stop]") : null;
      if (stopBtn) {
        stopPaperTrade(stopBtn.getAttribute("data-paper-stop"));
        return;
      }
      var replayBtn = ev.target && ev.target.closest ? ev.target.closest("[data-paper-replay]") : null;
      if (replayBtn) {
        replayPaperTradeFromList(replayBtn.getAttribute("data-paper-replay") || "");
      }
    });
  }
  if (dom.backtestChartViewGroup) {
    dom.backtestChartViewGroup.addEventListener("click", function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest("[data-bt-chart-view]") : null;
      if (!btn) return;
      state.backtestChartView = btn.getAttribute("data-bt-chart-view") || "EQUITY";
      syncBacktestControlVisibility();
      if (state.backtestResult && state.backtestResult.simulation) {
        renderBacktestChart(state.backtestResult, themeVars());
      }
    });
  }
  if (dom.backtestTradesBody) {
    dom.backtestTradesBody.addEventListener("click", function (ev) {
      var target = ev.target;
      if (!target || !target.closest) return;
      var toggleBtn = target.closest("[data-bt-trade-toggle]");
      if (toggleBtn) {
        ev.stopPropagation();
        var toggleId = toggleBtn.getAttribute("data-bt-trade-toggle") || "";
        if (toggleId && state.backtestResult && state.backtestResult.simulation) {
          toggleBacktestTradeExpand(toggleId);
          renderTradeLog(state.backtestResult.simulation);
        }
        return;
      }
      if (target.closest(".backtest-trade-detail-row")) return;
      var row = target.closest("tr.backtest-trade-row[data-trade-id]");
      if (!row || !state.backtestResult || !state.backtestResult.simulation) return;
      state.backtestSelectedTradeId = row.getAttribute("data-trade-id") || "";
      renderTradeLog(state.backtestResult.simulation);
      renderBacktestChart(state.backtestResult, themeVars());
    });
  }
  initBacktestTablesSplitter();
  bindRobustnessEvents();
}

export function initBacktestPanel() {
  setPaperTradesExpanded(false);
  renderCustomLegsEditor();
  syncBacktestControlVisibility();
  renderBacktestEmptyPlaceholder();
  loadPaperTrades();
}
