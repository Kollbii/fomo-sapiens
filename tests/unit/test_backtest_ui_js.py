"""Node.js ES-module tests for Backtest split-pane resizer and collapsible Paper Trades drawer."""

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

NODE_HARNESS_PREAMBLE = """
import { pathToFileURL } from "node:url";
import assert from "node:assert/strict";

const ROOT = process.env.DASHBOARD_ROOT;
const modUrl = (name) => pathToFileURL(`${ROOT}/${name}`).href;

const store = {};
const fetchCalls = [];

let nextFetchImpl = async (url, init) => {
  fetchCalls.push({ url: String(url), init: init || {} });
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify({}),
  };
};

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

class FakeStyle {
  constructor() {
    this._props = {};
  }
  setProperty(name, val) {
    this._props[String(name)] = String(val);
  }
  getPropertyValue(name) {
    return Object.prototype.hasOwnProperty.call(this._props, name) ? this._props[name] : "";
  }
  removeProperty(name) {
    delete this._props[name];
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

class FakeNode {
  constructor(tagName = "div", id = "") {
    this.tagName = String(tagName).toUpperCase();
    this.id = id;
    this._className = "";
    this.classList = new FakeClassList(this);
    this.attributes = {};
    this.style = new FakeStyle();
    this.dataset = {};
    this.children = [];
    this.listeners = {};
    this.value = "";
    this.textContent = "";
    this._innerHTML = "";
    this.checked = false;
    this.disabled = false;
    this.selected = false;
    this.hidden = false;
    this.title = "";
    this.type = "";
    this.parentNode = null;
    this._rectFn = null;
  }
  get className() {
    return this._className;
  }
  set className(val) {
    this._className = String(val || "");
    this.classList._syncFromString(this._className);
  }
  get innerHTML() {
    return this._innerHTML;
  }
  set innerHTML(val) {
    this._innerHTML = String(val || "");
    if (this._innerHTML === "") {
      for (const ch of this.children) ch.parentNode = null;
      this.children = [];
    }
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
  getBoundingClientRect() {
    if (typeof this._rectFn === "function") return this._rectFn();
    return { left: 0, top: 0, right: 1000, bottom: 300, width: 1000, height: 300 };
  }
  addEventListener(type, fn) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push(fn);
  }
  removeEventListener(type, fn) {
    const list = this.listeners[type];
    if (!list) return;
    this.listeners[type] = list.filter((f) => f !== fn);
  }
  dispatchEvent(ev) {
    const eventObj = ev || { type: "" };
    const isPlain = typeof Event === "undefined" || !(eventObj instanceof Event);
    if (isPlain) {
      if (!eventObj.target) eventObj.target = this;
      eventObj.defaultPrevented = Boolean(eventObj.defaultPrevented);
      eventObj.preventDefault = () => {
        eventObj.defaultPrevented = true;
      };
    }
    let stopped = false;
    if (isPlain) {
      eventObj.stopPropagation = () => {
        stopped = true;
      };
    }
    let cur = this;
    while (cur && !stopped) {
      const handlers = (cur.listeners[eventObj.type] || []).slice();
      for (const fn of handlers) {
        if (isPlain) eventObj.currentTarget = cur;
        fn.call(cur, eventObj);
      }
      cur = cur.parentNode;
    }
    return !eventObj.defaultPrevented;
  }
  closest(selector) {
    let cur = this;
    while (cur) {
      if (matchesSingleSelector(cur, selector)) return cur;
      cur = cur.parentNode;
    }
    return null;
  }
  querySelector(selector) {
    for (const ch of this.children) {
      if (matchesSingleSelector(ch, selector)) return ch;
      const deep = ch.querySelector(selector);
      if (deep) return deep;
    }
    return null;
  }
  querySelectorAll(selector) {
    const out = [];
    for (const ch of this.children) {
      if (matchesSingleSelector(ch, selector)) out.push(ch);
      out.push(...ch.querySelectorAll(selector));
    }
    return out;
  }
}

globalThis.Node = FakeNode;
globalThis.Element = FakeNode;

const docNode = new FakeNode("#document");
const htmlEl = new FakeNode("html");
htmlEl.getAttribute = () => "print";
const bodyEl = new FakeNode("body");
docNode.appendChild(htmlEl);
docNode.appendChild(bodyEl);

const elementsById = {};
function makeEl(tag, id) {
  const el = new FakeNode(tag, id);
  elementsById[id] = el;
  bodyEl.appendChild(el);
  return el;
}

const btCard = makeEl("article", "backtest-card");
btCard.setAttribute("data-panel-id", "backtest");
makeEl("span", "backtest-status-badge");
makeEl("div", "backtest-mode-group");
makeEl("button", "backtest-save-paper-btn");
makeEl("button", "backtest-run-btn");
makeEl("form", "backtest-form");
makeEl("div", "backtest-single-controls");
makeEl("div", "backtest-source-group");
makeEl("select", "backtest-entry-select");
makeEl("select", "backtest-end-select");
const btPaperSelect = makeEl("select", "backtest-paper-select");
const btStrategySelect = makeEl("select", "backtest-strategy-select");
btStrategySelect.value = "BULL_PUT_SPREAD";
makeEl("input", "backtest-dte-min").value = "30";
makeEl("input", "backtest-dte-max").value = "60";
makeEl("select", "backtest-cadence-select").value = "WEEKLY_DAY";
makeEl("select", "backtest-weekday-select").value = "4";
makeEl("input", "backtest-every-n-days").value = "5";
makeEl("input", "backtest-initial-capital").value = "10000";
makeEl("input", "backtest-max-open").value = "1";
makeEl("input", "backtest-qty").value = "1";
makeEl("input", "backtest-commission").value = "0.65";
makeEl("input", "backtest-start-date");
makeEl("input", "backtest-end-date");
makeEl("div", "backtest-price-basis-group");
makeEl("div", "backtest-close-mode-group");
makeEl("div", "backtest-tpsl-basis-group");
makeEl("input", "backtest-tp-pct").value = "50";
makeEl("input", "backtest-sl-pct").value = "100";
makeEl("input", "backtest-exit-dte");
makeEl("span", "backtest-entry-filters-label");
makeEl("input", "backtest-min-credit");
makeEl("input", "backtest-min-ror");
makeEl("div", "backtest-wheel-controls").hidden = true;
const btStrikeModeSelect = makeEl("select", "backtest-strike-mode-select");
btStrikeModeSelect.value = "TARGET_DELTA";
makeEl("input", "backtest-target-delta").value = "0.30";
const btCcFloorSelect = makeEl("select", "backtest-cc-floor-select");
btCcFloorSelect.value = "COST_BASIS";
const btStockStopSelect = makeEl("select", "backtest-stock-stop-select");
btStockStopSelect.value = "OPTION_ONLY";
const btCcItmSelect = makeEl("select", "backtest-cc-itm-select");
btCcItmSelect.value = "CALL_AWAY_AND_REBUY";
const btRollToggle = makeEl("input", "backtest-roll-toggle");
btRollToggle.checked = false;
const btRollCreditSelect = makeEl("select", "backtest-roll-credit-select");
btRollCreditSelect.value = "CREDIT_ONLY";
btRollCreditSelect.disabled = true;
makeEl("div", "backtest-legs-box");
makeEl("button", "backtest-import-lab-btn");
makeEl("button", "backtest-add-leg-btn");
makeEl("tbody", "backtest-legs-body");

const btPaperStrip = makeEl("section", "backtest-paper-strip");
const btPaperToggle = makeEl("button", "backtest-paper-toggle");
const btPaperToggleLabel = makeEl("span", "backtest-paper-toggle-label");
const btPaperTickerCount = makeEl("span", "backtest-paper-ticker-count");
btPaperTickerCount.hidden = true;
const btPaperDrawer = makeEl("div", "backtest-paper-drawer");
btPaperDrawer.hidden = true;
const btPaperSummary = makeEl("span", "backtest-paper-summary");
const btPaperFilter = makeEl("select", "backtest-paper-filter");
btPaperFilter.value = "ACTIVE";
const btPaperTickerFilter = makeEl("select", "backtest-paper-ticker-filter");
btPaperTickerFilter.value = "ALL";
makeEl("button", "backtest-paper-sync-ledger-btn");
makeEl("button", "backtest-paper-refresh-btn");
const btPaperList = makeEl("div", "backtest-paper-list");

makeEl("div", "backtest-warnings");
makeEl("div", "backtest-kpis");
makeEl("div", "backtest-chart-toolbar");
makeEl("span", "backtest-chart-caption");
makeEl("div", "backtest-chart-view-group");
makeEl("div", "backtest-chart");
const btTablesGrid = makeEl("div", "backtest-tables-grid");
const btTablesSplitter = makeEl("div", "backtest-tables-splitter");
makeEl("span", "backtest-trades-meta");
makeEl("tbody", "backtest-trades-body");
makeEl("strong", "backtest-steps-title");
makeEl("span", "backtest-steps-meta");
makeEl("tbody", "backtest-steps-body");
makeEl("ul", "backtest-assumptions");
makeEl("span", "backtest-cadence-legacy-hint").hidden = true;
makeEl("div", "backtest-capital-adequacy").hidden = true;
makeEl("span", "backtest-capital-adequacy-text");
makeEl("button", "backtest-match-capital-btn");
makeEl("section", "backtest-robustness-panel").hidden = true;
makeEl("span", "bt-rob-depth-badge");
makeEl("span", "bt-rob-timing");
makeEl("button", "bt-rob-run-all-btn");
makeEl("div", "bt-rob-verdicts");
makeEl("div", "bt-rob-tabs");
makeEl("div", "bt-rob-pane-cohorts");
makeEl("div", "bt-rob-pane-sensitivity").hidden = true;
makeEl("div", "bt-rob-pane-walkforward").hidden = true;
makeEl("div", "bt-rob-pane-montecarlo").hidden = true;
makeEl("input", "bt-rob-rolling-horizon");
makeEl("button", "bt-rob-rerun-cohorts-btn");
makeEl("div", "bt-rob-cohorts-kpis");
makeEl("span", "bt-rob-cohorts-chart-title");
makeEl("div", "bt-rob-cohorts-chart");
makeEl("table", "bt-rob-cohorts-table");
const btRobSensX = makeEl("select", "bt-rob-sens-x");
btRobSensX.value = "profit_target_pct";
const btRobSensY = makeEl("select", "bt-rob-sens-y");
btRobSensY.value = "stop_loss_pct";
const btRobSensObj = makeEl("select", "bt-rob-sens-obj");
btRobSensObj.value = "expectancy_dollars";
const btRobSensDensity = makeEl("select", "bt-rob-sens-density");
btRobSensDensity.value = "5";
makeEl("button", "bt-rob-rerun-sens-btn");
makeEl("div", "bt-rob-sens-kpis");
makeEl("span", "bt-rob-sens-chart-title");
makeEl("div", "bt-rob-sens-chart");
makeEl("span", "bt-rob-sens-cell-info");
makeEl("button", "bt-rob-apply-cell-btn").disabled = true;
const btRobWfoMode = makeEl("select", "bt-rob-wfo-mode");
btRobWfoMode.value = "PURGED_WFO";
const btRobWfoWindowType = makeEl("select", "bt-rob-wfo-window-type");
btRobWfoWindowType.value = "ROLLING";
makeEl("input", "bt-rob-wfo-is-bars").value = "120";
makeEl("input", "bt-rob-wfo-oos-bars").value = "40";
makeEl("input", "bt-rob-wfo-embargo").value = "0";
makeEl("input", "bt-rob-wfo-fixed-bars").value = "40";
const btRobWfoObj = makeEl("select", "bt-rob-wfo-obj");
btRobWfoObj.value = "expectancy_dollars";
makeEl("button", "bt-rob-rerun-wfo-btn");
makeEl("div", "bt-rob-wfo-kpis");
makeEl("div", "bt-rob-wfo-warn").hidden = true;
makeEl("span", "bt-rob-wfo-chart-title");
makeEl("div", "bt-rob-wfo-chart");
makeEl("table", "bt-rob-wfo-table");
makeEl("input", "bt-rob-mc-paths").value = "2000";
makeEl("input", "bt-rob-mc-block").value = "5";
makeEl("input", "bt-rob-mc-horizon");
makeEl("input", "bt-rob-mc-ruin").value = "25";
makeEl("input", "bt-rob-mc-seed").value = "42";
makeEl("input", "bt-rob-mc-slippage").checked = true;
makeEl("input", "bt-rob-mc-trade-boot").checked = true;
makeEl("button", "bt-rob-rerun-mc-btn");
makeEl("div", "bt-rob-mc-kpis");
makeEl("span", "bt-rob-mc-fan-chart-title");
makeEl("div", "bt-rob-mc-fan-chart");
makeEl("span", "bt-rob-mc-dd-chart-title");
makeEl("div", "bt-rob-mc-dd-chart");
makeEl("table", "bt-rob-mc-slippage-table");

globalThis.document = {
  documentElement: htmlEl,
  body: bodyEl,
  getElementById(id) {
    return elementsById[id] || null;
  },
  querySelector(sel) {
    return docNode.querySelector(sel);
  },
  querySelectorAll(sel) {
    return docNode.querySelectorAll(sel);
  },
  createElement(tag) {
    return new FakeNode(tag);
  },
  addEventListener(type, fn) {
    docNode.addEventListener(type, fn);
  },
  removeEventListener(type, fn) {
    docNode.removeEventListener(type, fn);
  },
  dispatchEvent(ev) {
    return docNode.dispatchEvent(ev);
  },
};

globalThis.window = {
  location: { reload() {} },
  Plotly: {
    react() {},
    purge() {},
    Plots: { resize() {} },
  },
  localStorage: {
    getItem(k) {
      return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null;
    },
    setItem(k, v) {
      store[k] = String(v);
    },
    removeItem(k) {
      delete store[k];
    },
  },
  getComputedStyle() {
    return { getPropertyValue: () => "#111111" };
  },
  fetch(url, init) {
    return nextFetchImpl(url, init);
  },
  addEventListener() {},
  removeEventListener() {},
  requestAnimationFrame(cb) {
    cb(16);
    return 1;
  },
  cancelAnimationFrame() {},
};
"""


def _run_node_scenario(script_body: str) -> dict[str, Any]:
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js binary not found on PATH for JS unit tests")
    full_script = NODE_HARNESS_PREAMBLE + "\n" + script_body
    env = os.environ.copy()
    env["DASHBOARD_ROOT"] = str(DASHBOARD_DIR).replace("\\", "/")
    proc = subprocess.run(
        [node_bin, "--input-type=module", "-e", full_script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"Node ES-module test failed (rc={proc.returncode}):\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    return json.loads(lines[-1]) if lines else {}


def test_backtest_tables_splitter_drag_persist_reset_and_keyboard() -> None:
    result = _run_node_scenario(
        """
        const { state, BACKTEST_SPLIT_KEY } = await import(modUrl("state.js"));
        const tables = await import(modUrl("backtest_tables.js"));

        // 1. Pure clamping & coordinate conversion helpers
        assert.equal(tables.DEFAULT_BACKTEST_SPLIT_PCT, 62);
        assert.equal(tables.clampBacktestSplitPct("bad"), 62);
        assert.equal(tables.clampBacktestSplitPct(10), 40);
        assert.equal(tables.clampBacktestSplitPct(95), 78);
        assert.equal(tables.clampBacktestSplitPct(64.26), 64.3);

        assert.equal(tables.computeBacktestSplitPct(700, { left: 100, width: 1000 }), 60);
        assert.equal(tables.computeBacktestSplitPct(150, { left: 100, width: 1000 }), 40);
        assert.equal(tables.computeBacktestSplitPct(980, { left: 100, width: 1000 }), 78);
        assert.equal(tables.computeBacktestSplitPct(500, { left: 0, width: 0 }), 62);

        // 2. Default init uses 62%
        btTablesGrid._rectFn = () => ({
          left: 100, top: 0, right: 1100, bottom: 300, width: 1000, height: 300,
        });
        tables.initBacktestTablesSplitter();
        assert.equal(state.backtestSplitPct, 62);
        assert.equal(btTablesGrid.style.getPropertyValue("--bt-trades-col"), "62%");
        assert.equal(btTablesSplitter.getAttribute("aria-valuenow"), "62");

        // 3. Pointer drag updates CSS variable, aria-valuenow, and persists on pointerup
        btTablesSplitter.dispatchEvent({
          type: "pointerdown", button: 0, pointerId: 1, clientX: 720,
        });
        assert.equal(btTablesSplitter.classList.contains("is-dragging"), true);
        assert.equal(btTablesGrid.classList.contains("is-dragging"), true);

        btTablesSplitter.dispatchEvent({ type: "pointermove", pointerId: 1, clientX: 800 });
        assert.equal(state.backtestSplitPct, 70);
        assert.equal(btTablesGrid.style.getPropertyValue("--bt-trades-col"), "70%");
        assert.equal(btTablesSplitter.getAttribute("aria-valuenow"), "70");

        btTablesSplitter.dispatchEvent({ type: "pointerup", pointerId: 1, clientX: 800 });
        assert.equal(btTablesSplitter.classList.contains("is-dragging"), false);
        assert.equal(btTablesGrid.classList.contains("is-dragging"), false);
        assert.deepEqual(JSON.parse(store[BACKTEST_SPLIT_KEY]), { leftPct: 70 });

        // 4. Re-init restores persisted split preference
        state.backtestSplitPct = 62;
        tables.initBacktestTablesSplitter();
        assert.equal(state.backtestSplitPct, 70);
        assert.equal(btTablesGrid.style.getPropertyValue("--bt-trades-col"), "70%");

        // 5. Double-click resets to 62% and clears preference
        btTablesSplitter.dispatchEvent({ type: "dblclick" });
        assert.equal(state.backtestSplitPct, 62);
        assert.equal(btTablesGrid.style.getPropertyValue("--bt-trades-col"), "62%");
        assert.equal(btTablesSplitter.getAttribute("aria-valuenow"), "62");
        assert.equal(store[BACKTEST_SPLIT_KEY], undefined);

        // 6. Keyboard support: ArrowRight, ArrowLeft, Home, End, Enter
        btTablesSplitter.dispatchEvent({ type: "keydown", key: "ArrowRight" });
        assert.equal(state.backtestSplitPct, 64);
        assert.deepEqual(JSON.parse(store[BACKTEST_SPLIT_KEY]), { leftPct: 64 });

        btTablesSplitter.dispatchEvent({ type: "keydown", key: "ArrowLeft" });
        assert.equal(state.backtestSplitPct, 62);

        btTablesSplitter.dispatchEvent({ type: "keydown", key: "Home" });
        assert.equal(state.backtestSplitPct, 40);

        btTablesSplitter.dispatchEvent({ type: "keydown", key: "End" });
        assert.equal(state.backtestSplitPct, 78);

        btTablesSplitter.dispatchEvent({ type: "keydown", key: "Enter" });
        assert.equal(state.backtestSplitPct, 62);
        assert.equal(store[BACKTEST_SPLIT_KEY], undefined);

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_backtest_paper_drawer_toggle_table_filter_and_replay_autocollapse() -> None:
    result = _run_node_scenario(
        """
        const { state } = await import(modUrl("state.js"));
        const paper = await import(modUrl("backtest_paper.js"));
        const bt = await import(modUrl("backtest.js"));

        const sampleTrades = [
          {
            id: "NVDA-20261112-aaaa1111",
            ticker: "NVDA",
            strategy_kind: "BULL_PUT_SPREAD",
            ledger_strategy_label: "Bull Put Credit Spread",
            source: "LEDGER",
            status: "ACTIVE",
            qty: 2,
            entry_net_cash: 240.0,
            expiry: "2026-12-18",
            market_date: "2026-11-12",
            legs: [
              {
                side: "SELL", right: "P", strike: 140, expiry: "2026-12-18",
                qty_ratio: 1, entry_price: 3.2,
              },
              {
                side: "BUY", right: "P", strike: 135, expiry: "2026-12-18",
                qty_ratio: 1, entry_price: 2.0,
              },
            ],
          },
          {
            id: "AVGO-20261112-bbbb2222",
            ticker: "AVGO",
            strategy_kind: "IRON_CONDOR",
            source: "MANUAL",
            status: "ACTIVE",
            qty: 1,
            entry_net_cash: 150.0,
            expiry: "2026-12-18",
            market_date: "2026-11-12",
            legs: [
              {
                side: "SELL", right: "P", strike: 170, expiry: "2026-12-18",
                qty_ratio: 1, entry_price: 2.5,
              },
            ],
          },
        ];

        // 1. Pure ticker filter helper
        assert.equal(paper.filterPaperTradesByTicker(sampleTrades, "ALL", "NVDA").length, 2);
        assert.deepEqual(
          paper.filterPaperTradesByTicker(sampleTrades, "CURRENT", "NVDA").map((t) => t.id),
          ["NVDA-20261112-aaaa1111"],
        );
        assert.deepEqual(
          paper.filterPaperTradesByTicker(sampleTrades, "AVGO", "NVDA").map((t) => t.id),
          ["AVGO-20261112-bbbb2222"],
        );
        assert.equal(paper.filterPaperTradesByTicker(sampleTrades, "MSFT", "NVDA").length, 0);

        nextFetchImpl = async (url, init) => {
          const sUrl = String(url);
          fetchCalls.push({ url: sUrl, init: init || {} });
          if (sUrl.startsWith("/api/backtest/paper-trades")) {
            return {
              ok: true,
              status: 200,
              text: async () => JSON.stringify({
                trades: sampleTrades,
                active_count: 2,
                max_active_trades: 120,
              }),
            };
          }
          if (sUrl.startsWith("/api/backtest/entries/")) {
            return {
              ok: true,
              status: 200,
              text: async () => JSON.stringify({ ticker: "MSFT", entries: [] }),
            };
          }
          if (sUrl === "/api/backtest/run") {
            return {
              ok: true,
              status: 200,
              text: async () => JSON.stringify({
                mode: "SINGLE_TRADE",
                daily_bar_count: 1,
                warnings: [],
                simulation: {
                  mode: "SINGLE_TRADE",
                  ticker: "NVDA",
                  config: { close_price_mode: "MID", price_basis: "AUTO_MID_LAST_CLOSE" },
                  summary: {
                    initial_capital: 10000,
                    final_equity: 10120,
                    net_pnl_dollars: 120,
                    gross_pnl_dollars: 122.6,
                    total_commissions_dollars: 2.6,
                    total_return_pct: 1.2,
                    realized_pnl_dollars: 0,
                    unrealized_pnl_dollars: 120,
                    trade_count: 1,
                    closed_trades: 0,
                    open_trades: 1,
                    winning_trades: 0,
                    losing_trades: 0,
                    max_drawdown_dollars: 0,
                    max_drawdown_pct: 0,
                    ok_bars: 1,
                    flagged_bars: 0,
                    skipped_entries_count: 0,
                    skipped_by_reason: {},
                  },
                  equity_curve: [],
                  trades: [],
                  notes: [],
                  assumptions: [],
                },
              }),
            };
          }
          return { ok: true, status: 200, text: async () => JSON.stringify({}) };
        };

        state.ticker = "NVDA";
        bt.bindBacktestEvents();
        bt.initBacktestPanel();
        await new Promise((r) => setTimeout(r, 20));

        // 2. Starts collapsed by default even after paper trades load
        assert.equal(state.backtestPaperExpanded, false);
        assert.equal(btPaperDrawer.hidden, true);
        assert.equal(btPaperToggle.getAttribute("aria-expanded"), "false");
        assert.equal(btPaperToggleLabel.textContent, "Show");
        assert.equal(btPaperStrip.classList.contains("is-expanded"), false);
        assert.equal(btPaperSummary.textContent, "2 / 120 active");
        assert.equal(btPaperTickerCount.hidden, false);
        assert.equal(btPaperTickerCount.textContent, "1 for NVDA");

        // 3. Toggle expands and shrinks cleanly
        btPaperToggle.dispatchEvent({ type: "click" });
        assert.equal(state.backtestPaperExpanded, true);
        assert.equal(btPaperDrawer.hidden, false);
        assert.equal(btPaperToggle.getAttribute("aria-expanded"), "true");
        assert.equal(btPaperToggleLabel.textContent, "Hide");
        assert.equal(btPaperStrip.classList.contains("is-expanded"), true);

        btPaperToggle.dispatchEvent({ type: "click" });
        assert.equal(state.backtestPaperExpanded, false);
        assert.equal(btPaperDrawer.hidden, true);
        assert.equal(btPaperToggle.getAttribute("aria-expanded"), "false");
        assert.equal(btPaperToggleLabel.textContent, "Show");

        // 4. Re-expand and inspect aligned table render + ticker filter isolation
        btPaperToggle.dispatchEvent({ type: "click" });
        const tableCls = 'table class="backtest-table backtest-paper-table"';
        assert.ok(btPaperList.innerHTML.includes(tableCls));
        assert.ok(btPaperList.innerHTML.includes("backtest-source-badge--ledger"));
        assert.equal((btPaperList.innerHTML.match(/data-paper-id=/g) || []).length, 2);
        assert.equal(btPaperSelect.children.length, 2);

        btPaperTickerFilter.value = "CURRENT";
        btPaperTickerFilter.dispatchEvent({ type: "change" });
        assert.equal(state.backtestPaperTickerFilter, "CURRENT");
        assert.equal((btPaperList.innerHTML.match(/data-paper-id=/g) || []).length, 1);
        assert.ok(btPaperList.innerHTML.includes("NVDA-20261112-aaaa1111"));
        assert.ok(!btPaperList.innerHTML.includes("AVGO-20261112-bbbb2222"));
        // Single Trade Replay dropdown stays unfiltered
        assert.equal(btPaperSelect.children.length, 2);

        // 5. Clicking Replay inside the drawer auto-collapses it and triggers /api/backtest/run
        fetchCalls.length = 0;
        const replayBtn = new FakeNode("button");
        replayBtn.setAttribute("data-paper-replay", "NVDA-20261112-aaaa1111");
        btPaperList.appendChild(replayBtn);
        replayBtn.dispatchEvent({ type: "click" });
        await new Promise((r) => setTimeout(r, 20));

        assert.equal(state.backtestPaperExpanded, false);
        assert.equal(btPaperDrawer.hidden, true);
        assert.equal(btPaperToggle.getAttribute("aria-expanded"), "false");
        assert.equal(btPaperToggleLabel.textContent, "Show");
        assert.ok(fetchCalls.some((c) => c.url === "/api/backtest/run"));

        // 6. Switching to a ticker with 0 paper trades hides the ticker count pill
        state.ticker = "MSFT";
        await bt.backtestOnSnapshotsLoaded("MSFT");
        assert.equal(btPaperTickerCount.hidden, true);
        assert.equal(btPaperTickerCount.textContent, "");

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_backtest_wheel_controls_payload_and_opt_stock_breakdown_rendering() -> None:
    result = _run_node_scenario(
        """
        const { state } = await import(modUrl("state.js"));
        const bt = await import(modUrl("backtest.js"));
        const tables = await import(modUrl("backtest_tables.js"));

        const btWheelControls = elementsById["backtest-wheel-controls"];
        const btFiltersLabel = elementsById["backtest-entry-filters-label"];
        let capturedRunPayload = null;

        nextFetchImpl = async (url, init) => {
          const sUrl = String(url);
          if (sUrl === "/api/backtest/run") {
            capturedRunPayload = JSON.parse(String(init.body || "{}"));
            return {
              ok: true,
              status: 200,
              text: async () => JSON.stringify({
                mode: "SYSTEMATIC",
                daily_bar_count: 2,
                warnings: [],
                simulation: {
                  mode: "SYSTEMATIC",
                  ticker: "NVDA",
                  config: capturedRunPayload.config,
                  summary: {
                    initial_capital: 10000,
                    final_equity: 10700,
                    net_pnl_dollars: 700,
                    gross_pnl_dollars: 700.65,
                    total_commissions_dollars: 0.65,
                    total_return_pct: 7.0,
                    realized_pnl_dollars: 700,
                    unrealized_pnl_dollars: 0,
                    trade_count: 1,
                    closed_trades: 1,
                    open_trades: 0,
                    winning_trades: 1,
                    losing_trades: 0,
                    max_drawdown_dollars: 0,
                    max_drawdown_pct: 0,
                    ok_bars: 2,
                    flagged_bars: 0,
                    skipped_entries_count: 0,
                    skipped_by_reason: {},
                  },
                  equity_curve: [],
                  trades: [
                    {
                      trade_id: "NVDA_BT_0001",
                      entry_date: "2026-11-06",
                      entry_snapshot_id: "s0",
                      exit_date: "2026-12-18",
                      exit_snapshot_id: "s1",
                      exit_reason: "CALLED_AWAY",
                      strategy_kind: "COVERED_CALL",
                      wheel_phase: "CC",
                      stock_shares: 100,
                      stock_cost_basis: 100.0,
                      stock_exit_price: 105.0,
                      option_net_pnl_dollars: 200.0,
                      stock_pnl_dollars: 500.0,
                      qty: 1,
                      entry_cash_mid_dollars: 200.65,
                      entry_cash_natural_dollars: 190.0,
                      entry_basis_dollars: 200.65,
                      commissions_dollars: 0.65,
                      gross_pnl_dollars: 700.65,
                      net_pnl_dollars: 700.0,
                      return_on_basis_pct: 348.87,
                      return_on_risk_pct: 7.14,
                      days_held: 42,
                      status: "CLOSED",
                      legs: [
                        {
                          side: "SELL", right: "C", expiry: "2026-12-18", dte: 42,
                          strike: 105.0, qty: 1, bid: 1.9, ask: 2.1, mid: 2.0, natural_fill: 1.9,
                        },
                      ],
                      steps: [
                        {
                          market_date: "2026-11-06",
                          snapshot_id: "s0",
                          ts_utc: "2026-11-06T20:00:00Z",
                          ts_et: "2026-11-06 15:00 ET",
                          dte_min_leg: 42,
                          spot: 100.0,
                          legs: [],
                          net_pnl_dollars: -0.65,
                          pnl_pct_of_basis: -0.32,
                          stock_shares: 100,
                          stock_cost_basis: 100.0,
                          stock_unrealized_pnl_dollars: 0.0,
                          status: "OK",
                        },
                        {
                          market_date: "2026-12-18",
                          snapshot_id: "s1",
                          ts_utc: "2026-12-18T21:20:00Z",
                          ts_et: "2026-12-18 16:20 ET",
                          dte_min_leg: 0,
                          spot: 110.0,
                          legs: [],
                          net_pnl_dollars: 700.0,
                          pnl_pct_of_basis: 348.87,
                          stock_shares: 0,
                          stock_cost_basis: 100.0,
                          stock_unrealized_pnl_dollars: 0.0,
                          status: "OK",
                        },
                      ],
                    },
                  ],
                  notes: [],
                  assumptions: [],
                },
              }),
            };
          }
          return {
            ok: true,
            status: 200,
            text: async () => JSON.stringify({ trades: [], active_count: 0 }),
          };
        };

        state.ticker = "NVDA";
        bt.bindBacktestEvents();
        bt.initBacktestPanel();

        // 1. Standard preset keeps Wheel controls hidden
        assert.equal(btWheelControls.hidden, true);
        assert.equal(btFiltersLabel.textContent, "BPS Entry Filters");

        // 2. Selecting WHEEL reveals controls, enables Roll Credit Rule on toggle
        btStrategySelect.value = "WHEEL";
        btStrategySelect.dispatchEvent({ type: "change" });
        assert.equal(btWheelControls.hidden, false);
        assert.equal(btFiltersLabel.textContent, "Credit Entry Filter");
        assert.equal(btRollCreditSelect.disabled, true);

        btRollToggle.checked = true;
        btRollToggle.dispatchEvent({ type: "change" });
        assert.equal(btRollCreditSelect.disabled, false);

        btCcFloorSelect.value = "NET_BREAKEVEN";
        btStockStopSelect.value = "LIQUIDATE_STOCK_ON_SL";
        btRollCreditSelect.value = "ZERO_OR_CREDIT";

        // 3. Running backtest sends configured fields and renders Opt / Stk breakdown
        await bt.runBacktestStudy();
        assert.ok(capturedRunPayload);
        assert.equal(capturedRunPayload.config.strategy_kind, "WHEEL");
        assert.equal(capturedRunPayload.config.strike_selection_mode, "TARGET_DELTA");
        assert.equal(capturedRunPayload.config.target_delta, 0.3);
        assert.equal(capturedRunPayload.config.cc_strike_floor, "NET_BREAKEVEN");
        assert.equal(capturedRunPayload.config.stock_stop_mode, "LIQUIDATE_STOCK_ON_SL");
        assert.equal(capturedRunPayload.config.roll_enabled, true);
        assert.equal(capturedRunPayload.config.roll_credit_mode, "ZERO_OR_CREDIT");

        const tbodyHtml = elementsById["backtest-trades-body"].innerHTML;
        assert.ok(tbodyHtml.includes("Covered Call"));
        assert.ok(tbodyHtml.includes("100 shs @ $100"));
        assert.ok(tbodyHtml.includes("Opt +$200.00 / Stk +$500.00"));

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_backtest_robustness_mode_cadence_hint_and_sensitivity_apply() -> None:
    result = _run_node_scenario(
        """
        const { state } = await import(modUrl("state.js"));
        const bt = await import(modUrl("backtest.js"));
        const rob = await import(modUrl("backtest_robustness.js"));

        const cadenceSel = elementsById["backtest-cadence-select"];
        const legacyHint = elementsById["backtest-cadence-legacy-hint"];
        const robPanel = elementsById["backtest-robustness-panel"];
        const runBtn = elementsById["backtest-run-btn"];
        const savePaperBtn = elementsById["backtest-save-paper-btn"];

        let capturedPayloads = [];
        nextFetchImpl = async (url, init) => {
          const sUrl = String(url);
          if (sUrl === "/api/backtest/robustness") {
            const body = JSON.parse(String(init.body || "{}"));
            capturedPayloads.push(body);
            const hasCohorts = body.sections.includes("COHORTS");
            const hasSens = body.sections.includes("SENSITIVITY");
            return {
              ok: true,
              status: 200,
              text: async () => JSON.stringify({
                history_snapshot_count: 15,
                daily_bar_count: 15,
                cache_hit: capturedPayloads.length > 1,
                warnings: [],
                result: {
                  ticker: "NVDA",
                  strategy_kind: body.config.strategy_kind,
                  total_bars: 15,
                  data_sufficiency_tier: "LIMITED",
                  requested_sections: body.sections,
                  compute_ms: 42.5,
                  notes: [],
                  verdicts: [
                    {
                      section: "COHORTS",
                      status: hasCohorts ? "PASS" : "SKIPPED",
                      headline: hasCohorts ? "Time-invariant +$25.00/trade" : "Skipped",
                      detail: "15 bars",
                    },
                    {
                      section: "SENSITIVITY",
                      status: hasSens ? "PASS" : "SKIPPED",
                      headline: hasSens ? "Stable plateau (72/100)" : "Skipped",
                      detail: "80% profitable",
                    },
                    {
                      section: "WALK_FORWARD",
                      status: "WARN",
                      headline: "Insufficient bars",
                      detail: "Short",
                    },
                    {
                      section: "MONTE_CARLO",
                      status: "PASS",
                      headline: "P(Profit) 70%",
                      detail: "Low ruin",
                    },
                  ],
                  cohorts: hasCohorts ? {
                    mode: "ALL_DAYS_SPREADS",
                    total_bars: 15,
                    attempted_entries: 12,
                    entered_cohorts: 10,
                    closed_cohorts: 8,
                    open_cohorts: 2,
                    unresolved_cohorts: 0,
                    avg_concurrent_positions: 2.5,
                    win_rate_pct: 75.0,
                    expectancy_dollars: 25.0,
                    avg_return_on_risk_pct: 8.5,
                    profit_factor: 2.1,
                    total_net_pnl_dollars: 200.0,
                    portfolio_total_return_pct: 2.0,
                    portfolio_max_drawdown_pct: 1.2,
                    portfolio_sharpe: 1.4,
                    equity_curve: [],
                    weekday_stability: [],
                    weekday_skew_detected: false,
                    rolling_cohorts: [],
                  } : null,
                  sensitivity: hasSens ? {
                    x_param: "profit_target_pct",
                    y_param: "stop_loss_pct",
                    objective: "expectancy_dollars",
                    x_values: [35, 50],
                    y_values: [75, 100],
                    plateau_stability_score: 72.0,
                    profitable_cells_pct: 80.0,
                    valid_cells_count: 4,
                    total_cells_count: 4,
                    deflated_sharpe_ratio: 0.9,
                    multiple_testing_warning: false,
                    cells: [
                      {
                        x_index: 0, y_index: 0, x_value: 35, y_value: 75,
                        x_label: "35%", y_label: "75%", objective_value: 30,
                        smoothed_score: 25, trade_count: 8, closed_trades: 8,
                        win_rate_pct: 75, expectancy_dollars: 30, total_return_pct: 2.4,
                        max_drawdown_pct: 1.0, is_baseline: false, is_optimal: true,
                      },
                    ],
                    optimal_cell: {
                      x_index: 0, y_index: 0, x_value: 35, y_value: 75,
                      x_label: "35%", y_label: "75%", objective_value: 30,
                      smoothed_score: 25, trade_count: 8, closed_trades: 8,
                      win_rate_pct: 75, expectancy_dollars: 30, total_return_pct: 2.4,
                      max_drawdown_pct: 1.0, is_baseline: false, is_optimal: true,
                    },
                  } : null,
                  walk_forward: null,
                  monte_carlo: null,
                },
              }),
            };
          }
          return {
            ok: true,
            status: 200,
            text: async () => JSON.stringify({ trades: [], active_count: 0 }),
          };
        };

        state.ticker = "NVDA";
        bt.bindBacktestEvents();
        bt.initBacktestPanel();

        // 1. Legacy cadence hint shows only in SYSTEMATIC + WEEKLY_DAY
        assert.equal(legacyHint.hidden, false);
        cadenceSel.value = "WHEN_FLAT";
        cadenceSel.dispatchEvent({ type: "change" });
        assert.equal(legacyHint.hidden, true);
        cadenceSel.value = "WEEKLY_DAY";
        cadenceSel.dispatchEvent({ type: "change" });
        assert.equal(legacyHint.hidden, false);

        // 2. Switching to ROBUSTNESS hides systematic outputs & shows robustness panel
        state.backtestMode = "ROBUSTNESS";
        bt.syncBacktestControlVisibility();
        assert.equal(robPanel.hidden, false);
        assert.equal(elementsById["backtest-kpis"].hidden, true);
        assert.equal(elementsById["backtest-chart"].hidden, true);
        assert.equal(elementsById["backtest-tables-grid"].hidden, true);
        assert.equal(savePaperBtn.hidden, true);
        assert.equal(savePaperBtn.disabled, true);
        assert.equal(runBtn.textContent, "RUN FULL AUDIT");
        assert.equal(legacyHint.hidden, true);

        // 3. Running Full Audit via runBacktestStudy hits /api/backtest/robustness
        await bt.runBacktestStudy();
        assert.equal(capturedPayloads.length, 1);
        assert.deepEqual(
          capturedPayloads[0].sections,
          ["COHORTS", "SENSITIVITY", "WALK_FORWARD", "MONTE_CARLO"],
        );
        const badgeTxt = elementsById["bt-rob-depth-badge"].textContent;
        assert.equal(badgeTxt, "LIMITED DEPTH \\u2022 15 BARS");
        const verdictsHtml = elementsById["bt-rob-verdicts"].innerHTML;
        assert.ok(verdictsHtml.includes("Time-invariant +$25.00/trade"));

        // 4. Single-section re-run preserves existing COHORTS data
        await rob.runRobustnessAudit(["SENSITIVITY"]);
        assert.equal(capturedPayloads.length, 2);
        assert.deepEqual(capturedPayloads[1].sections, ["SENSITIVITY"]);
        assert.ok(state.backtestRobustnessResult.cohorts !== null);
        assert.ok(state.backtestRobustnessResult.sensitivity !== null);

        // 5. Apply optimal cell -> SYSTEMATIC, updates TP/SL with human-readable labels
        elementsById["bt-rob-apply-cell-btn"].dispatchEvent({ type: "click" });
        assert.equal(state.backtestMode, "SYSTEMATIC");
        assert.equal(robPanel.hidden, true);
        assert.equal(elementsById["backtest-tp-pct"].value, "35");
        assert.equal(elementsById["backtest-sl-pct"].value, "75");
        const noticeHtml = elementsById["backtest-warnings"].innerHTML;
        assert.ok(noticeHtml.includes("Profit Target % = 35%"));
        assert.ok(noticeHtml.includes("Stop Loss % = 75%"));
        assert.ok(!noticeHtml.includes("profit_target_pct"));

        // 5b. Verdict strip renders native <button> cards (no role="button")
        assert.ok(verdictsHtml.includes('<button type="button" class="bt-rob-verdict-card'));
        assert.ok(!verdictsHtml.includes('role="button"'));

        // 6. Selecting COVERED_CALL sets cadence to WHEN_FLAT and sensitivity delta x TP axes
        btStrategySelect.value = "COVERED_CALL";
        btStrategySelect.dispatchEvent({ type: "change" });
        assert.equal(cadenceSel.value, "WHEN_FLAT");
        assert.equal(btRobSensX.value, "target_delta");
        assert.equal(btRobSensY.value, "profit_target_pct");

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_robustness_heatmap_contrast_wfo_title_tabs_and_hidden_theme_rerender() -> None:
    result = _run_node_scenario(
        """
        const { state } = await import(modUrl("state.js"));
        const helpers = await import(modUrl("helpers.js"));
        const panels = await import(modUrl("panels.js"));
        const rob = await import(modUrl("backtest_robustness.js"));

        // Use real Print / Terminal fallbacks from helpers.themeVars()
        let activeTheme = "print";
        htmlEl.getAttribute = (name) => (name === "data-theme" ? activeTheme : null);
        window.getComputedStyle = () => ({ getPropertyValue: () => "" });

        // Capture Plotly.react calls
        const plotlyCalls = [];
        window.Plotly.react = (el, traces, layout) => {
          el.data = traces;
          plotlyCalls.push({ id: el.id, traces, layout });
        };

        // Wire tab buttons inside #bt-rob-tabs to verify aria-selected without aria-pressed
        const btRobTabs = elementsById["bt-rob-tabs"];
        for (const sec of ["COHORTS", "SENSITIVITY", "WALK_FORWARD", "MONTE_CARLO"]) {
          const btn = new FakeNode("button");
          btn.setAttribute("role", "tab");
          btn.setAttribute("data-rob-tab", sec);
          btn.setAttribute("aria-selected", sec === "COHORTS" ? "true" : "false");
          btRobTabs.appendChild(btn);
        }
        rob.setActiveRobustnessTab("SENSITIVITY");
        for (const btn of btRobTabs.children) {
          const sec = btn.getAttribute("data-rob-tab");
          assert.equal(btn.getAttribute("aria-selected"), sec === "SENSITIVITY" ? "true" : "false");
          assert.equal(btn.getAttribute("aria-pressed"), null);
        }

        // Verify pickHeatmapTextColor chooses high-contrast ink vs bg in both Print and Terminal
        const printTheme = helpers.themeVars();
        assert.equal(rob.pickHeatmapTextColor(-100, -100, 100, printTheme), printTheme.bg);
        assert.equal(rob.pickHeatmapTextColor(0, -100, 100, printTheme), printTheme.ink);
        assert.equal(rob.pickHeatmapTextColor(100, -100, 100, printTheme), printTheme.bg);

        activeTheme = "terminal";
        const termTheme = helpers.themeVars();
        assert.equal(rob.pickHeatmapTextColor(-100, -100, 100, termTheme), termTheme.bg);
        assert.equal(rob.pickHeatmapTextColor(0, -100, 100, termTheme), termTheme.ink);
        assert.equal(rob.pickHeatmapTextColor(100, -100, 100, termTheme), termTheme.bg);

        // Render full robustness result in Print theme
        activeTheme = "print";
        state.backtestMode = "ROBUSTNESS";
        state.backtestRobustnessResult = {
          ticker: "NVDA",
          strategy_kind: "BULL_PUT_SPREAD",
          total_bars: 60,
          data_sufficiency_tier: "MODERATE",
          requested_sections: ["COHORTS", "SENSITIVITY", "WALK_FORWARD", "MONTE_CARLO"],
          compute_ms: 12,
          notes: [],
          verdicts: [],
          cohorts: null,
          sensitivity: {
            x_param: "profit_target_pct",
            y_param: "stop_loss_pct",
            objective: "expectancy_dollars",
            x_values: [35, 50],
            y_values: [75, 100],
            plateau_stability_score: 70,
            profitable_cells_pct: 66.7,
            valid_cells_count: 4,
            total_cells_count: 4,
            deflated_sharpe_ratio: 0.85,
            multiple_testing_warning: false,
            cells: [
              {
                x_index: 0, y_index: 0, x_value: 35, y_value: 75,
                x_label: "35%", y_label: "75%", objective_value: -50,
                smoothed_score: -40, trade_count: 10, closed_trades: 10,
                win_rate_pct: 40, expectancy_dollars: -50, total_return_pct: -2,
                max_drawdown_pct: 4, is_baseline: false, is_optimal: false,
              },
              {
                x_index: 1, y_index: 0, x_value: 50, y_value: 75,
                x_label: "50%", y_label: "75%", objective_value: 0,
                smoothed_score: 0, trade_count: 10, closed_trades: 10,
                win_rate_pct: 50, expectancy_dollars: 0, total_return_pct: 0,
                max_drawdown_pct: 2, is_baseline: true, is_optimal: false,
              },
              {
                x_index: 1, y_index: 1, x_value: 50, y_value: 100,
                x_label: "50%", y_label: "100%", objective_value: 50,
                smoothed_score: 45, trade_count: 10, closed_trades: 10,
                win_rate_pct: 80, expectancy_dollars: 50, total_return_pct: 5,
                max_drawdown_pct: 1, is_baseline: false, is_optimal: true,
              },
            ],
            baseline_cell: { x_index: 1, y_index: 0, x_label: "50%", y_label: "75%" },
            optimal_cell: { x_index: 1, y_index: 1, x_label: "50%", y_label: "100%" },
          },
          walk_forward: {
            mode: "PURGED_WFO",
            wfe_pct: 65,
            oos_total_return_pct: 4.5,
            oos_max_drawdown_pct: 1.5,
            baseline_oos_total_return_pct: 3.0,
            positive_windows_pct: 100,
            parameter_drift_ratio: 0.1,
            warning_message: null,
            stitched_oos_curve: [
              { market_date: "2026-10-01", equity_dollars: 10000 },
              { market_date: "2026-10-15", equity_dollars: 10450 },
            ],
            stitched_baseline_curve: [],
            folds: [],
          },
          monte_carlo: null,
        };

        rob.renderRobustnessResult(state.backtestRobustnessResult, helpers.themeVars());

        const sensCall = plotlyCalls.find((c) => c.id === "bt-rob-sens-chart");
        assert.ok(sensCall);
        assert.equal(sensCall.layout.title, undefined);
        assert.equal(sensCall.traces[0].texttemplate, undefined);
        assert.deepEqual(sensCall.traces[0].x, [0, 1]);
        assert.deepEqual(sensCall.traces[0].y, [0, 1]);
        assert.deepEqual(sensCall.traces[1].x, [1]);
        assert.deepEqual(sensCall.traces[1].y, [0]);
        assert.deepEqual(sensCall.traces[2].x, [1]);
        assert.deepEqual(sensCall.traces[2].y, [1]);
        assert.equal(sensCall.layout.xaxis.tickmode, "array");
        assert.deepEqual(sensCall.layout.xaxis.tickvals, [0, 1]);
        assert.deepEqual(sensCall.layout.xaxis.ticktext, ["35%", "50%"]);
        assert.equal(sensCall.layout.yaxis.tickmode, "array");
        assert.deepEqual(sensCall.layout.yaxis.tickvals, [0, 1]);
        assert.deepEqual(sensCall.layout.yaxis.ticktext, ["75%", "100%"]);
        assert.equal(sensCall.traces[0].zmin, -50);
        assert.equal(sensCall.traces[0].zmax, 50);
        assert.equal(sensCall.layout.annotations.length, 3);
        assert.equal(sensCall.layout.annotations[0].x, 0);
        assert.equal(sensCall.layout.annotations[0].y, 0);
        assert.equal(sensCall.layout.annotations[0].font.color, printTheme.bg);
        assert.equal(sensCall.layout.annotations[1].font.color, printTheme.ink);
        assert.equal(sensCall.layout.annotations[2].x, 1);
        assert.equal(sensCall.layout.annotations[2].y, 1);
        assert.equal(sensCall.layout.annotations[2].font.color, printTheme.bg);
        assert.ok(sensCall.layout.annotations[2].text.includes("\\u2605 OPT"));
        const sensTitleTxt = elementsById["bt-rob-sens-chart-title"].textContent;
        assert.ok(sensTitleTxt.includes("2D Sensitivity Heatmap"));

        const wfoCall = plotlyCalls.find((c) => c.id === "bt-rob-wfo-chart");
        assert.ok(wfoCall);
        assert.equal(wfoCall.layout.title, undefined);
        const wfoTitleTxt = elementsById["bt-rob-wfo-chart-title"].textContent;
        assert.ok(wfoTitleTxt.includes("Purged Walk-Forward"));
        assert.ok(!wfoTitleTxt.includes("PURGED_WFO"));

        // Switch away from backtest workspace, change theme to terminal, and return to backtest
        panels.applyWorkspaceMode("desk", { persist: false });
        activeTheme = "terminal";
        plotlyCalls.length = 0;
        panels.applyWorkspaceMode("backtest", { persist: false });

        const revealedSensCall = plotlyCalls.find((c) => c.id === "bt-rob-sens-chart");
        assert.ok(
          revealedSensCall,
          "returning to backtest workspace re-renders robustness charts with new theme",
        );
        assert.equal(revealedSensCall.layout.paper_bgcolor, termTheme.bg);
        assert.equal(revealedSensCall.layout.annotations[0].font.color, termTheme.bg);
        assert.equal(revealedSensCall.layout.annotations[1].font.color, termTheme.ink);

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_bounded_drawdown_axis_disable_capital_adequacy_and_stale_invalidation() -> None:
    result = _run_node_scenario(
        """
        const { state } = await import(modUrl("state.js"));
        const helpers = await import(modUrl("helpers.js"));
        const bt = await import(modUrl("backtest.js"));
        const btCharts = await import(modUrl("backtest_charts.js"));
        const rob = await import(modUrl("backtest_robustness.js"));

        const plotlyCalls = [];
        window.Plotly.react = (el, traces, layout) => {
          el.data = traces;
          plotlyCalls.push({ id: el.id, traces, layout });
        };

        // 1. Bounded yaxis2.range on Systematic & Cohorts charts, and MC MaxDD tickangle
        btCharts.renderBacktestChart(
          {
            simulation: {
              mode: "SYSTEMATIC",
              summary: { initial_capital: 10000 },
              trades: [],
              equity_curve: [
                {
                  market_date: "2026-10-01",
                  equity_dollars: 10000,
                  drawdown_dollars: 0,
                  drawdown_pct: 0,
                },
                {
                  market_date: "2026-10-02",
                  equity_dollars: 9500,
                  drawdown_dollars: 500,
                  drawdown_pct: 5,
                },
              ],
            },
          },
          helpers.themeVars(),
        );
        const sysCall = plotlyCalls.find((c) => c.id === "backtest-chart");
        assert.ok(sysCall);
        assert.equal(sysCall.layout.title, undefined);
        assert.deepEqual(sysCall.layout.yaxis2.range, [-1600, 0]);

        plotlyCalls.length = 0;
        rob.renderRobustnessResult(
          {
            ticker: "NVDA",
            strategy_kind: "BULL_PUT_SPREAD",
            total_bars: 30,
            data_sufficiency_tier: "LIMITED",
            verdicts: [],
            cohorts: {
              mode: "ALL_DAYS_SPREADS",
              closed_cohorts: 5,
              entered_cohorts: 5,
              open_cohorts: 0,
              avg_concurrent_positions: 1.0,
              win_rate_pct: 80,
              expectancy_dollars: 50,
              avg_return_on_risk_pct: 10,
              profit_factor: 2.0,
              total_net_pnl_dollars: 250,
              portfolio_total_return_pct: 2.5,
              portfolio_max_drawdown_pct: 4.0,
              portfolio_sharpe: 1.2,
              weekday_skew_detected: false,
              weekday_stability: [],
              rolling_cohorts: [],
              equity_curve: [
                { market_date: "2026-10-01", equity_dollars: 10000, drawdown_pct: 0 },
                { market_date: "2026-10-02", equity_dollars: 9600, drawdown_pct: 4.0 },
              ],
            },
            sensitivity: null,
            walk_forward: null,
            monte_carlo: {
              prob_profit_pct: 70,
              risk_of_ruin_pct: 10,
              ruin_drawdown_pct: 25,
              num_paths: 500,
              block_size: 5,
              terminal_return_p50_pct: 5,
              terminal_return_p05_pct: -5,
              terminal_return_p95_pct: 15,
              max_dd_p95_pct: 12,
              max_dd_p50_pct: 5,
              cvar_5_return_pct: -8,
              trade_count: 10,
              trade_expectancy_ci_low_dollars: 10,
              trade_expectancy_ci_high_dollars: 90,
              fan_series: {
                bar_offsets: [0, 1],
                p05: [9900, 9950],
                p25: [9950, 10000],
                p50: [10000, 10100],
                p75: [10050, 10200],
                p95: [10100, 10300],
              },
              max_dd_histogram: [
                { bin_start_pct: 0, bin_end_pct: 5, count: 400, frequency_pct: 80 },
                { bin_start_pct: 25, bin_end_pct: 30, count: 100, frequency_pct: 20 },
              ],
              slippage_stress: null,
            },
          },
          helpers.themeVars(),
        );
        const cohortsCall = plotlyCalls.find((c) => c.id === "bt-rob-cohorts-chart");
        assert.ok(cohortsCall);
        assert.equal(cohortsCall.layout.title, undefined);
        assert.deepEqual(cohortsCall.layout.yaxis2.range, [-12.8, 0]);
        const ddCall = plotlyCalls.find((c) => c.id === "bt-rob-mc-dd-chart");
        assert.ok(ddCall);
        assert.equal(ddCall.layout.title, undefined);
        assert.equal(ddCall.layout.showlegend, false);
        assert.equal(ddCall.layout.xaxis.tickangle, -30);

        // 2. Sensitivity axis disabling per strategy & X !== Y collision resolution
        function populateAxes(sel, defVal) {
          sel.options = [
            "profit_target_pct",
            "stop_loss_pct",
            "exit_dte",
            "dte_bucket",
            "target_delta",
            "min_entry_credit_dollars",
            "min_entry_ror_pct",
          ].map((val) => ({ value: val, textContent: val, disabled: false, dataset: {} }));
          sel.value = defVal;
        }
        populateAxes(btRobSensX, "profit_target_pct");
        populateAxes(btRobSensY, "stop_loss_pct");

        bt.bindBacktestEvents();
        bt.initBacktestPanel();

        // BULL_PUT_SPREAD disables target_delta
        const tdOptBps = btRobSensX.options.find((o) => o.value === "target_delta");
        assert.equal(tdOptBps.disabled, true);
        assert.ok(tdOptBps.textContent.includes("(N/A)"));

        // COVERED_CALL enables target_delta and disables min_entry_ror_pct
        btStrategySelect.value = "COVERED_CALL";
        btStrategySelect.dispatchEvent({ type: "change" });
        const tdOptCc = btRobSensX.options.find((o) => o.value === "target_delta");
        const rorOptCc = btRobSensX.options.find((o) => o.value === "min_entry_ror_pct");
        assert.equal(tdOptCc.disabled, false);
        assert.equal(rorOptCc.disabled, true);
        assert.equal(btRobSensX.value, "target_delta");
        assert.equal(btRobSensY.value, "profit_target_pct");

        // Setting X = Y shifts Y automatically so X !== Y
        btRobSensX.value = "profit_target_pct";
        btRobSensX.dispatchEvent({ type: "change" });
        assert.notEqual(btRobSensX.value, btRobSensY.value);

        // 3. Capital Adequacy warning + "Size to $..." button
        state.chainData = { underlying_price: 195.0 };
        elementsById["backtest-initial-capital"].value = "10000";
        elementsById["backtest-qty"].value = "1";
        bt.syncCapitalAdequacyHint();
        const capWarn = elementsById["backtest-capital-adequacy"];
        const matchBtn = elementsById["backtest-match-capital-btn"];
        assert.equal(capWarn.hidden, false);
        assert.equal(matchBtn.textContent, "Size to $19,500");

        matchBtn.dispatchEvent({ type: "click" });
        assert.equal(elementsById["backtest-initial-capital"].value, "19500");
        assert.equal(capWarn.hidden, true);

        // 4. Stale-section invalidation when base config changes & ticker changes
        btStrategySelect.value = "BULL_PUT_SPREAD";
        btStrategySelect.dispatchEvent({ type: "change" });
        elementsById["backtest-tp-pct"].value = "50";
        nextFetchImpl = async (url, init) => {
          const sUrl = String(url);
          if (sUrl === "/api/backtest/robustness") {
            const body = JSON.parse(String(init.body || "{}"));
            return {
              ok: true,
              status: 200,
              text: async () => JSON.stringify({
                history_snapshot_count: 20,
                daily_bar_count: 20,
                cache_hit: false,
                warnings: [],
                result: {
                  ticker: body.ticker,
                  strategy_kind: body.config.strategy_kind,
                  total_bars: 20,
                  data_sufficiency_tier: "LIMITED",
                  requested_sections: body.sections,
                  compute_ms: 10,
                  notes: [],
                  verdicts: body.sections.map((s) => ({
                    section: s, status: "PASS", headline: "OK", detail: "20 bars",
                  })),
                  cohorts: body.sections.includes("COHORTS") ? {
                    mode: "ALL_DAYS_SPREADS", closed_cohorts: 4, entered_cohorts: 4,
                    open_cohorts: 0, avg_concurrent_positions: 1, win_rate_pct: 75,
                    expectancy_dollars: 20, avg_return_on_risk_pct: 5, profit_factor: 2,
                    total_net_pnl_dollars: 80, portfolio_total_return_pct: 0.8,
                    portfolio_max_drawdown_pct: 0.5, portfolio_sharpe: 1.0,
                    weekday_skew_detected: false, weekday_stability: [],
                    rolling_cohorts: [], equity_curve: [],
                  } : null,
                  sensitivity: null,
                  walk_forward: null,
                  monte_carlo: null,
                },
              }),
            };
          }
          return {
            ok: true,
            status: 200,
            text: async () => JSON.stringify({ entries: [], trades: [] }),
          };
        };

        state.ticker = "NVDA";
        await rob.runRobustnessAudit(null);
        assert.ok(state.backtestRobustnessResult.cohorts !== null);

        // Change base TP % and re-run only SENSITIVITY -> stale COHORTS cleared
        elementsById["backtest-tp-pct"].value = "40";
        await rob.runRobustnessAudit(["SENSITIVITY"]);
        assert.equal(state.backtestRobustnessResult.cohorts, null);
        const cohortVerdict = state.backtestRobustnessResult.verdicts.find(
          (v) => v.section === "COHORTS",
        );
        assert.equal(cohortVerdict.status, "SKIPPED");

        // Switching ticker marks robustness stale & clears stale result
        state.ticker = "AVGO";
        await bt.backtestOnSnapshotsLoaded("AVGO");
        assert.equal(state.backtestRobustnessResult, null);
        assert.equal(elementsById["bt-rob-depth-badge"].textContent, "STALE");

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True
