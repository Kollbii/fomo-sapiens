/* ==========================================================================
   IBKR Options Analyzer — Interactive Multi-DTE Dashboard Controller
   ========================================================================== */

(function () {
  "use strict";

  var CHART_IDS = [
    "chart-walls",
    "chart-iv-smile",
    "chart-anomalies",
    "chart-greeks",
  ];

  var bootstrapEl = document.getElementById("app-bootstrap");
  var bootstrap = bootstrapEl ? JSON.parse(bootstrapEl.textContent || "{}") : {};

  var state = {
    ticker: bootstrap.defaultTicker || "NVDA",
    snapshotId: "",
    watchlist: bootstrap.watchlist || ["NVDA", "AVGO", "CRDO", "PLTR"],
    chainData: null,
    snapshots: [],
    status: null,
    loading: false,
  };

  var dom = {
    gatewayBadge: document.getElementById("gateway-badge"),
    mdTypeBadge: document.getElementById("md-type-badge"),
    greeksBadge: document.getElementById("greeks-source-badge"),
    themeToggle: document.getElementById("theme-toggle"),
    editorialBannerCopy: document.getElementById("editorial-banner-copy"),
    watchlistPills: document.getElementById("watchlist-pills"),
    customForm: document.getElementById("custom-ticker-form"),
    customInput: document.getElementById("custom-ticker-input"),
    rangeSelect: document.getElementById("range-pct-select"),
    stepSelect: document.getElementById("strike-step-select"),
    snapshotSelect: document.getElementById("snapshot-select"),
    refreshBtn: document.getElementById("refresh-btn"),
    exportCsvBtn: document.getElementById("export-csv-btn"),
    alertBanner: document.getElementById("alert-banner"),
    kpiSpot: document.getElementById("kpi-spot"),
    kpiCallWall: document.getElementById("kpi-call-wall"),
    kpiPutWall: document.getElementById("kpi-put-wall"),
    kpiAnomalies: document.getElementById("kpi-anomalies"),
    wallsBasisLabel: document.getElementById("walls-basis-label"),
    wallsViewSelect: document.getElementById("walls-view-select"),
    wallsMetricSelect: document.getElementById("walls-metric-select"),
    wallsDteSelect: document.getElementById("walls-dte-select"),
    wallsSplitDte: document.getElementById("walls-split-dte"),
    wallsSplitDteWrap: document.getElementById("walls-split-dte-wrap"),
    wallsShowAnomalies: document.getElementById("walls-show-anomalies"),
    wallsCopyAiBtn: document.getElementById("walls-copy-ai-btn"),
    greeksMetricSelect: document.getElementById("greeks-metric-select"),
    filterDte: document.getElementById("filter-dte"),
    filterRight: document.getElementById("filter-right"),
    filterUnusual: document.getElementById("filter-unusual"),
    tableBody: document.getElementById("chain-table-body"),
    tableRowCount: document.getElementById("table-row-count"),
  };

  // ------------------------------------------------------------------ Helpers
  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") === "terminal"
      ? "terminal"
      : "print";
  }

  function themeVars() {
    var styles = window.getComputedStyle(document.documentElement);
    function cssVar(name, fallback) {
      var val = styles.getPropertyValue(name);
      return val ? val.trim() : fallback;
    }
    var isTerminal = currentTheme() === "terminal";
    return {
      isTerminal: isTerminal,
      bg: cssVar("--plotly-bg", isTerminal ? "#11161F" : "#FFFFFF"),
      grid: cssVar("--plotly-grid", isTerminal ? "#1E2636" : "#E8E4DA"),
      ink: cssVar("--plotly-ink", isTerminal ? "#EAECEF" : "#111111"),
      muted: cssVar("--text-muted", isTerminal ? "#6C7687" : "#727272"),
      call: cssVar("--accent-call", isTerminal ? "#10B981" : "#0F6E4F"),
      put: cssVar("--accent-put", isTerminal ? "#EF4444" : "#A82020"),
      anomaly: cssVar("--accent-anomaly", isTerminal ? "#FB923C" : "#C2410C"),
      warn: cssVar("--accent-warn", isTerminal ? "#F59E0B" : "#B45309"),
      fontMono: cssVar("--font-mono", "monospace"),
      dtePalette: isTerminal
        ? ["#38BDF8", "#10B981", "#F59E0B", "#A78BFA", "#F472B6"]
        : ["#1D4ED8", "#0F6E4F", "#B45309", "#6D28D9", "#A82020"],
      callDteShades: isTerminal
        ? ["#059669", "#10B981", "#34D399", "#6EE7B7", "#A7F3D0"]
        : ["#0B4F38", "#0F6E4F", "#1B8A64", "#3FA682", "#72C0A4"],
      putDteShades: isTerminal
        ? ["#DC2626", "#EF4444", "#F87171", "#FCA5A5", "#FECACA"]
        : ["#7F1616", "#A82020", "#C53B3B", "#DC6363", "#EA9292"],
    };
  }

  function readPref(key, fallback) {
    try {
      var val = window.localStorage.getItem(key);
      return val !== null ? val : fallback;
    } catch (_err) {
      return fallback;
    }
  }

  function writePref(key, value) {
    try {
      window.localStorage.setItem(key, String(value));
    } catch (_err) {
      /* ignore */
    }
  }

  function formatExpiryIso(rawExpiry) {
    var s = String(rawExpiry || "").trim();
    if (/^\d{8}$/.test(s)) {
      return s.slice(0, 4) + "-" + s.slice(4, 6) + "-" + s.slice(6, 8);
    }
    return s;
  }

  function formatStrikeShort(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return "—";
    }
    var n = Number(val);
    if (Math.abs(n - Math.round(n)) < 1e-6) {
      return "$" + Math.round(n).toString();
    }
    return "$" + n.toFixed(2);
  }

  function fmtNum(val, decimals) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return "—";
    }
    return Number(val).toFixed(decimals);
  }

  function fmtInt(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return "—";
    }
    return Math.round(Number(val)).toLocaleString("en-US");
  }

  function fmtMoney(val) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return "—";
    }
    return "$" + Number(val).toFixed(2);
  }

  function fmtPct(val, decimals) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) {
      return "—";
    }
    return (Number(val) * 100).toFixed(decimals === undefined ? 1 : decimals) + "%";
  }

  function formatStepShort(stepVal) {
    if (stepVal === null || stepVal === undefined || Number.isNaN(Number(stepVal))) {
      return "—";
    }
    var n = Number(stepVal);
    return Math.abs(n - Math.round(n)) < 1e-6 ? "$" + Math.round(n) : "$" + n;
  }

  function formatRangeShort(rangeVal) {
    if (rangeVal === null || rangeVal === undefined || Number.isNaN(Number(rangeVal))) {
      return "—";
    }
    return "\u00B1" + Math.round(Number(rangeVal) * 100) + "%";
  }

  function formatPullScopeSummary(meta) {
    if (!meta) return "Range \u2014 \u2022 Step \u2014";
    var rangeTxt =
      meta.range_pct !== null && meta.range_pct !== undefined
        ? formatRangeShort(meta.range_pct)
        : "\u2014";
    var stepTxt =
      meta.strike_step !== null && meta.strike_step !== undefined
        ? formatStepShort(meta.strike_step) + " step"
        : "\u2014 step";
    if (
      meta.requested_strike_step !== null &&
      meta.requested_strike_step !== undefined &&
      meta.strike_step !== null &&
      meta.strike_step !== undefined &&
      Math.abs(Number(meta.requested_strike_step) - Number(meta.strike_step)) > 1e-4
    ) {
      stepTxt += " (req " + formatStepShort(meta.requested_strike_step) + ")";
    }
    return rangeTxt + " \u2022 " + stepTxt;
  }

  function syncSelectToNumeric(selectEl, targetVal, labelFn) {
    if (!selectEl) return;
    var stale = selectEl.querySelectorAll('option[data-pulled-temp="true"]');
    for (var s = 0; s < stale.length; s++) {
      stale[s].parentNode.removeChild(stale[s]);
    }
    if (targetVal === null || targetVal === undefined || Number.isNaN(Number(targetVal))) {
      return;
    }
    var numTarget = Number(targetVal);
    for (var i = 0; i < selectEl.options.length; i++) {
      var opt = selectEl.options[i];
      if (opt.value === "auto") continue;
      if (Math.abs(Number(opt.value) - numTarget) < 1e-4) {
        selectEl.value = opt.value;
        return;
      }
    }
    var customOpt = document.createElement("option");
    customOpt.value = String(numTarget);
    customOpt.textContent = labelFn(numTarget) + " (pulled)";
    customOpt.setAttribute("data-pulled-temp", "true");
    customOpt.selected = true;
    selectEl.appendChild(customOpt);
  }

  function syncControlsFromMeta(meta) {
    if (!meta) return;
    syncSelectToNumeric(dom.rangeSelect, meta.range_pct, formatRangeShort);
    syncSelectToNumeric(dom.stepSelect, meta.strike_step, formatStepShort);
  }

  function escapeHtml(raw) {
    return String(raw === null || raw === undefined ? "" : raw)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showBanner(kind, message) {
    if (!dom.alertBanner) return;
    if (!message) {
      dom.alertBanner.hidden = true;
      dom.alertBanner.textContent = "";
      return;
    }
    dom.alertBanner.className = "alert-banner alert-banner--" + (kind || "info");
    dom.alertBanner.textContent = message;
    dom.alertBanner.hidden = false;
  }

  function setActiveTickerUI(ticker) {
    if (dom.watchlistPills) {
      var pills = dom.watchlistPills.querySelectorAll(".pill-btn");
      for (var i = 0; i < pills.length; i++) {
        var btn = pills[i];
        btn.classList.toggle("active", btn.getAttribute("data-ticker") === ticker);
      }
    }
    if (dom.customInput && state.watchlist.indexOf(ticker) === -1) {
      dom.customInput.value = ticker;
    }
    updateExportLink();
  }

  function updateExportLink() {
    if (!dom.exportCsvBtn) return;
    var url = "/api/csv/" + encodeURIComponent(state.ticker);
    if (state.snapshotId) {
      url += "?snapshot_id=" + encodeURIComponent(state.snapshotId);
    }
    dom.exportCsvBtn.setAttribute("href", url);
  }

  function marketDataLabel(mdType) {
    if (mdType === 1) return "TYPE 1 LIVE";
    if (mdType === 4) return "TYPE 4 DELAYED-FROZEN";
    if (mdType === 2) return "TYPE 2 FROZEN";
    return "TYPE 3 DELAYED";
  }

  function updateThemeToggleLabel() {
    if (!dom.themeToggle) return;
    dom.themeToggle.textContent =
      currentTheme() === "terminal" ? "PRINT EDITION" : "TERMINAL THEME";
  }

  // ------------------------------------------------------------------ Badges
  function renderStatusBadges(statusPayload) {
    state.status = statusPayload;
    if (dom.gatewayBadge) {
      var connected = Boolean(statusPayload && statusPayload.connected);
      dom.gatewayBadge.className = "badge " + (connected ? "badge--online" : "badge--offline");
      dom.gatewayBadge.innerHTML =
        '<span class="badge-dot" aria-hidden="true"></span><span>' +
        (connected ? "GATEWAY ONLINE" : "GATEWAY OFFLINE (CACHE ONLY)") +
        "</span>";
    }
    if (dom.mdTypeBadge && statusPayload) {
      var md = statusPayload.active_market_data_type || statusPayload.default_market_data_type || 3;
      dom.mdTypeBadge.textContent = marketDataLabel(md);
    }
  }

  function renderChainBadges(meta) {
    if (dom.mdTypeBadge && meta) {
      dom.mdTypeBadge.textContent = marketDataLabel(meta.market_data_type);
    }
    if (dom.editorialBannerCopy) {
      if (!meta) {
        dom.editorialBannerCopy.textContent =
          "Immutable CSV Archive \u2022 Delayed Type 3/4 Feed \u2022 Weekly Expirations (>30D & <60D)";
      } else {
        var dteSpanTxt =
          meta.dtes && meta.dtes.length
            ? meta.dtes[0] +
              "\u2013" +
              meta.dtes[meta.dtes.length - 1] +
              "d (" +
              meta.dtes.length +
              " wk exp)"
            : ">30D & <60D";
        dom.editorialBannerCopy.textContent =
          meta.underlying +
          " (" +
          meta.market_date +
          ") \u2022 Pulled Scope: " +
          formatPullScopeSummary(meta) +
          " \u2022 Expirations: " +
          dteSpanTxt;
      }
    }
    if (dom.greeksBadge) {
      if (!meta || !meta.greeks_source_counts) {
        dom.greeksBadge.textContent = "GREEKS: —";
        return;
      }
      var counts = meta.greeks_source_counts;
      var parts = [];
      if (counts.IBKR_MODEL) parts.push("IBKR:" + counts.IBKR_MODEL);
      if (counts.BSM_FALLBACK) parts.push("BSM:" + counts.BSM_FALLBACK);
      if (counts.NONE) parts.push("NONE:" + counts.NONE);
      dom.greeksBadge.textContent = "GREEKS: " + (parts.length ? parts.join(" / ") : "—");
    }
  }

  // ------------------------------------------------------------------ KPI Strip
  function setKpi(cardEl, valueText, subText) {
    if (!cardEl) return;
    var valEl = cardEl.querySelector(".kpi-value");
    var subEl = cardEl.querySelector(".kpi-sub");
    if (valEl) valEl.textContent = valueText;
    if (subEl) subEl.textContent = subText;
  }

  function renderKpis(data) {
    if (!data) {
      setKpi(dom.kpiSpot, "—", "No cached snapshot for " + state.ticker);
      setKpi(dom.kpiCallWall, "—", "Click REFRESH FROM IBKR");
      setKpi(dom.kpiPutWall, "—", "Click REFRESH FROM IBKR");
      setKpi(dom.kpiAnomalies, "—", "0 flagged contracts");
      return;
    }

    var meta = data.meta;
    var walls = data.walls;
    var anomalies = data.anomalies;

    setKpi(
      dom.kpiSpot,
      fmtMoney(meta.underlying_price),
      meta.underlying +
        " \u2022 " +
        meta.source +
        " \u2022 " +
        meta.contract_count +
        "c \u2022 " +
        formatPullScopeSummary(meta) +
        " \u2022 " +
        meta.market_date
    );

    var cw = walls.aggregate_primary_call_wall;
    if (cw) {
      var cwSign = cw.distance_from_spot_pct >= 0 ? "+" : "";
      setKpi(
        dom.kpiCallWall,
        "$" + fmtNum(cw.strike, 2),
        walls.primary_basis +
          ": " +
          fmtInt(cw.metric_value) +
          " (" +
          cwSign +
          fmtNum(cw.distance_from_spot_pct, 1) +
          "% vs spot)"
      );
    } else {
      setKpi(dom.kpiCallWall, "—", "Insufficient Call " + walls.primary_basis);
    }

    var pw = walls.aggregate_primary_put_wall;
    if (pw) {
      var pwSign = pw.distance_from_spot_pct >= 0 ? "+" : "";
      setKpi(
        dom.kpiPutWall,
        "$" + fmtNum(pw.strike, 2),
        walls.primary_basis +
          ": " +
          fmtInt(pw.metric_value) +
          " (" +
          pwSign +
          fmtNum(pw.distance_from_spot_pct, 1) +
          "% vs spot)"
      );
    } else {
      setKpi(dom.kpiPutWall, "—", "Insufficient Put " + walls.primary_basis);
    }

    setKpi(
      dom.kpiAnomalies,
      String(anomalies.total_flagged),
      anomalies.vol_gt_oi_count +
        " Vol>OI \u2022 " +
        anomalies.high_zscore_count +
        " High Z-score"
    );
  }

  // ------------------------------------------------------------------ Plotly Charts
  function purgeCharts() {
    if (typeof window.Plotly === "undefined") return;
    for (var i = 0; i < CHART_IDS.length; i++) {
      var el = document.getElementById(CHART_IDS[i]);
      if (el) {
        window.Plotly.purge(el);
        el.innerHTML = "";
      }
    }
    if (dom.wallsBasisLabel) {
      dom.wallsBasisLabel.textContent = "NO DATA";
    }
  }

  function makeVerticalMarker(xVal, label, color, yPos, dash, theme) {
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

  function baseLayout(t, xTitle, yTitle, markers) {
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

  function syncWallsSplitCheckboxState() {
    if (!dom.wallsSplitDte) return;
    var dteVal = dom.wallsDteSelect ? dom.wallsDteSelect.value : "ALL";
    var metricVal = dom.wallsMetricSelect ? dom.wallsMetricSelect.value : "auto";
    var canSplit = dteVal === "ALL" && metricVal !== "overlay";
    dom.wallsSplitDte.disabled = !canSplit;
    if (dom.wallsSplitDteWrap) {
      dom.wallsSplitDteWrap.classList.toggle("is-disabled", !canSplit);
    }
  }

  function populateWallsDteFilter(data) {
    if (!dom.wallsDteSelect) return;
    var previous = dom.wallsDteSelect.value || "ALL";
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

    var minDte = expiries[0].dte;
    var maxDte = expiries[expiries.length - 1].dte;
    var dteSpan = minDte === maxDte ? minDte + "d" : minDte + "\u2013" + maxDte + "d";
    defaultOpt.textContent =
      "All DTEs (" + dteSpan + " \u2022 " + expiries.length + " exp)";
    dom.wallsDteSelect.appendChild(defaultOpt);

    for (var i = 0; i < expiries.length; i++) {
      var exp = expiries[i];
      var opt = document.createElement("option");
      opt.value = exp.expiry;
      opt.textContent = exp.dte + "d (" + formatExpiryIso(exp.expiry) + ")";
      if (opt.value === previous) {
        opt.selected = true;
      }
      dom.wallsDteSelect.appendChild(opt);
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

  function renderWallsChart(data, t) {
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

    var activeExpiries =
      dteSel === "ALL"
        ? allExpiries
        : allExpiries.filter(function (exp) {
            return exp.expiry === dteSel;
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
        dteSel === "ALL" &&
        effectiveMetric !== "overlay"
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
    if (dteSel === "ALL") {
      var dteList = activeExpiries
        .map(function (exp) {
          return exp.dte + "d";
        })
        .join(", ");
      var firstExp = formatExpiryIso(activeExpiries[0].expiry);
      var lastExp = formatExpiryIso(activeExpiries[activeExpiries.length - 1].expiry);
      var expSpan = firstExp === lastExp ? firstExp : firstExp + ".." + lastExp;
      dteBadgeText = "DTEs: " + dteList + " (" + expSpan + ")";
    } else {
      var onlyExp = activeExpiries[0];
      dteBadgeText = "DTE: " + onlyExp.dte + "d (EXP " + formatExpiryIso(onlyExp.expiry) + ")";
    }
    var pullScopeLabel =
      data.meta && (data.meta.range_pct !== null || data.meta.strike_step !== null)
        ? " \u2022 " +
          formatRangeShort(data.meta.range_pct) +
          " / " +
          formatStepShort(data.meta.strike_step) +
          " STEP"
        : "";
    if (dom.wallsBasisLabel) {
      dom.wallsBasisLabel.textContent =
        viewBadgeText + " \u2022 " + metricBadgeText + pullScopeLabel + " \u2022 " + dteBadgeText;
    }

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

    var minStep = 5;
    for (var sIdx = 1; sIdx < strikes.length; sIdx++) {
      var diff = strikes[sIdx] - strikes[sIdx - 1];
      if (diff > 0 && diff < minStep) {
        minStep = diff;
      }
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

      var slot = minStep * 0.4;
      var oiOptsCall = isLadder
        ? { width: minStep * 0.78, opacity: 0.34 }
        : { offset: -slot * 0.95, width: slot * 0.9, opacity: 0.34 };
      var volOptsCall = isLadder
        ? { width: minStep * 0.42, opacity: 1 }
        : { offset: -slot * 0.75, width: slot * 0.5, opacity: 1 };
      var oiOptsPut = isLadder
        ? { width: minStep * 0.78, opacity: 0.34 }
        : { offset: slot * 0.05, width: slot * 0.9, opacity: 0.34 };
      var volOptsPut = isLadder
        ? { width: minStep * 0.42, opacity: 1 }
        : { offset: slot * 0.25, width: slot * 0.5, opacity: 1 };

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

        traces.push({
          type: "bar",
          orientation: isLadder ? "h" : "v",
          name: "C " + bObj.dte + "d",
          x: isLadder ? callSlice : strikes,
          y: isLadder ? strikes : callSlice,
          offsetgroup: isLadder ? undefined : "calls",
          marker: { color: callColor },
          customdata: callCustom,
          hovertemplate: splitHoverTemplate,
        });
        traces.push({
          type: "bar",
          orientation: isLadder ? "h" : "v",
          name: "P " + bObj.dte + "d",
          x: isLadder ? putSlice : strikes,
          y: isLadder ? strikes : putSlice,
          offsetgroup: isLadder ? undefined : "puts",
          marker: { color: putColor },
          customdata: putCustom,
          hovertemplate: splitHoverTemplate,
        });
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

      traces.push(
        {
          type: "bar",
          orientation: isLadder ? "h" : "v",
          name: "Calls",
          x: isLadder ? callVals : strikes,
          y: isLadder ? strikes : callVals,
          marker: { color: t.call },
          customdata: customC,
          hovertemplate: combinedHoverTemplate,
        },
        {
          type: "bar",
          orientation: isLadder ? "h" : "v",
          name: "Puts",
          x: isLadder ? putVals : strikes,
          y: isLadder ? strikes : putVals,
          marker: { color: t.put },
          customdata: customP,
          hovertemplate: combinedHoverTemplate,
        }
      );
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
            var sideOffset = minStep * 0.2;
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

    if (isLadder) {
      var tickCfg = buildSymmetricPositiveTicks(maxLeft, maxRight);
      layout.margin.l = 58;
      layout.xaxis.tickmode = "array";
      layout.xaxis.tickvals = tickCfg.tickvals;
      layout.xaxis.ticktext = tickCfg.ticktext;
      layout.xaxis.range = tickCfg.range;
      layout.xaxis.zeroline = true;
      layout.xaxis.zerolinewidth = 1.5;
      layout.xaxis.zerolinecolor = t.ink;
      layout.yaxis.tickprefix = "$";
    } else {
      layout.xaxis.tickprefix = "$";
      layout.yaxis.rangemode = "nonnegative";
    }

    window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
  }

  function renderIvSmileChart(data, t) {
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

  function renderAnomaliesChart(data, t) {
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

  function renderGreeksChart(data, t) {
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

  function renderAllCharts(data) {
    if (!data) {
      purgeCharts();
      return;
    }
    var t = themeVars();
    renderWallsChart(data, t);
    renderIvSmileChart(data, t);
    renderAnomaliesChart(data, t);
    renderGreeksChart(data, t);
  }

  // ------------------------------------------------------------------ Option Chain Table
  function populateDteFilter(data) {
    if (!dom.filterDte) return;
    var previous = dom.filterDte.value || "ALL";
    dom.filterDte.innerHTML = '<option value="ALL">All DTEs</option>';
    var expiries =
      data && data.profiles && data.profiles.expiries ? data.profiles.expiries.slice() : [];
    expiries.sort(function (a, b) {
      return a.dte - b.dte;
    });
    for (var i = 0; i < expiries.length; i++) {
      var exp = expiries[i];
      var opt = document.createElement("option");
      opt.value = exp.expiry;
      opt.textContent = exp.dte + "d (" + formatExpiryIso(exp.expiry) + ")";
      if (opt.value === previous) opt.selected = true;
      dom.filterDte.appendChild(opt);
    }
  }

  function renderTable(data) {
    if (!dom.tableBody) return;
    if (!data || !data.contracts || !data.contracts.length) {
      dom.tableBody.innerHTML =
        '<tr><td colspan="17" class="text-left">No cached contracts available. Click REFRESH FROM IBKR.</td></tr>';
      if (dom.tableRowCount) dom.tableRowCount.textContent = "0 CONTRACTS";
      return;
    }

    var anomalyMap = {};
    var anoms = (data.anomalies && data.anomalies.anomalies) || [];
    for (var a = 0; a < anoms.length; a++) {
      var item = anoms[a];
      anomalyMap[item.expiry + "|" + item.strike + "|" + item.right] = item;
    }

    var dteFilter = dom.filterDte ? dom.filterDte.value : "ALL";
    var rightFilter = dom.filterRight ? dom.filterRight.value : "ALL";
    var unusualOnly = Boolean(dom.filterUnusual && dom.filterUnusual.checked);
    var spot = data.meta.underlying_price;

    var filtered = [];
    for (var i = 0; i < data.contracts.length; i++) {
      var c = data.contracts[i];
      if (dteFilter !== "ALL" && c.expiry !== dteFilter) continue;
      if (rightFilter !== "ALL" && c.right !== rightFilter) continue;
      var aKey = c.expiry + "|" + c.strike + "|" + c.right;
      var flagged = anomalyMap[aKey];
      if (unusualOnly && !flagged) continue;
      filtered.push({ row: c, anomaly: flagged || null });
    }

    if (dom.tableRowCount) {
      dom.tableRowCount.textContent =
        filtered.length + " OF " + data.contracts.length + " CONTRACTS";
    }

    if (!filtered.length) {
      dom.tableBody.innerHTML =
        '<tr><td colspan="17" class="text-left">No contracts match the active table filters.</td></tr>';
      return;
    }

    var html = [];
    for (var r = 0; r < filtered.length; r++) {
      var cRow = filtered[r].row;
      var flag = filtered[r].anomaly;
      var isAtm = spot > 0 && Math.abs((cRow.strike - spot) / spot) <= 0.015;
      var classes = [];
      if (isAtm) classes.push("row-atm");
      if (flag) classes.push("row-anomaly");

      var vol = cRow.volume !== null && cRow.volume !== undefined ? Number(cRow.volume) : null;
      var oi =
        cRow.open_interest !== null && cRow.open_interest !== undefined
          ? Number(cRow.open_interest)
          : null;
      var ratio = vol !== null && oi !== null && oi > 0 ? vol / oi : null;
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
          "</tr>"
      );
    }
    dom.tableBody.innerHTML = html.join("");
  }

  // ------------------------------------------------------------------ Snapshot Dropdown
  function renderSnapshotSelect(payload) {
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

  // ------------------------------------------------------------------ Network Requests
  async function apiJson(url, options) {
    var resp = await window.fetch(url, options);
    var body = null;
    try {
      body = await resp.json();
    } catch (_err) {
      body = null;
    }
    return { ok: resp.ok, status: resp.status, body: body };
  }

  async function fetchStatus() {
    try {
      var res = await apiJson("/api/status");
      if (res.ok && res.body) {
        renderStatusBadges(res.body);
      }
    } catch (_err) {
      renderStatusBadges({ connected: false, default_market_data_type: 3 });
    }
  }

  async function fetchSnapshots(ticker) {
    var res = await apiJson("/api/snapshots/" + encodeURIComponent(ticker));
    if (res.ok && res.body) {
      renderSnapshotSelect(res.body);
      return;
    }
    renderSnapshotSelect({ snapshots: [] });
  }

  async function loadChain(ticker, snapshotId) {
    state.ticker = ticker;
    state.snapshotId = snapshotId || "";
    setActiveTickerUI(ticker);

    await fetchSnapshots(ticker);
    var url = "/api/chain/" + encodeURIComponent(ticker);
    if (snapshotId) {
      url += "?snapshot_id=" + encodeURIComponent(snapshotId);
    }

    var res = await apiJson(url);
    if (res.ok && res.body) {
      state.chainData = res.body;
      state.snapshotId = res.body.meta.snapshot_id;
      updateExportLink();
      syncControlsFromMeta(res.body.meta);
      renderChainBadges(res.body.meta);
      renderKpis(res.body);
      populateDteFilter(res.body);
      populateWallsDteFilter(res.body);
      renderAllCharts(res.body);
      renderTable(res.body);

      if (res.body.meta.notices && res.body.meta.notices.length) {
        showBanner("info", res.body.meta.notices.join(" "));
      } else {
        showBanner("", "");
      }
      return;
    }

    if (res.status === 404) {
      state.chainData = null;
      purgeCharts();
      renderChainBadges(null);
      renderKpis(null);
      populateDteFilter(null);
      populateWallsDteFilter(null);
      renderTable(null);
      showBanner(
        "info",
        "No cached snapshot exists for " +
          ticker +
          ". Click REFRESH FROM IBKR to pull and archive live/delayed chain data."
      );
      return;
    }

    var errMsg =
      (res.body && res.body.message) || "Unable to load option chain for " + ticker + ".";
    showBanner("error", errMsg);
  }

  async function refreshFromIBKR() {
    if (state.loading) return;
    state.loading = true;
    if (dom.refreshBtn) {
      dom.refreshBtn.disabled = true;
      dom.refreshBtn.textContent = "REFRESHING...";
    }

    var rangeVal = dom.rangeSelect ? Number(dom.rangeSelect.value) : 0.2;
    var stepRaw = dom.stepSelect ? dom.stepSelect.value : "5.0";
    var stepVal = stepRaw === "auto" ? null : Number(stepRaw);

    try {
      var res = await apiJson("/api/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ticker: state.ticker,
          range_pct: rangeVal,
          strike_step: stepVal,
        }),
      });

      if (res.ok && res.body) {
        state.chainData = res.body;
        state.snapshotId = res.body.meta.snapshot_id;
        await fetchSnapshots(state.ticker);
        await fetchStatus();
        syncControlsFromMeta(res.body.meta);
        renderChainBadges(res.body.meta);
        renderKpis(res.body);
        populateDteFilter(res.body);
        populateWallsDteFilter(res.body);
        renderAllCharts(res.body);
        renderTable(res.body);
        var msg =
          res.body.meta.notices && res.body.meta.notices.length
            ? res.body.meta.notices.join(" ")
            : "Archived new IBKR snapshot " + res.body.meta.snapshot_id + ".";
        showBanner("info", msg);
      } else if (res.status === 503) {
        await fetchStatus();
        var offlineMsg =
          (res.body && res.body.message) ||
          "IBKR Gateway is offline or unreachable. Preserving cached snapshot view.";
        showBanner("warn", offlineMsg);
      } else {
        var errMsg = (res.body && res.body.message) || "Refresh failed.";
        showBanner("error", errMsg);
      }
    } catch (_err) {
      await fetchStatus();
      showBanner(
        "warn",
        "Network or gateway interruption during refresh. Preserving cached snapshot view."
      );
    } finally {
      state.loading = false;
      if (dom.refreshBtn) {
        dom.refreshBtn.disabled = false;
        dom.refreshBtn.textContent = "REFRESH FROM IBKR";
      }
    }
  }

  // ------------------------------------------------------------------ Event Bindings
  function bindEvents() {
    if (dom.themeToggle) {
      dom.themeToggle.addEventListener("click", function () {
        var next = currentTheme() === "terminal" ? "print" : "terminal";
        document.documentElement.setAttribute("data-theme", next);
        try {
          window.localStorage.setItem("ibkr_options_theme", next);
        } catch (_err) {
          /* ignore */
        }
        updateThemeToggleLabel();
        if (state.chainData) {
          renderAllCharts(state.chainData);
        }
      });
    }

    if (dom.watchlistPills) {
      dom.watchlistPills.addEventListener("click", function (ev) {
        var target = ev.target;
        if (target && target.matches && target.matches(".pill-btn")) {
          var sym = target.getAttribute("data-ticker");
          if (sym) {
            if (dom.customInput) dom.customInput.value = "";
            loadChain(sym, "");
          }
        }
      });
    }

    if (dom.customForm) {
      dom.customForm.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var raw = dom.customInput ? dom.customInput.value.trim().toUpperCase() : "";
        if (!raw) return;
        loadChain(raw, "");
      });
    }

    if (dom.snapshotSelect) {
      dom.snapshotSelect.addEventListener("change", function () {
        var snapId = dom.snapshotSelect.value;
        loadChain(state.ticker, snapId);
      });
    }

    if (dom.refreshBtn) {
      dom.refreshBtn.addEventListener("click", refreshFromIBKR);
    }

    if (dom.wallsCopyAiBtn) {
      dom.wallsCopyAiBtn.addEventListener("click", copyWallsAiPrompt);
    }

    if (dom.greeksMetricSelect) {
      dom.greeksMetricSelect.addEventListener("change", function () {
        if (state.chainData) {
          renderGreeksChart(state.chainData, themeVars());
        }
      });
    }

    function onWallsControlChange() {
      if (dom.wallsViewSelect) writePref("ibkr_walls_view", dom.wallsViewSelect.value);
      if (dom.wallsMetricSelect) writePref("ibkr_walls_metric", dom.wallsMetricSelect.value);
      if (dom.wallsSplitDte) writePref("ibkr_walls_split_dte", dom.wallsSplitDte.checked);
      if (dom.wallsShowAnomalies) {
        writePref("ibkr_walls_show_anomalies", dom.wallsShowAnomalies.checked);
      }
      syncWallsSplitCheckboxState();
      if (state.chainData) {
        renderWallsChart(state.chainData, themeVars());
      }
    }

    [
      dom.wallsViewSelect,
      dom.wallsMetricSelect,
      dom.wallsDteSelect,
      dom.wallsSplitDte,
      dom.wallsShowAnomalies,
    ].forEach(function (ctrl) {
      if (ctrl) {
        ctrl.addEventListener("change", onWallsControlChange);
      }
    });

    [dom.filterDte, dom.filterRight, dom.filterUnusual].forEach(function (ctrl) {
      if (ctrl) {
        ctrl.addEventListener("change", function () {
          renderTable(state.chainData);
        });
      }
    });
  }

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

  function computeScopedWallsForPrompt(activeExpiries, spot) {
    var byStrike = {};
    var totalCallOi = 0;
    var totalPutOi = 0;
    var totalCallVol = 0;
    var totalPutVol = 0;

    for (var i = 0; i < activeExpiries.length; i++) {
      var expObj = activeExpiries[i];
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
        if (pt.call_iv !== null && pt.call_iv !== undefined && !isNaN(Number(pt.call_iv))) {
          row.call_iv_sum += Number(pt.call_iv);
          row.call_iv_cnt += 1;
        }
        if (pt.put_iv !== null && pt.put_iv !== undefined && !isNaN(Number(pt.put_iv))) {
          row.put_iv_sum += Number(pt.put_iv);
          row.put_iv_cnt += 1;
        }
      }
    }

    var ladder = Object.keys(byStrike)
      .map(function (k) {
        return byStrike[k];
      })
      .sort(function (a, b) {
        return a.strike - b.strike;
      });

    function topN(field, n) {
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
          };
        });
    }

    return {
      ladder: ladder,
      totalCallOi: totalCallOi,
      totalPutOi: totalPutOi,
      totalCallVol: totalCallVol,
      totalPutVol: totalPutVol,
      topCallOi: topN("call_oi", 3),
      topPutOi: topN("put_oi", 3),
      topCallVol: topN("call_volume", 3),
      topPutVol: topN("put_volume", 3),
    };
  }

  function buildWallsAiPrompt(data) {
    if (!data || !data.meta || !data.profiles) return "";
    var meta = data.meta;
    var walls = data.walls || {};
    var spot = Number(meta.underlying_price || 0);
    var allExpiries = Array.isArray(data.profiles.expiries) ? data.profiles.expiries : [];
    var selectedExpiry = dom.wallsDteSelect ? dom.wallsDteSelect.value : "ALL";
    var isSingleDte = selectedExpiry && selectedExpiry !== "ALL";
    var activeExpiries = isSingleDte
      ? allExpiries.filter(function (ex) {
          return String(ex.expiry) === String(selectedExpiry);
        })
      : allExpiries;
    if (!activeExpiries.length) {
      activeExpiries = allExpiries;
      isSingleDte = false;
    }

    var scoped = computeScopedWallsForPrompt(activeExpiries, spot);
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
    var greeksCounts = meta.greeks_source_counts || {};
    var greeksLabel = Object.keys(greeksCounts).length
      ? Object.keys(greeksCounts)
          .map(function (k) {
            return k + ":" + greeksCounts[k];
          })
          .join(", ")
      : "n/a";

    var mdNames = {
      1: "TYPE 1 (Live OPRA)",
      2: "TYPE 2 (Frozen)",
      3: "TYPE 3 (15-min Delayed)",
      4: "TYPE 4 (Delayed-Frozen)",
    };
    var mdLabel = mdNames[meta.market_data_type] || "TYPE " + (meta.market_data_type || "?");

    var scopeTitle = isSingleDte
      ? "WYBRANE WYGAŚNIĘCIE (SINGLE DTE): " +
        activeExpiries[0].dte +
        " DTE (" +
        promptExpiryIso(activeExpiries[0].expiry) +
        ")"
      : "WSZYSTKIE TYGODNIOWE WYGAŚNIĘCIA (>30D i <60D): " +
        activeExpiries
          .map(function (ex) {
            return ex.dte + "d (" + promptExpiryIso(ex.expiry) + ")";
          })
          .join(", ");

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
        pw
      );
    });

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
    var scopedAnomalies = isSingleDte
      ? allAnomalies.filter(function (a) {
          return String(a.expiry) === String(activeExpiries[0].expiry);
        })
      : allAnomalies;

    var anomalyLines = scopedAnomalies.slice(0, 12).map(function (a) {
      var volOi =
        a.vol_oi_ratio !== null && a.vol_oi_ratio !== undefined
          ? promptNum(a.vol_oi_ratio, 2) + "x"
          : "n/a";
      var zsc =
        a.volume_zscore !== null && a.volume_zscore !== undefined
          ? "z=" + promptNum(a.volume_zscore, 2)
          : "z=n/a";
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
        ") | Flagi: " +
        (a.reasons && a.reasons.length ? a.reasons.join(", ") : "VOLUME")
      );
    });

    var ladderLines = ["Strike | OdSpot% | Call_OI | Put_OI | Net_OI(C-P) | Call_Vol | Put_Vol | Call_IV% | Put_IV%"];
    for (var m = 0; m < scoped.ladder.length; m++) {
      var st = scoped.ladder[m];
      var dist = spot > 0 ? ((st.strike - spot) / spot) * 100 : 0;
      var cIv = st.call_iv_cnt > 0 ? promptNum((st.call_iv_sum / st.call_iv_cnt) * 100, 1) + "%" : "-";
      var pIv = st.put_iv_cnt > 0 ? promptNum((st.put_iv_sum / st.put_iv_cnt) * 100, 1) + "%" : "-";
      ladderLines.push(
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
          pIv
      );
    }

    var todayIso = meta.market_date || "n/a";
    var lines = [
      "ROLA: Jesteś praktycznym analitykiem rynku opcji.",
      "ZADANIE WEWNĘTRZNE (zrób przed napisaniem odpowiedzi):",
      "1) Przeanalizuj głęboko poniższe dane opcyjne (ściany Put/Call z OI vs Volume, dystans od Spot, IV, anomalie Vol > OI i Z-Score, horyzont DTE).",
      "2) Uwzględnij kontekst dnia dzisiejszego (" + todayIso + ", UTC: " + (meta.snapshot_ts_utc || "n/a") + ") i aktualnego otoczenia rynkowego/makro/sektorowego dla " + (meta.underlying || "n/a") + " (jeśli nie masz dostępu do bieżących wiadomości lub daty wyników, zaznacz to w 1 zdaniu zamiast zgadywać).",
      "",
      "ŚCISŁE ZASADY ODPOWIEDZI:",
      "- JĘZYK: wyłącznie PROSTY, CODZIENNY POLSKI (zero ciężkiego żargonu; każde pojęcie typu Call Wall/Put Wall/IV wyjaśnij od razu w 3-5 prostych słowach).",
      "- FORMAT: BEZ ŚCIANY TEKSTU. Tylko krótkie wypunktowania dokładnie według szablonu poniżej.",
      "- LIMIT DŁUGOŚCI: MAKSYMALNIE 250-350 słów. Krótko, konkretnie, zero ogólników.",
      "",
      "=== DANE ANALITYCZNE (" + (meta.underlying || "n/a") + ") ===",
      "- Ticker: " + (meta.underlying || "n/a") + " | Cena Spot: $" + promptNum(spot, 2),
      "- Data rynkowa: " + todayIso + " | Timestamp UTC: " + (meta.snapshot_ts_utc || "n/a") + " | Snapshot ID: " + (meta.snapshot_id || "n/a"),
      "- Tryb danych IBKR: " + mdLabel + " | Źródło Greeks/IV: " + greeksLabel + " | Śr. ATM IV: " + avgAtmIvLabel,
      "- Zakres pobrania: Range " + rangeLabel + " | Step $ " + stepLabel + " | Metryka wykresu: " + selectedMetric.toUpperCase() + " (baza ścian: " + effectiveBasis + ")",
      "- Aktywny zakres DTE: " + scopeTitle,
      "",
      "=== PODSUMOWANIE DTE ===",
      dteSummaryLines.join("\n"),
      "",
      "=== BILANS PUT / CALL I TOP ŚCIANY ===",
      "- Call OI: " + promptInt(scoped.totalCallOi) + " | Put OI: " + promptInt(scoped.totalPutOi) + " | P/C (OI): " + pcOiRatio,
      "- Call Vol: " + promptInt(scoped.totalCallVol) + " | Put Vol: " + promptInt(scoped.totalPutVol) + " | P/C (Vol): " + pcVolRatio,
      "- Top 3 Call Walls (OI): " + fmtTopList(scoped.topCallOi),
      "- Top 3 Put Walls (OI): " + fmtTopList(scoped.topPutOi),
      "- Top 3 Call Walls (Vol): " + fmtTopList(scoped.topCallVol),
      "- Top 3 Put Walls (Vol): " + fmtTopList(scoped.topPutVol),
      "",
      "=== ANOMALIE (Volume > OI / Z-Score) ===",
      anomalyLines.length ? anomalyLines.join("\n") : "- Brak wykrytych anomalii w wybranym zakresie DTE.",
      "",
      "=== DRABINA STRIKE'ÓW ===",
      ladderLines.join("\n"),
      "",
      "=== WYMAGANA STRUKTURA ODPOWIEDZI (SKOPIUJ DOKŁADNIE TE 4 NAGŁÓWKI, BEZ DŁUGICH AKAPITÓW) ===",
      "### 1. Kontekst (dane + dziś na rynku)",
      "- **Obecna pozycja ($" + promptNum(spot, 2) + "):** 1 krótkie zdanie - gdzie stoi kurs między barierami i jaki widać klimat (spokój / presja wzrostowa / defensywa).",
      "- **Kontekst dnia (" + todayIso + ") i świata:** 1-2 krótkie punkty o sytuacji rynkowej/sektorowej dla " + (meta.underlying || "n/a") + " teraz (lub krótka wzmianka, jeśli brak live kalendarza).",
      "- **Zmienność i jakość danych:** 1 krótki punkt - ile wynosi IV (~" + avgAtmIvLabel + "), czy ściany opierają się na OI czy tylko Volume (" + mdLabel + "), oraz czy skok " + stepLabel + " i zakres " + rangeLabel + " dają pełny obraz.",
      "",
      "### 2. Put / Call Walls (Główne bariery)",
      "- **Call Wall (górna bariera / opór):** główny poziom $ i dystans %, plus co może się stać po wybiciu wyżej.",
      "- **Put Wall (dolna bariera / wsparcie):** główny poziom $ i dystans %, plus gdzie leży kolejne wsparcie.",
      "- **Przewaga stron (Put vs Call):** 1 krótki wniosek z P/C Ratio i układu ścian.",
      "",
      "### 3. Anomalie (Nietypowy ruch)",
      "- Maksymalnie 2-3 krótkie punkty o najsilniejszych sygnałach z sekcji ANOMALIE (DTE, C/P, strike $, Vol vs OI) i prosty wniosek: świeży zakład kierunkowy czy hedging (albo napisz wprost: brak istotnych anomalii).",
      "",
      "### 4. Top 3 strategie opcyjne pod ten układ",
      "Wybierz 3 najlepiej pasujące do tych barier, IV i wybranego DTE. Dla każdej napisz TYLKO te 2 krótkie podpunkty:",
      "1. **[Nazwa strategii — konkretne strike'i $ i data YYYY-MM-DD z danych]**",
      "   - *Dlaczego pasuje:* 1 krótkie, proste zdanie (jak wykorzystuje tutejsze ściany / anomalie / IV).",
      "   - *Scenariusz i ryzyko:* kiedy zarabia vs konkretny poziom $, gdzie teza się psuje.",
      "2. **[Nazwa strategii — konkretne strike'i $ i data YYYY-MM-DD z danych]**",
      "   - *Dlaczego pasuje:* 1 krótkie, proste zdanie.",
      "   - *Scenariusz i ryzyko:* kiedy zarabia vs konkretny poziom $, gdzie teza się psuje.",
      "3. **[Nazwa strategii — konkretne strike'i $ i data YYYY-MM-DD z danych]**",
      "   - *Dlaczego pasuje:* 1 krótkie, proste zdanie.",
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

  function copyWallsAiPrompt() {
    if (!state.chainData || !state.chainData.profiles) {
      showBanner("warn", "Load an option chain snapshot before copying the AI analysis prompt.");
      return;
    }
    var promptText = buildWallsAiPrompt(state.chainData);
    if (!promptText) {
      showBanner("warn", "No option strikes available in the active DTE scope.");
      return;
    }
    var dteScope =
      dom.wallsDteSelect && dom.wallsDteSelect.value !== "ALL"
        ? dom.wallsDteSelect.options[dom.wallsDteSelect.selectedIndex].textContent
        : "All weekly DTEs (>30d & <60d)";
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

  function restoreWallsPreferences() {
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

  var SNAP_COLS = [4, 6, 8, 12];
  var SNAP_HEIGHTS = [290, 350, 410, 470, 530, 590, 650, 720, 800, 880];
  var GRID_LAYOUT_KEY = "ibkr_chart_grid_layout_v1";

  function readGridLayoutPrefs() {
    try {
      var raw = window.localStorage.getItem(GRID_LAYOUT_KEY);
      if (!raw) return {};
      var parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_err) {
      return {};
    }
  }

  function writeGridLayoutPrefs(layoutMap) {
    try {
      window.localStorage.setItem(GRID_LAYOUT_KEY, JSON.stringify(layoutMap));
    } catch (_err) {
      /* ignore */
    }
  }

  function nearestSnapValue(value, candidates) {
    var best = candidates[0];
    var bestDist = Math.abs(value - best);
    for (var i = 1; i < candidates.length; i++) {
      var d = Math.abs(value - candidates[i]);
      if (d < bestDist) {
        best = candidates[i];
        bestDist = d;
      }
    }
    return best;
  }

  function applyCardSpanClass(card, span) {
    SNAP_COLS.forEach(function (col) {
      card.classList.remove("panel-card--span-" + col);
    });
    if (span) {
      card.classList.add("panel-card--span-" + span);
    }
  }

  function computeTargetSnap(gridEl, measuredWidth, measuredHeight) {
    var gridWidth = gridEl ? gridEl.getBoundingClientRect().width : 0;
    var gapPx = 16;
    if (gridEl) {
      var parsedGap = parseFloat(window.getComputedStyle(gridEl).columnGap);
      if (!isNaN(parsedGap)) gapPx = parsedGap;
    }
    var trackWidth = gridWidth > 0 ? Math.max(20, (gridWidth - gapPx * 11) / 12) : 80;
    var bestSpan = 6;
    var bestSpanDist = Infinity;
    for (var i = 0; i < SNAP_COLS.length; i++) {
      var cols = SNAP_COLS[i];
      var targetW = cols * trackWidth + (cols - 1) * gapPx;
      var dist = Math.abs(measuredWidth - targetW);
      if (dist < bestSpanDist) {
        bestSpanDist = dist;
        bestSpan = cols;
      }
    }
    var bestHeight = nearestSnapValue(measuredHeight, SNAP_HEIGHTS);
    return { span: bestSpan, height: bestHeight };
  }

  function initChartResizeHandling() {
    var gridEl = document.querySelector(".chart-grid");
    var cards = document.querySelectorAll(".chart-grid .panel-card");
    if (!cards.length) return;

    var rafByCanvasId = {};
    function schedulePlotlyResize(canvasEl) {
      if (!canvasEl || typeof window.Plotly === "undefined" || !canvasEl.data) return;
      var cid = canvasEl.id || "chart";
      if (rafByCanvasId[cid]) {
        window.cancelAnimationFrame(rafByCanvasId[cid]);
      }
      rafByCanvasId[cid] = window.requestAnimationFrame(function () {
        if (window.Plotly && window.Plotly.Plots && canvasEl.data) {
          window.Plotly.Plots.resize(canvasEl);
        }
      });
    }

    function cardKey(card, idx) {
      if (card.id) return card.id;
      var canvas = card.querySelector(".chart-canvas");
      return canvas && canvas.id ? canvas.id : "panel-" + idx;
    }

    var savedLayout = readGridLayoutPrefs();
    var activeDrag = null;

    function cardSpanValue(card) {
      for (var s = 0; s < SNAP_COLS.length; s++) {
        if (card.classList.contains("panel-card--span-" + SNAP_COLS[s])) {
          return SNAP_COLS[s];
        }
      }
      return 6;
    }

    function updateCardSpanButton(card) {
      var btn = card.querySelector(".panel-span-btn");
      if (btn) {
        btn.textContent = cardSpanValue(card) + "/12";
      }
    }

    function snapCardToGrid(card, key, measuredWidth, measuredHeight) {
      var snap = computeTargetSnap(gridEl, measuredWidth, measuredHeight);
      card.style.width = "";
      card.style.maxWidth = "";
      card.style.height = snap.height + "px";
      applyCardSpanClass(card, snap.span);
      updateCardSpanButton(card);
      card.classList.remove("is-snapping-drag");
      card.removeAttribute("data-snap-hint");

      savedLayout[key] = { span: snap.span, height: snap.height };
      writeGridLayoutPrefs(savedLayout);

      var canvasEl = card.querySelector(".chart-canvas");
      if (canvasEl) {
        schedulePlotlyResize(canvasEl);
        window.setTimeout(function () {
          schedulePlotlyResize(canvasEl);
        }, 60);
      }
    }

    var observer =
      typeof window.ResizeObserver !== "undefined"
        ? new window.ResizeObserver(function (entries) {
            for (var i = 0; i < entries.length; i++) {
              var canvas = entries[i].target.querySelector(".chart-canvas");
              if (canvas) schedulePlotlyResize(canvas);
            }
          })
        : null;

    for (var i = 0; i < cards.length; i++) {
      (function (card, idx) {
        var key = cardKey(card, idx);
        var defaultSpan = "";
        for (var s = 0; s < SNAP_COLS.length; s++) {
          if (card.classList.contains("panel-card--span-" + SNAP_COLS[s])) {
            defaultSpan = String(SNAP_COLS[s]);
            break;
          }
        }
        card.dataset.defaultSpan = defaultSpan;

        var pref = savedLayout[key];
        if (pref && typeof pref === "object") {
          if (pref.span && SNAP_COLS.indexOf(Number(pref.span)) !== -1) {
            applyCardSpanClass(card, Number(pref.span));
          }
          if (pref.height && !isNaN(Number(pref.height))) {
            card.style.height = Number(pref.height) + "px";
          }
        }

        if (observer) {
          observer.observe(card);
        }

        var canvasEl = card.querySelector(".chart-canvas");
        var titleEl = card.querySelector(".panel-title");

        // Clickable grid width cycle badge ([6/12] -> 8/12 -> 12/12 -> 4/12 -> 6/12)
        if (titleEl && !titleEl.querySelector(".panel-span-btn")) {
          var spanBtn = document.createElement("button");
          spanBtn.type = "button";
          spanBtn.className = "panel-span-btn";
          spanBtn.title = "Click to cycle panel width (4/6/8/12 columns)";
          spanBtn.setAttribute("aria-label", "Cycle panel grid width");
          spanBtn.addEventListener("dblclick", function (ev) {
            ev.stopPropagation();
          });
          spanBtn.addEventListener("click", function (ev) {
            ev.stopPropagation();
            var curSpan = cardSpanValue(card);
            var cycle = [6, 8, 12, 4];
            var nextSpan = cycle[(cycle.indexOf(curSpan) + 1) % cycle.length];
            var curHeight = Math.round(card.getBoundingClientRect().height) || 410;
            card.style.width = "";
            card.style.maxWidth = "";
            card.style.height = curHeight + "px";
            applyCardSpanClass(card, nextSpan);
            updateCardSpanButton(card);
            savedLayout[key] = { span: nextSpan, height: curHeight };
            writeGridLayoutPrefs(savedLayout);
            if (canvasEl) {
              schedulePlotlyResize(canvasEl);
              window.setTimeout(function () {
                schedulePlotlyResize(canvasEl);
              }, 60);
            }
          });
          titleEl.appendChild(spanBtn);
          updateCardSpanButton(card);
        }

        // Dedicated PointerEvents corner handle (never blocked by CSS Grid max-width or Plotly)
        var handle = card.querySelector(".panel-resize-handle");
        if (!handle) {
          handle = document.createElement("button");
          handle.type = "button";
          handle.className = "panel-resize-handle";
          handle.setAttribute("aria-label", "Drag corner to resize panel");
          handle.title = "Drag corner to resize and snap to 12-column grid";
          card.appendChild(handle);
        }

        var dragState = null;

        handle.addEventListener("pointerdown", function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          var startRect = card.getBoundingClientRect();
          var gridRect = gridEl ? gridEl.getBoundingClientRect() : startRect;
          dragState = {
            pointerId: ev.pointerId,
            startX: ev.clientX,
            startY: ev.clientY,
            startW: startRect.width,
            startH: startRect.height,
            maxW: Math.max(startRect.width, gridRect.width),
            mobileOnlyY: window.innerWidth <= 1120,
          };
          card.classList.add("is-snapping-drag");
          card.style.maxWidth = "none";
          if (!dragState.mobileOnlyY) {
            card.style.width = Math.round(startRect.width) + "px";
          }
          card.style.height = Math.round(startRect.height) + "px";
          var initSnap = computeTargetSnap(gridEl, startRect.width, startRect.height);
          card.setAttribute(
            "data-snap-hint",
            "Snap " + initSnap.span + "/12 col • " + initSnap.height + "px"
          );
          if (handle.setPointerCapture) {
            try {
              handle.setPointerCapture(ev.pointerId);
            } catch (_err) {
              /* ignore synthetic pointer events */
            }
          }
        });

        handle.addEventListener("pointermove", function (ev) {
          if (!dragState || ev.pointerId !== dragState.pointerId) return;
          var dx = ev.clientX - dragState.startX;
          var dy = ev.clientY - dragState.startY;
          var nextW = dragState.startW;
          if (!dragState.mobileOnlyY) {
            nextW = Math.max(260, Math.min(dragState.maxW, dragState.startW + dx));
            card.style.width = Math.round(nextW) + "px";
          }
          var nextH = Math.max(290, Math.min(920, dragState.startH + dy));
          card.style.height = Math.round(nextH) + "px";
          var preview = computeTargetSnap(gridEl, nextW, nextH);
          card.setAttribute(
            "data-snap-hint",
            "Snap " + preview.span + "/12 col • " + preview.height + "px"
          );
          if (canvasEl) schedulePlotlyResize(canvasEl);
        });

        function endHandleDrag(ev) {
          if (!dragState) return;
          if (ev && ev.pointerId !== undefined && ev.pointerId !== dragState.pointerId) return;
          var st = dragState;
          dragState = null;
          if (handle.releasePointerCapture && ev && ev.pointerId !== undefined) {
            try {
              if (!handle.hasPointerCapture || handle.hasPointerCapture(ev.pointerId)) {
                handle.releasePointerCapture(ev.pointerId);
              }
            } catch (_err) {
              /* ignore */
            }
          }
          var measuredW = st.mobileOnlyY
            ? card.getBoundingClientRect().width
            : parseFloat(card.style.width) || card.getBoundingClientRect().width;
          var measuredH = parseFloat(card.style.height) || card.getBoundingClientRect().height;
          if (st.mobileOnlyY) {
            var heightSnap = nearestSnapValue(measuredH, SNAP_HEIGHTS);
            card.style.width = "";
            card.style.maxWidth = "";
            card.style.height = heightSnap + "px";
            card.classList.remove("is-snapping-drag");
            card.removeAttribute("data-snap-hint");
            savedLayout[key] = { span: cardSpanValue(card), height: heightSnap };
            writeGridLayoutPrefs(savedLayout);
            if (canvasEl) schedulePlotlyResize(canvasEl);
          } else {
            snapCardToGrid(card, key, measuredW, measuredH);
          }
        }

        handle.addEventListener("pointerup", endHandleDrag);
        handle.addEventListener("pointercancel", endHandleDrag);

        if (titleEl) {
          titleEl.title =
            "Drag bottom-right corner to snap to 12-col grid; click [6/12] to resize width; double-click title to reset";
          titleEl.addEventListener("dblclick", function () {
            card.style.width = "";
            card.style.maxWidth = "";
            card.style.height = "";
            card.classList.remove("is-snapping-drag");
            card.removeAttribute("data-snap-hint");
            applyCardSpanClass(
              card,
              card.dataset.defaultSpan ? Number(card.dataset.defaultSpan) : null
            );
            updateCardSpanButton(card);
            delete savedLayout[key];
            writeGridLayoutPrefs(savedLayout);
            if (canvasEl) {
              schedulePlotlyResize(canvasEl);
              window.setTimeout(function () {
                schedulePlotlyResize(canvasEl);
              }, 60);
            }
          });
        }
      })(cards[i], i);
    }
  }

  // ------------------------------------------------------------------ Init
  function init() {
    updateThemeToggleLabel();
    restoreWallsPreferences();
    bindEvents();
    initChartResizeHandling();
    fetchStatus();
    loadChain(state.ticker, "");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
