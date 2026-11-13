/* IBKR Options Analyzer dashboard — DOM event bindings for controls, panels, table and Strategy Lab. */

import {
  DEFAULT_PANEL_VISIBILITY,
  STRATEGY_AUTO_POPULATE_KEY,
  cloneDefaultPanelVisibility,
  dom,
  state,
} from "./state.js";
import {
  clearAlertBanner,
  currentTheme,
  isStrategyAutoPopulateEnabled,
  syncAutoFollowOptionLabels,
  themeVars,
  updateThemeToggleLabel,
  writePref,
  writeTickerPullPrefs,
} from "./helpers.js";
import {
  renderGreeksChart,
  renderWallsChart,
  syncWallsSplitCheckboxState,
} from "./charts.js";
import {
  applyPanelVisibility,
  isPanelVisible,
  renderAllCharts,
} from "./panels.js";
import {
  TABLE_COLUMN_BY_KEY,
  renderTable,
  resetAllTableFilters,
  syncDteBucketTabs,
  syncTableHeaderSortUI,
  updateHeaderFilterHighlights,
  writeTableSortPref,
} from "./chain_table.js";
import {
  addCustomStrategyLegRow,
  addSpecificStrategyLeg,
  autoBuildStrategy,
  buildSeedLegsFromChain,
  evaluateStrategyRequest,
  maybeAutoPopulateStrategy,
  renderStrategyLegsTable,
  renderStrategySlidersMax,
  resetStrategyEvaluationUI,
  setActiveStrategyPresetUI,
} from "./strategy_lab.js";
import {
  applyCorridorFrame,
  buildLocalCorridorPreview,
  refreshCorridorQuotesOneShot,
  startCorridorStream,
  stopCorridorStream,
} from "./corridor.js";
import {
  loadChain,
  refreshFromIBKR,
} from "./network.js";
import {
  importLedgerFile,
  loadLedger,
  refreshOpenLedgerMtm,
  renderLedgerChart,
  saveLedgerPreferences,
  syncLedgerControlStates,
  syncLedgerFromUI,
  toggleCampaignExpand,
} from "./ledger.js";
import {
  copyWallsAiPrompt,
} from "./ai_prompt.js";

// ------------------------------------------------------------------ Event Bindings
export function bindEvents() {
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
      if (state.ledgerData && state.ledgerData.report && isPanelVisible("trade_ledger")) {
        renderLedgerChart(state.ledgerData.report, themeVars());
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

  if (dom.alertBanner) {
    dom.alertBanner.addEventListener("click", function (ev) {
      var closeBtn =
        ev.target && ev.target.closest ? ev.target.closest("#alert-banner-close, .alert-banner-close") : null;
      if (closeBtn) {
        clearAlertBanner();
      }
    });
  }

  [dom.rangeSelect, dom.stepSelect, dom.dteMaxSelect].forEach(function (ctrl) {
    if (ctrl) {
      ctrl.addEventListener("change", function () {
        writeTickerPullPrefs(state.ticker);
      });
    }
  });

  if (dom.wallsCopyAiBtn) {
    dom.wallsCopyAiBtn.addEventListener("click", copyWallsAiPrompt);
  }

  if (dom.greeksMetricSelect) {
    dom.greeksMetricSelect.addEventListener("change", function () {
      if (state.chainData && isPanelVisible("greeks")) {
        renderGreeksChart(state.chainData, themeVars());
      }
    });
  }

  if (dom.panelVisibilityControls) {
    dom.panelVisibilityControls.addEventListener("click", function (ev) {
      var target =
        ev.target && ev.target.closest ? ev.target.closest("[data-panel-target]") : null;
      if (!target) return;
      var panelId = target.getAttribute("data-panel-target");
      if (!panelId || !Object.prototype.hasOwnProperty.call(DEFAULT_PANEL_VISIBILITY, panelId)) {
        return;
      }
      var nextVis = Object.assign({}, state.panelVisibility);
      nextVis[panelId] = !isPanelVisible(panelId);
      applyPanelVisibility(nextVis, { persist: true });
    });
  }

  if (dom.panelVisibilityReset) {
    dom.panelVisibilityReset.addEventListener("click", function () {
      applyPanelVisibility(cloneDefaultPanelVisibility(), { clearOverrides: true });
    });
  }

  function onWallsControlChange(ev) {
    if (dom.wallsViewSelect) writePref("ibkr_walls_view", dom.wallsViewSelect.value);
    if (dom.wallsMetricSelect) writePref("ibkr_walls_metric", dom.wallsMetricSelect.value);
    if (dom.wallsSplitDte) writePref("ibkr_walls_split_dte", dom.wallsSplitDte.checked);
    if (dom.wallsShowAnomalies) {
      writePref("ibkr_walls_show_anomalies", dom.wallsShowAnomalies.checked);
    }
    syncWallsSplitCheckboxState();
    if (state.chainData && isPanelVisible("walls")) {
      renderWallsChart(state.chainData, themeVars());
    }
    if (ev && ev.target === dom.wallsDteSelect && state.chainData) {
      syncAutoFollowOptionLabels(state.chainData);
      if (dom.corridorExpirySelect && !dom.corridorExpirySelect.value) {
        if (state.corridorStreaming || state.corridorStreamSource) {
          stopCorridorStream({ silent: true });
        }
        applyCorridorFrame(buildLocalCorridorPreview(state.chainData), { mergeIntoChain: false });
      }
      if (
        dom.strategyExpirySelect &&
        !dom.strategyExpirySelect.value &&
        state.strategyKind !== "CUSTOM" &&
        (isStrategyAutoPopulateEnabled() || Boolean(state.strategyEvaluation))
      ) {
        autoBuildStrategy(state.strategyKind, { silent: true });
      }
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
        if (ctrl === dom.filterDte && dom.thFilterExpiry) {
          var dVal = dom.filterDte.value || "ALL";
          var expVal = dVal === "ALL" || dVal.indexOf("BUCKET:") === 0 ? "" : dVal;
          dom.thFilterExpiry.value = expVal;
          state.tableFilters.expiry = expVal;
        }
        if (ctrl === dom.filterRight && dom.thFilterRight) {
          var rVal = dom.filterRight.value === "ALL" ? "" : dom.filterRight.value;
          dom.thFilterRight.value = rVal;
          state.tableFilters.right = rVal;
        }
        updateHeaderFilterHighlights();
        syncDteBucketTabs(state.chainData);
        renderTable(state.chainData);
      });
    }
  });

  if (dom.dteBucketTabs) {
    dom.dteBucketTabs.addEventListener("click", function (ev) {
      var btn =
        ev.target && ev.target.closest ? ev.target.closest("[data-dte-bucket]") : null;
      if (!btn || btn.disabled) return;
      var bucketVal = btn.getAttribute("data-dte-bucket") || "ALL";
      if (dom.filterDte) {
        dom.filterDte.value = bucketVal;
      }
      if (dom.thFilterExpiry) {
        dom.thFilterExpiry.value = "";
        state.tableFilters.expiry = "";
        updateHeaderFilterHighlights();
      }
      syncDteBucketTabs(state.chainData);
      renderTable(state.chainData);
    });
  }

  if (dom.tableResetFiltersBtn) {
    dom.tableResetFiltersBtn.addEventListener("click", resetAllTableFilters);
  }

  if (dom.tableBody) {
    dom.tableBody.addEventListener("click", function (ev) {
      var legBtn =
        ev.target && ev.target.closest ? ev.target.closest(".chain-add-leg-btn") : null;
      if (!legBtn) return;
      addSpecificStrategyLeg(
        legBtn.getAttribute("data-leg-expiry"),
        legBtn.getAttribute("data-leg-right"),
        legBtn.getAttribute("data-leg-strike")
      );
    });
  }

  if (dom.chainTableHead) {
    dom.chainTableHead.addEventListener("click", function (ev) {
      var sortBtn =
        ev.target && ev.target.closest ? ev.target.closest(".th-sort-btn[data-sort-col]") : null;
      if (!sortBtn) return;
      var col = sortBtn.getAttribute("data-sort-col");
      var colDef = col ? TABLE_COLUMN_BY_KEY[col] : null;
      if (!colDef) return;

      var firstDir = colDef.defaultDir || "asc";
      var secondDir = firstDir === "asc" ? "desc" : "asc";
      if (!state.tableSort || state.tableSort.col !== col) {
        state.tableSort = { col: col, dir: firstDir };
      } else if (state.tableSort.dir === firstDir) {
        state.tableSort = { col: col, dir: secondDir };
      } else {
        state.tableSort = { col: null, dir: null };
      }
      writeTableSortPref(state.tableSort);
      syncTableHeaderSortUI();
      renderTable(state.chainData);
    });

    function onHeaderFilterChange(ev) {
      var target = ev.target;
      if (!target || !target.getAttribute) return;
      var col = target.getAttribute("data-filter-col");
      if (!col || !TABLE_COLUMN_BY_KEY[col]) return;

      var val = String(target.value || "").trim();
      state.tableFilters[col] = val;

      if (col === "expiry" && dom.filterDte) {
        dom.filterDte.value = val || "ALL";
        syncDteBucketTabs(state.chainData);
      } else if (col === "right" && dom.filterRight) {
        dom.filterRight.value = val || "ALL";
      }

      updateHeaderFilterHighlights();
      renderTable(state.chainData);
    }

    dom.chainTableHead.addEventListener("input", function (ev) {
      if (ev.target && ev.target.classList && ev.target.classList.contains("th-filter-input")) {
        onHeaderFilterChange(ev);
      }
    });

    dom.chainTableHead.addEventListener("change", function (ev) {
      if (ev.target && ev.target.classList && ev.target.classList.contains("th-filter-select")) {
        onHeaderFilterChange(ev);
      }
    });
  }

  if (dom.strategyPresetPills) {
    dom.strategyPresetPills.addEventListener("click", function (ev) {
      var btn =
        ev.target && ev.target.closest ? ev.target.closest("[data-strategy-preset]") : null;
      if (!btn) return;
      var preset = btn.getAttribute("data-strategy-preset") || "BULL_PUT_SPREAD";
      setActiveStrategyPresetUI(preset);
      if (preset === "CUSTOM") {
        if (!state.strategyLegs.length && state.chainData) {
          state.strategyLegs = buildSeedLegsFromChain(state.chainData);
          renderStrategyLegsTable();
        }
        if (dom.strategyRationale) {
          dom.strategyRationale.textContent =
            "Custom leg builder active. Edit strikes/rights below and click Evaluate / Re-Price.";
        }
        return;
      }
      autoBuildStrategy(preset);
    });
  }

  if (dom.strategyAutoBuildBtn) {
    dom.strategyAutoBuildBtn.addEventListener("click", function () {
      autoBuildStrategy(state.strategyKind);
    });
  }

  if (dom.strategyEvaluateBtn) {
    dom.strategyEvaluateBtn.addEventListener("click", function () {
      evaluateStrategyRequest();
    });
  }

  if (dom.strategyAddLegBtn) {
    dom.strategyAddLegBtn.addEventListener("click", function () {
      addCustomStrategyLegRow();
    });
  }

  if (dom.strategyExpirySelect) {
    dom.strategyExpirySelect.addEventListener("change", function () {
      if (!state.chainData) return;
      if (state.strategyKind !== "CUSTOM" || !state.strategyLegs.length) {
        autoBuildStrategy(state.strategyKind, { silent: true });
      }
    });
  }

  if (dom.strategyAutoPopulate) {
    dom.strategyAutoPopulate.addEventListener("change", function () {
      writePref(
        STRATEGY_AUTO_POPULATE_KEY,
        dom.strategyAutoPopulate.checked ? "true" : "false"
      );
      if (
        dom.strategyAutoPopulate.checked &&
        state.chainData &&
        !state.strategyEvaluation &&
        !state.strategyBusy
      ) {
        maybeAutoPopulateStrategy(state.chainData);
      }
    });
  }

  if (dom.strategyResetBtn) {
    dom.strategyResetBtn.addEventListener("click", function () {
      resetStrategyEvaluationUI(true);
    });
  }

  if (dom.strategyDaysSlider) {
    dom.strategyDaysSlider.addEventListener("input", function () {
      renderStrategySlidersMax(dom.strategyDaysSlider.max);
    });
    dom.strategyDaysSlider.addEventListener("change", function () {
      renderStrategySlidersMax(dom.strategyDaysSlider.max);
      if (state.chainData && state.strategyLegs.length) {
        evaluateStrategyRequest();
      }
    });
  }

  if (dom.strategyIvSlider) {
    dom.strategyIvSlider.addEventListener("input", function () {
      renderStrategySlidersMax(dom.strategyDaysSlider ? dom.strategyDaysSlider.max : 120);
    });
    dom.strategyIvSlider.addEventListener("change", function () {
      renderStrategySlidersMax(dom.strategyDaysSlider ? dom.strategyDaysSlider.max : 120);
      if (state.chainData && state.strategyLegs.length) {
        evaluateStrategyRequest();
      }
    });
  }

  function onCorridorControlChange() {
    if (state.corridorStreaming || state.corridorStreamSource) {
      stopCorridorStream({ silent: true });
    }
    if (state.chainData) {
      applyCorridorFrame(buildLocalCorridorPreview(state.chainData), { mergeIntoChain: false });
    }
  }

  if (dom.corridorExpirySelect) {
    dom.corridorExpirySelect.addEventListener("change", onCorridorControlChange);
  }
  if (dom.corridorWindowSelect) {
    dom.corridorWindowSelect.addEventListener("change", onCorridorControlChange);
  }
  if (dom.corridorRefreshBtn) {
    dom.corridorRefreshBtn.addEventListener("click", refreshCorridorQuotesOneShot);
  }
  if (dom.corridorStreamToggle) {
    dom.corridorStreamToggle.addEventListener("change", function () {
      if (dom.corridorStreamToggle.checked) {
        startCorridorStream();
      } else {
        stopCorridorStream();
      }
    });
  }
  if (dom.corridorAutoReprice) {
    dom.corridorAutoReprice.addEventListener("change", function () {
      if (
        dom.corridorAutoReprice.checked &&
        state.chainData &&
        state.strategyLegs.length &&
        state.corridorFrame &&
        (state.corridorStreaming || state.corridorFrame.source !== "CACHED_PREVIEW")
      ) {
        evaluateStrategyRequest({ silent: true });
      }
    });
  }

  // Trade Ledger bindings
  if (dom.ledgerWindowGroup) {
    dom.ledgerWindowGroup.addEventListener("click", function (ev) {
      var btn =
        ev.target && ev.target.closest ? ev.target.closest("[data-ledger-window]") : null;
      if (!btn) return;
      var win = btn.getAttribute("data-ledger-window");
      if (!win || win === state.ledgerWindow) return;
      state.ledgerWindow = win;
      saveLedgerPreferences();
      syncLedgerControlStates(state.ledgerData ? state.ledgerData.report : null);
      loadLedger();
    });
  }

  if (dom.ledgerViewGroup) {
    dom.ledgerViewGroup.addEventListener("click", function (ev) {
      var btn =
        ev.target && ev.target.closest ? ev.target.closest("[data-ledger-view]") : null;
      if (!btn) return;
      var viewVal = btn.getAttribute("data-ledger-view");
      if (!viewVal || viewVal === state.ledgerView) return;
      state.ledgerView = viewVal;
      saveLedgerPreferences();
      syncLedgerControlStates(state.ledgerData ? state.ledgerData.report : null);
      loadLedger();
    });
  }

  if (dom.ledgerGroupGroup) {
    dom.ledgerGroupGroup.addEventListener("click", function (ev) {
      if (state.ledgerView === "open_mtm") return;
      var btn =
        ev.target && ev.target.closest ? ev.target.closest("[data-ledger-group]") : null;
      if (!btn || btn.disabled) return;
      var grp = btn.getAttribute("data-ledger-group");
      if (!grp || grp === state.ledgerGroupBy) return;
      state.ledgerGroupBy = grp;
      saveLedgerPreferences();
      syncLedgerControlStates(state.ledgerData ? state.ledgerData.report : null);
      loadLedger();
    });
  }

  if (dom.ledgerStockGainToggle) {
    dom.ledgerStockGainToggle.addEventListener("change", function () {
      state.ledgerIncludeStockGain = Boolean(dom.ledgerStockGainToggle.checked);
      saveLedgerPreferences();
      loadLedger();
    });
  }

  if (dom.ledgerTickerFilter) {
    dom.ledgerTickerFilter.addEventListener("change", function () {
      state.ledgerTicker = String(dom.ledgerTickerFilter.value || "ALL").toUpperCase();
      saveLedgerPreferences();
      loadLedger();
    });
  }

  if (dom.ledgerSyncMode) {
    dom.ledgerSyncMode.addEventListener("change", function () {
      state.ledgerSyncMode = String(dom.ledgerSyncMode.value || "auto");
      saveLedgerPreferences();
    });
  }

  if (dom.ledgerSyncBtn) {
    dom.ledgerSyncBtn.addEventListener("click", syncLedgerFromUI);
  }

  if (dom.ledgerImportBtn && dom.ledgerImportFile) {
    dom.ledgerImportBtn.addEventListener("click", function () {
      dom.ledgerImportFile.click();
    });
    dom.ledgerImportFile.addEventListener("change", function () {
      var files = dom.ledgerImportFile.files;
      if (files && files[0]) {
        importLedgerFile(files[0]);
      }
    });
  }

  if (dom.ledgerMtmBtn) {
    dom.ledgerMtmBtn.addEventListener("click", refreshOpenLedgerMtm);
  }

  if (dom.ledgerTableBody) {
    dom.ledgerTableBody.addEventListener("click", function (ev) {
      var toggleBtn =
        ev.target && ev.target.closest ? ev.target.closest("[data-campaign-toggle]") : null;
      if (toggleBtn && !toggleBtn.disabled) {
        ev.stopPropagation();
        toggleCampaignExpand(toggleBtn.getAttribute("data-campaign-toggle"));
        return;
      }
      var row = ev.target && ev.target.closest ? ev.target.closest("[data-campaign-row]") : null;
      if (row) {
        var btn = row.querySelector("[data-campaign-toggle]");
        if (btn && !btn.disabled) {
          toggleCampaignExpand(row.getAttribute("data-campaign-row"));
        }
      }
    });
  }

  window.addEventListener("pagehide", function () {
    if (state.corridorStreaming || state.corridorStreamSource) {
      if (navigator.sendBeacon) {
        navigator.sendBeacon("/api/corridor/stop");
      }
      stopCorridorStream({ silent: true, skipRemoteStop: true });
    }
  });
}
