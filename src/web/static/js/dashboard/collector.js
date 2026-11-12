/* IBKR Options Analyzer dashboard — Scheduled collection control, status polling, refresh wait and corridor stop notice. */

import {
  extractApiErrorMessage,
  showBanner,
} from "./helpers.js";
import {
  apiJson,
} from "./network.js";
import {
  snapshotEtParts,
} from "./snapshots.js";
import {
  dom,
  state,
} from "./state.js";

// ------------------------------------------------------------------ Scheduled Collection
var COLLECTOR_POLL_MS = 30000;
var COLLECTOR_BUSY_POLL_MS = 3000;
export var REFRESH_COLLECTOR_WAIT_MS = 180000;
var COLLECTOR_STOP_NOTICE_MS = 180000;

function collectorEl(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

export function collectorEtTime(isoUtc) {
  var et = snapshotEtParts(isoUtc);
  return et ? et.dayShort + " " + et.time : String(isoUtc || "");
}

function collectorAllowedRanges() {
  var st = state.status || {};
  return (st.allowed_range_pcts && st.allowed_range_pcts.length
    ? st.allowed_range_pcts
    : [0.1, 0.15, 0.2, 0.25, 0.35, 0.5]
  ).map(Number);
}

function collectorAllowedSteps() {
  var st = state.status || {};
  return (st.allowed_strike_steps && st.allowed_strike_steps.length
    ? st.allowed_strike_steps
    : [1, 2.5, 5, 10, 25]
  ).map(Number);
}

function collectorDraftFromStatus(st) {
  return {
    enabled: Boolean(st.enabled),
    interval_min: Number(st.interval_min),
    tickers: (st.tickers || []).map(function (t) {
      return {
        symbol: t.symbol,
        range_pct: Number(t.range_pct),
        strike_step: Number(t.strike_step),
        dte_min: Number(t.dte_min),
        dte_max: Number(t.dte_max),
        pinned_since_utc: t.pinned_since_utc || null,
      };
    }),
  };
}

function setCollectorDirty(isDirty) {
  state.collectorDirty = Boolean(isDirty);
  var hint = document.getElementById("collector-dirty");
  if (hint) hint.textContent = state.collectorDirty ? "Unsaved changes" : "";
}

function collectorSelect(values, current, label, fmt) {
  var sel = collectorEl("select", "select-control");
  sel.setAttribute("aria-label", label);
  values.forEach(function (v) {
    var opt = collectorEl("option", null, fmt(v));
    opt.value = String(v);
    if (Math.abs(v - current) < 1e-9) opt.selected = true;
    sel.appendChild(opt);
  });
  return sel;
}

function renderCollectorTickers() {
  var body = document.getElementById("collector-tickers-body");
  if (!body || !state.collectorDraft) return;
  body.innerHTML = "";
  state.collectorDraft.tickers.forEach(function (pin, idx) {
    var tr = collectorEl("tr");
    tr.appendChild(collectorEl("td", "collector-tickers__sym", pin.symbol));
    var rangeTd = collectorEl("td");
    var rangeSel = collectorSelect(collectorAllowedRanges(), pin.range_pct, pin.symbol + " range", function (v) {
      return "\u00b1" + Math.round(v * 100) + "%";
    });
    rangeSel.addEventListener("change", function () {
      pin.range_pct = Number(rangeSel.value);
      setCollectorDirty(true);
    });
    rangeTd.appendChild(rangeSel);
    tr.appendChild(rangeTd);
    var stepTd = collectorEl("td");
    var stepSel = collectorSelect(collectorAllowedSteps(), pin.strike_step, pin.symbol + " strike step", function (v) {
      return "$" + v;
    });
    stepSel.addEventListener("change", function () {
      pin.strike_step = Number(stepSel.value);
      setCollectorDirty(true);
    });
    stepTd.appendChild(stepSel);
    tr.appendChild(stepTd);
    tr.appendChild(collectorEl("td", null, pin.dte_min + "\u2013" + pin.dte_max + "d"));
    tr.appendChild(
      collectorEl("td", "collector-muted", pin.pinned_since_utc ? collectorEtTime(pin.pinned_since_utc) : "new")
    );
    var rmTd = collectorEl("td");
    var rm = collectorEl("button", "btn collector-remove", "\u00d7");
    rm.type = "button";
    rm.title = "Stop collecting " + pin.symbol;
    rm.setAttribute("aria-label", "Remove " + pin.symbol);
    rm.addEventListener("click", function () {
      state.collectorDraft.tickers.splice(idx, 1);
      setCollectorDirty(true);
      renderCollectorTickers();
    });
    rmTd.appendChild(rm);
    tr.appendChild(rmTd);
    body.appendChild(tr);
  });
  if (!state.collectorDraft.tickers.length) {
    var empty = collectorEl("tr");
    var td = collectorEl("td", "collector-muted", "No tickers selected.");
    td.colSpan = 6;
    empty.appendChild(td);
    body.appendChild(empty);
  }
}

function collectorLine(parent, label, value, cls) {
  var row = collectorEl("div", "collector-status__row" + (cls ? " " + cls : ""));
  row.appendChild(collectorEl("span", "collector-status__label", label));
  row.appendChild(collectorEl("span", "collector-status__value", value));
  parent.appendChild(row);
}

function describeRun(rec) {
  var when = rec.actual_run_utc ? collectorEtTime(rec.actual_run_utc) : collectorEtTime(rec.planned_slot_utc);
  var bits = [rec.status, when];
  if (rec.attempt > 1) bits.push("retry");
  if (rec.quote_coverage !== null && rec.quote_coverage !== undefined) {
    bits.push("cov " + Number(rec.quote_coverage).toFixed(2));
  }
  if (rec.pin_drift) bits.push("PIN_DRIFT ($" + rec.requested_strike_step + " \u2192 $" + rec.effective_strike_step + ")");
  if (rec.status === "LOW_QUALITY" && rec.quality_reasons && rec.quality_reasons.length) {
    bits.push(rec.quality_reasons.join("; "));
  }
  if (rec.error) bits.push(rec.error);
  return bits.join(" \u2022 ");
}

function renderCollectorStatus(st) {
  var box = document.getElementById("collector-status");
  if (!box) return;
  box.innerHTML = "";
  var state_txt = st.enabled ? (st.busy ? "ON \u2022 collecting " + (st.current_ticker || "") : "ON") : "OFF";
  if (st.enabled && st.restored_on_startup) state_txt += " (restored after restart)";
  collectorLine(box, "Status", state_txt);
  if (st.next_run) {
    collectorLine(box, "Next run", st.next_run.et + " \u2022 " + st.next_run.poland + (st.next_run.kind === "CLOSING" ? " (closing)" : ""));
  } else {
    collectorLine(box, "Next run", st.enabled ? "none scheduled" : "collection is off");
  }
  if (st.next_closing) {
    collectorLine(box, "Closing snapshot", st.next_closing.et + " \u2022 " + st.next_closing.poland);
  }
  collectorLine(
    box,
    "Today",
    st.today_trading_day
      ? "US session " + st.today_session_et + (st.today_early_close ? " (early close)" : "")
      : "no US session (weekend or exchange holiday)"
  );
  if (st.last_cycle_planned) {
    collectorLine(
      box,
      "Last run",
      "planned " + st.last_cycle_planned.et + (st.last_cycle_actual ? " \u2022 ran " + st.last_cycle_actual.et : "")
    );
  }
  Object.keys(st.last_results || {}).forEach(function (sym) {
    var rec = st.last_results[sym];
    var bad = rec.status !== "OK";
    collectorLine(box, sym, describeRun(rec), bad ? "is-warn" : "");
  });
  if (st.last_error) {
    collectorLine(box, "Last error", st.last_error.ticker + ": " + describeRun(st.last_error), "is-warn");
  }
  collectorLine(
    box,
    "Snapshots today",
    st.snapshots_today + " \u2022 gaps today " + st.gaps_today + " \u2022 missed (7 days) " + st.missed_recent
  );
  if (st.calendar_warning) collectorLine(box, "Calendar", st.calendar_warning, "is-warn");
  var tr = st.tracker;
  if (tr) {
    collectorLine(box, "Tracked spreads", tr.active_count + " active of " + tr.max_active_spreads);
    if (tr.last_result) {
      var lr = tr.last_result;
      var lrBits = [lr.ticker, lr.status, collectorEtTime(lr.actual_run_utc || lr.planned_slot_utc)];
      if (lr.missing_legs && lr.missing_legs.length) lrBits.push(lr.missing_legs.length + " leg(s) missing");
      if (lr.quality_flags && lr.quality_flags.length) lrBits.push(lr.quality_flags.join(", "));
      if (lr.error) lrBits.push(lr.error);
      collectorLine(box, "Last tracker result", lrBits.join(" • "), lr.status === "OK" ? "" : "is-warn");
    }
    (tr.warnings || []).forEach(function (w) {
      collectorLine(box, "Tracker", w, "is-warn");
    });
  }
  var src = collectorEl("div", "collector-muted collector-source");
  src.textContent = "Holidays & early closes: NYSE calendar (until " + st.calendar_valid_until + ")";
  box.appendChild(src);
}

function renderCollector(st) {
  state.collectorStatus = st;
  var label = document.getElementById("collector-toggle-label");
  var dot = document.getElementById("collector-toggle-dot");
  var btn = document.getElementById("collector-toggle-btn");
  if (label) label.textContent = "COLLECTOR: " + (st.enabled ? (st.busy ? "RUNNING" : "ON") : "OFF");
  if (dot) {
    dot.classList.toggle("is-on", Boolean(st.enabled));
    dot.classList.toggle("is-busy", Boolean(st.busy));
  }
  if (btn) {
    btn.title = st.next_run
      ? "Next scheduled snapshot: " + st.next_run.et + " (" + st.next_run.poland + ")"
      : "Scheduled snapshot collection during the US session";
  }
  if (!state.collectorDirty || !state.collectorDraft) {
    state.collectorDraft = collectorDraftFromStatus(st);
    var en = document.getElementById("collector-enabled");
    var iv = document.getElementById("collector-interval");
    if (en) en.checked = state.collectorDraft.enabled;
    if (iv) iv.value = String(state.collectorDraft.interval_min);
    renderCollectorTickers();
  }
  renderCollectorStatus(st);
}

export async function fetchCollectorStatus() {
  try {
    var res = await apiJson("/api/collector/status");
    if (res.ok && res.body) {
      renderCollector(res.body);
      return res.body;
    }
  } catch (_err) {
    /* server unreachable: keep the last view */
  }
  return null;
}

async function saveCollectorSettings() {
  if (!state.collectorDraft) return;
  var draft = state.collectorDraft;
  var body = {
    enabled: draft.enabled,
    interval_min: draft.interval_min,
    tickers: draft.tickers.map(function (t) {
      return {
        symbol: t.symbol,
        range_pct: t.range_pct,
        strike_step: t.strike_step,
        dte_min: t.dte_min,
        dte_max: t.dte_max,
      };
    }),
  };
  var res = await apiJson("/api/collector/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (res.ok && res.body) {
    setCollectorDirty(false);
    state.collectorDraft = null;
    renderCollector(res.body);
    showBanner("info", "Collector settings saved (" + (res.body.enabled ? "ON" : "OFF") + ").", {
      phase: "COLLECTOR",
    });
    return;
  }
  showBanner("warn", extractApiErrorMessage(res.body, res.textSnippet || "Collector settings were not saved."), {
    phase: "COLLECTOR",
    status: res.status,
    detail: res.body && res.body.detail ? String(res.body.detail) : "",
  });
}

function addCollectorTicker() {
  var input = document.getElementById("collector-add-input");
  if (!input || !state.collectorDraft) return;
  var raw = String(input.value || "").trim().toUpperCase();
  if (!/^[A-Z]{1,5}$/.test(raw)) {
    showBanner("warn", "Ticker must be 1\u20135 letters (A\u2013Z).", { phase: "COLLECTOR" });
    return;
  }
  if (state.collectorDraft.tickers.some(function (t) { return t.symbol === raw; })) {
    showBanner("warn", raw + " is already collected.", { phase: "COLLECTOR" });
    return;
  }
  if (state.collectorDraft.tickers.length >= 6) {
    showBanner("warn", "At most 6 collected tickers.", { phase: "COLLECTOR" });
    return;
  }
  var st = state.status || {};
  state.collectorDraft.tickers.push({
    symbol: raw,
    range_pct: 0.35,
    strike_step: 5,
    dte_min: st.dte_min !== undefined && st.dte_min !== null ? Number(st.dte_min) : 30,
    dte_max: st.dte_max !== undefined && st.dte_max !== null ? Number(st.dte_max) : 120,
    pinned_since_utc: null,
  });
  input.value = "";
  setCollectorDirty(true);
  renderCollectorTickers();
}

function setCollectorPanelOpen(open) {
  var panel = document.getElementById("collector-panel");
  var btn = document.getElementById("collector-toggle-btn");
  if (!panel || !btn) return;
  panel.hidden = !open;
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) fetchCollectorStatus();
}

export function wireCollector() {
  var control = document.getElementById("collector-control");
  var panel = document.getElementById("collector-panel");
  var btn = document.getElementById("collector-toggle-btn");
  if (!control || !panel || !btn) return;
  btn.addEventListener("click", function () {
    setCollectorPanelOpen(panel.hidden);
  });
  var en = document.getElementById("collector-enabled");
  if (en) {
    en.addEventListener("change", function () {
      if (state.collectorDraft) state.collectorDraft.enabled = en.checked;
      setCollectorDirty(true);
    });
  }
  var iv = document.getElementById("collector-interval");
  if (iv) {
    iv.addEventListener("change", function () {
      if (state.collectorDraft) state.collectorDraft.interval_min = Number(iv.value);
      setCollectorDirty(true);
    });
  }
  var addBtn = document.getElementById("collector-add-btn");
  if (addBtn) addBtn.addEventListener("click", addCollectorTicker);
  var addInput = document.getElementById("collector-add-input");
  if (addInput) {
    addInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        addCollectorTicker();
      }
    });
  }
  var saveBtn = document.getElementById("collector-save-btn");
  if (saveBtn) saveBtn.addEventListener("click", saveCollectorSettings);
  document.addEventListener("click", function (ev) {
    if (!panel.hidden && ev.target instanceof Node && !control.contains(ev.target)) {
      setCollectorPanelOpen(false);
    }
  });
  control.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && !panel.hidden) {
      ev.preventDefault();
      setCollectorPanelOpen(false);
      btn.focus();
    }
  });
}

export function startCollectorPolling() {
  fetchCollectorStatus();
  window.setInterval(function () {
    fetchCollectorStatus();
  }, COLLECTOR_POLL_MS);
}

function collectorSleep(ms) {
  return new Promise(function (resolve) {
    window.setTimeout(resolve, ms);
  });
}

// POST /api/refresh; while a scheduled cycle holds the refresh lock (HTTP 409), show a
// "waiting for scheduled collection" notice, poll the collector status and retry, up to
// REFRESH_COLLECTOR_WAIT_MS. Returns the last response.
export async function postRefreshWaitingForCollector(bodyObj) {
  var waitedFrom = Date.now();
  var res;
  while (true) {
    res = await apiJson("/api/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      timeoutMs: 120000,
      body: JSON.stringify(bodyObj),
    });
    var code = res && res.body ? res.body.error_code : "";
    if (res.status !== 409 || (code !== "COLLECTOR_BUSY" && code !== "REFRESH_BUSY")) {
      return res;
    }
    if (Date.now() - waitedFrom > REFRESH_COLLECTOR_WAIT_MS) {
      res.collectorWaitTimedOut = true;
      return res;
    }
    if (dom.refreshBtn) dom.refreshBtn.textContent = "WAITING FOR COLLECTION...";
    var st = state.collectorStatus || {};
    showBanner(
      "info",
      code === "COLLECTOR_BUSY"
        ? "Waiting for scheduled collection" +
            (st.current_ticker ? " (" + st.current_ticker + ")" : "") +
            " to finish; your refresh will start automatically."
        : "Waiting for another refresh to finish; your refresh will start automatically.",
      { ticker: state.ticker, phase: "REFRESH_WAIT", errorCode: code }
    );
    var deadline = Date.now() + Math.max(COLLECTOR_BUSY_POLL_MS, (Number(res.body.retry_after_sec) || 3) * 250);
    while (Date.now() < deadline) {
      await collectorSleep(COLLECTOR_BUSY_POLL_MS);
      var latest = await fetchCollectorStatus();
      if (latest && !latest.busy) break;
    }
  }
}

// Called when the corridor stream drops: was it stopped by the scheduled collector?
export async function corridorStoppedByCollector() {
  var st = await fetchCollectorStatus();
  var stop = st && st.last_corridor_stop;
  if (!stop || stop.by !== "collector" || !stop.at_utc) return null;
  var at = new Date(stop.at_utc).getTime();
  if (isNaN(at) || Date.now() - at > COLLECTOR_STOP_NOTICE_MS) return null;
  return stop;
}
