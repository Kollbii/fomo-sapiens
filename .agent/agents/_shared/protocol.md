## Shared Sub-Agent Protocol (all agents)

**Hard invariants — never violate, never "temporarily" relax:**
1. No order/mutation APIs (`placeOrder`, `cancelOrder`, `reqGlobalCancel`, `exerciseOptions`, `whatIfOrder[Async]`) and no account queries (`reqAccountSummary`, `reqPositions`, `reqPnL`, `managedAccounts`). Never weaken `src/core/security.py`.
2. Every user ticker passes `TickerValidator.validate()` before touching file paths or IBKR contracts.
3. Never request an unfiltered chain: weekly `[dte_min, dte_max]` DTE + `range_pct` + `strike_step` filters, pages `<= 80`, corridor `<= 90` (`88` options + `1` spot = `89 <= 90`, keeping a 10-line buffer below IBKR's 100-line ceiling), every `reqMktData` paired with `cancelMktData` in `finally:`.
4. Never block the event loop (`time.sleep`, sync sockets). Use `asyncio.sleep` and `*Async` ib-async calls.
5. Never overwrite `data/snapshots/**`; writes are atomic (`.tmp` + `os.replace`).
6. No `pandas` in `src/`. Polars + NumPy/SciPy only. `mypy --strict` clean, full type hints.

**Token discipline:** read only your *Context Diet* files. In large files use search (`rg -n`, `grep -n`, or PowerShell `Select-String`) to locate the symbol, then read a bounded line range. Never paste whole files into your answer.

**Scope discipline:** edit only paths you *Own*. If the correct fix lies outside your scope, stop and hand off — do not patch around it.

**Verification:** before reporting done, run the narrowest relevant test file (`uv run --no-sync pytest -q <file>`), then `make check`. Reuse `tests/support/` helpers and `tests/conftest.py` fixtures instead of re-defining drivers/fakes.

**Hand-off format (always end with this block, <= 12 lines):**

    [HANDOFF <agent-name>]
    status: DONE | BLOCKED | NEEDS_REVIEW
    changed: <paths, comma-separated>
    contracts_touched: <public functions / API routes / CSV columns / config keys / CSS ids changed, or "none">
    tests: <command + result>
    docs_impact: <which docs/*.md need sync, or "none">
    next: <suggested next agent: doc-sync-auditor | safety-reviewer | qa-git-runner | <domain agent>>
    notes: <blockers, assumptions, risks — 1-3 lines>
