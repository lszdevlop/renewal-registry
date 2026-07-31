#!/usr/bin/env python3
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_init_module():
    path = ROOT / "scripts/init_activation_dates_202607.py"
    spec = importlib.util.spec_from_file_location("activation_init", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_maps_existing_billing_day_to_july_date_without_other_changes():
    mod = load_init_module()
    member = {"id": "A", "username": "A", "billing_day": 8, "price": 100, "payments": [{"month": "2026-07"}]}
    before = copy.deepcopy(member)
    changed = mod.initialize_member(member)
    assert changed == {"activation_date": "2026-07-08"}
    assert member["activation_date"] == "2026-07-08"
    assert member["billing_day"] == 8
    assert {k: v for k, v in member.items() if k != "activation_date"} == before


def test_missing_day_uses_explicit_fallback_28():
    mod = load_init_module()
    member = {"id": "B", "username": "B", "billing_day": None, "notes": "keep"}
    changed = mod.initialize_member(member, fallback_day=28)
    assert changed == {"activation_date": "2026-07-28", "billing_day": 28}
    assert member == {"id": "B", "username": "B", "billing_day": 28, "notes": "keep", "activation_date": "2026-07-28"}


def test_invalid_day_is_rejected():
    mod = load_init_module()
    for day in (0, 32, "8", True):
        member = {"id": "X", "billing_day": day}
        try:
            mod.initialize_member(member)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid day accepted: {day!r}")


def main():
    failed = []
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        try:
            test(); print("PASS", test.__name__)
        except Exception as exc:
            failed.append((test.__name__, repr(exc))); print("FAIL", test.__name__, repr(exc))
    print(f"TOTAL={len(tests)} FAIL={len(failed)}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
