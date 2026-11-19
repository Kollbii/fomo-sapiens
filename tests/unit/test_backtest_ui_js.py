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
    if (!eventObj.target) eventObj.target = this;
    eventObj.defaultPrevented = Boolean(eventObj.defaultPrevented);
    let stopped = false;
    eventObj.preventDefault = () => {
      eventObj.defaultPrevented = true;
    };
    eventObj.stopPropagation = () => {
      stopped = true;
    };
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
makeEl("input", "backtest-min-credit");
makeEl("input", "backtest-min-ror");
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
