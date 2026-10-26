#!/usr/bin/env python3
"""/api/bootstrap, storage-error status codes, catalog read cache, finance single-flight,
billing_day/id validation. Source-only sandbox with synthetic data on a random port."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
MODULES = ("server.py", "renewal_cli.py", "domain_catalog.py", "finance_metrics.py", "nonrenewal_loss.py", "team_costs.py")


def make_sandbox(prefix):
    root = Path(tempfile.mkdtemp(prefix=prefix)) / "renewal-registry"
    root.mkdir()
    for name in MODULES + ("index.html", "team-costs.html"):
        shutil.copy2(SOURCE / name, root / name)
    catalog = root / "data/domain_catalog.json"
    catalog.parent.mkdir()
    catalog.write_text(json.dumps({"default": "alpha.test", "domains": [
        {"id": "alpha.test", "label": "alpha.test", "billing_day": 4},
        {"id": "beta.test", "label": "beta.test", "billing_day": 20}]}))
    for domain, price in (("alpha.test", 300), ("beta.test", 150)):
        p = root / f"data/domains/{domain}/members.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"meta": {"domain": domain}, "members": [
            {"id": "m1", "username": "M1", "email": f"m1@{domain}", "billing_day": 1, "price": price, "payments": []}]}))
    return root


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.root = make_sandbox("bootstrap-http-")
        self.log = (self.root / "http.log").open("a")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "-c", "import server; h=server.OptimizedThreadingHTTPServer(('127.0.0.1',0),server.Handler); print(h.server_port,flush=True); h.serve_forever()"],
            cwd=self.root, stdout=subprocess.PIPE, stderr=self.log, text=True)
        self.port = int(self.proc.stdout.readline().strip())

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.log.close()
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def call(self, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, (json.load(e) if e.code != 304 else {}), dict(e.headers)

    def test_bootstrap_bundle_and_etag(self):
        status, boot, headers = self.call("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertEqual(boot["default"], "alpha.test")
        self.assertEqual({d["id"]: d["member_count"] for d in boot["domains"]}, {"alpha.test": 1, "beta.test": 1})
        self.assertEqual(boot["data"]["beta.test"]["members"][0]["price"], 150)
        self.assertIn("totals", boot["finance"])
        self.assertIn("ETag", headers)
        self.assertEqual(self.call("/api/bootstrap", headers={"If-None-Match": headers["ETag"]})[0], 304)
        # A write changes the bundle and therefore the ETag.
        self.assertEqual(self.call("/api/toggle-paid", {"domain": "beta.test", "id": "m1", "month": "2026-11", "paid": True})[0], 200)
        status, boot2, headers2 = self.call("/api/bootstrap", headers={"If-None-Match": headers["ETag"]})
        self.assertEqual(status, 200)
        self.assertNotEqual(headers2["ETag"], headers["ETag"])
        self.assertTrue(boot2["data"]["beta.test"]["members"][0]["payments"])
        self.assertIn("preload", (SOURCE / "index.html").read_text(encoding="utf-8"))

    def test_corrupt_roster_is_a_server_error_and_does_not_hide_other_domains(self):
        (self.root / "data/domains/beta.test/members.json").write_text("{broken")
        self.assertEqual(self.call("/api/data?domain=beta.test")[0], 500)
        self.assertEqual(self.call("/api/toggle-paid", {"domain": "beta.test", "id": "m1", "month": "2026-11", "paid": True})[0], 500)
        self.assertEqual(self.call("/api/update-member", {"domain": "beta.test", "id": "m1", "notes": "x"})[0], 500)
        status, domains, _ = self.call("/api/domains")
        self.assertEqual(status, 200)
        rows = {d["id"]: d for d in domains["domains"]}
        self.assertIn("load_error", rows["beta.test"])
        self.assertEqual(rows["alpha.test"]["member_count"], 1)
        status, finance, _ = self.call("/api/finance-summary?force=1")
        self.assertEqual(status, 200)
        self.assertIn("beta.test", finance.get("load_errors", {}))
        self.assertGreater(finance["totals"]["normal_daily"], 0)   # alpha still counted
        status, boot, _ = self.call("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertIn("load_error", boot["data"]["beta.test"]["meta"])
        self.assertEqual(self.call("/api/data?domain=alpha.test")[0], 200)

    def test_validation_edge_cases(self):
        for bad in (True, 8.9, "8.9", 0, 32, "abc"):
            status, body, _ = self.call("/api/update-member", {"domain": "alpha.test", "id": "m1", "billing_day": bad})
            self.assertEqual(status, 400, (bad, body))
        for good, expect in (("8", 8), (8.0, 8), (12, 12), ("", None)):
            status, body, _ = self.call("/api/update-member", {"domain": "alpha.test", "id": "m1", "billing_day": good})
            self.assertEqual((status, body["member"]["billing_day"]), (200, expect), (good, body))
        status, body, _ = self.call("/api/toggle-paid", {"domain": "alpha.test", "id": 123, "month": "2026-11", "paid": True})
        self.assertEqual(status, 404, body)   # unknown id, not a 500


class InProcessTests(unittest.TestCase):
    def setUp(self):
        self.root = make_sandbox("bootstrap-mod-")
        spec = importlib.util.spec_from_file_location("sandbox_server_bootstrap", self.root / "server.py")
        self.server = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(self.root))
        try:
            spec.loader.exec_module(self.server)
        finally:
            sys.path.remove(str(self.root))

    def tearDown(self):
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def test_catalog_read_is_cached_by_file_version(self):
        manager = self.server.DOMAIN_MANAGER
        first = manager.read()
        self.assertIsNotNone(manager._read_cache)
        second = manager.read()
        self.assertEqual(first, second)
        self.assertIsNot(first, second, "callers must get their own copy")
        second["domains"].append({"id": "mutated.test"})
        self.assertEqual(len(manager.read()["domains"]), 2, "mutating a returned copy must not poison the cache")
        catalog = self.root / "data/domain_catalog.json"
        state = json.loads(catalog.read_text())
        state["domains"].append({"id": "gamma.test", "label": "gamma.test", "billing_day": 1})
        catalog.write_text(json.dumps(state))
        self.assertEqual([d["id"] for d in manager.read()["domains"]], ["alpha.test", "beta.test", "gamma.test"])

    def test_finance_recompute_is_single_flight_for_readers(self):
        srv = self.server
        first = srv.recompute_finance_cache(reason="source-change", skip_if_current=True)
        again = srv.recompute_finance_cache(reason="source-change", skip_if_current=True)
        self.assertIs(first, again, "a reader queued behind a fresh recompute must reuse it")
        forced = srv.recompute_finance_cache(reason="api")
        self.assertIsNot(forced, again, "explicit refreshes still recompute")
        results = []
        def worker():
            results.append(srv.get_finance_cache())
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len({id(r) for r in results}), 1, "concurrent readers must share one cached payload")


if __name__ == "__main__":
    unittest.main(verbosity=2)
