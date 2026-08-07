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


def test_unique_unknown_admin_domain_is_detected_for_auto_creation():
    users = [
        {"email": "admin@new-team.example"},
        {"email": "shared@gmail.com"},
    ]
    assert server.infer_admin_package_domain(users) == "new-team.example"


def test_multiple_unknown_admin_domains_are_rejected():
    users = [
        {"email": "admin@one.example"},
        {"email": "admin@two.example"},
    ]
    try:
        server.infer_admin_package_domain(users)
    except ValueError as exc:
        assert "多个不同" in str(exc)
    else:
        raise AssertionError("multiple admin domains must be rejected")


def test_existing_admin_domain_is_not_recreated():
    with patch.object(server, "get_domain_ids", return_value={"known.example"}), patch.object(
        server.DOMAIN_MANAGER, "add"
    ) as add:
        assert server.ensure_admin_domain_for_auto_import([{"email": "admin@known.example"}]) is None
        add.assert_not_called()


def test_missing_admin_domain_creates_dual_platform_shells_once():
    live = {"lsznode.de"}

    def fake_add(domain):
        assert domain == "new-team.example"
        live.add(domain)
        return {"domain": domain}

    with patch.object(server, "get_domain_ids", side_effect=lambda: set(live)), patch.object(
        server.DOMAIN_MANAGER, "add", side_effect=fake_add
    ) as add:
        created = server.ensure_admin_domain_for_auto_import([
            {"email": "admin@new-team.example"},
            {"email": "member@gmail.com"},
        ])
        assert created == "new-team.example"
        add.assert_called_once_with("new-team.example")


def test_concurrent_same_domain_creation_is_idempotent():
    live = {"lsznode.de"}

    def raced_add(domain):
        live.add(domain)
        raise server.DomainCatalogError(f"域名已存在: {domain}")

    with patch.object(server, "get_domain_ids", side_effect=lambda: set(live)), patch.object(
        server.DOMAIN_MANAGER, "add", side_effect=raced_add
    ):
        assert server.ensure_admin_domain_for_auto_import([
            {"email": "admin@race.example"}
        ]) is None


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"TOTAL={len(tests)} FAIL=0")