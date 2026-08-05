#!/usr/bin/env python3
"""Regression: loaded renewal reminders must never animate completely off-screen."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")


def main() -> None:
    render_start = HTML.index("function renderRenewalAlerts")
    render_end = HTML.index("async function refreshAllRenewalAlerts", render_start)
    render = HTML[render_start:render_end]

    assert 'track.classList.toggle("scrolling"' not in render, (
        "renewal alerts must not use an off-screen marquee state"
    )
    assert ".renewal-alert-track.scrolling" not in HTML, (
        "scrolling animation creates blank periods where loaded reminders are invisible"
    )
    assert ".renewal-alert-track { display: flex;" in HTML, (
        "loaded reminders should use an always-visible flex layout"
    )
    assert "flex-wrap: wrap" in HTML, "long reminder lists should wrap instead of moving off-screen"
    print("PASS renewal reminders remain visible without marquee blank periods")


if __name__ == "__main__":
    main()
