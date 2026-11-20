/* IBKR Options Analyzer dashboard — Workspace tabs, Row 3 Pull-Scope popover, and sticky context bar. */

import {
  dom,
} from "./state.js";
import {
  syncPullScopeSummary,
} from "./helpers.js";
import {
  applyWorkspaceMode,
} from "./panels.js";
import {
  refreshFromIBKR,
} from "./network.js";

export function setPullScopeOpen(open) {
  if (!dom.pullScopePanel || !dom.pullScopeToggle) return;
  var show = Boolean(open);
  dom.pullScopePanel.hidden = !show;
  dom.pullScopeToggle.setAttribute("aria-expanded", show ? "true" : "false");
}

export function setStickyBarVisible(visible) {
  if (!dom.stickyContextBar) return;
  var show = Boolean(visible);
  dom.stickyContextBar.classList.toggle("is-visible", show);
  dom.stickyContextBar.setAttribute("aria-hidden", show ? "false" : "true");
}

function wireWorkspaceTabGroup(groupEl) {
  if (!groupEl) return;
  groupEl.addEventListener("click", function (ev) {
    var btn =
      ev.target && ev.target.closest ? ev.target.closest("[data-workspace-tab]") : null;
    if (!btn) return;
    var mode = btn.getAttribute("data-workspace-tab");
    if (!mode) return;
    applyWorkspaceMode(mode, { persist: true });
  });
}

function wireStickyBarScrollObserver() {
  if (!dom.stickyContextBar || !dom.controlBar) return;
  if (typeof window.IntersectionObserver === "function") {
    var observer = new window.IntersectionObserver(
      function (entries) {
        if (!entries || !entries.length) return;
        var entry = entries[0];
        var scrolledPast = !entry.isIntersecting && entry.boundingClientRect.bottom <= 0;
        setStickyBarVisible(scrolledPast);
      },
      { threshold: 0 }
    );
    observer.observe(dom.controlBar);
    return;
  }
  window.addEventListener("scroll", function () {
    if (!dom.controlBar || typeof dom.controlBar.getBoundingClientRect !== "function") return;
    var rect = dom.controlBar.getBoundingClientRect();
    setStickyBarVisible(rect.bottom <= 0);
  });
}

export function wireWorkspaceControls() {
  syncPullScopeSummary();
  wireWorkspaceTabGroup(dom.workspaceTabs);
  wireWorkspaceTabGroup(dom.stickyWorkspaceTabs);

  if (dom.pullScopeToggle && dom.pullScopePanel) {
    dom.pullScopeToggle.addEventListener("click", function (ev) {
      ev.stopPropagation();
      setPullScopeOpen(Boolean(dom.pullScopePanel.hidden));
    });
    document.addEventListener("click", function (ev) {
      if (dom.pullScopePanel && !dom.pullScopePanel.hidden) {
        if (dom.pullScopePicker && !dom.pullScopePicker.contains(ev.target)) {
          setPullScopeOpen(false);
        }
      }
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && dom.pullScopePanel && !dom.pullScopePanel.hidden) {
        setPullScopeOpen(false);
        if (dom.pullScopeToggle && typeof dom.pullScopeToggle.focus === "function") {
          dom.pullScopeToggle.focus();
        }
      }
    });
  }

  if (dom.stickyRefreshBtn) {
    dom.stickyRefreshBtn.addEventListener("click", refreshFromIBKR);
  }

  if (dom.stickyTopBtn) {
    dom.stickyTopBtn.addEventListener("click", function () {
      if (typeof window.scrollTo === "function") {
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
    });
  }

  wireStickyBarScrollObserver();
}
