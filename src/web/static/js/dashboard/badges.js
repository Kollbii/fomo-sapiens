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
    dom.gatewayBadge.className = "badge " + (connected ? "badge--online" : "badge--offline");
    dom.gatewayBadge.innerHTML =
      '<span class="badge-dot" aria-hidden="true"></span><span>' +
      (connected ? "GATEWAY ONLINE" : "GATEWAY OFFLINE (CACHE ONLY)") +
      "</span>";
    dom.gatewayBadge.title = "IBKR Gateway target: " + gatewayAddressLabel();
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
}

// ------------------------------------------------------------------ KPI Strip
function setKpi(cardEl, valueText, subText) {
  if (!cardEl) return;
  var valEl = cardEl.querySelector(".kpi-value");
  var subEl = cardEl.querySelector(".kpi-sub");
  if (valEl) valEl.textContent = valueText;
  if (subEl) subEl.textContent = subText;
}

export function renderKpis(data) {
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
