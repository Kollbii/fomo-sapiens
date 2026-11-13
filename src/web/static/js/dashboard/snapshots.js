/* IBKR Options Analyzer dashboard — Historical snapshot dropdown and day-grouped picker. */

import {
  formatRangeShort,
  formatStepShort,
  updateExportLink,
} from "./helpers.js";
import {
  dom,
  state,
} from "./state.js";

// ------------------------------------------------------------------ Snapshot Dropdown
// Exchange-time (America/New_York, DST-aware) parts of snapshot_ts_utc, or null.
export function snapshotEtParts(tsUtc) {
  var d = tsUtc ? new Date(tsUtc) : null;
  if (!d || isNaN(d.getTime()) || typeof Intl === "undefined") return null;
  try {
    var parts = {};
    new Intl.DateTimeFormat("en-US", {
      timeZone: "America/New_York",
      weekday: "short",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(d)
      .forEach(function (p) {
        parts[p.type] = p.value;
      });
    var minutes = Number(parts.hour) * 60 + Number(parts.minute);
    var weekend = parts.weekday === "Sat" || parts.weekday === "Sun";
    return {
      dayKey: parts.year + "-" + parts.month + "-" + parts.day,
      dayLabel: parts.day + "." + parts.month + "." + parts.year + " (" + parts.weekday + ")",
      dayShort: parts.day + "." + parts.month,
      time: parts.hour + ":" + parts.minute + " ET",
      // US regular session 09:30-16:00 ET, Mon-Fri. Exchange holidays and early closes are
      // not known here, so they still count as "in session".
      inSession: !weekend && minutes >= 9 * 60 + 30 && minutes < 16 * 60,
    };
  } catch (_err) {
    return null;
  }
}

var SNAPSHOT_OFF_HOURS_TITLE = "Outside the US regular session (09:30\u201316:00 ET, Mon\u2013Fri)";

function snapshotMetaText(snap) {
  var metaBits = ["T" + snap.market_data_type];
  if (snap.range_pct !== null && snap.range_pct !== undefined) {
    metaBits.push(formatRangeShort(snap.range_pct));
  }
  if (snap.strike_step !== null && snap.strike_step !== undefined) {
    metaBits.push(formatStepShort(snap.strike_step));
  }
  metaBits.push(snap.contract_count + "c");
  return metaBits.join(" \u2022 ");
}

// Groups snapshots by exchange date, newest first: [{key, label, items: [{snap, et}]}].
function groupSnapshotsByDay(items) {
  var groups = [];
  var byKey = {};
  for (var i = 0; i < items.length; i++) {
    var et = snapshotEtParts(items[i].snapshot_ts_utc);
    var key = et ? et.dayKey : items[i].market_date || "";
    if (!byKey[key]) {
      byKey[key] = {
        key: key,
        label: et ? et.dayLabel : items[i].market_date || "Unknown date",
        items: [],
      };
      groups.push(byKey[key]);
    }
    byKey[key].items.push({ snap: items[i], et: et });
  }
  return groups;
}

// The native <select> stays the source of truth (value + "change" event -> loadChain);
// the picker renders the same list with collapsible days and off-session markers.
export function renderSnapshotSelect(payload) {
  if (!dom.snapshotSelect) return;
  var items = (payload && payload.snapshots) || [];
  state.snapshots = items;
  dom.snapshotSelect.innerHTML = "";
  if (!items.length) {
    var emptyOpt = document.createElement("option");
    emptyOpt.value = "";
    emptyOpt.textContent = "No snapshots archived";
    dom.snapshotSelect.appendChild(emptyOpt);
    state.snapshotId = "";
    renderSnapshotPicker([], "");
    updateExportLink();
    return;
  }

  var selectedId = state.snapshotId || items[0].snapshot_id;
  var groups = groupSnapshotsByDay(items);
  for (var g = 0; g < groups.length; g++) {
    var optgroup = document.createElement("optgroup");
    optgroup.label = groups[g].label;
    for (var k = 0; k < groups[g].items.length; k++) {
      var entry = groups[g].items[k];
      var opt = document.createElement("option");
      opt.value = entry.snap.snapshot_id;
      opt.textContent =
        (entry.et ? entry.et.time : entry.snap.snapshot_id) + " \u2022 " + snapshotMetaText(entry.snap);
      if (entry.snap.snapshot_id === selectedId) opt.selected = true;
      optgroup.appendChild(opt);
    }
    dom.snapshotSelect.appendChild(optgroup);
  }
  if (!state.snapshotId && items.length) {
    state.snapshotId = items[0].snapshot_id;
  }
  renderSnapshotPicker(groups, selectedId);
  updateExportLink();
}

function makeOffHoursDot() {
  var dot = document.createElement("span");
  dot.className = "snap-picker__dot";
  dot.title = SNAPSHOT_OFF_HOURS_TITLE;
  dot.setAttribute("role", "img");
  dot.setAttribute("aria-label", SNAPSHOT_OFF_HOURS_TITLE);
  return dot;
}

function renderSnapshotPicker(groups, selectedId) {
  var current = document.getElementById("snapshot-picker-current");
  var panel = document.getElementById("snapshot-picker-panel");
  var toggle = document.getElementById("snapshot-picker-toggle");
  if (!current || !panel || !toggle) return;
  if (!state.snapshotPickerOpenDays) state.snapshotPickerOpenDays = {};

  current.innerHTML = "";
  panel.innerHTML = "";
  if (!groups.length) {
    current.textContent = "No snapshots archived";
    toggle.disabled = true;
    return;
  }
  toggle.disabled = false;

  for (var g = 0; g < groups.length; g++) {
    var group = groups[g];
    var hasSelected = false;
    var offCount = 0;
    for (var c = 0; c < group.items.length; c++) {
      if (group.items[c].snap.snapshot_id === selectedId) hasSelected = true;
      if (group.items[c].et && !group.items[c].et.inSession) offCount++;
    }
    var open = Boolean(state.snapshotPickerOpenDays[group.key]);

    var day = document.createElement("div");
    day.className = "snap-picker__day" + (hasSelected ? " is-current" : "");

    var dayBtn = document.createElement("button");
    dayBtn.type = "button";
    dayBtn.className = "snap-picker__day-btn";
    dayBtn.setAttribute("aria-expanded", open ? "true" : "false");
    dayBtn.setAttribute("data-snap-day", group.key);
    var dayCaret = document.createElement("span");
    dayCaret.className = "snap-picker__day-caret";
    dayCaret.setAttribute("aria-hidden", "true");
    var dayLabel = document.createElement("span");
    dayLabel.className = "snap-picker__day-label";
    dayLabel.textContent = group.label;
    var dayCount = document.createElement("span");
    dayCount.className = "snap-picker__day-count";
    dayCount.textContent =
      group.items.length + (group.items.length === 1 ? " snapshot" : " snapshots") +
      (offCount ? " \u2022 " + offCount + " off-hours" : "");
    dayBtn.appendChild(dayCaret);
    dayBtn.appendChild(dayLabel);
    dayBtn.appendChild(dayCount);
    day.appendChild(dayBtn);

    var hours = document.createElement("div");
    hours.className = "snap-picker__hours";
    hours.hidden = !open;
    for (var i = 0; i < group.items.length; i++) {
      var entry = group.items[i];
      var isSelected = entry.snap.snapshot_id === selectedId;
      var off = Boolean(entry.et && !entry.et.inSession);

      var optBtn = document.createElement("button");
      optBtn.type = "button";
      optBtn.className = "snap-picker__opt" + (isSelected ? " is-selected" : "");
      optBtn.setAttribute("role", "option");
      optBtn.setAttribute("aria-selected", isSelected ? "true" : "false");
      optBtn.setAttribute("data-snapshot-id", entry.snap.snapshot_id);
      optBtn.title = entry.snap.snapshot_id + (off ? " \u2014 " + SNAPSHOT_OFF_HOURS_TITLE : "");
      if (off) optBtn.appendChild(makeOffHoursDot());
      var timeEl = document.createElement("span");
      timeEl.className = "snap-picker__time";
      timeEl.textContent = entry.et ? entry.et.time : entry.snap.snapshot_id;
      var metaEl = document.createElement("span");
      metaEl.className = "snap-picker__meta";
      metaEl.textContent = snapshotMetaText(entry.snap);
      optBtn.appendChild(timeEl);
      optBtn.appendChild(metaEl);
      hours.appendChild(optBtn);

      if (isSelected) {
        if (off) current.appendChild(makeOffHoursDot());
        var currentText = document.createElement("span");
        currentText.textContent =
          (entry.et ? entry.et.dayShort + " " + entry.et.time : entry.snap.snapshot_id) +
          " \u2022 " +
          snapshotMetaText(entry.snap);
        current.appendChild(currentText);
      }
    }
    day.appendChild(hours);
    panel.appendChild(day);
  }
}

function setSnapshotPickerOpen(open) {
  var panel = document.getElementById("snapshot-picker-panel");
  var toggle = document.getElementById("snapshot-picker-toggle");
  if (!panel || !toggle) return;
  panel.hidden = !open;
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
}

export function wireSnapshotPicker() {
  var picker = document.getElementById("snapshot-picker");
  var panel = document.getElementById("snapshot-picker-panel");
  var toggle = document.getElementById("snapshot-picker-toggle");
  if (!picker || !panel || !toggle) return;

  toggle.addEventListener("click", function () {
    setSnapshotPickerOpen(panel.hidden);
  });

  panel.addEventListener("click", function (ev) {
    var target = ev.target instanceof Element ? ev.target : null;
    if (!target) return;
    var dayBtn = target.closest("[data-snap-day]");
    if (dayBtn) {
      var key = dayBtn.getAttribute("data-snap-day");
      if (!state.snapshotPickerOpenDays) state.snapshotPickerOpenDays = {};
      var nowOpen = !state.snapshotPickerOpenDays[key];
      state.snapshotPickerOpenDays[key] = nowOpen;
      dayBtn.setAttribute("aria-expanded", nowOpen ? "true" : "false");
      var hours = dayBtn.nextElementSibling;
      if (hours) hours.hidden = !nowOpen;
      return;
    }
    var optBtn = target.closest("[data-snapshot-id]");
    if (optBtn && dom.snapshotSelect) {
      setSnapshotPickerOpen(false);
      toggle.focus();
      var snapId = optBtn.getAttribute("data-snapshot-id") || "";
      if (snapId === state.snapshotId) return;
      dom.snapshotSelect.value = snapId;
      dom.snapshotSelect.dispatchEvent(new Event("change"));
    }
  });

  document.addEventListener("click", function (ev) {
    if (!panel.hidden && ev.target instanceof Node && !picker.contains(ev.target)) {
      setSnapshotPickerOpen(false);
    }
  });

  picker.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && !panel.hidden) {
      ev.preventDefault();
      setSnapshotPickerOpen(false);
      toggle.focus();
    }
  });
}
