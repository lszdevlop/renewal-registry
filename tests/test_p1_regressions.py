#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_cli(path: Path):
    spec = importlib.util.spec_from_file_location("renewal_cli_p1", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def member(mid: str, name: str, email: str, price: int = 100):
    return {
        "id": mid,
        "username": name,
        "email": email,
        "price": price,
        "billing_day": 1,
        "payments": [{"month": "2026-07", "paid": True, "amount": price}],
        "status": "active",
    }


def test_sync_releases_old_email_before_reuse_and_preserves_billing():
    cli = load_cli(ROOT / "renewal_cli.py")
    data = {"meta": {}, "members": [member("A", "Alice", "old@example.com")]}
    users = [
        {"id": "A", "username": "Alice", "email": "new@example.com"},
        {"id": "C", "username": "Carol", "email": "old@example.com"},
    ]
    cli.sync_members_from_users(data, users)
    assert len(data["members"]) == 2
    by_id = {m["id"]: m for m in data["members"]}
    assert by_id["A"]["email"] == "new@example.com"
    assert by_id["A"]["price"] == 100
    assert by_id["A"]["payments"][0]["amount"] == 100
    assert by_id["C"]["email"] == "old@example.com"


def test_sync_rejects_same_batch_duplicate_email_for_different_ids():
    cli = load_cli(ROOT / "renewal_cli.py")
    original = {"meta": {}, "members": []}
    data = copy.deepcopy(original)
    try:
        cli.sync_members_from_users(
            data,
            [
                {"id": "A", "username": "Alice", "email": "same@example.com"},
                {"id": "B", "username": "Bob", "email": "same@example.com"},
            ],
        )
    except ValueError as exc:
        assert "冲突" in str(exc)
    else:
        raise AssertionError("same-batch duplicate email must be rejected")
    assert data == original


def test_sync_rejects_id_email_pointing_to_different_members_atomically():
    cli = load_cli(ROOT / "renewal_cli.py")
    original = {
        "meta": {},
        "members": [
            member("A", "Alice", "alice@example.com"),
            member("B", "Bob", "bob@example.com"),
        ],
    }
    data = copy.deepcopy(original)
    try:
        cli.sync_members_from_users(
            data,
            [{"id": "A", "username": "Alice", "email": "bob@example.com"}],
        )
    except ValueError as exc:
        assert "冲突" in str(exc) or "conflict" in str(exc).lower()
    else:
        raise AssertionError("cross-identity import must be rejected")
    assert data == original, "failed sync must not partially mutate billing/payment data"


def test_cli_remove_duplicate_display_name_is_rejected_without_mutation():
    td = Path(tempfile.mkdtemp(prefix="renewal-p1-remove-"))
    try:
        shutil.copy2(ROOT / "renewal_cli.py", td / "renewal_cli.py")
        domain = td / "data/domains/328001.xyz"
        domain.mkdir(parents=True)
        payload = {
            "meta": {"domain": "328001.xyz"},
            "members": [
                member("id-1", "john", "one@example.com"),
                member("id-2", "John", "two@example.com"),
            ],
        }
        (domain / "members.json").write_text(json.dumps(payload), encoding="utf-8")
        result = subprocess.run(
            ["python3", "renewal_cli.py", "--domain", "328001.xyz", "remove", "john"],
            cwd=td,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert result.returncode != 0
        assert "多个成员" in result.stdout and "ID 或邮箱" in result.stdout
        after = json.loads((domain / "members.json").read_text(encoding="utf-8"))
        assert after == payload
    finally:
        shutil.rmtree(td, ignore_errors=True)


def test_cli_sync_identity_conflict_exits_cleanly_without_traceback():
    td = Path(tempfile.mkdtemp(prefix="renewal-p1-sync-cli-"))
    try:
        shutil.copy2(ROOT / "renewal_cli.py", td / "renewal_cli.py")
        domain = td / "data/domains/328001.xyz"
        domain.mkdir(parents=True)
        payload = {
            "meta": {"domain": "328001.xyz"},
            "members": [
                member("A", "Alice", "alice@example.com"),
                member("B", "Bob", "bob@example.com"),
            ],
        }
        (domain / "members.json").write_text(json.dumps(payload), encoding="utf-8")
        users = td / "users.json"
        users.write_text(json.dumps([{"uuid": "A", "full_name": "Alice", "email_address": "bob@example.com"}]), encoding="utf-8")
        result = subprocess.run(
            ["python3", "renewal_cli.py", "--domain", "328001.xyz", "sync", str(users)],
            cwd=td,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert result.returncode != 0
        assert "成员身份冲突" in result.stdout
        assert "Traceback" not in result.stdout
        assert json.loads((domain / "members.json").read_text()) == payload
    finally:
        shutil.rmtree(td, ignore_errors=True)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for test in tests:
        try:
            test()
            print("PASS", test.__name__)
        except Exception as exc:
            failed.append((test.__name__, repr(exc)))
            print("FAIL", test.__name__, repr(exc))
    print(f"TOTAL={len(tests)} FAIL={len(failed)}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
