/* IBKR Options Analyzer dashboard — Wall Strike Corridor: preview, one-shot quotes, SSE streaming and ladder chart. */

import {
  dom,
  state,
} from "./state.js";
import {
  configuredDefaultMdType,
  extractApiErrorMessage,
  fmtInt,
  fmtMoney,
  fmtNum,
  formatExpiryIso,
  formatStrikeShort,
  gatewayAddressLabel,
  showBanner,
  snapDteValueToExpiries,
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
  strategySortedExpiries,
} from "./strategy_lab.js";
import {
  apiJson,
  fetchStatus,
} from "./network.js";
import {
  schedulePlotlyResize,
} from "./grid_layout.js";

// ------------------------------------------------------------------ Wall Strike Corridor (+-5 Strikes & Live Streaming)
function corridorKey(expiry, strike, right) {
  return String(expiry) + "|" + Number(strike) + "|" + String(right).toUpperCase();
}

function corridorPriorityLegsList() {
  if (!state.strategyLegs || !state.strategyLegs.length) return [];
  return state.strategyLegs.map(function (l) {
    return {
      expiry: String(l.expiry),
      strike: Number(l.strike),
      right: String(l.right).toUpperCase(),
    };
  });
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
  var exps = strategySortedExpiries(data);
  if (!exps.length) return null;
  var win = dom.corridorWindowSelect ? Math.max(1, parseInt(dom.corridorWindowSelect.value, 10) || 5) : 5;
  state.corridorWindow = win;
  var maxCap = 56;
  var chosenExp =
    dom.corridorExpirySelect && dom.corridorExpirySelect.value
      ? dom.corridorExpirySelect.value
      : exps[0].expiry;
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
          wInfo.putWall !== null &&
          Math.abs(Number(row.strike) - wInfo.putWall) < 1e-4 &&
          row.right === "P",
        is_call_wall:
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
    subscribed_count: Math.min(60, selected.length + (selected.length < 60 ? 1 : 0)),
    subscription_cap: 60,
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

function updateCorridorStatusUI(frame, modeLabel) {
  var count = frame ? frame.subscribed_count : 0;
  var cap = frame && frame.subscription_cap ? frame.subscription_cap : 60;
  if (dom.corridorSubsBadge) {
    dom.corridorSubsBadge.textContent = count + " / " + cap + " SUBS";
    dom.corridorSubsBadge.className =
      "badge " + (state.corridorStreaming ? "badge--live" : "badge--info");
  }
  if (dom.corridorStatusBadge) {
    if (modeLabel) {
      dom.corridorStatusBadge.textContent = modeLabel;
    } else if (!frame) {
      dom.corridorStatusBadge.textContent = "IDLE \u2022 MANUAL";
    } else if (frame.mode === "STREAM") {
      dom.corridorStatusBadge.textContent =
        "STREAMING LIVE \u2022 #" + frame.sequence + " \u2022 " + frame.target_dte + "d EXP";
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

  var pwQ = null;
  var cwQ = null;
  var spreadSum = 0;
  var spreadCnt = 0;
  for (var i = 0; i < frame.quotes.length; i++) {
    var q = frame.quotes[i];
    if (q.is_put_wall && !pwQ) pwQ = q;
    if (q.is_call_wall && !cwQ) cwQ = q;
    if (q.spread_pct !== null && q.spread_pct !== undefined) {
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

function renderCorridorLadder(frame) {
  var tbody = dom.corridorLadderTbody;
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!frame || !frame.quotes || !frame.quotes.length) {
    var trEmpty = document.createElement("tr");
    var tdEmpty = document.createElement("td");
    tdEmpty.colSpan = 9;
    tdEmpty.className = "text-left";
    tdEmpty.textContent = "Load or refresh a ticker snapshot to view the Wall Corridor.";
    trEmpty.appendChild(tdEmpty);
    tbody.appendChild(trEmpty);
    return;
  }

  var changedSet = {};
  if (frame.changed_keys) {
    for (var c = 0; c < frame.changed_keys.length; c++) {
      changedSet[frame.changed_keys[c]] = true;
    }
  }
  var nextPrevMids = {};

  for (var i = 0; i < frame.quotes.length; i++) {
    var q = frame.quotes[i];
    var qKey = corridorKey(q.expiry, q.strike, q.right);
    var prevMid = state.corridorPrevMids[qKey];
    var curMid = q.mid !== null && q.mid !== undefined ? Number(q.mid) : null;
    if (curMid !== null) nextPrevMids[qKey] = curMid;

    var flashClass = "";
    if (
      frame.mode === "STREAM" &&
      changedSet[qKey] &&
      prevMid !== undefined &&
      curMid !== null
    ) {
      if (curMid > prevMid + 1e-4) flashClass = " flash-up";
      else if (curMid < prevMid - 1e-4) flashClass = " flash-down";
    }

    var tr = document.createElement("tr");
    var classes = [];
    if (q.is_put_wall) classes.push("row-wall-put");
    if (q.is_call_wall) classes.push("row-wall-call");
    if (q.is_strategy_leg) classes.push("row-strategy-leg");
    tr.className = classes.join(" ");

    var tdZone = document.createElement("td");
    tdZone.className = "text-left";
    var zSpan = document.createElement("span");
    var zLabel =
      q.zone === "PUT_WALL"
        ? "PW"
        : q.zone === "CALL_WALL"
          ? "CW"
          : q.zone === "BOTH_WALLS"
            ? "PW/CW"
            : q.zone === "STRATEGY_LEG"
              ? "LEG"
              : "ATM";
    zSpan.className =
      "corridor-zone-pill" +
      (q.is_put_wall
        ? " corridor-zone-pill--pw"
        : q.is_call_wall
          ? " corridor-zone-pill--cw"
          : "");
    zSpan.textContent = zLabel;
    tdZone.appendChild(zSpan);
    if (q.is_strategy_leg && zLabel !== "LEG") {
      var legSpan = document.createElement("span");
      legSpan.className = "corridor-zone-pill corridor-zone-pill--leg";
      legSpan.textContent = "LEG";
      tdZone.appendChild(legSpan);
    }
    tr.appendChild(tdZone);

    var tdRight = document.createElement("td");
    tdRight.className = "text-left";
    var rPill = document.createElement("span");
    rPill.className = "right-pill right-pill--" + q.right;
    rPill.textContent = q.right;
    tdRight.appendChild(rPill);
    tr.appendChild(tdRight);

    var tdStrike = document.createElement("td");
    tdStrike.className = "num";
    tdStrike.textContent =
      formatStrikeShort(q.strike) +
      (q.expiry !== frame.target_expiry ? " (" + q.dte + "d)" : "");
    tr.appendChild(tdStrike);

    var tdBid = document.createElement("td");
    tdBid.className = "num" + flashClass;
    tdBid.textContent = q.bid !== null && q.bid !== undefined ? fmtNum(q.bid, 2) : "\u2014";
    tr.appendChild(tdBid);

    var tdAsk = document.createElement("td");
    tdAsk.className = "num" + flashClass;
    tdAsk.textContent = q.ask !== null && q.ask !== undefined ? fmtNum(q.ask, 2) : "\u2014";
    tr.appendChild(tdAsk);

    var tdMid = document.createElement("td");
    tdMid.className = "num" + flashClass;
    tdMid.textContent = q.mid !== null && q.mid !== undefined ? fmtMoney(q.mid) : "\u2014";
    tr.appendChild(tdMid);

    var tdSpread = document.createElement("td");
    tdSpread.className = "num";
    tdSpread.textContent =
      q.spread_pct !== null && q.spread_pct !== undefined
        ? fmtNum(q.spread_pct, 1) + "%"
        : "\u2014";
    tr.appendChild(tdSpread);

    var tdVolOi = document.createElement("td");
    tdVolOi.className = "num";
    var vTxt = q.volume !== null && q.volume !== undefined ? fmtInt(q.volume) : "\u2014";
    var oiTxt =
      q.open_interest !== null && q.open_interest !== undefined
        ? fmtInt(q.open_interest)
        : "\u2014";
    tdVolOi.textContent = vTxt + " / " + oiTxt;
    tr.appendChild(tdVolOi);

    var tdGreeks = document.createElement("td");
    tdGreeks.className = "num";
    var gD = q.delta !== null && q.delta !== undefined ? fmtNum(q.delta, 2) : "\u2014";
    var gIv =
      q.iv !== null && q.iv !== undefined ? fmtNum(Number(q.iv) * 100, 1) + "%" : "\u2014";
    tdGreeks.textContent = gD + " \u2022 " + gIv;
    tr.appendChild(tdGreeks);

    tbody.appendChild(tr);
  }

  state.corridorPrevMids = nextPrevMids;
}

export function renderCorridorChart(frame, t) {
  var el = dom.corridorChartEl;
  if (!el || typeof window.Plotly === "undefined") return;
  if (!frame || !frame.quotes || !frame.quotes.length) {
    renderCorridorEmptyPlaceholder();
    return;
  }

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
    if (q.expiry !== frame.target_expiry) continue;
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

  if (!callX.length && !putX.length) {
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
        color: t.call,
        thickness: 1.5,
        width: 4,
      },
      line: { color: t.call, width: 2 },
      marker: { color: t.call, size: 7 },
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
        color: t.put,
        thickness: 1.5,
        width: 4,
      },
      line: { color: t.put, width: 2 },
      marker: { color: t.put, size: 7, symbol: "diamond" },
      text: putHover,
      hovertemplate: "%{text}<extra></extra>",
    });
  }

  var markers = [
    makeVerticalMarker(frame.underlying_price, "SPOT $" + fmtNum(frame.underlying_price, 2), t.ink, 0.99, "dash", t),
  ];
  if (frame.primary_put_wall) {
    markers.push(
      makeVerticalMarker(frame.primary_put_wall, "PW $" + fmtNum(frame.primary_put_wall, 0), t.put, 0.87, "dot", t)
    );
  }
  if (frame.primary_call_wall) {
    markers.push(
      makeVerticalMarker(frame.primary_call_wall, "CW $" + fmtNum(frame.primary_call_wall, 0), t.call, 0.75, "dot", t)
    );
  }

  var layout = baseLayout(t, "Corridor Strike ($)", "Option Mid & Bid-Ask ($)", markers);
  layout.xaxis.tickprefix = "$";
  layout.yaxis.tickprefix = "$";
  layout.legend = {
    orientation: "h",
    y: 1.13,
    x: 0.5,
    xanchor: "center",
    font: { size: 10, color: t.ink },
  };
  layout.margin = { t: 46, r: 20, b: 44, l: 58 };
  window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
  schedulePlotlyResize(el);
  window.setTimeout(function () {
    schedulePlotlyResize(el);
  }, 60);
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
  renderCorridorLadder(frame);
  if (isPanelVisible("corridor")) {
    renderCorridorChart(frame, themeVars());
  }
  if (opts.mergeIntoChain) {
    mergeCorridorQuotesIntoChain(frame, opts);
  }
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
    var exps = strategySortedExpiries(data);
    if (!exps.length) {
      var opt0 = document.createElement("option");
      opt0.value = "";
      opt0.textContent = "Front DTE";
      dom.corridorExpirySelect.appendChild(opt0);
    } else {
      var snappedCorridorExp = snapDteValueToExpiries(prev, exps, {
        allowBuckets: false,
        emptyValue: exps[0].expiry,
      });
      for (var i = 0; i < exps.length; i++) {
        var opt = document.createElement("option");
        opt.value = exps[i].expiry;
        opt.textContent = formatExpiryIso(exps[i].expiry) + " (" + exps[i].dte + "d)";
        if (snappedCorridorExp && snappedCorridorExp === exps[i].expiry) opt.selected = true;
        dom.corridorExpirySelect.appendChild(opt);
      }
      if (snappedCorridorExp) {
        dom.corridorExpirySelect.value = snappedCorridorExp;
      }
    }
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
  var winVal = dom.corridorWindowSelect ? parseInt(dom.corridorWindowSelect.value, 10) || 5 : 5;
  var expVal = dom.corridorExpirySelect && dom.corridorExpirySelect.value ? dom.corridorExpirySelect.value : null;
  try {
    var res = await apiJson("/api/corridor/quotes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticker: state.ticker,
        snapshot_id: state.snapshotId || null,
        target_expiry: expVal,
        window_strikes: winVal,
        max_contracts: 56,
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
  }
}

export function stopCorridorStream(options) {
  var opts = options || {};
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
    try {
      window.fetch("/api/corridor/stop", { method: "POST", keepalive: true }).catch(function () {});
    } catch (_err) {
      /* ignore */
    }
  }
  updateCorridorStatusUI(state.corridorFrame, opts.modeLabel || null);
  if (!opts.silent && wasActive) {
    showBanner("info", "Live Wall Corridor streaming stopped; IBKR subscriptions cancelled.");
  }
}

export function startCorridorStream() {
  if (!state.chainData) {
    if (dom.corridorStreamToggle) dom.corridorStreamToggle.checked = false;
    showBanner("warn", "Load or refresh an option snapshot before enabling Live Streaming.");
    return;
  }
  stopCorridorStream({ silent: true });

  var winVal = dom.corridorWindowSelect ? parseInt(dom.corridorWindowSelect.value, 10) || 5 : 5;
  var expVal = dom.corridorExpirySelect && dom.corridorExpirySelect.value ? dom.corridorExpirySelect.value : "";
  var params = new URLSearchParams();
  if (state.snapshotId) params.set("snapshot_id", state.snapshotId);
  if (expVal) params.set("target_expiry", expVal);
  params.set("window_strikes", String(winVal));
  params.set("max_contracts", "56");
  var legParam = corridorPriorityLegsParam();
  if (legParam) params.set("priority_legs", legParam);

  var url = "/api/corridor/stream/" + encodeURIComponent(state.ticker) + "?" + params.toString();
  state.corridorStreaming = true;
  if (dom.corridorStreamToggle) dom.corridorStreamToggle.checked = true;
  updateCorridorStatusUI(state.corridorFrame, "CONNECTING STREAM...");

  var es = new window.EventSource(url);
  state.corridorStreamSource = es;

  es.addEventListener("frame", function (ev) {
    if (!state.corridorStreaming) return;
    try {
      var frame = JSON.parse(ev.data);
      applyCorridorFrame(frame, { mergeIntoChain: true });
    } catch (_err) {
      /* ignore malformed tick */
    }
  });

  es.addEventListener("error", function (ev) {
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
    if (!state.corridorStreaming) return;
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
