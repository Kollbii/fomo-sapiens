"""Static read-only / anti-pattern guard for `src/` (run by `make security-guard`).

Enforces AGENTS.md §6 statically (Layer 3 of docs/SECURITY_AND_SAFETY.md). `src/core/security.py`
is exempt because it names the blocked methods in order to trap them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXEMPT: frozenset[str] = frozenset({"src/core/security.py"})

# Strict scoped allow-list: ONLY `src/broker/ledger_sync.py` may call read-only async
# execution/position queries; sync variants and all other account/order APIs stay blocked.
SCOPED_ALLOWLIST: dict[str, tuple[re.Pattern[str], ...]] = {
    "src/broker/ledger_sync.py": (
        re.compile(r"\.reqExecutionsAsync\s*\("),
        re.compile(r"\.reqPositionsAsync\s*\("),
    ),
}

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "order/mutation API",
        re.compile(
            r"\.(placeOrder|cancelOrder|reqGlobalCancel|exerciseOptions|whatIfOrder|whatIfOrderAsync)\s*\("
        ),
    ),
    (
        "account/position query",
        re.compile(
            r"\.(reqAccountSummary\w*|reqAccountUpdates\w*|reqPositions\w*|reqPnL\w*"
            r"|managedAccounts|reqExecutions\w*|reqOpenOrders\w*|reqAllOpenOrders\w*)\s*\("
        ),
    ),
    ("blocking sleep (use asyncio.sleep)", re.compile(r"\btime\.sleep\s*\(")),
    ("pandas import (use polars)", re.compile(r"^\s*(import pandas|from pandas\b)")),
)


def scan(src_dir: Path = ROOT / "src", *, root: Path = ROOT) -> list[str]:
    hits: list[str] = []
    for path in sorted(src_dir.rglob("*.py")):
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
        if rel in EXEMPT:
            continue
        allowed_patterns = SCOPED_ALLOWLIST.get(rel, ())
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            check_line = line
            for allowed_pat in allowed_patterns:
                check_line = allowed_pat.sub(".SCOPED_ALLOWED(", check_line)
            for label, pattern in RULES:
                if pattern.search(check_line):
                    hits.append(f"{rel}:{lineno}: [{label}] {line.strip()}")
    return hits


def main() -> int:
    hits = scan()
    if hits:
        print("SECURITY VIOLATION:\n" + "\n".join(hits), file=sys.stderr)
        return 1
    print("Read-Only Security Guard: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
