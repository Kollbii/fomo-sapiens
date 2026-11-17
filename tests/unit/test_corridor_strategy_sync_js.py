"""Node.js ES-module tests for Wall Strike Corridor <-> Strategy Lab synchronization."""

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
const plotlyCalls = [];
const fetchCalls = [];
const eventSources = [];
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

class FakeElement {
  constructor(tagName = "div", id = "") {
    this.tagName = String(tagName).toUpperCase();
    this.id = id;
    this._className = "";
    this.classList = new FakeClassList(this);
    this.attributes = {};
    this.style = {};
    this.dataset = {};
    this.children = [];
    this.options = [];
    this.listeners = {};
    this.value = "";
    this.textContent = "";
    this._innerHTML = "";
    this.checked = false;
    this.disabled = false;
    this.selected = false;
    this.title = "";
    this.type = "";
    this.min = "";
    this.max = "";
    this.parentNode = null;
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
      this.children = [];
      this.options = [];
    }
  }
  setAttribute(name, val) {
    this.attributes[name] = String(val);
    if (name === "data-strategy-preset") this.dataset.strategyPreset = String(val);
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name)
      ? this.attributes[name]
      : null;
  }
  appendChild(child) {
    if (!child) return child;
    child.parentNode = this;
    this.children.push(child);
    if (this.tagName === "SELECT" && child.tagName === "OPTION") {
      this.options.push(child);
      if (child.selected || this.options.length === 1) {
        this.value = child.value;
      }
    }
    return child;
  }
  addEventListener(type, fn) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push(fn);
  }
  dispatchEvent(ev) {
    const eventObj = ev || { type: "" };
    if (!eventObj.target) eventObj.target = this;
    let cur = this;
    while (cur) {
      const handlers = cur.listeners[eventObj.type] || [];
      for (const fn of handlers) {
        fn.call(cur, eventObj);
      }
      cur = cur.parentNode;
    }
    return true;
  }
  closest(selector) {
    if (
      selector === "[data-strategy-preset]" &&
      this.getAttribute("data-strategy-preset") !== null
    ) {
      return this;
    }
    return null;
  }
  querySelector(selector) {
    if (selector === "option[value='']" || selector === 'option[value=""]') {
      return this.options.find((o) => o.value === "") || null;
    }
    return null;
  }
  querySelectorAll() {
    return [];
  }
}

const elementsById = {};
const trackedIds = [
  "walls-dte-select",
  "strategy-expiry-select",
  "corridor-expiry-select",
  "corridor-window-select",
  "corridor-refresh-btn",
  "corridor-stream-toggle",
  "corridor-auto-reprice",
  "corridor-status-badge",
  "corridor-subs-badge",
  "corridor-kpi-pw",
  "corridor-kpi-pw-sub",
  "corridor-kpi-cw",
  "corridor-kpi-cw-sub",
  "corridor-kpi-spot",
  "corridor-kpi-spot-sub",
  "corridor-kpi-sync",
  "corridor-kpi-sync-sub",
  "corridor-tbody",
  "chart-corridor-ladder",
  "strategy-basis-badge",
  "strategy-auto-populate",
  "strategy-preset-pills",
  "strategy-qty-input",
  "strategy-autobuild-btn",
  "strategy-evaluate-btn",
  "strategy-add-leg-btn",
  "strategy-reset-btn",
  "strategy-kpi-price",
  "strategy-kpi-price-sub",
  "strategy-kpi-slip",
  "strategy-kpi-slip-sub",
  "strategy-kpi-risk",
  "strategy-kpi-risk-sub",
  "strategy-kpi-pop",
  "strategy-kpi-pop-sub",
  "strategy-kpi-greeks",
  "strategy-kpi-greeks-sub",
  "strategy-rationale",
  "strategy-legs-tbody",
  "strategy-days-slider",
  "strategy-days-readout",
  "strategy-iv-slider",
  "strategy-iv-readout",
  "chart-strategy-payoff",
  "alert-banner",
  "chain-table-body",
  "table-row-count",
];

for (const id of trackedIds) {
  const tag = id.endsWith("-select")
    ? "select"
    : id.endsWith("-tbody") || id.endsWith("-body")
      ? "tbody"
      : id.includes("input") ||
          id.includes("slider") ||
          id.includes("toggle") ||
          id.includes("reprice") ||
          id.includes("populate")
        ? "input"
        : id.endsWith("-btn")
          ? "button"
          : "div";
  elementsById[id] = new FakeElement(tag, id);
}
elementsById["corridor-window-select"].value = "10";
elementsById["strategy-qty-input"].value = "1";
elementsById["strategy-auto-populate"].checked = false;
elementsById["corridor-auto-reprice"].checked = true;

class FakeEventSource {
  constructor(url) {
    this.url = String(url);
    this.closed = false;
    this.handlers = {};
    this.onerror = null;
    eventSources.push(this);
  }
  addEventListener(type, fn) {
    if (!this.handlers[type]) this.handlers[type] = [];
    this.handlers[type].push(fn);
  }
  emit(type, payload) {
    const ev = typeof payload === "string" ? { data: payload } : payload || {};
    for (const fn of this.handlers[type] || []) {
      fn.call(this, ev);
    }
  }
  close() {
    this.closed = true;
  }
}

globalThis.Event = class FakeEvent {
  constructor(type) {
    this.type = type;
  }
};

globalThis.Plotly = {
  react(el, traces, layout, cfg) {
    plotlyCalls.push({ el, traces, layout, cfg });
  },
  purge() {},
};

const docEl = new FakeElement("html");
docEl.getAttribute = () => "print";

globalThis.document = {
  documentElement: docEl,
  getElementById(id) {
    return elementsById[id] || null;
  },
  querySelector() {
    return null;
  },
  querySelectorAll() {
    return [];
  },
  createElement(tag) {
    return new FakeElement(tag);
  },
  dispatchEvent() {},
  addEventListener() {},
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
    return {
      getPropertyValue(name) {
        const vals = {
          "--plotly-bg": "#FFFFFF",
          "--plotly-grid": "#E8E4DA",
          "--plotly-ink": "#111111",
          "--accent-call": "#0F6E4F",
          "--accent-put": "#A82020",
          "--corridor-leg-buy": "#B45309",
          "--corridor-leg-sell": "#EA580C",
          "--accent-warn": "#B45309",
          "--text-secondary": "#555555",
        };
        return vals[name] || "#111111";
      },
    };
  },
  fetch(url, init) {
    return nextFetchImpl(url, init);
  },
  Plotly: globalThis.Plotly,
  EventSource: FakeEventSource,
  addEventListener() {},
  setTimeout,
  clearTimeout,
};

function makeSampleChainData() {
  const contracts = [];
  const exps = [
    { expiry: "2026-12-18", dte: 33, pw: 95, cw: 105 },
    { expiry: "2027-01-15", dte: 61, pw: 90, cw: 110 },
    { expiry: "2027-02-19", dte: 96, pw: 85, cw: 115 },
  ];
  const strikes = [85, 90, 95, 100, 105, 110, 115];
  let cid = 1000;
  for (const e of exps) {
    for (const s of strikes) {
      for (const r of ["C", "P"]) {
        cid += 1;
        const dist = Math.abs(s - 100);
        const mid = Math.max(0.5, Number((6.0 - dist * 0.25 + (e.dte / 30) * 0.6).toFixed(2)));
        const vol =
          (r === "P" && s === e.pw) || (r === "C" && s === e.cw)
            ? 12000
            : 250;
        contracts.push({
          option_conid: cid,
          expiry: e.expiry,
          dte: e.dte,
          strike: s,
          right: r,
          bid: Number((mid - 0.1).toFixed(2)),
          ask: Number((mid + 0.1).toFixed(2)),
          mid,
          last: mid,
          close: mid,
          volume: vol,
          open_interest: vol * 2,
          iv: 0.35,
          delta: r === "C" ? 0.45 : -0.45,
          gamma: 0.04,
          theta: -0.05,
          vega: 0.12,
          greeks_source: "IBKR",
        });
      }
    }
  }
  return {
    meta: {
      underlying: "NVDA",
      underlying_price: 100,
      snapshot_id: "snap-001",
      snapshot_ts_utc: "2026-11-15T14:00:00Z",
    },
    walls: {
      primary_basis: "VOLUME",
      aggregate_primary_put_wall: { strike: 95, metric_value: 12000 },
      aggregate_primary_call_wall: { strike: 105, metric_value: 12000 },
      by_dte: [
        {
          expiry: "2026-12-18",
          dte: 33,
          primary_put_wall: { strike: 95, metric_value: 12000 },
          primary_call_wall: { strike: 105, metric_value: 12000 },
        },
        {
          expiry: "2027-01-15",
          dte: 61,
          primary_put_wall: { strike: 90, metric_value: 12000 },
          primary_call_wall: { strike: 110, metric_value: 12000 },
        },
        {
          expiry: "2027-02-19",
          dte: 96,
          primary_put_wall: { strike: 85, metric_value: 12000 },
          primary_call_wall: { strike: 115, metric_value: 12000 },
        },
      ],
    },
    contracts,
  };
}
"""


def _run_node_scenario(script_body: str) -> dict[str, Any]:
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.fail("Node.js binary not found on PATH for JS unit tests")
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


def test_corridor_expiry_resolution_and_auto_labels() -> None:
    result = _run_node_scenario(
        """
        const { state, dom } = await import(modUrl("state.js"));
        const helpers = await import(modUrl("helpers.js"));

        const data = makeSampleChainData();
        state.chainData = data;
        state.strategyLegs = [];

        // Populate dropdown option lists cleanly
        const dropdowns = [dom.wallsDteSelect, dom.strategyExpirySelect, dom.corridorExpirySelect];
        for (const sel of dropdowns) {
          sel.innerHTML = "";
          const autoOpt = document.createElement("option");
          autoOpt.value = "";
          autoOpt.textContent = "Auto";
          sel.appendChild(autoOpt);
          for (const e of helpers.sortedChainExpiries(data)) {
            const o = document.createElement("option");
            o.value = e.expiry;
            o.textContent = `${e.expiry} (${e.dte}d)`;
            sel.appendChild(o);
          }
        }
        dom.wallsDteSelect.value = "";
        dom.strategyExpirySelect.value = "";
        dom.corridorExpirySelect.value = "";

        // 1. Idle: follows governing Walls expiry (2026-12-18, 33d)
        const idleInfo = helpers.resolveCorridorExpiryInfo(data);
        assert.equal(idleInfo.expiry, "2026-12-18");
        assert.equal(idleInfo.source, "walls");
        assert.equal(
          helpers.formatCorridorAutoFollowLabel(data),
          "Auto (Follow Walls \\u2022 33d)",
        );

        // 2. Explicit Strategy Expiry (2027-01-15, 61d) overrides idle Walls when Corridor is Auto
        dom.strategyExpirySelect.value = "2027-01-15";
        const expOverride = helpers.resolveCorridorExpiryInfo(data);
        assert.equal(expOverride.expiry, "2027-01-15");
        assert.equal(expOverride.source, "strategy");
        assert.equal(
          helpers.formatCorridorAutoFollowLabel(data),
          "Auto (Follow Strategy \\u2022 61d)",
        );
        helpers.syncAutoFollowOptionLabels(data);
        assert.equal(
          dom.strategyExpirySelect.options[0].textContent,
          "Auto (Follow Walls \\u2022 33d)",
        );
        assert.equal(
          dom.corridorExpirySelect.options[0].textContent,
          "Auto (Follow Strategy \\u2022 61d)",
        );

        // 3. Auto Strategy Expiry + off-wall leg majority picks 2027-01-15 (2 legs vs 1 leg)
        dom.strategyExpirySelect.value = "";
        state.strategyLegs = [
          { side: "SELL", right: "P", expiry: "2027-01-15", strike: 95, qty: 1 },
          { side: "BUY", right: "P", expiry: "2027-01-15", strike: 90, qty: 1 },
          { side: "BUY", right: "C", expiry: "2027-02-19", strike: 110, qty: 1 },
        ];
        const majInfo = helpers.resolveCorridorExpiryInfo(data);
        assert.equal(majInfo.expiry, "2027-01-15");
        assert.equal(majInfo.source, "strategy");

        // 4. Tie between two non-wall expiries resolves to shortest DTE (2027-01-15, 61d < 96d)
        state.strategyLegs = [
          { side: "BUY", right: "C", expiry: "2027-02-19", strike: 110, qty: 1 },
          { side: "SELL", right: "P", expiry: "2027-01-15", strike: 95, qty: 1 },
        ];
        assert.equal(helpers.resolveCorridorTargetExpiry(data), "2027-01-15");

        // 5. Mixed legs containing Walls expiry (2026-12-18) prefer Walls expiry
        state.strategyLegs = [
          { side: "BUY", right: "C", expiry: "2027-02-19", strike: 110, qty: 1 },
          { side: "SELL", right: "P", expiry: "2026-12-18", strike: 95, qty: 1 },
        ];
        const wallMatchInfo = helpers.resolveCorridorExpiryInfo(data);
        assert.equal(wallMatchInfo.expiry, "2026-12-18");
        assert.equal(wallMatchInfo.source, "walls");

        // 6. Explicit Corridor override preserves manual selection even when Strategy differs
        dom.corridorExpirySelect.value = "2027-02-19";
        const manualInfo = helpers.resolveCorridorExpiryInfo(data);
        assert.equal(manualInfo.expiry, "2027-02-19");
        assert.equal(manualInfo.source, "manual");

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_subscription_signature_and_one_shot_reconciliation() -> None:
    result = _run_node_scenario(
        """
        const { state, dom } = await import(modUrl("state.js"));
        const corridor = await import(modUrl("corridor.js"));

        const data = makeSampleChainData();
        state.ticker = "NVDA";
        state.snapshotId = "snap-001";
        state.chainData = data;
        state.strategyLegs = [
          { side: "SELL", right: "P", expiry: "2026-12-18", strike: 95, qty: 1 },
          { side: "BUY", right: "P", expiry: "2026-12-18", strike: 90, qty: 1 },
        ];
        corridor.syncCorridorFromChain(data);

        // Signature stability across side flip / qty change / leg reordering
        const sigBase = corridor.corridorLegSubscriptionSignature(data);
        state.strategyLegs = [
          { side: "BUY", right: "P", expiry: "2026-12-18", strike: 90, qty: 4 },
          { side: "BUY", right: "P", expiry: "2026-12-18", strike: 95, qty: 2 },
        ];
        assert.equal(corridor.corridorLegSubscriptionSignature(data), sigBase);
        state.strategyLegs[1].strike = 85;
        assert.notEqual(corridor.corridorLegSubscriptionSignature(data), sigBase);

        // Reset back to 95P / 90P and simulate ONE_SHOT frame
        state.strategyLegs = [
          { side: "SELL", right: "P", expiry: "2026-12-18", strike: 95, qty: 1 },
          { side: "BUY", right: "P", expiry: "2026-12-18", strike: 90, qty: 1 },
        ];
        const preview = corridor.buildLocalCorridorPreview(data);
        const liveQuotes = preview.quotes.map((q) => ({
          ...q,
          bid: Number((q.bid + 1.0).toFixed(2)),
          ask: Number((q.ask + 1.0).toFixed(2)),
          mid: Number((q.mid + 1.0).toFixed(2)),
          updated_ts_utc: "2026-11-15T14:05:00Z",
        }));
        const oneShot = {
          ...preview,
          mode: "ONE_SHOT",
          subscribed_count: liveQuotes.length,
          subscription_cap: 90,
          market_data_type: 1,
          elapsed_ms: 142,
          updated_ts_utc: "2026-11-15T14:05:00Z",
          quotes: liveQuotes,
        };
        corridor.applyCorridorFrame(oneShot, { mergeIntoChain: true });

        // ONE_SHOT preservation on side/qty toggle (and NO /api/strategy/evaluate triggered)
        const countEvalCalls = () =>
          fetchCalls.filter((c) => c.url.includes("/api/strategy/evaluate")).length;
        const evalCallsBefore = countEvalCalls();
        state.strategyLegs[0].side = "BUY";
        state.strategyLegs[0].qty = 3;
        corridor.syncCorridorWithStrategy();
        assert.equal(state.corridorFrame.mode, "ONE_SHOT");
        assert.equal(state.corridorFrame.subscribed_count, liveQuotes.length);
        assert.equal(state.corridorFrame.updated_ts_utc, "2026-11-15T14:05:00Z");
        const evalCallsAfter = countEvalCalls();
        assert.equal(evalCallsAfter, evalCallsBefore);

        // Moving to an unquoted expiry downgrades cleanly to PREVIEW / 0 subs
        state.strategyLegs = [
          { side: "BUY", right: "C", expiry: "2027-01-15", strike: 110, qty: 1 },
        ];
        corridor.syncCorridorWithStrategy();
        assert.equal(state.corridorFrame.mode, "PREVIEW");
        assert.equal(state.corridorFrame.target_expiry, "2027-01-15");
        assert.equal(state.corridorFrame.subscribed_count, 0);

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_stream_sync_debounce_no_eval_loop_and_sse_epoch_race() -> None:
    result = _run_node_scenario(
        """
        const { state, dom } = await import(modUrl("state.js"));
        const corridor = await import(modUrl("corridor.js"));

        const data = makeSampleChainData();
        state.ticker = "NVDA";
        state.snapshotId = "snap-001";
        state.chainData = data;
        state.strategyLegs = [
          { side: "SELL", right: "P", expiry: "2026-12-18", strike: 95, qty: 1 },
        ];
        corridor.syncCorridorFromChain(data);

        let resolveStop;
        const stopOrder = [];
        nextFetchImpl = async (url, init) => {
          const u = String(url);
          fetchCalls.push({ url: u, init: init || {} });
          if (u === "/api/corridor/stop") {
            stopOrder.push("stop_start");
            return await new Promise((res) => {
              resolveStop = () => {
                stopOrder.push("stop_done");
                res({ ok: true, status: 200, text: async () => "{}" });
              };
            });
          }
          if (u === "/api/strategy/evaluate") {
            return {
              ok: true,
              status: 200,
              text: async () => JSON.stringify({
                ticker: "NVDA",
                underlying_price: 100,
                strategy_kind: "CUSTOM",
                legs: state.strategyLegs,
                max_profit: 100,
                max_loss: 400,
                net_price: 1.5,
                slippage_cost: 0.1,
                net_delta: 0.1,
                net_gamma: 0.01,
                net_theta: 0.02,
                net_vega: -0.03,
                breakevens: [93.5],
                pop_estimate: 0.65,
                payoff_curve: [],
                rationale: ["Evaluated"],
              }),
            };
          }
          return { ok: true, status: 200, text: async () => "{}" };
        };

        // 1. Start stream synchronously (wasActive=false -> EventSource opens right away)
        await corridor.startCorridorStream();
        assert.equal(eventSources.length, 1);
        const es1 = eventSources[0];
        assert.equal(state.corridorStreaming, true);

        // Frame with Auto-Reprice -> evaluateStrategyRequest -> render -> syncCorridorWithStrategy
        // Must NOT trigger stream restart when signature hasn't changed!
        const streamFrame = {
          ...corridor.buildLocalCorridorPreview(data),
          mode: "STREAM",
          sequence: 1,
          subscribed_count: 14,
        };
        es1.emit("frame", JSON.stringify(streamFrame));
        await new Promise((r) => setTimeout(r, 20));
        assert.equal(eventSources.length, 1);
        assert.equal(state.corridorStreamSyncTimer, null);

        // 2. Flip BUY/SELL during active stream -> immediate UI update, NO stream restart
        state.strategyLegs[0].side = "BUY";
        corridor.syncCorridorWithStrategy();
        assert.equal(state.corridorStreamSyncTimer, null);
        assert.ok(dom.corridorLadderTbody.innerHTML.includes("BUY P"));

        // 3. Change leg strike -> 400ms debounce -> closes es1 FIRST, awaits stop, opens es2
        state.strategyLegs[0].strike = 90;
        corridor.syncCorridorWithStrategy();
        assert.ok(state.corridorStreamSyncTimer !== null);
        await new Promise((r) => setTimeout(r, 430));
        assert.equal(es1.closed, true);
        assert.equal(stopOrder[0], "stop_start");
        // es2 must NOT open before /api/corridor/stop resolves!
        assert.equal(eventSources.length, 1);
        resolveStop();
        await new Promise((r) => setTimeout(r, 10));
        assert.equal(stopOrder[1], "stop_done");
        assert.equal(eventSources.length, 2);
        const es2 = eventSources[1];

        // Stale event emitted on closed es1 is ignored
        const staleCallsBefore = plotlyCalls.length;
        es1.emit("frame", JSON.stringify({ ...streamFrame, sequence: 99 }));
        assert.equal(plotlyCalls.length, staleCallsBefore);

        // 4. Epoch race: restart begins, stop arrives before stop finishes -> NO es3
        const pRestart = corridor.startCorridorStream();
        assert.equal(es2.closed, true);
        corridor.stopCorridorStream({ silent: true, skipRemoteStop: true });
        resolveStop();
        await pRestart;
        assert.equal(eventSources.length, 2);
        assert.equal(state.corridorStreaming, false);

        // 5. Rapid OFF -> ON awaits in-flight /api/corridor/stop even when wasActive=false
        await corridor.startCorridorStream();
        assert.equal(eventSources.length, 3);
        let stopResolveQueue = [];
        nextFetchImpl = async (url, init) => {
          const u = String(url);
          fetchCalls.push({ url: u, init: init || {} });
          if (u === "/api/corridor/stop") {
            return await new Promise((res) => {
              stopResolveQueue.push(() =>
                res({ ok: true, status: 200, text: async () => "{}" }),
              );
            });
          }
          return { ok: true, status: 200, text: async () => "{}" };
        };
        corridor.stopCorridorStream({ silent: true });
        assert.equal(stopResolveQueue.length, 1);
        const pOn = corridor.startCorridorStream();
        await new Promise((r) => setTimeout(r, 15));
        assert.equal(eventSources.length, 3); // Must NOT open before stop settles!
        stopResolveQueue.shift()();
        await pOn;
        assert.equal(eventSources.length, 4);

        // 6. Rapid OFF -> ON -> OFF cancels cleanly before opening EventSource
        corridor.stopCorridorStream({ silent: true });
        assert.equal(stopResolveQueue.length, 1);
        const pOnCancelled = corridor.startCorridorStream();
        corridor.stopCorridorStream({ silent: true, skipRemoteStop: true });
        stopResolveQueue.shift()();
        await pOnCancelled;
        assert.equal(eventSources.length, 4);
        assert.equal(state.corridorStreaming, false);

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_ladder_side_badges_tooltips_and_chart_markers() -> None:
    result = _run_node_scenario(
        """
        const { state, dom } = await import(modUrl("state.js"));
        const corridor = await import(modUrl("corridor.js"));

        const data = makeSampleChainData();
        state.ticker = "NVDA";
        state.chainData = data;
        state.strategyLegs = [
          { side: "SELL", right: "P", expiry: "2026-12-18", strike: 95, qty: 2 },
          { side: "BUY", right: "C", expiry: "2026-12-18", strike: 105, qty: 1 },
          { side: "BUY", right: "C", expiry: "2026-12-18", strike: 100, qty: 1 },
          { side: "SELL", right: "P", expiry: "2026-12-18", strike: 100, qty: 1 },
          // Off-expiry leg without bid/ask -> falls back cleanly on chart
          { side: "BUY", right: "C", expiry: "2027-01-15", strike: 110, qty: 1 },
        ];
        // Clear bid/ask/mid on off-expiry leg in chainData to verify off-expiry null-mid fallback!
        for (const c of data.contracts) {
          if (c.expiry === "2027-01-15" && c.strike === 110 && c.right === "C") {
            c.bid = null;
            c.ask = null;
            c.mid = null;
            c.last = null;
            c.close = null;
          }
        }

        // Pin Corridor to 2026-12-18 so 2027-01-15 leg is truly "Other Exp"
        dom.corridorExpirySelect.value = "2026-12-18";
        corridor.syncCorridorWithStrategy();
        const html = dom.corridorLadderTbody.innerHTML;

        // Verify strike 95 has SELL P badge (▼ 2x SELL P) on row and cell-leg-sell on Put side only
        assert.ok(html.includes("\\u25BC 2\\u00D7 SELL P"));
        assert.ok(html.includes("\\u25B2 BUY C"));
        assert.ok(html.includes("\\u25BC SELL P"));
        assert.ok(!html.includes(">LEG<"));

        // Split HTML into <tr> blocks to verify per-side highlighting!
        const rows = html.split("<tr ").slice(1);
        const row95 = rows.find((r) => r.includes(">$95<"));
        assert.ok(row95);
        const callTds95 = row95.match(/<td class="[^"]*corridor-call-cell[^"]*"/g) || [];
        const putTds95 = row95.match(/<td class="[^"]*corridor-put-cell[^"]*"/g) || [];
        assert.equal(callTds95.length, 5);
        assert.equal(putTds95.length, 5);
        assert.ok(callTds95.every((c) => !c.includes("corridor-cell-leg")));
        assert.ok(
          putTds95.every(
            (c) => c.includes("corridor-cell-leg") && c.includes("corridor-cell-leg-sell"),
          ),
        );
        assert.ok(row95.includes("STRATEGY LEG: SELL PUT x2"));

        // Verify strike 100 has BUY C on Call cells AND SELL P on Put cells
        const row100 = rows.find((r) => r.includes(">$100<"));
        assert.ok(row100);
        const callTds100 = row100.match(/<td class="[^"]*corridor-call-cell[^"]*"/g) || [];
        const putTds100 = row100.match(/<td class="[^"]*corridor-put-cell[^"]*"/g) || [];
        assert.ok(
          callTds100.every(
            (c) => c.includes("corridor-cell-leg-buy") && !c.includes("corridor-cell-leg-sell"),
          ),
        );
        assert.ok(
          putTds100.every(
            (c) => c.includes("corridor-cell-leg-sell") && !c.includes("corridor-cell-leg-buy"),
          ),
        );
        assert.ok(row100.includes("STRATEGY LEG: BUY CALL x1"));
        assert.ok(row100.includes("STRATEGY LEG: SELL PUT x1"));

        // Verify Plotly chart output: traces + vertical annotations
        assert.ok(plotlyCalls.length >= 1);
        const lastPlot = plotlyCalls[plotlyCalls.length - 1];
        const traceNames = lastPlot.traces.map((t) => t.name);
        assert.ok(traceNames.includes("Strategy Legs"));
        assert.ok(traceNames.includes("Strategy Legs (Other Exp)"));
        const stratTrace = lastPlot.traces.find((t) => t.name === "Strategy Legs");
        const otherTrace = lastPlot.traces.find((t) => t.name === "Strategy Legs (Other Exp)");
        assert.ok(stratTrace.marker.symbol.includes("triangle-up"));
        assert.ok(stratTrace.marker.symbol.includes("triangle-down"));
        assert.ok(stratTrace.marker.color.includes("#B45309"));
        assert.ok(stratTrace.marker.color.includes("#EA580C"));
        assert.ok(!stratTrace.marker.color.includes("#0F6E4F"));
        assert.ok(!stratTrace.marker.color.includes("#A82020"));
        assert.deepEqual(otherTrace.x, [110]);
        assert.equal(otherTrace.marker.symbol[0], "diamond-open");
        assert.equal(otherTrace.marker.color[0], "#B45309");
        assert.ok(Number.isFinite(otherTrace.y[0]));
        const legAnnots = (lastPlot.layout.annotations || []).filter(
          (a) =>
            !String(a.text).includes("SPOT ") &&
            !String(a.text).includes("PW ") &&
            !String(a.text).includes("CW "),
        );
        assert.equal(legAnnots.length, 4);
        assert.ok(legAnnots.some((a) => String(a.text).includes("\\u25BC 2\\u00D7 SELL P $95")));
        assert.ok(
          legAnnots.some((a) =>
            String(a.text).includes("\\u25B2 BUY C + \\u25BC SELL P $100"),
          ),
        );
        assert.ok(legAnnots.some((a) => String(a.text).includes("\\u25B2 BUY C $110 (61d)")));
        const legShapes = (lastPlot.layout.shapes || []).filter(
          (s) => s.line && (s.line.color === "#B45309" || s.line.color === "#EA580C"),
        );
        assert.ok(legShapes.length >= 2);
        assert.ok(legShapes.every((s) => s.line.dash === "dot"));
        const yVals = new Set(legAnnots.map((a) => a.y));
        assert.ok(yVals.has(0.62));
        assert.ok(yVals.has(0.32));

        // Verify malformed leg fields are rejected and HTML in marker/hover is escaped
        state.strategyLegs = [
          {
            side: "<script>alert(1)</script>",
            right: "C",
            expiry: "2027-03-19<img/src=x>",
            strike: 105,
            qty: 1,
          },
          { side: "BUY", right: "C", expiry: "2026-12-18", strike: Infinity, qty: 1 },
          { side: "SELL", right: "X", expiry: "2026-12-18", strike: 95, qty: 1 },
        ];
        const sigSanitized = corridor.corridorLegSubscriptionSignature(data);
        assert.ok(sigSanitized.includes("2027-03-19<img/src=x>|105|C"));
        assert.ok(!sigSanitized.includes("Infinity"));
        assert.ok(!sigSanitized.includes("|X"));
        const lookupSanitized = corridor.buildCorridorLegLookup(state.strategyLegs);
        assert.equal(lookupSanitized["2027-03-19<img/src=x>|105|C"][0].side, "BUY");
        corridor.renderCorridorChart(state.corridorFrame);
        const xssPlot = plotlyCalls[plotlyCalls.length - 1];
        const xssOther = xssPlot.traces.find((t) => t.name === "Strategy Legs (Other Exp)");
        assert.ok(xssOther);
        assert.equal(xssOther.x.length, 1);
        assert.ok(!xssOther.text[0].includes("<script>"));
        assert.ok(!xssOther.text[0].includes("<img"));
        assert.ok(xssOther.text[0].includes("&lt;img/src=x&gt;"));
        const xssAnnot = xssPlot.layout.annotations.find((a) =>
          String(a.text).includes("BUY C $105"),
        );
        assert.ok(xssAnnot);
        assert.ok(!String(xssAnnot.text).includes("<img"));
        assert.ok(String(xssAnnot.text).includes("&lt;img/src=x&gt;"));

        // Verify DTE switch + STREAM frame with Tier-2 secondary quotes never shows duplicate walls
        dom.corridorExpirySelect.value = "2027-01-15";
        state.strategyLegs = [
          { side: "SELL", right: "P", expiry: "2026-12-18", strike: 95, qty: 2 },
          { side: "SELL", right: "C", expiry: "2026-12-18", strike: 105, qty: 1 },
        ];
        corridor.syncCorridorWithStrategy();
        const preview2027 = state.corridorFrame;
        assert.equal(preview2027.target_expiry, "2027-01-15");
        const offLegsInPreview = preview2027.quotes.filter(
          (q) => q.expiry === "2026-12-18" && q.is_strategy_leg,
        );
        assert.equal(offLegsInPreview.length, 2);
        assert.ok(offLegsInPreview.every((q) => !q.is_put_wall && !q.is_call_wall));
        const previewHtml = dom.corridorLadderTbody.innerHTML;
        assert.equal((previewHtml.match(/wall-badge-put/g) || []).length, 1);
        assert.equal((previewHtml.match(/wall-badge-call/g) || []).length, 1);

        // Simulate backend STREAM frame with off-expiry Tier-2 quotes (defensive wall flags)
        corridor.applyCorridorFrame(
          {
            ticker: "NVDA",
            snapshot_id: "snap-001",
            target_expiry: "2027-01-15",
            target_dte: 61,
            window_strikes: 10,
            max_contracts: 88,
            subscribed_count: 8,
            subscription_cap: 90,
            underlying_price: 100,
            market_data_type: 3,
            primary_put_wall: 90,
            primary_call_wall: 110,
            wall_basis: "VOLUME",
            updated_ts_utc: "2026-11-17T20:00:00Z",
            sequence: 1,
            mode: "STREAM",
            changed_keys: [],
            notices: [],
            quotes: [
              // Target expiry 2027-01-15 (PW 90, CW 110)
              {
                expiry: "2027-01-15",
                dte: 61,
                strike: 90,
                right: "P",
                is_put_wall: true,
                is_call_wall: false,
                is_strategy_leg: false,
                bid: 2.1,
                ask: 2.3,
                mid: 2.2,
              },
              {
                expiry: "2027-01-15",
                dte: 61,
                strike: 100,
                right: "C",
                is_put_wall: false,
                is_call_wall: false,
                is_strategy_leg: false,
                bid: 5.0,
                ask: 5.2,
                mid: 5.1,
              },
              {
                expiry: "2027-01-15",
                dte: 61,
                strike: 110,
                right: "C",
                is_put_wall: false,
                is_call_wall: true,
                is_strategy_leg: false,
                bid: 2.0,
                ask: 2.2,
                mid: 2.1,
              },
              // Off-expiry Strategy Legs on 2026-12-18 (even if stale wall flags were set)
              {
                expiry: "2026-12-18",
                dte: 33,
                strike: 95,
                right: "P",
                is_put_wall: true,
                is_call_wall: false,
                is_strategy_leg: true,
                bid: 1.5,
                ask: 1.7,
                mid: 1.6,
              },
              {
                expiry: "2026-12-18",
                dte: 33,
                strike: 105,
                right: "C",
                is_put_wall: false,
                is_call_wall: true,
                is_strategy_leg: true,
                bid: 1.4,
                ask: 1.6,
                mid: 1.5,
              },
              // Tier-2 non-leg secondary expiry quotes on 2026-12-18
              {
                expiry: "2026-12-18",
                dte: 33,
                strike: 85,
                right: "P",
                is_put_wall: true,
                is_call_wall: false,
                is_strategy_leg: false,
                bid: 0.4,
                ask: 0.5,
                mid: 0.45,
              },
              {
                expiry: "2026-12-18",
                dte: 33,
                strike: 115,
                right: "C",
                is_put_wall: false,
                is_call_wall: true,
                is_strategy_leg: false,
                bid: 0.3,
                ask: 0.4,
                mid: 0.35,
              },
            ],
          },
          { mergeIntoChain: false },
        );
        const streamHtml = dom.corridorLadderTbody.innerHTML;
        assert.equal((streamHtml.match(/wall-badge-put/g) || []).length, 1);
        assert.equal((streamHtml.match(/wall-badge-call/g) || []).length, 1);
        assert.equal((streamHtml.match(/corridor-row-put-wall/g) || []).length, 1);
        assert.equal((streamHtml.match(/corridor-row-call-wall/g) || []).length, 1);
        assert.ok(!streamHtml.includes(">$85<"));
        assert.ok(!streamHtml.includes(">$115<"));
        const streamRows = streamHtml.split("<tr ").slice(1);
        const offRow95 = streamRows.find((r) => r.includes(">$95<"));
        assert.ok(offRow95);
        assert.ok(!offRow95.includes("corridor-row-put-wall"));
        assert.ok(!offRow95.includes("PUT WALL"));

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True


def test_strategy_lab_and_event_mutation_hooks_sync_corridor() -> None:
    result = _run_node_scenario(
        """
        const { state, dom } = await import(modUrl("state.js"));
        const helpers = await import(modUrl("helpers.js"));
        const corridor = await import(modUrl("corridor.js"));
        const strategy = await import(modUrl("strategy_lab.js"));
        const events = await import(modUrl("events.js"));

        const data = makeSampleChainData();
        state.ticker = "NVDA";
        state.snapshotId = "snap-001";
        state.chainData = data;
        dom.strategyPresetPills.appendChild((() => {
          const btn = document.createElement("button");
          btn.setAttribute("data-strategy-preset", "CUSTOM");
          return btn;
        })());

        // Populate selectors cleanly
        const allSelects = [dom.wallsDteSelect, dom.strategyExpirySelect, dom.corridorExpirySelect];
        for (const sel of allSelects) {
          sel.innerHTML = "";
          const autoOpt = document.createElement("option");
          autoOpt.value = "";
          autoOpt.textContent = "Auto (Follow Walls)";
          sel.appendChild(autoOpt);
          for (const e of helpers.sortedChainExpiries(data)) {
            const o = document.createElement("option");
            o.value = e.expiry;
            o.textContent = `${e.expiry} (${e.dte}d)`;
            sel.appendChild(o);
          }
        }
        dom.wallsDteSelect.value = "";
        dom.strategyExpirySelect.value = "";
        dom.corridorExpirySelect.value = "";

        events.bindEvents();
        strategy.syncStrategySelectors(data);
        corridor.syncCorridorFromChain(data);

        // 1. Trigger inline leg edit (change side + change expiry -> Corridor follows)
        const tr0 = dom.strategyLegsTbody.children[0];
        const selSide = tr0.children[0].children[0];
        const selExp = tr0.children[2].children[0];
        selSide.value = "BUY";
        selSide.dispatchEvent({ type: "change" });
        assert.equal(state.strategyLegs[0].side, "BUY");
        assert.ok(dom.corridorLadderTbody.innerHTML.includes("BUY"));

        // Make all legs point to 2027-01-15 via inline expiry select edits
        for (let i = 0; i < state.strategyLegs.length; i++) {
          const row = dom.strategyLegsTbody.children[i];
          const sExp = row.children[2].children[0];
          sExp.value = "2027-01-15";
          sExp.dispatchEvent({ type: "change" });
        }
        assert.equal(state.corridorFrame.target_expiry, "2027-01-15");
        assert.ok(dom.corridorExpirySelect.options[0].textContent.includes("Follow Strategy"));

        // 2. addSpecificStrategyLeg fixes old .source === "CACHED_PREVIEW" bug and syncs Corridor
        strategy.addSpecificStrategyLeg("2027-02-19", "C", 115);
        const hasFebLeg = () =>
          state.corridorFrame.quotes.some(
            (q) => q.expiry === "2027-02-19" && q.strike === 115 && q.is_strategy_leg,
          );
        assert.ok(hasFebLeg());

        // 3. Delete added leg via table 'x' button -> Corridor removes 2027-02-19 priority leg
        const lastRow = dom.strategyLegsTbody.children[dom.strategyLegsTbody.children.length - 1];
        const delBtn = lastRow.children[8].children[0];
        delBtn.dispatchEvent({ type: "click" });
        assert.ok(!hasFebLeg());

        // 4. wallsDteSelect change with Auto-Build OFF refreshes idle seed legs + syncs Corridor
        state.strategyKind = "BULL_PUT_SPREAD";
        state.strategyEvaluation = null;
        state.strategyEvaluationDirty = false;
        dom.strategyExpirySelect.value = "";
        dom.wallsDteSelect.value = "2027-02-19";
        dom.wallsDteSelect.dispatchEvent({ type: "change" });
        assert.equal(state.corridorFrame.target_expiry, "2027-02-19");

        // 5. CUSTOM preset click + strategyExpirySelect change syncs Corridor
        const customBtn = dom.strategyPresetPills.children[0];
        customBtn.dispatchEvent({ type: "click" });
        assert.equal(state.strategyKind, "CUSTOM");
        dom.strategyExpirySelect.value = "2027-01-15";
        dom.strategyExpirySelect.dispatchEvent({ type: "change" });
        assert.equal(state.corridorFrame.target_expiry, "2027-01-15");
        assert.ok(dom.corridorExpirySelect.options[0].textContent.includes("Follow Strategy"));

        console.log(JSON.stringify({ ok: true }));
        """
    )
    assert result["ok"] is True
