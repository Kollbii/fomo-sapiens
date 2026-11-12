/* IBKR Options Analyzer dashboard — Historical snapshot dropdown. */

import {
  dom,
  state,
} from "./state.js";
import {
  formatRangeShort,
  formatStepShort,
  updateExportLink,
} from "./helpers.js";

// ------------------------------------------------------------------ Snapshot Dropdown
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
    updateExportLink();
    return;
  }

  for (var i = 0; i < items.length; i++) {
    var snap = items[i];
    var opt = document.createElement("option");
    opt.value = snap.snapshot_id;
    var metaBits = [
      snap.market_date,
      "T" + snap.market_data_type,
    ];
    if (snap.range_pct !== null && snap.range_pct !== undefined) {
      metaBits.push(formatRangeShort(snap.range_pct));
    }
    if (snap.strike_step !== null && snap.strike_step !== undefined) {
      metaBits.push(formatStepShort(snap.strike_step));
    }
    metaBits.push(snap.contract_count + "c");
    opt.textContent = snap.snapshot_id + " (" + metaBits.join(" \u2022 ") + ")";
    if (state.snapshotId ? snap.snapshot_id === state.snapshotId : i === 0) {
      opt.selected = true;
    }
    dom.snapshotSelect.appendChild(opt);
  }
  if (!state.snapshotId && items.length) {
    state.snapshotId = items[0].snapshot_id;
  }
  updateExportLink();
}
