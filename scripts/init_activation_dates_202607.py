#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import fcntl
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import renewal_cli as cli  # noqa: E402

TARGET_YEAR_MONTH = "2026-07"
FALLBACK_DAYS = {
    ("55500678.xyz", "6c97338d-0318-48fb-8ab4-620297a5c1be"): 28,
    ("killclaude.de", "83aadb3e-11ad-4a1d-a1ee-ce7cd55cb4a3"): 28,
}


def initialize_member(member: dict[str, Any], fallback_day: int | None = None) -> dict[str, Any]:
    day = member.get("billing_day")
    changed: dict[str, Any] = {}
    if day is None and fallback_day is not None:
        day = fallback_day
        member["billing_day"] = day
        changed["billing_day"] = day
    if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31:
        raise ValueError(f"invalid billing_day for {member.get('id') or member.get('username')}: {day!r}")
    activation_date = f"{TARGET_YEAR_MONTH}-{day:02d}"
    if member.get("activation_date") != activation_date:
        member["activation_date"] = activation_date
        changed["activation_date"] = activation_date
    return changed


def member_without_targets(member: dict[str, Any], allow_billing_day: bool) -> dict[str, Any]:
    result = copy.deepcopy(member)
    result.pop("activation_date", None)
    if allow_billing_day:
        result.pop("billing_day", None)
    return result


def prepare() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, int]]:
    before: dict[str, dict[str, Any]] = {}
    after: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for domain in sorted(cli.get_domain_ids()):
        original = cli.load(domain)
        working = copy.deepcopy(original)
        before[domain] = original
        changed_count = 0
        for member in working.get("members", []):
            key = (domain, str(member.get("id") or ""))
            fallback = FALLBACK_DAYS.get(key)
            changed = initialize_member(member, fallback_day=fallback)
            if changed:
                changed_count += 1
        after[domain] = working
        counts[domain] = changed_count
    return before, after, counts


def verify(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> None:
    for domain in sorted(before):
        old_members = before[domain].get("members", [])
        new_members = after[domain].get("members", [])
        if len(old_members) != len(new_members):
            raise ValueError(f"member count changed in {domain}")
        old_by_id = {str(m.get("id")): m for m in old_members}
        new_by_id = {str(m.get("id")): m for m in new_members}
        if old_by_id.keys() != new_by_id.keys():
            raise ValueError(f"member ids changed in {domain}")
        for member_id, old in old_by_id.items():
            new = new_by_id[member_id]
            allow_day = (domain, member_id) in FALLBACK_DAYS
            if member_without_targets(old, allow_day) != member_without_targets(new, allow_day):
                raise ValueError(f"non-target member fields changed: {domain}/{member_id}")
            day = new.get("billing_day")
            expected = f"{TARGET_YEAR_MONTH}-{day:02d}"
            if new.get("activation_date") != expected:
                raise ValueError(f"activation mismatch: {domain}/{member_id}")


def apply_all(after: dict[str, dict[str, Any]]) -> None:
    with ExitStack() as stack:
        locks = []
        for domain in sorted(after):
            lock_path = cli.data_path(domain).parent / ".registry.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = stack.enter_context(lock_path.open("a+"))
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            locks.append(lock_file)
        for domain in sorted(after):
            cli.save(after[domain], domain)
        for lock_file in reversed(locks):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize all member activation dates from billing day for 2026-07")
    parser.add_argument("--apply", action="store_true", help="write changes; default is dry-run")
    args = parser.parse_args()

    before, after, counts = prepare()
    verify(before, after)
    total = sum(len(data.get("members", [])) for data in after.values())
    changed = sum(counts.values())
    print(json.dumps({"members": total, "changed": changed, "domains": counts}, ensure_ascii=False))
    if not args.apply:
        print("DRY_RUN_OK")
        return
    apply_all(after)
    persisted_before, persisted_after, _ = prepare()
    # prepare() is idempotent after write: persisted_before already satisfies all target values.
    verify(persisted_before, persisted_after)
    if any(persisted_before[d] != persisted_after[d] for d in persisted_before):
        raise SystemExit("post-write idempotency verification failed")
    print("APPLY_OK")


if __name__ == "__main__":
    main()
