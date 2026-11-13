/* IBKR Options Analyzer dashboard — Snap-to-grid panel resizing and Plotly resize scheduling. */

var SNAP_COLS = [4, 6, 8, 12];
var SNAP_HEIGHTS = [290, 350, 410, 470, 530, 590, 650, 720, 800, 880, 960, 1080];
var GRID_LAYOUT_KEY = "ibkr_chart_grid_layout_v1";

function readGridLayoutPrefs() {
  try {
    var raw = window.localStorage.getItem(GRID_LAYOUT_KEY);
    if (!raw) return {};
    var parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (_err) {
    return {};
  }
}

function writeGridLayoutPrefs(layoutMap) {
  try {
    window.localStorage.setItem(GRID_LAYOUT_KEY, JSON.stringify(layoutMap));
  } catch (_err) {
    /* ignore */
  }
}

function nearestSnapValue(value, candidates) {
  var best = candidates[0];
  var bestDist = Math.abs(value - best);
  for (var i = 1; i < candidates.length; i++) {
    var d = Math.abs(value - candidates[i]);
    if (d < bestDist) {
      best = candidates[i];
      bestDist = d;
    }
  }
  return best;
}

function applyCardSpanClass(card, span) {
  SNAP_COLS.forEach(function (col) {
    card.classList.remove("panel-card--span-" + col);
  });
  if (span) {
    card.classList.add("panel-card--span-" + span);
  }
}

function computeTargetSnap(gridEl, measuredWidth, measuredHeight) {
  var gridWidth = gridEl ? gridEl.getBoundingClientRect().width : 0;
  var gapPx = 16;
  if (gridEl) {
    var parsedGap = parseFloat(window.getComputedStyle(gridEl).columnGap);
    if (!isNaN(parsedGap)) gapPx = parsedGap;
  }
  var trackWidth = gridWidth > 0 ? Math.max(20, (gridWidth - gapPx * 11) / 12) : 80;
  var bestSpan = 6;
  var bestSpanDist = Infinity;
  for (var i = 0; i < SNAP_COLS.length; i++) {
    var cols = SNAP_COLS[i];
    var targetW = cols * trackWidth + (cols - 1) * gapPx;
    var dist = Math.abs(measuredWidth - targetW);
    if (dist < bestSpanDist) {
      bestSpanDist = dist;
      bestSpan = cols;
    }
  }
  var bestHeight = nearestSnapValue(measuredHeight, SNAP_HEIGHTS);
  return { span: bestSpan, height: bestHeight };
}

var rafByCanvasId = {};
export function schedulePlotlyResize(canvasEl) {
  if (!canvasEl || typeof window.Plotly === "undefined" || !canvasEl.data) return;
  var cardEl = canvasEl.closest ? canvasEl.closest(".panel-card") : null;
  if (cardEl && cardEl.classList.contains("is-hidden")) return;
  var cid = canvasEl.id || "chart";
  if (rafByCanvasId[cid]) {
    window.cancelAnimationFrame(rafByCanvasId[cid]);
  }
  rafByCanvasId[cid] = window.requestAnimationFrame(function () {
    var cardNow = canvasEl.closest ? canvasEl.closest(".panel-card") : null;
    if (cardNow && cardNow.classList.contains("is-hidden")) return;
    var rect = canvasEl.getBoundingClientRect ? canvasEl.getBoundingClientRect() : null;
    if (rect && (rect.width <= 0 || rect.height <= 0)) return;
    if (window.Plotly && window.Plotly.Plots && canvasEl.data) {
      window.Plotly.Plots.resize(canvasEl);
    }
  });
}

export function initChartResizeHandling() {
  var gridEl = document.querySelector(".chart-grid");
  var cards = document.querySelectorAll(".chart-grid .panel-card");
  if (!cards.length) return;

  function cardKey(card, idx) {
    if (card.id) return card.id;
    var canvas = card.querySelector(".chart-canvas");
    return canvas && canvas.id ? canvas.id : "panel-" + idx;
  }

  var savedLayout = readGridLayoutPrefs();

  function cardSpanValue(card) {
    for (var s = 0; s < SNAP_COLS.length; s++) {
      if (card.classList.contains("panel-card--span-" + SNAP_COLS[s])) {
        return SNAP_COLS[s];
      }
    }
    return 6;
  }

  function updateCardSpanButton(card) {
    var btn = card.querySelector(".panel-span-btn");
    if (btn) {
      btn.textContent = cardSpanValue(card) + "/12";
    }
  }

  function snapCardToGrid(card, key, measuredWidth, measuredHeight) {
    var snap = computeTargetSnap(gridEl, measuredWidth, measuredHeight);
    card.style.width = "";
    card.style.maxWidth = "";
    card.style.height = snap.height + "px";
    applyCardSpanClass(card, snap.span);
    updateCardSpanButton(card);
    card.classList.remove("is-snapping-drag");
    card.removeAttribute("data-snap-hint");

    savedLayout[key] = { span: snap.span, height: snap.height };
    writeGridLayoutPrefs(savedLayout);

    var canvasEl = card.querySelector(".chart-canvas");
    if (canvasEl) {
      schedulePlotlyResize(canvasEl);
      window.setTimeout(function () {
        schedulePlotlyResize(canvasEl);
      }, 60);
    }
  }

  var observer =
    typeof window.ResizeObserver !== "undefined"
      ? new window.ResizeObserver(function (entries) {
          for (var i = 0; i < entries.length; i++) {
            var canvas = entries[i].target.querySelector(".chart-canvas");
            if (canvas) schedulePlotlyResize(canvas);
          }
        })
      : null;

  for (var i = 0; i < cards.length; i++) {
    (function (card, idx) {
      var key = cardKey(card, idx);
      var defaultSpan = "";
      for (var s = 0; s < SNAP_COLS.length; s++) {
        if (card.classList.contains("panel-card--span-" + SNAP_COLS[s])) {
          defaultSpan = String(SNAP_COLS[s]);
          break;
        }
      }
      card.dataset.defaultSpan = defaultSpan;

      var pref = savedLayout[key];
      if (pref && typeof pref === "object") {
        if (pref.span && SNAP_COLS.indexOf(Number(pref.span)) !== -1) {
          applyCardSpanClass(card, Number(pref.span));
        }
        if (pref.height && !isNaN(Number(pref.height))) {
          var restoredH = Number(pref.height);
          var minGuard =
            key === "strategy-lab-card"
              ? 530
              : key === "backtest-card"
                ? 540
                : key === "trade-ledger-card"
                  ? 500
                  : key === "wall-corridor-card"
                    ? 460
                    : 0;
          if (restoredH >= minGuard) {
            card.style.height = restoredH + "px";
          }
        }
      }

      if (observer) {
        observer.observe(card);
      }

      var canvasEl = card.querySelector(".chart-canvas");
      var titleEl = card.querySelector(".panel-title");

      // Clickable grid width cycle badge ([6/12] -> 8/12 -> 12/12 -> 4/12 -> 6/12)
      if (titleEl && !titleEl.querySelector(".panel-span-btn")) {
        var spanBtn = document.createElement("button");
        spanBtn.type = "button";
        spanBtn.className = "panel-span-btn";
        spanBtn.title = "Click to cycle panel width (4/6/8/12 columns)";
        spanBtn.setAttribute("aria-label", "Cycle panel grid width");
        spanBtn.addEventListener("dblclick", function (ev) {
          ev.stopPropagation();
        });
        spanBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          var curSpan = cardSpanValue(card);
          var cycle = [6, 8, 12, 4];
          var nextSpan = cycle[(cycle.indexOf(curSpan) + 1) % cycle.length];
          var curHeight = Math.round(card.getBoundingClientRect().height) || 410;
          card.style.width = "";
          card.style.maxWidth = "";
          if (key === "strategy-lab-card" || key === "backtest-card") {
            card.style.height = "";
            savedLayout[key] = { span: nextSpan };
          } else {
            card.style.height = curHeight + "px";
            savedLayout[key] = { span: nextSpan, height: curHeight };
          }
          applyCardSpanClass(card, nextSpan);
          updateCardSpanButton(card);
          writeGridLayoutPrefs(savedLayout);
          if (canvasEl) {
            schedulePlotlyResize(canvasEl);
            window.setTimeout(function () {
              schedulePlotlyResize(canvasEl);
            }, 60);
          }
        });
        titleEl.appendChild(spanBtn);
        updateCardSpanButton(card);
      }

      // Dedicated PointerEvents corner handle (never blocked by CSS Grid max-width or Plotly)
      var handle = card.querySelector(".panel-resize-handle");
      if (!handle) {
        handle = document.createElement("button");
        handle.type = "button";
        handle.className = "panel-resize-handle";
        handle.setAttribute("aria-label", "Drag corner to resize panel");
        handle.title = "Drag corner to resize and snap to 12-column grid";
        card.appendChild(handle);
      }

      var dragState = null;

      handle.addEventListener("pointerdown", function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        var startRect = card.getBoundingClientRect();
        var gridRect = gridEl ? gridEl.getBoundingClientRect() : startRect;
        dragState = {
          pointerId: ev.pointerId,
          startX: ev.clientX,
          startY: ev.clientY,
          startW: startRect.width,
          startH: startRect.height,
          maxW: Math.max(startRect.width, gridRect.width),
          mobileOnlyY: window.innerWidth <= 1120,
        };
        card.classList.add("is-snapping-drag");
        card.style.maxWidth = "none";
        if (!dragState.mobileOnlyY) {
          card.style.width = Math.round(startRect.width) + "px";
        }
        card.style.height = Math.round(startRect.height) + "px";
        var initSnap = computeTargetSnap(gridEl, startRect.width, startRect.height);
        card.setAttribute(
          "data-snap-hint",
          "Snap " + initSnap.span + "/12 col • " + initSnap.height + "px"
        );
        if (handle.setPointerCapture) {
          try {
            handle.setPointerCapture(ev.pointerId);
          } catch (_err) {
            /* ignore synthetic pointer events */
          }
        }
      });

      handle.addEventListener("pointermove", function (ev) {
        if (!dragState || ev.pointerId !== dragState.pointerId) return;
        var dx = ev.clientX - dragState.startX;
        var dy = ev.clientY - dragState.startY;
        var nextW = dragState.startW;
        if (!dragState.mobileOnlyY) {
          nextW = Math.max(260, Math.min(dragState.maxW, dragState.startW + dx));
          card.style.width = Math.round(nextW) + "px";
        }
        var nextH = Math.max(290, Math.min(1200, dragState.startH + dy));
        card.style.height = Math.round(nextH) + "px";
        var preview = computeTargetSnap(gridEl, nextW, nextH);
        card.setAttribute(
          "data-snap-hint",
          "Snap " + preview.span + "/12 col • " + preview.height + "px"
        );
        if (canvasEl) schedulePlotlyResize(canvasEl);
      });

      function endHandleDrag(ev) {
        if (!dragState) return;
        if (ev && ev.pointerId !== undefined && ev.pointerId !== dragState.pointerId) return;
        var st = dragState;
        dragState = null;
        if (handle.releasePointerCapture && ev && ev.pointerId !== undefined) {
          try {
            if (!handle.hasPointerCapture || handle.hasPointerCapture(ev.pointerId)) {
              handle.releasePointerCapture(ev.pointerId);
            }
          } catch (_err) {
            /* ignore */
          }
        }
        var measuredW = st.mobileOnlyY
          ? card.getBoundingClientRect().width
          : parseFloat(card.style.width) || card.getBoundingClientRect().width;
        var measuredH = parseFloat(card.style.height) || card.getBoundingClientRect().height;
        if (st.mobileOnlyY) {
          var heightSnap = nearestSnapValue(measuredH, SNAP_HEIGHTS);
          card.style.width = "";
          card.style.maxWidth = "";
          card.style.height = heightSnap + "px";
          card.classList.remove("is-snapping-drag");
          card.removeAttribute("data-snap-hint");
          savedLayout[key] = { span: cardSpanValue(card), height: heightSnap };
          writeGridLayoutPrefs(savedLayout);
          if (canvasEl) schedulePlotlyResize(canvasEl);
        } else {
          snapCardToGrid(card, key, measuredW, measuredH);
        }
      }

      handle.addEventListener("pointerup", endHandleDrag);
      handle.addEventListener("pointercancel", endHandleDrag);

      if (titleEl) {
        titleEl.title =
          "Drag bottom-right corner to snap to 12-col grid; click [6/12] to resize width; double-click title to reset";
        titleEl.addEventListener("dblclick", function () {
          card.style.width = "";
          card.style.maxWidth = "";
          card.style.height = "";
          card.classList.remove("is-snapping-drag");
          card.removeAttribute("data-snap-hint");
          applyCardSpanClass(
            card,
            card.dataset.defaultSpan ? Number(card.dataset.defaultSpan) : null
          );
          updateCardSpanButton(card);
          delete savedLayout[key];
          writeGridLayoutPrefs(savedLayout);
          if (canvasEl) {
            schedulePlotlyResize(canvasEl);
            window.setTimeout(function () {
              schedulePlotlyResize(canvasEl);
            }, 60);
          }
        });
      }
    })(cards[i], i);
  }
}
