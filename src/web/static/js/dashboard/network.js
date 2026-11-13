/* IBKR Options Analyzer dashboard — Network requests: status, chain loading, refresh and snapshot fetches. */

import {
  bootstrap,
  dom,
  state,
} from "./state.js";
import {
  applyTickerPullPrefsToControls,
  buildActivePullScopeText,
  configuredDefaultMdType,
  configuredFallbackMdType,
  configuredMarketDataMode,
  extractApiErrorMessage,
  gatewayAddressLabel,
  resetControlCapabilities,
  setActiveTickerUI,
  showBanner,
  syncControlsFromMeta,
  updateExportLink,
  writeTickerPullPrefs,
} from "./helpers.js";
import {
  renderChainBadges,
  renderKpis,
  renderStatusBadges,
} from "./badges.js";
import {
  populateWallsDteFilter,
  purgeCharts,
} from "./charts.js";
import {
  renderAllCharts,
} from "./panels.js";
import {
  populateDteFilter,
  populateTableHeaderFilters,
  renderTable,
} from "./chain_table.js";
import {
  renderSnapshotSelect,
} from "./snapshots.js";
import {
  maybeAutoPopulateStrategy,
  syncStrategySelectors,
} from "./strategy_lab.js";
import {
  stopCorridorStream,
  syncCorridorFromChain,
} from "./corridor.js";
import {
  backtestOnSnapshotsLoaded,
} from "./backtest.js";
import {
  loadPaperTrades,
} from "./backtest_paper.js";

// ------------------------------------------------------------------ Network Requests
export async function apiJson(url, options) {
  var opts = options ? Object.assign({}, options) : {};
  var timeoutMs = Number(opts.timeoutMs || 0);
  delete opts.timeoutMs;

  var timerId = null;
  var timedOut = false;
  var controller = null;
  if (timeoutMs > 0 && typeof AbortController !== "undefined" && !opts.signal) {
    controller = new AbortController();
    opts.signal = controller.signal;
    timerId = window.setTimeout(function () {
      timedOut = true;
      try {
        controller.abort();
      } catch (_abortErr) {
        /* ignore */
      }
    }, timeoutMs);
  }

  var startedAt = Date.now();
  try {
    var resp = await window.fetch(url, opts);
    var elapsedMs = Date.now() - startedAt;
    var rawText = "";
    var body = null;
    if (typeof resp.text === "function") {
      try {
        rawText = await resp.text();
        if (rawText && rawText.trim()) {
          body = JSON.parse(rawText);
        }
      } catch (_parseErr) {
        body = null;
      }
    } else if (typeof resp.json === "function") {
      try {
        body = await resp.json();
      } catch (_jsonErr) {
        body = null;
      }
    }
    var snippet = rawText && body === null ? rawText.trim().slice(0, 280) : "";
    return {
      ok: resp.ok,
      status: resp.status,
      body: body,
      textSnippet: snippet,
      elapsedMs: elapsedMs,
      timedOut: timedOut,
    };
  } catch (err) {
    if (err && typeof err === "object") {
      err.elapsedMs = Date.now() - startedAt;
      err.timedOut = timedOut || err.name === "AbortError";
    }
    throw err;
  } finally {
    if (timerId !== null) {
      window.clearTimeout(timerId);
    }
  }
}

export async function fetchStatus(tickerOverride) {
  var targetTicker = tickerOverride || state.ticker;
  var url = "/api/status";
  if (targetTicker) {
    url += "?ticker=" + encodeURIComponent(targetTicker);
  }
  try {
    var res = await apiJson(url);
    if (res.ok && res.body) {
      renderStatusBadges(res.body);
    }
  } catch (_err) {
    renderStatusBadges({
      connected: false,
      host: bootstrap.ibkrHost || "127.0.0.1",
      port: bootstrap.ibkrPort || 4001,
      market_data_mode: configuredMarketDataMode(),
      default_market_data_type: configuredDefaultMdType(),
      fallback_market_data_type: configuredFallbackMdType(),
      active_market_data_type: configuredDefaultMdType(),
    });
  }
}

async function fetchSnapshots(ticker) {
  var res = await apiJson("/api/snapshots/" + encodeURIComponent(ticker));
  if (res.ok && res.body) {
    renderSnapshotSelect(res.body);
    backtestOnSnapshotsLoaded(ticker);
    return;
  }
  renderSnapshotSelect({ snapshots: [] });
  backtestOnSnapshotsLoaded(ticker);
}

export async function loadChain(ticker, snapshotId) {
  if (state.corridorStreaming || state.corridorStreamSource) {
    stopCorridorStream({ silent: true });
  }
  var prevTicker = state.ticker;
  state.ticker = ticker;
  state.snapshotId = snapshotId || "";
  setActiveTickerUI(ticker);
  if (ticker !== prevTicker) {
    applyTickerPullPrefsToControls(ticker);
  }

  await fetchSnapshots(ticker);
  var url = "/api/chain/" + encodeURIComponent(ticker);
  if (snapshotId) {
    url += "?snapshot_id=" + encodeURIComponent(snapshotId);
  }

  var res;
  try {
    res = await apiJson(url);
  } catch (err) {
    showBanner("error", "Unable to load option chain for " + ticker + ".", {
      ticker: ticker,
      phase: "CHAIN_LOAD",
      elapsedMs: err && err.elapsedMs,
      detail: err && err.message ? err.message : String(err || ""),
    });
    return;
  }

  if (res.ok && res.body) {
    try {
      state.chainData = res.body;
      state.snapshotId = res.body.meta.snapshot_id;
      updateExportLink();
      syncControlsFromMeta(res.body.meta);
      renderChainBadges(res.body.meta);
      renderKpis(res.body);
      populateDteFilter(res.body);
      populateTableHeaderFilters(res.body);
      populateWallsDteFilter(res.body);
      syncStrategySelectors(res.body);
      syncCorridorFromChain(res.body);
      renderAllCharts(res.body);
      renderTable(res.body);
      maybeAutoPopulateStrategy(res.body);

      if (res.body.meta.notices && res.body.meta.notices.length) {
        showBanner("info", res.body.meta.notices.join(" "), {
          ticker: ticker,
          phase: "CHAIN_LOAD",
          elapsedMs: res.elapsedMs,
        });
      } else {
        showBanner("", "");
      }
    } catch (renderErr) {
      if (window.console && window.console.error) {
        window.console.error(renderErr);
      }
      var renderMsg =
        renderErr && renderErr.message ? renderErr.message : String(renderErr || "Render error");
      showBanner("warn", "Snapshot loaded, UI render warning: " + renderMsg, {
        ticker: ticker,
        phase: "UI_RENDER",
        elapsedMs: res.elapsedMs,
        detail: renderErr && renderErr.stack ? String(renderErr.stack).slice(0, 400) : renderMsg,
      });
    }
    return;
  }

  if (res.status === 404) {
    state.chainData = null;
    state.strategyLoadedTicker = "";
    resetControlCapabilities();
    applyTickerPullPrefsToControls(ticker);
    await fetchStatus(ticker);
    purgeCharts();
    renderChainBadges(null);
    renderKpis(null);
    populateDteFilter(null);
    populateTableHeaderFilters(null);
    populateWallsDteFilter(null);
    syncStrategySelectors(null);
    syncCorridorFromChain(null);
    renderTable(null);
    showBanner(
      "info",
      "No cached snapshot exists for " +
        ticker +
        ". Click REFRESH FROM IBKR to pull and archive live/delayed chain data.",
      { ticker: ticker, phase: "CHAIN_LOAD", status: 404, elapsedMs: res.elapsedMs }
    );
    return;
  }

  var errMsg = extractApiErrorMessage(
    res.body,
    res.textSnippet || "Unable to load option chain for " + ticker + "."
  );
  showBanner("error", errMsg, {
    ticker: ticker,
    phase: "CHAIN_LOAD",
    status: res.status,
    errorCode: res.body && res.body.error_code,
    elapsedMs: res.elapsedMs,
    detail: res.body && res.body.detail ? String(res.body.detail) : "",
    rawBody: res.textSnippet,
  });
}

export async function refreshFromIBKR() {
  if (state.loading) return;
  if (state.corridorStreaming || state.corridorStreamSource) {
    stopCorridorStream({ silent: true });
  }
  state.loading = true;
  if (dom.refreshBtn) {
    dom.refreshBtn.disabled = true;
    dom.refreshBtn.textContent = "REFRESHING...";
  }

  var rangeVal = dom.rangeSelect ? Number(dom.rangeSelect.value) : 0.2;
  var stepRaw = dom.stepSelect ? dom.stepSelect.value : "auto";
  var stepVal = stepRaw === "auto" ? null : Number(stepRaw);
  var dteMaxVal = dom.dteMaxSelect ? Number(dom.dteMaxSelect.value) : 120;
  var pullScope = buildActivePullScopeText();
  writeTickerPullPrefs(state.ticker);

  try {
    // Stage 1: HTTP / IBKR Gateway Pull
    var res;
    try {
      res = await apiJson("/api/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        timeoutMs: 120000,
        body: JSON.stringify({
          ticker: state.ticker,
          range_pct: rangeVal,
          strike_step: stepVal,
          dte_min:
            state.status && state.status.dte_min !== null && state.status.dte_min !== undefined
              ? Number(state.status.dte_min)
              : bootstrap.dteMin !== null && bootstrap.dteMin !== undefined
                ? Number(bootstrap.dteMin)
                : 30,
          dte_max: dteMaxVal,
        }),
      });
    } catch (httpErr) {
      await fetchStatus();
      var rawErrMsg =
        httpErr && httpErr.message ? httpErr.message : String(httpErr || "Request aborted");
      var isTimeout = Boolean(httpErr && httpErr.timedOut);
      var bannerTitle = isTimeout
        ? "IBKR refresh timed out after 120s. Preserving cached snapshot view."
        : "Network or gateway interruption during refresh (" +
          rawErrMsg +
          "). Preserving cached snapshot view.";
      showBanner("warn", bannerTitle, {
        ticker: state.ticker,
        pullScope: pullScope,
        phase: isTimeout ? "TIMEOUT" : "REFRESH_HTTP",
        elapsedMs: httpErr && httpErr.elapsedMs,
        detail: rawErrMsg,
        hint: isTimeout
          ? "Try narrowing Range %, raising Strike Step ($5/$10), or lowering Max DTE before retrying."
          : "Verify IBKR Gateway (" +
            gatewayAddressLabel() +
            ") and FastAPI server availability; cached snapshot remains active.",
      });
      return;
    }

    if (!res.ok || !res.body) {
      await fetchStatus();
      var fallbackMsg =
        res.status === 503
          ? "IBKR Gateway (" +
            gatewayAddressLabel() +
            ") is offline or unreachable. Preserving cached snapshot view."
          : "Refresh failed (HTTP " + res.status + "). Preserving cached snapshot view.";
      var apiMsg = extractApiErrorMessage(res.body, res.textSnippet || fallbackMsg);
      showBanner(res.status === 503 ? "warn" : "error", apiMsg, {
        ticker: state.ticker,
        pullScope: pullScope,
        phase: res.status === 503 ? "GATEWAY" : "REFRESH_HTTP",
        status: res.status,
        errorCode: res.body && res.body.error_code,
        elapsedMs: res.elapsedMs,
        detail: res.body && res.body.detail ? String(res.body.detail) : "",
        rawBody: res.textSnippet,
        hint:
          res.status === 503
            ? "Check IBKR Gateway connection on " +
              gatewayAddressLabel() +
              "; cached view preserved."
            : "Preserving previous cached snapshot view.",
      });
      return;
    }

    // Stage 2: Post-Refresh DOM / Plotly Render
    try {
      state.chainData = res.body;
      state.snapshotId = res.body.meta.snapshot_id;
      updateExportLink();
      await fetchSnapshots(state.ticker);
      await fetchStatus();
      loadPaperTrades();
      syncControlsFromMeta(res.body.meta, { fromRefresh: true });
      renderChainBadges(res.body.meta);
      renderKpis(res.body);
      populateDteFilter(res.body);
      populateTableHeaderFilters(res.body);
      populateWallsDteFilter(res.body);
      syncStrategySelectors(res.body);
      syncCorridorFromChain(res.body);
      renderAllCharts(res.body);
      renderTable(res.body);
      maybeAutoPopulateStrategy(res.body);
      var msg =
        res.body.meta.notices && res.body.meta.notices.length
          ? res.body.meta.notices.join(" ")
          : res.body.meta.exchange_native_verified &&
              res.body.meta.exchange_validated_strike_count
            ? "Archived IBKR snapshot " +
              res.body.meta.snapshot_id +
              " (" +
              res.body.meta.exchange_validated_strike_count +
              " exchange-validated strikes)."
            : "Archived new IBKR snapshot " + res.body.meta.snapshot_id + ".";
      showBanner("info", msg, {
        ticker: state.ticker,
        pullScope: pullScope,
        phase: "REFRESH_OK",
        elapsedMs: res.elapsedMs,
      });
    } catch (renderErr) {
      if (window.console && window.console.error) {
        window.console.error(renderErr);
      }
      var renderErrMsg =
        renderErr && renderErr.message ? renderErr.message : String(renderErr || "Render error");
      showBanner("warn", "Snapshot saved, UI render warning: " + renderErrMsg, {
        ticker: state.ticker,
        pullScope: pullScope,
        phase: "UI_RENDER",
        elapsedMs: res.elapsedMs,
        detail:
          renderErr && renderErr.stack ? String(renderErr.stack).slice(0, 400) : renderErrMsg,
        hint: "The CSV snapshot was archived on disk; reload or adjust panel view if charts look stale.",
      });
    }
  } finally {
    state.loading = false;
    if (dom.refreshBtn) {
      dom.refreshBtn.disabled = false;
      dom.refreshBtn.textContent = "REFRESH FROM IBKR";
    }
  }
}
