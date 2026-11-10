#!/usr/bin/env python3
"""Regressions for the audit cleanup: stdlib multipart parser, removed legacy mirror and
alias routes, live-default-domain guards, finance cache writer, single cost table.

Runs only against temporary project copies with synthetic data.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
DEFAULT = "audit.test.example"
OTHER = "other.test.example"


def make_sandbox(tmp: Path) -> Path:
    """Project copy nested one level down, so the catalog's sibling hub/backup roots stay in tmp."""
    root = tmp / "renewal-registry"
    root.mkdir()
    for path in SOURCE.glob("*.py"):
        shutil.copy2(path, root / path.name)
    for name in ("index.html", "team-costs.html"):
        shutil.copy2(SOURCE / name, root / name)
    (root / "data/domains").mkdir(parents=True)
    (root / "data/domain_catalog.json").write_text(json.dumps({
        "version": 1,
        "default": DEFAULT,
        "domains": [{"id": d, "label": d, "billing_day": None} for d in (DEFAULT, OTHER)],
    }) + "\n")
    for domain in (DEFAULT, OTHER):
        (root / "data/domains" / domain).mkdir()
        (root / "data/domains" / domain / "members.json").write_text(
            json.dumps({"meta": {"domain": domain}, "members": []}) + "\n")
    return root


def multipart(boundary: str, parts: list[tuple[str, bytes]], *, close: bool = True) -> bytes:
    body = b"".join(
        f"--{boundary}\r\n{disposition}\r\n\r\n".encode() + blob + b"\r\n" for disposition, blob in parts)
    return body + (f"--{boundary}--\r\n".encode() if close else b"")


_TMP = tempfile.TemporaryDirectory(prefix="audit-cleanup-")
ROOT = make_sandbox(Path(_TMP.name))
sys.path.insert(0, str(ROOT))
import finance_metrics  # noqa: E402  (sandbox copies)
import nonrenewal_loss  # noqa: E402
import server  # noqa: E402
from domain_catalog import DomainCatalogError  # noqa: E402

for _module in (finance_metrics, nonrenewal_loss, server):
    # Never let these tests touch a real checkout's data directory.
    assert Path(_module.__file__).resolve().parent == ROOT.resolve(), f"{_module.__name__} not loaded from sandbox"


def tearDownModule():
    _TMP.cleanup()


class StaticTests(unittest.TestCase):
    def test_no_cgi_and_no_legacy_mirror_in_runtime_code(self):
        for name in ("server.py", "renewal_cli.py"):
            source = (SOURCE / name).read_text(encoding="utf-8")
            self.assertNotIn("import cgi", source, name)  # cgi is gone in Python 3.13
            self.assertNotIn("LEGACY_DATA_PATH", source, name)
        self.assertNotIn("data/members.json", (SOURCE / "index.html").read_text(encoding="utf-8"))

    def test_cost_table_is_identical_in_finance_and_loss_ledger(self):
        loss = nonrenewal_loss
        self.assertEqual(float(loss.FX_USD_CNY), finance_metrics.USD_CNY)
        self.assertEqual(loss.PRO_THRESHOLD, finance_metrics.PRO_THRESHOLD)
        self.assertAlmostEqual(float(loss.COST_USD["std"] * loss.FX_USD_CNY), finance_metrics.COST_STD_CNY)
        self.assertAlmostEqual(float(loss.COST_USD["pro"] * loss.FX_USD_CNY), finance_metrics.COST_PRO_CNY)


class MultipartIndexTests(unittest.TestCase):
    def setUp(self):
        self.chunk = server.UPLOAD_COPY_CHUNK_BYTES
        server.UPLOAD_COPY_CHUNK_BYTES = 7  # every delimiter straddles a chunk edge

    def tearDown(self):
        server.UPLOAD_COPY_CHUNK_BYTES = self.chunk

    def index(self, body: bytes, boundary: str = "XbX"):
        return server.index_multipart(io.BytesIO(body), boundary.encode())

    def test_fields_and_file_byte_ranges(self):
        blob = b"a,b\r\n--Xb\r\n1,2\r\n" * 40  # looks almost like a delimiter, repeatedly
        body = b"preamble\r\n" + multipart("XbX", [
            ('Content-Disposition: form-data; name="domain"', b"Other.Test.Example"),
            ('Content-Disposition: form-data; name="mark_missing"', b"1"),
            ('Content-Disposition: form-data; name="domain"', b"ignored-duplicate"),
            ('Content-Disposition: form-data; name="file"; filename="成员 表.csv"\r\nContent-Type: text/csv', blob),
            ("Content-Disposition: form-data; name=\"file\"; filename*=UTF-8''%E5%90%8D%E5%8D%95.csv", b""),
        ])
        fields, files = self.index(body)
        self.assertEqual(fields, {"domain": "Other.Test.Example", "mark_missing": "1"})
        self.assertEqual([(f[0], f[1]) for f in files], [("file", "成员 表.csv"), ("file", "名单.csv")])
        self.assertEqual(body[files[0][2]:files[0][3]], blob)
        self.assertEqual(files[1][2], files[1][3])  # empty part

    def test_truncated_or_malformed_bodies_are_rejected(self):
        part = ('Content-Disposition: form-data; name="file"; filename="a.csv"', b"x")
        for body in (
            b"no delimiter at all",
            multipart("XbX", [part], close=False)[:-2],          # cut inside the only part
            multipart("XbX", [part], close=False),                # no closing delimiter
            b"--XbX\r\nContent-Disposition: form-data; name=\"f\"\r\n",  # headers never end
        ):
            with self.assertRaises(ValueError, msg=body):
                self.index(body)
        self.assertEqual(self.index(b"--XbX--\r\n"), ({}, []))

    def test_part_count_and_field_size_are_bounded(self):
        field = ('Content-Disposition: form-data; name="domain"', b"x")
        self.index(multipart("XbX", [field] * server.MULTIPART_MAX_PARTS))
        with self.assertRaisesRegex(ValueError, "分段过多"):
            self.index(multipart("XbX", [field] * (server.MULTIPART_MAX_PARTS + 1)))
        with self.assertRaisesRegex(ValueError, "过大"):
            self.index(multipart("XbX", [(field[0], b"x" * (server.MULTIPART_FIELD_MAX_BYTES + 1))]))


class StorageTests(unittest.TestCase):
    def test_no_legacy_mirror_and_no_legacy_migration(self):
        legacy = ROOT / "data/members.json"
        legacy.write_text(json.dumps({"meta": {}, "members": [{"id": "legacy", "username": "old"}]}))
        try:
            domain_file = ROOT / "data/domains" / DEFAULT / "members.json"
            domain_file.unlink()
            server.invalidate_domain_cache(DEFAULT)
            server.ensure_domain_file(DEFAULT)
            self.assertEqual(json.loads(domain_file.read_text())["members"], [])  # not seeded from legacy
            before = legacy.read_bytes()
            data = server.load_data(DEFAULT)
            data["members"].append({"id": "m1", "username": "new", "email": f"new@{DEFAULT}"})
            server.save_data(data, DEFAULT)
            self.assertEqual(json.loads(domain_file.read_text())["members"][0]["id"], "m1")
            self.assertEqual(legacy.read_bytes(), before)  # default-domain saves no longer mirror
            self.assertEqual([p.name for p in domain_file.parent.iterdir() if p.name.endswith(".tmp")], [])
        finally:
            legacy.unlink()

    def test_live_default_domain_cannot_be_renamed(self):
        manager = server.DOMAIN_MANAGER
        self.assertNotEqual(manager.default_domain, DEFAULT)  # bootstrap seed differs from live default
        with self.assertRaisesRegex(DomainCatalogError, "默认域不可修改"):
            manager.rename(DEFAULT, "renamed.test.example", confirm_clear=True)
        self.assertEqual(manager.read()["default"], DEFAULT)
        self.assertTrue((ROOT / "data/domains" / DEFAULT).is_dir())

    def test_finance_cache_writer_leaves_only_the_cache_file(self):
        with tempfile.TemporaryDirectory(prefix="audit-fcache-") as tmp:
            path = Path(tmp) / "finance_cache.json"
            for revision in (1, 2):
                finance_metrics.write_cache(path, {"ok": True, "revision": revision})
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["finance_cache.json"])
            self.assertEqual(finance_metrics.read_cache(path)["revision"], 2)

    def test_cli_default_domain_follows_live_catalog(self):
        out = subprocess.run(
            [sys.executable, "-c",
             "import renewal_cli as c; print(c.current_default_domain(), c.normalize_domain(None), c.normalize_domain(''))"],
            cwd=ROOT, capture_output=True, text=True, timeout=30, check=True).stdout.split()
        self.assertEqual(out, [DEFAULT] * 3)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="audit-http-")
        cls.root = make_sandbox(Path(cls.tmp.name))
        cls.log = (cls.root / "http.log").open("a")
        cls.proc = subprocess.Popen(
            [sys.executable, "-u", "-c",
             "import server; h=server.OptimizedThreadingHTTPServer(('127.0.0.1',0),server.Handler); "
             "print(h.server_port,flush=True); h.serve_forever()"],
            cwd=cls.root, stdout=subprocess.PIPE, stderr=cls.log, text=True)
        cls.base = f"http://127.0.0.1:{int(cls.proc.stdout.readline().strip())}"

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls.proc.stdout.close()
        cls.log.close()
        cls.tmp.cleanup()

    def call(self, path: str, *, data: bytes | None = None, headers: dict | None = None):
        request = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            payload = json.load(exc)
            exc.close()
            return exc.code, payload

    def upload(self, parts, *, close=True, boundary="----auditForm"):
        return self.call(
            "/api/sync-export", data=multipart(boundary, parts, close=close),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def test_multipart_form_fields_route_the_import(self):
        users = [{"uuid": f"u{i}", "full_name": f"User {i}", "email_address": f"u{i}@{OTHER}"} for i in range(3)]
        status, payload = self.upload([
            ('Content-Disposition: form-data; name="domain"', OTHER.encode()),
            ('Content-Disposition: form-data; name="mark_missing"', b"0"),
            ('Content-Disposition: form-data; name="file"; filename="users.json"\r\nContent-Type: application/json',
             json.dumps(users).encode()),
        ])
        self.assertEqual((status, payload.get("ok")), (200, True), payload)
        roster = json.loads((self.root / "data/domains" / OTHER / "members.json").read_text())["members"]
        self.assertEqual(sorted(m["email"] for m in roster), sorted(u["email_address"] for u in users))
        self.assertEqual(json.loads((self.root / "data/domains" / DEFAULT / "members.json").read_text())["members"], [])
        self.assertFalse((self.root / "data/members.json").exists())
        self.assertFalse((self.root / "data/members.csv").exists())

    def test_bad_multipart_requests_are_client_errors(self):
        part = ('Content-Disposition: form-data; name="file"; filename="users.json"', b"[]")
        status, payload = self.upload([part], close=False)
        self.assertEqual(status, 400, payload)
        self.assertIn("boundary", payload["error"])
        status, payload = self.upload([('Content-Disposition: form-data; name="domain"', b"auto")])
        self.assertEqual(status, 400, payload)
        self.assertIn("file", payload["error"])
        status, payload = self.call("/api/sync-export", data=b"x", headers={"Content-Type": "multipart/form-data"})
        self.assertEqual(status, 400, payload)

    def test_removed_aliases_are_gone_and_canonical_routes_remain(self):
        for path in ("/api/members", "/api/finance-cache", "/data/members.json",
                     f"/data/members.json?domain={DEFAULT}"):
            self.assertEqual(self.call(path)[0], 404, path)
        self.assertEqual(self.call("/api/finance-summary/refresh", data=b"{}",
                                   headers={"Content-Type": "application/json"})[0], 404)
        self.assertEqual(self.call(f"/api/data?domain={DEFAULT}")[0], 200)
        self.assertEqual(self.call("/api/finance-summary?force=1")[0], 200)
        self.assertEqual(self.call("/api/finance-refresh", data=b"{}",
                                   headers={"Content-Type": "application/json"})[0], 200)


if __name__ == "__main__":
    unittest.main()
