/* IBKR Options Analyzer dashboard — Strategy Lab: legs table, presets, evaluation and `T+n` payoff chart. */

import {
  dom,
  state,
} from "./state.js";
import {
  extractApiErrorMessage,
  fmtMoney,
  fmtNum,
  formatAutoFollowLabel,
  formatExpiryIso,
  formatStrikeShort,
  isStrategyAutoPopulateEnabled,
  resolveStrategyTargetExpiry,
  showBanner,
  snapDteValueToExpiries,
  sortedChainExpiries,
  themeVars,
} from "./helpers.js";
import {
  baseLayout,
  makeVerticalMarker,
  renderStrategyEmptyPlaceholder,
} from "./charts.js";
import {
  applyPanelVisibility,
  isPanelVisible,
} from "./panels.js";
import {
  applyCorridorFrame,
  buildLocalCorridorPreview,
} from "./corridor.js";
import {
  apiJson,
} from "./network.js";

// ------------------------------------------------------------------ Strategy Lab & T+n Payoff
export function strategySortedExpiries(data) {
  return sortedChainExpiries(data);
}

function strategyStrikesFor(data, expiry, right) {
  if (!data || !data.contracts) return [];
  var seen = {};
  var strikes = [];
  for (var i = 0; i < data.contracts.length; i++) {
    var c = data.contracts[i];
    if (String(c.expiry) === String(expiry) && String(c.right) === String(right)) {
      var k = Number(c.strike);
      if (!seen[k]) {
        seen[k] = true;
        strikes.push(k);
      }
    }
  }
  strikes.sort(function (a, b) {
    return a - b;
  });
  return strikes;
}

function findLocalChainContract(data, expiry, right, strike) {
  if (!data || !data.contracts) return null;
  var targetK = Number(strike);
  for (var i = 0; i < data.contracts.length; i++) {
    var c = data.contracts[i];
    if (
      String(c.expiry) === String(expiry) &&
      String(c.right) === String(right) &&
      Math.abs(Number(c.strike) - targetK) < 1e-4
    ) {
      return c;
    }
  }
  return null;
}

function nearestStrikeFromList(strikes, targetStrike) {
  if (!strikes || !strikes.length) return null;
  var best = strikes[0];
  var bestDist = Math.abs(best - targetStrike);
  for (var i = 1; i < strikes.length; i++) {
    var d = Math.abs(strikes[i] - targetStrike);
    if (d < bestDist) {
      best = strikes[i];
      bestDist = d;
    }
  }
  return best;
}

export function setActiveStrategyPresetUI(kind) {
  state.strategyKind = kind;
  if (!dom.strategyPresetPills) return;
  var btns = dom.strategyPresetPills.querySelectorAll("[data-strategy-preset]");
  for (var i = 0; i < btns.length; i++) {
    var btn = btns[i];
    var isMatch = btn.getAttribute("data-strategy-preset") === kind;
    btn.classList.toggle("active", isMatch);
    btn.setAttribute("aria-pressed", isMatch ? "true" : "false");
  }
}

export function buildSeedLegsFromChain(data) {
  var exps = strategySortedExpiries(data);
  if (!exps.length) return [];
  var chosenExp = resolveStrategyTargetExpiry(data) || exps[0].expiry;
  var puts = strategyStrikesFor(data, chosenExp, "P");
  if (!puts.length) {
    var calls = strategyStrikesFor(data, chosenExp, "C");
    if (!calls.length) return [];
    return [
      { side: "BUY", right: "C", expiry: chosenExp, strike: calls[0], qty: 1 },
    ];
  }
  var spot = Number(data.meta.underlying_price || 100);
  var pw =
    data.walls && data.walls.aggregate_primary_put_wall
      ? Number(data.walls.aggregate_primary_put_wall.strike)
      : spot * 0.95;
  var shortK = nearestStrikeFromList(puts, pw);
  var lowerPuts = puts.filter(function (k) {
    return k < shortK - 1e-6;
  });
  var longK = lowerPuts.length ? lowerPuts[lowerPuts.length - 1] : shortK;
  if (Math.abs(longK - shortK) < 1e-6) {
    return [{ side: "SELL", right: "P", expiry: chosenExp, strike: shortK, qty: 1 }];
  }
  return [
    { side: "SELL", right: "P", expiry: chosenExp, strike: shortK, qty: 1 },
    { side: "BUY", right: "P", expiry: chosenExp, strike: longK, qty: 1 },
  ];
}

export function renderStrategySlidersMax(maxDays) {
  if (dom.strategyDaysSlider) {
    var cap = Math.max(1, Number(maxDays || 120));
    dom.strategyDaysSlider.max = String(cap);
    if (Number(dom.strategyDaysSlider.value) > cap) {
      dom.strategyDaysSlider.value = String(cap);
    }
    if (dom.strategyDaysReadout) {
      dom.strategyDaysReadout.textContent = "T+" + dom.strategyDaysSlider.value + "d";
    }
  }
  if (dom.strategyIvSlider && dom.strategyIvReadout) {
    var ivVal = Number(dom.strategyIvSlider.value || 0);
    dom.strategyIvReadout.textContent = (ivVal >= 0 ? "+" : "") + ivVal + "%";
  }
}

function resolveLocalContractMid(contract) {
  if (!contract) return null;
  var bidVal = contract.bid;
  var askVal = contract.ask;
  if (
    bidVal !== null &&
    bidVal !== undefined &&
    askVal !== null &&
    askVal !== undefined &&
    Number(bidVal) > 0 &&
    Number(askVal) >= Number(bidVal)
  ) {
    return (Number(bidVal) + Number(askVal)) / 2.0;
  }
  var keys = ["mid", "mark_price", "last", "close"];
  for (var i = 0; i < keys.length; i++) {
    var val = contract[keys[i]];
    if (val !== null && val !== undefined && Number(val) > 0) {
      return Number(val);
    }
  }
  if (contract.mid !== null && contract.mid !== undefined) {
    return Number(contract.mid);
  }
  return null;
}

function findExactEvaluatedLeg(leg, evaluatedLegs) {
  if (!leg || !Array.isArray(evaluatedLegs)) return null;
  for (var i = 0; i < evaluatedLegs.length; i++) {
    var evLeg = evaluatedLegs[i];
    if (
      String(evLeg.expiry) === String(leg.expiry) &&
      String(evLeg.right) === String(leg.right) &&
      Math.abs(Number(evLeg.strike) - Number(leg.strike)) < 1e-4
    ) {
      return evLeg;
    }
  }
  return null;
}

function formatLegQuoteCells(localMatch, evalFallback) {
  var srcBid =
    localMatch && localMatch.bid !== null && localMatch.bid !== undefined
      ? localMatch.bid
      : evalFallback
        ? evalFallback.bid
        : null;
  var srcAsk =
    localMatch && localMatch.ask !== null && localMatch.ask !== undefined
      ? localMatch.ask
      : evalFallback
        ? evalFallback.ask
        : null;
  var localMid = resolveLocalContractMid(localMatch);
  var srcMid =
    localMid !== null && localMid !== undefined
      ? localMid
      : evalFallback
        ? evalFallback.mid
        : null;
  var srcDelta =
    localMatch && localMatch.delta !== null && localMatch.delta !== undefined
      ? localMatch.delta
      : evalFallback
        ? evalFallback.delta
        : null;
  var srcIv =
    localMatch && localMatch.iv !== null && localMatch.iv !== undefined
      ? localMatch.iv
      : evalFallback
        ? evalFallback.iv
        : null;

  var bidAskText =
    (srcBid !== null && srcBid !== undefined ? fmtNum(srcBid, 2) : "\u2014") +
    " / " +
    (srcAsk !== null && srcAsk !== undefined ? fmtNum(srcAsk, 2) : "\u2014");
  var midText = srcMid !== null && srcMid !== undefined ? fmtMoney(srcMid) : "\u2014";
  var dTxt = srcDelta !== null && srcDelta !== undefined ? fmtNum(srcDelta, 2) : "\u2014";
  var ivTxt =
    srcIv !== null && srcIv !== undefined ? fmtNum(Number(srcIv) * 100, 1) + "%" : "\u2014";
  return {
    bidAskText: bidAskText,
    midText: midText,
    greeksText: dTxt + " \u2022 " + ivTxt,
  };
}

function snapLegToValidContract(data, leg, preferAxis) {
  if (!data || !leg) return true;
  var directStrikes = strategyStrikesFor(data, leg.expiry, leg.right);
  if (directStrikes.length) {
    leg.strike = nearestStrikeFromList(directStrikes, leg.strike);
    return true;
  }
  if (preferAxis === "expiry") {
    var oppRight = leg.right === "C" ? "P" : "C";
    var oppStrikes = strategyStrikesFor(data, leg.expiry, oppRight);
    if (oppStrikes.length) {
      leg.right = oppRight;
      leg.strike = nearestStrikeFromList(oppStrikes, leg.strike);
      return true;
    }
  }
  var exps = strategySortedExpiries(data);
  for (var i = 0; i < exps.length; i++) {
    var candStrikes = strategyStrikesFor(data, exps[i].expiry, leg.right);
    if (candStrikes.length) {
      leg.expiry = exps[i].expiry;
      leg.strike = nearestStrikeFromList(candStrikes, leg.strike);
      return true;
    }
  }
  return false;
}

function syncStrategyActionButtons() {
  var hasChain = Boolean(state.chainData);
  var hasLegs = Boolean(state.strategyLegs && state.strategyLegs.length > 0);
  if (dom.strategyAutoBuildBtn) {
    dom.strategyAutoBuildBtn.disabled = Boolean(state.strategyBusy || !hasChain);
  }
  if (dom.strategyEvaluateBtn) {
    dom.strategyEvaluateBtn.disabled = Boolean(state.strategyBusy || !hasChain || !hasLegs);
    dom.strategyEvaluateBtn.title = !hasLegs
      ? "Add at least one option leg first (+ Leg or Auto-Build)"
      : "Evaluate or re-price exact current legs table using cached or live Corridor quotes";
  }
  if (dom.strategyAddLegBtn) {
    dom.strategyAddLegBtn.disabled = Boolean(!hasChain);
  }
}

function clearStrategyKpis(subHint, basisText, rationaleText) {
  if (dom.strategyBasisBadge && basisText) {
    dom.strategyBasisBadge.textContent = basisText;
  }
  if (dom.strategyKpiPrice) {
    dom.strategyKpiPrice.textContent = "\u2014";
    dom.strategyKpiPrice.classList.remove("is-credit", "is-debit");
  }
  if (dom.strategyKpiPriceSub) {
    dom.strategyKpiPriceSub.textContent = subHint || "Click a preset or Auto-Build";
  }
  if (dom.strategyKpiSlip) dom.strategyKpiSlip.textContent = "\u2014";
  if (dom.strategyKpiSlipSub) dom.strategyKpiSlipSub.textContent = "Nat \u2192 Fair \u2192 Mid";
  if (dom.strategyKpiRisk) dom.strategyKpiRisk.textContent = "\u2014";
  if (dom.strategyKpiRiskSub) dom.strategyKpiRiskSub.textContent = "RoR %";
  if (dom.strategyKpiPop) dom.strategyKpiPop.textContent = "\u2014";
  if (dom.strategyKpiPopSub) dom.strategyKpiPopSub.textContent = "1\u03c3 expected move";
  if (dom.strategyKpiGreeks) dom.strategyKpiGreeks.textContent = "\u2014";
  if (dom.strategyKpiGreeksSub) {
    dom.strategyKpiGreeksSub.textContent = "\u0394 / \u0393 / \u0398 / V";
  }
  if (dom.strategyRationale && rationaleText !== undefined) {
    dom.strategyRationale.textContent = rationaleText;
  }
}

function markStrategyEvaluationDirty() {
  state.strategyEvaluationDirty = true;
  if (dom.strategyBasisBadge && state.chainData && state.strategyLegs.length) {
    dom.strategyBasisBadge.textContent = state.strategyEvaluation
      ? "CUSTOM EDITS \u2022 CLICK EVALUATE / RE-PRICE"
      : "READY \u2022 CLICK EVALUATE / RE-PRICE";
  }
  syncStrategyActionButtons();
}

export function resetStrategyEvaluationUI(keepChainLegs) {
  state.strategyEvaluation = null;
  state.strategyEvaluationDirty = false;
  state.strategyPendingEvaluate = null;
  if (dom.strategyDaysSlider) dom.strategyDaysSlider.value = "0";
  if (dom.strategyIvSlider) dom.strategyIvSlider.value = "0";
  renderStrategySlidersMax(120);

  var basisTxt = state.chainData
    ? "IDLE \u2022 CLICK TO BUILD"
    : "NO SNAPSHOT LOADED";
  var ratTxt = state.chainData
    ? "Cached snapshot ready. Click any strategy preset or Auto-Build from Walls, or edit legs below and click Evaluate / Re-Price."
    : "Load or refresh a ticker snapshot first to build option strategies.";
  clearStrategyKpis("Click a preset or Auto-Build", basisTxt, ratTxt);

  if (!state.chainData) {
    state.strategyLegs = [];
  } else if (!keepChainLegs) {
    state.strategyLegs = buildSeedLegsFromChain(state.chainData);
  }
  if (keepChainLegs && state.chainData && !state.strategyLegs.length) {
    clearStrategyKpis(
      "Add a leg or click Auto-Build",
      "NO LEGS \u2022 CLICK + LEG",
      "No legs configured. Click + Leg or Auto-Build from Walls."
    );
  }
  renderStrategyLegsTable();
  renderStrategyEmptyPlaceholder();
}

export function syncStrategySelectors(data) {
  var nextTicker = data && data.meta ? String(data.meta.underlying || "") : "";
  var sameTicker = Boolean(nextTicker && state.strategyLoadedTicker === nextTicker);
  state.strategyLoadedTicker = nextTicker;

  if (dom.strategyExpirySelect) {
    var prev = dom.strategyExpirySelect.value;
    dom.strategyExpirySelect.innerHTML = "";
    var autoOpt = document.createElement("option");
    autoOpt.value = "";
    autoOpt.textContent = formatAutoFollowLabel(data);
    dom.strategyExpirySelect.appendChild(autoOpt);
    var exps = strategySortedExpiries(data);
    var snappedExp = snapDteValueToExpiries(prev, exps, {
      allowBuckets: false,
      emptyValue: "",
    });
    for (var i = 0; i < exps.length; i++) {
      var opt = document.createElement("option");
      opt.value = exps[i].expiry;
      opt.textContent = formatExpiryIso(exps[i].expiry) + " (" + exps[i].dte + "d)";
      if (snappedExp && snappedExp === exps[i].expiry) {
        opt.selected = true;
      }
      dom.strategyExpirySelect.appendChild(opt);
    }
    dom.strategyExpirySelect.value = snappedExp || "";
  }

  if (sameTicker && data && state.strategyKind === "CUSTOM" && state.strategyLegs.length) {
    var kept = [];
    for (var j = 0; j < state.strategyLegs.length; j++) {
      var leg = state.strategyLegs[j];
      if (
        findLocalChainContract(data, leg.expiry, leg.right, leg.strike) ||
        snapLegToValidContract(data, leg, "expiry")
      ) {
        kept.push(leg);
      }
    }
    state.strategyLegs = kept;
    if (state.strategyLegs.length) {
      markStrategyEvaluationDirty();
      renderStrategyLegsTable();
      renderStrategyEmptyPlaceholder();
      return;
    }
  }

  if (!sameTicker && state.strategyKind === "CUSTOM") {
    setActiveStrategyPresetUI("BULL_PUT_SPREAD");
  }
  resetStrategyEvaluationUI(false);
}

export function renderStrategyLegsTable() {
  var tbody = dom.strategyLegsTbody;
  if (!tbody) return;
  tbody.innerHTML = "";
  syncStrategyActionButtons();
  if (!state.strategyLegs.length) {
    var emptyTr = document.createElement("tr");
    var emptyTd = document.createElement("td");
    emptyTd.colSpan = 9;
    emptyTd.textContent = state.chainData
      ? "No legs configured. Click + Leg or Auto-Build from Walls."
      : "Waiting for cached option snapshot.";
    emptyTr.appendChild(emptyTd);
    tbody.appendChild(emptyTr);
    return;
  }

  var exps = strategySortedExpiries(state.chainData);
  var evaluatedLegs =
    state.strategyEvaluation && state.strategyEvaluation.legs
      ? state.strategyEvaluation.legs
      : null;

  for (var i = 0; i < state.strategyLegs.length; i++) {
    (function (idx) {
      var leg = state.strategyLegs[idx];
      var localMatch = findLocalChainContract(
        state.chainData,
        leg.expiry,
        leg.right,
        leg.strike
      );
      var evalMatch = findExactEvaluatedLeg(leg, evaluatedLegs);
      var tr = document.createElement("tr");

      // 1. Side
      var tdSide = document.createElement("td");
      var selSide = document.createElement("select");
      selSide.className =
        "select-control strategy-leg-side " + (leg.side === "SELL" ? "is-sell" : "is-buy");
      selSide.setAttribute("aria-label", "Leg " + (idx + 1) + " side");
      ["BUY", "SELL"].forEach(function (sv) {
        var o = document.createElement("option");
        o.value = sv;
        o.textContent = sv;
        o.selected = leg.side === sv;
        selSide.appendChild(o);
      });
      selSide.addEventListener("change", function () {
        state.strategyLegs[idx].side = selSide.value;
        setActiveStrategyPresetUI("CUSTOM");
        markStrategyEvaluationDirty();
        renderStrategyLegsTable();
      });
      tdSide.appendChild(selSide);
      tr.appendChild(tdSide);

      // 2. Right
      var tdRight = document.createElement("td");
      var selRight = document.createElement("select");
      selRight.className = "select-control";
      selRight.setAttribute("aria-label", "Leg " + (idx + 1) + " right");
      ["P", "C"].forEach(function (rv) {
        var o = document.createElement("option");
        o.value = rv;
        o.textContent = rv === "C" ? "CALL" : "PUT";
        o.selected = leg.right === rv;
        selRight.appendChild(o);
      });
      selRight.addEventListener("change", function () {
        var prevRight = state.strategyLegs[idx].right;
        state.strategyLegs[idx].right = selRight.value;
        if (!snapLegToValidContract(state.chainData, state.strategyLegs[idx], "right")) {
          state.strategyLegs[idx].right = prevRight;
          showBanner("warn", "No " + (selRight.value === "C" ? "CALL" : "PUT") + " contracts found in snapshot.");
        }
        setActiveStrategyPresetUI("CUSTOM");
        markStrategyEvaluationDirty();
        renderStrategyLegsTable();
      });
      tdRight.appendChild(selRight);
      tr.appendChild(tdRight);

      // 3. Expiry
      var tdExp = document.createElement("td");
      var selExp = document.createElement("select");
      selExp.className = "select-control";
      selExp.setAttribute("aria-label", "Leg " + (idx + 1) + " expiry");
      var expList = exps.length ? exps : [{ expiry: leg.expiry, dte: 0 }];
      for (var e = 0; e < expList.length; e++) {
        var oe = document.createElement("option");
        oe.value = expList[e].expiry;
        oe.textContent = formatExpiryIso(expList[e].expiry) + " (" + expList[e].dte + "d)";
        oe.selected = expList[e].expiry === leg.expiry;
        selExp.appendChild(oe);
      }
      selExp.addEventListener("change", function () {
        state.strategyLegs[idx].expiry = selExp.value;
        snapLegToValidContract(state.chainData, state.strategyLegs[idx], "expiry");
        setActiveStrategyPresetUI("CUSTOM");
        markStrategyEvaluationDirty();
        renderStrategyLegsTable();
      });
      tdExp.appendChild(selExp);
      tr.appendChild(tdExp);

      // 4. Strike
      var tdStrike = document.createElement("td");
      var selStrike = document.createElement("select");
      selStrike.className = "select-control";
      selStrike.setAttribute("aria-label", "Leg " + (idx + 1) + " strike");
      var strikes = strategyStrikesFor(state.chainData, leg.expiry, leg.right);
      if (!strikes.length && leg.strike) {
        strikes = [Number(leg.strike)];
      }
      for (var k = 0; k < strikes.length; k++) {
        var ok = document.createElement("option");
        ok.value = String(strikes[k]);
        ok.textContent = formatStrikeShort(strikes[k]);
        ok.selected = Math.abs(strikes[k] - Number(leg.strike)) < 1e-4;
        selStrike.appendChild(ok);
      }
      selStrike.addEventListener("change", function () {
        state.strategyLegs[idx].strike = Number(selStrike.value);
        setActiveStrategyPresetUI("CUSTOM");
        markStrategyEvaluationDirty();
        renderStrategyLegsTable();
      });
      tdStrike.appendChild(selStrike);
      tr.appendChild(tdStrike);

      // 5. Qty
      var tdQty = document.createElement("td");
      tdQty.className = "num";
      var inpQty = document.createElement("input");
      inpQty.type = "number";
      inpQty.min = "1";
      inpQty.max = "100";
      inpQty.value = String(leg.qty || 1);
      inpQty.className = "select-control strategy-leg-qty";
      inpQty.setAttribute("aria-label", "Leg " + (idx + 1) + " qty");
      inpQty.addEventListener("input", function () {
        var parsed = parseInt(inpQty.value, 10);
        if (!Number.isNaN(parsed) && parsed >= 1) {
          state.strategyLegs[idx].qty = Math.min(100, parsed);
          setActiveStrategyPresetUI("CUSTOM");
          markStrategyEvaluationDirty();
        }
      });
      inpQty.addEventListener("change", function () {
        var v = Math.max(1, Math.min(100, parseInt(inpQty.value, 10) || 1));
        inpQty.value = String(v);
        state.strategyLegs[idx].qty = v;
        setActiveStrategyPresetUI("CUSTOM");
        markStrategyEvaluationDirty();
      });
      tdQty.appendChild(inpQty);
      tr.appendChild(tdQty);

      // 6-8. Live/Local-Preferred Quote Cells
      var qCells = formatLegQuoteCells(localMatch, evalMatch);

      var tdBa = document.createElement("td");
      tdBa.className = "num";
      tdBa.setAttribute("data-leg-idx", String(idx));
      tdBa.setAttribute("data-leg-cell", "bidask");
      tdBa.textContent = qCells.bidAskText;
      tr.appendChild(tdBa);

      var tdMid = document.createElement("td");
      tdMid.className = "num";
      tdMid.setAttribute("data-leg-idx", String(idx));
      tdMid.setAttribute("data-leg-cell", "mid");
      tdMid.textContent = qCells.midText;
      tr.appendChild(tdMid);

      var tdGreeks = document.createElement("td");
      tdGreeks.className = "num";
      tdGreeks.setAttribute("data-leg-idx", String(idx));
      tdGreeks.setAttribute("data-leg-cell", "greeks");
      tdGreeks.textContent = qCells.greeksText;
      tr.appendChild(tdGreeks);

      // 9. Remove (allowed down to 0 legs)
      var tdDel = document.createElement("td");
      var delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn strategy-leg-del-btn";
      delBtn.textContent = "\u00D7";
      delBtn.title = "Remove leg";
      delBtn.addEventListener("click", function () {
        state.strategyLegs.splice(idx, 1);
        setActiveStrategyPresetUI("CUSTOM");
        if (!state.strategyLegs.length) {
          state.strategyEvaluation = null;
          state.strategyEvaluationDirty = false;
          clearStrategyKpis(
            "Add a leg or click Auto-Build",
            "NO LEGS \u2022 CLICK + LEG",
            "No legs configured. Click + Leg or Auto-Build from Walls."
          );
          renderStrategyEmptyPlaceholder();
        } else {
          markStrategyEvaluationDirty();
        }
        renderStrategyLegsTable();
      });
      tdDel.appendChild(delBtn);
      tr.appendChild(tdDel);

      tbody.appendChild(tr);
    })(i);
  }
}

export function refreshStrategyLegsQuotesOnly() {
  if (!dom.strategyLegsTbody || !state.chainData || !state.strategyLegs.length) return;
  var evaluatedLegs =
    state.strategyEvaluation && state.strategyEvaluation.legs
      ? state.strategyEvaluation.legs
      : null;
  for (var i = 0; i < state.strategyLegs.length; i++) {
    var leg = state.strategyLegs[i];
    var match = findLocalChainContract(state.chainData, leg.expiry, leg.right, leg.strike);
    if (!match) continue;
    var qCells = formatLegQuoteCells(match, findExactEvaluatedLeg(leg, evaluatedLegs));
    var baCell = dom.strategyLegsTbody.querySelector(
      '[data-leg-idx="' + i + '"][data-leg-cell="bidask"]'
    );
    var midCell = dom.strategyLegsTbody.querySelector(
      '[data-leg-idx="' + i + '"][data-leg-cell="mid"]'
    );
    var greeksCell = dom.strategyLegsTbody.querySelector(
      '[data-leg-idx="' + i + '"][data-leg-cell="greeks"]'
    );
    if (baCell) baCell.textContent = qCells.bidAskText;
    if (midCell) midCell.textContent = qCells.midText;
    if (greeksCell) greeksCell.textContent = qCells.greeksText;
  }
}

function renderStrategyEvaluation(ev) {
  if (!ev) return;
  state.strategyEvaluation = ev;
  state.strategyEvaluationDirty = false;
  setActiveStrategyPresetUI(ev.strategy_kind);
  state.strategyLegs = ev.legs.map(function (l) {
    var perLegQty = ev.qty_multiplier > 0 ? Math.max(1, Math.round(l.qty / ev.qty_multiplier)) : l.qty;
    return {
      side: l.side,
      right: l.right,
      expiry: l.expiry,
      strike: l.strike,
      qty: perLegQty,
    };
  });
  renderStrategySlidersMax(ev.short_dte);
  renderStrategyLegsTable();

  if (dom.strategyBasisBadge) {
    dom.strategyBasisBadge.textContent =
      ev.strategy_kind.replace(/_/g, " ") + " \u2022 " + ev.short_dte + "d SHORT DTE";
  }

  var pr = ev.pricing;
  if (dom.strategyKpiPrice) {
    dom.strategyKpiPrice.classList.remove("is-credit", "is-debit");
    dom.strategyKpiPrice.classList.add(pr.is_credit ? "is-credit" : "is-debit");
    var cashAbs = Math.abs(pr.mid_cash_total);
    dom.strategyKpiPrice.textContent =
      (pr.is_credit ? "+" : "-") +
      fmtMoney(cashAbs) +
      (pr.is_credit ? " CR" : " DB") +
      " (" +
      fmtMoney(Math.abs(pr.net_mid_per_share)) +
      "/sh)";
  }
  if (dom.strategyKpiPriceSub) {
    dom.strategyKpiPriceSub.textContent =
      "Natural fill: " +
      (pr.natural_cash_total >= 0 ? "+" : "-") +
      fmtMoney(Math.abs(pr.natural_cash_total)) +
      " (" +
      fmtMoney(Math.abs(pr.net_natural_per_share)) +
      "/sh)";
  }

  if (dom.strategyKpiSlip) {
    var slipPctStr =
      pr.slippage_pct !== null && pr.slippage_pct !== undefined
        ? " (" + fmtNum(pr.slippage_pct, 1) + "%)"
        : "";
    dom.strategyKpiSlip.textContent = fmtMoney(pr.slippage_dollars) + slipPctStr;
  }
  if (dom.strategyKpiSlipSub) {
    dom.strategyKpiSlipSub.textContent =
      "Ladder: " +
      (pr.limit_ladder && pr.limit_ladder.length
        ? pr.limit_ladder
            .map(function (v) {
              return (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toFixed(2);
            })
            .join(" \u2192 ")
        : "\u2014");
  }

  var rk = ev.risk;
  if (dom.strategyKpiRisk) {
    var lossTxt = rk.max_loss_unbounded ? "UNBOUNDED" : fmtMoney(rk.max_loss);
    dom.strategyKpiRisk.textContent = "+" + fmtMoney(rk.max_profit) + " / -" + lossTxt;
  }
  if (dom.strategyKpiRiskSub) {
    dom.strategyKpiRiskSub.textContent =
      rk.return_on_risk_pct !== null && rk.return_on_risk_pct !== undefined
        ? "RoR: " + fmtNum(rk.return_on_risk_pct, 1) + "% on max risk"
        : rk.max_loss_unbounded
          ? "Defined upside cap / Naked risk"
          : "Risk envelope computed";
  }

  if (dom.strategyKpiPop) {
    var beTxt =
      rk.breakevens && rk.breakevens.length
        ? rk.breakevens
            .map(function (b) {
              return "$" + fmtNum(b, 2);
            })
            .join(" & ")
        : "None";
    var popTxt =
      rk.prob_of_profit_pct !== null && rk.prob_of_profit_pct !== undefined
        ? fmtNum(rk.prob_of_profit_pct, 1) + "% POP"
        : "\u2014";
    dom.strategyKpiPop.textContent = "BE " + beTxt + " \u2022 " + popTxt;
  }
  if (dom.strategyKpiPopSub) {
    dom.strategyKpiPopSub.textContent =
      rk.expected_move_1sigma_low !== null && rk.expected_move_1sigma_high !== null
        ? "\u00B11\u03c3 Move (" +
          fmtNum(rk.expected_move_1sigma_pct, 1) +
          "%): $" +
          fmtNum(rk.expected_move_1sigma_low, 2) +
          " \u2013 $" +
          fmtNum(rk.expected_move_1sigma_high, 2)
        : "\u00B11\u03c3 expected move unavailable";
  }

  var gr = ev.net_greeks;
  if (dom.strategyKpiGreeks) {
    dom.strategyKpiGreeks.textContent =
      "\u0394 " +
      fmtNum(gr.delta_shares, 1) +
      "sh \u2022 \u0398 " +
      (gr.theta_daily_dollars >= 0 ? "+" : "-") +
      fmtMoney(Math.abs(gr.theta_daily_dollars)) +
      "/d";
  }
  if (dom.strategyKpiGreeksSub) {
    dom.strategyKpiGreeksSub.textContent =
      "\u0393 " +
      fmtNum(gr.gamma_shares, 2) +
      " \u2022 Vega " +
      (gr.vega_per_1pct_dollars >= 0 ? "+" : "-") +
      fmtMoney(Math.abs(gr.vega_per_1pct_dollars)) +
      "/1%IV";
  }

  if (dom.strategyRationale) {
    dom.strategyRationale.textContent =
      ev.rationale && ev.rationale.length
        ? ev.rationale.join(" ")
        : "Strategy evaluated.";
  }

  if (state.corridorFrame && state.corridorFrame.source === "CACHED_PREVIEW" && state.chainData) {
    applyCorridorFrame(buildLocalCorridorPreview(state.chainData), { mergeIntoChain: false });
  }
  if (isPanelVisible("strategy")) {
    renderStrategyPayoffChart(ev, themeVars());
  }
}

export function renderStrategyPayoffChart(ev, t) {
  var el = dom.strategyChartEl;
  if (!el || typeof window.Plotly === "undefined") return;
  if (!ev || !ev.curves || !ev.curves.length || !ev.price_grid || !ev.price_grid.length) {
    renderStrategyEmptyPlaceholder();
    return;
  }

  el.classList.remove("is-idle");
  if (!el.data) {
    el.innerHTML = "";
  }
  var xs = ev.price_grid;
  var traces = [
    {
      type: "scatter",
      mode: "lines",
      name: "Zero P&L",
      x: [xs[0], xs[xs.length - 1]],
      y: [0, 0],
      line: { color: t.muted, width: 1, dash: "dot" },
      hoverinfo: "skip",
      showlegend: false,
    },
  ];

  for (var i = 0; i < ev.curves.length; i++) {
    var curve = ev.curves[i];
    var isExp = curve.label.indexOf("At ") === 0;
    var isSim = curve.label.indexOf("Simulated") === 0;
    var isMid = curve.label.indexOf("Midpoint") === 0;
    var lineColor = isExp
      ? t.call
      : isSim
        ? t.anomaly
        : isMid
          ? t.warn
          : t.dtePalette[0];
    traces.push({
      type: "scatter",
      mode: "lines",
      name: curve.label,
      x: xs,
      y: curve.pnl_values,
      line: {
        color: lineColor,
        width: isExp || isSim ? 2.5 : 1.75,
        dash: isMid ? "dot" : curve.days_elapsed === 0 && !isSim ? "dash" : "solid",
      },
      hovertemplate: curve.label + ": $%{y:,.2f}<extra></extra>",
    });
  }

  var markers = [
    makeVerticalMarker(ev.underlying_price, "SPOT $" + fmtNum(ev.underlying_price, 2), t.ink, 0.99, "dash", t),
  ];
  if (ev.primary_call_wall) {
    markers.push(
      makeVerticalMarker(
        ev.primary_call_wall,
        "CW $" + fmtNum(ev.primary_call_wall, 0),
        t.call,
        0.90,
        "dot",
        t
      )
    );
  }
  if (ev.primary_put_wall) {
    markers.push(
      makeVerticalMarker(
        ev.primary_put_wall,
        "PW $" + fmtNum(ev.primary_put_wall, 0),
        t.put,
        0.80,
        "dot",
        t
      )
    );
  }
  if (ev.risk && ev.risk.breakevens) {
    for (var b = 0; b < ev.risk.breakevens.length; b++) {
      markers.push(
        makeVerticalMarker(
          ev.risk.breakevens[b],
          "BE $" + fmtNum(ev.risk.breakevens[b], 2),
          t.warn,
          0.10 + b * 0.09,
          "dashdot",
          t
        )
      );
    }
  }

  var layout = baseLayout(t, "Underlying Price at Horizon ($)", "Net Strategy P&L ($)", markers);
  if (
    ev.risk &&
    ev.risk.expected_move_1sigma_low !== null &&
    ev.risk.expected_move_1sigma_high !== null
  ) {
    layout.shapes.unshift({
      type: "rect",
      xref: "x",
      yref: "paper",
      x0: ev.risk.expected_move_1sigma_low,
      x1: ev.risk.expected_move_1sigma_high,
      y0: 0,
      y1: 1,
      fillcolor: t.isTerminal ? "rgba(56, 189, 248, 0.06)" : "rgba(29, 78, 216, 0.05)",
      line: { width: 0 },
      layer: "below",
    });
  }
  layout.legend = {
    orientation: "h",
    y: 1.14,
    x: 0.5,
    xanchor: "center",
    font: { size: 10, color: t.ink },
  };
  layout.xaxis.tickprefix = "$";
  layout.yaxis.tickprefix = "$";
  layout.yaxis.zeroline = true;
  layout.yaxis.zerolinecolor = t.grid;
  layout.margin = { t: 48, r: 22, b: 44, l: 64 };
  window.Plotly.react(el, traces, layout, { responsive: true, displayModeBar: false });
  window.requestAnimationFrame(function () {
    if (window.Plotly && window.Plotly.Plots && el.data) {
      window.Plotly.Plots.resize(el);
    }
  });
  window.setTimeout(function () {
    if (window.Plotly && window.Plotly.Plots && el.data) {
      window.Plotly.Plots.resize(el);
    }
  }, 60);
}

function strategyControlParams() {
  var qty = dom.strategyQtyInput
    ? Math.max(1, Math.min(100, parseInt(dom.strategyQtyInput.value, 10) || 1))
    : 1;
  var days = dom.strategyDaysSlider ? Math.max(0, parseInt(dom.strategyDaysSlider.value, 10) || 0) : 0;
  var ivPct = dom.strategyIvSlider ? parseInt(dom.strategyIvSlider.value, 10) || 0 : 0;
  var targetExp = resolveStrategyTargetExpiry(state.chainData);
  return {
    qty: qty,
    daysForward: days,
    ivShift: ivPct / 100.0,
    targetExpiry: targetExp,
  };
}

function setStrategyButtonsBusy(busy) {
  state.strategyBusy = Boolean(busy);
  syncStrategyActionButtons();
}

export async function autoBuildStrategy(presetKind, callOptions) {
  var cOpts = callOptions || {};
  var silent = Boolean(cOpts.silent);
  if (!state.chainData || state.strategyBusy) {
    if (!state.chainData && !silent) {
      showBanner("warn", "Load or refresh an option snapshot before auto-building strategies.", {
        phase: "STRATEGY_BUILD",
      });
    }
    return;
  }
  var kind = presetKind && presetKind !== "CUSTOM" ? presetKind : "BULL_PUT_SPREAD";
  setActiveStrategyPresetUI(kind);
  var opts = strategyControlParams();
  setStrategyButtonsBusy(true);
  try {
    var res = await apiJson("/api/strategy/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticker: state.ticker,
        snapshot_id: state.snapshotId || null,
        strategy_kind: kind,
        target_expiry: opts.targetExpiry,
        days_forward: opts.daysForward,
        iv_shift: opts.ivShift,
        qty_multiplier: opts.qty,
      }),
    });
    if (res.ok && res.body && res.body.evaluations && res.body.evaluations.length) {
      renderStrategyEvaluation(res.body.evaluations[0]);
    } else if (!silent) {
      showBanner(
        "warn",
        extractApiErrorMessage(res.body, res.textSnippet || "Unable to auto-build strategy."),
        {
          ticker: state.ticker,
          phase: "STRATEGY_BUILD",
          status: res.status,
          elapsedMs: res.elapsedMs,
          rawBody: res.textSnippet,
        }
      );
    }
  } catch (err) {
    if (!silent) {
      showBanner("error", "Strategy auto-build request failed.", {
        ticker: state.ticker,
        phase: "STRATEGY_BUILD",
        elapsedMs: err && err.elapsedMs,
        detail: err && err.message ? err.message : String(err || ""),
      });
    }
  } finally {
    setStrategyButtonsBusy(false);
  }
}

export async function evaluateStrategyRequest(options) {
  var silent = Boolean(options && options.silent);
  if (!state.chainData) {
    if (!silent) {
      showBanner("warn", "Load or refresh an option snapshot before evaluating legs.");
    }
    return;
  }
  if (state.strategyBusy) {
    if (!state.strategyPendingEvaluate || !silent) {
      state.strategyPendingEvaluate = { silent: silent };
    }
    return;
  }
  if (!state.strategyLegs.length) {
    if (!silent) {
      showBanner("warn", "Add at least one option leg first.");
    }
    return;
  }

  var snappedAny = false;
  for (var i = 0; i < state.strategyLegs.length; i++) {
    var leg = state.strategyLegs[i];
    if (!findLocalChainContract(state.chainData, leg.expiry, leg.right, leg.strike)) {
      if (snapLegToValidContract(state.chainData, leg, "expiry")) {
        snappedAny = true;
      } else {
        if (!silent) {
          showBanner("warn", "Leg " + (i + 1) + " does not match any contract in the loaded snapshot.");
        }
        return;
      }
    }
  }
  if (snappedAny) {
    renderStrategyLegsTable();
  }

  var opts = strategyControlParams();
  setStrategyButtonsBusy(true);
  try {
    var res = await apiJson("/api/strategy/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticker: state.ticker,
        snapshot_id: state.snapshotId || null,
        strategy_kind: state.strategyKind || "CUSTOM",
        target_expiry: opts.targetExpiry,
        legs: state.strategyLegs.map(function (l) {
          return {
            side: l.side,
            right: l.right,
            expiry: l.expiry,
            strike: Number(l.strike),
            qty: Math.max(1, Math.min(100, parseInt(l.qty, 10) || 1)),
          };
        }),
        days_forward: opts.daysForward,
        iv_shift: opts.ivShift,
        qty_multiplier: opts.qty,
      }),
    });
    if (res.ok && res.body) {
      state.corridorLastRepriceAt = Date.now();
      renderStrategyEvaluation(res.body);
    } else if (!silent) {
      showBanner(
        "warn",
        extractApiErrorMessage(res.body, res.textSnippet || "Strategy evaluation failed."),
        {
          ticker: state.ticker,
          phase: "STRATEGY_EVAL",
          status: res.status,
          elapsedMs: res.elapsedMs,
          rawBody: res.textSnippet,
        }
      );
    }
  } catch (err) {
    if (!silent) {
      showBanner("error", "Strategy evaluation request failed.", {
        ticker: state.ticker,
        phase: "STRATEGY_EVAL",
        elapsedMs: err && err.elapsedMs,
        detail: err && err.message ? err.message : String(err || ""),
      });
    }
  } finally {
    setStrategyButtonsBusy(false);
    if (state.strategyPendingEvaluate) {
      var pending = state.strategyPendingEvaluate;
      state.strategyPendingEvaluate = null;
      evaluateStrategyRequest(pending);
    }
  }
}

export function addCustomStrategyLegRow() {
  if (!state.chainData) return;
  if (state.strategyLegs.length >= 6) {
    showBanner("warn", "Maximum 6 option legs supported per strategy.");
    return;
  }
  var exps = strategySortedExpiries(state.chainData);
  var exp =
    (dom.strategyExpirySelect && dom.strategyExpirySelect.value) ||
    resolveStrategyTargetExpiry(state.chainData) ||
    (state.strategyLegs.length ? state.strategyLegs[0].expiry : exps.length ? exps[0].expiry : "");
  var chosenRight = "C";
  var strikes = strategyStrikesFor(state.chainData, exp, chosenRight);
  if (!strikes.length) {
    chosenRight = "P";
    strikes = strategyStrikesFor(state.chainData, exp, chosenRight);
  }
  if (!strikes.length && state.chainData.contracts && state.chainData.contracts.length) {
    var fallbackC = state.chainData.contracts[0];
    exp = fallbackC.expiry;
    chosenRight = fallbackC.right;
    strikes = strategyStrikesFor(state.chainData, exp, chosenRight);
  }
  if (!strikes.length) {
    showBanner("warn", "No eligible option contracts found to add a leg.");
    return;
  }
  var spot = Number(state.chainData.meta.underlying_price || 100);
  var strike = nearestStrikeFromList(strikes, spot);
  state.strategyLegs.push({ side: "BUY", right: chosenRight, expiry: exp, strike: strike, qty: 1 });
  setActiveStrategyPresetUI("CUSTOM");
  markStrategyEvaluationDirty();
  renderStrategyLegsTable();
}

export function addSpecificStrategyLeg(expiry, right, strike) {
  if (!state.chainData) return;
  if (state.strategyLegs.length >= 6) {
    showBanner("warn", "Maximum 6 option legs supported per strategy.", {
      ticker: state.ticker,
      phase: "STRATEGY_LEG",
    });
    return;
  }
  var expStr = String(expiry || "");
  var rightNorm = String(right || "C").toUpperCase();
  var numStrike = Number(strike);
  if (!expStr || Number.isNaN(numStrike)) return;

  if (!isPanelVisible("strategy")) {
    var nextVis = Object.assign({}, state.panelVisibility, { strategy: true });
    applyPanelVisibility(nextVis, { persist: true, skipRender: true });
  }
  state.strategyLegs.push({
    side: "BUY",
    right: rightNorm,
    expiry: expStr,
    strike: numStrike,
    qty: 1,
  });
  setActiveStrategyPresetUI("CUSTOM");
  markStrategyEvaluationDirty();
  renderStrategyLegsTable();
  if (state.corridorFrame && state.corridorFrame.source === "CACHED_PREVIEW") {
    applyCorridorFrame(buildLocalCorridorPreview(state.chainData), { mergeIntoChain: false });
  }
  if (state.strategyEvaluation || isStrategyAutoPopulateEnabled()) {
    evaluateStrategyRequest({ silent: true });
  }
}

export function maybeAutoPopulateStrategy(data) {
  if (!data || !data.contracts || !data.contracts.length) return;
  if (!isStrategyAutoPopulateEnabled() || !isPanelVisible("strategy")) return;
  if (state.strategyKind === "CUSTOM" && state.strategyLegs.length) {
    evaluateStrategyRequest({ silent: true });
    return;
  }
  var preset = state.strategyKind && state.strategyKind !== "CUSTOM"
    ? state.strategyKind
    : "BULL_PUT_SPREAD";
  autoBuildStrategy(preset, { silent: true, autoTriggered: true });
}
