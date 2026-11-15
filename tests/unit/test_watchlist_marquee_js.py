"""Node.js ES-module tests for Watchlist marquee drag, modifiers, Shift+Click, and auto-scroll."""

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
let rafSeq = 0;
let rafQueue = new Map();
let selectionClearedCount = 0;
let lastBatchPollFn = null;

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
    this.style = {};
    this.dataset = {};
    this.children = [];
    this.listeners = { capture: {}, bubble: {} };
    this.value = "";
    this.textContent = "";
    this._innerHTML = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.title = "";
    this.type = "";
    this.parentNode = null;
    this.clientWidth = 0;
    this.clientHeight = 0;
    this.scrollTop = 0;
    this.scrollLeft = 0;
    this.scrollHeight = 0;
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
  get nextElementSibling() {
    if (!this.parentNode) return null;
    const idx = this.parentNode.children.indexOf(this);
    return idx >= 0 && idx + 1 < this.parentNode.children.length
      ? this.parentNode.children[idx + 1]
      : null;
  }
  get previousElementSibling() {
    if (!this.parentNode) return null;
    const idx = this.parentNode.children.indexOf(this);
    return idx > 0 ? this.parentNode.children[idx - 1] : null;
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
  remove() {
    if (this.parentNode) {
      this.parentNode.removeChild(this);
    }
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
    globalThis.document.activeElement = this;
  }
  getBoundingClientRect() {
    if (typeof this._rectFn === "function") return this._rectFn();
    return {
      left: 0,
      top: 0,
      right: this.clientWidth,
      bottom: this.clientHeight,
      width: this.clientWidth,
      height: this.clientHeight,
    };
  }
  addEventListener(type, fn, useCapture = false) {
    const bucket = useCapture ? this.listeners.capture : this.listeners.bubble;
    if (!bucket[type]) bucket[type] = [];
    bucket[type].push(fn);
  }
  removeEventListener(type, fn, useCapture = false) {
    const bucket = useCapture ? this.listeners.capture : this.listeners.bubble;
    const list = bucket[type];
    if (!list) return;
    bucket[type] = list.filter((f) => f !== fn);
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

    const chain = [];
    let cur = this;
    while (cur) {
      chain.push(cur);
      cur = cur.parentNode;
    }
    // Capture phase (outermost -> target)
    for (let i = chain.length - 1; i >= 0; i--) {
      if (stopped) break;
      const node = chain[i];
      const handlers = (node.listeners.capture[eventObj.type] || []).slice();
      for (const fn of handlers) {
        eventObj.currentTarget = node;
        fn.call(node, eventObj);
      }
    }
    // Bubble phase (target -> outermost)
    for (let i = 0; i < chain.length; i++) {
      if (stopped) break;
      const node = chain[i];
      const handlers = (node.listeners.bubble[eventObj.type] || []).slice();
      for (const fn of handlers) {
        eventObj.currentTarget = node;
        fn.call(node, eventObj);
      }
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
globalThis.Event = class FakeEvent {
  constructor(type) {
    this.type = type;
  }
};

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
  return el;
}

const wlPicker = makeEl("div", "watchlist-picker");
const wlToggle = makeEl("button", "watchlist-toggle");
const wlActive = makeEl("span", "watchlist-active");
const wlCount = makeEl("span", "watchlist-count");
const wlPanel = makeEl("div", "watchlist-panel");
wlPanel.hidden = true;
const wlNotice = makeEl("p", "watchlist-notice");
wlNotice.hidden = true;
const wlEmpty = makeEl("p", "watchlist-empty");
wlEmpty.hidden = true;
const wlRows = makeEl("ul", "watchlist-rows");
const wlFooter = new FakeNode("div");
wlFooter.className = "wl-picker__footer";
const wlSelectAll = makeEl("button", "watchlist-select-all");
const wlSelectNone = makeEl("button", "watchlist-select-none");
const wlSelectStale = makeEl("button", "watchlist-select-stale");
const wlLoadSelected = makeEl("button", "watchlist-load-selected");
const wlSelectedCount = makeEl("span", "watchlist-selected-count");
const wlLoadBar = makeEl("button", "watchlist-load-bar");
const wlLoadBarCount = makeEl("span", "watchlist-load-bar-count");
const wlAddForm = makeEl("form", "watchlist-add-form");
const wlAddInput = makeEl("input", "watchlist-add-input");
const wlAddSubmit = makeEl("button", "watchlist-add-submit");
const outsideEl = makeEl("div", "outside-area");
const refreshBtn = makeEl("button", "refresh-btn");

wlLoadSelected.appendChild(wlSelectedCount);
wlLoadBar.appendChild(wlLoadBarCount);
wlFooter.appendChild(wlSelectAll);
wlFooter.appendChild(wlSelectNone);
wlFooter.appendChild(wlSelectStale);
wlFooter.appendChild(wlLoadSelected);
wlPanel.appendChild(wlNotice);
wlPanel.appendChild(wlEmpty);
wlPanel.appendChild(wlRows);
wlPanel.appendChild(wlFooter);
wlPicker.appendChild(wlToggle);
wlPicker.appendChild(wlPanel);
bodyEl.appendChild(wlPicker);
bodyEl.appendChild(wlAddForm);
bodyEl.appendChild(wlLoadBar);
bodyEl.appendChild(outsideEl);
bodyEl.appendChild(refreshBtn);

globalThis.document = {
  documentElement: htmlEl,
  body: bodyEl,
  activeElement: bodyEl,
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
  addEventListener(type, fn, cap) {
    docNode.addEventListener(type, fn, cap);
  },
  removeEventListener(type, fn, cap) {
    docNode.removeEventListener(type, fn, cap);
  },
  dispatchEvent(ev) {
    return docNode.dispatchEvent(ev);
  },
};

globalThis.window = {
  location: { reload() {} },
  localStorage: {
    getItem(k) {
      return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null;
    },
    setItem(k, v) {
      store[k] = String(v);
    },
  },
  getComputedStyle() {
    return { getPropertyValue: () => "#111111" };
  },
  getSelection() {
    return {
      removeAllRanges() {
        selectionClearedCount += 1;
      },
    };
  },
  fetch(url, init) {
    return nextFetchImpl(url, init);
  },
  addEventListener(type, fn, cap) {
    docNode.addEventListener(`win:${type}`, fn, cap);
  },
  removeEventListener(type, fn, cap) {
    docNode.removeEventListener(`win:${type}`, fn, cap);
  },
  dispatchWindowEvent(ev) {
    return docNode.dispatchEvent({ ...ev, type: `win:${ev.type}` });
  },
  requestAnimationFrame(cb) {
    rafSeq += 1;
    rafQueue.set(rafSeq, cb);
    return rafSeq;
  },
  cancelAnimationFrame(id) {
    rafQueue.delete(id);
  },
  setTimeout(fn, ms) {
    if (ms === 1500) lastBatchPollFn = fn;
    return setTimeout(fn, ms);
  },
  clearTimeout,
};

function flushRaf(maxFrames = 10) {
  let ran = 0;
  while (rafQueue.size > 0 && ran < maxFrames) {
    const batch = Array.from(rafQueue.entries());
    rafQueue.clear();
    for (const [, cb] of batch) {
      cb(16);
    }
    ran += 1;
  }
  return ran;
}

function setupPanelGeometry({
  panelTop = 100,
  panelLeft = 20,
  panelWidth = 320,
  panelClientHeight = 400,
  rowHeight = 30,
  footerHeight = 40,
} = {}) {
  const rowsTopBase = panelTop + 4;
  function refreshLayout() {
    const rowEls = wlRows.children.filter((c) => c.getAttribute("data-wl-row") !== null);
    const rowsHeight = rowEls.length * rowHeight;
    wlRows.clientWidth = panelWidth;
    wlRows.clientHeight = rowsHeight;
    wlPanel.clientWidth = panelWidth;
    wlPanel.clientHeight = panelClientHeight;
    wlPanel.scrollHeight = Math.max(panelClientHeight, 4 + rowsHeight + footerHeight + 4);
    for (let idx = 0; idx < rowEls.length; idx++) {
      const li = rowEls[idx];
      const rowIndex = idx;
      li.clientWidth = panelWidth;
      li.clientHeight = rowHeight;
      li._rectFn = () => {
        const top = rowsTopBase + rowIndex * rowHeight - wlPanel.scrollTop;
        return {
          left: panelLeft,
          top,
          right: panelLeft + panelWidth,
          bottom: top + rowHeight,
          width: panelWidth,
          height: rowHeight,
        };
      };
    }
  }

  wlPanel._rectFn = () => ({
    left: panelLeft,
    top: panelTop,
    right: panelLeft + panelWidth,
    bottom: panelTop + panelClientHeight,
    width: panelWidth,
    height: panelClientHeight,
  });
  wlRows._rectFn = () => {
    const top = rowsTopBase - wlPanel.scrollTop;
    return {
      left: panelLeft,
      top,
      right: panelLeft + panelWidth,
      bottom: top + wlRows.clientHeight,
      width: panelWidth,
      height: wlRows.clientHeight,
    };
  };

  if (!wlRows._layoutHooked) {
    const origAppend = wlRows.appendChild.bind(wlRows);
    wlRows.appendChild = (child) => {
      const res = origAppend(child);
      if (child && child.getAttribute && child.getAttribute("data-wl-row") !== null) {
        refreshLayout();
      }
      return res;
    };
    wlRows._layoutHooked = true;
  }
  refreshLayout();
}

async function bootWatchlist(
  tickers = ["NVDA", "AVGO", "CRDO", "MRVL", "META"],
  initialUnchecked = {},
) {
  const { state, dom, WATCHLIST_SELECTION_KEY } = await import(modUrl("state.js"));
  const watchlistMod = await import(modUrl("watchlist.js"));
  state.watchlist = tickers.slice();
  state.ticker = tickers[0] || "NVDA";
  state.watchlistUnchecked = { ...initialUnchecked };
  store[WATCHLIST_SELECTION_KEY] = JSON.stringify({ unchecked: Object.keys(initialUnchecked) });
  nextFetchImpl = async (url, init) => {
    fetchCalls.push({ url: String(url), init: init || {} });
    if (String(url) === "/api/watchlist") {
      return {
        ok: true,
        status: 200,
        text: async () =>
          JSON.stringify({
            tickers: state.watchlist,
            entries: state.watchlist.map((t, i) => ({
              ticker: t,
              is_today_et: i % 2 === 0,
              latest_snapshot_utc: "2026-11-15T15:30:00Z",
            })),
            max_size: 20,
            allow_custom: true,
            writable: true,
            notice: "",
          }),
      };
    }
    if (String(url).startsWith("/api/watchlist/tickers/") && init && init.method === "DELETE") {
      const sym = decodeURIComponent(String(url).split("/").pop() || "");
      state.watchlist = state.watchlist.filter((s) => s !== sym);
      return {
        ok: true,
        status: 200,
        text: async () =>
          JSON.stringify({
            tickers: state.watchlist,
            entries: [],
            max_size: 20,
            allow_custom: true,
            writable: true,
            notice: "",
          }),
      };
    }
    return {
      ok: true,
      status: 200,
      text: async () => JSON.stringify({}),
    };
  };
  watchlistMod.wireWatchlist();
  await watchlistMod.initWatchlist();
  // Open panel
  wlToggle.dispatchEvent({ type: "click" });
  assert.equal(wlPanel.hidden, false);
  setupPanelGeometry();
  return { state, dom, WATCHLIST_SELECTION_KEY, watchlistMod };
}

function rowFor(sym) {
  return wlRows.querySelector(`[data-wl-row="${sym}"]`);
}
function checkFor(sym) {
  return wlRows.querySelector(`[data-wl-check="${sym}"]`);
}
function viewFor(sym) {
  return wlRows.querySelector(`[data-wl-view="${sym}"]`);
}
function removeFor(sym) {
  return wlRows.querySelector(`[data-wl-remove="${sym}"]`);
}
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


def test_pure_marquee_and_range_helpers() -> None:
    result = _run_node_scenario(
        """
        const wl = await import(modUrl("watchlist.js"));

        // clampPointToRect & computeMarqueeRect clamp strictly within [0..width, 0..height]
        assert.deepEqual(wl.clampPointToRect(-25, 500, 320, 150), { x: 0, y: 150 });
        const rect = wl.computeMarqueeRect(
          { x: 120, y: 90 },
          { x: -30, y: 240 },
          { width: 320, height: 150 },
        );
        assert.deepEqual(rect, {
          left: 0,
          top: 90,
          right: 120,
          bottom: 150,
          width: 120,
          height: 60,
        });

        // rectsIntersect handles overlap and touching edges cleanly
        assert.equal(
          wl.rectsIntersect(
            { left: 10, top: 10, right: 50, bottom: 50 },
            { left: 40, top: 40, right: 80, bottom: 80 },
          ),
          true,
        );
        assert.equal(
          wl.rectsIntersect(
            { left: 10, top: 10, right: 50, bottom: 50 },
            { left: 50, top: 10, right: 90, bottom: 50 },
          ),
          false,
        );

        // computeMarqueePreview: default toggle vs Ctrl/Cmd additive
        const list = ["NVDA", "AVGO", "CRDO", "MRVL"];
        const pre = { NVDA: true, AVGO: false, CRDO: true, MRVL: false };
        const hi = { AVGO: true, CRDO: true };
        assert.deepEqual(wl.computeMarqueePreview(list, pre, hi, false), {
          NVDA: true,
          AVGO: true,
          CRDO: false,
          MRVL: false,
        });
        assert.deepEqual(wl.computeMarqueePreview(list, pre, hi, true), {
          NVDA: true,
          AVGO: true,
          CRDO: true,
          MRVL: false,
        });

        // computeRangeSelection forward and backward
        const cur = { NVDA: true, AVGO: false, CRDO: true, MRVL: false };
        assert.deepEqual(wl.computeRangeSelection(list, "AVGO", "MRVL", true, cur), {
          NVDA: true,
          AVGO: true,
          CRDO: true,
          MRVL: true,
        });
        assert.deepEqual(wl.computeRangeSelection(list, "MRVL", "NVDA", false, cur), {
          NVDA: false,
          AVGO: false,
          CRDO: false,
          MRVL: false,
        });

        // computeAutoScrollStep: 24px top/bottom zones only when overflowing
        const pRect = { top: 100, bottom: 300 };
        assert.equal(wl.computeAutoScrollStep(290, pRect, 0, 200, 200), 0); // not overflowing
        assert.equal(wl.computeAutoScrollStep(200, pRect, 40, 500, 200), 0); // middle
        assert.equal(wl.computeAutoScrollStep(124, pRect, 40, 500, 200), 0); // exact 24px outside
        assert.ok(wl.computeAutoScrollStep(110, pRect, 40, 500, 200) < 0); // top zone scrolls up
        assert.equal(wl.computeAutoScrollStep(110, pRect, 0, 500, 200), 0); // already at top
        assert.ok(wl.computeAutoScrollStep(290, pRect, 40, 500, 200) > 0); // bottom scrolls down
        assert.equal(wl.computeAutoScrollStep(290, pRect, 300, 500, 200), 0); // already at bottom

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_stationary_clicks_and_remove_button_exclusion() -> None:
    result = _run_node_scenario(
        """
        const { state, WATCHLIST_SELECTION_KEY } = await bootWatchlist(
          ["NVDA", "AVGO", "CRDO", "MRVL"],
          { AVGO: true },
        );

        // 1. Stationary mousedown -> <5px move -> mouseup on row does NOT activate marquee
        const row0 = rowFor("NVDA");
        row0.dispatchEvent({ type: "mousedown", button: 0, clientX: 60, clientY: 118 });
        // hypot(3, 3) = 4.24 < 5
        document.dispatchEvent({ type: "mousemove", clientX: 63, clientY: 121 });
        assert.equal(wlPanel.classList.contains("is-marquee-active"), false);
        assert.equal(wlRows.querySelector(".wl-marquee"), null);
        document.dispatchEvent({ type: "mouseup", button: 0, clientX: 63, clientY: 121 });
        assert.equal(wlPanel.classList.contains("is-marquee-active"), false);

        // 2. Stationary checkbox toggle still toggles one ticker and persists
        const chkAvgo = checkFor("AVGO");
        chkAvgo.checked = true;
        chkAvgo.dispatchEvent({ type: "change" });
        assert.equal(Boolean(state.watchlistUnchecked.AVGO), false);
        assert.equal(wlSelectedCount.textContent, "4");
        assert.deepEqual(JSON.parse(store[WATCHLIST_SELECTION_KEY]).unchecked, []);

        // 3. Footer buttons (None / All / Stale only) remain intact
        wlSelectNone.dispatchEvent({ type: "click" });
        assert.equal(wlSelectedCount.textContent, "0");
        wlSelectStale.dispatchEvent({ type: "click" });
        assert.equal(wlSelectedCount.textContent, "2"); // AVGO (idx 1) and MRVL (idx 3)
        wlSelectAll.dispatchEvent({ type: "click" });
        assert.equal(wlSelectedCount.textContent, "4");

        // 4. Right-click (button 2) and x remove button do NOT trigger marquee drag
        row0.dispatchEvent({ type: "mousedown", button: 2, clientX: 60, clientY: 118 });
        document.dispatchEvent({ type: "mousemove", clientX: 60, clientY: 200 });
        assert.equal(wlPanel.classList.contains("is-marquee-active"), false);
        document.dispatchEvent({ type: "mouseup", button: 2, clientX: 60, clientY: 200 });

        const rmCrdo = removeFor("CRDO");
        rmCrdo.dispatchEvent({ type: "mousedown", button: 0, clientX: 300, clientY: 175 });
        document.dispatchEvent({ type: "mousemove", clientX: 300, clientY: 240 });
        assert.equal(wlPanel.classList.contains("is-marquee-active"), false);
        assert.equal(wlRows.querySelector(".wl-marquee"), null);
        document.dispatchEvent({ type: "mouseup", button: 0, clientX: 300, clientY: 240 });

        // Normal click on x still deletes ticker
        const deleteCallsBefore = fetchCalls.filter((c) => c.init.method === "DELETE").length;
        rmCrdo.dispatchEvent({ type: "click" });
        await new Promise((r) => setTimeout(r, 10));
        const deleteCallsAfter = fetchCalls.filter((c) => c.init.method === "DELETE").length;
        assert.equal(deleteCallsAfter, deleteCallsBefore + 1);
        assert.deepEqual(state.watchlist, ["NVDA", "AVGO", "MRVL"]);

        // 5. Stationary [data-wl-view] click closes panel and loads chain
        fetchCalls.length = 0;
        viewFor("AVGO").dispatchEvent({ type: "click" });
        await new Promise((r) => setTimeout(r, 10));
        assert.equal(wlPanel.hidden, true);
        assert.ok(fetchCalls.some((c) => c.url.includes("/api/chain/AVGO")));

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_default_toggle_drag_enter_leave_clamping_and_mid_drag_update_rows() -> None:
    result = _run_node_scenario(
        """
        const { state, WATCHLIST_SELECTION_KEY, watchlistMod } = await bootWatchlist(
          ["NVDA", "AVGO", "CRDO", "MRVL", "META"],
          { AVGO: true, META: true },
        );
        // Pre-drag: NVDA(T), AVGO(F), CRDO(T), MRVL(T), META(F) -> 3 selected
        assert.equal(wlSelectedCount.textContent, "3");

        // Rows: NVDA[104..134], AVGO[134..164], CRDO[164..194], MRVL[194..224], META[224..254]
        // Drag from AVGO (y=145) down through CRDO and MRVL (y=210)
        rowFor("AVGO").dispatchEvent({ type: "mousedown", button: 0, clientX: 50, clientY: 145 });
        document.dispatchEvent({ type: "mousemove", clientX: 140, clientY: 210 });

        assert.equal(wlPanel.classList.contains("is-marquee-active"), true);
        assert.equal(wlPanel.classList.contains("is-marquee-dragging"), true);
        assert.ok(selectionClearedCount >= 1);
        const overlay = wlRows.querySelector(".wl-marquee");
        assert.ok(overlay);

        // AVGO(F->T), CRDO(T->F), MRVL(T->F); NVDA(T) & META(F) untouched -> live count = 2
        assert.equal(rowFor("NVDA").classList.contains("is-marquee"), false);
        assert.equal(rowFor("AVGO").classList.contains("is-marquee"), true);
        assert.equal(rowFor("CRDO").classList.contains("is-marquee"), true);
        assert.equal(rowFor("MRVL").classList.contains("is-marquee"), true);
        assert.equal(rowFor("META").classList.contains("is-marquee"), false);
        assert.equal(checkFor("AVGO").checked, true);
        assert.equal(checkFor("CRDO").checked, false);
        assert.equal(checkFor("MRVL").checked, false);
        assert.equal(wlSelectedCount.textContent, "2");
        assert.equal(wlLoadBarCount.textContent, "2");
        // state.watchlistUnchecked must NOT be committed until mouseup!
        assert.equal(Boolean(state.watchlistUnchecked.AVGO), true);

        // Simulate live batch poll (applyBatch -> updateRows + syncControls) mid-drag
        state.watchlistBatch = {
          batch_id: "batch001",
          state: "running",
          total: 5,
          done_count: 1,
          failed_count: 0,
          cancelled_count: 0,
          current_ticker: "AVGO",
          items: [
            { ticker: "NVDA", state: "done" },
            { ticker: "AVGO", state: "running" },
            { ticker: "CRDO", state: "queued" },
            { ticker: "MRVL", state: "queued" },
            { ticker: "META", state: "queued" },
          ],
        };
        const origFetch = nextFetchImpl;
        nextFetchImpl = async (url, init) => {
          if (
            String(url) === "/api/refresh/batch/current" ||
            String(url).includes("/api/refresh/batch/batch001")
          ) {
            return {
              ok: true,
              status: 200,
              text: async () => JSON.stringify(state.watchlistBatch),
            };
          }
          return origFetch(url, init);
        };
        await watchlistMod.initWatchlist();
        assert.equal(typeof lastBatchPollFn, "function");
        window.clearTimeout(state.watchlistPollTimer);
        state.watchlistPollTimer = null;
        state.watchlistBatch.done_count = 2;
        state.watchlistBatch.current_ticker = "CRDO";
        state.watchlistBatch.items[1].state = "done";
        state.watchlistBatch.items[2].state = "running";
        await lastBatchPollFn();
        assert.ok(wlRows.querySelector(".wl-marquee"));
        assert.equal(rowFor("AVGO").classList.contains("is-marquee"), true);
        assert.equal(checkFor("AVGO").checked, true);
        assert.equal(checkFor("CRDO").checked, false);
        assert.equal(wlSelectedCount.textContent, "2");
        assert.equal(wlLoadBarCount.textContent, "2");
        window.clearTimeout(state.watchlistPollTimer);
        state.watchlistPollTimer = null;
        state.watchlistBatch.state = "done";
        await lastBatchPollFn();
        nextFetchImpl = origFetch;
        assert.ok(wlRows.querySelector(".wl-marquee"));
        assert.equal(rowFor("AVGO").classList.contains("is-marquee"), true);
        assert.equal(checkFor("AVGO").checked, true);
        assert.equal(checkFor("CRDO").checked, false);
        assert.equal(wlSelectedCount.textContent, "2");
        assert.equal(wlLoadBarCount.textContent, "2");

        // Also verify refreshWatchlist (renderRows) mid-drag preserves overlay and live preview!
        await watchlistMod.refreshWatchlist();
        assert.ok(wlRows.querySelector(".wl-marquee"));
        assert.equal(rowFor("AVGO").classList.contains("is-marquee"), true);
        assert.equal(checkFor("AVGO").checked, true);
        assert.equal(checkFor("CRDO").checked, false);
        assert.equal(wlSelectedCount.textContent, "2");

        // Move UP before release so MRVL exits -> loses .is-marquee & reverts to pre-drag (T)!
        document.dispatchEvent({ type: "mousemove", clientX: 140, clientY: 180 });
        assert.equal(rowFor("MRVL").classList.contains("is-marquee"), false);
        assert.equal(checkFor("MRVL").checked, true);
        assert.equal(rowFor("AVGO").classList.contains("is-marquee"), true);
        assert.equal(rowFor("CRDO").classList.contains("is-marquee"), true);
        assert.equal(wlSelectedCount.textContent, "3"); // NVDA(T), AVGO(T), CRDO(F), MRVL(T)

        // Move far below footer -> marquee clamped strictly inside #watchlist-rows [0..150]
        // Test clamping right before release by moving into footer area first
        document.dispatchEvent({ type: "mousemove", clientX: 500, clientY: 420 });
        const ov = wlRows.querySelector(".wl-marquee");
        const ovTop = parseFloat(ov.style.top);
        const ovHeight = parseFloat(ov.style.height);
        const ovLeft = parseFloat(ov.style.left);
        const ovWidth = parseFloat(ov.style.width);
        assert.ok(ovTop >= 0 && ovTop + ovHeight <= wlRows.clientHeight);
        assert.ok(ovLeft >= 0 && ovLeft + ovWidth <= wlRows.clientWidth);

        // Move back up to cover only AVGO & CRDO and release
        document.dispatchEvent({ type: "mouseup", button: 0, clientX: 140, clientY: 180 });
        assert.equal(wlPanel.classList.contains("is-marquee-active"), false);
        assert.equal(wlPanel.classList.contains("is-marquee-dragging"), false);
        assert.equal(wlRows.querySelector(".wl-marquee"), null);
        assert.equal(rowFor("AVGO").classList.contains("is-marquee"), false);

        // Final committed state: NVDA(T), AVGO(T), CRDO(F), MRVL(T), META(F)
        assert.equal(Boolean(state.watchlistUnchecked.NVDA), false);
        assert.equal(Boolean(state.watchlistUnchecked.AVGO), false);
        assert.equal(Boolean(state.watchlistUnchecked.CRDO), true);
        assert.equal(Boolean(state.watchlistUnchecked.MRVL), false);
        assert.equal(Boolean(state.watchlistUnchecked.META), true);
        assert.equal(wlSelectedCount.textContent, "3");
        assert.deepEqual(
          JSON.parse(store[WATCHLIST_SELECTION_KEY]).unchecked.sort(),
          ["CRDO", "META"],
        );

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_ctrl_and_cmd_additive_check_drag() -> None:
    result = _run_node_scenario(
        """
        const { state } = await bootWatchlist(
          ["NVDA", "AVGO", "CRDO", "MRVL", "META"],
          { AVGO: true, MRVL: true },
        );
        // Pre-drag: NVDA(T), AVGO(F), CRDO(T), MRVL(F), META(T)

        // Ctrl drag over AVGO, CRDO, MRVL -> all three become checked (true), CRDO stays true!
        rowFor("AVGO").dispatchEvent({
          type: "mousedown",
          button: 0,
          clientX: 50,
          clientY: 145,
          ctrlKey: true,
        });
        document.dispatchEvent({
          type: "mousemove",
          clientX: 160,
          clientY: 210,
          ctrlKey: true,
        });
        assert.equal(checkFor("AVGO").checked, true);
        assert.equal(checkFor("CRDO").checked, true);
        assert.equal(checkFor("MRVL").checked, true);
        assert.equal(wlSelectedCount.textContent, "5");
        document.dispatchEvent({
          type: "mouseup",
          button: 0,
          clientX: 160,
          clientY: 210,
          ctrlKey: true,
        });

        assert.equal(Boolean(state.watchlistUnchecked.AVGO), false);
        assert.equal(Boolean(state.watchlistUnchecked.CRDO), false);
        assert.equal(Boolean(state.watchlistUnchecked.MRVL), false);

        // Reset AVGO & MRVL unchecked, test Cmd (metaKey) pressed on release after plain drag
        state.watchlistUnchecked = { AVGO: true, MRVL: true };
        rowFor("AVGO").dispatchEvent({ type: "mousedown", button: 0, clientX: 50, clientY: 145 });
        document.dispatchEvent({ type: "mousemove", clientX: 160, clientY: 210 });
        // Before Cmd: CRDO was inverted to false
        assert.equal(checkFor("CRDO").checked, false);
        // Hold Cmd at release -> additive checks AVGO, CRDO, MRVL all true!
        document.dispatchEvent({
          type: "mouseup",
          button: 0,
          clientX: 160,
          clientY: 210,
          metaKey: true,
        });
        assert.equal(Boolean(state.watchlistUnchecked.AVGO), false);
        assert.equal(Boolean(state.watchlistUnchecked.CRDO), false);
        assert.equal(Boolean(state.watchlistUnchecked.MRVL), false);

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_post_drag_click_and_outside_release_suppression() -> None:
    result = _run_node_scenario(
        """
        const { state } = await bootWatchlist(["NVDA", "AVGO", "CRDO", "MRVL"], {});

        // 1. Release drag outside #watchlist-panel -> synthetic outside click MUST NOT close panel
        rowFor("NVDA").dispatchEvent({ type: "mousedown", button: 0, clientX: 50, clientY: 115 });
        document.dispatchEvent({ type: "mousemove", clientX: 420, clientY: 180 });
        document.dispatchEvent({ type: "mouseup", button: 0, clientX: 420, clientY: 180 });
        assert.equal(wlPanel.hidden, false);

        // Browser synthesizes click on outside element
        outsideEl.dispatchEvent({ type: "click" });
        assert.equal(wlPanel.hidden, false);

        // Subsequent normal click outside DOES close panel
        await new Promise((r) => setTimeout(r, 10));
        outsideEl.dispatchEvent({ type: "click" });
        assert.equal(wlPanel.hidden, true);

        // Reopen panel for [data-wl-view] & [data-wl-check] post-drag suppression check
        wlToggle.dispatchEvent({ type: "click" });
        assert.equal(wlPanel.hidden, false);
        setupPanelGeometry();
        fetchCalls.length = 0;

        // 2. Drag starting on [data-wl-view] across CRDO -> post-drag click MUST NOT loadChain
        const viewNvda = viewFor("NVDA");
        viewNvda.dispatchEvent({ type: "mousedown", button: 0, clientX: 70, clientY: 115 });
        document.dispatchEvent({ type: "mousemove", clientX: 70, clientY: 175 });
        document.dispatchEvent({ type: "mouseup", button: 0, clientX: 70, clientY: 175 });
        viewNvda.dispatchEvent({ type: "click" });
        assert.equal(wlPanel.hidden, false);
        assert.equal(fetchCalls.filter((c) => c.url.includes("/api/chain/")).length, 0);

        // 3. Drag starting on [data-wl-check] and releasing back MUST NOT double-toggle
        await new Promise((r) => setTimeout(r, 10));
        const chkAvgo = checkFor("AVGO");
        const avgoBefore = Boolean(state.watchlistUnchecked.AVGO); // false (checked)
        chkAvgo.dispatchEvent({ type: "mousedown", button: 0, clientX: 30, clientY: 145 });
        document.dispatchEvent({ type: "mousemove", clientX: 30, clientY: 200 });
        document.dispatchEvent({ type: "mouseup", button: 0, clientX: 30, clientY: 200 });
        const avgoAfterMarquee = Boolean(state.watchlistUnchecked.AVGO);
        assert.notEqual(avgoAfterMarquee, avgoBefore); // marquee toggled it once
        // Simulate browser click + change landing back on checkbox
        chkAvgo.checked = !chkAvgo.checked;
        chkAvgo.dispatchEvent({ type: "click" });
        chkAvgo.dispatchEvent({ type: "change" });
        assert.equal(Boolean(state.watchlistUnchecked.AVGO), avgoAfterMarquee);
        assert.equal(chkAvgo.checked, !avgoAfterMarquee);

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_escape_cancel_and_close_cleanup_mid_drag() -> None:
    result = _run_node_scenario(
        """
        const { state, WATCHLIST_SELECTION_KEY } = await bootWatchlist(
          ["NVDA", "AVGO", "CRDO", "MRVL"],
          { AVGO: true },
        );
        // Pre-drag: NVDA(T), AVGO(F), CRDO(T), MRVL(T) -> 3 selected
        assert.equal(wlSelectedCount.textContent, "3");

        rowFor("NVDA").dispatchEvent({ type: "mousedown", button: 0, clientX: 60, clientY: 115 });
        document.dispatchEvent({ type: "mousemove", clientX: 120, clientY: 180 });
        // Live preview changed NVDA(F), AVGO(T), CRDO(F) -> count = 2
        assert.equal(wlSelectedCount.textContent, "2");
        assert.equal(wlPanel.classList.contains("is-marquee-active"), true);

        // Press Escape on document (focus outside picker): cancels drag ONLY, panel stays open!
        document.dispatchEvent({ type: "keydown", key: "Escape" });
        assert.equal(wlPanel.hidden, false);
        assert.equal(wlPanel.classList.contains("is-marquee-active"), false);
        assert.equal(wlPanel.classList.contains("is-marquee-dragging"), false);
        assert.equal(wlRows.querySelector(".wl-marquee"), null);
        assert.equal(rowFor("NVDA").classList.contains("is-marquee"), false);
        assert.equal(checkFor("NVDA").checked, true);
        assert.equal(checkFor("AVGO").checked, false);
        assert.equal(checkFor("CRDO").checked, true);
        assert.equal(wlSelectedCount.textContent, "3");
        assert.deepEqual(JSON.parse(store[WATCHLIST_SELECTION_KEY]).unchecked, ["AVGO"]);

        // Releasing mouse button outside panel after Escape also leaves panel open
        document.dispatchEvent({ type: "mouseup", button: 0, clientX: 500, clientY: 300 });
        outsideEl.dispatchEvent({ type: "click" });
        assert.equal(wlPanel.hidden, false);

        // Repeat with Escape targeted at wlPicker itself
        rowFor("NVDA").dispatchEvent({ type: "mousedown", button: 0, clientX: 60, clientY: 115 });
        document.dispatchEvent({ type: "mousemove", clientX: 120, clientY: 180 });
        assert.equal(wlPanel.classList.contains("is-marquee-active"), true);
        wlPicker.dispatchEvent({ type: "keydown", key: "Escape" });
        assert.equal(wlPanel.hidden, false);
        assert.equal(wlPanel.classList.contains("is-marquee-active"), false);
        document.dispatchEvent({ type: "mouseup", button: 0, clientX: 120, clientY: 180 });

        // Next Escape (with no active drag) closes panel normally
        wlPicker.dispatchEvent({ type: "keydown", key: "Escape" });
        assert.equal(wlPanel.hidden, true);

        // Drag when panel is hidden is ignored
        rowFor("NVDA").dispatchEvent({ type: "mousedown", button: 0, clientX: 60, clientY: 115 });
        document.dispatchEvent({ type: "mousemove", clientX: 120, clientY: 180 });
        assert.equal(wlPanel.classList.contains("is-marquee-active"), false);
        document.dispatchEvent({ type: "mouseup", button: 0, clientX: 120, clientY: 180 });

        // Reopen, start drag, and close via toggle -> cleanly cancels & restores
        wlToggle.dispatchEvent({ type: "click" });
        assert.equal(wlPanel.hidden, false);
        setupPanelGeometry();
        rowFor("NVDA").dispatchEvent({ type: "mousedown", button: 0, clientX: 60, clientY: 115 });
        document.dispatchEvent({ type: "mousemove", clientX: 120, clientY: 180 });
        assert.equal(wlSelectedCount.textContent, "2");
        wlToggle.dispatchEvent({ type: "click" });
        assert.equal(wlPanel.hidden, true);
        assert.equal(wlPanel.classList.contains("is-marquee-active"), false);
        assert.equal(wlRows.querySelector(".wl-marquee"), null);
        assert.equal(wlSelectedCount.textContent, "3");

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_shift_click_forward_and_backward_range_selection() -> None:
    result = _run_node_scenario(
        """
        const { state, WATCHLIST_SELECTION_KEY } = await bootWatchlist(
          ["NVDA", "AVGO", "CRDO", "MRVL", "META"],
          { AVGO: true, CRDO: true, MRVL: true, META: true },
        );
        // Initially only NVDA is checked (count = 1)
        assert.equal(wlSelectedCount.textContent, "1");

        // 1. Normally toggle AVGO -> checked (true), recording AVGO as anchor
        const chkAvgo = checkFor("AVGO");
        chkAvgo.checked = true;
        chkAvgo.dispatchEvent({ type: "change" });
        assert.equal(wlSelectedCount.textContent, "2");

        // 2. Shift+Click MRVL (forward: AVGO..MRVL -> indices 1..3) => all become checked (true)
        const chkMrvl = checkFor("MRVL");
        chkMrvl.checked = true; // browser flips before click
        chkMrvl.dispatchEvent({ type: "click", shiftKey: true });
        chkMrvl.dispatchEvent({ type: "change" }); // browser fires change without shiftKey
        assert.equal(Boolean(state.watchlistUnchecked.NVDA), false);
        assert.equal(Boolean(state.watchlistUnchecked.AVGO), false);
        assert.equal(Boolean(state.watchlistUnchecked.CRDO), false);
        assert.equal(Boolean(state.watchlistUnchecked.MRVL), false);
        assert.equal(Boolean(state.watchlistUnchecked.META), true);
        assert.equal(wlSelectedCount.textContent, "4");
        assert.deepEqual(JSON.parse(store[WATCHLIST_SELECTION_KEY]).unchecked, ["META"]);

        // 3. Normally toggle META -> checked (true), recording META as new anchor
        const chkMeta = checkFor("META");
        chkMeta.checked = true;
        chkMeta.dispatchEvent({ type: "change" });
        assert.equal(wlSelectedCount.textContent, "5");

        // 4. Shift+Click CRDO (backward: META..CRDO -> indices 2..4) => unchecks CRDO, MRVL, META
        const chkCrdo = checkFor("CRDO");
        chkCrdo.checked = false;
        chkCrdo.dispatchEvent({ type: "click", shiftKey: true });
        chkCrdo.dispatchEvent({ type: "change" });
        assert.equal(Boolean(state.watchlistUnchecked.NVDA), false);
        assert.equal(Boolean(state.watchlistUnchecked.AVGO), false);
        assert.equal(Boolean(state.watchlistUnchecked.CRDO), true);
        assert.equal(Boolean(state.watchlistUnchecked.MRVL), true);
        assert.equal(Boolean(state.watchlistUnchecked.META), true);
        assert.equal(wlSelectedCount.textContent, "2");
        assert.deepEqual(
          JSON.parse(store[WATCHLIST_SELECTION_KEY]).unchecked.sort(),
          ["CRDO", "META", "MRVL"],
        );

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_scrolled_container_and_edge_auto_scroll() -> None:
    result = _run_node_scenario(
        """
        const tickers = [
          "T0", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10", "T11",
        ];
        const { state } = await bootWatchlist(tickers, {});
        // Viewport height = 140px, 12 rows * 30px = 360px + 48px padding/footer = 408px
        setupPanelGeometry({
          panelTop: 100,
          panelClientHeight: 140,
          rowHeight: 30,
          footerHeight: 40,
        });
        assert.ok(wlPanel.scrollHeight > wlPanel.clientHeight);

        // Start drag at T1 (y_local = 45 -> clientY = 149)
        rowFor("T1").dispatchEvent({ type: "mousedown", button: 0, clientX: 60, clientY: 149 });
        // Move into bottom 24px edge zone (panelBottom = 240 -> 234 is within 24px)
        document.dispatchEvent({ type: "mousemove", clientX: 120, clientY: 234 });
        assert.equal(wlPanel.classList.contains("is-marquee-active"), true);
        assert.ok(rafQueue.size > 0);

        // Step RAF frames so panel scrolls down smoothly while anchor stays at T1 (y_local=45)
        flushRaf(15);
        assert.ok(wlPanel.scrollTop > 100);

        // Release mouse at bottom edge -> marquee spans T1 through scrolled-into-view bottom rows
        document.dispatchEvent({ type: "mouseup", button: 0, clientX: 120, clientY: 234 });
        assert.equal(rafQueue.size, 0);

        // T0 was above T1 -> stayed checked; T1..T8 were traversed -> toggled to unchecked!
        assert.equal(Boolean(state.watchlistUnchecked.T0), false);
        assert.equal(Boolean(state.watchlistUnchecked.T1), true);
        assert.equal(Boolean(state.watchlistUnchecked.T5), true);
        assert.equal(Boolean(state.watchlistUnchecked.T8), true);

        // Verify top edge auto-scroll when panel is scrolled down
        wlPanel.scrollTop = 180;
        // Visible rows now start around T6 (rowsTop=104-180=-76, T6 is -76+180=104..134)
        rowFor("T7").dispatchEvent({ type: "mousedown", button: 0, clientX: 60, clientY: 150 });
        // Move into top 24px edge zone (panelTop = 100 -> clientY = 106)
        document.dispatchEvent({ type: "mousemove", clientX: 120, clientY: 106 });
        assert.ok(rafQueue.size > 0);
        flushRaf(15);
        assert.ok(wlPanel.scrollTop < 180);

        document.dispatchEvent({ type: "mouseup", button: 0, clientX: 120, clientY: 106 });
        assert.equal(rafQueue.size, 0);
        // Drag 2 inverted T7 back to checked (false) and scrolled up into T4 (false),
        // while T8 (below T7) stayed unchecked (true) and T0 (above T1) stayed checked (false).
        assert.equal(Boolean(state.watchlistUnchecked.T7), false);
        assert.equal(Boolean(state.watchlistUnchecked.T4), false);
        assert.equal(Boolean(state.watchlistUnchecked.T8), true);
        assert.equal(Boolean(state.watchlistUnchecked.T0), false);

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True
