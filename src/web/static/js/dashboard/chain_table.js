/* IBKR Options Analyzer dashboard — 18-column option chain table: columns, sorting, header filters, `+Leg`. */

import {
  TABLE_SORT_KEY,
  dom,
  state,
} from "./state.js";
import {
  escapeHtml,
  fmtInt,
  fmtNum,
  formatExpiryIso,
  matchesDteFilterValue,
  snapDteValueToExpiries,
} from "./helpers.js";
import {
  appendDteBucketOptions,
} from "./charts.js";

// ------------------------------------------------------------------ Option Chain Table
var TABLE_COLUMNS = [
  {
    key: "expiry",
    type: "string",
    defaultDir: "asc",
    filterType: "select",
    getValue: function (r) {
      return r.expiry;
    },
  },
  {
    key: "dte",
    type: "number",
    defaultDir: "asc",
    filterType: "expr",
    getValue: function (r) {
      return r.dte;
    },
  },
  {
    key: "right",
    type: "string",
    defaultDir: "asc",
    filterType: "select",
    getValue: function (r) {
      return r.right;
    },
  },
  {
    key: "strike",
    type: "number",
    defaultDir: "asc",
    filterType: "expr",
    getValue: function (r) {
      return r.strike;
    },
  },
  {
    key: "moneyness_pct",
    type: "number",
    defaultDir: "asc",
    filterType: "expr",
    getValue: function (r) {
      return r.moneyness_pct;
    },
  },
  {
    key: "bid",
    type: "number",
    defaultDir: "desc",
    filterType: "expr",
    getValue: function (r) {
      return r.bid;
    },
  },
  {
    key: "ask",
    type: "number",
    defaultDir: "desc",
    filterType: "expr",
    getValue: function (r) {
      return r.ask;
    },
  },
  {
    key: "mark_price",
    type: "number",
    defaultDir: "desc",
    filterType: "expr",
    getValue: function (r) {
      return r.mark_price;
    },
  },
  {
    key: "volume",
    type: "number",
    defaultDir: "desc",
    filterType: "expr",
    getValue: function (r) {
      return r.volume;
    },
  },
  {
    key: "open_interest",
    type: "number",
    defaultDir: "desc",
    filterType: "expr",
    getValue: function (r) {
      return r.open_interest;
    },
  },
  {
    key: "vol_oi",
    type: "number",
    defaultDir: "desc",
    filterType: "expr",
    getValue: function (_r, volOi) {
      return volOi;
    },
  },
  {
    key: "iv",
    type: "number",
    defaultDir: "desc",
    filterType: "expr",
    getValue: function (r) {
      return r.iv !== null && r.iv !== undefined ? Number(r.iv) * 100 : null;
    },
  },
  {
    key: "delta",
    type: "number",
    defaultDir: "desc",
    filterType: "expr",
    getValue: function (r) {
      return r.delta;
    },
  },
  {
    key: "gamma",
    type: "number",
    defaultDir: "desc",
    filterType: "expr",
    getValue: function (r) {
      return r.gamma;
    },
  },
  {
    key: "theta",
    type: "number",
    defaultDir: "asc",
    filterType: "expr",
    getValue: function (r) {
      return r.theta;
    },
  },
  {
    key: "vega",
    type: "number",
    defaultDir: "desc",
    filterType: "expr",
    getValue: function (r) {
      return r.vega;
    },
  },
  {
    key: "greeks_source",
    type: "string",
    defaultDir: "asc",
    filterType: "select",
    getValue: function (r) {
      return r.greeks_source;
    },
  },
];

export var TABLE_COLUMN_BY_KEY = {};
for (var tci = 0; tci < TABLE_COLUMNS.length; tci++) {
  TABLE_COLUMN_BY_KEY[TABLE_COLUMNS[tci].key] = TABLE_COLUMNS[tci];
}

function computeContractVolOi(cRow) {
  var vol = cRow.volume !== null && cRow.volume !== undefined ? Number(cRow.volume) : null;
  var oi =
    cRow.open_interest !== null && cRow.open_interest !== undefined
      ? Number(cRow.open_interest)
      : null;
  if (vol !== null && oi !== null && isFinite(vol) && isFinite(oi) && oi > 0) {
    return vol / oi;
  }
  return null;
}

export function readTableSortPref() {
  try {
    var raw = window.localStorage.getItem(TABLE_SORT_KEY);
    if (raw) {
      var parsed = JSON.parse(raw);
      if (
        parsed &&
        TABLE_COLUMN_BY_KEY[parsed.col] &&
        (parsed.dir === "asc" || parsed.dir === "desc")
      ) {
        return { col: parsed.col, dir: parsed.dir };
      }
    }
  } catch (_err) {
    /* ignore storage failure */
  }
  return { col: null, dir: null };
}

export function writeTableSortPref(sortObj) {
  try {
    if (sortObj && sortObj.col && sortObj.dir) {
      window.localStorage.setItem(TABLE_SORT_KEY, JSON.stringify(sortObj));
    } else {
      window.localStorage.removeItem(TABLE_SORT_KEY);
    }
  } catch (_err) {
    /* ignore storage failure */
  }
}

export function syncTableHeaderSortUI() {
  if (!dom.chainTableHead) return;
  var buttons = dom.chainTableHead.querySelectorAll(".th-sort-btn[data-sort-col]");
  for (var i = 0; i < buttons.length; i++) {
    var btn = buttons[i];
    var col = btn.getAttribute("data-sort-col");
    var th = btn.parentElement;
    var ind = btn.querySelector(".th-sort-ind");
    var ariaVal = "none";
    var glyph = "\u21C5";
    if (state.tableSort && state.tableSort.col === col && state.tableSort.dir) {
      ariaVal = state.tableSort.dir === "asc" ? "ascending" : "descending";
      glyph = state.tableSort.dir === "asc" ? "\u25B2" : "\u25BC";
    }
    if (th) th.setAttribute("aria-sort", ariaVal);
    if (ind) ind.textContent = glyph;
  }
}

function matchesNumericFilterExpr(value, rawExpr) {
  var trimmed = String(rawExpr || "").trim();
  if (!trimmed) return true;
  var clean = trimmed.replace(/%/g, "").trim();
  if (!clean) return true;

  if (value === null || value === undefined || value === "") return false;
  var num = Number(value);
  if (!isFinite(num)) return false;

  var cmpMatch = clean.match(/^(>=|<=|!=|>|<|=)\s*(-?\d+(?:\.\d+)?)$/);
  if (cmpMatch) {
    var op = cmpMatch[1];
    var rhs = Number(cmpMatch[2]);
    if (!isFinite(rhs)) return true;
    if (op === ">=") return num >= rhs;
    if (op === "<=") return num <= rhs;
    if (op === ">") return num > rhs;
    if (op === "<") return num < rhs;
    if (op === "!=") return Math.abs(num - rhs) >= 1e-6;
    return Math.abs(num - rhs) < 1e-6;
  }

  var rangeDots = clean.match(/^(-?\d+(?:\.\d+)?)\s*\.\.\s*(-?\d+(?:\.\d+)?)$/);
  var rangeDash = clean.match(/^(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)$/);
  var rangeMatch = rangeDots || rangeDash;
  if (rangeMatch) {
    var lo = Number(rangeMatch[1]);
    var hi = Number(rangeMatch[2]);
    if (!isFinite(lo) || !isFinite(hi)) return true;
    var minVal = Math.min(lo, hi);
    var maxVal = Math.max(lo, hi);
    return num >= minVal - 1e-9 && num <= maxVal + 1e-9;
  }

  var bareMatch = clean.match(/^(-?\d+(?:\.\d+)?)$/);
  if (bareMatch) {
    var target = Number(bareMatch[1]);
    if (Math.abs(num - target) < 1e-6) return true;
    return String(num).indexOf(clean) !== -1;
  }

  // Incomplete operator expression mid-keystroke (e.g. ">", "-", "30.."): do not blank table.
  return true;
}

function applyTableSort(entries) {
  var sortCfg = state.tableSort || {};
  if (!sortCfg.col || !sortCfg.dir) return entries;
  var colDef = TABLE_COLUMN_BY_KEY[sortCfg.col];
  if (!colDef) return entries;
  var dirMult = sortCfg.dir === "desc" ? -1 : 1;

  var copy = entries.slice();
  copy.sort(function (a, b) {
    var va = colDef.getValue(a.row, a.volOi);
    var vb = colDef.getValue(b.row, b.volOi);
    var aMissing =
      va === null ||
      va === undefined ||
      va === "" ||
      (colDef.type === "number" && !isFinite(Number(va)));
    var bMissing =
      vb === null ||
      vb === undefined ||
      vb === "" ||
      (colDef.type === "number" && !isFinite(Number(vb)));

    // Missing values always sink to the bottom regardless of asc/desc.
    if (aMissing && bMissing) return 0;
    if (aMissing) return 1;
    if (bMissing) return -1;

    var cmp = 0;
    if (colDef.type === "number") {
      cmp = Number(va) - Number(vb);
    } else {
      var sa = String(va);
      var sb = String(vb);
      cmp = sa < sb ? -1 : sa > sb ? 1 : 0;
    }
    if (cmp !== 0) return cmp * dirMult;

    // Deterministic tie-breaker: (dte asc, strike asc, right asc)
    var dteDiff = (Number(a.row.dte) || 0) - (Number(b.row.dte) || 0);
    if (dteDiff !== 0) return dteDiff;
    var strikeDiff = (Number(a.row.strike) || 0) - (Number(b.row.strike) || 0);
    if (strikeDiff !== 0) return strikeDiff;
    return a.row.right < b.row.right ? -1 : a.row.right > b.row.right ? 1 : 0;
  });
  return copy;
}

export function updateHeaderFilterHighlights() {
  if (!dom.chainTableHead) return;
  var controls = dom.chainTableHead.querySelectorAll("[data-filter-col]");
  for (var i = 0; i < controls.length; i++) {
    var ctrl = controls[i];
    var col = ctrl.getAttribute("data-filter-col");
    var active = Boolean(col && state.tableFilters[col]);
    ctrl.classList.toggle("is-filtered", active);
  }
}

export function populateTableHeaderFilters(data) {
  if (dom.thFilterExpiry) {
    var curExp = state.tableFilters.expiry || "";
    dom.thFilterExpiry.innerHTML = '<option value="">All</option>';
    var exps =
      data && data.profiles && data.profiles.expiries ? data.profiles.expiries.slice() : [];
    exps.sort(function (a, b) {
      return a.dte - b.dte;
    });
    var expMatched = false;
    for (var i = 0; i < exps.length; i++) {
      var opt = document.createElement("option");
      opt.value = exps[i].expiry;
      opt.textContent = formatExpiryIso(exps[i].expiry) + " (" + exps[i].dte + "d)";
      if (curExp && curExp === exps[i].expiry) {
        opt.selected = true;
        expMatched = true;
      }
      dom.thFilterExpiry.appendChild(opt);
    }
    if (curExp && !expMatched) {
      state.tableFilters.expiry = "";
    }
  }

  if (dom.thFilterSource) {
    var curSrc = state.tableFilters.greeks_source || "";
    var seenSrc = { IBKR_MODEL: true, BSM_FALLBACK: true, NONE: true };
    if (data && data.contracts) {
      for (var j = 0; j < data.contracts.length; j++) {
        var src = data.contracts[j].greeks_source;
        if (src) seenSrc[src] = true;
      }
    }
    dom.thFilterSource.innerHTML = '<option value="">All</option>';
    var orderedSources = ["IBKR_MODEL", "BSM_FALLBACK", "NONE"];
    Object.keys(seenSrc).forEach(function (k) {
      if (orderedSources.indexOf(k) === -1) orderedSources.push(k);
    });
    for (var s = 0; s < orderedSources.length; s++) {
      var sOpt = document.createElement("option");
      sOpt.value = orderedSources[s];
      sOpt.textContent = orderedSources[s];
      if (curSrc === orderedSources[s]) sOpt.selected = true;
      dom.thFilterSource.appendChild(sOpt);
    }
  }
  updateHeaderFilterHighlights();
}

export function resetAllTableFilters() {
  state.tableFilters = {};
  state.tableSort = { col: null, dir: null };
  writeTableSortPref(state.tableSort);
  if (dom.chainTableHead) {
    var ctrls = dom.chainTableHead.querySelectorAll("[data-filter-col]");
    for (var i = 0; i < ctrls.length; i++) {
      ctrls[i].value = "";
      ctrls[i].classList.remove("is-filtered");
    }
  }
  if (dom.filterDte) dom.filterDte.value = "ALL";
  if (dom.filterRight) dom.filterRight.value = "ALL";
  if (dom.filterUnusual) dom.filterUnusual.checked = false;
  syncDteBucketTabs(state.chainData);
  syncTableHeaderSortUI();
  renderTable(state.chainData);
}

export function syncDteBucketTabs(data) {
  if (!dom.dteBucketTabs) return;
  var activeVal = dom.filterDte ? dom.filterDte.value : "ALL";
  var expiries =
    data && data.profiles && data.profiles.expiries ? data.profiles.expiries : [];
  var buttons = dom.dteBucketTabs.querySelectorAll("[data-dte-bucket]");
  for (var i = 0; i < buttons.length; i++) {
    var btn = buttons[i];
    var bucketVal = btn.getAttribute("data-dte-bucket") || "ALL";
    var hasRows = bucketVal === "ALL";
    if (!hasRows) {
      for (var e = 0; e < expiries.length; e++) {
        if (matchesDteFilterValue(expiries[e].dte, expiries[e].expiry, bucketVal)) {
          hasRows = true;
          break;
        }
      }
    }
    btn.disabled = expiries.length > 0 && !hasRows;
    var isActive = activeVal === bucketVal;
    btn.classList.toggle("active", isActive);
    btn.setAttribute("aria-pressed", isActive ? "true" : "false");
  }
}

export function populateDteFilter(data) {
  if (!dom.filterDte) return;
  var rawPrev = dom.filterDte.value || "ALL";
  dom.filterDte.innerHTML = '<option value="ALL">All DTEs</option>';
  var expiries =
    data && data.profiles && data.profiles.expiries ? data.profiles.expiries.slice() : [];
  expiries.sort(function (a, b) {
    return a.dte - b.dte;
  });
  var previous = snapDteValueToExpiries(rawPrev, expiries, {
    allowBuckets: true,
    emptyValue: "ALL",
  });
  if (expiries.length) {
    appendDteBucketOptions(dom.filterDte, expiries, previous);
    var expGroup = document.createElement("optgroup");
    expGroup.label = "Expirations";
    for (var i = 0; i < expiries.length; i++) {
      var exp = expiries[i];
      var opt = document.createElement("option");
      opt.value = exp.expiry;
      opt.textContent = exp.dte + "d (" + formatExpiryIso(exp.expiry) + ")";
      if (opt.value === previous) opt.selected = true;
      expGroup.appendChild(opt);
    }
    dom.filterDte.appendChild(expGroup);
  }
  dom.filterDte.value = previous || "ALL";
  if (dom.thFilterExpiry && previous !== rawPrev) {
    var syncedExp = previous === "ALL" || previous.indexOf("BUCKET:") === 0 ? "" : previous;
    dom.thFilterExpiry.value = syncedExp;
    state.tableFilters.expiry = syncedExp;
    updateHeaderFilterHighlights();
  }
  syncDteBucketTabs(data);
}

export function renderTable(data) {
  syncTableHeaderSortUI();
  if (!dom.tableBody) return;
  if (!data || !data.contracts || !data.contracts.length) {
    dom.tableBody.innerHTML =
      '<tr><td colspan="18" class="text-left">No cached contracts available. Click REFRESH FROM IBKR.</td></tr>';
    if (dom.tableRowCount) dom.tableRowCount.textContent = "0 CONTRACTS";
    return;
  }

  var anomalyMap = {};
  var anoms = (data.anomalies && data.anomalies.anomalies) || [];
  for (var a = 0; a < anoms.length; a++) {
    var item = anoms[a];
    anomalyMap[item.expiry + "|" + item.strike + "|" + item.right] = item;
  }

  var dteFilter = dom.filterDte && dom.filterDte.value ? dom.filterDte.value : "ALL";
  var rightFilter = dom.filterRight && dom.filterRight.value ? dom.filterRight.value : "ALL";
  var unusualOnly = Boolean(dom.filterUnusual && dom.filterUnusual.checked);
  var activeFilterKeys = Object.keys(state.tableFilters).filter(function (k) {
    return Boolean(state.tableFilters[k]);
  });
  var spot = data.meta.underlying_price;

  var filtered = [];
  for (var i = 0; i < data.contracts.length; i++) {
    var c = data.contracts[i];
    if (!matchesDteFilterValue(c.dte, c.expiry, dteFilter)) continue;
    if (rightFilter !== "ALL" && c.right !== rightFilter) continue;
    var aKey = c.expiry + "|" + c.strike + "|" + c.right;
    var flagged = anomalyMap[aKey];
    if (unusualOnly && !flagged) continue;

    var volOi = computeContractVolOi(c);
    var headerPass = true;
    for (var fk = 0; fk < activeFilterKeys.length; fk++) {
      var fKey = activeFilterKeys[fk];
      var colDef = TABLE_COLUMN_BY_KEY[fKey];
      if (!colDef) continue;
      var fExpr = state.tableFilters[fKey];
      var cellVal = colDef.getValue(c, volOi);
      if (colDef.filterType === "select") {
        if (String(cellVal || "") !== String(fExpr)) {
          headerPass = false;
          break;
        }
      } else if (!matchesNumericFilterExpr(cellVal, fExpr)) {
        headerPass = false;
        break;
      }
    }
    if (!headerPass) continue;
    filtered.push({ row: c, anomaly: flagged || null, volOi: volOi });
  }

  filtered = applyTableSort(filtered);

  if (dom.tableRowCount) {
    dom.tableRowCount.textContent =
      filtered.length + " OF " + data.contracts.length + " CONTRACTS";
  }

  if (!filtered.length) {
    dom.tableBody.innerHTML =
      '<tr><td colspan="18" class="text-left">No contracts match the active table filters.</td></tr>';
    return;
  }

  var html = [];
  for (var r = 0; r < filtered.length; r++) {
    var cRow = filtered[r].row;
    var flag = filtered[r].anomaly;
    var ratio = filtered[r].volOi;
    var isAtm = spot > 0 && Math.abs((cRow.strike - spot) / spot) <= 0.015;
    var classes = [];
    if (isAtm) classes.push("row-atm");
    if (flag) classes.push("row-anomaly");

    var vol = cRow.volume !== null && cRow.volume !== undefined ? Number(cRow.volume) : null;
    var oi =
      cRow.open_interest !== null && cRow.open_interest !== undefined
        ? Number(cRow.open_interest)
        : null;
    var ratioWidth = ratio !== null ? Math.min(100, Math.round(ratio * 40)) : 0;
    var ratioLabel = ratio !== null ? ratio.toFixed(2) + "x" : "—";
    var flagTags = "";
    if (flag) {
      if (flag.is_vol_gt_oi) flagTags += '<span class="anomaly-tag">V&gt;OI</span>';
      if (flag.is_high_vol_zscore) flagTags += '<span class="anomaly-tag">Z</span>';
    }

    html.push(
      '<tr class="' +
        classes.join(" ") +
        '">' +
        '<td class="text-left">' +
        escapeHtml(cRow.expiry) +
        "</td>" +
        "<td>" +
        escapeHtml(cRow.dte) +
        "d</td>" +
        '<td class="text-left"><span class="right-pill right-pill--' +
        escapeHtml(cRow.right) +
        '">' +
        escapeHtml(cRow.right) +
        "</span>" +
        flagTags +
        "</td>" +
        "<td>$" +
        fmtNum(cRow.strike, 2) +
        "</td>" +
        "<td>" +
        (cRow.moneyness_pct >= 0 ? "+" : "") +
        fmtNum(cRow.moneyness_pct, 1) +
        "%</td>" +
        "<td>" +
        fmtNum(cRow.bid, 2) +
        "</td>" +
        "<td>" +
        fmtNum(cRow.ask, 2) +
        "</td>" +
        "<td>" +
        fmtNum(cRow.mark_price, 2) +
        "</td>" +
        "<td>" +
        fmtInt(vol) +
        "</td>" +
        "<td>" +
        fmtInt(oi) +
        "</td>" +
        '<td><span class="vol-oi-cell"><span>' +
        ratioLabel +
        '</span><span class="vol-oi-bar"><span class="vol-oi-fill" style="width:' +
        ratioWidth +
        '%"></span></span></span></td>' +
        "<td>" +
        (cRow.iv !== null && cRow.iv !== undefined ? (cRow.iv * 100).toFixed(1) + "%" : "—") +
        "</td>" +
        "<td>" +
        fmtNum(cRow.delta, 3) +
        "</td>" +
        "<td>" +
        fmtNum(cRow.gamma, 4) +
        "</td>" +
        "<td>" +
        fmtNum(cRow.theta, 3) +
        "</td>" +
        "<td>" +
        fmtNum(cRow.vega, 3) +
        "</td>" +
        '<td class="text-left"><span class="source-tag">' +
        escapeHtml(cRow.greeks_source) +
        "</span></td>" +
        '<td class="chain-leg-col"><button type="button" class="btn btn-xs chain-add-leg-btn" data-leg-expiry="' +
        escapeHtml(cRow.expiry) +
        '" data-leg-right="' +
        escapeHtml(cRow.right) +
        '" data-leg-strike="' +
        escapeHtml(cRow.strike) +
        '" title="Add this contract to Strategy Lab">+Leg</button></td>' +
        "</tr>"
    );
  }
  dom.tableBody.innerHTML = html.join("");
}
