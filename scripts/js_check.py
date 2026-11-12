"""Syntax-check frontend JavaScript with `node --check` (run by `make js-check`).

The project has no JS build or test toolchain; this is a zero-dependency parse gate so a broken
`dashboard.js` edit fails `make check` instead of silently blanking the UI. If Node.js is not
installed the check is skipped (exit 0) unless `--strict` is given. ES-module syntax is detected
automatically by Node >= 22.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "src" / "web" / "static" / "js"


def js_files(js_dir: Path = JS_DIR) -> list[Path]:
    return sorted(p for p in js_dir.rglob("*.js") if "vendor" not in p.parts)


def check(files: list[Path], node: str) -> list[str]:
    """Return one error string per file that fails to parse."""
    errors: list[str] = []
    for path in files:
        result = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            rel = path.relative_to(ROOT).as_posix()
            errors.append(f"{rel}:\n{result.stderr.strip()}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="node --check every frontend JS file")
    parser.add_argument("--strict", action="store_true", help="fail when node is missing")
    args = parser.parse_args(argv)
    node = shutil.which("node")
    if node is None:
        print("JS Syntax Check: SKIPPED (node not found)")
        return 1 if args.strict else 0
    files = js_files()
    errors = check(files, node)
    if errors:
        print("JS SYNTAX ERROR:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    print(f"JS Syntax Check: PASS ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
