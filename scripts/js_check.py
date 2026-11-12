"""Frontend JavaScript gate (run by `make js-check`).

The project has no JS build or test toolchain, so two zero-dependency checks keep a broken edit
from silently blanking the UI:

1. ES-module graph (pure Python, always runs): every relative `import { a } from "./x.js"`
   must point at an existing file that `export`s each imported name.
2. `node --check` syntax parse of every file (skipped when Node.js is absent unless `--strict`;
   ES-module syntax is auto-detected by Node >= 22).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS_DIR = ROOT / "src" / "web" / "static" / "js"


def js_files(js_dir: Path = JS_DIR) -> list[Path]:
    return sorted(p for p in js_dir.rglob("*.js") if "vendor" not in p.parts)


IMPORT_RE = re.compile(r"import\s*\{([^}]*)\}\s*from\s*[\"'](\.[^\"']+)[\"']")
EXPORT_RE = re.compile(r"^export\s+(?:async\s+)?(?:function\*?|var|let|const|class)\s+(\w+)", re.M)


def check_module_graph(files: list[Path]) -> list[str]:
    """Return one error per unresolved relative import or missing named export."""
    errors: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT).as_posix()
        for names_blob, spec in IMPORT_RE.findall(text):
            target = (path.parent / spec.split("?")[0]).resolve()
            if not target.is_file():
                errors.append(f"{rel}: import target not found: {spec}")
                continue
            exported = set(EXPORT_RE.findall(target.read_text(encoding="utf-8")))
            for raw in names_blob.split(","):
                name = raw.strip().split(" as ")[0].strip()
                if name and name not in exported:
                    errors.append(f"{rel}: {spec} does not export {name!r}")
    return errors


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
    files = js_files()
    graph_errors = check_module_graph(files)
    if graph_errors:
        print("JS MODULE GRAPH ERROR:\n" + "\n".join(graph_errors), file=sys.stderr)
        return 1
    node = shutil.which("node")
    if node is None:
        print(f"JS Module Graph: PASS ({len(files)} files); syntax check SKIPPED (node not found)")
        return 1 if args.strict else 0
    errors = check(files, node)
    if errors:
        print("JS SYNTAX ERROR:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    print(f"JS Syntax Check: PASS ({len(files)} files, module graph resolved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
