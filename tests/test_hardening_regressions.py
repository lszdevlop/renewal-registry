#!/usr/bin/env python3
"""Hardening regressions; runs only against a temporary project copy."""
import concurrent.futures, json, shutil, subprocess, tempfile, time, urllib.error, urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]

def req(base, path, method="GET", payload=None):
    body = None if payload is None else json.dumps(payload).encode()
    headers = {} if body is None else {"Content-Type": "application/json"}
    r = urllib.request.Request(base + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=20) as x:
            raw = x.read().decode()
            try:
                data = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                data = {"raw": raw}
            return x.status, data
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())

def main():
    td = Path(tempfile.mkdtemp(prefix="renew-hardening-"))
    try:
        for name in ("server.py", "renewal_cli.py", "domain_catalog.py", "finance_metrics.py", "index.html"):
            shutil.copy2(SRC / name, td / name)
        (td / "server.py").write_text((td / "server.py").read_text().replace("PORT = 8765", "PORT = 18765"))
        p = subprocess.Popen(["python3", "server.py"], cwd=td, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = "http://127.0.0.1:18765"
            for _ in range(100):
                try:
                    if req(base, "/")[0] == 200: break
                except Exception: pass
                time.sleep(.05)
            n = 40
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
                rows = list(ex.map(lambda i: req(base, "/api/add-member", "POST", {"domain":"328001.xyz","username":f"c{i}","email":f"c{i}@example.com"}), range(n)))
            code, data = req(base, "/api/data?domain=328001.xyz")
            assert code == 200, (code, data)
            assert sum(str(m.get("username","")).startswith("c") for m in data["data"]["members"]) == n, rows
            assert all(code == 200 for code, _ in rows), rows
            for path in ("/server.py", "/renewal_cli.py", "/data/", "/data/domains/"):
                assert req(base, path)[0] in (403, 404), (path, req(base, path)[0])
            assert req(base, "/api/update-member", "POST", {"domain":"328001.xyz","username":"c0","price":-1})[0] == 400
            assert req(base, "/api/toggle-paid", "POST", {"domain":"328001.xyz","username":"c0","month":"2026-13","paid":True})[0] == 400
            assert req(base, "/api/toggle-paid", "POST", {"domain":"328001.xyz","username":"c0","month":"2026-07","paid":"false"})[0] == 400
            assert req(base, "/api/toggle-paid", "POST", {"domain":"328001.xyz","username":"c0","month":"2026-07","paid":True,"amount":-1})[0] == 400
            print("PASS renewal hardening")
        finally:
            p.terminate(); p.wait(timeout=5)
    finally:
        shutil.rmtree(td, ignore_errors=True)

if __name__ == "__main__": main()
