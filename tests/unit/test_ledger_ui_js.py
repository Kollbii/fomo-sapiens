"""Node.js ES-module tests for the Trade Ledger Chart vs. Detail Table split-pane resizer."""

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
const plotlyResizes = [];

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
    this.data = null;
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
    return { left: 0, top: 0, right: 1000, bottom: 400, width: 1000, height: 400 };
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

const ledgerCard = makeEl("article", "trade-ledger-card");
ledgerCard.setAttribute("data-panel-id", "trade_ledger");
const ledgerSplit = makeEl("div", "ledger-split");
const ledgerChart = makeEl("div", "chart-ledger");
ledgerCard.appendChild(ledgerChart);
const ledgerSplitter = makeEl("div", "ledger-split-splitter");
makeEl("div", "ledger-kpi-strip");
makeEl("div", "ledger-table-container");
makeEl("thead", "ledger-table-head");
makeEl("tbody", "ledger-table-body");

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
    Plots: {
      resize(el) {
        plotlyResizes.push(el ? el.id : "");
      },
    },
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
  fetch: async () => ({
    ok: true,
    status: 200,
    text: async () => JSON.stringify({}),
  }),
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


def test_ledger_splitter_drag_persist_reset_keyboard_and_plotly_resize() -> None:
    result = _run_node_scenario(
        """
        const { state, LEDGER_SPLIT_KEY } = await import(modUrl("state.js"));
        const ledger = await import(modUrl("ledger.js"));

        // 1. Pure clamping & coordinate conversion helpers
        assert.equal(ledger.DEFAULT_LEDGER_SPLIT_PCT, 34);
        assert.equal(ledger.MIN_LEDGER_SPLIT_PCT, 22);
        assert.equal(ledger.MAX_LEDGER_SPLIT_PCT, 62);
        assert.equal(ledger.clampLedgerSplitPct("bad"), 34);
        assert.equal(ledger.clampLedgerSplitPct(5), 22);
        assert.equal(ledger.clampLedgerSplitPct(95), 62);
        assert.equal(ledger.clampLedgerSplitPct(44.26), 44.3);

        assert.equal(ledger.computeLedgerSplitPct(500, { left: 100, width: 1000 }), 40);
        assert.equal(ledger.computeLedgerSplitPct(120, { left: 100, width: 1000 }), 22);
        assert.equal(ledger.computeLedgerSplitPct(980, { left: 100, width: 1000 }), 62);
        assert.equal(ledger.computeLedgerSplitPct(500, { left: 0, width: 0 }), 34);

        // 2. Default init sets 34% / 66% split
        ledgerSplit._rectFn = () => ({
          left: 100, top: 0, right: 1100, bottom: 400, width: 1000, height: 400,
        });
        ledger.initLedgerSplitter();
        assert.equal(state.ledgerSplitPct, 34);
        assert.equal(ledgerSplit.style.getPropertyValue("--ledger-chart-col"), "34%");
        assert.equal(ledgerSplitter.getAttribute("aria-valuenow"), "34");

        // Enable plotted state to verify Plotly.Plots.resize triggers on resize
        ledgerChart.data = [{ x: ["2026-11-01"], y: [100] }];
        plotlyResizes.length = 0;

        // 3. Pointer drag updates CSS variable, aria-valuenow, triggers Plotly resize, and persists
        ledgerSplitter.dispatchEvent({
          type: "pointerdown", button: 0, pointerId: 2, clientX: 440,
        });
        assert.equal(ledgerSplitter.classList.contains("is-dragging"), true);
        assert.equal(ledgerSplit.classList.contains("is-dragging"), true);

        ledgerSplitter.dispatchEvent({ type: "pointermove", pointerId: 2, clientX: 550 });
        assert.equal(state.ledgerSplitPct, 45);
        assert.equal(ledgerSplit.style.getPropertyValue("--ledger-chart-col"), "45%");
        assert.equal(ledgerSplitter.getAttribute("aria-valuenow"), "45");
        assert.ok(plotlyResizes.includes("chart-ledger"));

        plotlyResizes.length = 0;
        ledgerSplitter.dispatchEvent({ type: "pointerup", pointerId: 2, clientX: 550 });
        assert.equal(ledgerSplitter.classList.contains("is-dragging"), false);
        assert.equal(ledgerSplit.classList.contains("is-dragging"), false);
        assert.deepEqual(JSON.parse(store[LEDGER_SPLIT_KEY]), { leftPct: 45 });
        assert.ok(plotlyResizes.includes("chart-ledger"));

        // 4. Re-init restores persisted split preference without duplicating event listeners
        state.ledgerSplitPct = 34;
        ledger.initLedgerSplitter();
        assert.equal(state.ledgerSplitPct, 45);
        assert.equal(ledgerSplit.style.getPropertyValue("--ledger-chart-col"), "45%");

        // 5. Double-click resets to 34% and clears preference
        ledgerSplitter.dispatchEvent({ type: "dblclick" });
        assert.equal(state.ledgerSplitPct, 34);
        assert.equal(ledgerSplit.style.getPropertyValue("--ledger-chart-col"), "34%");
        assert.equal(ledgerSplitter.getAttribute("aria-valuenow"), "34");
        assert.equal(store[LEDGER_SPLIT_KEY], undefined);

        // 6. Keyboard support: ArrowRight, ArrowLeft, Home, End, Enter, Space
        ledgerSplitter.dispatchEvent({ type: "keydown", key: "ArrowRight" });
        assert.equal(state.ledgerSplitPct, 36);
        assert.deepEqual(JSON.parse(store[LEDGER_SPLIT_KEY]), { leftPct: 36 });

        ledgerSplitter.dispatchEvent({ type: "keydown", key: "ArrowLeft" });
        assert.equal(state.ledgerSplitPct, 34);

        ledgerSplitter.dispatchEvent({ type: "keydown", key: "Home" });
        assert.equal(state.ledgerSplitPct, 22);

        ledgerSplitter.dispatchEvent({ type: "keydown", key: "End" });
        assert.equal(state.ledgerSplitPct, 62);

        ledgerSplitter.dispatchEvent({ type: "keydown", key: "Enter" });
        assert.equal(state.ledgerSplitPct, 34);
        assert.equal(store[LEDGER_SPLIT_KEY], undefined);

        ledgerSplitter.dispatchEvent({ type: "keydown", key: "ArrowRight" });
        assert.equal(state.ledgerSplitPct, 36);
        ledgerSplitter.dispatchEvent({ type: "keydown", key: " " });
        assert.equal(state.ledgerSplitPct, 34);
        assert.equal(store[LEDGER_SPLIT_KEY], undefined);

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True
