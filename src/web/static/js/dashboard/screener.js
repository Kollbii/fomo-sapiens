/* IBKR Options Analyzer dashboard — Best Option screener card: controls, sortable/filterable table,
   row expansion, "Open in Strategy Lab" and the optional live (SSE) stream. */

import { SCREENER_PREFS_KEY, dom, state } from "./state.js";
import {
  extractApiErrorMessage,
  fmtInt,
  fmtMoney,
  fmtNum,
  parseDashboardDteWindow,
  showBanner,
} from "./helpers.js";
import { apiJson, loadChain } from "./network.js";
import { snapshotEtParts } from "./snapshots.js";
import {
  evaluateStrategyRequest,
  renderStrategyLegsTable,
  setActiveStrategyPresetUI,
} from "./strategy_lab.js";
import { applyPanelVisibility } from "./panels.js";
import { stopCorridorStream } from "./corridor.js";

// ------------------------------------------------------------------ Constants
var PANEL_ID = "best_option";
var DASH = "—";
var LIVE_MAX_ROWS = 120;
var REALLOC_DEBOUNCE_MS = 300;
var RESUME_DELAY_MS = 600;
var BANNER_META = { ticker: "SCREENER", phase: "SCREENER" };

var KIND_LABELS = {
  BULL_PUT_SPREAD: "Bull Put",
  BEAR_CALL_SPREAD: "Bear Call",
  BULL_CALL_SPREAD: "Bull Call",
  BEAR_PUT_SPREAD: "Bear Put",
  DIAGONAL_SPREAD: "Diagonal",
  IRON_CONDOR: "Iron Condor",
};

var STATUS_LABELS = {
  filtered: "FILTERED",
  not_buildable: "NOT BUILDABLE",
  no_data: "NO DATA",
};

// Server-side stop reasons after which the UI resumes live mode once the lock holder finished.
var AUTO_RESUME_REASONS = {
  refresh_chain: "IBKR refresh",
  batch_refresh: "batch load",
  ledger: "Trade Ledger operation",
  ledger_sync: "Trade Ledger sync",
  ledger_mtm: "Trade Ledger MTM refresh",
  corridor_snapshot: "Wall Corridor snapshot",
};

var STOP_NOTICES = {
  superseded: "Live screener stopped: a newer screener stream replaced it.",
  corridor_stream: "Live screener stopped: Wall Corridor stream started.",
  shutdown: "Live screener stopped: server shutting down.",
};

var OVERLAY_FIELDS = [
  "underlying_price",
  "legs",
  "legs_label",
  "pricing",
  "risk",
  "net_greeks",
  "min_leg_open_interest",
];

// ------------------------------------------------------------------ Module state
var S = {
  prefs: null,
  result: null,
  rows: [],
  rowById: {},
  order: [],
  expanded: {},
  overlay: {},
  lastRunBody: null,
  runSeq: 0,
  running: false,
  stale: false,
  runNotices: [],
  liveNotices: [],
  es: null,
  streamId: "",
  opened: false,
  lastSentIds: [],
  reallocTimer: null,
  resumePending: false,
  resumeTimer: null,
  liveInfo: null,
  rowCache: {},
  colsKey: "",
  initialized: false,
};

var el = {};

// ------------------------------------------------------------------ Columns
function numOrNull(v) {
  if (v === null || v === undefined) return null;
  var n = Number(v);
  return Number.isNaN(n) ? null : n;
}

function kindLabel(kind) {
  if (!kind) return DASH;
  return KIND_LABELS[kind] || String(kind).replace(/_/g, " ");
}

function signedMoney(val) {
  var n = numOrNull(val);
  if (n === null) return DASH;
  return (n >= 0 ? "+" : "-") + fmtMoney(Math.abs(n));
}

function pctText(val, digits) {
  var n = numOrNull(val);
  return n === null ? DASH : fmtNum(n, digits === undefined ? 1 : digits) + "%";
}

function creditValue(r) {
  if (!r.pricing) return null;
  var cash = numOrNull(r.pricing.mid_cash_total);
  if (cash === null) return null;
  return r.pricing.is_credit ? Math.abs(cash) : -Math.abs(cash);
}

function firstBreakeven(r) {
  if (!r.risk || !r.risk.breakevens || !r.risk.breakevens.length) return null;
  return numOrNull(r.risk.breakevens[0]);
}

function asOfParts(r) {
  return r.snapshot_ts_utc ? snapshotEtParts(r.snapshot_ts_utc) : null;
}

var COLUMNS = [
  {
    key: "ticker",
    label: "Ticker",
    type: "text",
    def: true,
    left: true,
    sort: function (r) {
      return r.ticker;
    },
    text: function (r) {
      return r.ticker;
    },
  },
  {
    key: "strategy",
    label: "Strategy",
    type: "text",
    def: true,
    left: true,
    sort: function (r) {
      return r.strategy_kind ? kindLabel(r.strategy_kind) : null;
    },
    text: function (r) {
      return kindLabel(r.strategy_kind);
    },
  },
  {
    key: "expiry",
    label: "Expiry (DTE)",
    type: "expiry",
    def: true,
    left: true,
    sort: function (r) {
      return numOrNull(r.short_dte);
    },
    text: function (r) {
      if (!r.short_expiry) return DASH;
      var t = r.short_expiry + " (" + r.short_dte + "d)";
      if (r.long_expiry && r.long_expiry !== r.short_expiry) {
        t += " / " + r.long_expiry + " (" + r.long_dte + "d)";
      }
      return t;
    },
  },
  {
    key: "legs",
    label: "Legs",
    type: "none",
    def: true,
    left: true,
    sort: function (r) {
      return r.legs_label || null;
    },
    text: function (r) {
      return r.legs_label || DASH;
    },
  },
  {
    key: "credit",
    label: "Credit/Debit",
    type: "num",
    def: true,
    sort: creditValue,
    text: function (r) {
      var v = creditValue(r);
      if (v === null) return DASH;
      return (v >= 0 ? "+" : "-") + fmtMoney(Math.abs(v)) + (v >= 0 ? " CR" : " DB");
    },
  },
  {
    key: "max_profit",
    label: "Max profit",
    type: "num",
    def: true,
    sort: function (r) {
      return r.risk ? numOrNull(r.risk.max_profit) : null;
    },
    text: function (r) {
      return r.risk ? fmtMoney(r.risk.max_profit) : DASH;
    },
  },
  {
    key: "max_loss",
    label: "Max loss",
    type: "num",
    def: true,
    sort: function (r) {
      return r.risk && !r.risk.max_loss_unbounded ? numOrNull(r.risk.max_loss) : null;
    },
    text: function (r) {
      if (!r.risk) return DASH;
      return r.risk.max_loss_unbounded ? "Unlimited" : fmtMoney(r.risk.max_loss);
    },
  },
  {
    key: "ror",
    label: "RoR %",
    type: "num",
    def: true,
    sort: function (r) {
      return r.risk ? numOrNull(r.risk.return_on_risk_pct) : null;
    },
    text: function (r) {
      return r.risk ? pctText(r.risk.return_on_risk_pct) : DASH;
    },
  },
  {
    key: "pop",
    label: "POP %",
    type: "num",
    def: true,
    sort: function (r) {
      return r.risk ? numOrNull(r.risk.prob_of_profit_pct) : null;
    },
    text: function (r) {
      return r.risk ? pctText(r.risk.prob_of_profit_pct) : DASH;
    },
  },
  {
    key: "evr",
    label: "EV/risk %",
    type: "num",
    def: true,
    sort: function (r) {
      return r.risk ? numOrNull(r.risk.expected_return_on_risk_pct) : null;
    },
    text: function (r) {
      return r.risk ? pctText(r.risk.expected_return_on_risk_pct) : DASH;
    },
  },
  {
    key: "be",
    label: "Break-even(s)",
    type: "num",
    def: true,
    sort: firstBreakeven,
    text: function (r) {
      if (!r.risk || !r.risk.breakevens || !r.risk.breakevens.length) return DASH;
      return r.risk.breakevens
        .map(function (b) {
          return fmtNum(b, 2);
        })
        .join(" / ");
    },
  },
  {
    key: "spot",
    label: "Spot",
    type: "num",
    def: true,
    sort: function (r) {
      return numOrNull(r.underlying_price);
    },
    text: function (r) {
      return numOrNull(r.underlying_price) === null ? DASH : fmtMoney(r.underlying_price);
    },
  },
  {
    key: "asof",
    label: "Data as of (ET)",
    type: "none",
    def: true,
    sort: function (r) {
      var t = r.snapshot_ts_utc ? Date.parse(r.snapshot_ts_utc) : NaN;
      return Number.isNaN(t) ? null : t;
    },
    text: function (r) {
      var p = asOfParts(r);
      if (!p) return DASH;
      return r.is_today_et ? p.time : p.dayShort + " " + p.time;
    },
  },
  {
    key: "dist_be",
    label: "Dist. to BE %",
    type: "num",
    def: false,
    sort: function (r) {
      return r.risk ? numOrNull(r.risk.nearest_breakeven_distance_pct) : null;
    },
    text: function (r) {
      return r.risk ? pctText(r.risk.nearest_breakeven_distance_pct) : DASH;
    },
  },
  {
    key: "greeks",
    label: "Net Greeks (Δ / Θ / Vega)",
    type: "num",
    def: false,
    sort: function (r) {
      return r.net_greeks ? numOrNull(r.net_greeks.delta_shares) : null;
    },
    text: function (r) {
      var g = r.net_greeks;
      if (!g) return DASH;
      return (
        "Δ " +
        fmtNum(g.delta_shares, 1) +
        " · Θ " +
        signedMoney(g.theta_daily_dollars) +
        "/d · V " +
        signedMoney(g.vega_per_1pct_dollars)
      );
    },
  },
  {
    key: "liquidity",
    label: "Liquidity (slip $ / min OI)",
    type: "num",
    def: false,
    sort: function (r) {
      return r.pricing ? numOrNull(r.pricing.slippage_dollars) : null;
    },
    text: function (r) {
      if (!r.pricing) return DASH;
      var oi = numOrNull(r.min_leg_open_interest);
      return fmtMoney(r.pricing.slippage_dollars) + " · OI " + (oi === null ? DASH : fmtInt(oi));
    },
  },
  {
    key: "em",
    label: "Exp. move 1σ",
    type: "num",
    def: false,
    sort: function (r) {
      return r.risk ? numOrNull(r.risk.expected_move_1sigma_pct) : null;
    },
    text: function (r) {
      var k = r.risk;
      if (!k || numOrNull(k.expected_move_1sigma_pct) === null) return DASH;
      var t = "±" + fmtNum(k.expected_move_1sigma_pct, 1) + "%";
      if (numOrNull(k.expected_move_1sigma_low) !== null && numOrNull(k.expected_move_1sigma_high) !== null) {
        t += " ($" + fmtNum(k.expected_move_1sigma_low, 2) + "–$" + fmtNum(k.expected_move_1sigma_high, 2) + ")";
      }
      return t;
    },
  },
];

var COLUMN_BY_KEY = {};
COLUMNS.forEach(function (c) {
  COLUMN_BY_KEY[c.key] = c;
});

// ------------------------------------------------------------------ Preferences
function defaultPrefs() {
  var cols = {};
  COLUMNS.forEach(function (c) {
    cols[c.key] = c.def;
  });
  return {
    mode: "best",
    strategy: "BULL_PUT_SPREAD",
    dte: "120",
    minCredit: "",
    minRor: "",
    minPop: "",
    live: false,
    showStatus: true,
    columns: cols,
    sort: { key: "ror", dir: "desc" },
    filters: {},
  };
}

function readPrefs() {
  var prefs = defaultPrefs();
  try {
    var raw = window.localStorage.getItem(SCREENER_PREFS_KEY);
    if (!raw) return prefs;
    var p = JSON.parse(raw);
    if (!p || typeof p !== "object") return prefs;
    if (p.mode === "best" || p.mode === "selected" || p.mode === "all") prefs.mode = p.mode;
    if (typeof p.strategy === "string" && KIND_LABELS[p.strategy]) prefs.strategy = p.strategy;
    if (typeof p.dte === "string" && /^(\d+|\d+:\d+)$/.test(p.dte)) prefs.dte = p.dte;
    ["minCredit", "minRor", "minPop"].forEach(function (k) {
      if (typeof p[k] === "string" && (p[k] === "" || !Number.isNaN(Number(p[k])))) prefs[k] = p[k];
    });
    if (typeof p.live === "boolean") prefs.live = p.live;
    if (typeof p.showStatus === "boolean") prefs.showStatus = p.showStatus;
    if (p.columns && typeof p.columns === "object") {
      COLUMNS.forEach(function (c) {
        if (typeof p.columns[c.key] === "boolean") prefs.columns[c.key] = p.columns[c.key];
      });
    }
    if (
      p.sort &&
      COLUMN_BY_KEY[p.sort.key] &&
      (p.sort.dir === "asc" || p.sort.dir === "desc")
    ) {
      prefs.sort = { key: p.sort.key, dir: p.sort.dir };
    }
    if (p.filters && typeof p.filters === "object") {
      COLUMNS.forEach(function (c) {
        var f = p.filters[c.key];
        if (!f || typeof f !== "object") return;
        var clean = {};
        ["q", "min", "max", "v"].forEach(function (k) {
          if (typeof f[k] === "string" && f[k] !== "") clean[k] = f[k];
        });
        if (Object.keys(clean).length) prefs.filters[c.key] = clean;
      });
    }
  } catch (_err) {
    return defaultPrefs();
  }
  return prefs;
}

function writePrefs() {
  try {
    window.localStorage.setItem(SCREENER_PREFS_KEY, JSON.stringify(S.prefs));
  } catch (_err) {
    /* ignore */
  }
}

function visibleColumns() {
  return COLUMNS.filter(function (c) {
    return S.prefs.columns[c.key];
  });
}

// ------------------------------------------------------------------ DOM helpers
function h(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function cardVisible() {
  var vis = state.panelVisibility;
  return vis && typeof vis[PANEL_ID] === "boolean" ? vis[PANEL_ID] : true;
}

function liveWanted() {
  return Boolean(S.prefs && S.prefs.live && cardVisible());
}

function lockBusy() {
  return Boolean(
    state.loading || state.watchlistBatchBusy || state.ledgerBusy || state.corridorBusy
  );
}

function warn(msg, extra) {
  showBanner("warn", msg, Object.assign({}, BANNER_META, extra || {}));
}

// ------------------------------------------------------------------ Effective rows (live overlay)
function effective(row) {
  var ov = S.overlay[row.row_id];
  if (!ov || row.status !== "ok" || ov.live_status === "not_live_budget") return row;
  var merged = Object.assign({}, row);
  OVERLAY_FIELDS.forEach(function (k) {
    var v = ov[k];
    if (v === null || v === undefined) return;
    if (Array.isArray(v) && !v.length) return;
    merged[k] = v;
  });
  return merged;
}

function liveTags(row) {
  var ov = S.overlay[row.row_id];
  if (!ov || row.status !== "ok") return [];
  if (ov.live_status === "not_live_budget") return [{ cls: "is-budget", text: "not live (budget)" }];
  var tags = [];
  if (ov.live_status === "live") tags.push({ cls: "is-live", text: "live" });
  else tags.push({ cls: "is-noquotes", text: "no quotes" });
  if (ov.adjusted) tags.push({ cls: "is-adjusted", text: "adjusted" });
  return tags;
}

// ------------------------------------------------------------------ Filter / sort
function filterNum(str) {
  if (str === undefined || str === null || str === "") return null;
  var n = Number(str);
  return Number.isNaN(n) ? null : n;
}

function passesFilters(eff) {
  var cols = visibleColumns();
  for (var i = 0; i < cols.length; i++) {
    var c = cols[i];
    var f = S.prefs.filters[c.key];
    if (!f) continue;
    if (c.type === "text") {
      if (f.q && String(c.text(eff)).toLowerCase().indexOf(f.q.toLowerCase()) === -1) return false;
    } else if (c.type === "expiry") {
      if (f.v && eff.short_expiry !== f.v) return false;
    } else if (c.type === "num") {
      var min = filterNum(f.min);
      var max = filterNum(f.max);
      if (min === null && max === null) continue;
      var v = c.sort(eff);
      if (v === null) return false;
      if (min !== null && v < min) return false;
      if (max !== null && v > max) return false;
    }
  }
  return true;
}

function compareRows(a, b) {
  var col = COLUMN_BY_KEY[S.prefs.sort.key] || COLUMN_BY_KEY.ror;
  var dir = S.prefs.sort.dir === "asc" ? 1 : -1;
  var va = col.sort(a);
  var vb = col.sort(b);
  var aNull = va === null || va === undefined;
  var bNull = vb === null || vb === undefined;
  if (aNull && bNull) return a.row_id < b.row_id ? -1 : 1;
  if (aNull) return 1;
  if (bNull) return -1;
  var cmp;
  if (typeof va === "string" || typeof vb === "string") {
    cmp = String(va).localeCompare(String(vb));
  } else {
    cmp = va - vb;
  }
  if (cmp === 0) return a.row_id < b.row_id ? -1 : 1;
  return cmp * dir;
}

function computeOrder() {
  var rows = [];
  S.rows.forEach(function (base) {
    if (base.status !== "ok" && !S.prefs.showStatus) return;
    var eff = effective(base);
    if (!passesFilters(eff)) return;
    rows.push(eff);
  });
  rows.sort(compareRows);
  S.order = rows.map(function (r) {
    return r.row_id;
  });
}

function liveRowIds() {
  var ids = [];
  for (var i = 0; i < S.order.length && ids.length < LIVE_MAX_ROWS; i++) {
    var base = S.rowById[S.order[i]];
    if (base && base.status === "ok") ids.push(base.row_id);
  }
  return ids;
}

// ------------------------------------------------------------------ Rendering: header & filters
function sortIndicator(key) {
  if (S.prefs.sort.key !== key) return "⇅";
  return S.prefs.sort.dir === "asc" ? "▲" : "▼";
}

function renderHead() {
  var cols = visibleColumns();
  S.colsKey = cols
    .map(function (c) {
      return c.key;
    })
    .join(",");
  S.rowCache = {};
  el.headRow.textContent = "";
  el.filterRow.textContent = "";
  cols.forEach(function (c) {
    var th = h("th", c.left ? "text-left" : "");
    th.dataset.col = c.key;
    th.setAttribute(
      "aria-sort",
      S.prefs.sort.key === c.key
        ? S.prefs.sort.dir === "asc"
          ? "ascending"
          : "descending"
        : "none"
    );
    var btn = h("button", "th-sort-btn");
    btn.type = "button";
    btn.dataset.sortCol = c.key;
    btn.appendChild(h("span", "", c.label));
    btn.appendChild(h("span", "th-sort-ind", sortIndicator(c.key)));
    th.appendChild(btn);
    el.headRow.appendChild(th);

    var fth = h("th", c.left ? "text-left" : "");
    var f = S.prefs.filters[c.key] || {};
    if (c.type === "text") {
      var inp = h("input", "th-filter-input");
      inp.type = "text";
      inp.placeholder = "filter";
      inp.value = f.q || "";
      inp.dataset.filterCol = c.key;
      inp.dataset.filterKind = "q";
      inp.setAttribute("aria-label", "Filter " + c.label);
      inp.classList.toggle("is-filtered", Boolean(f.q));
      fth.appendChild(inp);
    } else if (c.type === "num") {
      var wrap = h("div", "screener-range");
      ["min", "max"].forEach(function (k) {
        var n = h("input", "th-filter-input");
        n.type = "number";
        n.step = "any";
        n.placeholder = k;
        n.value = f[k] || "";
        n.dataset.filterCol = c.key;
        n.dataset.filterKind = k;
        n.setAttribute("aria-label", k + " " + c.label);
        n.classList.toggle("is-filtered", Boolean(f[k]));
        wrap.appendChild(n);
      });
      fth.appendChild(wrap);
    } else if (c.type === "expiry") {
      var sel = h("select", "th-filter-select");
      sel.dataset.filterCol = c.key;
      sel.dataset.filterKind = "v";
      sel.setAttribute("aria-label", "Filter " + c.label);
      fth.appendChild(sel);
    }
    el.filterRow.appendChild(fth);
  });
  populateExpiryFilter();
}

function populateExpiryFilter() {
  var sel = el.filterRow.querySelector('select[data-filter-col="expiry"]');
  if (!sel) return;
  var seen = {};
  S.rows.forEach(function (r) {
    if (r.short_expiry && !seen[r.short_expiry]) seen[r.short_expiry] = r.short_dte;
  });
  var current = (S.prefs.filters.expiry && S.prefs.filters.expiry.v) || "";
  sel.textContent = "";
  var all = h("option", "", "All");
  all.value = "";
  sel.appendChild(all);
  Object.keys(seen)
    .sort()
    .forEach(function (exp) {
      var o = h("option", "", exp + " (" + seen[exp] + "d)");
      o.value = exp;
      sel.appendChild(o);
    });
  if (current && !seen[current]) {
    var keep = h("option", "", current);
    keep.value = current;
    sel.appendChild(keep);
  }
  sel.value = current;
  sel.classList.toggle("is-filtered", Boolean(current));
}

function updateSortUI() {
  var ths = el.headRow.querySelectorAll("th[data-col]");
  for (var i = 0; i < ths.length; i++) {
    var key = ths[i].dataset.col;
    ths[i].setAttribute(
      "aria-sort",
      S.prefs.sort.key === key
        ? S.prefs.sort.dir === "asc"
          ? "ascending"
          : "descending"
        : "none"
    );
    var ind = ths[i].querySelector(".th-sort-ind");
    if (ind) ind.textContent = sortIndicator(key);
  }
}

// ------------------------------------------------------------------ Rendering: body
function fillCell(td, col, eff, base) {
  var text = col.text(eff);
  if (col.key === "ticker") {
    td.appendChild(h("span", "screener-ticker", text));
    liveTags(base).forEach(function (t) {
      var tag = h("span", "screener-tag " + t.cls, t.text);
      var ov = S.overlay[base.row_id];
      if (t.text === "live" && ov && ov.keep_reason) tag.title = "Kept: " + ov.keep_reason;
      td.appendChild(tag);
    });
    return;
  }
  if (col.key === "asof") {
    if (eff.is_today_et) {
      var dot = h("span", "screener-fresh", "●");
      dot.title = "Snapshot from today (ET)";
      td.appendChild(dot);
    }
    td.appendChild(document.createTextNode(text));
    if (eff.snapshot_id) td.title = eff.snapshot_id;
    return;
  }
  td.textContent = text;
  if (col.key === "credit") {
    var v = creditValue(eff);
    if (v !== null) td.classList.add(v >= 0 ? "is-credit" : "is-debit");
  } else if (col.key === "ror" || col.key === "evr") {
    var n = col.sort(eff);
    if (n !== null) td.classList.add(n >= 0 ? "is-pos" : "is-neg");
  } else if (col.key === "max_loss" && eff.risk && eff.risk.max_loss_unbounded) {
    td.classList.add("is-neg");
  } else if (col.key === "legs") {
    td.classList.add("screener-legs");
  }
}

function buildStatusRow(base, ncols) {
  var tr = h("tr", "screener-row screener-row--status is-" + base.status.replace(/_/g, "-"));
  tr.dataset.rowId = base.row_id;
  var td = h("td", "text-left screener-status-cell");
  td.colSpan = ncols;
  td.appendChild(h("span", "screener-ticker", base.ticker));
  td.appendChild(h("span", "screener-status-tag", STATUS_LABELS[base.status] || base.status));
  var reason = base.status_reason || "";
  if (base.strategy_kind) reason = kindLabel(base.strategy_kind) + (reason ? ": " + reason : "");
  if (reason) td.appendChild(h("span", "screener-status-reason", reason));
  var p = asOfParts(base);
  if (p && base.status !== "no_data") {
    td.appendChild(h("span", "screener-status-asof", "data " + p.dayShort + " " + p.time));
  }
  tr.appendChild(td);
  return tr;
}

function legCell(tr, text, cls) {
  tr.appendChild(h("td", cls || "", text));
}

function buildLegsTable(legs) {
  var table = h("table", "screener-legs-table");
  var head = h("tr");
  ["Side", "Right", "Strike", "Expiry (DTE)", "Bid", "Ask", "Mid", "IV", "Δ", "OI"].forEach(function (t) {
    head.appendChild(h("th", "", t));
  });
  var thead = h("thead");
  thead.appendChild(head);
  table.appendChild(thead);
  var tbody = h("tbody");
  legs.forEach(function (l) {
    var tr = h("tr");
    legCell(tr, l.side + (l.qty > 1 ? " x" + l.qty : ""), l.side === "SELL" ? "is-neg" : "is-pos");
    legCell(tr, l.right === "C" ? "Call" : "Put");
    legCell(tr, fmtNum(l.strike, 2));
    legCell(tr, l.expiry + " (" + l.dte + "d)");
    legCell(tr, numOrNull(l.bid) === null ? DASH : fmtNum(l.bid, 2));
    legCell(tr, numOrNull(l.ask) === null ? DASH : fmtNum(l.ask, 2));
    legCell(tr, fmtNum(l.mid, 2));
    legCell(tr, numOrNull(l.iv) === null ? DASH : fmtNum(Number(l.iv) * 100, 1) + "%");
    legCell(tr, numOrNull(l.delta) === null ? DASH : fmtNum(l.delta, 2));
    legCell(tr, numOrNull(l.open_interest) === null ? DASH : fmtInt(l.open_interest));
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  return table;
}

function metricsList(eff) {
  var pr = eff.pricing;
  var rk = eff.risk;
  var gr = eff.net_greeks;
  var items = [];
  function add(label, value) {
    items.push([label, value]);
  }
  if (pr) {
    add(
      "Net " + (pr.is_credit ? "credit" : "debit") + " (mid)",
      fmtMoney(Math.abs(pr.mid_cash_total)) + " (" + fmtMoney(Math.abs(pr.net_mid_per_share)) + "/sh)"
    );
    add(
      "Natural fill",
      fmtMoney(Math.abs(pr.natural_cash_total)) + " (" + fmtMoney(Math.abs(pr.net_natural_per_share)) + "/sh)"
    );
    add(
      "Slippage",
      fmtMoney(pr.slippage_dollars) +
        (numOrNull(pr.slippage_pct) === null ? "" : " (" + fmtNum(pr.slippage_pct, 1) + "%)")
    );
  }
  if (rk) {
    add("Max profit", fmtMoney(rk.max_profit));
    add("Max loss", rk.max_loss_unbounded ? "Unlimited" : fmtMoney(rk.max_loss));
    add("RoR", pctText(rk.return_on_risk_pct));
    add("POP", pctText(rk.prob_of_profit_pct));
    add("EV/risk", pctText(rk.expected_return_on_risk_pct));
    add(
      "Break-even(s)",
      rk.breakevens && rk.breakevens.length
        ? rk.breakevens
            .map(function (b) {
              return fmtNum(b, 2);
            })
            .join(" / ")
        : DASH
    );
    add("Dist. to nearest BE", pctText(rk.nearest_breakeven_distance_pct));
    add(
      "Expected move 1σ",
      numOrNull(rk.expected_move_1sigma_pct) === null
        ? DASH
        : "±" +
            fmtNum(rk.expected_move_1sigma_pct, 1) +
            "% ($" +
            fmtNum(rk.expected_move_1sigma_low, 2) +
            "–$" +
            fmtNum(rk.expected_move_1sigma_high, 2) +
            ")"
    );
  }
  if (gr) {
    add("Net Δ / Γ (shares)", fmtNum(gr.delta_shares, 1) + " / " + fmtNum(gr.gamma_shares, 2));
    add("Net Θ / day", signedMoney(gr.theta_daily_dollars));
    add("Net Vega / 1% IV", signedMoney(gr.vega_per_1pct_dollars));
  }
  add("Min leg OI", numOrNull(eff.min_leg_open_interest) === null ? DASH : fmtInt(eff.min_leg_open_interest));
  add("Spot", numOrNull(eff.underlying_price) === null ? DASH : fmtMoney(eff.underlying_price));
  if (numOrNull(eff.primary_call_wall) !== null || numOrNull(eff.primary_put_wall) !== null) {
    add(
      "Put / Call wall",
      (numOrNull(eff.primary_put_wall) === null ? DASH : fmtNum(eff.primary_put_wall, 2)) +
        " / " +
        (numOrNull(eff.primary_call_wall) === null ? DASH : fmtNum(eff.primary_call_wall, 2))
    );
  }
  return items;
}

function buildDetailRow(eff, base, ncols) {
  var tr = h("tr", "screener-detail-row");
  tr.dataset.detailFor = base.row_id;
  var td = h("td", "text-left");
  td.colSpan = ncols;
  var wrap = h("div", "screener-detail");

  var head = h("div", "screener-detail-head");
  var asOf = asOfParts(eff);
  head.appendChild(
    h(
      "span",
      "screener-detail-title",
      eff.ticker +
        " · " +
        kindLabel(eff.strategy_kind) +
        " · snapshot " +
        (eff.snapshot_id || DASH) +
        (asOf ? " (" + asOf.dayShort + " " + asOf.time + ")" : "")
    )
  );
  var btn = h("button", "btn btn-xs", "Open in Strategy Lab");
  btn.type = "button";
  btn.dataset.action = "open-lab";
  btn.dataset.rowId = base.row_id;
  btn.title = "Switch the dashboard to this ticker and snapshot and load these exact legs (no IBKR call)";
  head.appendChild(btn);
  wrap.appendChild(head);

  var legsWrap = h("div", "screener-legs-wrap");
  legsWrap.appendChild(buildLegsTable(eff.legs || []));
  wrap.appendChild(legsWrap);

  var dl = h("dl", "screener-metrics");
  metricsList(eff).forEach(function (pair) {
    var item = h("div", "screener-metric");
    item.appendChild(h("dt", "", pair[0]));
    item.appendChild(h("dd", "", pair[1]));
    dl.appendChild(item);
  });
  wrap.appendChild(dl);

  if (eff.rationale && eff.rationale.length) {
    var ul = h("ul", "screener-rationale");
    eff.rationale.forEach(function (line) {
      ul.appendChild(h("li", "", line));
    });
    wrap.appendChild(ul);
  }
  td.appendChild(wrap);
  tr.appendChild(td);
  return tr;
}

function buildRowNodes(base) {
  var cols = visibleColumns();
  var eff = effective(base);
  if (base.status !== "ok") {
    var statusTr = buildStatusRow(base, cols.length);
    return { sig: JSON.stringify([S.colsKey, base.status, base.status_reason, base.strategy_kind]), nodes: [statusTr] };
  }
  var expanded = Boolean(S.expanded[base.row_id]);
  var tags = liveTags(base);
  var ov = S.overlay[base.row_id];
  var cells = cols.map(function (c) {
    return c.text(eff);
  });
  var sigParts = [
    S.colsKey,
    cells,
    tags.map(function (t) {
      return t.text;
    }),
    ov ? ov.keep_reason || "" : "",
    ov ? ov.passes_filters : null,
    eff.is_today_et,
    expanded,
  ];
  if (expanded) sigParts.push(JSON.stringify([eff.legs, eff.pricing, eff.risk, eff.net_greeks, eff.rationale]));
  var tr = h("tr", "screener-row");
  tr.dataset.rowId = base.row_id;
  tr.tabIndex = 0;
  tr.setAttribute("aria-expanded", expanded ? "true" : "false");
  if (expanded) tr.classList.add("is-expanded");
  if (ov && ov.passes_filters === false) {
    tr.classList.add("is-below-floor");
    tr.title = "Live quotes put this row below the screener floors";
  }
  if (ov && ov.live_status === "not_live_budget") tr.classList.add("is-not-live");
  cols.forEach(function (c) {
    var td = h("td", c.left ? "text-left" : "");
    if (c.type === "num" || c.key === "asof") td.classList.add("num");
    fillCell(td, c, eff, base);
    tr.appendChild(td);
  });
  var nodes = [tr];
  if (expanded) nodes.push(buildDetailRow(eff, base, cols.length));
  return { sig: JSON.stringify(sigParts), nodes: nodes };
}

function renderBody() {
  var tbody = el.body;
  var wanted = [];
  var nextCache = {};
  S.order.forEach(function (id) {
    var base = S.rowById[id];
    if (!base) return;
    var cached = S.rowCache[id];
    var built = buildRowNodes(base);
    if (cached && cached.sig === built.sig) {
      nextCache[id] = cached;
      cached.nodes.forEach(function (n) {
        wanted.push(n);
      });
      return;
    }
    if (cached && cached.nodes[0].parentNode === tbody) {
      // Swap in place so scroll position and hover survive a live update.
      cached.nodes[0].replaceWith(built.nodes[0]);
      for (var i = 1; i < cached.nodes.length; i++) {
        if (cached.nodes[i].parentNode === tbody) tbody.removeChild(cached.nodes[i]);
      }
      if (built.nodes.length > 1) built.nodes[0].after(built.nodes[1]);
    }
    nextCache[id] = built;
    built.nodes.forEach(function (n) {
      wanted.push(n);
    });
  });
  S.rowCache = nextCache;

  var current = tbody.children;
  var same = current.length === wanted.length;
  for (var j = 0; same && j < wanted.length; j++) {
    if (current[j] !== wanted[j]) same = false;
  }
  if (!same) tbody.replaceChildren.apply(tbody, wanted);

  var emptyMsg = "";
  if (!S.result) {
    emptyMsg = S.running ? "Calculating…" : "No calculation yet.";
  } else if (!S.rows.length) {
    emptyMsg = "No rows. Add tickers to the watchlist and load their snapshots.";
  } else if (!S.order.length) {
    emptyMsg = "No rows match the current filters.";
  }
  el.empty.hidden = !emptyMsg;
  el.empty.textContent = emptyMsg;
}

function renderStatusLine() {
  if (!S.result) {
    el.status.textContent = S.running ? "Calculating…" : "Waiting for the first calculation…";
  } else {
    var r = S.result;
    var gen = snapshotEtParts(r.generated_at_utc);
    var c = r.counts || {};
    el.status.textContent =
      "Calculated " +
      (gen ? gen.time : DASH) +
      " · " +
      r.tickers.length +
      " ticker(s) · " +
      (c.ok || 0) +
      " ok · " +
      (c.filtered || 0) +
      " filtered · " +
      (c.not_buildable || 0) +
      " not buildable · " +
      (c.no_data || 0) +
      " no data · cached snapshots only";
  }
  var notes = S.runNotices.concat(S.liveNotices);
  el.notices.textContent = "";
  notes.forEach(function (n) {
    el.notices.appendChild(h("li", "", n));
  });
  el.notices.hidden = !notes.length;
}

function renderAll() {
  computeOrder();
  renderBody();
  renderStatusLine();
}

// ------------------------------------------------------------------ Controls <-> prefs
function applyPrefsToControls() {
  var p = S.prefs;
  el.mode.value = p.mode;
  el.strategy.value = p.strategy;
  el.dte.value = p.dte;
  if (el.dte.value !== p.dte) el.dte.value = "120";
  el.minCredit.value = p.minCredit;
  el.minRor.value = p.minRor;
  el.minPop.value = p.minPop;
  el.live.checked = p.live;
  el.showStatus.checked = p.showStatus;
  el.strategy.disabled = p.mode !== "selected";
}

function readControlsIntoPrefs() {
  S.prefs.mode = el.mode.value;
  S.prefs.strategy = el.strategy.value;
  S.prefs.dte = el.dte.value;
  S.prefs.minCredit = el.minCredit.value.trim();
  S.prefs.minRor = el.minRor.value.trim();
  S.prefs.minPop = el.minPop.value.trim();
  el.strategy.disabled = S.prefs.mode !== "selected";
  writePrefs();
}

function parseFloor(raw, label, maxVal) {
  if (raw === "") return { ok: true, value: null };
  var n = Number(raw);
  if (Number.isNaN(n) || n < 0 || (maxVal !== null && n > maxVal)) {
    warn(label + " must be a number between 0 and " + (maxVal === null ? "∞" : maxVal) + ".");
    return { ok: false, value: null };
  }
  return { ok: true, value: n > 0 ? n : null };
}

function buildRunBody() {
  var p = S.prefs;
  var win = parseDashboardDteWindow(p.dte);
  var body = { mode: p.mode, dte_min: win.min, dte_max: Math.max(1, win.max) };
  if (p.mode === "selected") body.strategy_kind = String(p.strategy).toUpperCase();
  var credit = parseFloor(p.minCredit, "Min credit $", null);
  var ror = parseFloor(p.minRor, "Min RoR %", 1000);
  var pop = parseFloor(p.minPop, "Min POP %", 100);
  if (!credit.ok || !ror.ok || !pop.ok) return null;
  if (credit.value !== null) body.min_net_credit_dollars = credit.value;
  if (ror.value !== null) body.min_ror_pct = ror.value;
  if (pop.value !== null) body.min_pop_pct = pop.value;
  return body;
}

function markDirty(on) {
  el.recalcBtn.classList.toggle("is-dirty", on);
  el.recalcBtn.title = on ? "Inputs changed: click to recalculate" : "";
}

// ------------------------------------------------------------------ Run
async function runScreener() {
  if (!S.initialized) return;
  readControlsIntoPrefs();
  var body = buildRunBody();
  if (!body) return;
  if (!cardVisible()) {
    S.stale = true;
    return;
  }
  S.runSeq += 1;
  var seq = S.runSeq;
  S.running = true;
  S.stale = false;
  el.recalcBtn.disabled = true;
  // New inputs -> the stream's row ids are stale; it is re-opened after the run (local close only,
  // the new stream supersedes the old one server-side).
  closeLive({ remote: false, clearOverlay: true });
  var res = null;
  var failure = "";
  try {
    res = await apiJson("/api/screener/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      timeoutMs: 60000,
      body: JSON.stringify(body),
    });
  } catch (err) {
    failure = "Best Option calculation failed: " + (err && err.message ? err.message : "network error");
  }
  if (seq !== S.runSeq) return;
  S.running = false;
  el.recalcBtn.disabled = false;
  if (failure || !res || !res.ok || !res.body) {
    var msg = failure || extractApiErrorMessage(res && res.body, (res && res.textSnippet) || "Best Option calculation failed.");
    showBanner("error", msg, Object.assign({}, BANNER_META, {
      status: res ? res.status : undefined,
      errorCode: res && res.body ? res.body.error_code : undefined,
    }));
    renderAll();
    return;
  }
  S.result = res.body;
  S.rows = res.body.rows || [];
  S.rowById = {};
  S.rows.forEach(function (r) {
    S.rowById[r.row_id] = r;
  });
  Object.keys(S.expanded).forEach(function (id) {
    if (!S.rowById[id]) delete S.expanded[id];
  });
  S.lastRunBody = body;
  S.runNotices = res.body.notices || [];
  S.liveNotices = [];
  S.overlay = {};
  markDirty(false);
  populateExpiryFilter();
  renderAll();
  if (liveWanted()) openLive();
}

// ------------------------------------------------------------------ Live stream
function updateLiveBadge() {
  var badge = el.liveBadge;
  var md = el.mdLabel;
  if (!S.prefs.live) {
    badge.hidden = true;
    md.hidden = true;
    return;
  }
  badge.hidden = false;
  badge.className = "badge badge--muted";
  var info = S.liveInfo;
  if (!cardVisible()) {
    badge.textContent = "LIVE PAUSED (HIDDEN)";
  } else if (S.es && info && info.linesBudget) {
    badge.className = "badge badge--live";
    badge.textContent =
      "LIVE " + info.linesUsed + "/" + info.linesBudget + " lines (options ≤ " + info.optionBudget + ")";
  } else if (S.es) {
    badge.className = "badge badge--amber";
    badge.textContent = "LIVE CONNECTING…";
  } else if (S.resumePending) {
    badge.textContent = "LIVE WAITING (LOCK BUSY)";
  } else {
    badge.textContent = "LIVE STOPPED";
  }
  if (S.es && info && info.mdLabel) {
    md.hidden = false;
    md.className = /DELAYED/i.test(info.mdLabel) ? "badge badge--delayed" : "badge badge--live";
    md.textContent = info.mdLabel;
  } else {
    md.hidden = true;
  }
}

function closeLive(opts) {
  var o = opts || {};
  if (S.reallocTimer) {
    window.clearTimeout(S.reallocTimer);
    S.reallocTimer = null;
  }
  var had = Boolean(S.es);
  if (S.es) {
    try {
      S.es.close();
    } catch (_err) {
      /* ignore */
    }
    S.es = null;
  }
  if (o.remote && (had || o.force)) {
    try {
      window.fetch("/api/screener/stop", { method: "POST", keepalive: true }).catch(function () {});
    } catch (_err) {
      /* ignore */
    }
  }
  S.streamId = "";
  S.opened = false;
  S.liveInfo = null;
  S.lastSentIds = [];
  if (o.clearOverlay && Object.keys(S.overlay).length) {
    S.overlay = {};
    if (S.initialized && S.result) renderAll();
  }
  if (S.initialized) updateLiveBadge();
}

function openLive() {
  closeLive({ remote: false, clearOverlay: false });
  if (!liveWanted() || !S.result || !S.lastRunBody) {
    updateLiveBadge();
    return;
  }
  if (S.running) return; // the run completion re-opens the stream
  if (lockBusy()) {
    S.resumePending = true;
    updateLiveBadge();
    return;
  }
  var ids = liveRowIds();
  if (!ids.length) {
    S.liveNotices = ["Live screener idle: no calculated rows to stream."];
    renderStatusLine();
    updateLiveBadge();
    return;
  }
  if (state.corridorStreaming || state.corridorStreamSource) {
    stopCorridorStream({ silent: true, skipRemoteStop: true });
    showBanner("info", "Wall Corridor stream stopped: Best Option live screener started.", BANNER_META);
  }
  var req = Object.assign({}, S.lastRunBody, { row_ids: ids });
  var url = "/api/screener/stream?q=" + encodeURIComponent(JSON.stringify(req));
  S.resumePending = false;
  S.lastSentIds = ids;
  S.liveNotices = [];
  var es = new window.EventSource(url);
  S.es = es;
  updateLiveBadge();

  es.addEventListener("open", function (ev) {
    // The native `open` event carries no data; the server's named `open` event does.
    if (S.es !== es || !ev || !ev.data) return;
    try {
      var d = JSON.parse(ev.data);
      S.streamId = String(d.stream_id || "");
      S.opened = true;
      S.liveInfo = {
        linesUsed: 0,
        linesBudget: Number(d.lines_budget) || 0,
        optionBudget: Number(d.option_lines_budget) || 0,
        mdLabel: "",
      };
      updateLiveBadge();
    } catch (_err) {
      /* ignore malformed open */
    }
  });

  es.addEventListener("frame", function (ev) {
    if (S.es !== es) {
      try {
        es.close();
      } catch (_err) {
        /* ignore */
      }
      return;
    }
    var d = null;
    try {
      d = JSON.parse(ev.data);
    } catch (_err) {
      return;
    }
    if (S.streamId && d.stream_id && d.stream_id !== S.streamId) return;
    applyFrame(d);
  });

  es.addEventListener("stopped", function (ev) {
    if (S.es !== es) {
      try {
        es.close();
      } catch (_err) {
        /* ignore */
      }
      return;
    }
    var d = null;
    try {
      d = JSON.parse(ev.data);
    } catch (_err) {
      d = null;
    }
    closeLive({ remote: false, clearOverlay: true });
    handleStopped(d && d.stop_reason ? String(d.stop_reason) : "");
  });

  // One handler for the server's named `error` event (has data) and the native connection error
  // (no data). Either way the EventSource is closed so the browser never auto-reconnects.
  es.addEventListener("error", function (ev) {
    var current = S.es === es;
    try {
      es.close();
    } catch (_err) {
      /* ignore */
    }
    if (!current) return;
    var wasOpen = S.opened;
    if (ev && ev.data) {
      var code = "";
      var msg = "IBKR Gateway error during the live screener stream.";
      try {
        var errObj = JSON.parse(ev.data);
        if (errObj && errObj.message) msg = String(errObj.message);
        if (errObj && errObj.error_code) code = String(errObj.error_code);
      } catch (_err) {
        /* ignore */
      }
      closeLive({ remote: false, clearOverlay: true });
      if (code === "REFRESH_BUSY") S.resumePending = true;
      updateLiveBadge();
      warn(msg, { errorCode: code || undefined });
      return;
    }
    closeLive({ remote: wasOpen, clearOverlay: true });
    updateLiveBadge();
    warn(
      wasOpen
        ? "Live screener stream interrupted; IBKR subscriptions cancelled."
        : "Live screener stream could not be opened (refresh or batch in progress, invalid inputs, or Gateway unreachable)."
    );
  });
}

function handleStopped(reason) {
  if (reason === "manual_stop") {
    updateLiveBadge();
    return;
  }
  if (AUTO_RESUME_REASONS[reason]) {
    S.resumePending = true;
    S.liveNotices = [
      "Live screener paused: " + AUTO_RESUME_REASONS[reason] + " in progress; it resumes automatically afterwards.",
    ];
    renderStatusLine();
    updateLiveBadge();
    tryResume();
    return;
  }
  S.resumePending = false;
  var note = STOP_NOTICES[reason] || "Live screener stopped" + (reason ? " (" + reason + ")." : ".");
  S.liveNotices = [note];
  renderStatusLine();
  updateLiveBadge();
  showBanner("info", note, BANNER_META);
}

function tryResume() {
  if (!S.resumePending) return;
  if (!liveWanted() || S.es || S.running) return;
  if (lockBusy()) return;
  if (S.resumeTimer) window.clearTimeout(S.resumeTimer);
  S.resumeTimer = window.setTimeout(function () {
    S.resumeTimer = null;
    if (S.resumePending && liveWanted() && !S.es && !S.running && !lockBusy()) openLive();
  }, RESUME_DELAY_MS);
}

function applyFrame(d) {
  var overlay = {};
  (d.rows || []).forEach(function (r) {
    overlay[r.row_id] = r;
  });
  S.overlay = overlay;
  S.liveInfo = {
    linesUsed: Number(d.lines_used) || 0,
    linesBudget: Number(d.lines_budget) || (S.liveInfo ? S.liveInfo.linesBudget : 0),
    optionBudget: Number(d.option_lines_budget) || (S.liveInfo ? S.liveInfo.optionBudget : 0),
    mdLabel: d.market_data_type_label ? String(d.market_data_type_label) : "",
  };
  S.liveNotices = d.notices && d.notices.length ? d.notices.slice() : [];
  // Frozen order: frames update cell contents only; sort/filter re-evaluate on user action.
  renderBody();
  renderStatusLine();
  updateLiveBadge();
}

function scheduleReallocate() {
  if (!S.es || !S.streamId) return;
  if (S.reallocTimer) window.clearTimeout(S.reallocTimer);
  S.reallocTimer = window.setTimeout(doReallocate, REALLOC_DEBOUNCE_MS);
}

async function doReallocate() {
  S.reallocTimer = null;
  if (!S.es || !S.streamId) return;
  var ids = liveRowIds();
  var same =
    ids.length === S.lastSentIds.length &&
    ids.every(function (id, i) {
      return id === S.lastSentIds[i];
    });
  if (same) return;
  var sid = S.streamId;
  S.lastSentIds = ids;
  var res = null;
  try {
    res = await apiJson("/api/screener/stream/reallocate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stream_id: sid, row_ids: ids }),
    });
  } catch (err) {
    warn("Live screener re-allocation failed: " + (err && err.message ? err.message : "network error"));
    return;
  }
  if (sid !== S.streamId) return;
  if (res.status === 404) {
    openLive();
    return;
  }
  if (!res.ok) {
    warn(extractApiErrorMessage(res.body, "Live screener re-allocation failed."), {
      status: res.status,
      errorCode: res.body && res.body.error_code,
    });
    return;
  }
  if (res.body && res.body.notices && res.body.notices.length) {
    S.liveNotices = res.body.notices.slice();
    renderStatusLine();
  }
}

// ------------------------------------------------------------------ Open in Strategy Lab
function waitForStrategyIdle(timeoutMs) {
  return new Promise(function (resolve) {
    var started = Date.now();
    (function poll() {
      if (!state.strategyBusy || Date.now() - started > timeoutMs) {
        resolve();
        return;
      }
      window.setTimeout(poll, 100);
    })();
  });
}

async function openInStrategyLab(rowId) {
  var base = S.rowById[rowId];
  if (!base || base.status !== "ok") return;
  var eff = effective(base);
  if (!eff.legs || !eff.legs.length) return;
  if (dom.dteMaxSelect && S.prefs.dte && dom.dteMaxSelect.value !== S.prefs.dte) {
    var opt = dom.dteMaxSelect.querySelector('option[value="' + S.prefs.dte + '"]');
    if (opt) dom.dteMaxSelect.value = S.prefs.dte;
  }
  var loadedSnap =
    state.chainData && state.chainData.meta ? state.chainData.meta.snapshot_id : "";
  if (state.ticker !== base.ticker || (base.snapshot_id && loadedSnap !== base.snapshot_id)) {
    await loadChain(base.ticker, base.snapshot_id || "");
  }
  if (!state.chainData) return;
  if (!state.panelVisibility.strategy) {
    var nextVis = Object.assign({}, state.panelVisibility, { strategy: true });
    applyPanelVisibility(nextVis, { persist: true });
  }
  await waitForStrategyIdle(8000);
  if (dom.strategyQtyInput) dom.strategyQtyInput.value = "1";
  if (dom.strategyDaysSlider) dom.strategyDaysSlider.value = "0";
  if (dom.strategyIvSlider) dom.strategyIvSlider.value = "0";
  state.strategyLegs = eff.legs.map(function (l) {
    return { side: l.side, right: l.right, expiry: l.expiry, strike: l.strike, qty: l.qty };
  });
  setActiveStrategyPresetUI(base.strategy_kind);
  renderStrategyLegsTable();
  await evaluateStrategyRequest({ silent: false });
  var card = document.querySelector('.chart-grid .panel-card[data-panel-id="strategy"]');
  if (card && card.scrollIntoView) card.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ------------------------------------------------------------------ Columns menu
function buildColumnsMenu() {
  el.columnsMenu.textContent = "";
  COLUMNS.forEach(function (c) {
    var label = h("label", "screener-columns-item");
    var cb = document.createElement("input");
    cb.type = "checkbox";
    cb.dataset.colKey = c.key;
    cb.checked = Boolean(S.prefs.columns[c.key]);
    label.appendChild(cb);
    label.appendChild(h("span", "", c.label));
    el.columnsMenu.appendChild(label);
  });
}

function setColumnsMenuOpen(open) {
  el.columnsMenu.hidden = !open;
  el.columnsBtn.setAttribute("aria-expanded", open ? "true" : "false");
}

// ------------------------------------------------------------------ Event wiring
function onViewChanged() {
  writePrefs();
  computeOrder();
  renderBody();
  scheduleReallocate();
}

function onFilterInput(target) {
  var key = target.dataset.filterCol;
  var kind = target.dataset.filterKind;
  if (!key || !kind) return;
  var f = Object.assign({}, S.prefs.filters[key] || {});
  var val = target.value;
  if (val === "") delete f[kind];
  else f[kind] = val;
  if (Object.keys(f).length) S.prefs.filters[key] = f;
  else delete S.prefs.filters[key];
  target.classList.toggle("is-filtered", val !== "");
  onViewChanged();
}

function bindEvents() {
  el.form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    runScreener();
  });
  [el.mode, el.strategy, el.dte, el.minCredit, el.minRor, el.minPop].forEach(function (ctl) {
    ctl.addEventListener("change", function () {
      readControlsIntoPrefs();
      markDirty(true);
    });
  });
  el.minCredit.addEventListener("input", function () {
    markDirty(true);
  });
  el.minRor.addEventListener("input", function () {
    markDirty(true);
  });
  el.minPop.addEventListener("input", function () {
    markDirty(true);
  });
  el.showStatus.addEventListener("change", function () {
    S.prefs.showStatus = el.showStatus.checked;
    onViewChanged();
  });
  el.live.addEventListener("change", function () {
    S.prefs.live = el.live.checked;
    writePrefs();
    if (S.prefs.live) {
      S.resumePending = false;
      if (cardVisible()) {
        if (S.result && !S.running) openLive();
        else if (!S.running) runScreener();
      }
    } else {
      S.resumePending = false;
      closeLive({ remote: true, clearOverlay: true });
    }
    updateLiveBadge();
  });
  el.resetFilters.addEventListener("click", function () {
    S.prefs.filters = {};
    renderHead();
    onViewChanged();
  });

  el.headRow.addEventListener("click", function (ev) {
    var btn = ev.target.closest ? ev.target.closest("[data-sort-col]") : null;
    if (!btn) return;
    var key = btn.dataset.sortCol;
    var col = COLUMN_BY_KEY[key];
    if (!col) return;
    if (S.prefs.sort.key === key) {
      S.prefs.sort.dir = S.prefs.sort.dir === "asc" ? "desc" : "asc";
    } else {
      S.prefs.sort = { key: key, dir: col.type === "text" || col.type === "expiry" ? "asc" : "desc" };
    }
    updateSortUI();
    onViewChanged();
  });

  function filterHandler(ev) {
    var t = ev.target;
    if (t && t.dataset && t.dataset.filterCol) onFilterInput(t);
  }
  el.filterRow.addEventListener("input", filterHandler);
  el.filterRow.addEventListener("change", filterHandler);

  el.body.addEventListener("click", function (ev) {
    var target = ev.target;
    var actionBtn = target.closest ? target.closest('[data-action="open-lab"]') : null;
    if (actionBtn) {
      openInStrategyLab(actionBtn.dataset.rowId);
      return;
    }
    if (target.closest && target.closest(".screener-detail-row")) return;
    var tr = target.closest ? target.closest("tr[data-row-id]") : null;
    if (!tr || tr.classList.contains("screener-row--status")) return;
    var id = tr.dataset.rowId;
    if (S.expanded[id]) delete S.expanded[id];
    else S.expanded[id] = true;
    renderBody();
  });
  el.body.addEventListener("keydown", function (ev) {
    if (ev.key !== "Enter" && ev.key !== " ") return;
    var tr = ev.target && ev.target.matches && ev.target.matches("tr.screener-row[data-row-id]") ? ev.target : null;
    if (!tr || tr.classList.contains("screener-row--status")) return;
    ev.preventDefault();
    var id = tr.dataset.rowId;
    if (S.expanded[id]) delete S.expanded[id];
    else S.expanded[id] = true;
    renderBody();
  });

  el.columnsBtn.addEventListener("click", function (ev) {
    ev.stopPropagation();
    setColumnsMenuOpen(el.columnsMenu.hidden);
  });
  el.columnsMenu.addEventListener("click", function (ev) {
    ev.stopPropagation();
  });
  el.columnsMenu.addEventListener("change", function (ev) {
    var cb = ev.target;
    if (!cb || !cb.dataset || !cb.dataset.colKey) return;
    S.prefs.columns[cb.dataset.colKey] = cb.checked;
    writePrefs();
    renderHead();
    computeOrder();
    renderBody();
    scheduleReallocate();
  });
  document.addEventListener("click", function () {
    if (!el.columnsMenu.hidden) setColumnsMenuOpen(false);
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && !el.columnsMenu.hidden) {
      setColumnsMenuOpen(false);
      el.columnsBtn.focus();
    }
  });

  // Recalculate from fresh cached CSVs after a single refresh or a batch load.
  document.addEventListener("ibkr:snapshot-saved", onDataChanged);
  document.addEventListener("ibkr:batch-finished", onDataChanged);
  // Auto-resume after any short refresh_lock holder triggered by the UI finished.
  document.addEventListener("ibkr:lock-released", tryResume);

  document.addEventListener("ibkr:panel-visibility", function (ev) {
    var d = ev && ev.detail;
    if (!d || d.panelId !== PANEL_ID) return;
    if (d.visible) {
      // Shown again: recalculate; the run re-opens live mode when Live is still ticked.
      runScreener();
    } else {
      // Hidden: free every IBKR line but keep the Live checkbox state.
      S.resumePending = false;
      closeLive({ remote: true, clearOverlay: true });
    }
    updateLiveBadge();
  });

  window.addEventListener("pagehide", function () {
    closeLive({ remote: true, clearOverlay: false });
  });
}

function onDataChanged() {
  if (!S.initialized) return;
  if (!cardVisible()) {
    S.stale = true;
    return;
  }
  runScreener();
}

// ------------------------------------------------------------------ Init
export function initScreener() {
  var card = document.getElementById("best-option-card");
  if (!card || S.initialized) return;
  el = {
    form: document.getElementById("screener-form"),
    mode: document.getElementById("screener-mode"),
    strategy: document.getElementById("screener-strategy"),
    dte: document.getElementById("screener-dte"),
    minCredit: document.getElementById("screener-min-credit"),
    minRor: document.getElementById("screener-min-ror"),
    minPop: document.getElementById("screener-min-pop"),
    recalcBtn: document.getElementById("screener-recalc-btn"),
    live: document.getElementById("screener-live"),
    showStatus: document.getElementById("screener-show-status"),
    resetFilters: document.getElementById("screener-reset-filters"),
    columnsBtn: document.getElementById("screener-columns-btn"),
    columnsMenu: document.getElementById("screener-columns-menu"),
    status: document.getElementById("screener-status"),
    notices: document.getElementById("screener-notices"),
    headRow: document.getElementById("screener-head-row"),
    filterRow: document.getElementById("screener-filter-row"),
    body: document.getElementById("screener-body"),
    empty: document.getElementById("screener-empty"),
    liveBadge: document.getElementById("screener-live-badge"),
    mdLabel: document.getElementById("screener-md-label"),
  };
  S.prefs = readPrefs();
  S.initialized = true;
  applyPrefsToControls();
  buildColumnsMenu();
  renderHead();
  bindEvents();
  renderAll();
  updateLiveBadge();
  if (cardVisible()) runScreener();
  else S.stale = true;
}
