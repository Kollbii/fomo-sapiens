/* IBKR Options Analyzer dashboard — Masthead badges, editorial banner copy and KPI strip rendering. */

import {
  bootstrap,
  dom,
  state,
} from "./state.js";
import {
  activeStatusMdType,
  applyMdTypeBadge,
  configuredDefaultMdType,
  configuredDteWindowLabel,
  configuredFallbackMdType,
  configuredFeedBannerSummary,
  configuredMarketDataMode,
  fmtInt,
  fmtMoney,
  fmtNum,
  formatPullScopeSummary,
  gatewayAddressLabel,
  readTickerPullPrefs,
  syncControlCapabilities,
} from "./helpers.js";

// ------------------------------------------------------------------ Badges
export function renderStatusBadges(statusPayload) {
  state.status = statusPayload;
  if (dom.gatewayBadge) {
    var connected = Boolean(statusPayload && statusPayload.connected);
    var connState =
      statusPayload && typeof statusPayload.connection_state === "string"
        ? statusPayload.connection_state
        : "offline";
    var connNotice =
      statusPayload && typeof statusPayload.connection_notice === "string"
        ? statusPayload.connection_notice.trim()
        : "";
    var badgeClass = "badge--offline";
    var badgeText = "GATEWAY OFFLINE (CACHE ONLY)";
    var badgeTitle = "IBKR Gateway target: " + gatewayAddressLabel();

    if (connected) {
      badgeClass = "badge--online";
      badgeText = "GATEWAY ONLINE";
    } else if (connState === "timeout_reset") {
      badgeClass = "badge--amber";
      badgeText = "GATEWAY TIMEOUT \u2022 SOCKET RESET";
      badgeTitle =
        (connNotice ||
          "Socket recycled after IBKR Gateway time-out; auto-reconnects on next pull.") +
        " (" +
        gatewayAddressLabel() +
        ")";
    } else if (connState === "connection_lost") {
      badgeClass = "badge--offline";
      badgeText = "GATEWAY LINK LOST";
      badgeTitle =
        (connNotice || "IBKR Gateway connection lost.") + " (" + gatewayAddressLabel() + ")";
    }

    dom.gatewayBadge.className = "badge " + badgeClass;
    dom.gatewayBadge.innerHTML =
      '<span class="badge-dot" aria-hidden="true"></span><span>' + badgeText + "</span>";
    dom.gatewayBadge.title = badgeTitle;
  }
  if (dom.mdTypeBadge && statusPayload) {
    var md =
      statusPayload.active_market_data_type !== null &&
      statusPayload.active_market_data_type !== undefined
        ? statusPayload.active_market_data_type
        : statusPayload.default_market_data_type !== null &&
            statusPayload.default_market_data_type !== undefined
          ? statusPayload.default_market_data_type
          : configuredDefaultMdType();
    applyMdTypeBadge(md);
    var subNote = statusPayload.live_subscription_missing
      ? " • Gateway reported missing live subscription; using Delayed fallback"
      : "";
    dom.mdTypeBadge.title =
      (statusPayload.connected ? "Active connection feed (" : "Configured feed (offline, ") +
      gatewayAddressLabel() +
      "): Mode " +
      configuredMarketDataMode() +
      ", Default Type " +
      configuredDefaultMdType() +
      ", Fallback Type " +
      configuredFallbackMdType() +
      subNote;
  }
  if (
    dom.includeShortDteToggle &&
    statusPayload &&
    typeof statusPayload.include_short_dte_for_backtest === "boolean"
  ) {
    var savedPrefs = readTickerPullPrefs(state.ticker);
    if (!savedPrefs || typeof savedPrefs.include_short_dte !== "boolean") {
      dom.includeShortDteToggle.checked = statusPayload.include_short_dte_for_backtest;
    }
  }
  if (statusPayload && !state.chainData) {
    syncControlCapabilities(statusPayload);
    renderChainBadges(null);
  } else if (state.chainData && state.chainData.meta) {
    renderChainBadges(state.chainData.meta);
  }
}

export function renderChainBadges(meta) {
  if (dom.mdTypeBadge) {
    if (
      state.status &&
      (state.status.active_market_data_type !== null &&
        state.status.active_market_data_type !== undefined)
    ) {
      applyMdTypeBadge(state.status.active_market_data_type);
    } else if (bootstrap.defaultMarketDataType) {
      applyMdTypeBadge(bootstrap.defaultMarketDataType);
    } else if (meta && meta.market_data_type) {
      applyMdTypeBadge(meta.market_data_type);
    }
  }
  if (dom.editorialBannerCopy) {
    if (!meta) {
      dom.editorialBannerCopy.textContent =
        "Immutable CSV Archive \u2022 " +
        configuredFeedBannerSummary() +
        " \u2022 Weekly Expirations (" +
        configuredDteWindowLabel() +
        " DTE)";
    } else {
      var dteSpanTxt =
        meta.dtes && meta.dtes.length
          ? meta.dtes[0] +
            "\u2013" +
            meta.dtes[meta.dtes.length - 1] +
            "d (" +
            meta.dtes.length +
            " wk exp)"
          : configuredDteWindowLabel() + " DTE";
      var verifiedSuffix =
        meta.exchange_native_verified && meta.exchange_validated_strike_count
          ? " \u2022 " + meta.exchange_validated_strike_count + " IBKR strikes"
          : "";
      var snapMd =
        meta.market_data_type !== null && meta.market_data_type !== undefined
          ? Number(meta.market_data_type)
          : null;
      var activeMd = activeStatusMdType();
      var isOffline = !(state.status && state.status.connected);
      var snapFeedSuffix =
        snapMd && (isOffline || snapMd !== activeMd) ? " \u2022 Snap T" + snapMd : "";
      dom.editorialBannerCopy.textContent =
        meta.underlying +
        " (" +
        meta.market_date +
        ") \u2022 Pulled Scope: " +
        formatPullScopeSummary(meta) +
        " \u2022 Expirations: " +
        dteSpanTxt +
        verifiedSuffix +
        snapFeedSuffix;
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
  syncStickyContextBar(state.chainData);
}

// ------------------------------------------------------------------ KPI Strip Helpers
export function formatWallBasisShort(basis) {
  var key = String(basis || "OPEN_INTEREST").trim().toUpperCase();
  if (key === "OPEN_INTEREST") return "OI";
  if (key === "VOLUME") return "Vol";
  return String(basis || "OI");
}

export function formatKpiContractScope(meta, snapshots) {
  if (!meta) return "\u2014";
  var viewCount = Number(meta.contract_count || 0);
  var fullCount = null;
  if (Array.isArray(snapshots) && snapshots.length) {
    var byId = null;
    var byDate = null;
    for (var i = 0; i < snapshots.length; i++) {
      var s = snapshots[i];
      if (!s) continue;
      if (meta.snapshot_id && s.snapshot_id === meta.snapshot_id) {
        byId = s;
        break;
      }
      if (!byDate && meta.market_date && s.market_date === meta.market_date) {
        byDate = s;
      }
    }
    var matched = byId || byDate;
    if (matched && Number.isFinite(Number(matched.contract_count))) {
      fullCount = Number(matched.contract_count);
    }
  }
  if (fullCount !== null && fullCount > viewCount && viewCount > 0) {
    return viewCount + "/" + fullCount + "c";
  }
  return viewCount + "c";
}

export function computeCorridorSpotPct(spot, putStrike, callStrike) {
  var s = Number(spot);
  var p = Number(putStrike);
  var c = Number(callStrike);
  if (
    spot === null ||
    spot === undefined ||
    putStrike === null ||
    putStrike === undefined ||
    callStrike === null ||
    callStrike === undefined ||
    !Number.isFinite(s) ||
    !Number.isFinite(p) ||
    !Number.isFinite(c) ||
    c <= p
  ) {
    return { pinPct: 50, rawPct: null, valid: false };
  }
  var rawPct = ((s - p) / (c - p)) * 100;
  var pinPct = Math.max(4, Math.min(96, Math.round(rawPct * 10) / 10));
  return { pinPct: pinPct, rawPct: rawPct, valid: true };
}

export function formatTopAnomalySummary(anomaliesReport) {
  var list =
    anomaliesReport && Array.isArray(anomaliesReport.anomalies) ? anomaliesReport.anomalies : [];
  if (!list.length) {
    var total = anomaliesReport && anomaliesReport.total_flagged ? anomaliesReport.total_flagged : 0;
    return total ? total + " flagged contracts" : "0 flagged contracts";
  }
  var top = list[0];
  var strikeNum = Number(top.strike);
  var strikeStr = Number.isFinite(strikeNum)
    ? "$" + fmtNum(strikeNum, strikeNum % 1 ? 2 : 0)
    : "\u2014";
  var dteStr = top.dte !== null && top.dte !== undefined ? " (" + top.dte + "d)" : "";
  var ratioNum =
    top.vol_oi_ratio !== null && top.vol_oi_ratio !== undefined ? Number(top.vol_oi_ratio) : NaN;
  var metricStr =
    Number.isFinite(ratioNum) && ratioNum > 0
      ? fmtNum(ratioNum, 1) + "\u00D7 Vol/OI"
      : "Z " + fmtNum(top.volume_zscore, 1);
  return "Top: " + strikeStr + (top.right || "") + dteStr + " \u2022 " + metricStr;
}

// ------------------------------------------------------------------ KPI Strip
function setKpi(cardEl, valueText, subText) {
  if (!cardEl) return;
  var valEl = cardEl.querySelector(".kpi-value");
  var subEl = cardEl.querySelector(".kpi-sub");
  if (valEl) valEl.textContent = valueText;
  if (subEl) subEl.textContent = subText;
}

export function syncStickyContextBar(data) {
  if (dom.stickyTicker) {
    dom.stickyTicker.textContent = state.ticker || "NVDA";
  }
  if (!data || !data.meta) {
    if (dom.stickySpot) dom.stickySpot.textContent = "\u2014";
    if (dom.stickyWalls) dom.stickyWalls.textContent = "CW \u2014 | PW \u2014";
    if (dom.stickySnap) dom.stickySnap.textContent = "NO CACHE";
    return;
  }
  var meta = data.meta;
  var walls = data.walls || {};
  if (dom.stickySpot) {
    dom.stickySpot.textContent = fmtMoney(meta.underlying_price);
  }
  if (dom.stickyWalls) {
    var cw = walls.aggregate_primary_call_wall;
    var pw = walls.aggregate_primary_put_wall;
    var cwText = cw ? "$" + fmtNum(cw.strike, cw.strike % 1 ? 2 : 0) : "\u2014";
    var pwText = pw ? "$" + fmtNum(pw.strike, pw.strike % 1 ? 2 : 0) : "\u2014";
    dom.stickyWalls.textContent = "CW " + cwText + " | PW " + pwText;
  }
  if (dom.stickySnap) {
    var mdTag =
      meta.market_data_type !== null && meta.market_data_type !== undefined
        ? " \u2022 T" + meta.market_data_type
        : "";
    dom.stickySnap.textContent = String(meta.market_date || "CACHE") + mdTag;
  }
}

export function renderKpis(data) {
  if (!data) {
    setKpi(dom.kpiSpot, "\u2014", "No cached snapshot for " + state.ticker);
    setKpi(dom.kpiPutWall, "\u2014", "Click REFRESH FROM IBKR");
    setKpi(dom.kpiCallWall, "\u2014", "Click REFRESH FROM IBKR");
    setKpi(dom.kpiAnomalies, "\u2014", "0 Vol>OI");
    if (dom.kpiCorridorCaption) dom.kpiCorridorCaption.textContent = "OI / Vol structure";
    if (dom.kpiCorridorPin && dom.kpiCorridorPin.style) dom.kpiCorridorPin.style.left = "50%";
    if (dom.kpiAnomalyTop) dom.kpiAnomalyTop.textContent = "0 flagged contracts";
    syncStickyContextBar(null);
    return;
  }

  var meta = data.meta;
  var walls = data.walls || {};
  var anomalies = data.anomalies || {};
  var basisShort = formatWallBasisShort(walls.primary_basis);

  setKpi(
    dom.kpiSpot,
    fmtMoney(meta.underlying_price),
    meta.underlying +
      " \u2022 " +
      formatKpiContractScope(meta, state.snapshots) +
      " \u2022 " +
      formatPullScopeSummary(meta) +
      " \u2022 " +
      meta.market_date
  );

  var pw = walls.aggregate_primary_put_wall;
  if (pw) {
    var pwSign = pw.distance_from_spot_pct >= 0 ? "+" : "";
    setKpi(
      dom.kpiPutWall,
      "$" + fmtNum(pw.strike, pw.strike % 1 ? 2 : 0),
      pwSign +
        fmtNum(pw.distance_from_spot_pct, 1) +
        "% \u2022 " +
        basisShort +
        " " +
        fmtInt(pw.metric_value)
    );
  } else {
    setKpi(dom.kpiPutWall, "\u2014", "Insufficient Put " + basisShort);
  }

  var cw = walls.aggregate_primary_call_wall;
  if (cw) {
    var cwSign = cw.distance_from_spot_pct >= 0 ? "+" : "";
    setKpi(
      dom.kpiCallWall,
      "$" + fmtNum(cw.strike, cw.strike % 1 ? 2 : 0),
      cwSign +
        fmtNum(cw.distance_from_spot_pct, 1) +
        "% \u2022 " +
        basisShort +
        " " +
        fmtInt(cw.metric_value)
    );
  } else {
    setKpi(dom.kpiCallWall, "\u2014", "Insufficient Call " + basisShort);
  }

  var pos = computeCorridorSpotPct(
    meta.underlying_price,
    pw ? pw.strike : null,
    cw ? cw.strike : null
  );
  if (dom.kpiCorridorPin && dom.kpiCorridorPin.style) {
    dom.kpiCorridorPin.style.left = pos.pinPct + "%";
  }
  var cpRatio = null;
  if (walls.primary_basis === "VOLUME") {
    if (Number(walls.total_put_volume || 0) > 0) {
      cpRatio = Number(walls.total_call_volume || 0) / Number(walls.total_put_volume);
    }
  } else if (Number(walls.total_put_oi || 0) > 0) {
    cpRatio = Number(walls.total_call_oi || 0) / Number(walls.total_put_oi);
  }
  if (dom.kpiCorridorCaption) {
    var capParts = [];
    if (pos.valid) {
      capParts.push("Spot " + Math.round(pos.rawPct) + "% of range");
    }
    if (cpRatio !== null && Number.isFinite(cpRatio)) {
      capParts.push("C/P " + basisShort + " " + fmtNum(cpRatio, 2) + "\u00D7");
    }
    dom.kpiCorridorCaption.textContent = capParts.length
      ? capParts.join(" \u2022 ")
      : basisShort + " structure";
  }

  setKpi(
    dom.kpiAnomalies,
    String(anomalies.total_flagged || 0),
    (anomalies.vol_gt_oi_count || 0) +
      " Vol>OI \u2022 " +
      (anomalies.high_zscore_count || 0) +
      " High Z"
  );
  if (dom.kpiAnomalyTop) {
    dom.kpiAnomalyTop.textContent = formatTopAnomalySummary(anomalies);
  }
  syncStickyContextBar(data);
}
