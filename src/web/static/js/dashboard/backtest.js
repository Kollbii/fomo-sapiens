/* IBKR Options Analyzer dashboard — Option Backtest Analyzer panel (#backtest-card). */

import {
  baseLayout,
} from "./charts.js";
import {
  themeVars,
} from "./helpers.js";
import {
  apiJson,
} from "./network.js";
import {
  isPanelVisible,
} from "./panels.js";
import {
  state,
} from "./state.js";

// ------------------------------------------------------------------ Option Backtest Analyzer
// One bull put spread replayed over the saved snapshots (POST /api/backtest/run). Click-only:
// nothing runs until RUN BACKTEST. Reads only its own #backtest-* elements.
var backtestState = {
  source: "LEGS",
  ticker: "",
  entries: [],
  entriesTicker: "",
  chainCache: {},
  trackedList: [],
  result: null,
  running: false,
};
var BT_MAX_TICKS = 12;

function btEl(id) {
  return document.getElementById(id);
}

function btText(tag, cls, text) {
  var el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined && text !== null) el.textContent = String(text);
  return el;
}

function btMoney(v, digits) {
  if (v === null || v === undefined || isNaN(Number(v))) return "—";
  var n = Number(v);
  var d = digits === undefined ? 2 : digits;
  var sign = n < 0 ? "-" : "";
  return sign + "$" + Math.abs(n).toFixed(d);
}

function btSignClass(v) {
  if (v === null || v === undefined || isNaN(Number(v)) || Number(v) === 0) return "";
  return Number(v) > 0 ? "is-pos" : "is-neg";
}

function btErrorText(res) {
  var body = res && res.body;
  if (body && typeof body.detail === "string") return body.detail;
  if (body && Array.isArray(body.detail) && body.detail.length) {
    return body.detail
      .map(function (d) {
        return (d && d.msg ? String(d.msg).replace(/^Value error, /, "") : "Invalid input");
      })
      .join("; ");
  }
  if (body && body.message) return body.message;
  if (res && res.textSnippet) return res.textSnippet;
  return "Request failed (HTTP " + (res ? res.status : "?") + ").";
}

function btSetBadge(text) {
  var badge = btEl("backtest-badge");
  if (badge) badge.textContent = text;
}

function btShowMessages(list) {
  var box = btEl("backtest-warnings");
  if (!box) return;
  box.innerHTML = "";
  (list || []).forEach(function (w) {
    var row = btText("div", "backtest-warning" + (w.error ? " backtest-warning--error" : ""));
    row.appendChild(btText("strong", null, w.code));
    row.appendChild(document.createTextNode(w.message));
    box.appendChild(row);
  });
}

function btCollectorNeverOn() {
  var st = state.collectorStatus;
  if (!st) return false;
  return !st.enabled && !(st.recent_runs && st.recent_runs.length);
}

function btSetSource(source) {
  backtestState.source = source;
  var btns = document.querySelectorAll(".backtest-source-btn");
  for (var i = 0; i < btns.length; i++) {
    var on = btns[i].getAttribute("data-backtest-source") === source;
    btns[i].classList.toggle("active", on);
    btns[i].setAttribute("aria-pressed", on ? "true" : "false");
  }
  var fields = document.querySelectorAll("#backtest-form [data-backtest-for]");
  for (var j = 0; j < fields.length; j++) {
    var forList = (fields[j].getAttribute("data-backtest-for") || "").split(" ");
    fields[j].hidden = forList.indexOf(source) === -1;
  }
  if (backtestState.result && backtestState.result.source !== source) {
    // Never show one source's result under another source's tab.
    backtestState.result = null;
    btSetBadge("IDLE • CLICK RUN");
    btShowMessages([]);
    btClearOutput("Click RUN BACKTEST to replay this source.");
    var assumptions = btEl("backtest-assumptions");
    if (assumptions) assumptions.textContent = "";
  }
  if (source === "TRACKED") btLoadTracked();
  btSyncTrackButton();
}

// ---- entry steps (newest first, latest selected) ----
async function btLoadEntries(ticker, force) {
  if (!ticker) return;
  var tickerLabel = btEl("backtest-ticker");
  if (tickerLabel) tickerLabel.textContent = ticker;
  if (!force && backtestState.entriesTicker === ticker && backtestState.entries.length) return;
  var select = btEl("backtest-entry");
  var hint = btEl("backtest-entry-hint");
  var previous = select ? select.value : "";
  var res;
  try {
    res = await apiJson("/api/backtest/entries/" + encodeURIComponent(ticker));
  } catch (_err) {
    if (hint) hint.textContent = "Could not load the saved snapshots.";
    return;
  }
  if (!res.ok || !res.body) {
    if (hint) hint.textContent = btErrorText(res);
    return;
  }
  backtestState.ticker = ticker;
  backtestState.entriesTicker = ticker;
  backtestState.entries = res.body.entries || [];
  if (!select) return;
  select.innerHTML = "";
  if (!backtestState.entries.length) {
    var none = btText("option", null, "No saved snapshots for " + ticker);
    none.value = "";
    select.appendChild(none);
    select.disabled = true;
    if (hint) {
      hint.textContent = "No history for " + ticker + ": load or refresh a snapshot first.";
      hint.classList.add("is-warn");
    }
    btFillLegs(null);
    return;
  }
  select.disabled = false;
  backtestState.entries.forEach(function (e) {
    var cov = e.quote_coverage === null || e.quote_coverage === undefined
      ? "cov n/a"
      : "cov " + Number(e.quote_coverage).toFixed(2);
    var quality = e.usable ? "OK" : "LOW: " + (e.quality_reasons || []).join("; ");
    var mode = e.collection_mode === "SCHEDULED" ? "sched" : e.collection_mode === "MANUAL" ? "manual" : "";
    var opt = btText(
      "option",
      null,
      e.ts_et + " • " + e.ts_poland + " • " + cov + " • " + quality + (mode ? " • " + mode : "")
    );
    opt.value = e.snapshot_id;
    select.appendChild(opt);
  });
  var keep = backtestState.entries.some(function (e) {
    return e.snapshot_id === previous;
  });
  select.value = keep ? previous : backtestState.entries[0].snapshot_id;
  btUpdateEntryHint();
  btLoadLegChoices();
}

function btUpdateEntryHint() {
  var hint = btEl("backtest-entry-hint");
  var select = btEl("backtest-entry");
  if (!hint || !select) return;
  var idx = select.selectedIndex;
  var n = backtestState.entries.length;
  var later = idx >= 0 ? idx : 0; // newest first: index = number of later snapshots
  var text = n + " snapshot(s) saved; " + later + " after this entry.";
  var warn = later === 0;
  if (warn) text += " Pick an earlier entry to see any steps after it.";
  hint.textContent = text;
  hint.classList.toggle("is-warn", warn);
}

// ---- legs from the entry snapshot (only expiries / strikes that exist there) ----
async function btLoadLegChoices() {
  var select = btEl("backtest-entry");
  var sid = select ? select.value : "";
  if (!sid) {
    btFillLegs(null);
    return;
  }
  var ticker = backtestState.ticker;
  var key = ticker + "|" + sid;
  if (!backtestState.chainCache[key]) {
    var res;
    try {
      res = await apiJson(
        "/api/chain/" + encodeURIComponent(ticker) + "?snapshot_id=" + encodeURIComponent(sid)
      );
    } catch (_err) {
      btFillLegs(null);
      return;
    }
    if (!res.ok || !res.body) {
      btFillLegs(null);
      return;
    }
    var puts = {};
    var spot = res.body.meta ? Number(res.body.meta.underlying_price) : NaN;
    (res.body.contracts || []).forEach(function (c) {
      if (c.right !== "P") return;
      if (!puts[c.expiry]) puts[c.expiry] = { dte: c.dte, strikes: [] };
      puts[c.expiry].strikes.push(Number(c.strike));
    });
    Object.keys(puts).forEach(function (exp) {
      puts[exp].strikes.sort(function (a, b) {
        return a - b;
      });
    });
    backtestState.chainCache[key] = { puts: puts, spot: spot };
  }
  btFillLegs(backtestState.chainCache[key]);
}

function btFillSelect(select, values, labelFn, preferred) {
  if (!select) return;
  select.innerHTML = "";
  values.forEach(function (v) {
    var opt = btText("option", null, labelFn(v));
    opt.value = String(v);
    select.appendChild(opt);
  });
  select.disabled = !values.length;
  if (preferred !== undefined && preferred !== null && values.length) {
    select.value = String(preferred);
  }
}

function btFillLegs(chain) {
  var expSel = btEl("backtest-expiry");
  if (!chain || !Object.keys(chain.puts).length) {
    btFillSelect(expSel, [], String);
    btFillSelect(btEl("backtest-short"), [], String);
    btFillSelect(btEl("backtest-long"), [], String);
    return;
  }
  var exps = Object.keys(chain.puts).sort();
  var current = expSel ? expSel.value : "";
  var target = exps.indexOf(current) !== -1 ? current : exps.reduce(function (best, e) {
    return Math.abs(chain.puts[e].dte - 38) < Math.abs(chain.puts[best].dte - 38) ? e : best;
  }, exps[0]);
  btFillSelect(expSel, exps, function (e) {
    return e + " (" + chain.puts[e].dte + " DTE)";
  }, target);
  btFillStrikes(chain);
}

function btFillStrikes(chain) {
  var expSel = btEl("backtest-expiry");
  var shortSel = btEl("backtest-short");
  var longSel = btEl("backtest-long");
  var exp = expSel ? expSel.value : "";
  var strikes = chain && chain.puts[exp] ? chain.puts[exp].strikes : [];
  var spot = chain ? chain.spot : NaN;
  var prevShort = shortSel ? Number(shortSel.value) : NaN;
  var shortDefault = strikes.indexOf(prevShort) !== -1 ? prevShort : null;
  if (shortDefault === null && strikes.length > 1) {
    var aim = isNaN(spot) ? strikes[Math.floor(strikes.length / 2)] : spot * 0.95;
    shortDefault = strikes.slice(1).reduce(function (best, k) {
      return Math.abs(k - aim) < Math.abs(best - aim) ? k : best;
    }, strikes[1]);
  }
  var label = function (k) {
    var pct = isNaN(spot) ? "" : " (" + ((k / spot - 1) * 100).toFixed(1) + "%)";
    return "$" + k + pct;
  };
  btFillSelect(shortSel, strikes.slice(1), label, shortDefault);
  btFillLongStrikes(chain);
}

function btFillLongStrikes(chain) {
  var expSel = btEl("backtest-expiry");
  var shortSel = btEl("backtest-short");
  var longSel = btEl("backtest-long");
  var exp = expSel ? expSel.value : "";
  var strikes = chain && chain.puts[exp] ? chain.puts[exp].strikes : [];
  var shortK = shortSel ? Number(shortSel.value) : NaN;
  var below = strikes.filter(function (k) {
    return k < shortK;
  });
  var prevLong = longSel ? Number(longSel.value) : NaN;
  var longDefault = below.indexOf(prevLong) !== -1 ? prevLong : below.length ? below[below.length - 1] : null;
  btFillSelect(longSel, below, function (k) {
    return "$" + k + " (width $" + (shortK - k) + ")";
  }, longDefault);
}

function btCurrentChain() {
  var select = btEl("backtest-entry");
  return backtestState.chainCache[backtestState.ticker + "|" + (select ? select.value : "")] || null;
}

// ---- tracked spreads ----
async function btLoadTracked() {
  var select = btEl("backtest-tracked");
  if (!select) return;
  var res;
  try {
    res = await apiJson("/api/tracker/spreads");
  } catch (_err) {
    return;
  }
  if (!res.ok || !res.body) return;
  backtestState.trackedList = res.body.spreads || [];
  var previous = select.value;
  select.innerHTML = "";
  if (!backtestState.trackedList.length) {
    var none = btText("option", null, "No tracked spreads yet");
    none.value = "";
    select.appendChild(none);
    select.disabled = true;
    btShowMessages([
      {
        code: btCollectorNeverOn() ? "COLLECTOR_NEVER_ON" : "NO_TRACKED_SPREADS",
        message: btCollectorNeverOn()
          ? "The collector has never run, so nothing is tracked yet. Switch COLLECTOR on: tracking only runs inside its slots."
          : "No spreads are tracked. Use TRACK THIS SPREAD after a run, or wait for the daily AUTO pick (collector ON).",
      },
    ]);
    return;
  }
  select.disabled = false;
  backtestState.trackedList
    .slice()
    .sort(function (a, b) {
      return a.created_at_utc < b.created_at_utc ? 1 : -1;
    })
    .forEach(function (s) {
      var short = s.legs.filter(function (l) {
        return l.side === "SELL";
      })[0];
      var long = s.legs.filter(function (l) {
        return l.side === "BUY";
      })[0];
      var legsText = short && long ? short.strike + "/" + long.strike + "P " + short.expiry : "";
      var opt = btText(
        "option",
        null,
        s.ticker + " • " + legsText + " • x" + s.qty + " • " + s.source + " • " + s.status + " • " + s.id
      );
      opt.value = s.id;
      select.appendChild(opt);
    });
  if (previous) select.value = previous;
}

// ---- run ----
function btRequestBody() {
  var src = backtestState.source;
  if (src === "TRACKED") {
    var tr = btEl("backtest-tracked");
    return tr && tr.value ? { source: "TRACKED", spread_id: tr.value } : null;
  }
  var entry = btEl("backtest-entry");
  var qtyEl = btEl("backtest-qty");
  var qty = Math.round(Number(qtyEl ? qtyEl.value : 1));
  if (!entry || !entry.value) return null;
  var body = {
    source: src,
    ticker: backtestState.ticker,
    entry_snapshot_id: entry.value,
    qty: isNaN(qty) ? 1 : Math.min(100, Math.max(1, qty)),
  };
  if (src === "LEGS") {
    var exp = btEl("backtest-expiry");
    var sh = btEl("backtest-short");
    var lo = btEl("backtest-long");
    if (!exp || !exp.value || !sh || !sh.value || !lo || !lo.value) return null;
    body.expiry = exp.value;
    body.short_strike = Number(sh.value);
    body.long_strike = Number(lo.value);
  }
  return body;
}

async function btRun() {
  if (backtestState.running) return;
  var body = btRequestBody();
  if (!body) {
    btShowMessages([
      {
        code: "INPUT",
        error: true,
        message:
          backtestState.source === "TRACKED"
            ? "Pick a tracked spread first."
            : "Pick an entry step" + (backtestState.source === "LEGS" ? ", an expiry and both strikes." : "."),
      },
    ]);
    return;
  }
  backtestState.running = true;
  var runBtn = btEl("backtest-run-btn");
  if (runBtn) runBtn.disabled = true;
  btSetBadge("RUNNING…");
  var res;
  try {
    res = await apiJson("/api/backtest/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      timeoutMs: 60000,
    });
  } catch (err) {
    res = { ok: false, status: 0, body: null, textSnippet: err && err.timedOut ? "Timed out." : "Network error." };
  } finally {
    backtestState.running = false;
    if (runBtn) runBtn.disabled = false;
  }
  if (!res.ok || !res.body) {
    backtestState.result = null;
    btSetBadge("ERROR");
    btShowMessages([{ code: "HTTP " + res.status, error: true, message: btErrorText(res) }]);
    btClearOutput();
    return;
  }
  backtestState.result = res.body;
  btRender();
}

function btClearOutput(message) {
  var kpis = btEl("backtest-kpis");
  if (kpis) kpis.innerHTML = "";
  var info = btEl("backtest-entry-info");
  if (info) info.innerHTML = "";
  var body = btEl("backtest-table-body");
  if (body) body.innerHTML = "";
  var chart = btEl("backtest-chart");
  if (chart && window.Plotly) window.Plotly.purge(chart);
  if (chart) {
    chart.classList.add("is-idle");
    chart.innerHTML = "";
    chart.appendChild(
      btText("div", "strategy-empty-state", message || "No result. Fix the input above and run again.")
    );
  }
  btSyncTrackButton();
}

// ---- render ----
function btRender() {
  var data = backtestState.result;
  if (!data) return;
  var r = data.result;
  btSetBadge(
    r.ticker + " • " + data.source + " • " + data.outcome + " • ENGINE v" + r.reproducibility.engine_version
  );
  btShowMessages(data.warnings);
  btRenderKpis(data);
  btRenderChart();
  btRenderEntryInfo(data);
  btRenderTable(data);
  var assumptions = btEl("backtest-assumptions");
  if (assumptions) assumptions.textContent = "Assumptions: " + data.assumptions.join(" ");
  btSyncTrackButton();
}

function btKpi(box, label, value, sub, cls) {
  var card = btText("div", "backtest-kpi");
  card.appendChild(btText("span", "backtest-kpi__label", label));
  card.appendChild(btText("span", "backtest-kpi__value" + (cls ? " " + cls : ""), value));
  if (sub) card.appendChild(btText("span", "backtest-kpi__sub", sub));
  box.appendChild(card);
}

function btRenderKpis(data) {
  var box = btEl("backtest-kpis");
  if (!box) return;
  box.innerHTML = "";
  var r = data.result;
  var e = r.entry;
  var s = r.summary;
  btKpi(box, "Entry credit (mid)", btMoney(e.entry_cash_mid_dollars), "natural " + btMoney(e.entry_cash_natural_dollars) + " • slip " + btMoney(e.natural_slippage_dollars));
  btKpi(box, "Max profit", btMoney(e.max_profit_dollars), null, "is-pos");
  btKpi(box, "Max loss", btMoney(-Math.abs(e.max_loss_dollars)), null, "is-neg");
  var pnlLabel = "Latest OK P&L";
  var pnl = s.last_ok_pnl_dollars;
  var pnlSub = s.last_ok_snapshot_id ? "at " + s.last_ok_snapshot_id : "no OK step";
  if (r.settlement.status === "SETTLED") {
    pnlLabel = "Final P&L";
    pnl = r.settlement.pnl_dollars;
    pnlSub = "settled " + (r.settlement.ts_et || "");
  }
  btKpi(box, pnlLabel, btMoney(pnl), pnlSub, btSignClass(pnl));
  btKpi(box, "Days held", Number(data.days_held).toFixed(1), "calendar days");
  var outcomeSub = data.outcome === "SETTLED"
    ? "spot " + r.settlement.spot + " (" + (r.settlement.spot_from || "") + ")"
    : data.outcome === "OPEN" ? "expiry " + r.settlement.expiry : "no usable closing spot";
  btKpi(box, "Outcome", data.outcome, outcomeSub, data.outcome === "SETTLED" ? "" : "");
  btKpi(
    box,
    "Steps OK / flagged / gap",
    s.steps_ok + " / " + (s.steps_unreliable + s.steps_incomplete) + " / " + s.steps_gap,
    s.steps_unreliable + " unreliable • " + s.steps_incomplete + " incomplete"
  );
}

function btQuoteSources(step) {
  var set = {};
  (step.legs || []).forEach(function (l) {
    if (l.quote_source) set[l.quote_source] = true;
  });
  var s = set.SNAPSHOT;
  var t = set.TRACKED;
  return s && t ? "S+T" : s ? "S" : t ? "T" : "—";
}

function btEtDay(step) {
  return String(step.ts_et || "").slice(0, 10);
}

export function btRenderChart() {
  var chart = btEl("backtest-chart");
  var data = backtestState.result;
  if (!chart || !data || !window.Plotly || !isPanelVisible("backtest")) return;
  var r = data.result;
  var t = themeVars();
  if (chart.classList.contains("is-idle")) {
    // Remove the placeholder only; an existing plot is updated in place by Plotly.react
    // (clearing its DOM would leave Plotly diffing against nodes that no longer exist).
    window.Plotly.purge(chart);
    chart.innerHTML = "";
    chart.classList.remove("is-idle");
  }
  var steps = r.steps;
  var n = steps.length;
  var xs = [];
  var lineY = [];
  var ok = { x: [], y: [], text: [] };
  var unrel = { x: [], y: [], text: [] };
  var none = { x: [], y: [], text: [] };
  var tickvals = [];
  var ticktext = [];
  var shapes = [];
  var annotations = [];
  // Tick thinning: every trading day keeps its divider, but only every `dayStride`-th day
  // start is labelled; times inside a day are labelled only while there is room.
  var dayCount = 0;
  for (var d = 0; d < n; d++) {
    if (d === 0 || btEtDay(steps[d]) !== btEtDay(steps[d - 1])) dayCount++;
  }
  var dayStride = Math.max(1, Math.ceil(dayCount / BT_MAX_TICKS));
  var stride = Math.max(1, Math.ceil(n / BT_MAX_TICKS));
  var intraDayLabels = dayCount * 2 <= BT_MAX_TICKS;
  var dayIndex = -1;
  for (var i = 0; i < n; i++) {
    var st = steps[i];
    var label = String(st.ts_et).slice(5, 16);
    var hover =
      st.ts_et + " • " + (data.ts_poland[i] || "") + "<br>" + st.kind + " • " + st.status +
      " • DTE " + st.short_dte + "<br>P&L " + btMoney(st.pnl_dollars) + " • spread " +
      (st.spread_value_per_share === null ? "—" : st.spread_value_per_share) +
      " • quotes " + btQuoteSources(st) + (st.flags.length ? "<br>" + st.flags.join("<br>") : "");
    xs.push(i);
    var hasValue = st.pnl_dollars !== null && st.pnl_dollars !== undefined;
    lineY.push(hasValue ? st.pnl_dollars : null);
    var bucket = !hasValue ? none : st.status === "UNRELIABLE" ? unrel : ok;
    bucket.x.push(i);
    bucket.y.push(hasValue ? st.pnl_dollars : 0);
    bucket.text.push(hover);
    var newDay = i === 0 || btEtDay(st) !== btEtDay(steps[i - 1]);
    if (newDay) {
      dayIndex++;
      if (i > 0) {
        shapes.push({
          type: "line", xref: "x", yref: "paper", x0: i - 0.5, x1: i - 0.5, y0: 0, y1: 1,
          line: { color: t.muted, width: 1, dash: "dot" },
        });
      }
      if (dayIndex % dayStride === 0) {
        tickvals.push(i);
        ticktext.push(label);
      }
    } else if (
      intraDayLabels &&
      i % stride === 0 &&
      (tickvals.length === 0 || i - tickvals[tickvals.length - 1] >= stride)
    ) {
      tickvals.push(i);
      ticktext.push(label.slice(6));
    }
  }
  var e = r.entry;
  var settled = r.settlement.status === "SETTLED";
  var traces = [
    {
      x: xs, y: lineY, type: "scatter", mode: "lines", name: "P&L",
      line: { color: t.ink, width: 1.6 }, connectgaps: false, hoverinfo: "skip",
    },
    {
      x: ok.x, y: ok.y, text: ok.text, type: "scatter", mode: "markers", name: "OK",
      marker: { color: t.ink, size: 7 }, hovertemplate: "%{text}<extra></extra>",
    },
    {
      x: unrel.x, y: unrel.y, text: unrel.text, type: "scatter", mode: "markers", name: "Unreliable",
      marker: { color: t.bg, size: 8, line: { color: t.warn, width: 2 } },
      hovertemplate: "%{text}<extra></extra>",
    },
    {
      x: none.x, y: none.y, text: none.text, type: "scatter", mode: "markers", name: "No value (incomplete / gap)",
      marker: { color: t.muted, size: 8, symbol: "x-thin", line: { color: t.muted, width: 2 } },
      hovertemplate: "%{text}<extra></extra>",
    },
    {
      x: [0], y: [steps.length && steps[0].pnl_dollars !== null ? steps[0].pnl_dollars : 0],
      type: "scatter", mode: "markers", name: "Entry",
      marker: { color: t.call, size: 12, symbol: "star" },
      hovertemplate: "Entry " + e.ts_et + "<br>credit " + btMoney(e.entry_cash_mid_dollars) + "<extra></extra>",
    },
  ];
  if (settled || r.settlement.status === "UNRESOLVED") {
    tickvals.push(n);
    ticktext.push(settled ? "Settle" : "Expiry");
    shapes.push({
      type: "line", xref: "x", yref: "paper", x0: n - 0.5, x1: n - 0.5, y0: 0, y1: 1,
      line: { color: t.ink, width: 1 },
    });
    if (settled) {
      traces.push({
        x: [n], y: [r.settlement.pnl_dollars], type: "scatter", mode: "markers", name: "Settlement",
        marker: { color: r.settlement.pnl_dollars >= 0 ? t.call : t.put, size: 12, symbol: "diamond" },
        hovertemplate:
          "Settled " + r.settlement.ts_et + " • " + (data.settlement_ts_poland || "") +
          "<br>spot " + r.settlement.spot + " (" + (r.settlement.spot_source || "?") + ", type " +
          (r.settlement.market_data_type || "?") + ", from " + (r.settlement.spot_from || "?") +
          ")<br>P&L " + btMoney(r.settlement.pnl_dollars) + "<extra></extra>",
      });
    }
  }
  var refs = [
    { y: 0, color: t.muted, dash: "solid", text: "" },
    { y: e.max_profit_dollars, color: t.call, dash: "dash", text: "max profit " + btMoney(e.max_profit_dollars, 0) },
    { y: -Math.abs(e.max_loss_dollars), color: t.put, dash: "dash", text: "max loss " + btMoney(-Math.abs(e.max_loss_dollars), 0) },
  ];
  refs.forEach(function (ref) {
    shapes.push({
      type: "line", xref: "paper", yref: "y", x0: 0, x1: 1, y0: ref.y, y1: ref.y,
      line: { color: ref.color, width: 1, dash: ref.dash },
    });
    if (ref.text) {
      annotations.push({
        xref: "paper", yref: "y", x: 1, y: ref.y, xanchor: "right", yanchor: "bottom",
        text: ref.text, showarrow: false, font: { size: 10, color: ref.color },
      });
    }
  });
  var layout = baseLayout(t, "Time step (ET)", "P&L ($)");
  layout.shapes = shapes;
  layout.annotations = annotations;
  layout.hovermode = "closest";
  layout.margin = { t: 16, r: 22, b: 92, l: 64 };
  layout.legend = { orientation: "h", x: 0, y: -0.36, yanchor: "top", font: { size: 10, color: t.ink } };
  layout.xaxis.tickmode = "array";
  layout.xaxis.tickvals = tickvals;
  layout.xaxis.ticktext = ticktext;
  layout.xaxis.tickangle = -30;
  layout.xaxis.range = [-0.6, (settled || r.settlement.status === "UNRESOLVED" ? n : n - 1) + 0.6];
  layout.xaxis.zeroline = false;
  window.Plotly.react(chart, traces, layout, { responsive: true, displaylogo: false });
}

function btRenderEntryInfo(data) {
  var box = btEl("backtest-entry-info");
  if (!box) return;
  box.innerHTML = "";
  var r = data.result;
  var e = r.entry;
  box.appendChild(
    btText(
      "div",
      null,
      "Entry " + e.snapshot_id + " • " + e.ts_et + " • spot " + e.spot + " • breakevens " +
        (e.breakevens || []).join(", ") + " • " + data.history_snapshot_count + " snapshot(s) saved for " + r.ticker
    )
  );
  var legs = btText("ul");
  e.legs.forEach(function (l) {
    legs.appendChild(
      btText(
        "li",
        null,
        l.side + " " + l.strike + l.right + " " + l.expiry + " ×" + l.qty + " @ mid " + l.mid +
          " (bid " + (l.bid === null ? "—" : l.bid) + " / ask " + (l.ask === null ? "—" : l.ask) + ")"
      )
    );
  });
  box.appendChild(legs);
  if (data.source === "AUTO_BUILD" && e.rationale && e.rationale.length) {
    box.appendChild(btText("div", null, "Auto-Builder rationale:"));
    var why = btText("ul");
    e.rationale.forEach(function (line) {
      why.appendChild(btText("li", null, line));
    });
    box.appendChild(why);
  }
  var s = r.settlement;
  box.appendChild(
    btText(
      "div",
      null,
      "Settlement: " + s.status + (s.status === "SETTLED"
        ? " • " + s.ts_et + " • spot " + s.spot + " (" + (s.spot_source || "?") + ", type " +
          (s.market_data_type || "?") + ", from " + (s.spot_from || "?") + ") • intrinsic " +
          s.intrinsic_spread_per_share + "/share"
        : " • " + s.note)
    )
  );
  if (r.notes && r.notes.length) {
    box.appendChild(btText("div", null, "Engine notes:"));
    var notes = btText("ul");
    r.notes.forEach(function (n) {
      notes.appendChild(btText("li", null, n));
    });
    box.appendChild(notes);
  }
  var repro = r.reproducibility;
  var details = btText("details");
  details.appendChild(btText("summary", null, "Engine settings & reproducibility"));
  details.appendChild(
    btText(
      "pre",
      null,
      JSON.stringify(
        {
          engine_version: repro.engine_version,
          strategies_sha256: repro.strategies_sha256,
          risk_free_rate: repro.risk_free_rate,
          walls_top_n: repro.walls_top_n,
          quality_thresholds: repro.quality_thresholds,
          tracked_data_used: repro.tracked_data_used,
          tracked_files: repro.tracked_files,
          tracker_run_entries: repro.tracker_run_entries,
          skipped_run_log_lines: repro.skipped_run_log_lines,
        },
        null,
        1
      )
    )
  );
  box.appendChild(details);
}

function btRenderTable(data) {
  var body = btEl("backtest-table-body");
  if (!body) return;
  body.innerHTML = "";
  var steps = data.result.steps;
  steps.forEach(function (st, i) {
    var tr = document.createElement("tr");
    if (i > 0 && btEtDay(st) !== btEtDay(steps[i - 1])) tr.className = "is-day-start";
    var cells = [
      st.ts_et.replace(" ET", ""),
      (data.ts_poland[i] || "").replace(" PL", ""),
      st.kind,
      st.short_dte,
      st.spot === null ? "—" : st.spot,
      st.spread_value_per_share === null ? "—" : st.spread_value_per_share,
      null,
      st.pnl_pct_of_max_risk === null ? "—" : st.pnl_pct_of_max_risk + "%",
      null,
      btQuoteSources(st),
      st.flags.join(", "),
    ];
    cells.forEach(function (val, c) {
      var td;
      if (c === 6) {
        td = btText("td", "backtest-pnl " + btSignClass(st.pnl_dollars), btMoney(st.pnl_dollars));
      } else if (c === 8) {
        var label = st.kind === "GAP" ? "GAP" : st.status;
        td = btText("td", "backtest-status backtest-status--" + label, label);
      } else {
        td = btText("td", null, val);
      }
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
  var s = data.result.settlement;
  if (s.status === "SETTLED") {
    var row = document.createElement("tr");
    row.className = "is-day-start";
    [
      s.ts_et.replace(" ET", ""),
      (data.settlement_ts_poland || "").replace(" PL", ""),
      "SETTLE",
      0,
      s.spot,
      s.intrinsic_spread_per_share,
      btMoney(s.pnl_dollars),
      s.pnl_pct_of_max_risk === null ? "—" : s.pnl_pct_of_max_risk + "%",
      "SETTLED",
      s.spot_from === "TRACKER_RUN_LOG" ? "run log" : "S",
      (s.spot_source || "?") + " spot, type " + (s.market_data_type || "?"),
    ].forEach(function (val) {
      row.appendChild(btText("td", null, val));
    });
    body.appendChild(row);
  }
}

// ---- tracking ----
function btSyncTrackButton() {
  var btn = btEl("backtest-track-btn");
  var note = btEl("backtest-track-note");
  if (!btn) return;
  var data = backtestState.result;
  var msg = "";
  btn.textContent = "TRACK THIS SPREAD";
  btn.disabled = true;
  if (backtestState.source === "TRACKED") {
    btn.hidden = true;
    if (data && data.source === "TRACKED") msg = "Already a tracked spread (" + (data.spread_id || "") + ").";
  } else {
    btn.hidden = false;
    if (!data || data.source !== backtestState.source) {
      msg = "Run a backtest first; then the spread can be tracked below 30 DTE.";
    } else if (data.tracked) {
      btn.textContent = "TRACKED ✓";
      msg = "Tracked as " + data.tracked.spread_id + " (" + data.tracked.source + ", " + data.tracked.status + ").";
    } else if (data.outcome !== "OPEN") {
      msg = "This spread has expired; there is nothing left to track.";
    } else {
      btn.disabled = false;
      msg = "Not tracked yet.";
      if (data.previously_tracked && data.previously_tracked.length) {
        msg += " Previously tracked: " + data.previously_tracked.map(function (p) {
          return p.spread_id + " (" + p.status + ")";
        }).join(", ") + ".";
      }
    }
    if (data && data.collector && !data.collector.enabled) {
      msg += " Tracking only runs while the COLLECTOR is ON (it is OFF" +
        (data.collector.ever_enabled ? "" : " and has never run") + ").";
    }
  }
  if (note) note.textContent = msg;
}

async function btTrack() {
  var data = backtestState.result;
  if (!data || data.tracked || data.source === "TRACKED") return;
  var r = data.result;
  var body = {
    ticker: r.ticker,
    snapshot_id: r.entry.snapshot_id,
    legs: r.entry.legs.map(function (l) {
      return { side: l.side, right: l.right, expiry: l.expiry, strike: l.strike, qty: l.qty };
    }),
  };
  var btn = btEl("backtest-track-btn");
  if (btn) btn.disabled = true;
  var res;
  try {
    res = await apiJson("/api/tracker/spreads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (_err) {
    res = { ok: false, status: 0, body: null, textSnippet: "Network error." };
  }
  if (res.ok && res.body) {
    data.tracked = { spread_id: res.body.id, source: res.body.source, status: res.body.status };
    btSyncTrackButton();
    return;
  }
  btSyncTrackButton();
  var note = btEl("backtest-track-note");
  if (note) note.textContent = "Not tracked: " + btErrorText(res);
}

// Called after every snapshot-list load (ticker switch, manual refresh): keeps the entry list
// current; the selected entry is kept when it still exists.
export function backtestOnSnapshotsLoaded(ticker) {
  if (!ticker) return;
  btLoadEntries(ticker, true);
}

export function wireBacktest() {
  var card = btEl("backtest-card");
  if (!card) return;
  var sources = document.querySelectorAll(".backtest-source-btn");
  for (var i = 0; i < sources.length; i++) {
    sources[i].addEventListener("click", function (ev) {
      btSetSource(ev.currentTarget.getAttribute("data-backtest-source"));
    });
  }
  var form = btEl("backtest-form");
  if (form) {
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      btRun();
    });
  }
  var entry = btEl("backtest-entry");
  if (entry) {
    entry.addEventListener("change", function () {
      btUpdateEntryHint();
      btLoadLegChoices();
    });
  }
  var exp = btEl("backtest-expiry");
  if (exp) {
    exp.addEventListener("change", function () {
      btFillStrikes(btCurrentChain());
    });
  }
  var sh = btEl("backtest-short");
  if (sh) {
    sh.addEventListener("change", function () {
      btFillLongStrikes(btCurrentChain());
    });
  }
  var trackBtn = btEl("backtest-track-btn");
  if (trackBtn) trackBtn.addEventListener("click", btTrack);
  btSetSource("LEGS");
}
