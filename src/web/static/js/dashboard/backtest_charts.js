/* IBKR Options Analyzer dashboard — Plotly Equity Curve & Single-Trade P&L chart for Backtest. */

import {
  dom,
  state,
} from "./state.js";
import {
  baseLayout,
} from "./charts.js";
import {
  themeVars,
} from "./helpers.js";

function signedDollar(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "\u2014";
  var n = Number(val);
  var absStr = Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return (n >= 0 ? "+$" : "-$") + absStr;
}

export function renderBacktestEmptyPlaceholder(messageHtml) {
  if (!dom.backtestChartEl) return;
  if (typeof window.Plotly !== "undefined" && dom.backtestChartEl.data) {
    window.Plotly.purge(dom.backtestChartEl);
  }
  dom.backtestChartEl.classList.add("is-idle");
  dom.backtestChartEl.innerHTML =
    '<div class="strategy-empty-state">' +
    (messageHtml ||
      "Configure rules and click <strong>RUN BACKTEST</strong> to simulate daily portfolio rules or replay a single trade.") +
    "</div>";
}

export function purgeBacktestChart() {
  renderBacktestEmptyPlaceholder();
}

function selectedTradeForChart(sim) {
  if (!sim || !Array.isArray(sim.trades) || !sim.trades.length) return null;
  var targetId = state.backtestSelectedTradeId;
  if (targetId) {
    for (var i = 0; i < sim.trades.length; i++) {
      if (sim.trades[i].trade_id === targetId) return sim.trades[i];
    }
  }
  return sim.trades[0];
}

function stepMarkerColor(status, t) {
  if (status === "OK") return t.call;
  if (status === "UNRELIABLE") return t.warn;
  return t.muted;
}

function renderSystematicChart(sim, t) {
  var curve = Array.isArray(sim.equity_curve) ? sim.equity_curve : [];
  if (!curve.length) {
    renderBacktestEmptyPlaceholder("No daily bars matched the selected date window.");
    return;
  }

  var xDates = [];
  var equityY = [];
  var drawdownY = [];
  var hoverTexts = [];
  for (var i = 0; i < curve.length; i++) {
    var pt = curve[i];
    xDates.push(pt.market_date);
    equityY.push(Number(pt.equity_dollars));
    drawdownY.push(-Math.abs(Number(pt.drawdown_dollars || 0)));
    var flagsStr = pt.flags && pt.flags.length ? "<br>Flags: " + pt.flags.join(", ") : "";
    hoverTexts.push(
      "<b>" +
        pt.market_date +
        "</b><br>Equity: $" +
        Number(pt.equity_dollars).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) +
        "<br>Realized: " +
        signedDollar(pt.realized_pnl_dollars) +
        "<br>Unrealized: " +
        signedDollar(pt.unrealized_pnl_dollars) +
        "<br>Drawdown: -$" +
        Math.abs(Number(pt.drawdown_dollars || 0)).toFixed(2) +
        " (" +
        Number(pt.drawdown_pct || 0).toFixed(2) +
        "%)" +
        "<br>Open trades: " +
        (pt.open_trades || 0) +
        flagsStr
    );
  }

  var eqTrace = {
    type: "scatter",
    mode: "lines+markers",
    name: "Portfolio Equity ($)",
    x: xDates,
    y: equityY,
    text: hoverTexts,
    hoverinfo: "text",
    line: { color: t.call, width: 2.5 },
    marker: { size: 5, color: t.call },
    yaxis: "y",
  };

  var ddTrace = {
    type: "bar",
    name: "Drawdown ($)",
    x: xDates,
    y: drawdownY,
    marker: { color: t.put, opacity: 0.35 },
    hovertemplate: "<b>%{x}</b><br>Drawdown: $%{y:,.2f}<extra></extra>",
    yaxis: "y2",
  };

  var initCap = sim.summary && sim.summary.initial_capital ? Number(sim.summary.initial_capital) : null;
  var layout = baseLayout(t, "Trading Date", "Portfolio Equity ($)", []);
  layout.xaxis.type = "date";
  layout.margin = { t: 34, r: 58, b: 42, l: 64 };
  layout.yaxis2 = {
    title: { text: "Drawdown ($)", font: { size: 11, color: t.muted } },
    overlaying: "y",
    side: "right",
    showgrid: false,
    zeroline: true,
    zerolinecolor: t.grid,
    tickfont: { color: t.muted },
  };
  if (initCap !== null) {
    layout.shapes.push({
      type: "line",
      xref: "paper",
      x0: 0,
      x1: 1,
      y0: initCap,
      y1: initCap,
      line: { color: t.muted, width: 1, dash: "dot" },
    });
  }

  dom.backtestChartEl.classList.remove("is-idle");
  dom.backtestChartEl.innerHTML = "";
  window.Plotly.react(dom.backtestChartEl, [ddTrace, eqTrace], layout, {
    responsive: true,
    displayModeBar: false,
  });
}

function renderSingleTradeChart(sim, t) {
  var trade = selectedTradeForChart(sim);
  if (!trade || !Array.isArray(trade.steps) || !trade.steps.length) {
    renderSystematicChart(sim, t);
    return;
  }

  var xDates = [];
  var pnlY = [];
  var spotY = [];
  var markerColors = [];
  var hoverTexts = [];
  for (var i = 0; i < trade.steps.length; i++) {
    var st = trade.steps[i];
    xDates.push(st.market_date);
    var netPnl = st.net_pnl_dollars !== null && st.net_pnl_dollars !== undefined ? Number(st.net_pnl_dollars) : null;
    pnlY.push(netPnl);
    spotY.push(st.spot !== null && st.spot !== undefined ? Number(st.spot) : null);
    markerColors.push(stepMarkerColor(st.status, t));
    var flagsStr = st.flags && st.flags.length ? "<br>Flags: " + st.flags.join(", ") : "";
    hoverTexts.push(
      "<b>" +
        st.market_date +
        "</b> (" +
        (st.ts_et || st.snapshot_id) +
        ")<br>Net P&amp;L: " +
        signedDollar(netPnl) +
        "<br>Mark: " +
        signedDollar(st.mark_value_dollars) +
        "<br>DTE: " +
        st.dte_min_leg +
        " \u2022 Status: " +
        st.status +
        flagsStr
    );
  }

  var pnlTrace = {
    type: "scatter",
    mode: "lines+markers",
    name: "Trade Net P&L ($)",
    x: xDates,
    y: pnlY,
    text: hoverTexts,
    hoverinfo: "text",
    connectgaps: false,
    line: { color: t.ink, width: 2 },
    marker: { size: 8, color: markerColors, line: { color: t.bg, width: 1 } },
    yaxis: "y",
  };

  var spotTrace = {
    type: "scatter",
    mode: "lines",
    name: "Underlying Spot ($)",
    x: xDates,
    y: spotY,
    connectgaps: true,
    line: { color: t.dtePalette[0], width: 1.5, dash: "dot" },
    hovertemplate: "<b>%{x}</b><br>Spot: $%{y:,.2f}<extra></extra>",
    yaxis: "y2",
  };

  var layout = baseLayout(t, "Trading Date", "Net P&L ($)", []);
  layout.xaxis.type = "date";
  layout.margin = { t: 34, r: 60, b: 42, l: 64 };
  layout.yaxis.zeroline = true;
  layout.yaxis.zerolinecolor = t.muted;
  layout.yaxis2 = {
    title: { text: "Spot ($)", font: { size: 11, color: t.muted } },
    overlaying: "y",
    side: "right",
    showgrid: false,
    tickfont: { color: t.muted },
  };

  dom.backtestChartEl.classList.remove("is-idle");
  dom.backtestChartEl.innerHTML = "";
  window.Plotly.react(dom.backtestChartEl, [pnlTrace, spotTrace], layout, {
    responsive: true,
    displayModeBar: false,
  });
}

export function renderBacktestChart(result, theme) {
  if (!dom.backtestChartEl || typeof window.Plotly === "undefined") return;
  if (!result || !result.simulation) {
    renderBacktestEmptyPlaceholder();
    return;
  }
  var sim = result.simulation;
  var t = theme || themeVars();
  if (sim.mode === "SINGLE_TRADE") {
    renderSingleTradeChart(sim, t);
  } else {
    renderSystematicChart(sim, t);
  }
}
