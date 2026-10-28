#!/usr/bin/env python3
"""Offline syntax, legacy CLI contract and unittest verification (no live probe)."""
from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "official-bot"


def flags(source: str) -> set[str]:
    tree = ast.parse(source)
    return {arg.value for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            for arg in node.args if isinstance(arg, ast.Constant)
            and isinstance(arg.value, str) and arg.value.startswith("-")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--syntax-only", action="store_true")
    args = parser.parse_args()
    files = sorted(BOT.rglob("*.py")) + [Path(__file__)]
    for path in files:
        if any(part in {"knowledge", "c2c", ".venv", "__pycache__"} for part in path.parts):
            continue
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    print(f"Python syntax: {len(files)} source files parsed", flush=True)
    # This revision is the reviewed compatibility baseline, not a moving branch.
    for name in ("send_group.py", "make_sticker.py", "kb.py"):
        baseline = subprocess.run(
            ["git", "show", f"4fb912b:official-bot/{name}"], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8",
        )
        if baseline.returncode:
            print("CLI baseline unavailable; run in the complete Git checkout", file=sys.stderr)
            return 1
        missing = flags(baseline.stdout) - flags((BOT / name).read_text(encoding="utf-8"))
        if missing:
            print(f"Removed legacy flags in {name}: {sorted(missing)}", file=sys.stderr)
            return 1
    print("Legacy CLI option names: preserved", flush=True)
    if args.syntax_only:
        return 0
    with tempfile.TemporaryDirectory(prefix="qqbot-check-") as data:
        environment = dict(os.environ, QQBOT_DATA_DIR=data, PYTHONDONTWRITEBYTECODE="1",
                           PYTHONUTF8="1")
        return subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(BOT / "tests"),
             "-p", "test_*.py", "-v"], cwd=ROOT, env=environment,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
