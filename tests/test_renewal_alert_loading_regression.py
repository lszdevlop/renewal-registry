#!/usr/bin/env python3
"""Regression: a stale fallback domain must not leave renewal alerts loading forever."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")


def main() -> None:
    refresh_start = HTML.index("async function refreshAllRenewalAlerts")
    refresh_end = HTML.index("\n    function render()", refresh_start)
    refresh = HTML[refresh_start:refresh_end]

    startup_start = HTML.index("(async () => {", HTML.index('$("domain-delete-submit")'))
    startup = HTML[startup_start:]

    assert "Promise.allSettled" in refresh, "single stale domain failure must not reject the whole alert refresh"
    assert "datasets.filter" in refresh or "fulfilled" in refresh, "successful domains must still render alerts"
    assert "await loadDomains({ force: true })" in startup, "startup must replace fallback domains before alert refresh"
    assert startup.index("await loadDomains({ force: true })") < startup.index("refreshAllRenewalAlerts"), (
        "domain catalog must load before renewal alerts"
    )
    print("PASS renewal alerts tolerate stale fallback domains and load after live catalog")


if __name__ == "__main__":
    main()
