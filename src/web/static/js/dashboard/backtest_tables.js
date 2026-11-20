/* IBKR Options Analyzer dashboard — Backtest KPI strip, Trade & Legs History, & Daily Step MTM tables. */

import {
  BACKTEST_SPLIT_KEY,
  dom,
  state,
} from "./state.js";
import {
  escapeHtml,
  formatStrategyShortLabel,
  readJsonPref,
  removePref,
  themeVars,
  writeJsonPref,
} from "./helpers.js";
import {
  renderBacktestChart,
} from "./backtest_charts.js";

export function signedMoney(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "\u2014";
  var n = Number(val);
  var absStr = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return (n >= 0 ? "+$" : "-$") + absStr;
}

export function fmtPlainMoney(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "\u2014";
  return (
    "$" +
    Number(val).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })
  );
}

export function fmtPct(val, signed) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "\u2014";
  var n = Number(val);
  return (signed && n > 0 ? "+" : "") + n.toFixed(2) + "%";
}

function pnlClass(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "";
  return Number(val) >= 0 ? "is-pos" : "is-neg";
}

function fmtStrike(strike) {
  var n = Number(strike);
  if (Number.isNaN(n)) return String(strike || "");
  return Number.isInteger(n) ? String(n) : String(Number(n.toFixed(2)));
}

function formatCompactLegs(legs) {
  if (!Array.isArray(legs) || !legs.length) return "\u2014";
  var firstExpiry = legs[0].expiry || "";
  var sameExpiry = Boolean(firstExpiry);
  for (var i = 1; i < legs.length; i++) {
    if ((legs[i].expiry || "") !== firstExpiry) {
      sameExpiry = false;
      break;
    }
  }
  var parts = [];
  for (var j = 0; j < legs.length; j++) {
    var leg = legs[j];
    var sign = leg.side === "BUY" ? "+" : "-";
    var qtyPrefix = Number(leg.qty || 1) > 1 ? Number(leg.qty) + "x" : "";
    var base = sign + qtyPrefix + fmtStrike(leg.strike) + (leg.right || "");
    parts.push(sameExpiry ? base : base + " (" + (leg.expiry || "") + ")");
  }
  return sameExpiry ? parts.join(" / ") + " @ " + firstExpiry : parts.join(" / ");
}

function findExitLegMark(leg, idx, lastStep) {
  if (!lastStep || !Array.isArray(lastStep.legs) || !lastStep.legs.length) return null;
  for (var i = 0; i < lastStep.legs.length; i++) {
    var m = lastStep.legs[i];
    if (
      m.side === leg.side &&
      m.right === leg.right &&
      m.expiry === leg.expiry &&
      Math.abs(Number(m.strike) - Number(leg.strike)) < 1e-4
    ) {
      return m;
    }
  }
  return lastStep.legs.length > idx ? lastStep.legs[idx] : null;
}

function buildTradeDrawerHtml(tr, sim) {
  var legs = Array.isArray(tr.legs) ? tr.legs : [];
  var steps = Array.isArray(tr.steps) ? tr.steps : [];
  var lastStep = steps.length ? steps[steps.length - 1] : null;
  var closeMode = sim && sim.config ? sim.config.close_price_mode : "MID";

  var legRowsHtml = legs.length
    ? legs
        .map(function (leg, idx) {
          var exitMark = findExitLegMark(leg, idx, lastStep);
          var exitPriceVal = null;
          var exitSrc = "";
          if (exitMark) {
            exitPriceVal =
              closeMode === "NATURAL_SPREAD" &&
              exitMark.natural_price !== null &&
              exitMark.natural_price !== undefined
                ? exitMark.natural_price
                : exitMark.price_used;
            exitSrc = exitMark.price_source || "";
          }
          var entryDeltaStr =
            leg.delta !== null && leg.delta !== undefined ? Number(leg.delta).toFixed(2) : "\u2014";
          var sideLabel = leg.side === "SELL" ? "SHORT" : "LONG";
          var exitStr =
            exitPriceVal !== null && exitPriceVal !== undefined
              ? fmtPlainMoney(exitPriceVal) + (exitSrc ? " (" + exitSrc + ")" : "")
              : "\u2014";
          return (
            "<tr>" +
            '<td class="text-left">' +
            sideLabel +
            "</td>" +
            '<td class="text-left">' +
            escapeHtml(leg.right) +
            "</td>" +
            "<td>$" +
            escapeHtml(fmtStrike(leg.strike)) +
            "</td>" +
            '<td class="text-left">' +
            escapeHtml(leg.expiry) +
            "</td>" +
            "<td>" +
            escapeHtml(leg.qty) +
            "</td>" +
            "<td>" +
            fmtPlainMoney(leg.mid) +
            "</td>" +
            "<td>" +
            escapeHtml(entryDeltaStr) +
            "</td>" +
            "<td>" +
            escapeHtml(exitStr) +
            "</td>" +
            "</tr>"
          );
        })
        .join("")
    : '<tr><td colspan="8" class="text-left">No legs recorded.</td></tr>';

  var notes = Array.isArray(tr.selection_notes) && tr.selection_notes.length ? tr.selection_notes.slice() : [];
  if (!notes.length) {
    for (var n = 0; n < legs.length; n++) {
      if (legs[n].selection_reason) notes.push(legs[n].selection_reason);
    }
  }
  var rationaleHtml = notes.length
    ? '<ul class="backtest-rationale-list">' +
      notes
        .map(function (note) {
          return "<li>" + escapeHtml(note) + "</li>";
        })
        .join("") +
      "</ul>"
    : '<span class="backtest-submeta">Manual or custom leg entry (no automated wall/delta rationale).</span>';

  var flagsLine =
    Array.isArray(tr.flags) && tr.flags.length
      ? '<div class="backtest-submeta">Trade Flags: ' + escapeHtml(tr.flags.join(", ")) + "</div>"
      : "";

  var stockLotLine =
    Number(tr.stock_shares || 0) > 0
      ? '<div class="backtest-submeta">Synthetic Stock: ' +
        escapeHtml(tr.stock_shares) +
        " shs @ " +
        fmtPlainMoney(tr.stock_cost_basis) +
        (tr.stock_exit_price !== null && tr.stock_exit_price !== undefined
          ? " \u2192 Exit " + fmtPlainMoney(tr.stock_exit_price)
          : " (Held)") +
        (tr.option_net_pnl_dollars !== null && tr.option_net_pnl_dollars !== undefined
          ? " \u2022 Opt " + signedMoney(tr.option_net_pnl_dollars)
          : "") +
        (tr.stock_pnl_dollars !== null && tr.stock_pnl_dollars !== undefined
          ? " / Stk " + signedMoney(tr.stock_pnl_dollars)
          : "") +
        "</div>"
      : "";

  return (
    '<tr class="backtest-trade-detail-row" data-trade-detail-for="' +
    escapeHtml(tr.trade_id) +
    '">' +
    '<td colspan="10">' +
    '<div class="backtest-drawer">' +
    '<div class="backtest-drawer-section">' +
    '<span class="backtest-drawer-title">Legs Breakdown (' +
    escapeHtml(tr.trade_id) +
    " \u2022 Qty " +
    escapeHtml(tr.qty) +
    (tr.status === "OPEN" ? " \u2022 Latest MTM" : " \u2022 Exit Mark") +
    ")</span>" +
    stockLotLine +
    '<table class="backtest-legs-subtable">' +
    "<thead><tr>" +
    '<th class="text-left">Side</th>' +
    '<th class="text-left">Right</th>' +
    "<th>Strike</th>" +
    '<th class="text-left">Expiry</th>' +
    "<th>Qty</th>" +
    "<th>Entry Price</th>" +
    "<th>Entry Delta</th>" +
    "<th>" +
    (tr.status === "OPEN" ? "Latest Mark" : "Exit Price") +
    "</th>" +
    "</tr></thead>" +
    "<tbody>" +
    legRowsHtml +
    "</tbody>" +
    "</table>" +
    "</div>" +
    '<div class="backtest-drawer-section">' +
    '<span class="backtest-drawer-title">Entry Selection Rationale</span>' +
    rationaleHtml +
    flagsLine +
    "</div>" +
    "</div>" +
    "</td>" +
    "</tr>"
  );
}

var SKIP_REASON_LABELS = {
  BELOW_MIN_CREDIT: "min credit",
  BELOW_MIN_ROR: "min RoR",
  BELOW_MIN_CREDIT_AND_ROR: "credit & RoR",
  BELOW_COST_BASIS_FLOOR: "cost floor",
  NO_EXPIRY_IN_DTE_WINDOW: "DTE window",
  EMPTY_CHAIN: "empty chain",
  INVALID_SPOT: "invalid spot",
  MISSING_CONTRACT: "missing leg",
  UNUSABLE_ENTRY_PRICE: "no quote",
  NO_OTM_PUT_CANDIDATES: "no OTM puts",
  NO_OTM_CALL_CANDIDATES: "no OTM calls",
  NO_CALL_CONTRACTS: "no calls",
  NO_PUT_CONTRACTS: "no puts",
  INSUFFICIENT_WINGS: "no wings",
  NON_POSITIVE_CREDIT: "<=0 credit",
  NON_POSITIVE_MAX_LOSS: "<=0 max loss",
  STRATEGY_BUILD_ERROR: "build err",
  ALREADY_SETTLED: "settled",
};

function formatSkippedReasons(byReason) {
  if (!byReason || typeof byReason !== "object") {
    return { sub: "No entries skipped", title: "" };
  }
  var keys = Object.keys(byReason).sort(function (a, b) {
    return (Number(byReason[b]) || 0) - (Number(byReason[a]) || 0) || a.localeCompare(b);
  });
  if (!keys.length) {
    return { sub: "No entries skipped", title: "" };
  }
  var shortParts = [];
  var fullParts = [];
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i];
    var cnt = Number(byReason[k]) || 0;
    var label = SKIP_REASON_LABELS[k] || k.toLowerCase().replace(/_/g, " ");
    fullParts.push(k + ": " + cnt);
    if (i < 2) {
      shortParts.push(cnt + " " + label);
    }
  }
  if (keys.length > 2) {
    shortParts.push("+" + (keys.length - 2) + " more");
  }
  return {
    sub: shortParts.join(" \u2022 "),
    title: fullParts.join(", "),
  };
}

export function toggleBacktestTradeExpand(tradeId) {
  if (!tradeId) return;
  if (state.backtestExpandedTradeIds[tradeId]) {
    delete state.backtestExpandedTradeIds[tradeId];
  } else {
    state.backtestExpandedTradeIds[tradeId] = true;
  }
}

export function renderBacktestKpis(sim) {
  if (!dom.backtestKpis) return;
  var s = sim && sim.summary ? sim.summary : null;
  if (!s) {
    dom.backtestKpis.innerHTML = "";
    return;
  }
  var closedCount = Number(s.closed_trades || 0);
  var openCount = Number(s.open_trades || 0);
  var winRateStr =
    closedCount > 0 && s.win_rate_pct !== null && s.win_rate_pct !== undefined
      ? Number(s.win_rate_pct).toFixed(1) + "%"
      : "\u2014";
  var pfStr =
    closedCount > 0 && s.profit_factor !== null && s.profit_factor !== undefined
      ? Number(s.profit_factor).toFixed(2)
      : "\u2014";
  var expStr =
    closedCount > 0 && s.expectancy_dollars !== null && s.expectancy_dollars !== undefined
      ? signedMoney(s.expectancy_dollars)
      : "\u2014";
  var skippedCount = Number(s.skipped_entries_count || 0);
  var skippedInfo = formatSkippedReasons(s.skipped_by_reason);
  var realizedVal = Number(s.realized_pnl_dollars || 0);
  var unrealizedVal = Number(s.unrealized_pnl_dollars || 0);
  var hasOpenExposure = openCount > 0 || Math.abs(unrealizedVal) > 1e-6;
  var realizedOpenValue = hasOpenExposure
    ? signedMoney(realizedVal) + " / " + signedMoney(unrealizedVal)
    : signedMoney(realizedVal);
  var realizedOpenCls = hasOpenExposure
    ? pnlClass(s.net_pnl_dollars)
    : closedCount > 0
      ? pnlClass(realizedVal)
      : "";

  var tiles = [
    {
      label: "Final Equity",
      value: fmtPlainMoney(s.final_equity),
      sub: "Return " + fmtPct(s.total_return_pct, true) + " (Start " + fmtPlainMoney(s.initial_capital) + ")",
      cls: pnlClass(s.net_pnl_dollars),
    },
    {
      label: "Net P&L",
      value: signedMoney(s.net_pnl_dollars),
      sub:
        "Gross " +
        signedMoney(s.gross_pnl_dollars) +
        " \u2022 Fees $" +
        Number(s.total_commissions_dollars || 0).toFixed(2),
      cls: pnlClass(s.net_pnl_dollars),
    },
    {
      label: hasOpenExposure ? "Realized / Open MTM" : "Realized P&L",
      value: realizedOpenValue,
      sub: closedCount + " closed \u2022 " + openCount + " open",
      cls: realizedOpenCls,
    },
    {
      label: "Trades / Win Rate",
      value: s.trade_count + " \u2022 " + winRateStr,
      sub:
        s.winning_trades +
        "W / " +
        s.losing_trades +
        "L (" +
        closedCount +
        " closed, " +
        openCount +
        " open)",
      cls: "",
    },
    {
      label: "Profit Factor",
      value: pfStr,
      sub: "Expectancy " + expStr,
      cls: "",
    },
    {
      label: "Max Drawdown",
      value: "-$" + Math.abs(Number(s.max_drawdown_dollars || 0)).toFixed(2),
      sub: fmtPct(-Math.abs(Number(s.max_drawdown_pct || 0)), false),
      cls: Number(s.max_drawdown_dollars || 0) > 0 ? "is-neg" : "",
    },
    {
      label: "Skipped Entries",
      value: String(skippedCount),
      sub: skippedCount > 0 ? skippedInfo.sub : "No entries skipped",
      title: skippedInfo.title,
      cls: skippedCount > 0 ? "is-warn" : "",
    },
    {
      label: "Bar Quality",
      value: s.ok_bars + " OK / " + s.flagged_bars + " flagged",
      sub: sim.config.price_basis + " \u2022 " + sim.config.close_price_mode,
      cls: "",
    },
  ];
  dom.backtestKpis.innerHTML = tiles
    .map(function (k) {
      var titleAttr = k.title ? ' title="' + escapeHtml(k.title) + '"' : "";
      return (
        '<div class="backtest-kpi"' +
        titleAttr +
        ">" +
        '<span class="backtest-kpi__label">' +
        escapeHtml(k.label) +
        "</span>" +
        '<strong class="backtest-kpi__value ' +
        k.cls +
        '">' +
        escapeHtml(k.value) +
        "</strong>" +
        '<span class="backtest-kpi__sub">' +
        escapeHtml(k.sub) +
        "</span>" +
        "</div>"
      );
    })
    .join("");
}

function formatStepLegMarks(step) {
  var legs = Array.isArray(step.legs) ? step.legs : [];
  if (!legs.length) return "\u2014";
  return legs
    .map(function (leg) {
      var px =
        leg.price_used !== null && leg.price_used !== undefined
          ? "$" + Number(leg.price_used).toFixed(2)
          : "\u2014";
      return fmtStrike(leg.strike) + (leg.right || "") + ":" + px;
    })
    .join(" / ");
}

export function renderTradeSteps(trade) {
  if (!dom.backtestStepsBody) return;
  if (!trade || !Array.isArray(trade.steps) || !trade.steps.length) {
    if (dom.backtestStepsTitle) {
      dom.backtestStepsTitle.textContent = "Daily Mark-to-Market Step History";
    }
    if (dom.backtestStepsMeta) dom.backtestStepsMeta.textContent = "No steps available";
    dom.backtestStepsBody.innerHTML =
      '<tr><td colspan="7" class="text-left">No daily mark-to-market steps recorded for this trade.</td></tr>';
    return;
  }
  if (dom.backtestStepsTitle) {
    dom.backtestStepsTitle.textContent = "Daily Mark-to-Market Step History \u2022 " + trade.trade_id;
  }
  if (dom.backtestStepsMeta) {
    var statusTag = trade.status === "OPEN" ? "OPEN (Unrealized)" : trade.exit_reason;
    dom.backtestStepsMeta.textContent =
      trade.steps.length +
      (trade.steps.length === 1 ? " bar" : " bars") +
      " \u2022 " +
      trade.strategy_kind +
      " \u2022 " +
      statusTag;
  }

  var prevValidNet = null;
  var lastIdx = trade.steps.length - 1;
  dom.backtestStepsBody.innerHTML = trade.steps
    .map(function (st, idx) {
      var isMissing =
        Boolean(st.quote_missing) ||
        st.net_pnl_dollars === null ||
        st.net_pnl_dollars === undefined;
      var netVal = isMissing ? null : Number(st.net_pnl_dollars);
      var dailyVal = null;
      if (!isMissing) {
        dailyVal = prevValidNet === null ? netVal : netVal - prevValidNet;
        prevValidNet = netVal;
      }

      var pctStr =
        !isMissing && st.pnl_pct_of_basis !== null && st.pnl_pct_of_basis !== undefined
          ? fmtPct(st.pnl_pct_of_basis, true)
          : "\u2014";
      var spotStr =
        st.spot !== null && st.spot !== undefined
          ? "$" + Number(st.spot).toFixed(2)
          : "\u2014";
      var spotSub = st.spot_source ? '<span class="backtest-submeta">' + escapeHtml(st.spot_source) + "</span>" : "";

      var markSrc = st.mark_source || (isMissing ? "MISSING" : "N/A");
      var sourceBadge =
        '<span class="backtest-pill backtest-pill--source">' + escapeHtml(markSrc) + "</span>";
      var missingBadge = isMissing
        ? '<span class="backtest-pill backtest-pill--missing">MISSING QUOTE</span>'
        : "";
      var marksStr = formatStepLegMarks(st);

      var exitHtml = "";
      if (idx === lastIdx) {
        if (trade.status === "OPEN") {
          exitHtml = '<span class="backtest-pill backtest-pill--open">OPEN MTM</span>';
        } else if (trade.status === "CLOSED") {
          exitHtml =
            '<span class="backtest-pill backtest-pill--closed">' +
            escapeHtml(trade.exit_reason) +
            "</span>";
        } else {
          exitHtml =
            '<span class="backtest-pill backtest-pill--unresolved">' +
            escapeHtml(trade.status) +
            "</span>";
        }
      } else {
        exitHtml =
          '<span class="backtest-status backtest-status--' +
          escapeHtml(st.status) +
          '">' +
          escapeHtml(st.status) +
          "</span>";
      }
      var flagsSub =
        st.flags && st.flags.length
          ? '<span class="backtest-submeta">' + escapeHtml(st.flags.join(", ")) + "</span>"
          : "";

      var stkStepLine =
        Number(st.stock_shares || 0) > 0 &&
        st.stock_unrealized_pnl_dollars !== null &&
        st.stock_unrealized_pnl_dollars !== undefined
          ? '<span class="backtest-submeta">Stk MTM ' +
            signedMoney(st.stock_unrealized_pnl_dollars) +
            "</span>"
          : "";

      return (
        "<tr>" +
        '<td class="text-left" title="' +
        escapeHtml(st.snapshot_id) +
        '">' +
        escapeHtml(st.market_date) +
        '<span class="backtest-submeta">' +
        escapeHtml(st.ts_et || st.snapshot_id) +
        "</span></td>" +
        "<td>" +
        escapeHtml(st.dte_min_leg) +
        "d</td>" +
        "<td>" +
        escapeHtml(spotStr) +
        spotSub +
        "</td>" +
        '<td class="text-left"><div class="backtest-step-marks"><div class="backtest-step-marks-head">' +
        sourceBadge +
        missingBadge +
        '</div><span class="backtest-submeta">' +
        escapeHtml(marksStr) +
        "</span></div></td>" +
        '<td class="backtest-pnl ' +
        pnlClass(dailyVal) +
        '">' +
        signedMoney(dailyVal) +
        "</td>" +
        '<td class="backtest-pnl ' +
        pnlClass(netVal) +
        '">' +
        signedMoney(netVal) +
        '<span class="backtest-submeta">' +
        escapeHtml(pctStr) +
        "</span>" +
        stkStepLine +
        "</td>" +
        '<td class="text-left">' +
        exitHtml +
        flagsSub +
        "</td>" +
        "</tr>"
      );
    })
    .join("");
}

export function renderTradeLog(sim) {
  if (!dom.backtestTradesBody) return;
  var trades = sim && Array.isArray(sim.trades) ? sim.trades : [];
  if (dom.backtestTradesMeta) {
    dom.backtestTradesMeta.textContent =
      trades.length +
      (trades.length === 1 ? " trade" : " trades") +
      " \u2022 click row to plot/inspect \u2022 \u25B6 for legs & rationale";
  }
  if (!trades.length) {
    dom.backtestTradesBody.innerHTML =
      '<tr><td colspan="10" class="text-left">No simulated trades entered in this window.</td></tr>';
    renderTradeSteps(null);
    return;
  }

  var selected = null;
  for (var k = 0; k < trades.length; k++) {
    if (trades[k].trade_id === state.backtestSelectedTradeId) {
      selected = trades[k];
      break;
    }
  }
  if (!selected) {
    selected = trades[0];
    state.backtestSelectedTradeId = selected.trade_id;
  }

  dom.backtestTradesBody.innerHTML = trades
    .map(function (tr) {
      var isSel = tr.trade_id === selected.trade_id;
      var isExpanded = Boolean(state.backtestExpandedTradeIds[tr.trade_id]);
      var legsSummary = formatCompactLegs(tr.legs);
      var statusCellHtml = "";
      if (tr.status === "OPEN") {
        statusCellHtml =
          '<span class="backtest-pill backtest-pill--open">OPEN</span>' +
          '<span class="backtest-submeta">MTM ' +
          escapeHtml(tr.exit_date) +
          "</span>";
      } else if (tr.status === "UNRESOLVED") {
        statusCellHtml =
          '<span class="backtest-pill backtest-pill--unresolved">UNRESOLVED</span>' +
          '<span class="backtest-submeta">' +
          escapeHtml(tr.exit_date) +
          "</span>";
      } else {
        statusCellHtml =
          escapeHtml(tr.exit_date) +
          '<span class="backtest-submeta">CLOSED \u2022 ' +
          escapeHtml(tr.days_held) +
          "d</span>";
      }
      var pnlTag = tr.status === "OPEN" ? "Unrealized" : "Realized";
      if (
        tr.option_net_pnl_dollars !== null &&
        tr.option_net_pnl_dollars !== undefined &&
        (Number(tr.stock_shares || 0) > 0 ||
          (tr.stock_pnl_dollars !== null && tr.stock_pnl_dollars !== undefined))
      ) {
        var hasStkClose =
          tr.stock_exit_price !== null && tr.stock_exit_price !== undefined;
        var hasStkMtm =
          tr.stock_pnl_dollars !== null &&
          tr.stock_pnl_dollars !== undefined &&
          (Math.abs(Number(tr.stock_pnl_dollars)) > 1e-6 || hasStkClose || tr.status === "OPEN");
        pnlTag = hasStkMtm
          ? "Opt " + signedMoney(tr.option_net_pnl_dollars) + " / Stk " + signedMoney(tr.stock_pnl_dollars)
          : "Opt " +
            signedMoney(tr.option_net_pnl_dollars) +
            " \u2022 " +
            escapeHtml(tr.stock_shares) +
            " shs held";
      }
      var phaseMeta = "";
      if (tr.wheel_phase || Number(tr.stock_shares || 0) > 0) {
        var parts = [];
        if (tr.wheel_phase) parts.push(tr.wheel_phase);
        if (Number(tr.stock_shares || 0) > 0 && tr.stock_cost_basis !== null && tr.stock_cost_basis !== undefined) {
          parts.push(tr.stock_shares + " shs @ $" + fmtStrike(tr.stock_cost_basis));
        }
        phaseMeta = '<span class="backtest-submeta">' + escapeHtml(parts.join(" \u2022 ")) + "</span>";
      }
      var rowHtml =
        '<tr class="backtest-trade-row' +
        (isSel ? " is-selected" : "") +
        '" data-trade-id="' +
        escapeHtml(tr.trade_id) +
        '">' +
        '<td class="backtest-expand-col text-left">' +
        '<button type="button" class="backtest-expand-btn" data-bt-trade-toggle="' +
        escapeHtml(tr.trade_id) +
        '" aria-expanded="' +
        (isExpanded ? "true" : "false") +
        '" aria-label="Toggle legs and selection rationale">' +
        (isExpanded ? "\u25BC" : "\u25B6") +
        "</button></td>" +
        '<td class="text-left"><strong>' +
        escapeHtml(tr.trade_id) +
        '</strong><span class="backtest-submeta">' +
        escapeHtml(tr.qty) +
        "x</span></td>" +
        '<td class="text-left" title="' +
        escapeHtml(tr.strategy_kind) +
        '">' +
        escapeHtml(formatStrategyShortLabel(tr.strategy_kind)) +
        phaseMeta +
        "</td>" +
        '<td class="text-left">' +
        escapeHtml(tr.entry_date) +
        "</td>" +
        '<td class="text-left">' +
        statusCellHtml +
        "</td>" +
        '<td class="text-left">' +
        escapeHtml(tr.exit_reason) +
        "</td>" +
        '<td class="text-left backtest-legs-summary" title="' +
        escapeHtml(legsSummary) +
        '">' +
        escapeHtml(legsSummary) +
        "</td>" +
        "<td>" +
        signedMoney(tr.entry_cash_mid_dollars) +
        "</td>" +
        '<td class="backtest-pnl ' +
        pnlClass(tr.net_pnl_dollars) +
        '">' +
        signedMoney(tr.net_pnl_dollars) +
        '<span class="backtest-submeta">' +
        pnlTag +
        "</span></td>" +
        '<td class="backtest-pnl ' +
        pnlClass(tr.return_on_risk_pct) +
        '" title="' +
        escapeHtml(
          tr.return_on_basis_pct !== null && tr.return_on_basis_pct !== undefined
            ? "Return on basis: " + fmtPct(tr.return_on_basis_pct, true)
            : ""
        ) +
        '">' +
        fmtPct(tr.return_on_risk_pct, true) +
        "</td>" +
        "</tr>";

      if (isExpanded) {
        rowHtml += buildTradeDrawerHtml(tr, sim);
      }
      return rowHtml;
    })
    .join("");
  renderTradeSteps(selected);
}

function deduplicateStrings(items) {
  var seen = {};
  var out = [];
  for (var i = 0; i < items.length; i++) {
    var txt = String(items[i] || "").trim();
    if (!txt || seen[txt]) continue;
    seen[txt] = true;
    out.push(txt);
  }
  return out;
}

export function renderBacktestResult(body) {
  state.backtestResult = body;
  var sim = body.simulation;
  var notices = deduplicateStrings(
    []
      .concat(Array.isArray(body.warnings) ? body.warnings : [])
      .concat(sim && Array.isArray(sim.notes) ? sim.notes : [])
  );
  if (dom.backtestWarnings) {
    dom.backtestWarnings.innerHTML = notices
      .map(function (msg) {
        return '<div class="backtest-warning">' + escapeHtml(msg) + "</div>";
      })
      .join("");
  }
  if (dom.backtestStatusBadge && sim && sim.summary) {
    dom.backtestStatusBadge.className = "badge badge--info";
    dom.backtestStatusBadge.textContent =
      sim.summary.trade_count + " TRADES \u2022 " + body.daily_bar_count + " BARS";
  }
  renderBacktestKpis(sim);
  renderTradeLog(sim);
  renderBacktestChart(body, themeVars());
  if (dom.backtestAssumptions) {
    var assump = sim && Array.isArray(sim.assumptions) ? sim.assumptions : [];
    dom.backtestAssumptions.innerHTML = assump
      .map(function (line) {
        return "<li>" + escapeHtml(line) + "</li>";
      })
      .join("");
  }
}

export var DEFAULT_BACKTEST_SPLIT_PCT = 62;
export var MIN_BACKTEST_SPLIT_PCT = 40;
export var MAX_BACKTEST_SPLIT_PCT = 78;

export function clampBacktestSplitPct(pct) {
  var num = Number(pct);
  if (!Number.isFinite(num)) return DEFAULT_BACKTEST_SPLIT_PCT;
  var clamped = Math.max(MIN_BACKTEST_SPLIT_PCT, Math.min(MAX_BACKTEST_SPLIT_PCT, num));
  return Math.round(clamped * 10) / 10;
}

export function computeBacktestSplitPct(clientX, gridRect) {
  if (!gridRect || !(gridRect.width > 0) || !Number.isFinite(Number(clientX))) {
    return DEFAULT_BACKTEST_SPLIT_PCT;
  }
  var raw = ((Number(clientX) - Number(gridRect.left || 0)) / Number(gridRect.width)) * 100;
  return clampBacktestSplitPct(raw);
}

export function readBacktestSplitPref() {
  var parsed = readJsonPref(BACKTEST_SPLIT_KEY, null);
  if (parsed && typeof parsed === "object" && parsed.leftPct !== undefined) {
    return clampBacktestSplitPct(parsed.leftPct);
  }
  return DEFAULT_BACKTEST_SPLIT_PCT;
}

export function writeBacktestSplitPref(pct) {
  var clamped = clampBacktestSplitPct(pct);
  writeJsonPref(BACKTEST_SPLIT_KEY, { leftPct: clamped });
  return clamped;
}

export function clearBacktestSplitPref() {
  removePref(BACKTEST_SPLIT_KEY);
}

export function applyBacktestSplitPct(pct) {
  var clamped = clampBacktestSplitPct(pct);
  state.backtestSplitPct = clamped;
  if (dom.backtestTablesGrid && dom.backtestTablesGrid.style) {
    if (typeof dom.backtestTablesGrid.style.setProperty === "function") {
      dom.backtestTablesGrid.style.setProperty("--bt-trades-col", clamped + "%");
    } else {
      dom.backtestTablesGrid.style["--bt-trades-col"] = clamped + "%";
    }
  }
  if (dom.backtestTablesSplitter) {
    dom.backtestTablesSplitter.setAttribute("aria-valuenow", String(Math.round(clamped)));
  }
  return clamped;
}

export function initBacktestTablesSplitter() {
  var saved = readBacktestSplitPref();
  applyBacktestSplitPct(saved);

  var splitter = dom.backtestTablesSplitter;
  var grid = dom.backtestTablesGrid;
  if (!splitter || !grid) return;
  if (splitter.dataset && splitter.dataset.btSplitterWired === "1") return;
  if (splitter.dataset) splitter.dataset.btSplitterWired = "1";

  var dragState = null;

  splitter.addEventListener("pointerdown", function (ev) {
    if (ev.button !== undefined && ev.button !== 0) return;
    if (ev.preventDefault) ev.preventDefault();
    var rect = grid.getBoundingClientRect ? grid.getBoundingClientRect() : null;
    dragState = {
      pointerId: ev.pointerId,
      rect: rect,
    };
    splitter.classList.add("is-dragging");
    grid.classList.add("is-dragging");
    if (splitter.setPointerCapture && ev.pointerId !== undefined) {
      try {
        splitter.setPointerCapture(ev.pointerId);
      } catch (_err) {
        /* ignore synthetic events */
      }
    }
  });

  splitter.addEventListener("pointermove", function (ev) {
    if (!dragState) return;
    if (
      ev.pointerId !== undefined &&
      dragState.pointerId !== undefined &&
      ev.pointerId !== dragState.pointerId
    ) {
      return;
    }
    var rect = grid.getBoundingClientRect ? grid.getBoundingClientRect() : dragState.rect;
    var nextPct = computeBacktestSplitPct(ev.clientX, rect);
    applyBacktestSplitPct(nextPct);
  });

  function finishDrag(ev) {
    if (!dragState) return;
    if (
      ev &&
      ev.pointerId !== undefined &&
      dragState.pointerId !== undefined &&
      ev.pointerId !== dragState.pointerId
    ) {
      return;
    }
    var pid = dragState.pointerId;
    dragState = null;
    splitter.classList.remove("is-dragging");
    grid.classList.remove("is-dragging");
    if (splitter.releasePointerCapture && pid !== undefined) {
      try {
        if (!splitter.hasPointerCapture || splitter.hasPointerCapture(pid)) {
          splitter.releasePointerCapture(pid);
        }
      } catch (_err) {
        /* ignore */
      }
    }
    writeBacktestSplitPref(state.backtestSplitPct);
  }

  splitter.addEventListener("pointerup", finishDrag);
  splitter.addEventListener("pointercancel", finishDrag);

  splitter.addEventListener("dblclick", function () {
    applyBacktestSplitPct(DEFAULT_BACKTEST_SPLIT_PCT);
    clearBacktestSplitPref();
  });

  splitter.addEventListener("keydown", function (ev) {
    var key = ev.key;
    if (key === "ArrowLeft") {
      if (ev.preventDefault) ev.preventDefault();
      var nextLeft = applyBacktestSplitPct(
        (state.backtestSplitPct || DEFAULT_BACKTEST_SPLIT_PCT) - 2
      );
      writeBacktestSplitPref(nextLeft);
    } else if (key === "ArrowRight") {
      if (ev.preventDefault) ev.preventDefault();
      var nextRight = applyBacktestSplitPct(
        (state.backtestSplitPct || DEFAULT_BACKTEST_SPLIT_PCT) + 2
      );
      writeBacktestSplitPref(nextRight);
    } else if (key === "Home") {
      if (ev.preventDefault) ev.preventDefault();
      writeBacktestSplitPref(applyBacktestSplitPct(MIN_BACKTEST_SPLIT_PCT));
    } else if (key === "End") {
      if (ev.preventDefault) ev.preventDefault();
      writeBacktestSplitPref(applyBacktestSplitPct(MAX_BACKTEST_SPLIT_PCT));
    } else if (key === "Enter" || key === " ") {
      if (ev.preventDefault) ev.preventDefault();
      applyBacktestSplitPct(DEFAULT_BACKTEST_SPLIT_PCT);
      clearBacktestSplitPref();
    }
  });
}
