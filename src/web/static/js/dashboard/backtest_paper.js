/* IBKR Options Analyzer dashboard — Backtest custom legs editor & Paper Trades workflow. */

import {
  dom,
  state,
} from "./state.js";
import {
  escapeHtml,
  extractApiErrorMessage,
  formatExpiryIso,
  showBanner,
} from "./helpers.js";
import {
  apiJson,
} from "./network.js";

export function setBacktestNotice(tone, msg) {
  if (!dom.backtestWarnings) return;
  if (!msg) {
    dom.backtestWarnings.innerHTML = "";
    return;
  }
  var cls = tone === "error" ? "backtest-warning backtest-warning--error" : "backtest-warning";
  dom.backtestWarnings.innerHTML = '<div class="' + cls + '">' + escapeHtml(msg) + "</div>";
}

function defaultLegFromChain() {
  var exp = "";
  var strike = 100;
  if (state.chainData && state.chainData.contracts && state.chainData.contracts.length) {
    var midIdx = Math.floor(state.chainData.contracts.length / 2);
    var sample = state.chainData.contracts[midIdx] || state.chainData.contracts[0];
    exp = formatExpiryIso(sample.expiry);
    strike = Number(sample.strike) || 100;
  }
  return { side: "SELL", right: "P", expiry: exp, strike: strike, qty: 1 };
}

export function renderCustomLegsEditor() {
  if (!dom.backtestLegsBody) return;
  var legs = Array.isArray(state.backtestCustomLegs) ? state.backtestCustomLegs : [];
  if (!legs.length) {
    dom.backtestLegsBody.innerHTML =
      '<tr><td colspan="6">No custom legs defined. Click <strong>Copy Strategy Lab Legs</strong> or <strong>+ Add Leg</strong>.</td></tr>';
    return;
  }
  var html = "";
  for (var i = 0; i < legs.length; i++) {
    var leg = legs[i];
    html +=
      '<tr data-bt-leg-row="' +
      i +
      '">' +
      '<td><select class="select-control" data-bt-leg-field="side" aria-label="Leg Side">' +
      '<option value="SELL"' + (leg.side === "SELL" ? " selected" : "") + ">SELL</option>" +
      '<option value="BUY"' + (leg.side === "BUY" ? " selected" : "") + ">BUY</option>" +
      "</select></td>" +
      '<td><select class="select-control" data-bt-leg-field="right" aria-label="Leg Right">' +
      '<option value="P"' + (leg.right === "P" ? " selected" : "") + ">Put</option>" +
      '<option value="C"' + (leg.right === "C" ? " selected" : "") + ">Call</option>" +
      "</select></td>" +
      '<td><input class="input-control" type="text" placeholder="YYYY-MM-DD" data-bt-leg-field="expiry" value="' +
      escapeHtml(formatExpiryIso(leg.expiry)) +
      '" aria-label="Leg Expiry" /></td>' +
      '<td><input class="input-control" type="number" step="0.5" min="0.01" data-bt-leg-field="strike" value="' +
      escapeHtml(leg.strike) +
      '" aria-label="Leg Strike" /></td>' +
      '<td><input class="input-control" type="number" step="1" min="1" max="100" data-bt-leg-field="qty" value="' +
      escapeHtml(leg.qty || 1) +
      '" aria-label="Leg Qty" /></td>' +
      '<td><button type="button" class="btn btn-sm" data-bt-leg-remove="' +
      i +
      '" aria-label="Remove leg">\u00D7</button></td>' +
      "</tr>";
  }
  dom.backtestLegsBody.innerHTML = html;
}

export function readCustomLegsFromUI() {
  if (!dom.backtestLegsBody) return state.backtestCustomLegs.slice();
  var rows = dom.backtestLegsBody.querySelectorAll("tr[data-bt-leg-row]");
  if (!rows.length) return [];
  var out = [];
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var sideEl = row.querySelector('[data-bt-leg-field="side"]');
    var rightEl = row.querySelector('[data-bt-leg-field="right"]');
    var expEl = row.querySelector('[data-bt-leg-field="expiry"]');
    var strikeEl = row.querySelector('[data-bt-leg-field="strike"]');
    var qtyEl = row.querySelector('[data-bt-leg-field="qty"]');
    var expiry = formatExpiryIso(expEl ? expEl.value : "");
    var strike = Number(strikeEl ? strikeEl.value : 0);
    var qty = parseInt(qtyEl ? qtyEl.value : "1", 10) || 1;
    out.push({
      side: sideEl && sideEl.value === "BUY" ? "BUY" : "SELL",
      right: rightEl && rightEl.value === "C" ? "C" : "P",
      expiry: expiry,
      strike: strike,
      qty: Math.max(1, qty),
    });
  }
  state.backtestCustomLegs = out;
  return out;
}

export function addCustomBacktestLeg() {
  readCustomLegsFromUI();
  if (state.backtestCustomLegs.length >= 8) {
    setBacktestNotice("warn", "Paper trades and custom replays support at most 8 legs.");
    return;
  }
  state.backtestCustomLegs.push(defaultLegFromChain());
  renderCustomLegsEditor();
}

export function removeCustomBacktestLeg(idx) {
  readCustomLegsFromUI();
  state.backtestCustomLegs.splice(idx, 1);
  renderCustomLegsEditor();
}

export function copyStrategyLabLegsToBacktest() {
  var src = Array.isArray(state.strategyLegs) ? state.strategyLegs : [];
  if (!src.length) {
    setBacktestNotice("warn", "Strategy Lab has no legs built yet for the active snapshot.");
    return false;
  }
  state.backtestCustomLegs = src.slice(0, 8).map(function (leg) {
    return {
      side: leg.side === "BUY" ? "BUY" : "SELL",
      right: leg.right === "C" ? "C" : "P",
      expiry: formatExpiryIso(leg.expiry),
      strike: Number(leg.strike),
      qty: Math.max(1, parseInt(leg.qty || 1, 10) || 1),
    };
  });
  renderCustomLegsEditor();
  setBacktestNotice("", "");
  return true;
}

export function formatLegsCompact(legs) {
  if (!Array.isArray(legs) || !legs.length) return "\u2014";
  return legs
    .map(function (leg) {
      var sign = leg.side === "SELL" ? "-" : "+";
      var ratio = leg.qty_ratio > 1 ? leg.qty_ratio + "x " : "";
      var fill =
        leg.entry_price !== null && leg.entry_price !== undefined
          ? " @ $" + Number(leg.entry_price).toFixed(2)
          : "";
      return ratio + sign + leg.strike + leg.right + " (" + formatExpiryIso(leg.expiry) + ")" + fill;
    })
    .join(" / ");
}

function populatePaperTradeSelect(trades) {
  if (!dom.backtestPaperSelect) return;
  var prev = dom.backtestPaperSelect.value;
  dom.backtestPaperSelect.innerHTML = "";
  if (!trades || !trades.length) {
    var opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No saved paper trades";
    dom.backtestPaperSelect.appendChild(opt);
    return;
  }
  for (var i = 0; i < trades.length; i++) {
    var pt = trades[i];
    var option = document.createElement("option");
    var stratLabel = pt.ledger_strategy_label || pt.strategy_kind;
    var ledgerTag = pt.source === "LEDGER" ? " [LEDGER]" : "";
    option.value = pt.id;
    option.textContent =
      pt.id +
      " \u2022 " +
      pt.ticker +
      " \u2022 " +
      stratLabel +
      " (" +
      pt.status +
      ")" +
      ledgerTag;
    if (pt.id === prev) option.selected = true;
    dom.backtestPaperSelect.appendChild(option);
  }
}

export function setPaperTradesExpanded(expanded) {
  var next = Boolean(expanded);
  state.backtestPaperExpanded = next;
  if (dom.backtestPaperDrawer) {
    dom.backtestPaperDrawer.hidden = !next;
  }
  if (dom.backtestPaperToggle) {
    dom.backtestPaperToggle.setAttribute("aria-expanded", next ? "true" : "false");
  }
  if (dom.backtestPaperToggleLabel) {
    dom.backtestPaperToggleLabel.textContent = next ? "Hide" : "Show";
  }
  if (dom.backtestPaperStrip) {
    dom.backtestPaperStrip.classList.toggle("is-expanded", next);
  }
}

export function togglePaperTradesDrawer() {
  setPaperTradesExpanded(!state.backtestPaperExpanded);
}

export function filterPaperTradesByTicker(trades, filterVal, activeTicker) {
  if (!Array.isArray(trades)) return [];
  var val = String(filterVal || "ALL").trim().toUpperCase();
  if (!val || val === "ALL") return trades.slice();
  var target = val === "CURRENT" ? String(activeTicker || "").trim().toUpperCase() : val;
  if (!target) return [];
  return trades.filter(function (pt) {
    return String((pt && pt.ticker) || "").trim().toUpperCase() === target;
  });
}

function syncPaperTickerFilterOptions(trades) {
  var sel = dom.backtestPaperTickerFilter;
  if (!sel) return;
  var activeSym = String(state.ticker || "").trim().toUpperCase();
  var seen = {};
  var tickers = [];
  for (var i = 0; i < trades.length; i++) {
    var sym = String((trades[i] && trades[i].ticker) || "").trim().toUpperCase();
    if (sym && !seen[sym]) {
      seen[sym] = true;
      tickers.push(sym);
    }
  }
  tickers.sort();

  var wanted = String(state.backtestPaperTickerFilter || "ALL").trim().toUpperCase();
  if (wanted !== "ALL" && wanted !== "CURRENT" && !seen[wanted]) {
    wanted = "ALL";
    state.backtestPaperTickerFilter = "ALL";
  }

  sel.innerHTML = "";
  var allOpt = document.createElement("option");
  allOpt.value = "ALL";
  allOpt.textContent = "All Tickers";
  sel.appendChild(allOpt);

  var curOpt = document.createElement("option");
  curOpt.value = "CURRENT";
  curOpt.textContent = activeSym ? "Active (" + activeSym + ")" : "Active Ticker";
  sel.appendChild(curOpt);

  for (var j = 0; j < tickers.length; j++) {
    var opt = document.createElement("option");
    opt.value = tickers[j];
    opt.textContent = tickers[j];
    sel.appendChild(opt);
  }
  sel.value = wanted;
}

export function renderPaperTradesList() {
  var trades = Array.isArray(state.backtestPaperTrades) ? state.backtestPaperTrades : [];
  populatePaperTradeSelect(trades);
  if (dom.backtestPaperSummary) {
    dom.backtestPaperSummary.textContent =
      (state.backtestPaperActiveCount || 0) +
      " / " +
      (state.backtestPaperMaxActive || 120) +
      " active";
  }
  var activeSym = String(state.ticker || "").trim().toUpperCase();
  if (dom.backtestPaperTickerCount) {
    var activeCount = activeSym
      ? trades.filter(function (pt) {
          return String((pt && pt.ticker) || "").trim().toUpperCase() === activeSym;
        }).length
      : 0;
    if (activeCount > 0) {
      dom.backtestPaperTickerCount.hidden = false;
      dom.backtestPaperTickerCount.textContent = activeCount + " for " + activeSym;
    } else {
      dom.backtestPaperTickerCount.hidden = true;
      dom.backtestPaperTickerCount.textContent = "";
    }
  }
  syncPaperTickerFilterOptions(trades);
  if (!dom.backtestPaperList) return;
  if (!trades.length) {
    dom.backtestPaperList.innerHTML =
      '<div class="backtest-paper-hint">No matching paper trades saved yet. Use <strong>Save Paper Trade</strong> above to track a position.</div>';
    return;
  }
  var visibleTrades = filterPaperTradesByTicker(
    trades,
    state.backtestPaperTickerFilter,
    state.ticker
  );
  if (!visibleTrades.length) {
    dom.backtestPaperList.innerHTML =
      '<div class="backtest-paper-hint">No paper trades match the current ticker filter.</div>';
    return;
  }
  var rowsHtml = "";
  for (var i = 0; i < visibleTrades.length; i++) {
    var pt = visibleTrades[i];
    var cashStr =
      pt.entry_net_cash >= 0
        ? "+$" + Number(pt.entry_net_cash).toFixed(2) + " cr"
        : "-$" + Math.abs(Number(pt.entry_net_cash)).toFixed(2) + " db";
    var canStop = pt.status === "ACTIVE";
    var sourceBadge =
      pt.source === "LEDGER"
        ? '<span class="backtest-source-badge backtest-source-badge--ledger">LEDGER</span>'
        : "";
    var stratLabel = pt.ledger_strategy_label || pt.strategy_kind;
    var legsText = formatLegsCompact(pt.legs);
    rowsHtml +=
      '<tr data-paper-id="' +
      escapeHtml(pt.id) +
      '">' +
      '<td class="text-left"><strong>' +
      escapeHtml(pt.id) +
      "</strong>" +
      sourceBadge +
      "</td>" +
      '<td class="text-left"><span class="backtest-status backtest-status--' +
      escapeHtml(pt.status) +
      '">' +
      escapeHtml(pt.status) +
      "</span></td>" +
      '<td class="text-left"><strong>' +
      escapeHtml(pt.ticker) +
      "</strong></td>" +
      '<td class="text-left">' +
      escapeHtml(stratLabel) +
      "</td>" +
      '<td class="text-left backtest-paper-legs-cell" title="' +
      escapeHtml(legsText) +
      '">' +
      escapeHtml(legsText) +
      "</td>" +
      "<td>x" +
      escapeHtml(pt.qty) +
      " \u2022 " +
      escapeHtml(cashStr) +
      "</td>" +
      '<td class="text-left">' +
      escapeHtml(pt.expiry || "\u2014") +
      "</td>" +
      '<td class="backtest-paper-actions-cell"><div class="backtest-paper-actions-group">' +
      '<button type="button" class="btn btn-sm" data-paper-replay="' +
      escapeHtml(pt.id) +
      '">Replay</button>' +
      (canStop
        ? '<button type="button" class="btn btn-sm" data-paper-stop="' +
          escapeHtml(pt.id) +
          '">Stop</button>'
        : "") +
      "</div></td>" +
      "</tr>";
  }
  dom.backtestPaperList.innerHTML =
    '<div class="backtest-paper-table-wrap">' +
    '<table class="backtest-table backtest-paper-table">' +
    "<thead><tr>" +
    '<th class="text-left">Trade</th>' +
    '<th class="text-left">Status</th>' +
    '<th class="text-left">Ticker</th>' +
    '<th class="text-left">Strategy</th>' +
    '<th class="text-left">Legs</th>' +
    "<th>Qty / Entry</th>" +
    '<th class="text-left">Expiry</th>' +
    "<th>Actions</th>" +
    "</tr></thead>" +
    "<tbody>" +
    rowsHtml +
    "</tbody>" +
    "</table>" +
    "</div>";
}

export async function syncPaperTradesFromLedger() {
  if (dom.backtestPaperSyncLedgerBtn) {
    dom.backtestPaperSyncLedgerBtn.disabled = true;
  }
  try {
    var res = await apiJson("/api/backtest/paper-trades/sync-ledger", {
      method: "POST",
    });
    if (!res.ok || !res.body) {
      var msg = extractApiErrorMessage(
        res.body,
        "Failed to sync paper trades from Trade Ledger."
      );
      setBacktestNotice("error", msg);
      return null;
    }
    setBacktestNotice("", "");
    await loadPaperTrades();
    var b = res.body;
    showBanner(
      "info",
      "Synced Trade Ledger to Paper Trades: " +
        Number(b.created_count || 0) +
        " created, " +
        Number(b.updated_count || 0) +
        " updated, " +
        Number(b.closed_count || 0) +
        " closed (" +
        Number(b.active_ledger_count || 0) +
        " active ledger).",
      { phase: "PAPER_TRADE_SYNC" }
    );
    return b;
  } finally {
    if (dom.backtestPaperSyncLedgerBtn) {
      dom.backtestPaperSyncLedgerBtn.disabled = false;
    }
  }
}

export async function loadPaperTrades() {
  var statusVal = dom.backtestPaperFilter ? dom.backtestPaperFilter.value : "ACTIVE";
  var url = "/api/backtest/paper-trades";
  var params = [];
  if (statusVal && statusVal !== "ALL") {
    params.push("status=" + encodeURIComponent(statusVal));
  }
  if (params.length) {
    url += "?" + params.join("&");
  }
  var res = await apiJson(url);
  if (res.ok && res.body) {
    state.backtestPaperTrades = Array.isArray(res.body.trades) ? res.body.trades : [];
    state.backtestPaperActiveCount = Number(res.body.active_count || 0);
    state.backtestPaperMaxActive = Number(res.body.max_active_trades || 120);
    renderPaperTradesList();
  }
}

export async function stopPaperTrade(tradeId) {
  if (!tradeId) return;
  var res = await apiJson("/api/backtest/paper-trades/" + encodeURIComponent(tradeId) + "/stop", {
    method: "POST",
  });
  if (!res.ok) {
    var msg = extractApiErrorMessage(res.body, "Failed to stop paper trade " + tradeId);
    setBacktestNotice("error", msg);
    return;
  }
  setBacktestNotice("", "");
  await loadPaperTrades();
  showBanner("info", "Stopped paper trade " + tradeId + ".", {
    ticker: res.body && res.body.ticker ? res.body.ticker : state.ticker,
    phase: "PAPER_TRADE",
  });
}

async function resolveLegsForPaperTrade(snapshotId) {
  var strat = dom.backtestStrategySelect ? dom.backtestStrategySelect.value : "BULL_PUT_SPREAD";
  var custom = readCustomLegsFromUI().filter(function (leg) {
    return Boolean(leg.expiry) && leg.strike > 0;
  });
  if ((strat === "CUSTOM" || state.backtestSource === "LEGS") && custom.length) {
    return { strategy_kind: strat === "CUSTOM" ? "CUSTOM" : strat, legs: custom };
  }
  if (
    Array.isArray(state.strategyLegs) &&
    state.strategyLegs.length &&
    state.strategyLoadedTicker === state.ticker &&
    (!dom.backtestStrategySelect || dom.backtestStrategySelect.value === state.strategyKind)
  ) {
    return {
      strategy_kind: state.strategyKind || strat,
      legs: state.strategyLegs.map(function (leg) {
        return {
          side: leg.side === "BUY" ? "BUY" : "SELL",
          right: leg.right === "C" ? "C" : "P",
          expiry: formatExpiryIso(leg.expiry),
          strike: Number(leg.strike),
          qty: Math.max(1, parseInt(leg.qty || 1, 10) || 1),
        };
      }),
    };
  }
  if (strat !== "CUSTOM") {
    var btDteMin = dom.backtestDteMin ? parseInt(dom.backtestDteMin.value, 10) : NaN;
    var btDteMax = dom.backtestDteMax ? parseInt(dom.backtestDteMax.value, 10) : NaN;
    var suggestBody = {
      ticker: state.ticker,
      snapshot_id: snapshotId,
      strategy_kind: strat,
      qty_multiplier: 1,
    };
    if (!Number.isNaN(btDteMin) && btDteMin >= 0) {
      suggestBody.dte_min = btDteMin;
    }
    if (!Number.isNaN(btDteMax) && btDteMax >= 1) {
      suggestBody.dte_max = btDteMax;
    }
    var suggestRes = await apiJson("/api/strategy/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(suggestBody),
    });
    var evals =
      suggestRes.ok && suggestRes.body && Array.isArray(suggestRes.body.evaluations)
        ? suggestRes.body.evaluations
        : [];
    var pickedEval = evals.length ? evals[0] : null;
    if (pickedEval && Array.isArray(pickedEval.legs) && pickedEval.legs.length) {
      return {
        strategy_kind: strat,
        legs: pickedEval.legs.map(function (leg) {
          return {
            side: leg.side,
            right: leg.right,
            expiry: formatExpiryIso(leg.expiry),
            strike: Number(leg.strike),
            qty: Math.max(1, parseInt(leg.qty || 1, 10) || 1),
          };
        }),
      };
    }
  }
  return null;
}

export async function saveCurrentPaperTrade() {
  var snapId =
    (state.backtestMode === "SINGLE_TRADE" && dom.backtestEntrySelect && dom.backtestEntrySelect.value) ||
    state.snapshotId ||
    (state.chainData && state.chainData.meta && state.chainData.meta.snapshot_id) ||
    "";
  if (!snapId) {
    setBacktestNotice("error", "Load or select a saved snapshot before saving a paper trade.");
    return;
  }
  var resolved = await resolveLegsForPaperTrade(snapId);
  if (!resolved || !resolved.legs.length) {
    setBacktestNotice("error", "Define custom legs or build a strategy in Strategy Lab before saving a paper trade.");
    return;
  }
  var qty = Math.max(1, parseInt(dom.backtestQty ? dom.backtestQty.value : "1", 10) || 1);
  var res = await apiJson("/api/backtest/paper-trades", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ticker: state.ticker,
      snapshot_id: snapId,
      strategy_kind: resolved.strategy_kind,
      legs: resolved.legs,
      qty: qty,
    }),
  });
  if (!res.ok) {
    var msg = extractApiErrorMessage(res.body, "Unable to save paper trade.");
    setBacktestNotice("error", msg);
    return;
  }
  setBacktestNotice("", "");
  await loadPaperTrades();
  showBanner(
    "info",
    "Saved paper trade " + res.body.id + " (" + resolved.strategy_kind + ").",
    { ticker: state.ticker, phase: "PAPER_TRADE" }
  );
}
