#!/usr/bin/env python3
"""The production board must not show maintenance-oriented footer help text."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")


def main() -> None:
    forbidden = [
        "看板操作：",
        "常用 CLI：",
        "python3 renewal_cli.py board",
        "sync /path/to/export.zip",
    ]
    found = [text for text in forbidden if text in HTML]
    assert not found, "footer help text still present: " + ", ".join(found)
    assert '<tbody id="tbody"></tbody>' in HTML
    assert '<script>' in HTML
    print("PASS maintenance footer help text removed while board remains intact")


if __name__ == "__main__":
    main()
