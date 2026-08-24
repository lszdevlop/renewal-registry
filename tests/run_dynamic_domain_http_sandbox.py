#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent


def req(base: str, path: str, method: str = "GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={} if body is None else {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def wait_ready(base: str, path: str) -> None:
    last = None
    for _ in range(200):
        try:
            last = req(base, path)
            if last[0] == 200:
                return
        except Exception as exc:
            last = repr(exc)
        time.sleep(0.05)
    raise RuntimeError(f"service not ready: {base}{path}; last={last}")


def main() -> None:
    td = Path(tempfile.mkdtemp(prefix="dynamic-domain-http-"))
    renewal = td / "renewal-registry"
    hub = td / "claude-export-hub"
    backups = td / "backups"
    try:
        shutil.copytree(ROOT, renewal, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        shutil.copytree(WORKSPACE / "claude-export-hub", hub, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "uploads"))
        # Use small empty fixtures, not production stores.
        shutil.rmtree(renewal / "data", ignore_errors=True)
        shutil.rmtree(hub / "data", ignore_errors=True)
        for product in (renewal, hub):
            (product / "data/domains/lsznode.de").mkdir(parents=True)
        (renewal / "data/domains/lsznode.de/members.json").write_text(json.dumps({"meta":{"domain":"lsznode.de"},"members":[]})+"\n")
        (hub / "data/domains/lsznode.de/store.json").write_text(json.dumps({"meta":{"domain":"lsznode.de","uploads":[]},"users":{},"conversations":{},"memories":{},"projects":{},"design_chats":{},"profiles":{}})+"\n")
        (hub / "data/domains/lsznode.de/uploads").mkdir()
        # Sandbox manager must use sandbox backup root.
        dc = renewal / "domain_catalog.py"
        text = dc.read_text().replace(
            'backup_root=workspace / "backups/domain-management"',
            f'backup_root=Path({str(backups)!r})',
        )
        dc.write_text(text)
        (renewal / "server.py").write_text((renewal / "server.py").read_text().replace("PORT = 8765", "PORT = 18765"))
        (hub / "server.py").write_text((hub / "server.py").read_text().replace("PORT = 8766", "PORT = 18766"))
        renewal_log = (td / "renewal.log").open("w+")
        hub_log = (td / "hub.log").open("w+")
        procs = [
            subprocess.Popen([sys.executable, "server.py"], cwd=renewal, stdout=renewal_log, stderr=subprocess.STDOUT),
            subprocess.Popen([sys.executable, "server.py"], cwd=hub, stdout=hub_log, stderr=subprocess.STDOUT),
        ]
        try:
            rbase, hbase = "http://127.0.0.1:18765", "http://127.0.0.1:18766"
            wait_ready(rbase, "/api/domains")
            wait_ready(hbase, "/api/domains")

            code, added = req(rbase, "/api/domain-management", "POST", {"action":"add","domain":"alpha.example"})
            assert code == 200 and added["domain"] == "alpha.example", added
            for base in (rbase, hbase):
                ids = [d["id"] for d in req(base, "/api/domains")[1]["domains"]]
                assert "alpha.example" in ids, (base, ids)
            assert req(rbase, "/api/add-member", "POST", {"domain":"alpha.example","username":"probe","email":"probe@alpha.example"})[0] == 200
            code, billing = req(rbase, "/api/domain-management", "POST", {
                "action":"set_billing_day","domain":"alpha.example","billing_day":17
            })
            assert code == 200 and billing["billing_day"] == 17, billing
            rdomains = {d["id"]: d for d in req(rbase, "/api/domains")[1]["domains"]}
            assert rdomains["alpha.example"]["billing_day"] == 17, rdomains["alpha.example"]
            assert req(rbase, "/api/domain-management", "POST", {
                "action":"set_billing_day","domain":"alpha.example","billing_day":0
            })[0] == 400

            code, renamed = req(rbase, "/api/domain-management", "POST", {
                "action":"rename","domain":"alpha.example","new_domain":"beta.example","confirm_clear":True,"confirm_domain":"alpha.example"
            })
            assert code == 200, renamed
            assert req(rbase, "/api/data?domain=alpha.example")[0] == 400
            assert req(hbase, "/api/members?domain=alpha.example")[0] == 400
            assert req(rbase, "/api/data?domain=beta.example")[1]["data"]["members"] == []
            assert req(hbase, "/api/members?domain=beta.example")[1]["members"] == []
            assert Path(renamed["backup_path"]).exists()

            code, deleted = req(rbase, "/api/domain-management", "POST", {
                "action":"delete","domain":"beta.example","confirm":True,"confirm_domain":"beta.example"
            })
            assert code == 200, deleted
            for base, path in ((rbase,"/api/data?domain=beta.example"),(hbase,"/api/members?domain=beta.example")):
                assert req(base, path)[0] == 400
            assert Path(deleted["backup_path"]).exists()
            assert req(rbase, "/api/domain-management", "POST", {"action":"delete","domain":"lsznode.de","confirm":True,"confirm_domain":"lsznode.de"})[0] == 400
            print("PASS dynamic domain HTTP add -> member probe -> rename clear -> delete across 8765/8766")
        except Exception:
            for handle, name in ((renewal_log, "renewal"), (hub_log, "hub")):
                handle.flush(); handle.seek(0)
                print(f"--- {name} sandbox log ---\n{handle.read()}", file=sys.stderr)
            raise
        finally:
            for p in procs:
                p.terminate()
            for p in procs:
                try: p.wait(timeout=5)
                except subprocess.TimeoutExpired: p.kill()
            renewal_log.close(); hub_log.close()
    finally:
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    main()
