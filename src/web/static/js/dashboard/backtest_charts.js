/* IBKR Options Analyzer dashboard — Plotly Equity Curve & Interactive Trade P&L chart for Backtest. */

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

function fmtShortDollar(val) {
  var n = Number(val);
  if (Number.isNaN(n)) return "\u2014";
  return "$" + Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function shiftIsoDate(isoStr, deltaDays) {
  var parts = String(isoStr || "").split("-");
  if (parts.length !== 3) return isoStr;
  var dt = new Date(Date.UTC(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]) + deltaDays));
  if (Number.isNaN(dt.getTime())) return isoStr;
  return dt.toISOString().slice(0, 10);
}

function setChartCaption(text) {
  if (dom.backtestChartCaption) {
    dom.backtestChartCaption.textContent = text;
  }
}

function plotBacktestChart(traces, layout) {
  dom.backtestChartEl.classList.remove("is-idle");
  if (!dom.backtestChartEl.data) {
    dom.backtestChartEl.innerHTML = "";
  }
  window.Plotly.react(dom.backtestChartEl, traces, layout, {
    responsive: true,
    displayModeBar: false,
  });
}

export function renderBacktestEmptyPlaceholder(messageHtml) {
  if (!dom.backtestChartEl) return;
  if (typeof window.Plotly !== "undefined") {
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
  setChartCaption("Portfolio Equity & Drawdown");
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

function makeHorizontalRefLine(yVal, label, color, dash, t) {
  return {
    shape: {
      type: "line",
      xref: "paper",
      x0: 0,
      x1: 1,
      yref: "y",
      y0: yVal,
      y1: yVal,
      line: { color: color, width: 1.4, dash: dash || "dash" },
    },
    annotation: label
      ? {
          xref: "paper",
          x: 0.995,
          xanchor: "right",
          yref: "y",
          y: yVal,
          yanchor: yVal >= 0 ? "bottom" : "top",
          yshift: yVal >= 0 ? 2 : -2,
          text: "<b>" + label + "</b>",
          showarrow: false,
          font: { size: 10, color: color, family: t.fontMono },
          bgcolor: t.bg,
          bordercolor: color,
          borderwidth: 1,
          borderpad: 2,
        }
      : null,
  };
}

function renderSystematicChart(sim, t) {
  var curve = Array.isArray(sim.equity_curve) ? sim.equity_curve : [];
  if (!curve.length) {
    setChartCaption("Portfolio Equity & Drawdown");
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
        Number(pt.equity_dollars).toLocaleString("en-US", {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        }) +
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
    marker: { size: xDates.length === 1 ? 10 : 5, color: t.call },
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

  var sel = selectedTradeForChart(sim);
  var tradeCount = Array.isArray(sim.trades) ? sim.trades.length : 0;
  var caption =
    "Portfolio Equity & Drawdown \u2022 " +
    tradeCount +
    (tradeCount === 1 ? " trade" : " trades") +
    (sel ? " \u2022 Selected: " + sel.trade_id : "");
  setChartCaption(caption);

  var initCap = sim.summary && sim.summary.initial_capital ? Number(sim.summary.initial_capital) : null;
  var layout = baseLayout(t, "Trading Date", "Portfolio Equity ($)", []);
  layout.xaxis.type = "date";
  if (xDates.length === 1) {
    layout.xaxis.range = [shiftIsoDate(xDates[0], -1), shiftIsoDate(xDates[0], 1)];
  }
  layout.margin = { t: 48, r: 58, b: 42, l: 64 };
  layout.title = {
    text: caption,
    x: 0,
    xanchor: "left",
    y: 0.98,
    font: { size: 11, color: t.ink, family: t.fontMono },
  };
  layout.legend = { orientation: "h", y: 1.09, x: 0, font: { size: 10, color: t.ink } };
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

  plotBacktestChart([ddTrace, eqTrace], layout);
}

function renderSingleTradeChart(sim, t) {
  var trade = selectedTradeForChart(sim);
  if (!trade || !Array.isArray(trade.steps) || !trade.steps.length) {
    if (sim.mode === "SYSTEMATIC" && state.backtestChartView === "TRADE") {
      setChartCaption("Selected Trade P&L \u2022 No trades");
      renderBacktestEmptyPlaceholder(
        "No simulated trades opened in this window. Switch to <strong>Equity &amp; Drawdown</strong> to inspect portfolio bars."
      );
      return;
    }
    renderSystematicChart(sim, t);
    return;
  }

  var steps = trade.steps;
  var lastValidIdx = -1;
  for (var idx = 0; idx < steps.length; idx++) {
    var rawStep = steps[idx];
    var rawMissing =
      Boolean(rawStep.quote_missing) ||
      rawStep.net_pnl_dollars === null ||
      rawStep.net_pnl_dollars === undefined;
    if (!rawMissing) lastValidIdx = idx;
  }

  var xDates = [];
  var pnlY = [];
  var spotY = [];
  var markerColors = [];
  var markerSizes = [];
  var markerSymbols = [];
  var markerLineColors = [];
  var hoverTexts = [];
  var missingX = [];
  var missingY = [];
  var missingHover = [];
  var validPnlValues = [];
  var prevValidNetPnl = null;
  var prevValidDate = null;

  for (var i = 0; i < steps.length; i++) {
    var st = steps[i];
    xDates.push(st.market_date);
    var isMissing =
      Boolean(st.quote_missing) ||
      st.net_pnl_dollars === null ||
      st.net_pnl_dollars === undefined;
    var netPnl = isMissing ? null : Number(st.net_pnl_dollars);
    var spotVal = st.spot !== null && st.spot !== undefined ? Number(st.spot) : null;
    spotY.push(spotVal);
    pnlY.push(netPnl);

    var dailyPnl = null;
    if (!isMissing) {
      dailyPnl = prevValidNetPnl === null ? netPnl : netPnl - prevValidNetPnl;
    }

    var markSrc = st.mark_source || (isMissing ? "MISSING" : "N/A");
    var spotLine =
      spotVal !== null
        ? "$" + spotVal.toFixed(2) + (st.spot_source ? " (" + st.spot_source + ")" : "")
        : "\u2014";
    var flagsStr = st.flags && st.flags.length ? "<br>Flags: " + st.flags.join(", ") : "";
    var dailyLabel =
      i === 0
        ? "Day-0 MTM"
        : prevValidDate && i > 0 && prevValidDate !== steps[i - 1].market_date
          ? "Change (since " + prevValidDate + ")"
          : "Daily P&L";

    var stateLine =
      i === steps.length - 1
        ? "<br>Position: " +
          (trade.status === "OPEN" ? "OPEN (Unrealized)" : trade.status + " (" + trade.exit_reason + ")")
        : "";

    var hover =
      "<b>" +
      st.market_date +
      "</b> (" +
      (st.ts_et || st.snapshot_id) +
      ")<br>Spot: " +
      spotLine +
      (isMissing
        ? "<br>Daily P&L: \u2014 (Quote missing)<br>Cumulative MTM P&L: \u2014 (MTM omitted)<br>Mark Source: " +
          markSrc +
          " \u2022 Missing Quote"
        : "<br>" +
          dailyLabel +
          ": " +
          signedDollar(dailyPnl) +
          "<br>Cumulative MTM P&L: " +
          signedDollar(netPnl) +
          "<br>Mark Source: " +
          markSrc) +
      "<br>DTE: " +
      st.dte_min_leg +
      "d \u2022 Bar Status: " +
      st.status +
      stateLine +
      flagsStr;
    hoverTexts.push(hover);

    if (isMissing) {
      missingX.push(st.market_date);
      missingY.push(prevValidNetPnl !== null ? prevValidNetPnl : 0);
      missingHover.push(hover);
      markerColors.push(t.muted);
      markerSizes.push(6);
      markerSymbols.push("circle-open");
      markerLineColors.push(t.bg);
    } else {
      validPnlValues.push(netPnl);
      prevValidNetPnl = netPnl;
      prevValidDate = st.market_date;
      if (i === lastValidIdx && trade.status === "OPEN") {
        markerColors.push(t.warn);
        markerSizes.push(11);
        markerSymbols.push("diamond");
        markerLineColors.push(t.ink);
      } else if (i === lastValidIdx && trade.status === "CLOSED") {
        markerColors.push(netPnl >= 0 ? t.call : t.put);
        markerSizes.push(10);
        markerSymbols.push("square");
        markerLineColors.push(t.ink);
      } else {
        markerColors.push(stepMarkerColor(st.status, t));
        markerSizes.push(steps.length === 1 ? 11 : 8);
        markerSymbols.push("circle");
        markerLineColors.push(t.bg);
      }
    }
  }

  var pnlTrace = {
    type: "scatter",
    mode: "lines+markers",
    name: trade.status === "OPEN" ? "Unrealized MTM P&L ($)" : "Cumulative Net P&L ($)",
    x: xDates,
    y: pnlY,
    text: hoverTexts,
    hoverinfo: "text",
    connectgaps: false,
    line: { color: t.ink, width: 2.25 },
    marker: {
      size: markerSizes,
      symbol: markerSymbols,
      color: markerColors,
      line: { color: markerLineColors, width: 1.5 },
    },
    yaxis: "y",
  };

  var spotTrace = {
    type: "scatter",
    mode: steps.length === 1 ? "lines+markers" : "lines",
    name: "Underlying Spot ($)",
    x: xDates,
    y: spotY,
    connectgaps: true,
    line: { color: t.dtePalette[0], width: 1.5, dash: "dot" },
    marker: { size: 6, color: t.dtePalette[0] },
    hovertemplate: "<b>%{x}</b><br>Spot: $%{y:,.2f}<extra></extra>",
    yaxis: "y2",
  };

  var traces = [pnlTrace, spotTrace];
  if (missingX.length) {
    traces.push({
      type: "scatter",
      mode: "markers",
      name: "Missing Quote",
      x: missingX,
      y: missingY,
      text: missingHover,
      hoverinfo: "text",
      marker: { symbol: "x", size: 9, color: t.put, line: { color: t.put, width: 1.5 } },
      yaxis: "y",
    });
  }

  var refs = [makeHorizontalRefLine(0, "Break-even $0", t.muted, "solid", t)];
  var tpVal =
    trade.profit_target_dollars !== null &&
    trade.profit_target_dollars !== undefined &&
    Number(trade.profit_target_dollars) > 0
      ? Number(trade.profit_target_dollars)
      : null;
  var slVal =
    trade.stop_loss_dollars !== null &&
    trade.stop_loss_dollars !== undefined &&
    Number(trade.stop_loss_dollars) < 0
      ? Number(trade.stop_loss_dollars)
      : null;

  if (tpVal !== null) {
    refs.push(makeHorizontalRefLine(tpVal, "TP +" + fmtShortDollar(tpVal), t.call, "dash", t));
  }
  if (slVal !== null) {
    refs.push(makeHorizontalRefLine(slVal, "SL -" + fmtShortDollar(slVal), t.put, "dash", t));
  }

  var statusLabel =
    trade.status === "OPEN"
      ? "OPEN (Unrealized)"
      : trade.status + " (" + trade.exit_reason + ")";
  var caption =
    "Trade " +
    trade.trade_id +
    " \u2022 " +
    trade.strategy_kind +
    " \u2022 " +
    statusLabel +
    " \u2022 " +
    trade.entry_date +
    " \u2192 " +
    trade.exit_date;
  setChartCaption(caption);

  var layout = baseLayout(t, "Trading Date", "Cumulative Net P&L ($)", refs);
  layout.xaxis.type = "date";
  if (xDates.length === 1) {
    layout.xaxis.range = [shiftIsoDate(xDates[0], -1), shiftIsoDate(xDates[0], 1)];
  }

  var maxAbsPnl = 0;
  for (var j = 0; j < validPnlValues.length; j++) {
    maxAbsPnl = Math.max(maxAbsPnl, Math.abs(validPnlValues[j]));
  }
  var rangeCandidates = validPnlValues.slice();
  rangeCandidates.push(0);
  var thresholdCap = Math.max(250, maxAbsPnl * 3);
  if (tpVal !== null && Math.abs(tpVal) <= thresholdCap) rangeCandidates.push(tpVal);
  if (slVal !== null && Math.abs(slVal) <= thresholdCap) rangeCandidates.push(slVal);
  var minY = Math.min.apply(null, rangeCandidates);
  var maxY = Math.max.apply(null, rangeCandidates);
  var span = Math.max(20, maxY - minY);
  layout.yaxis.range = [minY - span * 0.16, maxY + span * 0.18];
  layout.yaxis.zeroline = true;
  layout.yaxis.zerolinecolor = t.muted;

  layout.margin = { t: 54, r: 60, b: 42, l: 64 };
  layout.title = {
    text: caption,
    x: 0,
    xanchor: "left",
    y: 0.98,
    font: { size: 11, color: t.ink, family: t.fontMono },
  };
  layout.legend = { orientation: "h", y: 1.09, x: 0, font: { size: 10, color: t.ink } };
  layout.yaxis2 = {
    title: { text: "Spot ($)", font: { size: 11, color: t.muted } },
    overlaying: "y",
    side: "right",
    showgrid: false,
    tickfont: { color: t.muted },
  };

  plotBacktestChart(traces, layout);
}

export function renderBacktestChart(result, theme) {
  if (!dom.backtestChartEl || typeof window.Plotly === "undefined") return;
  if (!result || !result.simulation) {
    setChartCaption("Portfolio Equity & Drawdown");
    renderBacktestEmptyPlaceholder();
    return;
  }
  var sim = result.simulation;
  var t = theme || themeVars();
  if (sim.mode === "SINGLE_TRADE" || state.backtestChartView === "TRADE") {
    renderSingleTradeChart(sim, t);
  } else {
    renderSystematicChart(sim, t);
  }
}
