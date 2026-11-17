/* IBKR Options Analyzer dashboard — Wall Strike Corridor: preview, one-shot quotes, SSE streaming and ladder chart. */

import {
  dom,
  state,
} from "./state.js";
import {
  configuredDefaultMdType,
  escapeHtml,
  extractApiErrorMessage,
  fmtInt,
  fmtMoney,
  fmtNum,
  formatCorridorAutoFollowLabel,
  formatExpiryIso,
  gatewayAddressLabel,
  resolveCorridorTargetExpiry,
  showBanner,
  snapDteValueToExpiries,
  sortedChainExpiries,
  syncAutoFollowOptionLabels,
  themeVars,
} from "./helpers.js";
import {
  baseLayout,
  makeVerticalMarker,
  renderCorridorEmptyPlaceholder,
} from "./charts.js";
import {
  isPanelVisible,
} from "./panels.js";
import {
  renderTable,
} from "./chain_table.js";
import {
  evaluateStrategyRequest,
  refreshStrategyLegsQuotesOnly,
} from "./strategy_lab.js";
import {
  apiJson,
  fetchStatus,
} from "./network.js";
import {
  schedulePlotlyResize,
} from "./grid_layout.js";

// ------------------------------------------------------------------ Wall Strike Corridor (+-10 Strikes & Live Streaming)
export function getDefaultWindowStrikes() {
  var v = state.status ? state.status.corridor_default_window_strikes : null;
  var n = v !== null && v !== undefined ? Number(v) : NaN;
  return Number.isFinite(n) && n >= 1 ? Math.round(n) : 10;
}

export function getDefaultMaxContracts() {
  var v = state.status ? state.status.corridor_default_max_contracts : null;
  var n = v !== null && v !== undefined ? Number(v) : NaN;
  return Number.isFinite(n) && n >= 1 ? Math.round(n) : 88;
}

export function getHardMaxContracts() {
  var v = state.status ? state.status.corridor_hard_max_contracts : null;
  var n = v !== null && v !== undefined ? Number(v) : NaN;
  return Number.isFinite(n) && n >= 1 ? Math.round(n) : 90;
}

export function syncCorridorDefaultsFromStatus() {
  var defWin = getDefaultWindowStrikes();
  if (!state.corridorWindowUserSet) {
    state.corridorWindow = defWin;
    if (dom.corridorWindowSelect) {
      for (var i = 0; i < dom.corridorWindowSelect.options.length; i++) {
        if (dom.corridorWindowSelect.options[i].value === String(defWin)) {
          dom.corridorWindowSelect.value = String(defWin);
          break;
        }
      }
    }
    if (
      state.chainData &&
      !state.corridorStreaming &&
      (!state.corridorFrame || state.corridorFrame.mode === "PREVIEW") &&
      (!state.corridorFrame || state.corridorFrame.window_strikes !== defWin)
    ) {
      applyCorridorFrame(buildLocalCorridorPreview(state.chainData), { mergeIntoChain: false });
      return;
    }
  }
  updateCorridorSubsBadge(state.corridorFrame);
}

export function corridorKey(expiry, strike, right) {
  return String(expiry) + "|" + Number(strike) + "|" + String(right).toUpperCase();
}

function corridorFixedKey(expiry, strike, right) {
  return String(expiry) + "|" + Number(strike).toFixed(4) + "|" + String(right).toUpperCase();
}

function normalizeCorridorLeg(leg) {
  if (!leg || !leg.expiry || leg.strike === null || leg.strike === undefined || !leg.right) {
    return null;
  }
  var expStr = String(leg.expiry).trim();
  var strikeNum = Number(leg.strike);
  var rightStr = String(leg.right).toUpperCase();
  if (!expStr || !Number.isFinite(strikeNum) || (rightStr !== "C" && rightStr !== "P")) {
    return null;
  }
  var rawSide = String(leg.side || "BUY").toUpperCase();
  return {
    side: rawSide === "SELL" ? "SELL" : "BUY",
    right: rightStr,
    expiry: expStr,
    strike: strikeNum,
    qty: Math.max(1, parseInt(leg.qty, 10) || 1),
  };
}

export function buildCorridorLegLookup(legs) {
  var list = Array.isArray(legs) ? legs : state.strategyLegs || [];
  var lookup = {};
  for (var i = 0; i < list.length; i++) {
    var norm = normalizeCorridorLeg(list[i]);
    if (!norm) continue;
    var key = corridorKey(norm.expiry, norm.strike, norm.right);
    if (!lookup[key]) lookup[key] = [];
    lookup[key].push({
      idx: i,
      side: norm.side,
      right: norm.right,
      expiry: norm.expiry,
      strike: norm.strike,
      qty: norm.qty,
    });
  }
  return lookup;
}

export function corridorLegSubscriptionSignature(data) {
  var targetExp = data ? resolveCorridorTargetExpiry(data) || "" : "";
  var defWin = getDefaultWindowStrikes();
  var win = dom.corridorWindowSelect
    ? Math.max(1, parseInt(dom.corridorWindowSelect.value, 10) || defWin)
    : Number(state.corridorWindow || defWin);
  var keySet = {};
  var legs = Array.isArray(state.strategyLegs) ? state.strategyLegs : [];
  for (var i = 0; i < legs.length; i++) {
    var norm = normalizeCorridorLeg(legs[i]);
    if (!norm) continue;
    var k = corridorKey(norm.expiry, norm.strike, norm.right);
    keySet[k] = true;
  }
  var sortedKeys = Object.keys(keySet).sort();
  return [targetExp, String(win), sortedKeys.join(",")].join("::");
}

function fmtStrike(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return "\u2014";
  }
  var n = Number(val);
  if (Math.abs(n - Math.round(n)) < 1e-4) {
    return String(Math.round(n));
  }
  if (Math.abs(n * 10 - Math.round(n * 10)) < 1e-4) {
    return n.toFixed(1);
  }
  return n.toFixed(2);
}

function corridorPriorityLegsList() {
  if (!state.strategyLegs || !state.strategyLegs.length) return [];
  var seen = {};
  var out = [];
  for (var i = 0; i < state.strategyLegs.length; i++) {
    var norm = normalizeCorridorLeg(state.strategyLegs[i]);
    if (!norm) continue;
    var k = corridorKey(norm.expiry, norm.strike, norm.right);
    if (seen[k]) continue;
    seen[k] = true;
    out.push({ expiry: norm.expiry, strike: norm.strike, right: norm.right });
  }
  return out;
}

function corridorPriorityLegsParam() {
  return corridorPriorityLegsList()
    .map(function (l) {
      return l.expiry + "|" + l.strike + "|" + l.right;
    })
    .join(",");
}

function corridorWallsForExpiry(data, targetExp) {
  if (!data || !data.walls) return { putWall: null, callWall: null, basis: "VOLUME" };
  var basis = data.walls.primary_basis || "VOLUME";
  var byDte = data.walls.by_dte || [];
  for (var i = 0; i < byDte.length; i++) {
    if (String(byDte[i].expiry) === String(targetExp)) {
      return {
        putWall: byDte[i].primary_put_wall ? Number(byDte[i].primary_put_wall.strike) : null,
        callWall: byDte[i].primary_call_wall ? Number(byDte[i].primary_call_wall.strike) : null,
        basis: basis,
      };
    }
  }
  return {
    putWall: data.walls.aggregate_primary_put_wall
      ? Number(data.walls.aggregate_primary_put_wall.strike)
      : null,
    callWall: data.walls.aggregate_primary_call_wall
      ? Number(data.walls.aggregate_primary_call_wall.strike)
      : null,
    basis: basis,
  };
}

export function buildLocalCorridorPreview(data) {
  if (!data || !data.contracts || !data.contracts.length) return null;
  var exps = sortedChainExpiries(data);
  if (!exps.length) return null;
  var defWin = getDefaultWindowStrikes();
  var win = dom.corridorWindowSelect
    ? Math.max(1, parseInt(dom.corridorWindowSelect.value, 10) || defWin)
    : defWin;
  state.corridorWindow = win;
  var maxCap = getDefaultMaxContracts();
  var hardMax = getHardMaxContracts();
  var chosenExp = resolveCorridorTargetExpiry(data) || exps[0].expiry;
  var chosenDte = exps[0].dte;
  var foundExp = false;
  for (var i = 0; i < exps.length; i++) {
    if (exps[i].expiry === chosenExp) {
      chosenDte = exps[i].dte;
      foundExp = true;
      break;
    }
  }
  if (!foundExp) {
    chosenExp = exps[0].expiry;
    chosenDte = exps[0].dte;
  }
  state.corridorExpiry = chosenExp;

  var spot = Number(data.meta.underlying_price || 0);
  var wInfo = corridorWallsForExpiry(data, chosenExp);
  var expRows = data.contracts.filter(function (c) {
    return String(c.expiry) === String(chosenExp);
  });
  if (!expRows.length) return null;

  var strikeMap = {};
  for (var r = 0; r < expRows.length; r++) {
    strikeMap[Number(expRows[r].strike)] = true;
  }
  var strikes = Object.keys(strikeMap)
    .map(function (s) {
      return Number(s);
    })
    .sort(function (a, b) {
      return a - b;
    });
  if (!strikes.length) return null;

  function nearestIdx(targetVal) {
    var bestI = 0;
    var bestD = Math.abs(strikes[0] - targetVal);
    for (var k = 1; k < strikes.length; k++) {
      var d = Math.abs(strikes[k] - targetVal);
      if (d < bestD) {
        bestI = k;
        bestD = d;
      }
    }
    return bestI;
  }

  var pwIdx = nearestIdx(wInfo.putWall !== null ? wInfo.putWall : spot);
  var cwIdx = nearestIdx(wInfo.callWall !== null ? wInfo.callWall : spot);
  var atmIdx = nearestIdx(spot);
  var idxLookup = {};
  for (var si = 0; si < strikes.length; si++) {
    idxLookup[strikes[si].toFixed(6)] = si;
  }

  var legKeys = {};
  var legs = corridorPriorityLegsList();
  for (var l = 0; l < legs.length; l++) {
    legKeys[corridorKey(legs[l].expiry, legs[l].strike, legs[l].right)] = true;
  }

  function inBand(idx, anchorIdx) {
    return idx >= anchorIdx - win && idx <= anchorIdx + win;
  }

  var candidates = [];
  var seen = {};
  for (var a = 0; a < data.contracts.length; a++) {
    var row = data.contracts[a];
    var rk = corridorKey(row.expiry, row.strike, row.right);
    var isExpMatch = String(row.expiry) === String(chosenExp);
    var sIdx = isExpMatch ? idxLookup[Number(row.strike).toFixed(6)] : undefined;
    var inPw = sIdx !== undefined && inBand(sIdx, pwIdx);
    var inCw = sIdx !== undefined && inBand(sIdx, cwIdx);
    var inAtm = sIdx !== undefined && inBand(sIdx, atmIdx);
    var isLeg = Boolean(legKeys[rk]);
    if (!isLeg && !(inPw || inCw || inAtm)) continue;
    if (seen[rk]) continue;
    seen[rk] = true;

    var tier = 3;
    var dist = sIdx !== undefined ? Math.abs(sIdx - atmIdx) : 0;
    if (isLeg) {
      tier = 0;
      dist = 0;
    } else if ((inPw && row.right === "P") || (inCw && row.right === "C")) {
      tier = 1;
      dist = inPw && row.right === "P" ? Math.abs(sIdx - pwIdx) : Math.abs(sIdx - cwIdx);
    } else if (inPw || inCw) {
      tier = 2;
      dist = Math.min(
        inPw ? Math.abs(sIdx - pwIdx) : 999,
        inCw ? Math.abs(sIdx - cwIdx) : 999
      );
    }

    var zone = "ATM";
    if (sIdx === undefined) zone = "STRATEGY_LEG";
    else if (inPw && inCw) zone = "BOTH_WALLS";
    else if (inPw) zone = "PUT_WALL";
    else if (inCw) zone = "CALL_WALL";
    else if (isLeg) zone = "STRATEGY_LEG";

    var bid = row.bid !== null && row.bid !== undefined ? Number(row.bid) : null;
    var ask = row.ask !== null && row.ask !== undefined ? Number(row.ask) : null;
    var mid =
      bid !== null && ask !== null && bid > 0 && ask >= bid
        ? (bid + ask) / 2.0
        : row.mid !== null && row.mid !== undefined
          ? Number(row.mid)
          : row.last !== null && row.last !== undefined
            ? Number(row.last)
            : row.close !== null && row.close !== undefined
              ? Number(row.close)
              : null;
    var spreadPct =
      bid !== null && ask !== null && mid && bid > 0 && ask >= bid
        ? ((ask - bid) / mid) * 100.0
        : null;

    candidates.push({
      sortRank: [tier, dist, Math.abs(Number(row.strike) - spot), Number(row.strike)],
      quote: {
        option_conid: row.option_conid || 0,
        expiry: String(row.expiry),
        dte: Number(row.dte),
        strike: Number(row.strike),
        right: String(row.right),
        zone: zone,
        is_put_wall:
          isExpMatch &&
          wInfo.putWall !== null &&
          Math.abs(Number(row.strike) - wInfo.putWall) < 1e-4 &&
          row.right === "P",
        is_call_wall:
          isExpMatch &&
          wInfo.callWall !== null &&
          Math.abs(Number(row.strike) - wInfo.callWall) < 1e-4 &&
          row.right === "C",
        is_strategy_leg: isLeg,
        bid: bid,
        ask: ask,
        mid: mid,
        last: row.last !== null && row.last !== undefined ? Number(row.last) : null,
        close: row.close !== null && row.close !== undefined ? Number(row.close) : null,
        spread_pct: spreadPct,
        volume: row.volume,
        open_interest: row.open_interest,
        iv: row.iv,
        delta: row.delta,
        gamma: row.gamma,
        theta: row.theta,
        vega: row.vega,
        greeks_source: row.greeks_source || "NONE",
        updated_ts_utc: data.meta.snapshot_ts_utc || "",
      },
    });
  }

  candidates.sort(function (a, b) {
    for (var tIdx = 0; tIdx < a.sortRank.length; tIdx++) {
      if (a.sortRank[tIdx] !== b.sortRank[tIdx]) return a.sortRank[tIdx] - b.sortRank[tIdx];
    }
    return 0;
  });
  var selected = candidates.slice(0, maxCap).map(function (x) {
    return x.quote;
  });
  selected.sort(function (a, b) {
    if (a.dte !== b.dte) return a.dte - b.dte;
    if (a.strike !== b.strike) return a.strike - b.strike;
    return a.right < b.right ? -1 : a.right > b.right ? 1 : 0;
  });

  return {
    ticker: data.meta.underlying,
    snapshot_id: data.meta.snapshot_id,
    target_expiry: chosenExp,
    target_dte: chosenDte,
    window_strikes: win,
    max_contracts: maxCap,
    subscribed_count: 0,
    subscription_cap: hardMax,
    underlying_price: spot,
    market_data_type: data.meta.market_data_type || configuredDefaultMdType(),
    primary_call_wall: wInfo.callWall,
    primary_put_wall: wInfo.putWall,
    wall_basis: wInfo.basis,
    updated_ts_utc: data.meta.snapshot_ts_utc || "",
    sequence: 0,
    mode: "PREVIEW",
    elapsed_ms: null,
    changed_keys: [],
    quotes: selected,
    notices: [],
  };
}

export function updateCorridorSubsBadge(frame) {
  if (!dom.corridorSubsBadge) return;
  var hardMax =
    frame && frame.subscription_cap ? Number(frame.subscription_cap) : getHardMaxContracts();
  var quotes = frame && Array.isArray(frame.quotes) ? frame.quotes : [];
  var optCount = quotes.length;
  var totalSubs =
    frame && frame.subscribed_count !== null && frame.subscribed_count !== undefined
      ? Number(frame.subscribed_count)
      : 0;
  var isActive = Boolean(
    frame && frame.mode && frame.mode !== "PREVIEW" && optCount > 0 && totalSubs > 0
  );
  if (!isActive) {
    dom.corridorSubsBadge.textContent = "0 / " + hardMax + " SUBS";
    dom.corridorSubsBadge.className = "badge badge--info";
    return;
  }
  var spotSubscribed =
    typeof frame.spot_subscribed === "boolean" ? frame.spot_subscribed : totalSubs > optCount;
  var detail = spotSubscribed ? optCount + "+1 spot" : optCount + " opt";
  dom.corridorSubsBadge.textContent = totalSubs + " / " + hardMax + " SUBS (" + detail + ")";
  dom.corridorSubsBadge.className =
    "badge " + (state.corridorStreaming ? "badge--live" : "badge--info");
}

function updateCorridorStatusUI(frame, modeLabel) {
  updateCorridorSubsBadge(frame);
  if (dom.corridorStatusBadge) {
    if (modeLabel) {
      dom.corridorStatusBadge.textContent = modeLabel;
    } else if (!frame) {
      dom.corridorStatusBadge.textContent = "IDLE \u2022 MANUAL";
    } else if (frame.mode === "STREAM" && state.corridorStreaming) {
      dom.corridorStatusBadge.textContent =
        "STREAMING LIVE \u2022 #" + frame.sequence + " \u2022 " + frame.target_dte + "d EXP";
    } else if (frame.mode === "STREAM") {
      dom.corridorStatusBadge.textContent =
        "STREAM STOPPED \u2022 " + frame.quotes.length + " OPT \u2022 " + frame.target_dte + "d";
    } else if (frame.mode === "ONE_SHOT") {
      dom.corridorStatusBadge.textContent =
        "ONE-SHOT UPDATED \u2022 " + frame.target_dte + "d EXP";
    } else {
      dom.corridorStatusBadge.textContent =
        "IDLE \u2022 PREVIEW (" + frame.quotes.length + " OPT \u2022 " + frame.target_dte + "d)";
    }
  }
}

function renderCorridorKpis(frame) {
  if (!frame || !frame.quotes || !frame.quotes.length) {
    if (dom.corridorKpiPw) dom.corridorKpiPw.textContent = "\u2014";
    if (dom.corridorKpiPwSub) dom.corridorKpiPwSub.textContent = "Put corridor avg mid / spread";
    if (dom.corridorKpiCw) dom.corridorKpiCw.textContent = "\u2014";
    if (dom.corridorKpiCwSub) dom.corridorKpiCwSub.textContent = "Call corridor avg mid / spread";
    if (dom.corridorKpiSpot) dom.corridorKpiSpot.textContent = "\u2014";
    if (dom.corridorKpiSpotSub) dom.corridorKpiSpotSub.textContent = "Cached baseline (manual)";
    if (dom.corridorKpiSync) dom.corridorKpiSync.textContent = "IDLE";
    if (dom.corridorKpiSyncSub) dom.corridorKpiSyncSub.textContent = "Click Refresh or toggle Streaming ON";
    return;
  }

  var kpiExp = String(frame.target_expiry || "") || findDominantExpiry(frame.quotes);
  var pwQ = null;
  var cwQ = null;
  var spreadSum = 0;
  var spreadCnt = 0;
  for (var i = 0; i < frame.quotes.length; i++) {
    var q = frame.quotes[i];
    var isTargetExp = !kpiExp || String(q.expiry) === kpiExp;
    if (isTargetExp && q.is_put_wall && !pwQ) pwQ = q;
    if (isTargetExp && q.is_call_wall && !cwQ) cwQ = q;
    if (isTargetExp && q.spread_pct !== null && q.spread_pct !== undefined) {
      spreadSum += Number(q.spread_pct);
      spreadCnt += 1;
    }
  }

  if (dom.corridorKpiPw) {
    dom.corridorKpiPw.textContent =
      frame.primary_put_wall !== null && frame.primary_put_wall !== undefined
        ? "$" + fmtNum(frame.primary_put_wall, 2) + (pwQ && pwQ.mid ? " \u2022 " + fmtMoney(pwQ.mid) : "")
        : "\u2014";
  }
  if (dom.corridorKpiPwSub) {
    dom.corridorKpiPwSub.textContent = pwQ
      ? "Bid/Ask " +
        (pwQ.bid !== null ? fmtNum(pwQ.bid, 2) : "\u2014") +
        "/" +
        (pwQ.ask !== null ? fmtNum(pwQ.ask, 2) : "\u2014") +
        (pwQ.delta !== null ? " \u2022 \u0394 " + fmtNum(pwQ.delta, 2) : "")
      : "Put Wall window \u00B1" + frame.window_strikes;
  }

  if (dom.corridorKpiCw) {
    dom.corridorKpiCw.textContent =
      frame.primary_call_wall !== null && frame.primary_call_wall !== undefined
        ? "$" + fmtNum(frame.primary_call_wall, 2) + (cwQ && cwQ.mid ? " \u2022 " + fmtMoney(cwQ.mid) : "")
        : "\u2014";
  }
  if (dom.corridorKpiCwSub) {
    dom.corridorKpiCwSub.textContent = cwQ
      ? "Bid/Ask " +
        (cwQ.bid !== null ? fmtNum(cwQ.bid, 2) : "\u2014") +
        "/" +
        (cwQ.ask !== null ? fmtNum(cwQ.ask, 2) : "\u2014") +
        (cwQ.delta !== null ? " \u2022 \u0394 " + fmtNum(cwQ.delta, 2) : "")
      : "Call Wall window \u00B1" + frame.window_strikes;
  }

  if (dom.corridorKpiSpot) {
    dom.corridorKpiSpot.textContent =
      "$" + fmtNum(frame.underlying_price, 2) + " \u2022 " + frame.target_dte + "d";
  }
  if (dom.corridorKpiSpotSub) {
    var avgSpreadTxt = spreadCnt ? "Avg Sprd " + fmtNum(spreadSum / spreadCnt, 1) + "%" : "Spread n/a";
    dom.corridorKpiSpotSub.textContent = "Type " + frame.market_data_type + " \u2022 " + avgSpreadTxt;
  }

  if (dom.corridorKpiSync) {
    dom.corridorKpiSync.textContent =
      frame.mode === "STREAM"
        ? "LIVE #" + frame.sequence
        : frame.mode === "ONE_SHOT"
          ? (frame.elapsed_ms ? Math.round(frame.elapsed_ms) + " ms" : "UPDATED")
          : "PREVIEW";
  }
  if (dom.corridorKpiSyncSub) {
    var tsShort = frame.updated_ts_utc ? frame.updated_ts_utc.replace("T", " ").replace("Z", " UTC") : "Cached snapshot";
    dom.corridorKpiSyncSub.textContent = tsShort;
  }
}

function findDominantExpiry(quotes) {
  if (!Array.isArray(quotes) || !quotes.length) return "";
  var stats = {};
  for (var i = 0; i < quotes.length; i++) {
    var q = quotes[i];
    var exp = String(q.expiry || "");
    if (!exp) continue;
    var dte = Number(q.dte);
    if (!stats[exp]) {
      stats[exp] = { expiry: exp, count: 0, minDte: Number.isFinite(dte) ? dte : Infinity };
    }
    stats[exp].count += 1;
    if (Number.isFinite(dte) && dte < stats[exp].minDte) {
      stats[exp].minDte = dte;
    }
  }
  var keys = Object.keys(stats);
  if (!keys.length) return "";
  keys.sort(function (a, b) {
    if (stats[b].count !== stats[a].count) return stats[b].count - stats[a].count;
    if (stats[a].minDte !== stats[b].minDte) return stats[a].minDte - stats[b].minDte;
    return a.localeCompare(b);
  });
  return keys[0];
}

function resolveCorridorSideFlash(q, fixedKey, frame, changedSet, nextPrevMids) {
  if (!q) return "";
  var curMid = q.mid !== null && q.mid !== undefined ? Number(q.mid) : null;
  if (curMid === null || Number.isNaN(curMid)) return "";
  nextPrevMids[fixedKey] = curMid;
  var prevMid = state.corridorPrevMids[fixedKey];
  if (prevMid === undefined || prevMid === null) return "";
  var mode = frame && frame.mode ? frame.mode : "";
  if (mode !== "STREAM" && mode !== "ONE_SHOT") return "";
  var rawKey = corridorKey(q.expiry, q.strike, q.right);
  var hasChangedList = Array.isArray(frame.changed_keys) && frame.changed_keys.length > 0;
  if (mode === "STREAM" && hasChangedList && !changedSet[rawKey] && !changedSet[fixedKey]) {
    return "";
  }
  if (curMid > prevMid + 1e-4) return " flash-up";
  if (curMid < prevMid - 1e-4) return " flash-down";
  return "";
}

function formatSideLegRoles(sideLegs, sideLabel, hasFallbackLeg) {
  var out = [];
  if (Array.isArray(sideLegs) && sideLegs.length) {
    for (var i = 0; i < sideLegs.length; i++) {
      var leg = sideLegs[i];
      var qty = Math.max(1, parseInt(leg.qty, 10) || 1);
      out.push("STRATEGY LEG: " + leg.side + " " + sideLabel + " x" + qty);
    }
  } else if (hasFallbackLeg) {
    out.push("STRATEGY LEG: " + sideLabel);
  }
  return out;
}

function corridorSideLegClasses(legs, hasFallbackLeg) {
  if ((!legs || !legs.length) && !hasFallbackLeg) return "";
  var hasBuy = false;
  var hasSell = false;
  if (legs && legs.length) {
    for (var i = 0; i < legs.length; i++) {
      if (legs[i].side === "BUY") hasBuy = true;
      else if (legs[i].side === "SELL") hasSell = true;
    }
  }
  var cls = " corridor-cell-leg";
  if (hasBuy) cls += " corridor-cell-leg-buy";
  if (hasSell) cls += " corridor-cell-leg-sell";
  return cls;
}

function buildSideBadgeEntries(legs, rightLabel, hasFallback) {
  if (!legs || !legs.length) {
    if (!hasFallback) return [];
    return [{ cls: "wall-badge wall-badge-strategy", text: "LEG " + rightLabel }];
  }
  var bySide = {};
  var sideOrder = [];
  for (var i = 0; i < legs.length; i++) {
    var side = legs[i].side === "SELL" ? "SELL" : "BUY";
    var qty = Math.max(1, parseInt(legs[i].qty, 10) || 1);
    if (!bySide[side]) {
      bySide[side] = 0;
      sideOrder.push(side);
    }
    bySide[side] += qty;
  }
  return sideOrder.map(function (side) {
    var totalQty = bySide[side];
    var sideCls = side === "SELL" ? "wall-badge-leg-sell" : "wall-badge-leg-buy";
    var arrow = side === "SELL" ? "\u25BC " : "\u25B2 ";
    var prefix = totalQty > 1 ? totalQty + "\u00D7 " : "";
    return {
      cls: "wall-badge wall-badge-strategy " + sideCls,
      text: arrow + prefix + side + " " + rightLabel,
    };
  });
}

function buildCorridorLegBadgesHtml(callLegs, putLegs, hasCallFallback, hasPutFallback) {
  var entries = []
    .concat(buildSideBadgeEntries(callLegs, "C", hasCallFallback))
    .concat(buildSideBadgeEntries(putLegs, "P", hasPutFallback));
  var html = "";
  for (var i = 0; i < entries.length; i++) {
    html +=
      '<span class="' +
      escapeHtml(entries[i].cls) +
      '">' +
      escapeHtml(entries[i].text) +
      "</span>";
  }
  return html;
}

export function buildCorridorCellTooltip(q, row, sideLabel, roles, sideLegs) {
  var baseRoles = Array.isArray(roles) ? roles.slice() : [];
  var hasFallbackLeg = Boolean(q && (q.is_strategy_leg || q.priority_tier === "PRIORITY_STRATEGY"));
  var legRoles = formatSideLegRoles(sideLegs, sideLabel, hasFallbackLeg);
  if (!q) {
    return (
      "No " +
      sideLabel +
      " quote in corridor for " +
      formatExpiryIso(row.expiry) +
      " (" +
      row.dte +
      "d) $" +
      fmtStrike(row.strike) +
      (legRoles.length ? "\n" + legRoles.join(", ") : "")
    );
  }
  var bidStr = q.bid !== null && q.bid !== undefined ? fmtNum(q.bid, 2) : "\u2014";
  var midStr = q.mid !== null && q.mid !== undefined ? fmtNum(q.mid, 2) : "\u2014";
  var askStr = q.ask !== null && q.ask !== undefined ? fmtNum(q.ask, 2) : "\u2014";
  var lastStr = q.last !== null && q.last !== undefined ? fmtNum(q.last, 2) : "\u2014";
  var closeStr = q.close !== null && q.close !== undefined ? fmtNum(q.close, 2) : "\u2014";
  var sprdStr =
    q.spread_pct !== null && q.spread_pct !== undefined ? fmtNum(q.spread_pct, 1) + "%" : "\u2014";
  var volStr = q.volume !== null && q.volume !== undefined ? fmtInt(q.volume) : "\u2014";
  var oiStr =
    q.open_interest !== null && q.open_interest !== undefined ? fmtInt(q.open_interest) : "\u2014";
  var ivStr = q.iv !== null && q.iv !== undefined ? fmtNum(Number(q.iv) * 100, 1) + "%" : "\u2014";
  var dStr = q.delta !== null && q.delta !== undefined ? fmtNum(q.delta, 2) : "\u2014";
  var combinedRoles = baseRoles.concat(legRoles);
  var roleStr = combinedRoles.length ? combinedRoles.join(", ") : q.zone || "CORRIDOR";
  return [
    (state.ticker ? state.ticker + " " : "") +
      formatExpiryIso(q.expiry) +
      " (" +
      q.dte +
      "d) $" +
      fmtStrike(q.strike) +
      " " +
      sideLabel,
    "Expiry (DTE): " + formatExpiryIso(q.expiry) + " (" + q.dte + "d)",
    "Bid / Mid / Ask: " + bidStr + " / " + midStr + " / " + askStr,
    "Last / Close: " + lastStr + " / " + closeStr,
    "Sprd%: " + sprdStr,
    "Vol / OI: " + volStr + " / " + oiStr,
    "IV / \u0394: " + ivStr + " / " + dStr,
    "Greeks source: " + (q.greeks_source || "NONE"),
    "Wall Role: " + roleStr,
  ].join("\n");
}

export function renderCorridorTable(frame) {
  var tbody = dom.corridorLadderTbody;
  if (!tbody) return;
  if (!frame || !Array.isArray(frame.quotes) || !frame.quotes.length) {
    tbody.innerHTML =
      '<tr><td colspan="11" class="empty-row">No corridor quotes loaded.</td></tr>';
    state.corridorPrevMids = {};
    return;
  }

  var primaryExpiry = String(frame.target_expiry || "") || findDominantExpiry(frame.quotes);
  var legLookup = buildCorridorLegLookup(state.strategyLegs);
  var changedSet = {};
  if (Array.isArray(frame.changed_keys)) {
    for (var ck = 0; ck < frame.changed_keys.length; ck++) {
      changedSet[frame.changed_keys[ck]] = true;
    }
  }

  var pairedMap = {};
  for (var i = 0; i < frame.quotes.length; i++) {
    var q = frame.quotes[i];
    var expStr = String(q.expiry || "");
    var strikeNum = Number(q.strike);
    var pKey = expStr + "|" + strikeNum.toFixed(4);
    if (!pairedMap[pKey]) {
      pairedMap[pKey] = {
        key: pKey,
        expiry: expStr,
        dte: Number(q.dte),
        strike: strikeNum,
        call: null,
        put: null,
      };
    }
    var rightUpper = String(q.right || "").toUpperCase();
    if (rightUpper === "C") {
      pairedMap[pKey].call = q;
    } else if (rightUpper === "P") {
      pairedMap[pKey].put = q;
    }
  }

  var rows = Object.keys(pairedMap).map(function (k) {
    return pairedMap[k];
  });
  rows.sort(function (a, b) {
    if (a.strike !== b.strike) return a.strike - b.strike;
    return a.expiry.localeCompare(b.expiry);
  });

  var spot =
    frame.underlying_price !== null && frame.underlying_price !== undefined
      ? Number(frame.underlying_price)
      : null;
  if (spot !== null && (Number.isNaN(spot) || spot <= 0)) {
    spot = null;
  }

  var atmRowKey = "";
  if (spot !== null && rows.length) {
    var primaryRows = rows.filter(function (r) {
      return r.expiry === primaryExpiry;
    });
    var atmPool = primaryRows.length ? primaryRows : rows;
    var bestAtm = atmPool[0];
    var bestDiff = Math.abs(bestAtm.strike - spot);
    for (var ai = 1; ai < atmPool.length; ai++) {
      var diff = Math.abs(atmPool[ai].strike - spot);
      if (diff < bestDiff - 1e-6) {
        bestAtm = atmPool[ai];
        bestDiff = diff;
      }
    }
    atmRowKey = bestAtm.key;
  }

  var nextPrevMids = {};
  var html = [];

  for (var r = 0; r < rows.length; r++) {
    var row = rows[r];
    var c = row.call;
    var p = row.put;

    var callLegs = legLookup[corridorKey(row.expiry, row.strike, "C")] || [];
    var putLegs = legLookup[corridorKey(row.expiry, row.strike, "P")] || [];
    var hasCallFallback =
      !callLegs.length && Boolean(c && (c.is_strategy_leg || c.priority_tier === "PRIORITY_STRATEGY"));
    var hasPutFallback =
      !putLegs.length && Boolean(p && (p.is_strategy_leg || p.priority_tier === "PRIORITY_STRATEGY"));
    var isLeg = Boolean(
      callLegs.length || putLegs.length || hasCallFallback || hasPutFallback
    );
    var isPrimaryExp = row.expiry === primaryExpiry;
    if (!isPrimaryExp && !isLeg) {
      continue;
    }

    var cFixedKey = corridorFixedKey(row.expiry, row.strike, "C");
    var pFixedKey = corridorFixedKey(row.expiry, row.strike, "P");
    var callFlash = resolveCorridorSideFlash(c, cFixedKey, frame, changedSet, nextPrevMids);
    var putFlash = resolveCorridorSideFlash(p, pFixedKey, frame, changedSet, nextPrevMids);

    var isPutWall = Boolean(
      isPrimaryExp &&
        ((p && p.is_put_wall) ||
          (frame.primary_put_wall !== null &&
            frame.primary_put_wall !== undefined &&
            Math.abs(row.strike - Number(frame.primary_put_wall)) < 1e-4))
    );
    var isCallWall = Boolean(
      isPrimaryExp &&
        ((c && c.is_call_wall) ||
          (frame.primary_call_wall !== null &&
            frame.primary_call_wall !== undefined &&
            Math.abs(row.strike - Number(frame.primary_call_wall)) < 1e-4))
    );
    var isAtm = row.key === atmRowKey;

    var rowClasses = [];
    if (isPutWall) rowClasses.push("corridor-row-put-wall");
    if (isCallWall) rowClasses.push("corridor-row-call-wall");
    if (isAtm) rowClasses.push("corridor-row-atm");
    if (isLeg) rowClasses.push("corridor-row-strategy");

    var wallRoles = [];
    if (isPutWall) wallRoles.push("PUT WALL");
    if (isCallWall) wallRoles.push("CALL WALL");
    if (isAtm) wallRoles.push("ATM");

    var callItm = spot !== null && spot > row.strike;
    var putItm = spot !== null && spot < row.strike;
    var callLegCls = corridorSideLegClasses(callLegs, hasCallFallback);
    var putLegCls = corridorSideLegClasses(putLegs, hasPutFallback);
    var callClsBase = "num corridor-call-cell" + (callItm ? " corridor-side-itm" : "") + callLegCls;
    var putClsBase = "num corridor-put-cell" + (putItm ? " corridor-side-itm" : "") + putLegCls;

    var callTipAttr = escapeHtml(buildCorridorCellTooltip(c, row, "CALL", wallRoles, callLegs));
    var putTipAttr = escapeHtml(buildCorridorCellTooltip(p, row, "PUT", wallRoles, putLegs));

    var cIv = c && c.iv !== null && c.iv !== undefined ? fmtNum(Number(c.iv) * 100, 1) + "%" : "\u2014";
    var cDelta = c && c.delta !== null && c.delta !== undefined ? fmtNum(c.delta, 2) : "\u2014";
    var cBid = c && c.bid !== null && c.bid !== undefined ? fmtNum(c.bid, 2) : "\u2014";
    var cMid = c && c.mid !== null && c.mid !== undefined ? fmtNum(c.mid, 2) : "\u2014";
    var cAsk = c && c.ask !== null && c.ask !== undefined ? fmtNum(c.ask, 2) : "\u2014";

    var pBid = p && p.bid !== null && p.bid !== undefined ? fmtNum(p.bid, 2) : "\u2014";
    var pMid = p && p.mid !== null && p.mid !== undefined ? fmtNum(p.mid, 2) : "\u2014";
    var pAsk = p && p.ask !== null && p.ask !== undefined ? fmtNum(p.ask, 2) : "\u2014";
    var pDelta = p && p.delta !== null && p.delta !== undefined ? fmtNum(p.delta, 2) : "\u2014";
    var pIv = p && p.iv !== null && p.iv !== undefined ? fmtNum(Number(p.iv) * 100, 1) + "%" : "\u2014";

    var badgesHtml = "";
    if (isPutWall) badgesHtml += '<span class="wall-badge wall-badge-put">PUT WALL</span>';
    if (isCallWall) badgesHtml += '<span class="wall-badge wall-badge-call">CALL WALL</span>';
    if (isAtm) badgesHtml += '<span class="wall-badge wall-badge-atm">ATM</span>';
    badgesHtml += buildCorridorLegBadgesHtml(callLegs, putLegs, hasCallFallback, hasPutFallback);

    var offExpHtml =
      row.expiry !== primaryExpiry
        ? '<span class="corridor-off-expiry-tag">\u2022 ' + escapeHtml(String(row.dte)) + "d</span>"
        : "";

    var strikeRoles = wallRoles
      .concat(formatSideLegRoles(callLegs, "CALL", hasCallFallback))
      .concat(formatSideLegRoles(putLegs, "PUT", hasPutFallback));
    var strikeTitle = escapeHtml(
      formatExpiryIso(row.expiry) +
        " (" +
        row.dte +
        "d) \u2022 $" +
        fmtStrike(row.strike) +
        (strikeRoles.length ? " \u2022 " + strikeRoles.join(" / ") : "")
    );

    html.push(
      '<tr class="' +
        escapeHtml(rowClasses.join(" ")) +
        '">' +
        '<td class="' +
        callClsBase +
        '" title="' +
        callTipAttr +
        '">' +
        escapeHtml(cIv) +
        "</td>" +
        '<td class="' +
        callClsBase +
        '" title="' +
        callTipAttr +
        '">' +
        escapeHtml(cDelta) +
        "</td>" +
        '<td class="' +
        callClsBase +
        '" title="' +
        callTipAttr +
        '">' +
        escapeHtml(cBid) +
        "</td>" +
        '<td class="' +
        callClsBase +
        " corridor-call-mid" +
        callFlash +
        '" title="' +
        callTipAttr +
        '">' +
        escapeHtml(cMid) +
        "</td>" +
        '<td class="' +
        callClsBase +
        '" title="' +
        callTipAttr +
        '">' +
        escapeHtml(cAsk) +
        "</td>" +
        '<td class="corridor-strike-cell" title="' +
        strikeTitle +
        '">' +
        '<span class="corridor-strike-value">$' +
        escapeHtml(fmtStrike(row.strike)) +
        "</span>" +
        offExpHtml +
        (badgesHtml ? '<span class="corridor-strike-badges">' + badgesHtml + "</span>" : "") +
        "</td>" +
        '<td class="' +
        putClsBase +
        '" title="' +
        putTipAttr +
        '">' +
        escapeHtml(pBid) +
        "</td>" +
        '<td class="' +
        putClsBase +
        " corridor-put-mid" +
        putFlash +
        '" title="' +
        putTipAttr +
        '">' +
        escapeHtml(pMid) +
        "</td>" +
        '<td class="' +
        putClsBase +
        '" title="' +
        putTipAttr +
        '">' +
        escapeHtml(pAsk) +
        "</td>" +
        '<td class="' +
        putClsBase +
        '" title="' +
        putTipAttr +
        '">' +
        escapeHtml(pDelta) +
        "</td>" +
        '<td class="' +
        putClsBase +
        '" title="' +
        putTipAttr +
        '">' +
        escapeHtml(pIv) +
        "</td>" +
        "</tr>"
    );
  }

  tbody.innerHTML = html.join("");
  state.corridorPrevMids = nextPrevMids;
}

function resolveCorridorChartLegs(frame, chartExp) {
  var quoteByKey = {};
  var dteByExp = {};
  for (var i = 0; i < frame.quotes.length; i++) {
    var fq = frame.quotes[i];
    var fk = corridorKey(fq.expiry, fq.strike, fq.right);
    quoteByKey[fk] = fq;
    if (fq.expiry && fq.dte !== null && fq.dte !== undefined) {
      dteByExp[String(fq.expiry)] = Number(fq.dte);
    }
  }
  if (state.chainData && Array.isArray(state.chainData.contracts)) {
    for (var ci = 0; ci < state.chainData.contracts.length; ci++) {
      var cr = state.chainData.contracts[ci];
      if (cr.expiry && !Object.prototype.hasOwnProperty.call(dteByExp, String(cr.expiry))) {
        dteByExp[String(cr.expiry)] = Number(cr.dte);
      }
    }
  }

  var resolved = [];
  if (Array.isArray(state.strategyLegs) && state.strategyLegs.length) {
    for (var l = 0; l < state.strategyLegs.length; l++) {
      var norm = normalizeCorridorLeg(state.strategyLegs[l]);
      if (!norm) continue;
      var q = quoteByKey[corridorKey(norm.expiry, norm.strike, norm.right)] || null;
      var legDte =
        q && q.dte !== null && q.dte !== undefined && Number.isFinite(Number(q.dte))
          ? Number(q.dte)
          : Object.prototype.hasOwnProperty.call(dteByExp, norm.expiry)
            ? dteByExp[norm.expiry]
            : null;
      resolved.push({
        side: norm.side,
        right: norm.right,
        expiry: norm.expiry,
        strike: norm.strike,
        qty: norm.qty,
        quote: q,
        dte: legDte,
        isOffExpiry: Boolean(chartExp && norm.expiry !== String(chartExp)),
      });
    }
    return resolved;
  }

  for (var qi = 0; qi < frame.quotes.length; qi++) {
    var fqLeg = frame.quotes[qi];
    if (!(fqLeg && (fqLeg.is_strategy_leg || fqLeg.priority_tier === "PRIORITY_STRATEGY"))) {
      continue;
    }
    var fExp = fqLeg.expiry ? String(fqLeg.expiry).trim() : "";
    var fRight = fqLeg.right ? String(fqLeg.right).toUpperCase() : "";
    var fStrike = Number(fqLeg.strike);
    if (!fExp || !Number.isFinite(fStrike) || (fRight !== "C" && fRight !== "P")) continue;
    resolved.push({
      side: "",
      right: fRight,
      expiry: fExp,
      strike: fStrike,
      qty: 1,
      quote: fqLeg,
      dte:
        fqLeg.dte !== null && fqLeg.dte !== undefined && Number.isFinite(Number(fqLeg.dte))
          ? Number(fqLeg.dte)
          : null,
      isOffExpiry: Boolean(chartExp && fExp !== String(chartExp)),
    });
  }
  return resolved;
}

function formatCorridorLegMarkerHover(leg) {
  var sideArrow = leg.side === "SELL" ? "\u25BC " : leg.side === "BUY" ? "\u25B2 " : "";
  var sidePrefix = leg.side ? sideArrow + leg.side + " " : "LEG ";
  var qtySuffix = leg.qty > 1 ? " x" + leg.qty : "";
  var dteTxt = leg.dte !== null && leg.dte !== undefined ? " (" + leg.dte + "d)" : "";
  var lines = [
    "<b>" +
      escapeHtml(sidePrefix + leg.right + qtySuffix) +
      "</b> $" +
      escapeHtml(fmtStrike(leg.strike)),
    "Expiry: " + escapeHtml(formatExpiryIso(leg.expiry) + dteTxt),
  ];
  var q = leg.quote;
  if (q) {
    var bidStr = q.bid !== null && q.bid !== undefined ? fmtNum(q.bid, 2) : "\u2014";
    var askStr = q.ask !== null && q.ask !== undefined ? fmtNum(q.ask, 2) : "\u2014";
    var midStr = q.mid !== null && q.mid !== undefined ? "$" + fmtNum(q.mid, 2) : "\u2014";
    lines.push("Bid/Ask: " + escapeHtml(bidStr + " / " + askStr));
    lines.push("Mid: " + escapeHtml(midStr));
    if (q.delta !== null && q.delta !== undefined) {
      lines.push("\u0394: " + escapeHtml(fmtNum(q.delta, 2)));
    }
  }
  return lines.join("<br>");
}

export function renderCorridorChart(frame, t) {
  var el = dom.corridorChartEl;
  if (!el || typeof window.Plotly === "undefined") return;
  if (!frame || !frame.quotes || !frame.quotes.length) {
    renderCorridorEmptyPlaceholder();
    return;
  }

  var chartTheme = t || themeVars();
  var chartExp = frame.target_expiry || findDominantExpiry(frame.quotes);
  var callX = [];
  var callY = [];
  var callUp = [];
  var callDn = [];
  var callHover = [];
  var putX = [];
  var putY = [];
  var putUp = [];
  var putDn = [];
  var putHover = [];

  for (var i = 0; i < frame.quotes.length; i++) {
    var q = frame.quotes[i];
    if (chartExp && q.expiry !== chartExp) continue;
    if (q.mid === null || q.mid === undefined) continue;
    var midVal = Number(q.mid);
    var bidVal = q.bid !== null && q.bid !== undefined ? Number(q.bid) : midVal;
    var askVal = q.ask !== null && q.ask !== undefined ? Number(q.ask) : midVal;
    var upErr = Math.max(0, askVal - midVal);
    var dnErr = Math.max(0, midVal - bidVal);
    var label =
      "$" +
      fmtNum(q.strike, 2) +
      " " +
      q.right +
      "<br>Bid/Ask: " +
      fmtNum(bidVal, 2) +
      " / " +
      fmtNum(askVal, 2) +
      "<br>Mid: $" +
      fmtNum(midVal, 2) +
      (q.delta !== null && q.delta !== undefined ? "<br>\u0394: " + fmtNum(q.delta, 2) : "");
    if (q.right === "C") {
      callX.push(q.strike);
      callY.push(midVal);
      callUp.push(upErr);
      callDn.push(dnErr);
      callHover.push(label);
    } else {
      putX.push(q.strike);
      putY.push(midVal);
      putUp.push(upErr);
      putDn.push(dnErr);
      putHover.push(label);
    }
  }

  function anchorMidForLeg(leg) {
    var lq = leg.quote;
    if (lq && lq.mid !== null && lq.mid !== undefined && Number.isFinite(Number(lq.mid))) {
      return Number(lq.mid);
    }
    var primaryX = leg.right === "P" ? putX : callX;
    var primaryY = leg.right === "P" ? putY : callY;
    var secondaryX = leg.right === "P" ? callX : putX;
    var secondaryY = leg.right === "P" ? callY : putY;
    var pools = [
      { xs: primaryX, ys: primaryY },
      { xs: secondaryX, ys: secondaryY },
    ];
    var bestY = null;
    var bestDist = Infinity;
    for (var pi = 0; pi < pools.length; pi++) {
      var xs = pools[pi].xs;
      var ys = pools[pi].ys;
      for (var xi = 0; xi < xs.length; xi++) {
        var d = Math.abs(xs[xi] - leg.strike);
        if (d < bestDist) {
          bestDist = d;
          bestY = ys[xi];
        }
      }
      if (bestDist < 1e-6 && bestY !== null) return bestY;
    }
    return bestY !== null && Number.isFinite(bestY) ? bestY : 0;
  }

  var chartLegs = resolveCorridorChartLegs(frame, chartExp);
  var onExpX = [];
  var onExpY = [];
  var onExpSymbols = [];
  var onExpColors = [];
  var onExpHover = [];
  var offExpX = [];
  var offExpY = [];
  var offExpSymbols = [];
  var offExpColors = [];
  var offExpHover = [];
  var markerGroupMap = {};

  for (var li = 0; li < chartLegs.length; li++) {
    var leg = chartLegs[li];
    var gk = leg.strike.toFixed(4) + "|" + (leg.isOffExpiry ? "1" : "0");
    if (!markerGroupMap[gk]) {
      markerGroupMap[gk] = {
        strike: leg.strike,
        isOffExpiry: leg.isOffExpiry,
        legs: [],
      };
    }
    markerGroupMap[gk].legs.push(leg);

    var legY = anchorMidForLeg(leg);
    var legColor =
      leg.side === "SELL"
        ? chartTheme.legSell || chartTheme.anomaly
        : leg.side === "BUY"
          ? chartTheme.legBuy || chartTheme.warn
          : chartTheme.warn;
    if (leg.isOffExpiry) {
      offExpX.push(leg.strike);
      offExpY.push(legY);
      offExpSymbols.push("diamond-open");
      offExpColors.push(legColor);
      offExpHover.push(formatCorridorLegMarkerHover(leg));
    } else {
      onExpX.push(leg.strike);
      onExpY.push(legY);
      onExpSymbols.push(
        leg.side === "SELL" ? "triangle-down" : leg.side === "BUY" ? "triangle-up" : "circle"
      );
      onExpColors.push(legColor);
      onExpHover.push(formatCorridorLegMarkerHover(leg));
    }
  }

  if (!callX.length && !putX.length && !onExpX.length && !offExpX.length) {
    renderCorridorEmptyPlaceholder();
    return;
  }

  el.classList.remove("is-idle");
  if (!el.data) {
    el.innerHTML = "";
  }

  var traces = [];
  if (callX.length) {
    traces.push({
      type: "scatter",
      mode: "lines+markers",
      name: "Call Mid (Bid-Ask)",
      x: callX,
      y: callY,
      error_y: {
        type: "data",
        symmetric: false,
        array: callUp,
        arrayminus: callDn,
        color: chartTheme.call,
        thickness: 1.5,
        width: 4,
      },
      line: { color: chartTheme.call, width: 2 },
      marker: { color: chartTheme.call, size: 7 },
      text: callHover,
      hovertemplate: "%{text}<extra></extra>",
    });
  }
  if (putX.length) {
    traces.push({
      type: "scatter",
      mode: "lines+markers",
      name: "Put Mid (Bid-Ask)",
      x: putX,
      y: putY,
      error_y: {
        type: "data",
        symmetric: false,
        array: putUp,
        arrayminus: putDn,
        color: chartTheme.put,
        thickness: 1.5,
        width: 4,
      },
      line: { color: chartTheme.put, width: 2 },
      marker: { color: chartTheme.put, size: 7, symbol: "diamond" },
      text: putHover,
      hovertemplate: "%{text}<extra></extra>",
    });
  }
  if (onExpX.length) {
    traces.push({
      type: "scatter",
      mode: "markers",
      name: "Strategy Legs",
      x: onExpX,
      y: onExpY,
      marker: {
        symbol: onExpSymbols,
        color: onExpColors,
        size: 12,
        line: { color: chartTheme.ink, width: 1.8 },
      },
      text: onExpHover,
      hovertemplate: "%{text}<extra></extra>",
    });
  }
  if (offExpX.length) {
    traces.push({
      type: "scatter",
      mode: "markers",
      name: "Strategy Legs (Other Exp)",
      x: offExpX,
      y: offExpY,
      marker: {
        symbol: offExpSymbols,
        color: offExpColors,
        size: 12,
        line: { color: offExpColors, width: 2.2 },
      },
      text: offExpHover,
      hovertemplate: "%{text}<extra></extra>",
    });
  }

  var markers = [
    makeVerticalMarker(frame.underlying_price, "SPOT $" + fmtNum(frame.underlying_price, 2), chartTheme.ink, 0.99, "dash", chartTheme),
  ];
  if (frame.primary_put_wall) {
    markers.push(
      makeVerticalMarker(frame.primary_put_wall, "PW $" + fmtNum(frame.primary_put_wall, 0), chartTheme.put, 0.87, "dot", chartTheme)
    );
  }
  if (frame.primary_call_wall) {
    markers.push(
      makeVerticalMarker(frame.primary_call_wall, "CW $" + fmtNum(frame.primary_call_wall, 0), chartTheme.call, 0.75, "dot", chartTheme)
    );
  }

  var markerGroups = Object.keys(markerGroupMap).map(function (k) {
    return markerGroupMap[k];
  });
  markerGroups.sort(function (a, b) {
    if (a.strike !== b.strike) return a.strike - b.strike;
    return Number(a.isOffExpiry) - Number(b.isOffExpiry);
  });

  for (var gi = 0; gi < markerGroups.length; gi++) {
    var grp = markerGroups[gi];
    var partsByKey = {};
    var partOrder = [];
    var hasBuy = false;
    var hasSell = false;
    var hasUnknown = false;
    var dteTags = {};

    for (var gli = 0; gli < grp.legs.length; gli++) {
      var gLeg = grp.legs[gli];
      if (gLeg.side === "BUY") hasBuy = true;
      else if (gLeg.side === "SELL") hasSell = true;
      else hasUnknown = true;

      var pk = (gLeg.side || "LEG") + "|" + gLeg.right;
      if (!partsByKey[pk]) {
        partsByKey[pk] = { side: gLeg.side, right: gLeg.right, qty: 0 };
        partOrder.push(pk);
      }
      partsByKey[pk].qty += gLeg.qty;
      if (grp.isOffExpiry) {
        if (gLeg.dte !== null && gLeg.dte !== undefined) {
          dteTags[gLeg.dte + "d"] = true;
        } else if (gLeg.expiry) {
          dteTags[formatExpiryIso(gLeg.expiry)] = true;
        }
      }
    }

    var legDesc = partOrder
      .map(function (pk) {
        var entry = partsByKey[pk];
        var arrow = entry.side === "SELL" ? "\u25BC " : entry.side === "BUY" ? "\u25B2 " : "";
        var prefix = entry.qty > 1 ? entry.qty + "\u00D7 " : "";
        return arrow + prefix + (entry.side ? entry.side + " " : "LEG ") + entry.right;
      })
      .join(" + ");
    var dteList = Object.keys(dteTags);
    var markerLabel = escapeHtml(
      legDesc +
        " $" +
        fmtStrike(grp.strike) +
        (grp.isOffExpiry && dteList.length ? " (" + dteList.join(", ") + ")" : "")
    );
    var markerColor =
      hasBuy && !hasSell && !hasUnknown
        ? chartTheme.legBuy || chartTheme.warn
        : hasSell && !hasBuy && !hasUnknown
          ? chartTheme.legSell || chartTheme.anomaly
          : chartTheme.warn;
    var yPos = Number((0.62 - (gi % 5) * 0.1).toFixed(2));
    markers.push(
      makeVerticalMarker(grp.strike, markerLabel, markerColor, yPos, "dot", chartTheme)
    );
  }

  var layout = baseLayout(chartTheme, "Corridor Strike ($)", "Option Mid & Bid-Ask ($)", markers);
  layout.xaxis.tickprefix = "$";
  layout.yaxis.tickprefix = "$";
  layout.legend = {
    orientation: "h",
    y: 1.13,
    x: 0.5,
    xanchor: "center",
    font: { size: 10, color: chartTheme.ink },
  };
  layout.margin = { t: 46, r: 20, b: 44, l: 58 };
  window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
  schedulePlotlyResize(el);
  window.setTimeout(function () {
    schedulePlotlyResize(el);
  }, 60);
}

export function renderCorridorLadderChart(frame, t) {
  return renderCorridorChart(frame, t);
}

function mergeCorridorQuotesIntoChain(frame, options) {
  if (!state.chainData || !state.chainData.contracts || !frame || !frame.quotes) return false;
  var opts = options || {};
  var bypassThrottle = Boolean(opts.bypassAutoThrottle);
  if (frame.underlying_price && frame.underlying_price > 0) {
    state.chainData.meta.underlying_price = frame.underlying_price;
    if (dom.kpiSpot) {
      var spotValEl = dom.kpiSpot.querySelector(".kpi-value");
      if (spotValEl) spotValEl.textContent = fmtMoney(frame.underlying_price);
    }
  }

  var byKey = {};
  for (var i = 0; i < frame.quotes.length; i++) {
    var q = frame.quotes[i];
    byKey[corridorKey(q.expiry, q.strike, q.right)] = q;
  }

  var touched = false;
  for (var c = 0; c < state.chainData.contracts.length; c++) {
    var row = state.chainData.contracts[c];
    var match = byKey[corridorKey(row.expiry, row.strike, row.right)];
    if (!match) continue;
    touched = true;
    row.bid = match.bid;
    row.ask = match.ask;
    row.mid = match.mid;
    if (match.mid !== null && match.mid !== undefined) row.mark_price = match.mid;
    row.last = match.last;
    row.close = match.close;
    if (match.volume !== null && match.volume !== undefined) row.volume = match.volume;
    if (match.open_interest !== null && match.open_interest !== undefined) {
      row.open_interest = match.open_interest;
    }
    if (match.iv !== null && match.iv !== undefined) row.iv = match.iv;
    if (match.delta !== null && match.delta !== undefined) row.delta = match.delta;
    if (match.gamma !== null && match.gamma !== undefined) row.gamma = match.gamma;
    if (match.theta !== null && match.theta !== undefined) row.theta = match.theta;
    if (match.vega !== null && match.vega !== undefined) row.vega = match.vega;
    if (match.greeks_source && match.greeks_source !== "NONE") {
      row.greeks_source = match.greeks_source;
    }
  }

  if (touched) {
    refreshStrategyLegsQuotesOnly();
    renderTable(state.chainData);
  }

  if (
    dom.corridorAutoReprice &&
    dom.corridorAutoReprice.checked &&
    state.strategyLegs.length &&
    (bypassThrottle || (!state.strategyBusy && Date.now() - state.corridorLastRepriceAt >= 2000))
  ) {
    evaluateStrategyRequest({ silent: true });
  }
  return touched;
}

export function applyCorridorFrame(frame, options) {
  var opts = options || {};
  state.corridorFrame = frame;
  updateCorridorStatusUI(frame, opts.modeLabel);
  renderCorridorKpis(frame);
  renderCorridorTable(frame);
  if (isPanelVisible("corridor")) {
    renderCorridorChart(frame, themeVars());
  }
  if (opts.mergeIntoChain) {
    mergeCorridorQuotesIntoChain(frame, opts);
  }
}

function copyLiveQuoteOverlay(srcQuote, dstQuote) {
  if (!srcQuote || !dstQuote) return dstQuote;
  var out = Object.assign({}, dstQuote);
  var fields = [
    "option_conid",
    "bid",
    "ask",
    "mid",
    "last",
    "close",
    "spread_pct",
    "volume",
    "open_interest",
    "iv",
    "delta",
    "gamma",
    "theta",
    "vega",
    "greeks_source",
    "updated_ts_utc",
  ];
  for (var i = 0; i < fields.length; i++) {
    var f = fields[i];
    if (srcQuote[f] !== undefined) {
      out[f] = srcQuote[f];
    }
  }
  return out;
}

export function reconcileCorridorFrameWithStrategy(prevFrame, preview, strategyLegs) {
  if (!preview) return prevFrame || null;
  var legLookup = buildCorridorLegLookup(strategyLegs);
  if (!prevFrame || prevFrame.mode === "PREVIEW" || !Array.isArray(prevFrame.quotes)) {
    return preview;
  }

  var prevByKey = {};
  for (var i = 0; i < prevFrame.quotes.length; i++) {
    var pq = prevFrame.quotes[i];
    if (!pq) continue;
    prevByKey[corridorKey(pq.expiry, pq.strike, pq.right)] = pq;
  }

  var sameTargetExpiry = String(prevFrame.target_expiry || "") === String(preview.target_expiry || "");
  var allCovered = sameTargetExpiry && preview.quotes.length > 0;
  if (allCovered) {
    for (var j = 0; j < preview.quotes.length; j++) {
      var q = preview.quotes[j];
      if (!prevByKey[corridorKey(q.expiry, q.strike, q.right)]) {
        allCovered = false;
        break;
      }
    }
  }

  var mergedQuotes = [];
  for (var k = 0; k < preview.quotes.length; k++) {
    var pqNext = preview.quotes[k];
    var pKey = corridorKey(pqNext.expiry, pqNext.strike, pqNext.right);
    var prevQ = prevByKey[pKey];
    var baseQ = prevQ ? copyLiveQuoteOverlay(prevQ, pqNext) : Object.assign({}, pqNext);
    baseQ.is_strategy_leg = Boolean(legLookup[pKey]);
    baseQ.zone = pqNext.zone;
    baseQ.is_put_wall = pqNext.is_put_wall;
    baseQ.is_call_wall = pqNext.is_call_wall;
    mergedQuotes.push(baseQ);
  }

  if (allCovered) {
    return Object.assign({}, prevFrame, {
      window_strikes: preview.window_strikes,
      max_contracts: preview.max_contracts,
      quotes: mergedQuotes,
    });
  }

  return Object.assign({}, preview, { quotes: mergedQuotes });
}

export function syncCorridorWithStrategy(options) {
  var opts = options || {};
  if (!state.chainData) return null;
  syncAutoFollowOptionLabels(state.chainData);

  if (state.corridorStreaming) {
    var frame = state.corridorFrame;
    if (frame && Array.isArray(frame.quotes)) {
      var legLookup = buildCorridorLegLookup(state.strategyLegs);
      for (var i = 0; i < frame.quotes.length; i++) {
        var q = frame.quotes[i];
        if (!q) continue;
        q.is_strategy_leg = Boolean(legLookup[corridorKey(q.expiry, q.strike, q.right)]);
      }
      renderCorridorTable(frame);
      if (isPanelVisible("corridor")) {
        renderCorridorChart(frame, themeVars());
      }
    }

    var nextSig = corridorLegSubscriptionSignature(state.chainData);
    if (nextSig === state.corridorStreamSignature) {
      if (state.corridorStreamSyncTimer) {
        clearTimeout(state.corridorStreamSyncTimer);
        state.corridorStreamSyncTimer = null;
      }
      return frame || null;
    }

    if (state.corridorStreamSyncTimer) {
      clearTimeout(state.corridorStreamSyncTimer);
      state.corridorStreamSyncTimer = null;
    }
    state.corridorStreamSyncTimer = setTimeout(function () {
      state.corridorStreamSyncTimer = null;
      if (
        state.corridorStreaming &&
        state.chainData &&
        corridorLegSubscriptionSignature(state.chainData) !== state.corridorStreamSignature
      ) {
        startCorridorStream();
      }
    }, 400);
    return frame || null;
  }

  var preview = buildLocalCorridorPreview(state.chainData);
  var nextFrame =
    !state.corridorFrame || state.corridorFrame.mode === "PREVIEW" || opts.forcePreview
      ? preview
      : reconcileCorridorFrameWithStrategy(state.corridorFrame, preview, state.strategyLegs);
  applyCorridorFrame(nextFrame, { mergeIntoChain: false });
  return nextFrame;
}

export function syncCorridorFromChain(data) {
  if (state.corridorStreaming || state.corridorStreamSource) {
    stopCorridorStream({ silent: true });
  }
  state.corridorPrevMids = {};
  if (dom.corridorStreamToggle) dom.corridorStreamToggle.checked = false;

  if (dom.corridorExpirySelect) {
    var prev = dom.corridorExpirySelect.value;
    dom.corridorExpirySelect.innerHTML = "";
    var autoOpt = document.createElement("option");
    autoOpt.value = "";
    autoOpt.textContent = formatCorridorAutoFollowLabel(data);
    dom.corridorExpirySelect.appendChild(autoOpt);
    var exps = sortedChainExpiries(data);
    var snappedCorridorExp = snapDteValueToExpiries(prev, exps, {
      allowBuckets: false,
      emptyValue: "",
    });
    for (var i = 0; i < exps.length; i++) {
      var opt = document.createElement("option");
      opt.value = exps[i].expiry;
      opt.textContent = formatExpiryIso(exps[i].expiry) + " (" + exps[i].dte + "d)";
      if (snappedCorridorExp && snappedCorridorExp === exps[i].expiry) opt.selected = true;
      dom.corridorExpirySelect.appendChild(opt);
    }
    dom.corridorExpirySelect.value = snappedCorridorExp || "";
  }

  var preview = buildLocalCorridorPreview(data);
  applyCorridorFrame(preview, { mergeIntoChain: false });
}

export async function refreshCorridorQuotesOneShot() {
  if (!state.chainData || state.corridorBusy) {
    if (!state.chainData) {
      showBanner("warn", "Load or refresh an option snapshot before fetching Corridor quotes.");
    }
    return;
  }
  state.corridorBusy = true;
  if (dom.corridorRefreshBtn) {
    dom.corridorRefreshBtn.disabled = true;
    dom.corridorRefreshBtn.textContent = "FETCHING CORRIDOR...";
  }
  var defWin = getDefaultWindowStrikes();
  var maxContracts = getDefaultMaxContracts();
  var winVal = dom.corridorWindowSelect
    ? Math.max(1, parseInt(dom.corridorWindowSelect.value, 10) || defWin)
    : Number(state.corridorWindow || defWin);
  var expVal = resolveCorridorTargetExpiry(state.chainData);
  try {
    var res = await apiJson("/api/corridor/quotes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticker: state.ticker,
        snapshot_id: state.snapshotId || null,
        target_expiry: expVal,
        window_strikes: winVal,
        max_contracts: maxContracts,
        priority_legs: corridorPriorityLegsList(),
      }),
    });
    if (res.ok && res.body) {
      applyCorridorFrame(res.body, { mergeIntoChain: true, bypassAutoThrottle: true });
      await fetchStatus();
      if (res.body.notices && res.body.notices.length) {
        showBanner("info", res.body.notices.join(" "));
      }
    } else if (res.status === 503) {
      await fetchStatus();
      showBanner(
        "warn",
        extractApiErrorMessage(
          res.body,
          res.textSnippet || "IBKR Gateway is offline. Preserving cached corridor view."
        ),
        {
          ticker: state.ticker,
          phase: "CORRIDOR",
          status: res.status,
          elapsedMs: res.elapsedMs,
          rawBody: res.textSnippet,
        }
      );
    } else {
      showBanner(
        "warn",
        extractApiErrorMessage(res.body, res.textSnippet || "Corridor quotes pull failed."),
        {
          ticker: state.ticker,
          phase: "CORRIDOR",
          status: res.status,
          elapsedMs: res.elapsedMs,
          rawBody: res.textSnippet,
        }
      );
    }
  } catch (err) {
    showBanner("error", "Corridor quote request failed.", {
      ticker: state.ticker,
      phase: "CORRIDOR",
      elapsedMs: err && err.elapsedMs,
      detail: err && err.message ? err.message : String(err || ""),
    });
  } finally {
    state.corridorBusy = false;
    if (dom.corridorRefreshBtn) {
      dom.corridorRefreshBtn.disabled = false;
      dom.corridorRefreshBtn.textContent = "Refresh Corridor Quotes";
    }
    document.dispatchEvent(new Event("ibkr:lock-released"));
  }
}

function postCorridorStop() {
  var prior = state.corridorStopPromise;
  var runStop = function () {
    try {
      return Promise.resolve(
        window.fetch("/api/corridor/stop", { method: "POST", keepalive: true })
      ).catch(function () {});
    } catch (_err) {
      return Promise.resolve();
    }
  };
  var p = (prior ? prior.then(runStop, runStop) : runStop()).finally(function () {
    if (state.corridorStopPromise === p) {
      state.corridorStopPromise = null;
    }
  });
  state.corridorStopPromise = p;
  return p;
}

export function stopCorridorStream(options) {
  var opts = options || {};
  if (state.corridorStreamSyncTimer) {
    clearTimeout(state.corridorStreamSyncTimer);
    state.corridorStreamSyncTimer = null;
  }
  state.corridorStreamEpoch += 1;
  state.corridorStreamSignature = "";
  var wasActive = Boolean(state.corridorStreaming || state.corridorStreamSource);
  state.corridorStreaming = false;
  if (dom.corridorStreamToggle) dom.corridorStreamToggle.checked = false;
  if (state.corridorStreamSource) {
    try {
      state.corridorStreamSource.close();
    } catch (_err) {
      /* ignore */
    }
    state.corridorStreamSource = null;
  }
  if (wasActive && !opts.skipRemoteStop) {
    postCorridorStop();
  }
  updateCorridorStatusUI(state.corridorFrame, opts.modeLabel || null);
  if (wasActive) {
    updateCorridorSubsBadge(null);
  }
  if (!opts.silent && wasActive) {
    showBanner("info", "Live Wall Corridor streaming stopped; IBKR subscriptions cancelled.");
  }
}

export async function startCorridorStream() {
  if (!state.chainData) {
    if (dom.corridorStreamToggle) dom.corridorStreamToggle.checked = false;
    showBanner("warn", "Load or refresh an option snapshot before enabling Live Streaming.");
    return;
  }
  if (state.corridorStreamSyncTimer) {
    clearTimeout(state.corridorStreamSyncTimer);
    state.corridorStreamSyncTimer = null;
  }

  var myEpoch = ++state.corridorStreamEpoch;
  var wasActive = Boolean(state.corridorStreaming || state.corridorStreamSource);
  if (state.corridorStreamSource) {
    try {
      state.corridorStreamSource.close();
    } catch (_err) {
      /* ignore */
    }
    state.corridorStreamSource = null;
  }

  state.corridorStreaming = true;
  state.corridorStreamSignature = corridorLegSubscriptionSignature(state.chainData);
  if (dom.corridorStreamToggle) dom.corridorStreamToggle.checked = true;
  updateCorridorStatusUI(state.corridorFrame, "CONNECTING STREAM...");

  if (wasActive) {
    postCorridorStop();
  }
  while (state.corridorStopPromise) {
    await state.corridorStopPromise;
    if (!state.corridorStreaming || state.corridorStreamEpoch !== myEpoch || !state.chainData) {
      return;
    }
  }

  state.corridorStreamSignature = corridorLegSubscriptionSignature(state.chainData);
  var defWin = getDefaultWindowStrikes();
  var maxContracts = getDefaultMaxContracts();
  var winVal = dom.corridorWindowSelect
    ? Math.max(1, parseInt(dom.corridorWindowSelect.value, 10) || defWin)
    : Number(state.corridorWindow || defWin);
  var expVal = resolveCorridorTargetExpiry(state.chainData) || "";
  var params = new URLSearchParams();
  if (state.snapshotId) params.set("snapshot_id", state.snapshotId);
  if (expVal) params.set("target_expiry", expVal);
  params.set("window_strikes", String(winVal));
  params.set("max_contracts", String(maxContracts));
  var legParam = corridorPriorityLegsParam();
  if (legParam) params.set("priority_legs", legParam);

  var url = "/api/corridor/stream/" + encodeURIComponent(state.ticker) + "?" + params.toString();
  var es = new window.EventSource(url);
  state.corridorStreamSource = es;

  es.addEventListener("frame", function (ev) {
    if (
      !state.corridorStreaming ||
      state.corridorStreamSource !== es ||
      state.corridorStreamEpoch !== myEpoch
    ) {
      return;
    }
    if (!state.status || !state.status.connected) {
      fetchStatus();
    }
    try {
      var frame = JSON.parse(ev.data);
      applyCorridorFrame(frame, { mergeIntoChain: true });
    } catch (_err) {
      /* ignore malformed tick */
    }
  });

  es.addEventListener("error", function (ev) {
    if (state.corridorStreamSource !== es || state.corridorStreamEpoch !== myEpoch) return;
    if (ev && ev.data) {
      var msg = "IBKR Gateway error during live corridor stream.";
      try {
        var errObj = JSON.parse(ev.data);
        if (errObj && errObj.message) msg = errObj.message;
      } catch (_err) {
        /* ignore */
      }
      stopCorridorStream({ silent: true, skipRemoteStop: true, modeLabel: "STREAM OFFLINE" });
      fetchStatus();
      showBanner("warn", msg);
    }
  });

  es.onerror = function () {
    if (
      !state.corridorStreaming ||
      state.corridorStreamSource !== es ||
      state.corridorStreamEpoch !== myEpoch
    ) {
      return;
    }
    stopCorridorStream({ silent: true, modeLabel: "STREAM DISCONNECTED" });
    fetchStatus();
    showBanner(
      "warn",
      "Live corridor stream interrupted or IBKR Gateway unreachable (" +
        gatewayAddressLabel() +
        "). Subscriptions cancelled."
    );
  };
}
