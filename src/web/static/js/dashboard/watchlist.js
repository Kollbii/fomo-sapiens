/* IBKR Options Analyzer dashboard — Watchlist panel, per-row freshness markers and sequential multi-ticker batch load. */

import {
  WATCHLIST_SELECTION_KEY,
  dom,
  state,
} from "./state.js";
import {
  buildRefreshDtePayload,
  extractApiErrorMessage,
  readPref,
  showBanner,
  writePref,
} from "./helpers.js";
import {
  apiJson,
  loadChain,
} from "./network.js";
import {
  stopCorridorStream,
} from "./corridor.js";
import {
  snapshotEtParts,
} from "./snapshots.js";

var BATCH_POLL_MS = 1500;
var BATCH_POLL_WARN_AFTER = 3;
var lastNoticeShown = "";

var SYM_FRESH = "\u25CF";
var SYM_NONE = "\u25CB";
var SYM_PENDING = "\u25D0";
var SYM_FAILED = "\u2715";

// ------------------------------------------------------------------ Helpers
function isLiveBatch(batch) {
  return Boolean(batch) && (batch.state === "running" || batch.state === "cancelling");
}

function batchItem(sym) {
  var batch = state.watchlistBatch;
  if (!batch || !batch.items) return null;
  for (var i = 0; i < batch.items.length; i++) {
    if (batch.items[i].ticker === sym) return batch.items[i];
  }
  return null;
}

function retryTickers(batch) {
  if (!batch || !batch.items) return [];
  return batch.items
    .filter(function (it) {
      return it.state !== "done";
    })
    .map(function (it) {
      return it.ticker;
    });
}

function isChecked(sym) {
  return !state.watchlistUnchecked[sym];
}

function selectedTickers() {
  return state.watchlist.filter(isChecked);
}

function readSelection() {
  var out = {};
  try {
    var parsed = JSON.parse(readPref(WATCHLIST_SELECTION_KEY, "{}"));
    var list = parsed && Array.isArray(parsed.unchecked) ? parsed.unchecked : [];
    for (var i = 0; i < list.length; i++) {
      if (typeof list[i] === "string") out[list[i]] = true;
    }
  } catch (_err) {
    /* ignore corrupt prefs */
  }
  return out;
}

function writeSelection() {
  var pruned = {};
  for (var i = 0; i < state.watchlist.length; i++) {
    var sym = state.watchlist[i];
    if (state.watchlistUnchecked[sym]) pruned[sym] = true;
  }
  state.watchlistUnchecked = pruned;
  writePref(WATCHLIST_SELECTION_KEY, JSON.stringify({ unchecked: Object.keys(pruned) }));
}

function markerFor(sym) {
  var item = batchItem(sym);
  var live = isLiveBatch(state.watchlistBatch);
  if (item) {
    if (item.state === "queued") {
      return { cls: "is-pending", sym: SYM_PENDING, text: "queued\u2026", sr: "queued for batch load" };
    }
    if (item.state === "running") {
      return { cls: "is-pending", sym: SYM_PENDING, text: "loading\u2026", sr: "loading from IBKR" };
    }
    if (item.state === "failed") {
      var why = item.error || "load failed";
      return {
        cls: "is-failed",
        sym: SYM_FAILED,
        text: "failed",
        sr: "failed: " + why + (item.partial ? " (partial snapshot saved)" : ""),
        title: why,
        partial: Boolean(item.partial),
      };
    }
    if (item.state === "not_started") {
      return { cls: "is-none", sym: SYM_NONE, text: "not started", sr: "not started" };
    }
    if (item.state === "cancelled") {
      return {
        cls: "is-none",
        sym: "\u2014",
        text: "cancelled",
        sr: item.partial ? "cancelled, partial snapshot saved" : "cancelled",
        partial: Boolean(item.partial),
      };
    }
    if (item.state === "done" && live) {
      return { cls: "is-fresh", sym: SYM_FRESH, text: "saved", sr: "snapshot saved" };
    }
  }
  var entry = state.watchlistEntries[sym];
  if (entry && entry.is_today_et) {
    var et = snapshotEtParts(entry.latest_snapshot_utc);
    var when = et ? et.time : "today";
    return { cls: "is-fresh", sym: SYM_FRESH, text: when, sr: "latest snapshot today at " + when };
  }
  return { cls: "is-none", sym: SYM_NONE, text: "\u2014", sr: "no snapshot today" };
}

// ------------------------------------------------------------------ Rendering
function buildRow(sym) {
  var li = document.createElement("li");
  li.className = "wl-row";
  li.setAttribute("data-wl-row", sym);

  var check = document.createElement("input");
  check.type = "checkbox";
  check.className = "wl-row__check";
  check.setAttribute("data-wl-check", sym);
  check.setAttribute("aria-label", "Select " + sym + " for batch load");

  var symBtn = document.createElement("button");
  symBtn.type = "button";
  symBtn.className = "wl-row__sym";
  symBtn.setAttribute("data-wl-view", sym);
  symBtn.title = "View saved " + sym + " snapshot (no IBKR call)";
  symBtn.textContent = sym;

  var status = document.createElement("span");
  status.className = "wl-row__status";
  var mark = document.createElement("span");
  mark.className = "wl-row__mark";
  mark.setAttribute("aria-hidden", "true");
  var text = document.createElement("span");
  text.className = "wl-row__text";
  text.setAttribute("aria-hidden", "true");
  var part = document.createElement("span");
  part.className = "wl-row__partial";
  part.setAttribute("aria-hidden", "true");
  part.hidden = true;
  part.textContent = "\u25D1 partial";
  var sr = document.createElement("span");
  sr.className = "sr-only";
  status.appendChild(mark);
  status.appendChild(text);
  status.appendChild(part);
  status.appendChild(sr);

  var rm = document.createElement("button");
  rm.type = "button";
  rm.className = "wl-row__remove";
  rm.setAttribute("data-wl-remove", sym);
  rm.setAttribute("aria-label", "Remove " + sym + " from watchlist");
  rm.textContent = "\u00D7";

  li.appendChild(check);
  li.appendChild(symBtn);
  li.appendChild(status);
  li.appendChild(rm);
  return li;
}

function updateRows() {
  if (!dom.watchlistRows) return;
  var rows = dom.watchlistRows.querySelectorAll("[data-wl-row]");
  for (var i = 0; i < rows.length; i++) {
    var li = rows[i];
    var sym = li.getAttribute("data-wl-row");
    var m = markerFor(sym);
    var status = li.querySelector(".wl-row__status");
    status.className = "wl-row__status " + m.cls;
    status.title = m.title || "";
    li.querySelector(".wl-row__mark").textContent = m.sym;
    li.querySelector(".wl-row__text").textContent = m.text;
    var partEl = li.querySelector(".wl-row__partial");
    partEl.hidden = !m.partial;
    partEl.title = m.partial
      ? "Partial snapshot: only finished pages were saved; needs a complete re-fetch."
      : "";
    li.querySelector(".sr-only").textContent = m.sr;
    li.querySelector("[data-wl-check]").checked = isChecked(sym);
    var item = batchItem(sym);
    var inFlight = Boolean(item) && isLiveBatch(state.watchlistBatch) &&
      (item.state === "queued" || item.state === "running");
    li.querySelector("[data-wl-remove]").disabled = !state.watchlistWritable || inFlight;
    li.classList.toggle("is-active", sym === state.ticker);
  }
}

function renderRows() {
  if (!dom.watchlistRows) return;
  dom.watchlistRows.innerHTML = "";
  for (var i = 0; i < state.watchlist.length; i++) {
    dom.watchlistRows.appendChild(buildRow(state.watchlist[i]));
  }
  if (dom.watchlistEmpty) dom.watchlistEmpty.hidden = state.watchlist.length > 0;
  if (dom.watchlistCount) dom.watchlistCount.textContent = String(state.watchlist.length);
  updateRows();
  syncControls();
}

function syncControls() {
  var busy = Boolean(state.watchlistBatchBusy);
  var n = selectedTickers().length;
  if (dom.watchlistSelectedCount) dom.watchlistSelectedCount.textContent = String(n);
  var loadDisabled = busy || n === 0 || Boolean(state.loading);
  if (dom.watchlistLoadBarCount) dom.watchlistLoadBarCount.textContent = String(n);
  [dom.watchlistLoadSelected, dom.watchlistLoadBar].forEach(function (btn) {
    if (btn) btn.disabled = loadDisabled;
  });
  if (dom.refreshBtn) {
    dom.refreshBtn.disabled = busy || Boolean(state.loading);
    dom.refreshBtn.title = busy ? "Disabled while a watchlist batch load is running" : "";
  }
  if (dom.watchlistNotice) {
    dom.watchlistNotice.hidden = !state.watchlistNotice;
    dom.watchlistNotice.textContent = state.watchlistNotice || "";
  }
}

function renderProgress() {
  if (!dom.watchlistProgress) return;
  var b = state.watchlistBatch;
  if (!b) {
    dom.watchlistProgress.hidden = true;
    return;
  }
  var live = isLiveBatch(b);
  dom.watchlistProgress.hidden = false;
  if (dom.watchlistProgressRunning) dom.watchlistProgressRunning.hidden = !live;
  if (dom.watchlistSummary) dom.watchlistSummary.hidden = live;
  if (live) {
    var total = Number(b.total) || 0;
    var finished =
      (Number(b.done_count) || 0) + (Number(b.failed_count) || 0) + (Number(b.cancelled_count) || 0);
    var pct = total > 0 ? Math.min(100, Math.round((finished / total) * 100)) : 0;
    if (dom.watchlistProgressFill) dom.watchlistProgressFill.style.width = pct + "%";
    if (dom.watchlistProgressTrack) dom.watchlistProgressTrack.setAttribute("aria-valuenow", String(pct));
    var cur = b.current_ticker || "";
    if (dom.watchlistProgressText) {
      dom.watchlistProgressText.textContent =
        b.state === "cancelling"
          ? "Stopping\u2026"
          : finished + "/" + total + " \u00B7 " + (cur ? cur + "\u2026" : "starting\u2026");
    }
    if (dom.watchlistCancel) dom.watchlistCancel.disabled = b.state === "cancelling";
    return;
  }
  var text = (b.done_count || 0) + " loaded, " + (b.failed_count || 0) + " failed";
  if (b.not_started_count) text += ", " + b.not_started_count + " not started";
  var partialCount = (b.items || []).filter(function (it) {
    return it.partial;
  }).length;
  if (partialCount) text += ", " + partialCount + " partial";
  if (b.cancelled_count) text += ", " + b.cancelled_count + " cancelled";
  if (dom.watchlistSummaryText) dom.watchlistSummaryText.textContent = text;
  var retryN = retryTickers(b).length;
  if (dom.watchlistRetry) {
    dom.watchlistRetry.hidden = retryN === 0;
    dom.watchlistRetry.textContent = "Retry (" + retryN + ")";
  }
}

// ------------------------------------------------------------------ Watchlist data
function applyWatchlist(body) {
  if (!body || !Array.isArray(body.tickers)) return;
  state.watchlist = body.tickers.slice();
  var entries = {};
  (body.entries || []).forEach(function (e) {
    if (e && e.ticker) entries[e.ticker] = e;
  });
  state.watchlistEntries = entries;
  if (body.max_size) state.watchlistMaxSize = Number(body.max_size);
  if (typeof body.allow_custom === "boolean") state.watchlistAllowCustom = body.allow_custom;
  state.watchlistWritable = body.writable !== false;
  state.watchlistNotice = body.notice || "";
  writeSelection();
  renderRows();
  if (state.watchlistNotice && state.watchlistNotice !== lastNoticeShown) {
    lastNoticeShown = state.watchlistNotice;
    showBanner("warn", state.watchlistNotice, { phase: "WATCHLIST" });
  }
}

export async function refreshWatchlist() {
  try {
    var res = await apiJson("/api/watchlist");
    if (res.ok && res.body) {
      applyWatchlist(res.body);
      return true;
    }
  } catch (_err) {
    /* markers stay as they are */
  }
  return false;
}

async function removeTicker(sym) {
  var res;
  try {
    res = await apiJson("/api/watchlist/tickers/" + encodeURIComponent(sym), { method: "DELETE" });
  } catch (err) {
    showBanner("error", "Unable to remove " + sym + " from the watchlist.", {
      ticker: sym,
      phase: "WATCHLIST",
      detail: err && err.message ? err.message : String(err || ""),
    });
    return;
  }
  if (res.ok && res.body) {
    applyWatchlist(res.body);
    return;
  }
  showBanner("error", extractApiErrorMessage(res.body, "Unable to remove " + sym + "."), {
    ticker: sym,
    phase: "WATCHLIST",
    status: res.status,
    errorCode: res.body && res.body.error_code,
  });
}

// ------------------------------------------------------------------ Batch lifecycle
function stopPoll() {
  if (state.watchlistPollTimer) {
    window.clearTimeout(state.watchlistPollTimer);
    state.watchlistPollTimer = null;
  }
}

function schedulePoll() {
  if (state.watchlistPollTimer) return;
  state.watchlistPollTimer = window.setTimeout(pollBatch, BATCH_POLL_MS);
}

function applyBatch(batch, source) {
  state.watchlistBatch = batch;
  var live = isLiveBatch(batch);
  state.watchlistBatchBusy = live;
  renderProgress();
  updateRows();
  syncControls();
  if (live) {
    schedulePoll();
    return;
  }
  stopPoll();
  if (source !== "attach") {
    onBatchFinished(batch);
  }
}

async function onBatchFinished(batch) {
  // Lets the Best Option screener recalculate from the new snapshots and auto-resume live mode.
  document.dispatchEvent(new Event("ibkr:batch-finished"));
  document.dispatchEvent(new Event("ibkr:lock-released"));
  await refreshWatchlist();
  var follow = state.watchlistFollow;
  state.watchlistFollow = null;
  if (follow) {
    var item = null;
    for (var i = 0; i < batch.items.length; i++) {
      if (batch.items[i].ticker === follow.ticker) item = batch.items[i];
    }
    if (item && item.state === "done") {
      // User-initiated single load: the dashboard view follows it.
      state.watchlistBatch = null;
      renderProgress();
      updateRows();
      await loadChain(follow.ticker, "");
      if (follow.notice) {
        showBanner("info", follow.notice, { ticker: follow.ticker, phase: "WATCHLIST" });
      }
      return;
    }
  }
  if (batch.state === "aborted" && batch.notices && batch.notices.length) {
    showBanner("warn", batch.notices[batch.notices.length - 1], { phase: "BATCH_LOAD" });
    return;
  }
  if (follow) {
    var fItem = null;
    for (var j = 0; j < batch.items.length; j++) {
      if (batch.items[j].ticker === follow.ticker) fItem = batch.items[j];
    }
    if (fItem && fItem.state === "failed") {
      showBanner("error", fItem.error || "Load failed for " + follow.ticker + ".", {
        ticker: follow.ticker,
        phase: "BATCH_LOAD",
        errorCode: fItem.error_code,
      });
      return;
    }
    if (fItem && (fItem.state === "cancelled" || fItem.state === "not_started")) {
      showBanner("info", "Load of " + follow.ticker + " was cancelled.", {
        ticker: follow.ticker,
        phase: "BATCH_LOAD",
      });
      return;
    }
  }
  if (batch.notices && batch.notices.length) {
    showBanner("info", batch.notices.join(" "), { phase: "BATCH_LOAD" });
  }
}

async function pollBatch() {
  state.watchlistPollTimer = null;
  var batch = state.watchlistBatch;
  if (!isLiveBatch(batch)) return;
  var res = null;
  try {
    res = await apiJson("/api/refresh/batch/" + encodeURIComponent(batch.batch_id));
  } catch (_err) {
    res = null;
  }
  if (res && res.ok && res.body) {
    state.watchlistPollFailures = 0;
    applyBatch(res.body, "poll");
    return;
  }
  if (res && res.status === 404) {
    state.watchlistBatch = null;
    state.watchlistBatchBusy = false;
    state.watchlistFollow = null;
    renderProgress();
    updateRows();
    syncControls();
    document.dispatchEvent(new Event("ibkr:lock-released"));
    showBanner("warn", "Batch status was lost (server restarted?). Reloading watchlist markers.", {
      phase: "BATCH_LOAD",
      status: 404,
    });
    refreshWatchlist();
    return;
  }
  state.watchlistPollFailures += 1;
  if (state.watchlistPollFailures === BATCH_POLL_WARN_AFTER) {
    showBanner("warn", "Lost contact with the batch status endpoint; retrying\u2026", {
      phase: "BATCH_LOAD",
    });
  }
  schedulePoll();
}

async function attachCurrentBatch() {
  try {
    var res = await apiJson("/api/refresh/batch/current");
    if (res.ok && res.body && isLiveBatch(res.body)) {
      state.watchlistPollFailures = 0;
      applyBatch(res.body, "attach");
      return true;
    }
  } catch (_err) {
    /* nothing to attach */
  }
  return false;
}

async function startBatch(tickers, follow) {
  if (!tickers.length || state.watchlistBatchBusy) return false;
  if (state.loading) {
    showBanner("warn", "A single IBKR refresh is in progress. Try again when it finishes.", {
      phase: "BATCH_LOAD",
    });
    return false;
  }
  if (state.corridorStreaming || state.corridorStreamSource) {
    stopCorridorStream({ silent: true });
  }
  var dte = buildRefreshDtePayload();
  var body = {
    tickers: tickers,
    range_pct: dom.rangeSelect ? Number(dom.rangeSelect.value) : 0.2,
    dte_min: dte.dte_min,
    dte_max: dte.dte_max,
    include_short_dte: dte.include_short_dte,
  };
  state.watchlistFollow = follow || null;
  state.watchlistBatchBusy = true;
  syncControls();
  var res;
  try {
    res = await apiJson("/api/refresh/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    state.watchlistBatchBusy = false;
    state.watchlistFollow = null;
    syncControls();
    showBanner("error", "Unable to start the batch load (network error).", {
      phase: "BATCH_LOAD",
      detail: err && err.message ? err.message : String(err || ""),
    });
    return false;
  }
  if (res.ok && res.body && res.body.batch_id) {
    state.watchlistPollFailures = 0;
    applyBatch(res.body, "start");
    return true;
  }
  state.watchlistBatchBusy = false;
  state.watchlistFollow = null;
  syncControls();
  showBanner(res.status === 409 ? "warn" : "error", extractApiErrorMessage(res.body, "Batch load could not be started."), {
    phase: "BATCH_LOAD",
    status: res.status,
    errorCode: res.body && res.body.error_code,
    detail: res.body && res.body.detail ? String(res.body.detail) : "",
  });
  if (res.status === 409) {
    attachCurrentBatch();
  }
  return false;
}

async function cancelBatch() {
  var batch = state.watchlistBatch;
  if (!isLiveBatch(batch)) return;
  if (dom.watchlistCancel) dom.watchlistCancel.disabled = true;
  if (dom.watchlistProgressText) dom.watchlistProgressText.textContent = "Stopping\u2026";
  try {
    var res = await apiJson("/api/refresh/batch/" + encodeURIComponent(batch.batch_id) + "/cancel", {
      method: "POST",
    });
    if (res.ok && res.body) {
      applyBatch(res.body, "poll");
      return;
    }
    showBanner("error", extractApiErrorMessage(res.body, "Unable to cancel the batch."), {
      phase: "BATCH_LOAD",
      status: res.status,
      errorCode: res.body && res.body.error_code,
    });
  } catch (err) {
    showBanner("error", "Unable to cancel the batch (network error).", {
      phase: "BATCH_LOAD",
      detail: err && err.message ? err.message : String(err || ""),
    });
  }
  if (dom.watchlistCancel) dom.watchlistCancel.disabled = false;
}

// ------------------------------------------------------------------ Add ticker (Enter)
async function addAndLoadTicker(raw) {
  var sym = String(raw || "").trim().toUpperCase();
  if (!sym) return;
  var added = false;
  var notAddedFull = false;
  var res;
  try {
    res = await apiJson("/api/watchlist/tickers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker: sym }),
    });
  } catch (err) {
    showBanner("error", "Unable to add " + sym + " (network error).", {
      ticker: sym,
      phase: "WATCHLIST",
      detail: err && err.message ? err.message : String(err || ""),
    });
    return;
  }
  var code = res.body && res.body.error_code;
  if (res.ok && res.body) {
    applyWatchlist(res.body);
    added = true;
  } else if (res.status === 409 && code === "WATCHLIST_FULL") {
    notAddedFull = true;
  } else if (code === "STORAGE_UNAVAILABLE") {
    showBanner("warn", extractApiErrorMessage(res.body, "Watchlist storage is unavailable."), {
      ticker: sym,
      phase: "WATCHLIST",
      status: res.status,
      errorCode: code,
    });
  } else {
    showBanner("error", extractApiErrorMessage(res.body, "Unable to add " + sym + "."), {
      ticker: sym,
      phase: "WATCHLIST",
      status: res.status,
      errorCode: code,
    });
    return;
  }
  if (dom.watchlistAddInput) dom.watchlistAddInput.value = "";

  if (state.watchlistBatchBusy) {
    var msg = added
      ? "Batch load running. " + sym + " added; load it after the batch."
      : "Batch load running. " + sym + " was not added; try again after the batch.";
    showBanner(added ? "info" : "warn", msg, { ticker: sym, phase: "WATCHLIST" });
    return;
  }
  var max = state.watchlistMaxSize;
  var notice = notAddedFull
    ? "Loaded " + sym + ". Not added: watchlist is full (" + max + "/" + max + ")."
    : "";
  await startBatch([sym], { ticker: sym, notice: notice });
}

// ------------------------------------------------------------------ Panel wiring
function setPanelOpen(open) {
  if (!dom.watchlistPanel || !dom.watchlistToggle) return;
  dom.watchlistPanel.hidden = !open;
  dom.watchlistToggle.setAttribute("aria-expanded", open ? "true" : "false");
}

export function wireWatchlist() {
  var picker = dom.watchlistPicker;
  var panel = dom.watchlistPanel;
  var toggle = dom.watchlistToggle;
  if (!picker || !panel || !toggle) return;

  toggle.addEventListener("click", function () {
    setPanelOpen(panel.hidden);
  });

  document.addEventListener("click", function (ev) {
    if (!panel.hidden && ev.target instanceof Node && !picker.contains(ev.target)) {
      setPanelOpen(false);
    }
  });

  picker.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && !panel.hidden) {
      ev.preventDefault();
      setPanelOpen(false);
      toggle.focus();
    }
  });

  if (dom.watchlistRows) {
    dom.watchlistRows.addEventListener("change", function (ev) {
      var t = ev.target instanceof Element ? ev.target : null;
      var sym = t ? t.getAttribute("data-wl-check") : null;
      if (!sym) return;
      if (t.checked) delete state.watchlistUnchecked[sym];
      else state.watchlistUnchecked[sym] = true;
      writeSelection();
      syncControls();
    });
    dom.watchlistRows.addEventListener("click", function (ev) {
      var t = ev.target instanceof Element ? ev.target : null;
      if (!t) return;
      var viewBtn = t.closest("[data-wl-view]");
      if (viewBtn) {
        setPanelOpen(false);
        toggle.focus();
        loadChain(viewBtn.getAttribute("data-wl-view"), "");
        return;
      }
      var rmBtn = t.closest("[data-wl-remove]");
      if (rmBtn && !rmBtn.disabled) {
        var rmSym = rmBtn.getAttribute("data-wl-remove");
        var li = rmBtn.closest("[data-wl-row]");
        var next = li && (li.nextElementSibling || li.previousElementSibling);
        removeTicker(rmSym).then(function () {
          var target = next ? next.querySelector("[data-wl-view]") : null;
          (target || toggle).focus();
        });
      }
    });
  }

  function selectAll(pred) {
    var next = {};
    state.watchlist.forEach(function (sym) {
      if (!pred(sym)) next[sym] = true;
    });
    state.watchlistUnchecked = next;
    writeSelection();
    updateRows();
    syncControls();
  }
  if (dom.watchlistSelectAll) {
    dom.watchlistSelectAll.addEventListener("click", function () {
      selectAll(function () {
        return true;
      });
    });
  }
  if (dom.watchlistSelectNone) {
    dom.watchlistSelectNone.addEventListener("click", function () {
      selectAll(function () {
        return false;
      });
    });
  }
  if (dom.watchlistSelectStale) {
    dom.watchlistSelectStale.addEventListener("click", function () {
      selectAll(function (sym) {
        var e = state.watchlistEntries[sym];
        return !(e && e.is_today_et);
      });
    });
  }
  [dom.watchlistLoadSelected, dom.watchlistLoadBar].forEach(function (btn) {
    if (btn) {
      btn.addEventListener("click", function () {
        startBatch(selectedTickers(), null);
      });
    }
  });
  if (dom.watchlistCancel) {
    dom.watchlistCancel.addEventListener("click", cancelBatch);
  }
  if (dom.watchlistRetry) {
    dom.watchlistRetry.addEventListener("click", function () {
      startBatch(retryTickers(state.watchlistBatch), null);
    });
  }
  if (dom.watchlistSummaryDismiss) {
    dom.watchlistSummaryDismiss.addEventListener("click", function () {
      if (isLiveBatch(state.watchlistBatch)) return;
      state.watchlistBatch = null;
      renderProgress();
      updateRows();
    });
  }
  if (dom.watchlistAddForm) {
    dom.watchlistAddForm.addEventListener("submit", function (ev) {
      ev.preventDefault();
      addAndLoadTicker(dom.watchlistAddInput ? dom.watchlistAddInput.value : "");
    });
  }
  if (dom.watchlistAddInput && !state.watchlistAllowCustom) {
    dom.watchlistAddInput.title =
      "Custom tickers are disabled: only configured tickers can be added.";
  }
  document.addEventListener("ibkr:snapshot-saved", function () {
    refreshWatchlist();
  });
}

export async function initWatchlist() {
  state.watchlistUnchecked = readSelection();
  renderRows();
  await refreshWatchlist();
  await attachCurrentBatch();
}
