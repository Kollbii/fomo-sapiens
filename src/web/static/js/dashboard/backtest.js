/* IBKR Options Analyzer dashboard — Backtest controls, execution, KPIs & step drill-down. */

import {
  dom,
  state,
} from "./state.js";
import {
  extractApiErrorMessage,
  themeVars,
} from "./helpers.js";
import {
  isPanelVisible,
} from "./panels.js";
import {
  apiJson,
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
  saveCurrentPaperTrade,
  setBacktestNotice,
  stopPaperTrade,
  toIsoExpiry,
} from "./backtest_paper.js";
import {
  renderBacktestResult,
  renderTradeLog,
  toggleBacktestTradeExpand,
} from "./backtest_tables.js";

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
  var source = state.backtestSource;
  var cadence = dom.backtestCadenceSelect ? dom.backtestCadenceSelect.value : "WEEKLY_DAY";
  var strat = dom.backtestStrategySelect ? dom.backtestStrategySelect.value : "BULL_PUT_SPREAD";

  if (dom.backtestSingleControls) dom.backtestSingleControls.hidden = !isSingle;

  var sysEls = document.querySelectorAll("[data-bt-systematic]");
  for (var i = 0; i < sysEls.length; i++) {
    sysEls[i].hidden = isSingle;
  }

  var weekdayEl = document.querySelector("[data-bt-weekday]");
  if (weekdayEl) weekdayEl.hidden = isSingle || cadence !== "WEEKLY_DAY";

  var everyNEl = document.querySelector("[data-bt-every-n]");
  if (everyNEl) everyNEl.hidden = isSingle || cadence !== "EVERY_N_DAYS";

  var entryFiltersEl = document.querySelector("[data-bt-entry-filters]");
  if (entryFiltersEl) entryFiltersEl.hidden = isSingle || strat !== "BULL_PUT_SPREAD";

  var entryWrap = document.querySelector("[data-bt-entry-wrap]");
  if (entryWrap) entryWrap.hidden = !isSingle || source === "PAPER_TRADE";

  var endWrap = document.querySelector("[data-bt-end-wrap]");
  if (endWrap) endWrap.hidden = !isSingle;

  var paperWrap = document.querySelector("[data-bt-paper-wrap]");
  if (paperWrap) paperWrap.hidden = !isSingle || source !== "PAPER_TRADE";

  if (dom.backtestLegsBox) {
    dom.backtestLegsBox.hidden = !(strat === "CUSTOM" || (isSingle && source === "LEGS"));
  }

  var simMode =
    state.backtestResult && state.backtestResult.simulation
      ? state.backtestResult.simulation.mode
      : null;
  var showChartViewToggle = simMode ? simMode === "SYSTEMATIC" : !isSingle;
  if (dom.backtestChartViewGroup) {
    dom.backtestChartViewGroup.hidden = !showChartViewToggle;
  }

  setSegActive(dom.backtestModeGroup, "data-bt-mode", state.backtestMode);
  setSegActive(dom.backtestSourceGroup, "data-bt-source", state.backtestSource);
  setSegActive(dom.backtestPriceBasisGroup, "data-bt-price-basis", state.backtestPriceBasis);
  setSegActive(dom.backtestCloseModeGroup, "data-bt-close-mode", state.backtestCloseMode);
  setSegActive(dom.backtestTpSlBasisGroup, "data-bt-tpsl-basis", state.backtestTpSlBasis);
  setSegActive(dom.backtestChartViewGroup, "data-bt-chart-view", state.backtestChartView);
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
  var res = await apiJson("/api/backtest/entries/" + encodeURIComponent(sym));
  if (res.ok && res.body) {
    state.backtestEntries = Array.isArray(res.body.entries) ? res.body.entries : [];
    state.backtestEntriesTicker = sym;
    renderEntrySelectOptions(state.backtestEntries);
  }
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
      expiry: toIsoExpiry(leg.expiry),
      strike: Number(leg.strike),
      qty: Math.max(1, parseInt(leg.qty || 1, 10) || 1),
    };
  });
}

function buildBacktestPayload() {
  var mode = state.backtestMode;
  var strat = dom.backtestStrategySelect ? dom.backtestStrategySelect.value : "BULL_PUT_SPREAD";
  var dteMinParsed = parseInt(dom.backtestDteMin ? dom.backtestDteMin.value : "30", 10);
  var dteMaxParsed = parseInt(dom.backtestDteMax ? dom.backtestDteMax.value : "60", 10);
  var cfg = {
    strategy_kind: strat,
    target_dte_min: Number.isNaN(dteMinParsed) ? 30 : dteMinParsed,
    target_dte_max: Number.isNaN(dteMaxParsed) ? 60 : dteMaxParsed,
    entry_cadence: dom.backtestCadenceSelect ? dom.backtestCadenceSelect.value : "WEEKLY_DAY",
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
      mode === "SYSTEMATIC" ? optionalFilterNumberInput(dom.backtestMinCredit) : null,
    min_entry_ror_pct:
      mode === "SYSTEMATIC" ? optionalFilterNumberInput(dom.backtestMinRor) : null,
  };
  if (Number.isNaN(cfg.entry_weekday)) cfg.entry_weekday = 4;
  if (Number.isNaN(cfg.commission_per_contract)) cfg.commission_per_contract = 0.65;

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
      payload.paper_trade_id =
        dom.backtestPaperSelect && dom.backtestPaperSelect.value ? dom.backtestPaperSelect.value : null;
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

export async function runBacktestStudy() {
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
  if (state.backtestResult && isPanelVisible("backtest")) {
    renderBacktestChart(state.backtestResult, themeVars());
  }
}

function wireSegGroup(groupEl, attrName, stateKey, fallback) {
  if (!groupEl) return;
  groupEl.addEventListener("click", function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest("[" + attrName + "]") : null;
    if (!btn) return;
    state[stateKey] = btn.getAttribute(attrName) || fallback;
    syncBacktestControlVisibility();
  });
}

export function bindBacktestEvents() {
  wireSegGroup(dom.backtestModeGroup, "data-bt-mode", "backtestMode", "SYSTEMATIC");
  wireSegGroup(dom.backtestSourceGroup, "data-bt-source", "backtestSource", "AUTO_BUILD");
  wireSegGroup(dom.backtestPriceBasisGroup, "data-bt-price-basis", "backtestPriceBasis", "AUTO_MID_LAST_CLOSE");
  wireSegGroup(dom.backtestCloseModeGroup, "data-bt-close-mode", "backtestCloseMode", "MID");
  wireSegGroup(dom.backtestTpSlBasisGroup, "data-bt-tpsl-basis", "backtestTpSlBasis", "CREDIT_DEBIT_PCT");

  if (dom.backtestCadenceSelect) dom.backtestCadenceSelect.addEventListener("change", syncBacktestControlVisibility);
  if (dom.backtestStrategySelect) dom.backtestStrategySelect.addEventListener("change", syncBacktestControlVisibility);
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
  if (dom.backtestPaperFilter) dom.backtestPaperFilter.addEventListener("change", loadPaperTrades);
  if (dom.backtestPaperRefreshBtn) dom.backtestPaperRefreshBtn.addEventListener("click", loadPaperTrades);
  if (dom.backtestPaperList) {
    dom.backtestPaperList.addEventListener("click", function (ev) {
      var stopBtn = ev.target && ev.target.closest ? ev.target.closest("[data-paper-stop]") : null;
      if (stopBtn) {
        stopPaperTrade(stopBtn.getAttribute("data-paper-stop"));
        return;
      }
      var replayBtn = ev.target && ev.target.closest ? ev.target.closest("[data-paper-replay]") : null;
      if (replayBtn) {
        var pid = replayBtn.getAttribute("data-paper-replay") || "";
        state.backtestMode = "SINGLE_TRADE";
        state.backtestSource = "PAPER_TRADE";
        syncBacktestControlVisibility();
        if (dom.backtestPaperSelect && pid) dom.backtestPaperSelect.value = pid;
        runBacktestStudy();
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
}

export function initBacktestPanel() {
  renderCustomLegsEditor();
  syncBacktestControlVisibility();
  renderBacktestEmptyPlaceholder();
  loadPaperTrades();
}
