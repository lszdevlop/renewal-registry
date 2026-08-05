#!/usr/bin/env python3
"""Regression: refreshing the board must revalidate index.html immediately after UI fixes."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")


def main() -> None:
    assert 'cache_control="no-cache, must-revalidate"' in SERVER
    assert 'cache_control="public, max-age=60, must-revalidate"' not in SERVER
    print("PASS index.html revalidates on every browser refresh")


if __name__ == "__main__":
    main()
