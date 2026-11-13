/* IBKR Options Analyzer dashboard — Plotly multi-DTE charts (walls, IV smile, anomalies, Greeks) and walls view prefs. */

import {
  CHART_IDS,
  dom,
} from "./state.js";
import {
  DTE_BUCKET_DEFS,
  fmtInt,
  fmtNum,
  formatExpiryIso,
  formatRangeShort,
  formatStepShort,
  formatStrikeShort,
  matchesDteFilterValue,
  readPref,
  snapDteValueToExpiries,
} from "./helpers.js";

// ------------------------------------------------------------------ Plotly Charts
export function renderCorridorEmptyPlaceholder() {
  if (!dom.corridorChartEl) return;
  if (typeof window.Plotly !== "undefined" && dom.corridorChartEl.data) {
    window.Plotly.purge(dom.corridorChartEl);
  }
  dom.corridorChartEl.classList.add("is-idle");
  dom.corridorChartEl.innerHTML =
    '<div class="strategy-empty-state">Load an option snapshot to preview the Put/Call Wall \u00B15 strike corridor, or click <strong>Refresh Corridor Quotes</strong>.</div>';
}

export function renderStrategyEmptyPlaceholder() {
  if (!dom.strategyChartEl) return;
  if (typeof window.Plotly !== "undefined" && dom.strategyChartEl.data) {
    window.Plotly.purge(dom.strategyChartEl);
  }
  dom.strategyChartEl.classList.add("is-idle");
  dom.strategyChartEl.innerHTML =
    '<div class="strategy-empty-state">Click a preset or <strong>Auto-Build from Walls</strong> / <strong>Evaluate / Re-Price</strong> to plot T+0, Midpoint, Expiration &amp; Simulated P&amp;L curves.</div>';
}

export function purgeCharts() {
  if (typeof window.Plotly !== "undefined") {
    for (var i = 0; i < CHART_IDS.length; i++) {
      var el = document.getElementById(CHART_IDS[i]);
      if (el) {
        window.Plotly.purge(el);
        el.innerHTML = "";
      }
    }
  }
  renderCorridorEmptyPlaceholder();
  renderStrategyEmptyPlaceholder();
  if (dom.wallsBasisLabel) {
    dom.wallsBasisLabel.textContent = "NO DATA";
  }
}

export function makeVerticalMarker(xVal, label, color, yPos, dash, theme) {
  if (xVal === null || xVal === undefined || Number.isNaN(Number(xVal))) {
    return { shape: null, annotation: null };
  }
  var bgFill = theme && theme.bg ? theme.bg : "rgba(255,255,255,0.92)";
  var fontFam = theme && theme.fontMono ? theme.fontMono : "monospace";
  return {
    shape: {
      type: "line",
      x0: xVal,
      x1: xVal,
      y0: 0,
      y1: 1,
      yref: "paper",
      line: { color: color, width: 2, dash: dash || "dash" },
    },
    annotation: {
      x: xVal,
      y: yPos !== undefined ? yPos : 0.99,
      yref: "paper",
      text: "<b>" + label + "</b>",
      showarrow: false,
      font: { size: 11, color: color, family: fontFam },
      bgcolor: bgFill,
      bordercolor: color,
      borderwidth: 1.5,
      borderpad: 3,
      yanchor: "top",
    },
  };
}

function makeWallReferenceMarker(
  orientation,
  strikeVal,
  label,
  color,
  posOffset,
  dash,
  placeAbove,
  theme
) {
  if (strikeVal === null || strikeVal === undefined || Number.isNaN(Number(strikeVal))) {
    return { shape: null, annotation: null };
  }
  var bgFill = theme && theme.bg ? theme.bg : "rgba(255,255,255,0.92)";
  var fontFam = theme && theme.fontMono ? theme.fontMono : "monospace";
  if (orientation === "ladder") {
    var xAnchorPos = posOffset !== undefined ? posOffset : 0.99;
    return {
      shape: {
        type: "line",
        x0: 0,
        x1: 1,
        xref: "paper",
        y0: strikeVal,
        y1: strikeVal,
        line: { color: color, width: 2, dash: dash || "dash" },
      },
      annotation: {
        x: xAnchorPos,
        xref: "paper",
        xanchor: xAnchorPos < 0.5 ? "left" : "right",
        y: strikeVal,
        yanchor: placeAbove === false ? "top" : "bottom",
        yshift: placeAbove === false ? -3 : 3,
        text: "<b>" + label + "</b>",
        showarrow: false,
        font: { size: 11, color: color, family: fontFam },
        bgcolor: bgFill,
        bordercolor: color,
        borderwidth: 1.5,
        borderpad: 3,
      },
    };
  }
  return makeVerticalMarker(strikeVal, label, color, posOffset, dash, theme);
}

export function baseLayout(t, xTitle, yTitle, markers) {
  var shapes = [];
  var annotations = [];
  if (markers && markers.length) {
    for (var i = 0; i < markers.length; i++) {
      if (markers[i] && markers[i].shape) shapes.push(markers[i].shape);
      if (markers[i] && markers[i].annotation) annotations.push(markers[i].annotation);
    }
  }
  return {
    autosize: true,
    paper_bgcolor: t.bg,
    plot_bgcolor: t.bg,
    font: { family: t.fontMono, size: 11, color: t.ink },
    margin: { t: 28, r: 22, b: 44, l: 54 },
    xaxis: {
      title: { text: xTitle, font: { size: 11, color: t.muted } },
      gridcolor: t.grid,
      zerolinecolor: t.grid,
      tickfont: { color: t.ink },
    },
    yaxis: {
      title: { text: yTitle, font: { size: 11, color: t.muted } },
      gridcolor: t.grid,
      zerolinecolor: t.grid,
      tickfont: { color: t.ink },
    },
    legend: {
      orientation: "h",
      y: 1.12,
      x: 0,
      font: { size: 10, color: t.ink },
    },
    shapes: shapes,
    annotations: annotations,
    hovermode: "x unified",
    hoverlabel: {
      align: "left",
      font: { family: t.fontMono, size: 11 },
    },
  };
}

function formatDteBucketLabel(filterVal) {
  for (var b = 0; b < DTE_BUCKET_DEFS.length; b++) {
    if (DTE_BUCKET_DEFS[b].key === filterVal) return DTE_BUCKET_DEFS[b].label;
  }
  return String(filterVal || "");
}

export function appendDteBucketOptions(selectEl, expiries, previous) {
  var matchingBuckets = [];
  for (var b = 0; b < DTE_BUCKET_DEFS.length; b++) {
    var def = DTE_BUCKET_DEFS[b];
    var cnt = 0;
    for (var e = 0; e < expiries.length; e++) {
      if (expiries[e].dte >= def.min && expiries[e].dte <= def.max) cnt += 1;
    }
    if (cnt > 0) {
      matchingBuckets.push({ def: def, cnt: cnt });
    }
  }
  if (!matchingBuckets.length) return;
  var group = document.createElement("optgroup");
  group.label = "DTE Buckets (30\u2013120d)";
  for (var m = 0; m < matchingBuckets.length; m++) {
    var item = matchingBuckets[m];
    var bOpt = document.createElement("option");
    bOpt.value = item.def.key;
    bOpt.textContent = item.def.label + " (" + item.cnt + " exp)";
    if (bOpt.value === previous) bOpt.selected = true;
    group.appendChild(bOpt);
  }
  selectEl.appendChild(group);
}

export function syncWallsSplitCheckboxState() {
  if (!dom.wallsSplitDte) return;
  var dteVal = dom.wallsDteSelect ? dom.wallsDteSelect.value : "ALL";
  var metricVal = dom.wallsMetricSelect ? dom.wallsMetricSelect.value : "auto";
  var canSplit = (dteVal === "ALL" || dteVal.indexOf("BUCKET:") === 0) && metricVal !== "overlay";
  dom.wallsSplitDte.disabled = !canSplit;
  if (dom.wallsSplitDteWrap) {
    dom.wallsSplitDteWrap.classList.toggle("is-disabled", !canSplit);
  }
}

export function populateWallsDteFilter(data) {
  if (!dom.wallsDteSelect) return;
  var rawPrev = dom.wallsDteSelect.value || "ALL";
  dom.wallsDteSelect.innerHTML = "";

  var defaultOpt = document.createElement("option");
  defaultOpt.value = "ALL";

  var expiries =
    data && data.profiles && data.profiles.expiries ? data.profiles.expiries.slice() : [];
  expiries.sort(function (a, b) {
    return a.dte - b.dte;
  });

  if (!expiries.length) {
    defaultOpt.textContent = "All DTEs";
    dom.wallsDteSelect.appendChild(defaultOpt);
    syncWallsSplitCheckboxState();
    return;
  }

  var previous = snapDteValueToExpiries(rawPrev, expiries, {
    allowBuckets: true,
    emptyValue: "ALL",
  });

  var minDte = expiries[0].dte;
  var maxDte = expiries[expiries.length - 1].dte;
  var dteSpan = minDte === maxDte ? minDte + "d" : minDte + "\u2013" + maxDte + "d";
  defaultOpt.textContent =
    "All DTEs (" + dteSpan + " \u2022 " + expiries.length + " exp)";
  dom.wallsDteSelect.appendChild(defaultOpt);

  appendDteBucketOptions(dom.wallsDteSelect, expiries, previous);

  var expGroup = document.createElement("optgroup");
  expGroup.label = "Expirations";
  for (var i = 0; i < expiries.length; i++) {
    var exp = expiries[i];
    var opt = document.createElement("option");
    opt.value = exp.expiry;
    opt.textContent = exp.dte + "d (" + formatExpiryIso(exp.expiry) + ")";
    if (opt.value === previous) {
      opt.selected = true;
    }
    expGroup.appendChild(opt);
  }
  dom.wallsDteSelect.appendChild(expGroup);
  if (previous) {
    dom.wallsDteSelect.value = previous;
  }
  syncWallsSplitCheckboxState();
}

function buildSymmetricPositiveTicks(maxLeft, maxRight) {
  var absMax = Math.max(maxLeft || 0, maxRight || 0, 1);
  var rawStep = absMax / 3;
  var mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  var norm = rawStep / mag;
  var niceNorm = 1;
  if (norm > 5) niceNorm = 10;
  else if (norm > 2.5) niceNorm = 5;
  else if (norm > 2) niceNorm = 2.5;
  else if (norm > 1) niceNorm = 2;
  var step = Math.max(1, Math.round(niceNorm * mag));

  var leftSteps = maxLeft > 0 ? Math.ceil(maxLeft / step) : 0;
  var rightSteps = maxRight > 0 ? Math.ceil(maxRight / step) : 1;
  var vals = [];
  for (var l = leftSteps; l >= 1; l--) {
    vals.push(-l * step);
  }
  vals.push(0);
  for (var r = 1; r <= rightSteps; r++) {
    vals.push(r * step);
  }
  var labels = vals.map(function (v) {
    return Math.round(Math.abs(v)).toLocaleString("en-US");
  });
  var leftBound = maxLeft > 0 ? Math.max(maxLeft * 1.14, step * 0.6) : step * 0.25;
  var rightBound = maxRight > 0 ? Math.max(maxRight * 1.14, step * 0.6) : step * 0.25;
  return {
    tickvals: vals,
    ticktext: labels,
    range: [-leftBound, rightBound],
  };
}

function resolveWallCandidate(data, activeExpiries, dteSelection, basisKey, side) {
  var isCall = side === "C";
  var dataMatchesPrimary =
    (basisKey === "oi" && data.walls.primary_basis === "OPEN_INTEREST") ||
    (basisKey === "volume" && data.walls.primary_basis === "VOLUME");

  var wallStrike = null;
  if (dataMatchesPrimary) {
    if (dteSelection === "ALL") {
      var agg = isCall
        ? data.walls.aggregate_primary_call_wall
        : data.walls.aggregate_primary_put_wall;
      wallStrike = agg ? agg.strike : null;
    } else {
      var byDte = data.walls.by_dte || [];
      for (var i = 0; i < byDte.length; i++) {
        if (byDte[i].expiry === dteSelection) {
          var bw = isCall ? byDte[i].primary_call_wall : byDte[i].primary_put_wall;
          wallStrike = bw ? bw.strike : null;
          break;
        }
      }
    }
  }

  if (wallStrike === null) {
    var totalsByStrike = {};
    for (var bIdx = 0; bIdx < activeExpiries.length; bIdx++) {
      var rows = activeExpiries[bIdx].strikes || [];
      for (var rIdx = 0; rIdx < rows.length; rIdx++) {
        var row = rows[rIdx];
        var k = Number(row.strike).toFixed(4);
        var field = isCall
          ? basisKey === "volume"
            ? "call_volume"
            : "call_oi"
          : basisKey === "volume"
          ? "put_volume"
          : "put_oi";
        totalsByStrike[k] = {
          strike: Number(row.strike),
          val: (totalsByStrike[k] ? totalsByStrike[k].val : 0) + (row[field] || 0),
        };
      }
    }
    var bestVal = 0;
    Object.keys(totalsByStrike).forEach(function (k) {
      if (totalsByStrike[k].val > bestVal) {
        bestVal = totalsByStrike[k].val;
        wallStrike = totalsByStrike[k].strike;
      }
    });
  }

  if (wallStrike === null) {
    return null;
  }

  var prefix = isCall ? "CALL WALL " : "PUT WALL ";
  if (dteSelection !== "ALL" && activeExpiries.length === 1) {
    return {
      strike: wallStrike,
      label: prefix + formatStrikeShort(wallStrike) + " (" + activeExpiries[0].dte + "d)",
    };
  }

  var fieldName = isCall
    ? basisKey === "volume"
      ? "call_volume"
      : "call_oi"
    : basisKey === "volume"
    ? "put_volume"
    : "put_oi";
  var totalAtWall = 0;
  var domDte = null;
  var domVal = -1;
  var contributingExpiries = 0;
  for (var j = 0; j < activeExpiries.length; j++) {
    var expItem = activeExpiries[j];
    var expRows = expItem.strikes || [];
    for (var m = 0; m < expRows.length; m++) {
      if (Math.abs(Number(expRows[m].strike) - wallStrike) < 1e-6) {
        var v = expRows[m][fieldName] || 0;
        if (v > 0) contributingExpiries += 1;
        totalAtWall += v;
        if (v > domVal) {
          domVal = v;
          domDte = expItem.dte;
        }
      }
    }
  }

  if (domDte === null || totalAtWall <= 0) {
    return { strike: wallStrike, label: prefix + formatStrikeShort(wallStrike) };
  }
  var sharePct = Math.round((domVal / totalAtWall) * 100);
  var dteSuffix =
    contributingExpiries <= 1 || activeExpiries.length === 1
      ? "(" + domDte + "d)"
      : "(dom. " + domDte + "d \u2022 " + sharePct + "%)";
  return {
    strike: wallStrike,
    label: prefix + formatStrikeShort(wallStrike) + " " + dteSuffix,
  };
}

/**
 * Derive true strike spacing, per-bar widths/offsets, array ticks, and padded range
 * from active plotted strikes so `$0.5/$1/$2.5/$5/$10/$25+` and mixed grids scale natively.
 */
function computeStrikeAxisGeometry(strikes, metaStrikeStep, spot, callWall, putWall) {
  var count = Array.isArray(strikes) ? strikes.length : 0;
  var fallbackStep = Number(metaStrikeStep) > 0 ? Number(metaStrikeStep) : 5;
  if (count === 0) {
    return {
      minStep: fallbackStep,
      maxStep: fallbackStep,
      modalStep: fallbackStep,
      distinctSteps: [],
      localSteps: [],
      localStepByKey: {},
      tickvals: [],
      ticktext: [],
      axisRange: null,
      stepBadgeText: formatStepShort(metaStrikeStep) + " STEP",
    };
  }

  var diffs = [];
  var minStep = Infinity;
  var maxStep = 0;
  var diffCounts = {};
  for (var i = 1; i < count; i++) {
    var rawDiff = strikes[i] - strikes[i - 1];
    if (rawDiff > 1e-6) {
      var d = Math.round(rawDiff * 10000) / 10000;
      diffs.push(d);
      if (d < minStep) minStep = d;
      if (d > maxStep) maxStep = d;
      var dKey = d.toFixed(4);
      if (!diffCounts[dKey]) {
        diffCounts[dKey] = { step: d, count: 0 };
      }
      diffCounts[dKey].count += 1;
    }
  }

  var modalStep = fallbackStep;
  var distinctSteps = [];
  if (diffs.length > 0) {
    var bestEntry = null;
    Object.keys(diffCounts).forEach(function (k) {
      var entry = diffCounts[k];
      distinctSteps.push(entry.step);
      if (
        !bestEntry ||
        entry.count > bestEntry.count ||
        (entry.count === bestEntry.count && entry.step < bestEntry.step)
      ) {
        bestEntry = entry;
      }
    });
    distinctSteps.sort(function (a, b) {
      return a - b;
    });
    if (bestEntry) modalStep = bestEntry.step;
  } else {
    minStep = fallbackStep;
    maxStep = fallbackStep;
  }

  var localSteps = [];
  var localStepByKey = {};
  if (count === 1 || diffs.length === 0) {
    localSteps.push(fallbackStep);
    localStepByKey[Number(strikes[0]).toFixed(4)] = fallbackStep;
  } else {
    var hasMixedSteps = distinctSteps.length > 1;
    var mixedCap = modalStep * 1.5;
    for (var sIdx = 0; sIdx < count; sIdx++) {
      var prevDiff = sIdx > 0 ? diffs[sIdx - 1] : diffs[0];
      var nextDiff = sIdx < count - 1 ? diffs[sIdx] : diffs[diffs.length - 1];
      var localGap = Math.min(prevDiff, nextDiff);
      var effStep = hasMixedSteps ? Math.min(localGap, mixedCap) : localGap;
      localSteps.push(effStep);
      localStepByKey[Number(strikes[sIdx]).toFixed(4)] = effStep;
    }
  }

  var tickvals = [];
  var ticktext = [];
  if (count <= 22) {
    for (var tIdx = 0; tIdx < count; tIdx++) {
      tickvals.push(strikes[tIdx]);
      ticktext.push(formatStrikeShort(strikes[tIdx]));
    }
  } else {
    var stride = Math.ceil(count / 18);
    for (var kIdx = 0; kIdx < count; kIdx += stride) {
      tickvals.push(strikes[kIdx]);
      ticktext.push(formatStrikeShort(strikes[kIdx]));
    }
  }

  var coords = [strikes[0], strikes[count - 1]];
  if (spot > 0 && !Number.isNaN(Number(spot))) coords.push(Number(spot));
  if (callWall && !Number.isNaN(Number(callWall.strike))) coords.push(Number(callWall.strike));
  if (putWall && !Number.isNaN(Number(putWall.strike))) coords.push(Number(putWall.strike));
  var minCoord = Math.min.apply(null, coords);
  var maxCoord = Math.max.apply(null, coords);
  var edgeFirst = localSteps[0] || modalStep || fallbackStep;
  var edgeLast = localSteps[localSteps.length - 1] || modalStep || fallbackStep;
  var axisRange = [minCoord - 0.6 * edgeFirst, maxCoord + 0.6 * edgeLast];

  var stepBadgeText =
    distinctSteps.length === 1
      ? formatStepShort(distinctSteps[0]) + " STEP"
      : distinctSteps.length > 1
      ? formatStepShort(minStep) + "\u2013" + formatStepShort(maxStep) + " STEP"
      : formatStepShort(metaStrikeStep) + " STEP";

  return {
    minStep: minStep,
    maxStep: maxStep,
    modalStep: modalStep,
    distinctSteps: distinctSteps,
    localSteps: localSteps,
    localStepByKey: localStepByKey,
    tickvals: tickvals,
    ticktext: ticktext,
    axisRange: axisRange,
    stepBadgeText: stepBadgeText,
  };
}

export function renderWallsChart(data, t) {
  var el = document.getElementById("chart-walls");
  if (!el || typeof window.Plotly === "undefined") return;

  var allExpiries =
    data && data.profiles && data.profiles.expiries ? data.profiles.expiries.slice() : [];
  allExpiries.sort(function (a, b) {
    return a.dte - b.dte;
  });

  var viewMode = dom.wallsViewSelect ? dom.wallsViewSelect.value : "ladder";
  var metricSel = dom.wallsMetricSelect ? dom.wallsMetricSelect.value : "auto";
  var dteSel = dom.wallsDteSelect ? dom.wallsDteSelect.value : "ALL";
  syncWallsSplitCheckboxState();

  var activeExpiries = allExpiries.filter(function (exp) {
    return matchesDteFilterValue(exp.dte, exp.expiry, dteSel);
  });

  if (!activeExpiries.length) {
    window.Plotly.purge(el);
    el.innerHTML = "";
    if (dom.wallsBasisLabel) {
      dom.wallsBasisLabel.textContent = "NO DATA";
    }
    return;
  }

  var effectiveMetric = metricSel;
  if (metricSel === "auto") {
    effectiveMetric = data.walls.primary_basis === "VOLUME" ? "volume" : "oi";
  }
  var splitByDte = Boolean(
    dom.wallsSplitDte &&
      dom.wallsSplitDte.checked &&
      (dteSel === "ALL" || dteSel.indexOf("BUCKET:") === 0) &&
      effectiveMetric !== "overlay"
  );

  var spot = Number(data.meta.underlying_price || 0);
  var strikeMap = {};
  for (var bIdx = 0; bIdx < activeExpiries.length; bIdx++) {
    var expObj = activeExpiries[bIdx];
    var bRows = expObj.strikes || [];
    for (var rIdx = 0; rIdx < bRows.length; rIdx++) {
      var row = bRows[rIdx];
      var key = Number(row.strike).toFixed(4);
      if (!strikeMap[key]) {
        strikeMap[key] = {
          strike: Number(row.strike),
          callOi: 0,
          putOi: 0,
          callVol: 0,
          putVol: 0,
          byExpiry: {},
        };
      }
      var entry = strikeMap[key];
      var cOi = row.call_oi || 0;
      var pOi = row.put_oi || 0;
      var cVol = row.call_volume || 0;
      var pVol = row.put_volume || 0;
      entry.callOi += cOi;
      entry.putOi += pOi;
      entry.callVol += cVol;
      entry.putVol += pVol;
      entry.byExpiry[expObj.expiry] = {
        dte: expObj.dte,
        expiryIso: formatExpiryIso(expObj.expiry),
        callOi: cOi,
        putOi: pOi,
        callVol: cVol,
        putVol: pVol,
      };
    }
  }

  var sorted = Object.keys(strikeMap)
    .map(function (k) {
      return strikeMap[k];
    })
    .sort(function (a, b) {
      return a.strike - b.strike;
    });

  var strikes = sorted.map(function (s) {
    return s.strike;
  });

  var wallMetricKey =
    effectiveMetric === "overlay"
      ? data.walls.primary_basis === "VOLUME"
        ? "volume"
        : "oi"
      : effectiveMetric;
  var callWall = resolveWallCandidate(data, activeExpiries, dteSel, wallMetricKey, "C");
  var putWall = resolveWallCandidate(data, activeExpiries, dteSel, wallMetricKey, "P");
  var sameWallStrike = Boolean(
    callWall && putWall && Math.abs(callWall.strike - putWall.strike) < 1e-6
  );
  var axisGeo = computeStrikeAxisGeometry(
    strikes,
    data.meta ? data.meta.strike_step : null,
    spot,
    callWall,
    putWall
  );

  var metricBadgeText =
    effectiveMetric === "overlay"
      ? "OI + VOLUME"
      : effectiveMetric === "volume"
      ? metricSel === "auto"
        ? "VOLUME [FALLBACK]"
        : "VOLUME"
      : "OPEN INTEREST";
  var viewBadgeText = viewMode === "grouped" ? "GROUPED" : "LADDER";
  var dteBadgeText = "";
  if (dteSel === "ALL" || dteSel.indexOf("BUCKET:") === 0) {
    var dteList = activeExpiries
      .map(function (exp) {
        return exp.dte + "d";
      })
      .join(", ");
    var firstExp = formatExpiryIso(activeExpiries[0].expiry);
    var lastExp = formatExpiryIso(activeExpiries[activeExpiries.length - 1].expiry);
    var expSpan = firstExp === lastExp ? firstExp : firstExp + ".." + lastExp;
    var bucketPrefix =
      dteSel.indexOf("BUCKET:") === 0 ? formatDteBucketLabel(dteSel) + ": " : "DTEs: ";
    dteBadgeText = bucketPrefix + dteList + " (" + expSpan + ")";
  } else {
    var onlyExp = activeExpiries[0];
    dteBadgeText = "DTE: " + onlyExp.dte + "d (EXP " + formatExpiryIso(onlyExp.expiry) + ")";
  }
  var rangeBadgePart =
    data.meta && data.meta.range_pct !== null && data.meta.range_pct !== undefined
      ? formatRangeShort(data.meta.range_pct)
      : "";
  var stepBadgePart = axisGeo.stepBadgeText || "";
  var scopePieces = [];
  if (rangeBadgePart) scopePieces.push(rangeBadgePart);
  if (stepBadgePart && stepBadgePart !== "\u2014 STEP") scopePieces.push(stepBadgePart);
  var pullScopeLabel = scopePieces.length ? " \u2022 " + scopePieces.join(" / ") : "";
  if (dom.wallsBasisLabel) {
    dom.wallsBasisLabel.textContent =
      viewBadgeText + " \u2022 " + metricBadgeText + pullScopeLabel + " \u2022 " + dteBadgeText;
  }

  var strikeMeta = sorted.map(function (s) {
    var distPct = spot > 0 ? ((s.strike - spot) / spot) * 100 : 0;
    var pctStr = (distPct >= 0 ? "+" : "") + distPct.toFixed(1) + "%";
    var lines = [];
    for (var bi = 0; bi < activeExpiries.length; bi++) {
      var ab = activeExpiries[bi];
      var bData = s.byExpiry[ab.expiry] || {
        dte: ab.dte,
        expiryIso: formatExpiryIso(ab.expiry),
        callOi: 0,
        putOi: 0,
        callVol: 0,
        putVol: 0,
      };
      var metricSummary =
        effectiveMetric === "volume"
          ? "C " + fmtInt(bData.callVol) + " / P " + fmtInt(bData.putVol) + " Vol"
          : effectiveMetric === "overlay"
          ? "OI C " +
            fmtInt(bData.callOi) +
            "/P " +
            fmtInt(bData.putOi) +
            " | Vol C " +
            fmtInt(bData.callVol) +
            "/P " +
            fmtInt(bData.putVol)
          : "C " + fmtInt(bData.callOi) + " / P " + fmtInt(bData.putOi) + " OI";
      lines.push("\u2022 " + bData.dte + "d (" + bData.expiryIso + "): " + metricSummary);
    }
    return {
      strikeStr: formatStrikeShort(s.strike),
      pctStr: pctStr,
      callOiStr: fmtInt(s.callOi),
      putOiStr: fmtInt(s.putOi),
      callVolStr: fmtInt(s.callVol),
      putVolStr: fmtInt(s.putVol),
      dteHtml: "<br><b>By DTE:</b><br>" + lines.join("<br>"),
    };
  });

  var isLadder = viewMode === "ladder";
  var traces = [];
  var barmode = "relative";
  var maxLeft = 0;
  var maxRight = 0;

  function buildCombinedHover(sideLabel, primaryLabel, metricKind) {
    return sorted.map(function (s, idx) {
      var m = strikeMeta[idx];
      var primaryVal =
        metricKind === "volume"
          ? sideLabel === "CALLS"
            ? m.callVolStr
            : m.putVolStr
          : sideLabel === "CALLS"
          ? m.callOiStr
          : m.putOiStr;
      var oppSide = sideLabel === "CALLS" ? "Puts" : "Calls";
      var oppVal =
        metricKind === "volume"
          ? sideLabel === "CALLS"
            ? m.putVolStr
            : m.callVolStr
          : sideLabel === "CALLS"
          ? m.putOiStr
          : m.callOiStr;
      var auxLine =
        metricKind === "volume"
          ? "Open Interest: C " + m.callOiStr + " / P " + m.putOiStr
          : "Session Volume: C " + m.callVolStr + " / P " + m.putVolStr;
      return [m.strikeStr, m.pctStr, primaryLabel, primaryVal, oppSide, oppVal, auxLine, m.dteHtml];
    });
  }

  var combinedHoverTemplate =
    "<b>%{fullData.name} \u2022 Strike %{customdata[0]} (%{customdata[1]} vs Spot)</b><br>" +
    "%{customdata[2]}: %{customdata[3]}<br>" +
    "Opposite %{customdata[4]}: %{customdata[5]}<br>" +
    "%{customdata[6]}" +
    "%{customdata[7]}<extra></extra>";

  function scaleSteps(factor) {
    return axisGeo.localSteps.map(function (stepVal) {
      return stepVal * factor;
    });
  }
  var ladderBarWidths = scaleSteps(0.78);
  var groupedSideWidths = scaleSteps(0.36);
  var groupedCallOffsets = scaleSteps(-0.38);
  var groupedPutOffsets = scaleSteps(0.02);

  if (effectiveMetric === "overlay") {
    barmode = "overlay";
    var callOiVals = sorted.map(function (s) {
      if (s.callOi > maxRight) maxRight = s.callOi;
      return s.callOi;
    });
    var callVolVals = sorted.map(function (s) {
      if (s.callVol > maxRight) maxRight = s.callVol;
      return s.callVol;
    });
    var putOiVals = sorted.map(function (s) {
      if (s.putOi > maxLeft) maxLeft = s.putOi;
      return isLadder ? -s.putOi : s.putOi;
    });
    var putVolVals = sorted.map(function (s) {
      if (s.putVol > maxLeft) maxLeft = s.putVol;
      return isLadder ? -s.putVol : s.putVol;
    });

    var overlayTemplate =
      "<b>%{fullData.name} \u2022 Strike %{customdata[0]} (%{customdata[1]} vs Spot)</b><br>" +
      "Calls \u2014 OI: %{customdata[2]} | Vol: %{customdata[3]}<br>" +
      "Puts \u2014 OI: %{customdata[4]} | Vol: %{customdata[5]}" +
      "%{customdata[6]}<extra></extra>";
    var overlayAllCustom = sorted.map(function (_s, idx) {
      var m = strikeMeta[idx];
      return [m.strikeStr, m.pctStr, m.callOiStr, m.callVolStr, m.putOiStr, m.putVolStr, m.dteHtml];
    });

    var groupedVolWidths = scaleSteps(0.2);
    var oiOptsCall = isLadder
      ? { width: ladderBarWidths, opacity: 0.34 }
      : { offset: groupedCallOffsets, width: groupedSideWidths, opacity: 0.34 };
    var volOptsCall = isLadder
      ? { width: scaleSteps(0.42), opacity: 1 }
      : { offset: scaleSteps(-0.3), width: groupedVolWidths, opacity: 1 };
    var oiOptsPut = isLadder
      ? { width: ladderBarWidths, opacity: 0.34 }
      : { offset: groupedPutOffsets, width: groupedSideWidths, opacity: 0.34 };
    var volOptsPut = isLadder
      ? { width: scaleSteps(0.42), opacity: 1 }
      : { offset: scaleSteps(0.1), width: groupedVolWidths, opacity: 1 };

    function makeOverlayTrace(name, vals, color, opts) {
      var tr = Object.assign(
        {
          type: "bar",
          name: name,
          marker: { color: color },
          customdata: overlayAllCustom,
          hovertemplate: overlayTemplate,
        },
        opts
      );
      if (isLadder) {
        tr.orientation = "h";
        tr.y = strikes;
        tr.x = vals;
      } else {
        tr.x = strikes;
        tr.y = vals;
      }
      return tr;
    }

    traces.push(
      makeOverlayTrace("Calls OI", callOiVals, t.call, oiOptsCall),
      makeOverlayTrace("Calls Vol", callVolVals, t.call, volOptsCall),
      makeOverlayTrace("Puts OI", putOiVals, t.put, oiOptsPut),
      makeOverlayTrace("Puts Vol", putVolVals, t.put, volOptsPut)
    );
  } else if (splitByDte) {
    barmode = "relative";
    var metricFieldPrefix = effectiveMetric === "volume" ? "Vol" : "OI";
    var splitHoverTemplate =
      "<b>%{fullData.name} \u2022 Strike %{customdata[0]} (%{customdata[1]} vs Spot)</b><br>" +
      "%{customdata[2]}: %{customdata[3]}<br>" +
      "All DTEs Total: %{customdata[4]}" +
      "%{customdata[5]}<extra></extra>";

    sorted.forEach(function (s) {
      var cTotal = effectiveMetric === "volume" ? s.callVol : s.callOi;
      var pTotal = effectiveMetric === "volume" ? s.putVol : s.putOi;
      if (cTotal > maxRight) maxRight = cTotal;
      if (pTotal > maxLeft) maxLeft = pTotal;
    });

    for (var bi = 0; bi < activeExpiries.length; bi++) {
      var bObj = activeExpiries[bi];
      var bKey = bObj.expiry;
      var expLabel = formatExpiryIso(bObj.expiry);
      var callColor = t.callDteShades[bi % t.callDteShades.length];
      var putColor = t.putDteShades[bi % t.putDteShades.length];

      var callSlice = [];
      var putSlice = [];
      var callCustom = [];
      var putCustom = [];

      for (var si = 0; si < sorted.length; si++) {
        var sRow = sorted[si];
        var sm = strikeMeta[si];
        var bInfo = sRow.byExpiry[bKey] || { callOi: 0, putOi: 0, callVol: 0, putVol: 0 };
        var cVal = effectiveMetric === "volume" ? bInfo.callVol : bInfo.callOi;
        var pVal = effectiveMetric === "volume" ? bInfo.putVol : bInfo.putOi;
        var totalLine =
          effectiveMetric === "volume"
            ? "Call Vol " + sm.callVolStr + " / Put Vol " + sm.putVolStr
            : "Call OI " + sm.callOiStr + " / Put OI " + sm.putOiStr;

        callSlice.push(cVal);
        putSlice.push(isLadder ? -pVal : pVal);
        callCustom.push([
          sm.strikeStr,
          sm.pctStr,
          bObj.dte + "d (" + expLabel + ") Call " + metricFieldPrefix,
          fmtInt(cVal),
          totalLine,
          sm.dteHtml,
        ]);
        putCustom.push([
          sm.strikeStr,
          sm.pctStr,
          bObj.dte + "d (" + expLabel + ") Put " + metricFieldPrefix,
          fmtInt(pVal),
          totalLine,
          sm.dteHtml,
        ]);
      }

      var callSplitTrace = {
        type: "bar",
        orientation: isLadder ? "h" : "v",
        name: "C " + bObj.dte + "d",
        x: isLadder ? callSlice : strikes,
        y: isLadder ? strikes : callSlice,
        offsetgroup: isLadder ? undefined : "calls",
        width: isLadder ? ladderBarWidths : groupedSideWidths,
        marker: { color: callColor },
        customdata: callCustom,
        hovertemplate: splitHoverTemplate,
      };
      var putSplitTrace = {
        type: "bar",
        orientation: isLadder ? "h" : "v",
        name: "P " + bObj.dte + "d",
        x: isLadder ? putSlice : strikes,
        y: isLadder ? strikes : putSlice,
        offsetgroup: isLadder ? undefined : "puts",
        width: isLadder ? ladderBarWidths : groupedSideWidths,
        marker: { color: putColor },
        customdata: putCustom,
        hovertemplate: splitHoverTemplate,
      };
      if (!isLadder) {
        callSplitTrace.offset = groupedCallOffsets;
        putSplitTrace.offset = groupedPutOffsets;
      }
      traces.push(callSplitTrace, putSplitTrace);
    }
  } else {
    barmode = isLadder ? "relative" : "group";
    var callTitle = effectiveMetric === "volume" ? "Call Volume" : "Call Open Interest";
    var putTitle = effectiveMetric === "volume" ? "Put Volume" : "Put Open Interest";
    var callVals = sorted.map(function (s) {
      var v = effectiveMetric === "volume" ? s.callVol : s.callOi;
      if (v > maxRight) maxRight = v;
      return v;
    });
    var putVals = sorted.map(function (s) {
      var v = effectiveMetric === "volume" ? s.putVol : s.putOi;
      if (v > maxLeft) maxLeft = v;
      return isLadder ? -v : v;
    });
    var customC = buildCombinedHover("CALLS", callTitle, effectiveMetric);
    var customP = buildCombinedHover("PUTS", putTitle, effectiveMetric);

    var callStdTrace = {
      type: "bar",
      orientation: isLadder ? "h" : "v",
      name: "Calls",
      x: isLadder ? callVals : strikes,
      y: isLadder ? strikes : callVals,
      width: isLadder ? ladderBarWidths : groupedSideWidths,
      marker: { color: t.call },
      customdata: customC,
      hovertemplate: combinedHoverTemplate,
    };
    var putStdTrace = {
      type: "bar",
      orientation: isLadder ? "h" : "v",
      name: "Puts",
      x: isLadder ? putVals : strikes,
      y: isLadder ? strikes : putVals,
      width: isLadder ? ladderBarWidths : groupedSideWidths,
      marker: { color: t.put },
      customdata: customP,
      hovertemplate: combinedHoverTemplate,
    };
    if (!isLadder) {
      callStdTrace.offset = groupedCallOffsets;
      putStdTrace.offset = groupedPutOffsets;
    }
    traces.push(callStdTrace, putStdTrace);
  }

  var showAnomalies = Boolean(dom.wallsShowAnomalies && dom.wallsShowAnomalies.checked);
  var anomTotalCount = 0;
  if (showAnomalies && data.anomalies && data.anomalies.anomalies) {
    var scopedAnoms = data.anomalies.anomalies.filter(function (a) {
      return dteSel === "ALL" || a.expiry === dteSel;
    });
    anomTotalCount = scopedAnoms.length;

    if (scopedAnoms.length > 0) {
      var strikeLookup = {};
      sorted.forEach(function (s) {
        var callLen =
          effectiveMetric === "overlay"
            ? Math.max(s.callOi, s.callVol)
            : effectiveMetric === "volume"
            ? s.callVol
            : s.callOi;
        var putLen =
          effectiveMetric === "overlay"
            ? Math.max(s.putOi, s.putVol)
            : effectiveMetric === "volume"
            ? s.putVol
            : s.putOi;
        strikeLookup[Number(s.strike).toFixed(4)] = { callLen: callLen, putLen: putLen };
      });

      var groupedAnoms = {};
      for (var ai = 0; ai < scopedAnoms.length; ai++) {
        var a = scopedAnoms[ai];
        var gKey = Number(a.strike).toFixed(4) + "|" + a.right;
        if (!groupedAnoms[gKey]) {
          groupedAnoms[gKey] = { strike: Number(a.strike), right: a.right, items: [] };
        }
        groupedAnoms[gKey].items.push(a);
      }

      var tipPad = Math.max(maxRight, maxLeft, 10) * 0.055;
      var anomX = [];
      var anomY = [];
      var anomText = [];

      Object.keys(groupedAnoms).forEach(function (gKey) {
        var grp = groupedAnoms[gKey];
        var sInfo = strikeLookup[Number(grp.strike).toFixed(4)];
        if (!sInfo) return;
        var isCall = grp.right === "C";
        var barBase = isCall ? sInfo.callLen : sInfo.putLen;
        var tipVal = barBase + tipPad;

        var lines = grp.items.map(function (item) {
          var flags = [];
          if (item.is_vol_gt_oi) flags.push("VOL>OI");
          if (item.is_high_vol_zscore) flags.push("Z-SCORE");
          var ratioTxt =
            item.vol_oi_ratio !== null && item.vol_oi_ratio !== undefined
              ? Number(item.vol_oi_ratio).toFixed(2) + "x"
              : "—";
          return (
            "\u2022 " +
            item.dte +
            "d (" +
            formatExpiryIso(item.expiry) +
            "): Vol " +
            fmtInt(item.volume) +
            " / OI " +
            fmtInt(item.open_interest) +
            " (" +
            ratioTxt +
            ") | Z=" +
            fmtNum(item.volume_zscore, 2) +
            " [" +
            flags.join(", ") +
            "]"
          );
        });

        anomText.push(
          "<b>UNUSUAL " +
            (isCall ? "CALL" : "PUT") +
            " ACTIVITY \u2022 Strike " +
            formatStrikeShort(grp.strike) +
            "</b><br>" +
            lines.join("<br>")
        );

        if (isLadder) {
          anomY.push(grp.strike);
          if (isCall) {
            anomX.push(tipVal);
            if (tipVal > maxRight) maxRight = tipVal;
          } else {
            anomX.push(-tipVal);
            if (tipVal > maxLeft) maxLeft = tipVal;
          }
        } else {
          var anomStrikeKey = Number(grp.strike).toFixed(4);
          var strikeLocalStep =
            axisGeo.localStepByKey[anomStrikeKey] !== undefined
              ? axisGeo.localStepByKey[anomStrikeKey]
              : axisGeo.modalStep;
          var sideOffset = strikeLocalStep * 0.2;
          anomX.push(isCall ? grp.strike - sideOffset : grp.strike + sideOffset);
          anomY.push(tipVal);
        }
      });

      if (anomX.length > 0) {
        traces.push({
          type: "scatter",
          mode: "markers",
          name: "Anomalies (Vol>OI / Z)",
          x: anomX,
          y: anomY,
          text: anomText,
          hoverinfo: "text",
          marker: {
            color: t.anomaly,
            size: 10,
            symbol: "diamond",
            line: { width: 1.5, color: t.ink },
          },
        });
      }
    }
  }

  if (dom.wallsBasisLabel && showAnomalies) {
    dom.wallsBasisLabel.textContent += " \u2022 ANOMALIES: " + anomTotalCount;
  }

  var markers = [
    makeWallReferenceMarker(
      viewMode,
      spot,
      "SPOT " + formatStrikeShort(spot),
      t.ink,
      isLadder ? 0.01 : 0.99,
      "dash",
      true,
      t
    ),
  ];
  if (callWall) {
    markers.push(
      makeWallReferenceMarker(
        viewMode,
        callWall.strike,
        callWall.label,
        t.call,
        isLadder ? 0.99 : 0.88,
        "dot",
        true,
        t
      )
    );
  }
  if (putWall) {
    markers.push(
      makeWallReferenceMarker(
        viewMode,
        putWall.strike,
        putWall.label,
        t.put,
        isLadder ? 0.99 : 0.14,
        "dot",
        sameWallStrike ? false : true,
        t
      )
    );
  }

  var countAxisTitle =
    effectiveMetric === "overlay"
      ? "Contracts (OI Backdrop + Session Vol)"
      : effectiveMetric === "volume"
      ? "Session Volume (Contracts)"
      : "Open Interest (Contracts)";
  var layout = isLadder
    ? baseLayout(t, "\u2190 Puts  |  " + countAxisTitle + "  |  Calls \u2192", "Strike ($)", markers)
    : baseLayout(t, "Strike ($)", countAxisTitle, markers);
  layout.barmode = barmode;
  layout.hovermode = "closest";

  var hasDecimalStrikeTicks = axisGeo.ticktext.some(function (lbl) {
    return lbl.indexOf(".") !== -1;
  });
  if (isLadder) {
    var tickCfg = buildSymmetricPositiveTicks(maxLeft, maxRight);
    layout.margin.l = hasDecimalStrikeTicks ? 68 : 58;
    layout.xaxis.tickmode = "array";
    layout.xaxis.tickvals = tickCfg.tickvals;
    layout.xaxis.ticktext = tickCfg.ticktext;
    layout.xaxis.range = tickCfg.range;
    layout.xaxis.zeroline = true;
    layout.xaxis.zerolinewidth = 1.5;
    layout.xaxis.zerolinecolor = t.ink;
    layout.yaxis.tickmode = "array";
    layout.yaxis.tickvals = axisGeo.tickvals;
    layout.yaxis.ticktext = axisGeo.ticktext;
    layout.yaxis.tickprefix = "";
    if (axisGeo.axisRange) {
      layout.yaxis.range = axisGeo.axisRange;
    }
  } else {
    layout.xaxis.tickmode = "array";
    layout.xaxis.tickvals = axisGeo.tickvals;
    layout.xaxis.ticktext = axisGeo.ticktext;
    layout.xaxis.tickprefix = "";
    if (axisGeo.axisRange) {
      layout.xaxis.range = axisGeo.axisRange;
    }
    layout.yaxis.rangemode = "nonnegative";
  }

  window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
}

export function renderIvSmileChart(data, t) {
  var el = document.getElementById("chart-iv-smile");
  if (!el || typeof window.Plotly === "undefined") return;

  var expiries = data.profiles.expiries || [];
  var traces = [];

  for (var i = 0; i < expiries.length; i++) {
    var b = expiries[i];
    var xs = [];
    var ys = [];
    var rows = b.strikes || [];
    for (var j = 0; j < rows.length; j++) {
      var r = rows[j];
      var ivVals = [];
      if (r.call_iv !== null && r.call_iv !== undefined) ivVals.push(r.call_iv);
      if (r.put_iv !== null && r.put_iv !== undefined) ivVals.push(r.put_iv);
      if (ivVals.length > 0) {
        var meanIv =
          ivVals.reduce(function (acc, v) {
            return acc + v;
          }, 0) / ivVals.length;
        xs.push(r.strike);
        ys.push(Number((meanIv * 100).toFixed(2)));
      }
    }
    traces.push({
      type: "scatter",
      mode: "lines+markers",
      name: b.dte + "d (" + formatExpiryIso(b.expiry) + ")",
      x: xs,
      y: ys,
      line: { color: t.dtePalette[i % t.dtePalette.length], width: 2 },
      marker: { size: 5 },
    });
  }

  var markers = [
    makeVerticalMarker(data.meta.underlying_price, "SPOT", t.ink, 0.99, "dash", t),
  ];
  var layout = baseLayout(t, "Strike ($)", "Implied Volatility (%)", markers);
  window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
}

export function renderAnomaliesChart(data, t) {
  var el = document.getElementById("chart-anomalies");
  if (!el || typeof window.Plotly === "undefined") return;

  var anomalyMap = {};
  var anoms = data.anomalies.anomalies || [];
  for (var i = 0; i < anoms.length; i++) {
    var a = anoms[i];
    anomalyMap[a.option_conid + "|" + a.expiry + "|" + a.strike + "|" + a.right] = a;
  }

  var normalX = [];
  var normalY = [];
  var normalText = [];
  var anomX = [];
  var anomY = [];
  var anomText = [];
  var anomSizes = [];

  var contracts = data.contracts || [];
  for (var j = 0; j < contracts.length; j++) {
    var c = contracts[j];
    var vol = c.volume !== null && c.volume !== undefined ? Number(c.volume) : 0;
    var oi = c.open_interest !== null && c.open_interest !== undefined ? Number(c.open_interest) : 0;
    var ratio = oi > 0 ? vol / oi : vol > 0 ? vol : 0;
    var key = c.option_conid + "|" + c.expiry + "|" + c.strike + "|" + c.right;
    var flag = anomalyMap[key];
    var label =
      c.expiry +
      " $" +
      c.strike +
      " " +
      c.right +
      " | Vol: " +
      vol +
      " / OI: " +
      oi +
      " (" +
      ratio.toFixed(2) +
      "x)";
    if (flag) {
      anomX.push(c.strike);
      anomY.push(vol);
      anomText.push(label + " | Z=" + fmtNum(flag.volume_zscore, 2));
      anomSizes.push(Math.min(22, Math.max(9, 8 + ratio * 2)));
    } else if (vol > 0) {
      normalX.push(c.strike);
      normalY.push(vol);
      normalText.push(label);
    }
  }

  var traces = [
    {
      type: "scatter",
      mode: "markers",
      name: "Standard Volume",
      x: normalX,
      y: normalY,
      text: normalText,
      hoverinfo: "text",
      marker: { color: t.muted, size: 6, opacity: 0.55 },
    },
    {
      type: "scatter",
      mode: "markers",
      name: "Unusual (Vol>OI / Z-Score)",
      x: anomX,
      y: anomY,
      text: anomText,
      hoverinfo: "text",
      marker: {
        color: t.anomaly,
        size: anomSizes.length ? anomSizes : 10,
        symbol: "diamond",
        line: { width: 1, color: t.ink },
      },
    },
  ];

  var markers = [
    makeVerticalMarker(data.meta.underlying_price, "SPOT", t.ink, 0.99, "dash", t),
  ];
  var layout = baseLayout(t, "Strike ($)", "Contract Volume", markers);
  layout.hovermode = "closest";
  window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
}

export function renderGreeksChart(data, t) {
  var el = document.getElementById("chart-greeks");
  if (!el || typeof window.Plotly === "undefined") return;

  var metric = dom.greeksMetricSelect ? dom.greeksMetricSelect.value : "gamma";
  var expiries = data.profiles.expiries || [];
  var traces = [];

  for (var i = 0; i < expiries.length; i++) {
    var b = expiries[i];
    var xs = [];
    var ys = [];
    var rows = b.strikes || [];
    for (var j = 0; j < rows.length; j++) {
      var r = rows[j];
      var callVal = r["call_" + metric];
      var putVal = r["put_" + metric];
      var combined = null;
      if (callVal !== null && callVal !== undefined && putVal !== null && putVal !== undefined) {
        combined = metric === "delta" ? (callVal + putVal) / 2 : (callVal + putVal) / 2;
      } else if (callVal !== null && callVal !== undefined) {
        combined = callVal;
      } else if (putVal !== null && putVal !== undefined) {
        combined = putVal;
      }
      if (combined !== null) {
        xs.push(r.strike);
        ys.push(combined);
      }
    }
    traces.push({
      type: "scatter",
      mode: "lines+markers",
      name: b.dte + "d (" + formatExpiryIso(b.expiry) + ")",
      x: xs,
      y: ys,
      line: { color: t.dtePalette[i % t.dtePalette.length], width: 2 },
      marker: { size: 4 },
    });
  }

  var markers = [
    makeVerticalMarker(data.meta.underlying_price, "SPOT", t.ink, 0.99, "dash", t),
  ];
  var yTitle = "Avg Call/Put " + metric.toUpperCase();
  var layout = baseLayout(t, "Strike ($)", yTitle, markers);
  window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
}

export function restoreWallsPreferences() {
  var savedView = readPref("ibkr_walls_view", "ladder");
  if (dom.wallsViewSelect && (savedView === "ladder" || savedView === "grouped")) {
    dom.wallsViewSelect.value = savedView;
  }
  var savedMetric = readPref("ibkr_walls_metric", "auto");
  if (
    dom.wallsMetricSelect &&
    (savedMetric === "auto" ||
      savedMetric === "oi" ||
      savedMetric === "volume" ||
      savedMetric === "overlay")
  ) {
    dom.wallsMetricSelect.value = savedMetric;
  }
  var savedSplit = readPref("ibkr_walls_split_dte", "false");
  if (dom.wallsSplitDte) {
    dom.wallsSplitDte.checked = savedSplit === "true";
  }
  var savedAnoms = readPref("ibkr_walls_show_anomalies", "true");
  if (dom.wallsShowAnomalies) {
    dom.wallsShowAnomalies.checked = savedAnoms !== "false";
  }
  syncWallsSplitCheckboxState();
}
