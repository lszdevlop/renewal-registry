#!/usr/bin/env python3
from __future__ import annotations

import csv
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_api_accepts_and_clears_activation_date():
    server = load_module("renewal_server_activation", ROOT / "server.py")
    member = {"id": "u", "username": "U", "email": "u@example.com"}
    changed = server.update_member_fields(member, {"activation_date": "2026-07-29"})
    assert member["activation_date"] == "2026-07-29"
    assert changed == {"activation_date": "2026-07-29"}
    changed = server.update_member_fields(member, {"activation_date": ""})
    assert member["activation_date"] is None
    assert changed == {"activation_date": None}


def test_api_rejects_invalid_activation_date_without_mutation():
    server = load_module("renewal_server_activation_bad", ROOT / "server.py")
    member = {"id": "u", "activation_date": "2026-01-01"}
    for bad in ("2026-02-30", "2026-13-01", "29/07/2026", True):
        before = dict(member)
        try:
            server.update_member_fields(member, {"activation_date": bad})
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid activation date accepted: {bad!r}")
        assert member == before


def test_create_member_and_cli_csv_include_activation_date():
    server = load_module("renewal_server_create_activation", ROOT / "server.py")
    data = {"meta": {"domain": "328001.xyz"}, "members": []}
    m = server.create_member(data, {
        "username": "U", "email": "u@example.com", "activation_date": "2026-07-01"
    })
    assert m["activation_date"] == "2026-07-01"

    td = Path(tempfile.mkdtemp(prefix="activation-csv-"))
    try:
        shutil.copy2(ROOT / "renewal_cli.py", td / "renewal_cli.py")
        shutil.copy2(ROOT / "domain_catalog.py", td / "domain_catalog.py")
        cli = load_module("renewal_cli_activation", td / "renewal_cli.py")
        cli.ACTIVE_DOMAIN = "328001.xyz"
        cli.write_csv({"meta": {"domain": "328001.xyz"}, "members": [m]}, "328001.xyz")
        with (td / "data/domains/328001.xyz/members.csv").open(encoding="utf-8-sig", newline="") as f:
            row = next(csv.DictReader(f))
        assert row["activation_date"] == "2026-07-01"
    finally:
        shutil.rmtree(td, ignore_errors=True)


def extract_function(source: str, name: str) -> str:
    marker = f"function {name}"
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for i in range(brace, len(source)):
        if source[i] == "{": depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"unterminated function {name}")


def test_frontend_has_activation_column_and_seven_day_alert_algorithm():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "开通日期" in html
    assert 'type="date"' in html and 'data-field="activation_date"' in html
    assert 'id="renewal-alert-bar"' in html
    assert html.index('id="renewal-alert-bar"') < html.index('<div class="domain-tabs" id="domain-tabs"')
    assert 'track.textContent = "未来7天暂无用户续费"' in html
    assert '全部域：未来7天暂无用户续费' not in html

    assert "renderRenewalAlerts(members);" not in html, "domain table render must not recalculate global alerts"
    assert "async function refreshAllRenewalAlerts" in html
    js = "\n".join([
        extract_function(html, "isPaid"),
        extract_function(html, "nextRenewalDue"),
        extract_function(html, "daysUntilNextRenewal"),
        extract_function(html, "buildRenewalAlerts"),
        "const members = [",
        " {username:'today',billing_day:29,status:'active',domain:'lsznode.de'},",
        " {username:'week',billing_day:5,status:'active',domain:'peaceai.de'},",
        " {username:'later',billing_day:6,status:'active',domain:'peaceai.de'},",
        " {username:'inactive',billing_day:30,status:'inactive',domain:'328001.xyz'},",
        # already paid for the due month (July 29 due → ym 2026-07) must not alert
        " {username:'paid-today',billing_day:29,status:'active',domain:'lsznode.de',payments:[{month:'2026-07',paid:true}]},",
        # paid a different month still alerts
        " {username:'paid-other',billing_day:29,status:'active',domain:'lsznode.de',payments:[{month:'2026-06',paid:true}]},",
        "];",
        "const out=buildRenewalAlerts(members,new Date(2026,6,29));",
        "console.log(JSON.stringify(out));"])
    result = subprocess.run(["node", "-e", js], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert result.returncode == 0, result.stdout
    alerts = json.loads(result.stdout)
    assert [(x["username"], x["domain"], x["days"]) for x in alerts] == [
        ("paid-other", "lsznode.de", 0),
        ("today", "lsznode.de", 0),
        ("week", "peaceai.de", 7)]
    assert all(x["username"] != "paid-today" for x in alerts)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for test in tests:
        try:
            test(); print("PASS", test.__name__)
        except Exception as exc:
            failed.append((test.__name__, repr(exc))); print("FAIL", test.__name__, repr(exc))
    print(f"TOTAL={len(tests)} FAIL={len(failed)}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
