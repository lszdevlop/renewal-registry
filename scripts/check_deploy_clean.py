#!/usr/bin/env python3
"""Fail deployment when tracked files differ from HEAD."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
        cwd=root,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        print("deploy cleanliness check failed: " + (proc.stderr.strip() or "git status failed"), file=sys.stderr)
        return 2
    dirty = [line for line in proc.stdout.splitlines() if line.strip()]
    if dirty:
        print("deploy blocked: tracked files are dirty:", file=sys.stderr)
        print("\n".join(dirty), file=sys.stderr)
        return 1
    print("deploy cleanliness check passed: tracked files match HEAD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())