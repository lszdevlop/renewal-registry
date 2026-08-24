#!/usr/bin/env python3
"""Multi-domain HTTP isolation/performance test using only a temporary project copy."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]
DOMAINS = ["alpha.test.example", "beta.test.example"]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(base: str, path: str, method: str = "GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        base + path,
        data=body,
        method=method,
        headers={} if body is None else {"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}"), (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}"), (time.perf_counter() - started) * 1000


def main() -> None:
    td = Path(tempfile.mkdtemp(prefix="renewal-multi-domain-"))
    proc = None
    log = None
    try:
        for name in ("server.py", "renewal_cli.py", "domain_catalog.py", "finance_metrics.py", "index.html"):
            shutil.copy2(SRC / name, td / name)
        catalog = {
            "version": 1,
            "default": DOMAINS[0],
            "updated_at": "2026-01-01T00:00:00Z",
            "domains": [{"id": domain, "label": domain} for domain in DOMAINS],
        }
        (td / "data").mkdir()
        (td / "data/domain_catalog.json").write_text(json.dumps(catalog) + "\n", encoding="utf-8")
        for domain in DOMAINS:
            root = td / "data/domains" / domain
            root.mkdir(parents=True)
            (root / "members.json").write_text(
                json.dumps({"meta": {"domain": domain}, "members": []}) + "\n", encoding="utf-8"
            )
        # Sandbox manager must not point to neighboring production products/backups.
        dc = td / "domain_catalog.py"
        dc.write_text(
            dc.read_text(encoding="utf-8")
            .replace('hub_root=workspace / "claude-export-hub"', 'hub_root=renewal / "sandbox-hub"')
            .replace('backup_root=workspace / "backups/domain-management"', 'backup_root=renewal / "sandbox-backups"'),
            encoding="utf-8",
        )
        port = free_port()
        server_text = (td / "server.py").read_text(encoding="utf-8").replace("PORT = 8765", f"PORT = {port}")
        (td / "server.py").write_text(server_text, encoding="utf-8")
        log = (td / "server.log").open("w+")
        proc = subprocess.Popen([sys.executable, "server.py"], cwd=td, stdout=log, stderr=subprocess.STDOUT)
        base = f"http://127.0.0.1:{port}"
        for _ in range(200):
            try:
                if request(base, "/api/domains")[0] == 200:
                    break
            except Exception:
                pass
            time.sleep(0.05)
        else:
            raise RuntimeError("sandbox server not ready")

        code, payload, _ = request(base, "/api/domains")
        assert code == 200
        assert [row["id"] for row in payload["domains"]] == DOMAINS, payload
        username = "sandbox-probe"
        for domain in DOMAINS:
            code, added, _ = request(base, "/api/add-member", "POST", {
                "domain": domain, "username": username, "email": f"{username}@{domain}", "price": 88,
            })
            assert code == 200 and added["domain"] == domain, added
        for domain in DOMAINS:
            code, data, _ = request(base, f"/api/data?domain={domain}")
            assert code == 200
            members = data["data"]["members"]
            assert len(members) == 1 and members[0]["email"] == f"{username}@{domain}", members
        code, _, _ = request(base, "/api/data?domain=unknown.test.example")
        assert code == 400
        samples = [request(base, f"/api/data?domain={DOMAINS[i % 2]}")[2] for i in range(30)]
        assert max(samples) < 500, max(samples)
        print(f"PASS multi-domain sandbox domains={DOMAINS} port={port} max_read_ms={max(samples):.2f}")
    except Exception:
        if log is not None:
            log.flush(); log.seek(0)
            print(log.read(), file=sys.stderr)
        raise
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if log is not None:
            log.close()
        shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    main()
