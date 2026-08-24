#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from domain_catalog import DomainCatalogError, DomainCatalogManager, normalize_domain_id


def expect_error(fn, text: str) -> None:
    try:
        fn()
    except DomainCatalogError as exc:
        assert text in str(exc), (text, str(exc))
    else:
        raise AssertionError(f"expected DomainCatalogError containing {text!r}")


def main() -> None:
    td = Path(tempfile.mkdtemp(prefix="dynamic-domains-"))
    renewal = td / "renewal-registry"
    hub = td / "claude-export-hub"
    backups = td / "backups"
    catalog = renewal / "data/domain_catalog.json"
    for root in (renewal, hub):
        (root / "data/domains").mkdir(parents=True)

    manager = DomainCatalogManager(
        catalog_path=catalog,
        renewal_root=renewal,
        hub_root=hub,
        backup_root=backups,
        initial_domains=["lsznode.de", "peaceai.de"],
        default_domain="lsznode.de",
    )

    # Seeded catalog is persistent and ordered.
    state = manager.read()
    assert state["default"] == "lsznode.de"
    assert [d["id"] for d in state["domains"]] == ["lsznode.de", "peaceai.de"]
    assert [d.get("billing_day") for d in state["domains"]] == [None, None]
    assert catalog.exists()

    # Domain-level billing day is optional, validated, and persistent.
    updated = manager.set_billing_day("lsznode.de", 12)
    assert updated["billing_day"] == 12
    assert manager.read()["domains"][0]["billing_day"] == 12
    expect_error(lambda: manager.set_billing_day("lsznode.de", 0), "1-31")
    expect_error(lambda: manager.set_billing_day("lsznode.de", 32), "1-31")
    expect_error(lambda: manager.set_billing_day("lsznode.de", "abc"), "1-31")
    expect_error(lambda: manager.set_billing_day("lsznode.de", True), "1-31")
    expect_error(lambda: manager.set_billing_day("lsznode.de", 12.5), "1-31")
    for invalid in (True, False, 12.0, 12.5, "12"):
        expect_error(lambda invalid=invalid: manager.set_billing_day("lsznode.de", invalid), "1-31")

    # Legacy rows gain billing_day=None on read while labels remain intact.
    legacy = json.loads(catalog.read_text())
    legacy["domains"][0]["label"] = "Main Team"
    legacy["domains"][0].pop("billing_day", None)
    catalog.write_text(json.dumps(legacy) + "\n")
    state = manager.read()
    assert state["domains"][0] == {"id": "lsznode.de", "label": "Main Team", "billing_day": None}
    manager.set_billing_day("lsznode.de", 12)

    # Domain validation is strict and normalizes case.
    assert normalize_domain_id(" New-Team.Example.COM ") == "new-team.example.com"
    for bad in ("", "../evil.com", "http://x.com", "a_b.com", "-bad.com", "bad-.com", "localhost"):
        expect_error(lambda bad=bad: normalize_domain_id(bad), "域名")

    # Add creates empty shells on both products.
    result = manager.add("New-Team.Example.COM")
    assert result["domain"] == "new-team.example.com"
    assert [d["id"] for d in manager.read()["domains"]] == [
        "lsznode.de", "peaceai.de", "new-team.example.com"
    ]
    rdata = json.loads((renewal / "data/domains/new-team.example.com/members.json").read_text())
    hdata = json.loads((hub / "data/domains/new-team.example.com/store.json").read_text())
    assert rdata["meta"]["domain"] == "new-team.example.com" and rdata["members"] == []
    assert hdata["meta"]["domain"] == "new-team.example.com" and hdata["users"] == {}
    assert (hub / "data/domains/new-team.example.com/uploads").is_dir()
    assert manager.read()["domains"][-1]["billing_day"] is None
    assert manager.read()["domains"][0]["billing_day"] == 12
    expect_error(lambda: manager.add("new-team.example.com"), "已存在")

    manager.set_billing_day("peaceai.de", 28)

    # Put data in old domain; rename must require explicit clear confirmation,
    # archive the old directories, and create a fresh empty target on both products.
    (renewal / "data/domains/peaceai.de/members.json").parent.mkdir(parents=True, exist_ok=True)
    (renewal / "data/domains/peaceai.de/members.json").write_text(
        json.dumps({"meta": {"domain": "peaceai.de"}, "members": [{"username": "keep-in-backup"}]})
    )
    (hub / "data/domains/peaceai.de/store.json").parent.mkdir(parents=True, exist_ok=True)
    (hub / "data/domains/peaceai.de/store.json").write_text(
        json.dumps({"meta": {"domain": "peaceai.de"}, "users": {"u": {}}})
    )
    expect_error(lambda: manager.rename("peaceai.de", "renamed.example", confirm_clear=False), "确认")
    renamed = manager.rename("peaceai.de", "renamed.example", confirm_clear=True)
    assert renamed["backup_path"]
    assert not (renewal / "data/domains/peaceai.de").exists()
    assert not (hub / "data/domains/peaceai.de").exists()
    assert json.loads((renewal / "data/domains/renamed.example/members.json").read_text())["members"] == []
    assert json.loads((hub / "data/domains/renamed.example/store.json").read_text())["users"] == {}
    renamed_domains = {row["id"]: row.get("billing_day") for row in manager.read()["domains"]}
    assert renamed_domains["lsznode.de"] == 12
    assert renamed_domains["renamed.example"] is None
    backup = Path(renamed["backup_path"])
    assert (backup / "renewal-registry/peaceai.de/members.json").exists()
    assert (backup / "claude-export-hub/peaceai.de/store.json").exists()

    # Delete requires confirmation, removes both products, and is recoverable from backup.
    expect_error(lambda: manager.delete("renamed.example", confirm=False), "确认")
    deleted = manager.delete("renamed.example", confirm=True)
    assert not (renewal / "data/domains/renamed.example").exists()
    assert not (hub / "data/domains/renamed.example").exists()
    assert Path(deleted["backup_path"]).exists()
    assert "renamed.example" not in [d["id"] for d in manager.read()["domains"]]
    assert manager.set_billing_day("lsznode.de", None)["billing_day"] is None
    assert manager.set_billing_day("lsznode.de", "")["billing_day"] is None

    # Default domain is protected from destructive catalog operations.
    expect_error(lambda: manager.rename("lsznode.de", "new-default.example", confirm_clear=True), "默认域")
    expect_error(lambda: manager.delete("lsznode.de", confirm=True), "默认域")

    print("PASS dynamic domain catalog add/rename/delete with dual-platform shells and recovery backup")


if __name__ == "__main__":
    main()
