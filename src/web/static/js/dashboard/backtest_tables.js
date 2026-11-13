/* IBKR Options Analyzer dashboard — Backtest KPI strip, Trade Log & Daily Step Valuation tables. */

import {
  dom,
  state,
} from "./state.js";
import {
  escapeHtml,
  themeVars,
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

export function renderBacktestKpis(sim) {
  if (!dom.backtestKpis) return;
  var s = sim && sim.summary ? sim.summary : null;
  if (!s) {
    dom.backtestKpis.innerHTML = "";
    return;
  }
  var winRateStr =
    s.win_rate_pct !== null && s.win_rate_pct !== undefined ? s.win_rate_pct.toFixed(1) + "%" : "\u2014";
  var pfStr =
    s.profit_factor !== null && s.profit_factor !== undefined ? Number(s.profit_factor).toFixed(2) : "\u2014";
  var expStr =
    s.expectancy_dollars !== null && s.expectancy_dollars !== undefined
      ? signedMoney(s.expectancy_dollars)
      : "\u2014";
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
      sub: "Gross " + signedMoney(s.gross_pnl_dollars) + " \u2022 Fees $" + Number(s.total_commissions_dollars || 0).toFixed(2),
      cls: pnlClass(s.net_pnl_dollars),
    },
    {
      label: "Trades / Win Rate",
      value: s.trade_count + " \u2022 " + winRateStr,
      sub: s.winning_trades + "W / " + s.losing_trades + "L (" + s.open_trades + " open)",
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
      label: "Bar Quality",
      value: s.ok_bars + " OK / " + s.flagged_bars + " flagged",
      sub: sim.config.price_basis + " \u2022 " + sim.config.close_price_mode,
      cls: "",
    },
  ];
  dom.backtestKpis.innerHTML = tiles
    .map(function (k) {
      return (
        '<div class="backtest-kpi">' +
        '<span class="backtest-kpi__label">' + escapeHtml(k.label) + "</span>" +
        '<strong class="backtest-kpi__value ' + k.cls + '">' + escapeHtml(k.value) + "</strong>" +
        '<span class="backtest-kpi__sub">' + escapeHtml(k.sub) + "</span>" +
        "</div>"
      );
    })
    .join("");
}

export function renderTradeSteps(trade) {
  if (!dom.backtestStepsBody) return;
  if (!trade || !Array.isArray(trade.steps) || !trade.steps.length) {
    if (dom.backtestStepsTitle) dom.backtestStepsTitle.textContent = "Daily Valuation & Flags";
    if (dom.backtestStepsMeta) dom.backtestStepsMeta.textContent = "No steps available";
    dom.backtestStepsBody.innerHTML =
      '<tr><td colspan="9" class="text-left">No daily steps recorded for this trade.</td></tr>';
    return;
  }
  if (dom.backtestStepsTitle) dom.backtestStepsTitle.textContent = "Daily Valuation \u2022 " + trade.trade_id;
  if (dom.backtestStepsMeta) {
    dom.backtestStepsMeta.textContent =
      trade.steps.length + " bars \u2022 " + trade.strategy_kind + " \u2022 " + trade.exit_reason;
  }
  dom.backtestStepsBody.innerHTML = trade.steps
    .map(function (st) {
      var pctStr =
        st.pnl_pct_of_basis !== null && st.pnl_pct_of_basis !== undefined
          ? fmtPct(st.pnl_pct_of_basis, true)
          : "\u2014";
      var spotStr =
        st.spot !== null && st.spot !== undefined
          ? "$" + Number(st.spot).toFixed(2) + (st.spot_source ? " (" + st.spot_source + ")" : "")
          : "\u2014";
      var flagsStr = st.flags && st.flags.length ? st.flags.join(", ") : "\u2014";
      return (
        "<tr>" +
        '<td class="text-left" title="' + escapeHtml(st.snapshot_id) + '">' +
        escapeHtml(st.market_date + " " + (st.ts_et || "")) +
        "</td>" +
        "<td>" + escapeHtml(st.dte_min_leg) + "d</td>" +
        "<td>" + escapeHtml(spotStr) + "</td>" +
        "<td>" + fmtPlainMoney(st.mark_value_dollars) + "</td>" +
        "<td>" + fmtPlainMoney(st.natural_close_value_dollars) + "</td>" +
        '<td class="backtest-pnl ' + pnlClass(st.net_pnl_dollars) + '">' + signedMoney(st.net_pnl_dollars) + "</td>" +
        '<td class="backtest-pnl ' + pnlClass(st.pnl_pct_of_basis) + '">' + escapeHtml(pctStr) + "</td>" +
        '<td class="text-left backtest-status backtest-status--' + escapeHtml(st.status) + '">' +
        escapeHtml(st.status) +
        "</td>" +
        '<td class="backtest-flags-cell">' + escapeHtml(flagsStr) + "</td>" +
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
      " \u2022 click a row to inspect daily steps";
  }
  if (!trades.length) {
    dom.backtestTradesBody.innerHTML =
      '<tr><td colspan="8" class="text-left">No simulated trades entered in this window.</td></tr>';
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
      return (
        '<tr class="backtest-trade-row' + (isSel ? " is-selected" : "") + '" data-trade-id="' + escapeHtml(tr.trade_id) + '">' +
        '<td class="text-left">' + escapeHtml(tr.trade_id) + "</td>" +
        '<td class="text-left">' + escapeHtml(tr.entry_date + " \u2192 " + tr.exit_date) + "</td>" +
        '<td class="text-left">' + escapeHtml(tr.exit_reason) + "</td>" +
        "<td>" + escapeHtml(tr.qty) + "</td>" +
        "<td>" + signedMoney(tr.entry_cash_mid_dollars) + "</td>" +
        '<td class="backtest-pnl ' + pnlClass(tr.net_pnl_dollars) + '">' + signedMoney(tr.net_pnl_dollars) + "</td>" +
        '<td class="backtest-pnl ' + pnlClass(tr.return_on_basis_pct) + '">' + fmtPct(tr.return_on_basis_pct, true) + "</td>" +
        '<td class="text-left backtest-status backtest-status--' + escapeHtml(tr.status) + '">' +
        escapeHtml(tr.status) +
        "</td>" +
        "</tr>"
      );
    })
    .join("");
  renderTradeSteps(selected);
}

export function renderBacktestResult(body) {
  state.backtestResult = body;
  var sim = body.simulation;
  var notices = []
    .concat(Array.isArray(body.warnings) ? body.warnings : [])
    .concat(sim && Array.isArray(sim.notes) ? sim.notes : []);
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
