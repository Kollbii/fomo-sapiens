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
    };
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
        " contracts (" +
        meta.market_date +
        ")"
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
          fmtPct(cw.distance_from_spot_pct, 1) +
          " vs spot)"
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
          fmtPct(pw.distance_from_spot_pct, 1) +
          " vs spot)"
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

    if (dom.wallsBasisLabel) {
      dom.wallsBasisLabel.textContent = "BASIS: " + walls.primary_basis.replace("_", " ");
    }
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
  }

  function makeVerticalMarker(xVal, label, color, yPos, dash) {
    if (xVal === null || xVal === undefined || Number.isNaN(Number(xVal))) {
      return { shape: null, annotation: null };
    }
    return {
      shape: {
        type: "line",
        x0: xVal,
        x1: xVal,
        y0: 0,
        y1: 1,
        yref: "paper",
        line: { color: color, width: 1.5, dash: dash || "dash" },
      },
      annotation: {
        x: xVal,
        y: yPos !== undefined ? yPos : 0.99,
        yref: "paper",
        text: label,
        showarrow: false,
        font: { size: 10, color: color },
        bgcolor: "rgba(0,0,0,0)",
        yanchor: "top",
      },
    };
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
      paper_bgcolor: t.bg,
      plot_bgcolor: t.bg,
      font: { family: t.fontMono, size: 11, color: t.ink },
      margin: { t: 22, r: 16, b: 42, l: 54 },
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
    };
  }

  function renderWallsChart(data, t) {
    var el = document.getElementById("chart-walls");
    if (!el || typeof window.Plotly === "undefined") return;

    var useVolume = data.walls.primary_basis === "VOLUME";
    var byStrike = {};
    var buckets = data.profiles.buckets || [];
    for (var i = 0; i < buckets.length; i++) {
      var rows = buckets[i].strikes || [];
      for (var j = 0; j < rows.length; j++) {
        var row = rows[j];
        var key = Number(row.strike).toFixed(2);
        if (!byStrike[key]) {
          byStrike[key] = { strike: Number(row.strike), callVal: 0, putVal: 0 };
        }
        byStrike[key].callVal += useVolume ? (row.call_volume || 0) : (row.call_oi || 0);
        byStrike[key].putVal += useVolume ? (row.put_volume || 0) : (row.put_oi || 0);
      }
    }

    var sorted = Object.keys(byStrike)
      .map(function (k) {
        return byStrike[k];
      })
      .sort(function (a, b) {
        return a.strike - b.strike;
      });

    var strikes = sorted.map(function (s) {
      return s.strike;
    });
    var calls = sorted.map(function (s) {
      return s.callVal;
    });
    var puts = sorted.map(function (s) {
      return -s.putVal;
    });

    var markers = [
      makeVerticalMarker(data.meta.underlying_price, "SPOT", t.ink, 0.99, "dash"),
    ];
    if (data.walls.aggregate_primary_call_wall) {
      markers.push(
        makeVerticalMarker(
          data.walls.aggregate_primary_call_wall.strike,
          "CALL WALL",
          t.call,
          0.88,
          "dot"
        )
      );
    }
    if (data.walls.aggregate_primary_put_wall) {
      markers.push(
        makeVerticalMarker(
          data.walls.aggregate_primary_put_wall.strike,
          "PUT WALL",
          t.put,
          0.14,
          "dot"
        )
      );
    }

    var yAxisTitle = useVolume ? "Volume (+Call / -Put)" : "Open Interest (+Call / -Put)";
    var traces = [
      {
        type: "bar",
        name: "Calls",
        x: strikes,
        y: calls,
        marker: { color: t.call },
      },
      {
        type: "bar",
        name: "Puts",
        x: strikes,
        y: puts,
        marker: { color: t.put },
      },
    ];
    var layout = baseLayout(t, "Strike ($)", yAxisTitle, markers);
    layout.barmode = "relative";

    window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
  }

  function renderIvSmileChart(data, t) {
    var el = document.getElementById("chart-iv-smile");
    if (!el || typeof window.Plotly === "undefined") return;

    var buckets = data.profiles.buckets || [];
    var traces = [];

    for (var i = 0; i < buckets.length; i++) {
      var b = buckets[i];
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
        name: b.target_dte_bucket + "D (" + b.dte + "d)",
        x: xs,
        y: ys,
        line: { color: t.dtePalette[i % t.dtePalette.length], width: 2 },
        marker: { size: 5 },
      });
    }

    var markers = [
      makeVerticalMarker(data.meta.underlying_price, "SPOT", t.ink, 0.99, "dash"),
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
      makeVerticalMarker(data.meta.underlying_price, "SPOT", t.ink, 0.99, "dash"),
    ];
    var layout = baseLayout(t, "Strike ($)", "Contract Volume", markers);
    layout.hovermode = "closest";
    window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
  }

  function renderGreeksChart(data, t) {
    var el = document.getElementById("chart-greeks");
    if (!el || typeof window.Plotly === "undefined") return;

    var metric = dom.greeksMetricSelect ? dom.greeksMetricSelect.value : "gamma";
    var buckets = data.profiles.buckets || [];
    var traces = [];

    for (var i = 0; i < buckets.length; i++) {
      var b = buckets[i];
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
        name: b.target_dte_bucket + "D (" + b.dte + "d)",
        x: xs,
        y: ys,
        line: { color: t.dtePalette[i % t.dtePalette.length], width: 2 },
        marker: { size: 4 },
      });
    }

    var markers = [
      makeVerticalMarker(data.meta.underlying_price, "SPOT", t.ink, 0.99, "dash"),
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
    if (!data || !data.meta || !data.meta.target_dte_buckets) return;
    var buckets = data.meta.target_dte_buckets;
    for (var i = 0; i < buckets.length; i++) {
      var opt = document.createElement("option");
      opt.value = String(buckets[i]);
      opt.textContent = buckets[i] + "D Bucket";
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
      if (dteFilter !== "ALL" && String(c.target_dte_bucket) !== dteFilter) continue;
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
          " (" +
          escapeHtml(cRow.target_dte_bucket) +
          "D)</td>" +
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
          fmtPct(cRow.moneyness_pct, 1) +
          "</td>" +
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
      opt.textContent =
        snap.snapshot_id +
        " (" +
        snap.market_date +
        " \u2022 T" +
        snap.market_data_type +
        " \u2022 " +
        snap.contract_count +
        "c)";
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
      renderChainBadges(res.body.meta);
      renderKpis(res.body);
      populateDteFilter(res.body);
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
        renderChainBadges(res.body.meta);
        renderKpis(res.body);
        populateDteFilter(res.body);
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

    if (dom.greeksMetricSelect) {
      dom.greeksMetricSelect.addEventListener("change", function () {
        if (state.chainData) {
          renderGreeksChart(state.chainData, themeVars());
        }
      });
    }

    [dom.filterDte, dom.filterRight, dom.filterUnusual].forEach(function (ctrl) {
      if (ctrl) {
        ctrl.addEventListener("change", function () {
          renderTable(state.chainData);
        });
      }
    });
  }

  // ------------------------------------------------------------------ Init
  function init() {
    updateThemeToggleLabel();
    bindEvents();
    fetchStatus();
    loadChain(state.ticker, "");
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
