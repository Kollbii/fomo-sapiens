/* IBKR Options Analyzer dashboard — AI analysis prompt export (`#walls-copy-ai-btn`) and clipboard helpers. */

import {
  dom,
  state,
} from "./state.js";
import {
  configuredDefaultMdType,
  configuredDteWindowLabel,
  formatRangeShort,
  showBanner,
} from "./helpers.js";

function promptNum(val, digits) {
  if (val === null || val === undefined || isNaN(Number(val))) return "n/a";
  return Number(val).toFixed(digits === undefined ? 2 : digits);
}

function promptInt(val) {
  if (val === null || val === undefined || isNaN(Number(val))) return "0";
  return Math.round(Number(val)).toLocaleString("en-US");
}

function promptExpiryIso(raw) {
  var s = String(raw || "");
  if (/^\d{8}$/.test(s)) {
    return s.slice(0, 4) + "-" + s.slice(4, 6) + "-" + s.slice(6, 8);
  }
  return s || "n/a";
}

function promptGreekVal(val, digits) {
  if (val === null || val === undefined || isNaN(Number(val))) return "n/a";
  return Number(val).toFixed(digits);
}

function promptGammaVal(val) {
  if (val === null || val === undefined || isNaN(Number(val))) return "n/a";
  var num = Number(val);
  var abs = Math.abs(num);
  var digits = abs > 0 && abs < 0.005 ? 4 : 3;
  return num.toFixed(digits);
}

function promptThetaVal(val) {
  if (val === null || val === undefined || isNaN(Number(val))) return "n/a";
  var num = Number(val);
  var abs = Math.abs(num);
  var digits = abs > 0 && abs < 0.02 ? 3 : 2;
  return num.toFixed(digits);
}

function firstValidNum(primary, fallback) {
  if (primary !== null && primary !== undefined && !isNaN(Number(primary))) {
    return Number(primary);
  }
  if (fallback !== null && fallback !== undefined && !isNaN(Number(fallback))) {
    return Number(fallback);
  }
  return null;
}

function formatPromptGreeksInline(ivVal, deltaVal, gammaVal, thetaVal) {
  var ivStr =
    ivVal !== null && ivVal !== undefined && !isNaN(Number(ivVal))
      ? promptNum(Number(ivVal) * 100, 1) + "%"
      : "n/a";
  return (
    "IV " +
    ivStr +
    ", Δ " +
    promptGreekVal(deltaVal, 2) +
    ", Γ " +
    promptGammaVal(gammaVal) +
    ", Θ " +
    promptThetaVal(thetaVal)
  );
}

function computeScopedWallsForPrompt(activeExpiries, spot, contractByKey) {
  var byStrike = {};
  var cMap = contractByKey || {};
  var totalCallOi = 0;
  var totalPutOi = 0;
  var totalCallVol = 0;
  var totalPutVol = 0;

  function addMetric(row, prefix, metric, val) {
    if (val === null || val === undefined || isNaN(Number(val))) return;
    row[prefix + "_" + metric + "_sum"] += Number(val);
    row[prefix + "_" + metric + "_cnt"] += 1;
  }

  for (var i = 0; i < activeExpiries.length; i++) {
    var expObj = activeExpiries[i];
    var expKey = String(expObj.expiry || "");
    var strikes = expObj && expObj.strikes ? expObj.strikes : [];
    for (var j = 0; j < strikes.length; j++) {
      var pt = strikes[j];
      var k = Number(pt.strike);
      if (isNaN(k)) continue;
      var key = String(k);
      if (!byStrike[key]) {
        byStrike[key] = {
          strike: k,
          call_oi: 0,
          put_oi: 0,
          call_volume: 0,
          put_volume: 0,
          call_iv_sum: 0,
          call_iv_cnt: 0,
          put_iv_sum: 0,
          put_iv_cnt: 0,
          call_delta_sum: 0,
          call_delta_cnt: 0,
          put_delta_sum: 0,
          put_delta_cnt: 0,
          call_gamma_sum: 0,
          call_gamma_cnt: 0,
          put_gamma_sum: 0,
          put_gamma_cnt: 0,
          call_theta_sum: 0,
          call_theta_cnt: 0,
          put_theta_sum: 0,
          put_theta_cnt: 0,
        };
      }
      var row = byStrike[key];
      var coi = Number(pt.call_oi || 0);
      var poi = Number(pt.put_oi || 0);
      var cvol = Number(pt.call_volume || 0);
      var pvol = Number(pt.put_volume || 0);
      row.call_oi += coi;
      row.put_oi += poi;
      row.call_volume += cvol;
      row.put_volume += pvol;
      totalCallOi += coi;
      totalPutOi += poi;
      totalCallVol += cvol;
      totalPutVol += pvol;

      var cRow = cMap[expKey + "|" + k + "|C"] || null;
      var pRow = cMap[expKey + "|" + k + "|P"] || null;
      addMetric(row, "call", "iv", firstValidNum(pt.call_iv, cRow ? cRow.iv : null));
      addMetric(row, "put", "iv", firstValidNum(pt.put_iv, pRow ? pRow.iv : null));
      addMetric(row, "call", "delta", firstValidNum(pt.call_delta, cRow ? cRow.delta : null));
      addMetric(row, "put", "delta", firstValidNum(pt.put_delta, pRow ? pRow.delta : null));
      addMetric(row, "call", "gamma", firstValidNum(pt.call_gamma, cRow ? cRow.gamma : null));
      addMetric(row, "put", "gamma", firstValidNum(pt.put_gamma, pRow ? pRow.gamma : null));
      addMetric(row, "call", "theta", firstValidNum(pt.call_theta, cRow ? cRow.theta : null));
      addMetric(row, "put", "theta", firstValidNum(pt.put_theta, pRow ? pRow.theta : null));
    }
  }

  function avgMetric(row, prefix, metric) {
    var cnt = row[prefix + "_" + metric + "_cnt"];
    return cnt > 0 ? row[prefix + "_" + metric + "_sum"] / cnt : null;
  }

  var ladder = Object.keys(byStrike)
    .map(function (k) {
      var r = byStrike[k];
      r.call_iv = avgMetric(r, "call", "iv");
      r.put_iv = avgMetric(r, "put", "iv");
      r.call_delta = avgMetric(r, "call", "delta");
      r.put_delta = avgMetric(r, "put", "delta");
      r.call_gamma = avgMetric(r, "call", "gamma");
      r.put_gamma = avgMetric(r, "put", "gamma");
      r.call_theta = avgMetric(r, "call", "theta");
      r.put_theta = avgMetric(r, "put", "theta");
      return r;
    })
    .sort(function (a, b) {
      return a.strike - b.strike;
    });

  function topN(field, n, side) {
    return ladder
      .filter(function (r) {
        return Number(r[field] || 0) > 0;
      })
      .slice()
      .sort(function (a, b) {
        if (b[field] !== a[field]) return b[field] - a[field];
        return Math.abs(a.strike - spot) - Math.abs(b.strike - spot);
      })
      .slice(0, n)
      .map(function (r) {
        var distPct = spot > 0 ? ((r.strike - spot) / spot) * 100 : 0;
        return {
          strike: r.strike,
          value: r[field],
          dist_pct: distPct,
          iv: r[side + "_iv"],
          delta: r[side + "_delta"],
          gamma: r[side + "_gamma"],
          theta: r[side + "_theta"],
        };
      });
  }

  return {
    ladder: ladder,
    totalCallOi: totalCallOi,
    totalPutOi: totalPutOi,
    totalCallVol: totalCallVol,
    totalPutVol: totalPutVol,
    topCallOi: topN("call_oi", 3, "call"),
    topPutOi: topN("put_oi", 3, "put"),
    topCallVol: topN("call_volume", 3, "call"),
    topPutVol: topN("put_volume", 3, "put"),
  };
}

function buildWallsAiPrompt(data) {
  if (!data || !data.meta || !data.profiles) return "";
  var meta = data.meta;
  var walls = data.walls || {};
  var spot = Number(meta.underlying_price || 0);
  var allExpiries = Array.isArray(data.profiles.expiries) ? data.profiles.expiries : [];
  // Multi-expiration mode: the prompt always covers every weekly expiry in the loaded
  // chain (independent of the #walls-dte-select chart scope).
  var isSingleDte = false;
  var activeExpiries = allExpiries;
  if (!activeExpiries.length) return "";

  var activeExpirySet = {};
  for (var ae = 0; ae < activeExpiries.length; ae++) {
    activeExpirySet[String(activeExpiries[ae].expiry)] = true;
  }

  var contracts = Array.isArray(data.contracts) ? data.contracts : [];
  var contractByConid = {};
  var contractByKey = {};
  var scopedSourceCounts = {};
  for (var ci = 0; ci < contracts.length; ci++) {
    var cItem = contracts[ci];
    if (!cItem) continue;
    if (cItem.option_conid !== null && cItem.option_conid !== undefined) {
      contractByConid[String(cItem.option_conid)] = cItem;
    }
    var cExp = String(cItem.expiry || "");
    var cKey = cExp + "|" + Number(cItem.strike) + "|" + String(cItem.right || "");
    contractByKey[cKey] = cItem;
    if (activeExpirySet[cExp] && cItem.greeks_source) {
      scopedSourceCounts[cItem.greeks_source] =
        (scopedSourceCounts[cItem.greeks_source] || 0) + 1;
    }
  }

  var scoped = computeScopedWallsForPrompt(activeExpiries, spot, contractByKey);
  var hasOi = scoped.totalCallOi + scoped.totalPutOi > 0;
  var selectedMetric = dom.wallsMetricSelect ? dom.wallsMetricSelect.value : "auto";
  var effectiveBasis =
    selectedMetric === "volume"
      ? "VOLUME"
      : selectedMetric === "oi"
      ? "OPEN_INTEREST"
      : hasOi
      ? walls.primary_basis || "OPEN_INTEREST"
      : "VOLUME";

  var rangeLabel =
    meta.range_pct !== null && meta.range_pct !== undefined
      ? formatRangeShort(meta.range_pct)
      : "n/a";
  var stepLabel =
    meta.strike_step !== null && meta.strike_step !== undefined
      ? "$" + promptNum(meta.strike_step, Number(meta.strike_step) % 1 === 0 ? 0 : 2)
      : "Auto";
  if (
    meta.requested_strike_step !== null &&
    meta.requested_strike_step !== undefined &&
    meta.strike_step !== null &&
    meta.strike_step !== undefined &&
    Math.abs(Number(meta.requested_strike_step) - Number(meta.strike_step)) > 1e-6
  ) {
    stepLabel +=
      " (żądano $" +
      promptNum(
        meta.requested_strike_step,
        Number(meta.requested_strike_step) % 1 === 0 ? 0 : 2
      ) +
      ")";
  }

  var ivSum = 0;
  var ivCnt = 0;
  for (var vi = 0; vi < activeExpiries.length; vi++) {
    var exIv = activeExpiries[vi].atm_iv;
    if (exIv !== null && exIv !== undefined && !isNaN(Number(exIv))) {
      ivSum += Number(exIv);
      ivCnt += 1;
    }
  }
  var avgAtmIvLabel = ivCnt > 0 ? promptNum((ivSum / ivCnt) * 100, 1) + "%" : "n/a";
  var greeksCounts = Object.keys(scopedSourceCounts).length
    ? scopedSourceCounts
    : meta.greeks_source_counts || {};
  var greeksSourceNames = Object.keys(greeksCounts);
  var greeksLabel = greeksSourceNames.length
    ? greeksSourceNames
        .map(function (k) {
          return k + ":" + greeksCounts[k];
        })
        .join(", ")
    : "n/a";
  var greeksSourceSummary = greeksSourceNames.length
    ? greeksSourceNames.join(" / ")
    : "n/a";

  var mdNames = {
    1: "TYPE 1 (Live OPRA)",
    2: "TYPE 2 (Frozen)",
    3: "TYPE 3 (15-min Delayed)",
    4: "TYPE 4 (Delayed-Frozen)",
  };
  var resolvedMdType =
    meta.market_data_type !== null && meta.market_data_type !== undefined
      ? meta.market_data_type
      : configuredDefaultMdType();
  var mdLabel = mdNames[resolvedMdType] || "TYPE " + resolvedMdType;

  var scopeTitle = isSingleDte
    ? "WYBRANE WYGAŚNIĘCIE (SINGLE DTE): " +
      activeExpiries[0].dte +
      " DTE (" +
      promptExpiryIso(activeExpiries[0].expiry) +
      ")"
    : "WSZYSTKIE TYGODNIOWE WYGAŚNIĘCIA (" +
      configuredDteWindowLabel() +
      " DTE): " +
      activeExpiries
        .map(function (ex) {
          return ex.dte + "d (" + promptExpiryIso(ex.expiry) + ")";
        })
        .join(", ");

  var atmStats = {
    cDeltaSum: 0,
    cDeltaCnt: 0,
    pDeltaSum: 0,
    pDeltaCnt: 0,
    cGammaSum: 0,
    cGammaCnt: 0,
    pGammaSum: 0,
    pGammaCnt: 0,
    cThetaSum: 0,
    cThetaCnt: 0,
    pThetaSum: 0,
    pThetaCnt: 0,
  };

  var byDteList = Array.isArray(walls.by_dte) ? walls.by_dte : [];
  var dteSummaryLines = activeExpiries.map(function (ex) {
    var wMatch = null;
    for (var k = 0; k < byDteList.length; k++) {
      if (String(byDteList[k].expiry) === String(ex.expiry)) {
        wMatch = byDteList[k];
        break;
      }
    }
    var cw =
      wMatch && wMatch.primary_call_wall
        ? "$" +
          promptNum(wMatch.primary_call_wall.strike, 2) +
          " (" +
          promptInt(wMatch.primary_call_wall.value) +
          ")"
        : "n/a";
    var pw =
      wMatch && wMatch.primary_put_wall
        ? "$" +
          promptNum(wMatch.primary_put_wall.strike, 2) +
          " (" +
          promptInt(wMatch.primary_put_wall.value) +
          ")"
        : "n/a";
    var atmIv =
      ex.atm_iv !== null && ex.atm_iv !== undefined ? promptNum(Number(ex.atm_iv) * 100, 1) + "%" : "n/a";

    var targetAtm =
      ex.atm_strike !== null && ex.atm_strike !== undefined && !isNaN(Number(ex.atm_strike))
        ? Number(ex.atm_strike)
        : spot;
    var exStrikes = Array.isArray(ex.strikes) ? ex.strikes : [];
    var atmPt = null;
    var bestAtmDist = Infinity;
    for (var si = 0; si < exStrikes.length; si++) {
      var stCand = exStrikes[si];
      var d = Math.abs(Number(stCand.strike) - targetAtm);
      if (d < bestAtmDist) {
        bestAtmDist = d;
        atmPt = stCand;
      }
    }
    var atmCallRow = atmPt
      ? contractByKey[String(ex.expiry) + "|" + Number(atmPt.strike) + "|C"] || null
      : null;
    var atmPutRow = atmPt
      ? contractByKey[String(ex.expiry) + "|" + Number(atmPt.strike) + "|P"] || null
      : null;
    var acDelta = atmPt ? firstValidNum(atmPt.call_delta, atmCallRow ? atmCallRow.delta : null) : null;
    var apDelta = atmPt ? firstValidNum(atmPt.put_delta, atmPutRow ? atmPutRow.delta : null) : null;
    var acGamma = atmPt ? firstValidNum(atmPt.call_gamma, atmCallRow ? atmCallRow.gamma : null) : null;
    var apGamma = atmPt ? firstValidNum(atmPt.put_gamma, atmPutRow ? atmPutRow.gamma : null) : null;
    var acTheta = atmPt ? firstValidNum(atmPt.call_theta, atmCallRow ? atmCallRow.theta : null) : null;
    var apTheta = atmPt ? firstValidNum(atmPt.put_theta, atmPutRow ? atmPutRow.theta : null) : null;

    if (acDelta !== null) {
      atmStats.cDeltaSum += acDelta;
      atmStats.cDeltaCnt += 1;
    }
    if (apDelta !== null) {
      atmStats.pDeltaSum += apDelta;
      atmStats.pDeltaCnt += 1;
    }
    if (acGamma !== null) {
      atmStats.cGammaSum += acGamma;
      atmStats.cGammaCnt += 1;
    }
    if (apGamma !== null) {
      atmStats.pGammaSum += apGamma;
      atmStats.pGammaCnt += 1;
    }
    if (acTheta !== null) {
      atmStats.cThetaSum += acTheta;
      atmStats.cThetaCnt += 1;
    }
    if (apTheta !== null) {
      atmStats.pThetaSum += apTheta;
      atmStats.pThetaCnt += 1;
    }

    return (
      "- " +
      ex.dte +
      " DTE | Exp " +
      promptExpiryIso(ex.expiry) +
      " | ATM Strike $" +
      promptNum(ex.atm_strike, 2) +
      " | ATM IV " +
      atmIv +
      " | Call Wall: " +
      cw +
      " | Put Wall: " +
      pw +
      " | ATM Greki C[Δ " +
      promptGreekVal(acDelta, 2) +
      ", Γ " +
      promptGammaVal(acGamma) +
      ", Θ " +
      promptThetaVal(acTheta) +
      "] P[Δ " +
      promptGreekVal(apDelta, 2) +
      ", Γ " +
      promptGammaVal(apGamma) +
      ", Θ " +
      promptThetaVal(apTheta) +
      "]"
    );
  });

  var avgAtmCallDelta = atmStats.cDeltaCnt ? atmStats.cDeltaSum / atmStats.cDeltaCnt : null;
  var avgAtmPutDelta = atmStats.pDeltaCnt ? atmStats.pDeltaSum / atmStats.pDeltaCnt : null;
  var avgAtmCallGamma = atmStats.cGammaCnt ? atmStats.cGammaSum / atmStats.cGammaCnt : null;
  var avgAtmPutGamma = atmStats.pGammaCnt ? atmStats.pGammaSum / atmStats.pGammaCnt : null;
  var avgAtmCallTheta = atmStats.cThetaCnt ? atmStats.cThetaSum / atmStats.cThetaCnt : null;
  var avgAtmPutTheta = atmStats.pThetaCnt ? atmStats.pThetaSum / atmStats.pThetaCnt : null;
  var atmGreeksSummary =
    "Call [Δ " +
    promptGreekVal(avgAtmCallDelta, 2) +
    ", Γ " +
    promptGammaVal(avgAtmCallGamma) +
    ", Θ " +
    promptThetaVal(avgAtmCallTheta) +
    "] | Put [Δ " +
    promptGreekVal(avgAtmPutDelta, 2) +
    ", Γ " +
    promptGammaVal(avgAtmPutGamma) +
    ", Θ " +
    promptThetaVal(avgAtmPutTheta) +
    "]";

  function fmtTopList(items) {
    if (!items.length) return "brak niezerowych pozycji (0)";
    return items
      .map(function (item, idx) {
        var sign = item.dist_pct >= 0 ? "+" : "";
        return (
          "#" +
          (idx + 1) +
          " $" +
          promptNum(item.strike, 2) +
          " (" +
          sign +
          promptNum(item.dist_pct, 1) +
          "% od Spot, kontr.: " +
          promptInt(item.value) +
          ", " +
          formatPromptGreeksInline(item.iv, item.delta, item.gamma, item.theta) +
          ")"
        );
      })
      .join("; ");
  }

  var pcOiRatio =
    scoped.totalCallOi > 0 ? promptNum(scoped.totalPutOi / scoped.totalCallOi, 2) : "n/a";
  var pcVolRatio =
    scoped.totalCallVol > 0 ? promptNum(scoped.totalPutVol / scoped.totalCallVol, 2) : "n/a";

  var allAnomalies =
    data.anomalies && Array.isArray(data.anomalies.anomalies) ? data.anomalies.anomalies : [];
  var formatAnomalyLine = function (a) {
    var volOi =
      a.vol_oi_ratio !== null && a.vol_oi_ratio !== undefined
        ? promptNum(a.vol_oi_ratio, 2) + "x"
        : "n/a";
    var zsc =
      a.volume_zscore !== null && a.volume_zscore !== undefined
        ? "z=" + promptNum(a.volume_zscore, 2)
        : "z=n/a";
    var matchedContract =
      (a.option_conid !== null && a.option_conid !== undefined
        ? contractByConid[String(a.option_conid)]
        : null) ||
      contractByKey[
        String(a.expiry || "") + "|" + Number(a.strike) + "|" + String(a.right || "")
      ] ||
      null;
    var aIv = firstValidNum(a.iv, matchedContract ? matchedContract.iv : null);
    var aDelta = firstValidNum(a.delta, matchedContract ? matchedContract.delta : null);
    var aGamma = firstValidNum(a.gamma, matchedContract ? matchedContract.gamma : null);
    var aTheta = firstValidNum(a.theta, matchedContract ? matchedContract.theta : null);
    var aSrc =
      a.greeks_source ||
      (matchedContract && matchedContract.greeks_source) ||
      greeksSourceSummary;
    return (
      "- " +
      a.dte +
      "d (" +
      promptExpiryIso(a.expiry) +
      ") " +
      a.right +
      " $" +
      promptNum(a.strike, 2) +
      " | Vol: " +
      promptInt(a.volume) +
      " vs OI: " +
      promptInt(a.open_interest) +
      " (Vol/OI=" +
      volOi +
      ", " +
      zsc +
      ") | " +
      formatPromptGreeksInline(aIv, aDelta, aGamma, aTheta) +
      " [" +
      aSrc +
      "] | Flagi: " +
      (a.reasons && a.reasons.length ? a.reasons.join(", ") : "VOLUME")
    );
  };

  function formatLadderLines(sc) {
    var out = [
      "Strike | OdSpot% | Call_OI | Put_OI | Net_OI(C-P) | Call_Vol | Put_Vol | Call_IV% | Put_IV% | C_Δ | P_Δ | C_Γ | P_Γ | C_Θ | P_Θ",
    ];
    for (var m = 0; m < sc.ladder.length; m++) {
      out.push(formatLadderRow(sc.ladder[m]));
    }
    return out;
  }

  function formatLadderRow(st) {
    var dist = spot > 0 ? ((st.strike - spot) / spot) * 100 : 0;
    var cIv =
      st.call_iv !== null && st.call_iv !== undefined
        ? promptNum(st.call_iv * 100, 1) + "%"
        : "n/a";
    var pIv =
      st.put_iv !== null && st.put_iv !== undefined
        ? promptNum(st.put_iv * 100, 1) + "%"
        : "n/a";
    return (
      "$" +
        promptNum(st.strike, 2) +
        " | " +
        (dist >= 0 ? "+" : "") +
        promptNum(dist, 1) +
        "% | " +
        promptInt(st.call_oi) +
        " | " +
        promptInt(st.put_oi) +
        " | " +
        promptInt(st.call_oi - st.put_oi) +
        " | " +
        promptInt(st.call_volume) +
        " | " +
        promptInt(st.put_volume) +
        " | " +
        cIv +
        " | " +
        pIv +
        " | " +
        promptGreekVal(st.call_delta, 2) +
        " | " +
        promptGreekVal(st.put_delta, 2) +
        " | " +
        promptGammaVal(st.call_gamma) +
        " | " +
        promptGammaVal(st.put_gamma) +
        " | " +
        promptThetaVal(st.call_theta) +
        " | " +
        promptThetaVal(st.put_theta)
    );
  }

  var todayIso = meta.market_date || "n/a";
  var undName = meta.underlying || "n/a";

  // Per-expiry detail blocks (Section 3).
  var expiryBlocks = [];
  var totalAnomalyCount = 0;
  for (var ei = 0; ei < activeExpiries.length; ei++) {
    var exp = activeExpiries[ei];
    var expSc = computeScopedWallsForPrompt([exp], spot, contractByKey);
    var expAnoms = allAnomalies.filter(function (a) {
      return String(a.expiry) === String(exp.expiry);
    });
    totalAnomalyCount += expAnoms.length;
    var expAtmIv =
      exp.atm_iv !== null && exp.atm_iv !== undefined && !isNaN(Number(exp.atm_iv))
        ? promptNum(Number(exp.atm_iv) * 100, 1) + "%"
        : "n/a";
    var expPcOi =
      expSc.totalCallOi > 0 ? promptNum(expSc.totalPutOi / expSc.totalCallOi, 2) : "n/a";
    var expPcVol =
      expSc.totalCallVol > 0 ? promptNum(expSc.totalPutVol / expSc.totalCallVol, 2) : "n/a";
    var expCallShare =
      expSc.totalCallOi + expSc.totalPutOi > 0
        ? promptNum((expSc.totalCallOi / (expSc.totalCallOi + expSc.totalPutOi)) * 100, 0) + "%"
        : "n/a";
    var block = [
      "--- EXPIRY " +
        (ei + 1) +
        "/" +
        activeExpiries.length +
        ": " +
        exp.dte +
        " DTE (" +
        promptExpiryIso(exp.expiry) +
        ") | ATM IV " +
        expAtmIv +
        " | ATM Strike $" +
        promptNum(exp.atm_strike, 2) +
        " ---",
      "- Call OI: " +
        promptInt(expSc.totalCallOi) +
        " | Put OI: " +
        promptInt(expSc.totalPutOi) +
        " | P/C (OI): " +
        expPcOi +
        " | udział Call w OI: " +
        expCallShare,
      "- Call Vol: " +
        promptInt(expSc.totalCallVol) +
        " | Put Vol: " +
        promptInt(expSc.totalPutVol) +
        " | P/C (Vol): " +
        expPcVol,
      "- Top 3 Call Walls (OI): " + fmtTopList(expSc.topCallOi),
      "- Top 3 Put Walls (OI): " + fmtTopList(expSc.topPutOi),
      "- Top 3 Call Walls (Vol): " + fmtTopList(expSc.topCallVol),
      "- Top 3 Put Walls (Vol): " + fmtTopList(expSc.topPutVol),
      "- Anomalie (Vol > OI / Z-Score), " + exp.dte + "d:",
      expAnoms.length
        ? expAnoms.slice(0, 6).map(formatAnomalyLine).join("\n")
        : "  brak wykrytych anomalii dla tego wygaśnięcia.",
      "- Drabina strike'ów (Greki: " + greeksSourceSummary + "):",
      formatLadderLines(expSc).join("\n"),
    ];
    expiryBlocks.push(block.join("\n"));
  }

  var lines = [
    "ROLA: Jesteś praktycznym analitykiem rynku opcji (struktura terminowa, wielu wygaśnięć naraz).",
    "ZADANIE WEWNĘTRZNE (zrób przed napisaniem odpowiedzi):",
    "1) Przeanalizuj głęboko WSZYSTKIE poniższe wygaśnięcia razem: porównaj ATM IV między datami (kształt struktury terminowej: contango / backwardation), migrację ścian Put/Call między datami (czy ściany rosną, przesuwają się wyżej/niżej, czy się kumulują), rozkład Gamma i Theta w czasie, układ P/C OI i Volume oraz anomalie Vol > OI i Z-Score.",
    "2) Uwzględnij kontekst dnia dzisiejszego (" + todayIso + ", UTC: " + (meta.snapshot_ts_utc || "n/a") + ") i aktualnego otoczenia rynkowego/makro/sektorowego dla " + undName + " (jeśli nie masz dostępu do bieżących wiadomości lub daty wyników, zaznacz to w 1 zdaniu zamiast zgadywać).",
    "3) Szukaj zależności między wygaśnięciami: czy ta sama ściana powtarza się w kilku datach (silny poziom), czy anomalie koncentrują się w jednej dacie, gdzie Theta jest najdroższa, a gdzie Gamma najbardziej wrażliwa.",
    "",
    "ŚCISŁE ZASADY ODPOWIEDZI:",
    "- JĘZYK: wyłącznie PROSTY, CODZIENNY POLSKI (zero ciężkiego żargonu; każde pojęcie typu Call Wall/Put Wall/IV/Delta/Gamma/Theta/struktura terminowa wyjaśnij od razu w prostych, ludzkich słowach).",
    "- FORMAT: BEZ ŚCIANY TEKSTU. Tylko krótkie wypunktowania dokładnie według 5-punktowego szablonu poniżej; w każdej sekcji porównuj wygaśnięcia (np. 'Expiry 1 vs Expiry 3'), nie opisuj każdego osobno od zera.",
    "- OBOWIĄZKOWO WYJAŚNIJ GREKI (SEKCJA 3): dokładnie 3 krótkie punkty (Delta, Gamma, Theta; max 2 zdania na każdy) z odniesieniem do różnic między wygaśnięciami.",
    "- LIMIT DŁUGOŚCI: MAKSYMALNIE 450-600 słów łącznie. Dokładnie, konkretnie, zero ogólników i powtórzeń danych.",
    "",
    "=== SEKCJA 1: KONTEKST OGÓLNY ===",
    "- Ticker: " + undName + " | Cena Spot: $" + promptNum(spot, 2),
    "- Data rynkowa: " + todayIso + " | Timestamp UTC: " + (meta.snapshot_ts_utc || "n/a") + " | Snapshot ID: " + (meta.snapshot_id || "n/a"),
    "- Tryb danych IBKR: " + mdLabel + " | Źródło Greeks/IV: " + greeksLabel + " | Śr. ATM IV (wszystkie daty): " + avgAtmIvLabel,
    "- Śr. Greeks ATM (" + greeksSourceSummary + "): " + atmGreeksSummary,
    "- Zakres pobrania: Range " + rangeLabel + " | Step $ " + stepLabel + " | Baza ścian: " + effectiveBasis,
    "- Zakres DTE: " + scopeTitle,
    "- Łącznie: Call OI " + promptInt(scoped.totalCallOi) + " | Put OI " + promptInt(scoped.totalPutOi) + " | P/C (OI) " + pcOiRatio + " | Call Vol " + promptInt(scoped.totalCallVol) + " | Put Vol " + promptInt(scoped.totalPutVol) + " | P/C (Vol) " + pcVolRatio + " | Anomalie: " + totalAnomalyCount,
    "",
    "=== SEKCJA 2: STRUKTURA TERMINOWA - PORÓWNANIE WYGAŚNIĘĆ (Expiry 1-" + activeExpiries.length + ") ===",
    dteSummaryLines.join("\n"),
    "",
    "=== SEKCJA 3: SZCZEGÓŁY WG WYGAŚNIĘCIA (ściany, Greki, anomalie, drabina) ===",
    expiryBlocks.join("\n\n"),
    "",
    "=== WYMAGANA STRUKTURA ODPOWIEDZI (SKOPIUJ DOKŁADNIE TE 5 NAGŁÓWKÓW, BEZ DŁUGICH AKAPITÓW) ===",
    "### 1. Kontekst i Struktura Terminowa",
    "- **Obecna pozycja ($" + promptNum(spot, 2) + "):** 1 krótkie zdanie - gdzie stoi kurs względem ścian z różnych dat i jaki widać klimat (spokój / presja wzrostowa / defensywa).",
    "- **Struktura terminowa IV:** 1-2 punkty - ATM IV rośnie czy maleje z datą (Expiry 1 vs ostatnie), co to mówi o oczekiwaniach rynku (np. strach krótkoterminowy vs spokój dalej).",
    "- **Kontekst dnia (" + todayIso + ") i świata:** 1-2 krótkie punkty o sytuacji rynkowej/sektorowej dla " + undName + " (lub wzmianka, jeśli brak live kalendarza, np. daty wyników).",
    "- **Jakość danych:** 1 punkt - źródło Greków (" + greeksSourceSummary + "), baza ścian (" + effectiveBasis + ", " + mdLabel + "), czy " + stepLabel + " i " + rangeLabel + " dają pełny obraz.",
    "",
    "### 2. Porównanie Walli (Expirations 1-" + activeExpiries.length + ")",
    "- **Call Wall (opór) w czasie:** główne poziomy $ w kolejnych datach i dystans %; czy ściana migruje w górę/dół, czy powtarza się (silny opór).",
    "- **Put Wall (wsparcie) w czasie:** analogicznie dla wsparcia; gdzie leży najmocniejsze wsparcie wspólne dla kilku dat.",
    "- **Przewaga stron (P/C):** 1 wniosek porównujący P/C OI i Volume między datami (która data jest najbardziej defensywna / agresywna).",
    "",
    "### 3. Analiza Greków (Delta, Gamma, Theta)",
    "- **Delta (wpływ ruchu kursu):** max 2 zdania - jak mocno reagują opcje przy ścianach i czy różni się to między bliską a dalszą datą.",
    "- **Gamma (przyspieszenie):** max 2 zdania - w której dacie Gamma jest największa przy kursie i co to znaczy dla gwałtownych ruchów.",
    "- **Theta (ucieczka czasu):** max 2 zdania - gdzie czas ucieka najszybciej i kto zyskuje (kupujący vs sprzedający opcje).",
    "",
    "### 4. Wykryte Anomalie",
    "- Maksymalnie 3 punkty o najsilniejszych sygnałach (data, C/P, strike $, Vol vs OI, Delta/Gamma/Theta - ostrożny zakład przy kursie czy tani los na loterię) i prosty wniosek, czy anomalie układają się w jedną spójną tezę między datami (albo wprost: brak istotnych anomalii).",
    "",
    "### 5. Top 3 strategie opcyjne pod ten układ",
    "Wybierz 3 najlepiej pasujące do struktury terminowej, ścian i IV (możesz użyć kalendarza/diagonali łączących różne daty). Dla każdej napisz TYLKO te podpunkty:",
    "1. **[Nazwa strategii - konkretne strike'i $ i daty YYYY-MM-DD z danych]**",
    "   - *Dlaczego pasuje:* 1 krótkie zdanie (jak wykorzystuje ściany / IV / różnicę między datami).",
    "   - *Scenariusz i ryzyko:* kiedy zarabia vs konkretny poziom $, gdzie teza się psuje.",
    "2. **[Nazwa strategii - konkretne strike'i $ i daty YYYY-MM-DD z danych]**",
    "   - *Dlaczego pasuje:* 1 krótkie zdanie.",
    "   - *Scenariusz i ryzyko:* kiedy zarabia vs konkretny poziom $, gdzie teza się psuje.",
    "3. **[Nazwa strategii - konkretne strike'i $ i daty YYYY-MM-DD z danych]**",
    "   - *Dlaczego pasuje:* 1 krótkie zdanie.",
    "   - *Scenariusz i ryzyko:* kiedy zarabia vs konkretny poziom $, gdzie teza się psuje.",
  ];

  return lines.join("\n");
}

function copyTextToClipboard(text) {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    return navigator.clipboard.writeText(text).catch(function () {
      return fallbackCopyText(text);
    });
  }
  return fallbackCopyText(text);
}

function fallbackCopyText(text) {
  return new Promise(function (resolve, reject) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-9999px";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, text.length);
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if (ok) resolve();
      else reject(new Error("Clipboard copy failed"));
    } catch (err) {
      reject(err);
    }
  });
}

export function copyWallsAiPrompt() {
  if (!state.chainData || !state.chainData.profiles) {
    showBanner("warn", "Load an option chain snapshot before copying the AI analysis prompt.");
    return;
  }
  var promptText = buildWallsAiPrompt(state.chainData);
  if (!promptText) {
    showBanner("warn", "No option strikes available in the active DTE scope.");
    return;
  }
  var dteScope = "all weekly expirations (30\u2013120 DTE)";
  copyTextToClipboard(promptText)
    .then(function () {
      if (dom.wallsCopyAiBtn) {
        dom.wallsCopyAiBtn.classList.add("is-copied");
        window.setTimeout(function () {
          if (dom.wallsCopyAiBtn) dom.wallsCopyAiBtn.classList.remove("is-copied");
        }, 1800);
      }
      showBanner(
        "success",
        "Copied Polish AI Put/Call Wall prompt to clipboard (" + dteScope + ")."
      );
    })
    .catch(function (err) {
      showBanner("error", "Could not copy prompt to clipboard: " + (err && err.message ? err.message : err));
    });
}
