#!/usr/bin/env python3
"""Smart-import routing for public/shared email addresses. No production writes."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


DOMAINS = {"lsznode.de", "peaceai.de", "01255012.xyz"}


def route(users, filename="export-batch-0000", hint="auto"):
    with patch.object(server, "get_domain_ids", return_value=set(DOMAINS)):
        return server.group_users_by_domain(users, filename=filename, domain_hint=hint)


def test_public_email_inherits_majority_team_domain():
    groups, fallback = route([
        {"email": "one@01255012.xyz"},
        {"email": "two@01255012.xyz"},
        {"email": "lytx83@gmail.com"},
    ])
    assert fallback == "01255012.xyz"
    assert [u["email"] for u in groups["01255012.xyz"]] == [
        "one@01255012.xyz", "two@01255012.xyz", "lytx83@gmail.com"
    ]


def test_admin_domain_wins_package_fallback():
    groups, fallback = route([
        {"email": "admin@01255012.xyz"},
        {"email": "one@peaceai.de"},
        {"email": "two@peaceai.de"},
        {"email": "shared@gmail.com"},
    ])
    assert fallback == "01255012.xyz"
    assert [u["email"] for u in groups["01255012.xyz"]] == [
        "admin@01255012.xyz", "shared@gmail.com"
    ]
    assert [u["email"] for u in groups["peaceai.de"]] == ["one@peaceai.de", "two@peaceai.de"]


def test_known_other_team_host_still_overrides_package_fallback():
    groups, fallback = route([
        {"email": "admin@01255012.xyz"},
        {"email": "member@peaceai.de"},
        {"email": "shared@outlook.com"},
    ])
    assert fallback == "01255012.xyz"
    assert [u["email"] for u in groups["peaceai.de"]] == ["member@peaceai.de"]
    assert [u["email"] for u in groups["01255012.xyz"]] == [
        "admin@01255012.xyz", "shared@outlook.com"
    ]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"TOTAL={len(tests)} FAIL=0")