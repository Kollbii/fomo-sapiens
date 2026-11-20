"""Node.js ES-module tests for Workspace Mode Tabs, Pull Scope Popover, and Sticky Context Bar."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = REPO_ROOT / "src" / "web" / "static" / "js" / "dashboard"

NODE_PREAMBLE = """
import { pathToFileURL } from "node:url";
import assert from "node:assert/strict";

const ROOT = process.env.DASHBOARD_ROOT;
const modUrl = (name) => pathToFileURL(`${ROOT}/${name}`).href;

const store = {};
const scrollCalls = [];
const scrollIntoViewCalls = [];

class FakeClassList {
  constructor(owner) {
    this.owner = owner;
    this._set = new Set();
  }
  _syncFromString(str) {
    this._set = new Set(String(str || "").split(/\\s+/).filter(Boolean));
  }
  _syncToOwner() {
    this.owner._className = Array.from(this._set).join(" ");
  }
  add(...names) {
    for (const n of names) {
      if (n) this._set.add(String(n));
    }
    this._syncToOwner();
  }
  remove(...names) {
    for (const n of names) this._set.delete(String(n));
    this._syncToOwner();
  }
  toggle(name, force) {
    if (force === true) this._set.add(name);
    else if (force === false) this._set.delete(name);
    else if (this._set.has(name)) this._set.delete(name);
    else this._set.add(name);
    this._syncToOwner();
    return this._set.has(name);
  }
  contains(name) {
    return this._set.has(name);
  }
}

function matchesSingleSelector(el, selector) {
  if (!el || !selector) return false;
  if (selector.startsWith(".")) {
    return el.classList && el.classList.contains(selector.slice(1));
  }
  if (selector.startsWith("#")) {
    return el.id === selector.slice(1);
  }
  const attrMatch = selector.match(/^\\[([a-zA-Z0-9_-]+)(?:=["']?([^"'\\]]*)["']?)?\\]$/);
  if (attrMatch) {
    const val = el.getAttribute ? el.getAttribute(attrMatch[1]) : null;
    if (val === null) return false;
    return attrMatch[2] === undefined ? true : val === attrMatch[2];
  }
  return el.tagName === selector.toUpperCase();
}

function matchesCompoundSelector(el, selector) {
  const parts = String(selector || "").trim().split(/\\s+/);
  if (parts.length === 1) {
    const segs = parts[0].match(/(?:#[^.#\\[]+|\\.[^.#\\[]+|\\[[^\\]]+\\]|[^.#\\[]+)/g) || [];
    return segs.every((s) => matchesSingleSelector(el, s));
  }
  if (!matchesCompoundSelector(el, parts[parts.length - 1])) return false;
  let cur = el.parentNode;
  for (let i = parts.length - 2; i >= 0; i--) {
    while (cur && !matchesCompoundSelector(cur, parts[i])) {
      cur = cur.parentNode;
    }
    if (!cur) return false;
    cur = cur.parentNode;
  }
  return true;
}

class FakeNode {
  constructor(tagName = "div", id = "") {
    this.tagName = String(tagName).toUpperCase();
    this.id = id;
    this._className = "";
    this.classList = new FakeClassList(this);
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this.value = "";
    this.textContent = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this._innerHTML = "";
    this.style = {};
    this.dataset = {};
    this.parentNode = null;
    this.focused = false;
    this._rectBottom = 120;
  }
  get innerHTML() {
    return this._innerHTML;
  }
  set innerHTML(val) {
    this._innerHTML = String(val || "");
  }
  scrollIntoView() {
    scrollIntoViewCalls.push(this.id || this.getAttribute("data-panel-id") || this.tagName);
  }
  get className() {
    return this._className;
  }
  set className(val) {
    this._className = String(val || "");
    this.classList._syncFromString(this._className);
  }
  setAttribute(name, val) {
    this.attributes[name] = String(val);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name]
      : null;
  }
  appendChild(child) {
    if (!child) return child;
    if (child.parentNode && child.parentNode !== this) {
      child.parentNode.removeChild(child);
    }
    child.parentNode = this;
    if (!this.children.includes(child)) {
      this.children.push(child);
    }
    return child;
  }
  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx !== -1) {
      this.children.splice(idx, 1);
      child.parentNode = null;
    }
    return child;
  }
  contains(node) {
    let cur = node;
    while (cur) {
      if (cur === this) return true;
      cur = cur.parentNode;
    }
    return false;
  }
  focus() {
    this.focused = true;
  }
  getBoundingClientRect() {
    return { left: 0, top: 0, right: 1000, bottom: this._rectBottom, width: 1000, height: 120 };
  }
  addEventListener(type, fn) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push(fn);
  }
  dispatchEvent(ev) {
    const eventObj = ev || { type: "" };
    if (!eventObj.target) eventObj.target = this;
    eventObj.defaultPrevented = Boolean(eventObj.defaultPrevented);
    let stopped = false;
    eventObj.preventDefault = () => { eventObj.defaultPrevented = true; };
    eventObj.stopPropagation = () => { stopped = true; };
    let cur = this;
    while (cur && !stopped) {
      const handlers = (cur.listeners[eventObj.type] || []).slice();
      for (const fn of handlers) {
        eventObj.currentTarget = cur;
        fn.call(cur, eventObj);
      }
      cur = cur.parentNode;
    }
    return !eventObj.defaultPrevented;
  }
  closest(selector) {
    let cur = this;
    while (cur) {
      if (matchesCompoundSelector(cur, selector)) return cur;
      cur = cur.parentNode;
    }
    return null;
  }
  querySelector(selector) {
    const all = this.querySelectorAll(selector);
    return all.length ? all[0] : null;
  }
  querySelectorAll(selector) {
    const out = [];
    for (const ch of this.children) {
      if (matchesCompoundSelector(ch, selector)) out.push(ch);
      out.push(...ch.querySelectorAll(selector));
    }
    return out;
  }
}

const docNode = new FakeNode("#document");
const htmlEl = new FakeNode("html");
htmlEl.setAttribute("data-theme", "print");
const bodyEl = new FakeNode("body");
docNode.appendChild(htmlEl);
docNode.appendChild(bodyEl);

const byId = {};
function makeEl(tag, id, cls = "") {
  const el = new FakeNode(tag, id);
  if (cls) el.className = cls;
  if (id) byId[id] = el;
  bodyEl.appendChild(el);
  return el;
}

const controlBar = makeEl("section", "", "control-bar");
const workspaceTabs = makeEl("div", "workspace-tabs");
controlBar.appendChild(workspaceTabs);
for (const mode of ["desk", "portfolio", "backtest", "all"]) {
  const b = new FakeNode("button");
  b.className = "workspace-tab";
  b.setAttribute("data-workspace-tab", mode);
  workspaceTabs.appendChild(b);
}

const stickyBar = makeEl("aside", "sticky-context-bar", "sticky-context-bar");
stickyBar.setAttribute("aria-hidden", "true");
const stickyTabs = makeEl("div", "sticky-workspace-tabs");
stickyBar.appendChild(stickyTabs);
for (const mode of ["desk", "portfolio", "backtest", "all"]) {
  const b = new FakeNode("button");
  b.className = "workspace-tab workspace-tab--compact";
  b.setAttribute("data-workspace-tab", mode);
  stickyTabs.appendChild(b);
}
const stickyTicker = makeEl("strong", "sticky-ticker");
const stickySpot = makeEl("span", "sticky-spot");
const stickyWalls = makeEl("span", "sticky-walls");
const stickySnap = makeEl("span", "sticky-snap");
const stickyRefresh = makeEl("button", "sticky-refresh-btn");
const stickyTop = makeEl("button", "sticky-top-btn");
stickyBar.appendChild(stickyTicker);
stickyBar.appendChild(stickySpot);
stickyBar.appendChild(stickyWalls);
stickyBar.appendChild(stickySnap);
stickyBar.appendChild(stickyRefresh);
stickyBar.appendChild(stickyTop);

const pullPicker = makeEl("div", "pull-scope-picker");
const pullToggle = makeEl("button", "pull-scope-toggle");
const pullSummary = makeEl("span", "pull-scope-summary");
const pullPanel = makeEl("div", "pull-scope-panel");
pullPanel.hidden = true;
pullPicker.appendChild(pullToggle);
pullToggle.appendChild(pullSummary);
pullPicker.appendChild(pullPanel);

const rangeSel = makeEl("select", "range-pct-select");
rangeSel.value = "0.2";
const stepSel = makeEl("select", "strike-step-select");
stepSel.value = "auto";
const dteSel = makeEl("select", "dte-max-select");
dteSel.value = "120";
const shortDteCb = makeEl("input", "include-short-dte-toggle");
shortDteCb.checked = true;

const panelBar = makeEl("div", "panel-visibility-controls");
const allPanels = [
  "walls",
  "iv_smile",
  "anomalies",
  "greeks",
  "corridor",
  "strategy",
  "trade_ledger",
  "backtest",
  "best_option",
];
for (const pid of allPanels) {
  const pill = new FakeNode("button");
  pill.className = "panel-toggle-pill";
  pill.setAttribute("data-panel-target", pid);
  panelBar.appendChild(pill);
}

const chartGrid = makeEl("section", "", "chart-grid");
for (const pid of allPanels) {
  const card = new FakeNode("article");
  card.className = "panel-card";
  card.setAttribute("data-panel-id", pid);
  if (pid === "corridor") {
    card.id = "wall-corridor-card";
    byId[card.id] = card;
  }
  if (pid === "strategy") {
    card.id = "strategy-lab-card";
    byId[card.id] = card;
  }
  chartGrid.appendChild(card);
}

const kpiStrip = makeEl("section", "", "kpi-strip");
const kpiSpot = makeEl("article", "kpi-spot", "kpi-card kpi-card--spot");
const spotVal = new FakeNode("strong");
spotVal.className = "kpi-value";
const spotSub = new FakeNode("span");
spotSub.className = "kpi-sub";
kpiSpot.appendChild(spotVal);
kpiSpot.appendChild(spotSub);
kpiStrip.appendChild(kpiSpot);

const kpiWallsRibbon = makeEl("article", "kpi-walls-ribbon", "kpi-card kpi-card--walls-ribbon");
const kpiCorridorCaption = makeEl("span", "kpi-corridor-caption", "kpi-corridor-caption");
const kpiPutWall = makeEl("div", "kpi-put-wall", "kpi-wall-node kpi-wall-node--put");
kpiPutWall.setAttribute("data-kpi-action", "walls");
const pwVal = new FakeNode("strong");
pwVal.className = "kpi-value";
const pwSub = new FakeNode("span");
pwSub.className = "kpi-sub";
kpiPutWall.appendChild(pwVal);
kpiPutWall.appendChild(pwSub);
const kpiCorridorGauge = makeEl("div", "kpi-corridor-gauge", "kpi-corridor-gauge");
kpiCorridorGauge.setAttribute("data-kpi-action", "walls");
const kpiCorridorPin = makeEl("span", "kpi-corridor-pin", "kpi-corridor-pin");
kpiCorridorGauge.appendChild(kpiCorridorPin);
const kpiCallWall = makeEl("div", "kpi-call-wall", "kpi-wall-node kpi-wall-node--call");
kpiCallWall.setAttribute("data-kpi-action", "walls");
const cwVal = new FakeNode("strong");
cwVal.className = "kpi-value";
const cwSub = new FakeNode("span");
cwSub.className = "kpi-sub";
kpiCallWall.appendChild(cwVal);
kpiCallWall.appendChild(cwSub);
kpiWallsRibbon.appendChild(kpiCorridorCaption);
kpiWallsRibbon.appendChild(kpiPutWall);
kpiWallsRibbon.appendChild(kpiCorridorGauge);
kpiWallsRibbon.appendChild(kpiCallWall);
kpiStrip.appendChild(kpiWallsRibbon);

const kpiAnomalies = makeEl("article", "kpi-anomalies", "kpi-card kpi-card--anomaly");
const anomVal = new FakeNode("strong");
anomVal.className = "kpi-value";
const anomSub = new FakeNode("span");
anomSub.className = "kpi-sub";
const kpiAnomalyTop = makeEl("button", "kpi-anomaly-top", "kpi-anomaly-top");
kpiAnomalyTop.setAttribute("data-kpi-action", "chain-unusual");
kpiAnomalies.appendChild(anomVal);
kpiAnomalies.appendChild(anomSub);
kpiAnomalies.appendChild(kpiAnomalyTop);
kpiStrip.appendChild(kpiAnomalies);

const filterUnusual = makeEl("input", "filter-unusual");
makeEl("table", "chain-table");
makeEl("tbody", "chain-table-body");
makeEl("span", "table-row-count");
makeEl("div", "dte-bucket-tabs");

globalThis.Node = FakeNode;
globalThis.Element = FakeNode;

const outsideBtn = makeEl("button", "outside-el");
const customEvents = [];

let observerCallback = null;
globalThis.window = {
  document: docNode,
  localStorage: {
    getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
  IntersectionObserver: function FakeObserver(cb) {
    observerCallback = cb;
    return { observe() {} };
  },
  scrollTo: (opts) => scrollCalls.push(opts),
  addEventListener: () => {},
  requestAnimationFrame: () => {},
  setTimeout: (fn) => { fn(); return 1; },
  clearTimeout: () => {},
  getComputedStyle: () => ({ getPropertyValue: () => "#111111" }),
  Plotly: { Plots: { resize() {} }, purge() {}, react() {} },
};

globalThis.document = {
  documentElement: htmlEl,
  body: bodyEl,
  readyState: "complete",
  getElementById: (id) => byId[id] || null,
  querySelector: (sel) => docNode.querySelector(sel),
  querySelectorAll: (sel) => docNode.querySelectorAll(sel),
  createElement: (tag) => new FakeNode(tag),
  addEventListener: (t, fn) => docNode.addEventListener(t, fn),
  dispatchEvent: (ev) => {
    customEvents.push(ev);
    return docNode.dispatchEvent(ev);
  },
};
globalThis.CustomEvent = class FakeCustomEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.detail = init.detail;
  }
};
globalThis.Event = class FakeEvent {
  constructor(type) { this.type = type; }
};
"""


def _run_node(script_body: str) -> dict[str, Any]:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js not available")
    env = dict(os.environ)
    env["DASHBOARD_ROOT"] = str(DASHBOARD_DIR)
    proc = subprocess.run(
        [node, "--input-type=module", "-e", NODE_PREAMBLE + "\n" + script_body],
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, f"Node failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}"
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    parsed: dict[str, Any] = json.loads(lines[-1])
    return parsed


def test_workspace_modes_and_sticky_controls() -> None:
    result = _run_node(
        """
        const stateMod = await import(modUrl("state.js"));
        const helpersMod = await import(modUrl("helpers.js"));
        const panelsMod = await import(modUrl("panels.js"));
        const badgesMod = await import(modUrl("badges.js"));
        const workspaceMod = await import(modUrl("workspace.js"));

        const { WORKSPACE_MODE_KEY, dom, state } = stateMod;
        const { syncPullScopeSummary } = helpersMod;
        const {
          normalizeWorkspaceMode,
          isPanelInWorkspace,
          workspaceForPanel,
          isPanelVisible,
          applyPanelVisibility,
          applyWorkspaceMode,
          ensurePanelVisible,
        } = panelsMod;
        const { syncStickyContextBar } = badgesMod;
        const { wireWorkspaceControls } = workspaceMod;

        assert.equal(normalizeWorkspaceMode("PORTFOLIO"), "portfolio");
        assert.equal(normalizeWorkspaceMode("invalid"), "desk");
        assert.equal(isPanelInWorkspace("best_option", "desk"), true);
        assert.equal(isPanelInWorkspace("trade_ledger", "desk"), false);
        assert.equal(workspaceForPanel("trade_ledger"), "portfolio");
        assert.equal(workspaceForPanel("backtest"), "backtest");
        assert.equal(workspaceForPanel("strategy"), "desk");

        applyPanelVisibility(state.panelVisibility, { skipRender: true });
        applyWorkspaceMode("desk", { persist: false, skipRender: true });
        assert.equal(htmlEl.getAttribute("data-workspace-mode"), "desk");
        assert.equal(isPanelVisible("best_option"), true);
        assert.equal(isPanelVisible("trade_ledger"), false);
        assert.equal(isPanelVisible("iv_smile"), false);
        assert.equal(panelBar.hidden, false);
        assert.equal(
          panelBar.querySelector('[data-panel-target="trade_ledger"]').hidden,
          true
        );

        customEvents.length = 0;
        applyWorkspaceMode("portfolio", { persist: true, skipRender: true });
        assert.equal(store[WORKSPACE_MODE_KEY], "portfolio");
        assert.equal(htmlEl.getAttribute("data-workspace-mode"), "portfolio");
        assert.equal(isPanelVisible("trade_ledger"), true);
        assert.equal(isPanelVisible("best_option"), false);
        assert.equal(panelBar.hidden, true);
        assert.equal(customEvents[0].type, "ibkr:panel-visibility");
        assert.equal(customEvents[0].detail.visible, false);

        ensurePanelVisible("backtest");
        assert.equal(state.workspaceMode, "backtest");
        assert.equal(isPanelVisible("backtest"), true);

        ensurePanelVisible("strategy");
        assert.equal(state.workspaceMode, "desk");
        assert.equal(isPanelVisible("strategy"), true);

        applyWorkspaceMode("all", { persist: true, skipRender: true });
        assert.equal(isPanelVisible("trade_ledger"), true);
        assert.equal(isPanelVisible("backtest"), true);
        assert.equal(isPanelVisible("best_option"), true);
        assert.equal(panelBar.hidden, false);
        assert.equal(
          panelBar.querySelector('[data-panel-target="trade_ledger"]').hidden,
          false
        );

        // Sticky bar telemetry update
        syncStickyContextBar({
          meta: { underlying_price: 149.3, market_date: "2026-11-19", market_data_type: 1 },
          walls: {
            aggregate_primary_call_wall: { strike: 200 },
            aggregate_primary_put_wall: { strike: 100 },
          },
        });
        assert.equal(stickySpot.textContent, "$149.30");
        assert.equal(stickyWalls.textContent, "CW $200 | PW $100");
        assert.equal(stickySnap.textContent, "2026-11-19 \\u2022 T1");

        // Wire workspace controls: Pull-Scope summary + popover open/close/Escape + observer
        wireWorkspaceControls();
        assert.equal(
          pullSummary.textContent,
          "Auto \\u2022 \\u00B120% \\u2022 30\\u2013120d (+0\\u201329d)",
        );

        stepSel.value = "5";
        shortDteCb.checked = false;
        syncPullScopeSummary();
        assert.equal(pullSummary.textContent, "$5 \\u2022 \\u00B120% \\u2022 30\\u2013120d");

        pullToggle.dispatchEvent({ type: "click" });
        assert.equal(pullPanel.hidden, false);
        assert.equal(pullToggle.getAttribute("aria-expanded"), "true");

        docNode.dispatchEvent({ type: "keydown", key: "Escape" });
        assert.equal(pullPanel.hidden, true);
        assert.equal(pullToggle.getAttribute("aria-expanded"), "false");
        assert.equal(pullToggle.focused, true);

        pullToggle.dispatchEvent({ type: "click" });
        assert.equal(pullPanel.hidden, false);
        outsideBtn.dispatchEvent({ type: "click" });
        assert.equal(pullPanel.hidden, true);

        // Sticky context bar appears when control bar scrolls past top
        observerCallback([{ isIntersecting: false, boundingClientRect: { bottom: -10 } }]);
        assert.equal(stickyBar.classList.contains("is-visible"), true);
        assert.equal(stickyBar.getAttribute("aria-hidden"), "false");

        stickyTop.dispatchEvent({ type: "click" });
        assert.equal(scrollCalls.length, 1);

        observerCallback([{ isIntersecting: true, boundingClientRect: { bottom: 200 } }]);
        assert.equal(stickyBar.classList.contains("is-visible"), false);
        assert.equal(stickyBar.getAttribute("aria-hidden"), "true");

        // Clicking compact tab switches workspace mode
        const portTab = dom.stickyWorkspaceTabs.querySelector('[data-workspace-tab="portfolio"]');
        portTab.dispatchEvent({ type: "click" });
        assert.equal(state.workspaceMode, "portfolio");

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_helpers_json_prefs_and_formatters() -> None:
    result = _run_node(
        """
        const {
          PRESET_STRATEGY_LABELS,
          formatExpiryIso,
          formatStrategyShortLabel,
          formatStrikeShort,
          readJsonPref,
          removePref,
          writeJsonPref,
        } = await import(modUrl("helpers.js"));

        assert.equal(readJsonPref("missing_key", "fallback"), "fallback");
        writeJsonPref("test_json_pref_v1", { a: 1, b: ["x"] });
        assert.deepEqual(readJsonPref("test_json_pref_v1", null), { a: 1, b: ["x"] });

        store["corrupt_json_pref"] = "{not-json";
        assert.deepEqual(readJsonPref("corrupt_json_pref", { ok: false }), { ok: false });

        removePref("test_json_pref_v1");
        assert.equal(readJsonPref("test_json_pref_v1", null), null);

        // Graceful fallback when localStorage throws
        const origStorage = globalThis.window.localStorage;
        globalThis.window.localStorage = {
          getItem() { throw new Error("blocked"); },
          setItem() { throw new Error("quota"); },
          removeItem() { throw new Error("blocked"); },
        };
        assert.equal(readJsonPref("any", "safe"), "safe");
        writeJsonPref("any", { x: 1 });
        removePref("any");
        globalThis.window.localStorage = origStorage;

        // Strike & expiry formatters
        assert.equal(formatStrikeShort(120), "$120");
        assert.equal(formatStrikeShort(120.5), "$120.50");
        assert.equal(formatStrikeShort(null), "\\u2014");
        assert.equal(formatStrikeShort(null, ""), "");
        assert.equal(formatExpiryIso("20261218"), "2026-12-18");
        assert.equal(formatExpiryIso("2026-12-18"), "2026-12-18");

        // Strategy label formatting & preset validation exclusion
        assert.equal(formatStrategyShortLabel("BULL_PUT_SPREAD"), "Bull Put");
        assert.equal(formatStrategyShortLabel("CUSTOM"), "Custom");
        assert.equal(formatStrategyShortLabel(null), "\\u2014");
        assert.equal(formatStrategyShortLabel("NEW_KIND"), "NEW KIND");
        assert.equal(Object.prototype.hasOwnProperty.call(PRESET_STRATEGY_LABELS, "CUSTOM"), false);

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_row4_market_ribbon_formatting_and_guided_flow_actions() -> None:
    result = _run_node(
        """
        const stateMod = await import(modUrl("state.js"));
        const badgesMod = await import(modUrl("badges.js"));
        const panelsMod = await import(modUrl("panels.js"));
        const eventsMod = await import(modUrl("events.js"));

        const { state, dom } = stateMod;
        const {
          computeCorridorSpotPct,
          formatKpiContractScope,
          formatTopAnomalySummary,
          formatWallBasisShort,
          renderKpis,
        } = badgesMod;
        const { applyPanelVisibility, applyWorkspaceMode, isPanelVisible } = panelsMod;
        const { bindEvents, handleKpiRibbonAction } = eventsMod;

        // 1. Pure helpers
        assert.equal(formatWallBasisShort("OPEN_INTEREST"), "OI");
        assert.equal(formatWallBasisShort("VOLUME"), "Vol");

        assert.equal(
          formatKpiContractScope(
            { snapshot_id: "s1", market_date: "2026-11-20", contract_count: 160 },
            [{ snapshot_id: "s1", market_date: "2026-11-20", contract_count: 440 }],
          ),
          "160/440c",
        );
        assert.equal(
          formatKpiContractScope(
            { snapshot_id: "s2", market_date: "2026-11-20", contract_count: 440 },
            [{ snapshot_id: "s2", market_date: "2026-11-20", contract_count: 440 }],
          ),
          "440c",
        );

        const pos = computeCorridorSpotPct(137.79, 100, 200);
        assert.equal(pos.valid, true);
        assert.equal(pos.pinPct, 37.8);
        assert.equal(computeCorridorSpotPct(90, 100, 200).pinPct, 4);
        assert.equal(computeCorridorSpotPct(250, 100, 200).pinPct, 96);
        assert.equal(computeCorridorSpotPct(100, 200, 100).valid, false);

        assert.equal(
          formatTopAnomalySummary({
            total_flagged: 26,
            anomalies: [
              { strike: 150, right: "C", dte: 28, vol_oi_ratio: 2.8, volume_zscore: 4.1 },
            ],
          }),
          "Top: $150C (28d) \\u2022 2.8\\u00D7 Vol/OI",
        );
        assert.equal(
          formatTopAnomalySummary({ total_flagged: 0, anomalies: [] }),
          "0 flagged contracts",
        );

        // 2. renderKpis populates compact corridor ribbon with human labels
        state.snapshots = [
          { snapshot_id: "snap1", market_date: "2026-11-20", contract_count: 440 },
        ];
        const sample = {
          meta: {
            snapshot_id: "snap1",
            underlying: "NVDA",
            underlying_price: 137.79,
            market_date: "2026-11-20",
            contract_count: 160,
            range_pct: 0.5,
            strike_step: 5,
            source: "CACHE",
          },
          walls: {
            primary_basis: "OPEN_INTEREST",
            total_call_oi: 1730000,
            total_put_oi: 1000000,
            aggregate_primary_put_wall: {
              strike: 100,
              distance_from_spot_pct: -27.4,
              metric_value: 241696,
            },
            aggregate_primary_call_wall: {
              strike: 200,
              distance_from_spot_pct: 45.1,
              metric_value: 417950,
            },
          },
          anomalies: {
            total_flagged: 26,
            vol_gt_oi_count: 18,
            high_zscore_count: 9,
            anomalies: [
              { strike: 150, right: "C", dte: 28, vol_oi_ratio: 2.8, volume_zscore: 4.1 },
            ],
          },
        };
        renderKpis(sample);

        assert.equal(dom.kpiSpot.querySelector(".kpi-value").textContent, "$137.79");
        assert.ok(dom.kpiSpot.querySelector(".kpi-sub").textContent.includes("160/440c"));
        assert.equal(dom.kpiPutWall.querySelector(".kpi-value").textContent, "$100");
        assert.ok(dom.kpiPutWall.querySelector(".kpi-sub").textContent.includes("OI 241,696"));
        assert.ok(!dom.kpiPutWall.querySelector(".kpi-sub").textContent.includes("OPEN_INTEREST"));
        assert.equal(dom.kpiCallWall.querySelector(".kpi-value").textContent, "$200");
        assert.ok(dom.kpiCallWall.querySelector(".kpi-sub").textContent.includes("OI 417,950"));
        assert.equal(dom.kpiCorridorPin.style.left, "37.8%");
        assert.equal(
          dom.kpiCorridorCaption.textContent,
          "Spot 38% of range \\u2022 C/P OI 1.73\\u00D7",
        );
        assert.equal(dom.kpiAnomalies.querySelector(".kpi-value").textContent, "26");
        assert.equal(dom.kpiAnomalyTop.textContent, "Top: $150C (28d) \\u2022 2.8\\u00D7 Vol/OI");

        // 3. Guided flow actions reveal hidden panels, filter unusual chain rows, and scroll
        applyWorkspaceMode("desk", { persist: false, skipRender: true });
        applyPanelVisibility(
          { ...state.panelVisibility, walls: false, anomalies: false, corridor: false },
          { skipRender: true },
        );
        bindEvents();

        scrollIntoViewCalls.length = 0;
        stickyBar.getBoundingClientRect = () => ({
          left: 0,
          top: 0,
          right: 1000,
          bottom: 46,
          width: 1000,
          height: 46,
        });
        const wallsBtn = new FakeNode("button");
        wallsBtn.setAttribute("data-kpi-action", "walls");
        kpiWallsRibbon.appendChild(wallsBtn);
        wallsBtn.dispatchEvent({ type: "click" });
        assert.equal(isPanelVisible("walls"), true);
        assert.ok(scrollIntoViewCalls.includes("walls"));
        const wallsCardEl = document.querySelector('.panel-card[data-panel-id="walls"]');
        assert.equal(wallsCardEl.style.scrollMarginTop, "58px");

        handleKpiRibbonAction("corridor");
        assert.equal(isPanelVisible("corridor"), true);
        assert.ok(scrollIntoViewCalls.includes("wall-corridor-card"));
        assert.equal(document.getElementById("wall-corridor-card").style.scrollMarginTop, "58px");

        handleKpiRibbonAction("anomalies");
        assert.equal(isPanelVisible("anomalies"), true);
        assert.ok(scrollIntoViewCalls.includes("anomalies"));

        assert.equal(dom.filterUnusual.checked, false);
        handleKpiRibbonAction("chain-unusual");
        assert.equal(dom.filterUnusual.checked, true);
        assert.ok(scrollIntoViewCalls.includes("chain-table"));
        assert.equal(document.getElementById("chain-table").style.scrollMarginTop, "58px");

        // Keyboard Enter on role="button" wall node triggers action
        scrollIntoViewCalls.length = 0;
        dom.kpiPutWall.dispatchEvent({ type: "keydown", key: "Enter" });
        assert.ok(scrollIntoViewCalls.includes("walls"));

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True
