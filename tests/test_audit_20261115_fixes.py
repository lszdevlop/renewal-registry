#!/usr/bin/env python3
"""Regressions for the 2026-11-15 audit fixes. Synthetic data only, random port.

- adding/renaming a domain never builds empty shells over an existing data directory
- membership CSV size and row count are bounded before parsing
- a date typed by keyboard is saved once, when the user leaves the field
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

import renewal_cli  # noqa: E402
from domain_catalog import DomainCatalogError, DomainCatalogManager  # noqa: E402

MODULES = ("server.py", "renewal_cli.py", "domain_catalog.py", "finance_metrics.py", "nonrenewal_loss.py", "team_costs.py")
HEADER = "Name,Email,Role,Status,Seat Tier\n"


class ExistingDomainDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="audit-domain-")
        base = Path(self.tmp.name)
        self.renewal, self.hub = base / "renewal-registry", base / "claude-export-hub"
        (self.renewal / "data").mkdir(parents=True)
        self.hub.mkdir()
        self.manager = DomainCatalogManager(
            catalog_path=self.renewal / "data/domain_catalog.json",
            renewal_root=self.renewal,
            hub_root=self.hub,
            backup_root=base / "backups",
            initial_domains=["alpha.test", "beta.test"],
            default_domain="alpha.test",
        )
        self.manager.read()

    def tearDown(self):
        self.tmp.cleanup()

    def roster_path(self, domain):
        return self.renewal / "data/domains" / domain / "members.json"

    def write_roster(self, domain):
        path = self.roster_path(domain)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"meta": {"domain": domain}, "members": [{"id": "m1", "username": "Kept"}]}))
        return path

    def test_add_refuses_a_restored_roster_and_leaves_it_untouched(self):
        path = self.write_roster("restored.test")
        before = path.read_bytes()
        with self.assertRaises(DomainCatalogError) as caught:
            self.manager.add("restored.test")
        self.assertIn("members.json", str(caught.exception))
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn("restored.test", self.manager.domain_ids())

    def test_add_refuses_leftover_hub_data(self):
        shard = self.hub / "data/domains/orphan.test/shards/users/u1.json"
        shard.parent.mkdir(parents=True)
        shard.write_text("{}")
        with self.assertRaises(DomainCatalogError):
            self.manager.add("orphan.test")
        self.assertTrue(shard.exists())
        self.assertFalse(self.roster_path("orphan.test").exists())

    def test_add_still_works_over_lock_files_and_empty_directories(self):
        renewal_dir = self.renewal / "data/domains/fresh.test"
        hub_dir = self.hub / "data/domains/fresh.test"
        renewal_dir.mkdir(parents=True)
        (renewal_dir / ".registry.lock").touch()
        (hub_dir / "uploads").mkdir(parents=True)
        (hub_dir / ".store.lock").touch()
        self.manager.add("fresh.test")
        self.assertIn("fresh.test", self.manager.domain_ids())
        self.assertEqual(json.loads(self.roster_path("fresh.test").read_text())["members"], [])

    def test_rename_refuses_a_target_that_already_holds_data(self):
        self.manager.add("old.test")
        kept = self.write_roster("taken.test")
        before = kept.read_bytes()
        with self.assertRaises(DomainCatalogError):
            self.manager.rename("old.test", "taken.test", confirm_clear=True)
        self.assertEqual(kept.read_bytes(), before)
        self.assertIn("old.test", self.manager.domain_ids())
        self.assertTrue(self.roster_path("old.test").exists())


class CsvBoundsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="audit-csv-")
        self.path = Path(self.tmp.name) / "members.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def test_oversized_file_is_refused_before_parsing(self):
        row = "Ada,ada@gmail.com,User,Active,Standard\n"
        self.path.write_text(HEADER + row * (renewal_cli.MEMBERS_CSV_MAX_BYTES // len(row) + 2))
        with self.assertRaisesRegex(ValueError, "CSV 过大"):
            renewal_cli.load_members_csv_file(self.path)

    def test_row_count_is_capped(self):
        original = renewal_cli.MEMBERS_CSV_MAX_ROWS
        renewal_cli.MEMBERS_CSV_MAX_ROWS = 3
        try:
            self.path.write_text(HEADER + ",,,,\n" * 4)
            with self.assertRaisesRegex(ValueError, "最多 3 行"):
                renewal_cli.load_members_csv_file(self.path)
        finally:
            renewal_cli.MEMBERS_CSV_MAX_ROWS = original

    def test_a_normal_roster_still_loads(self):
        self.path.write_text(HEADER + ",boss@alpha.test,Primary Owner,Active,\nAda,ada@gmail.com,User,Active,Standard\n")
        users = renewal_cli.load_members_csv_file(self.path)
        self.assertEqual([u["email"] for u in users], ["ada@gmail.com"])
        self.assertEqual(users.team_domain, "alpha.test")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="audit-http-")) / "renewal-registry"
        self.root.mkdir()
        for name in MODULES + ("index.html", "team-costs.html"):
            shutil.copy2(SOURCE / name, self.root / name)
        catalog = self.root / "data/domain_catalog.json"
        catalog.parent.mkdir()
        catalog.write_text(json.dumps({"default": "alpha.test", "domains": [
            {"id": "alpha.test", "label": "alpha.test", "billing_day": 4}]}))
        roster = self.root / "data/domains/alpha.test/members.json"
        roster.parent.mkdir(parents=True)
        roster.write_text(json.dumps({"meta": {"domain": "alpha.test"}, "members": [
            {"id": "m1", "username": "M1", "email": "m1@alpha.test", "activation_date": "2026-05-10",
             "billing_day": 1, "price": 100, "payments": []}]}))
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

    def send(self, path, data, content_type):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                         headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            payload = json.load(exc)
            exc.close()
            return exc.code, payload

    def update(self, **fields):
        return self.send("/api/update-member", json.dumps({"domain": "alpha.test", "id": "m1", **fields}).encode(),
                         "application/json")

    def stored_date(self):
        roster = json.loads((self.root / "data/domains/alpha.test/members.json").read_text())
        return roster["members"][0]["activation_date"]

    def test_oversized_csv_upload_answers_413_and_the_service_keeps_running(self):
        row = b"Ada,ada@gmail.com,User,Active,Standard\n"
        blob = HEADER.encode() + row * (renewal_cli.MEMBERS_CSV_MAX_BYTES // len(row) + 2)
        boundary = "----auditcsv"
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="big.csv"\r\n'
                f'Content-Type: text/csv\r\n\r\n').encode() + blob + f"\r\n--{boundary}--\r\n".encode()
        status, payload = self.send("/api/sync-export?domain=auto&auto_domain=1", body,
                                    f"multipart/form-data; boundary={boundary}")
        self.assertEqual(status, 413, payload)
        self.assertIn("CSV 过大", payload["error"])
        self.assertIsNone(self.proc.poll())
        self.assertEqual(self.update(notes="still alive")[0], 200)

    def test_a_half_typed_year_is_refused_by_the_server(self):
        status, payload = self.update(activation_date="0002-11-15")
        self.assertEqual(status, 400, payload)
        self.assertEqual(self.stored_date(), "2026-05-10")
        status, payload = self.update(activation_date="2026-11-15")
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.stored_date(), "2026-11-15")


class DateTypingFrontendTests(unittest.TestCase):
    """Run the page's own handlers in node against a recording stub."""

    @classmethod
    def setUpClass(cls):
        cls.html = (SOURCE / "index.html").read_text()

    def block(self, start, end):
        match = re.search(re.escape(start) + r".*?(?=" + re.escape(end) + ")", self.html, re.S)
        self.assertIsNotNone(match, f"missing block {start!r}")
        return match.group(0)

    def run_node(self, script):
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return result.stdout

    def test_typed_date_waits_for_blur_and_a_picked_date_saves_at_once(self):
        helpers = self.block("    function dateTypingKey(key) {", "    function fieldDisplay(field, value) {")
        handlers = self.block("    function removedByRender(input) {", "    function setAddPanel(open) {")
        script = """
let tableRebuilding = false;
const saved = [];
function saveMemberField(input) { saved.push(input.value); }
function findMemberLocal() { return { activation_date: "2026-05-10" }; }
function fieldDisplay(field, value) { return value == null ? "" : String(value); }
function autosizeNotes() {}
const listeners = {};
const tbody = { addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); } };
const $ = () => tbody;
""" + helpers + handlers + """
function fire(type, input, extra) {
  (listeners[type] || []).forEach(fn => fn(Object.assign({ target: input, preventDefault() {} }, extra)));
}
function makeInput(value) {
  return { type: "date", value, dataset: {}, isConnected: true, validity: { badInput: false },
    classList: { remove() {} }, matches() { return false }, closest() { return this },
    getAttribute(name) { return name === "data-field" ? "activation_date" : "m1" },
    blur() { fire("focusout", this) } };
}
function keys(input, text) { for (const key of text) fire("keydown", input, { key }); }
function expect(label, actual, wanted) {
  if (JSON.stringify(actual) !== JSON.stringify(wanted)) throw new Error(label + ": " + JSON.stringify(actual));
}

// Year/month/day order: the control reports a complete date after the first day digit.
let input = makeInput("");
keys(input, "2026111"); input.value = "2026-11-01"; fire("change", input);
expect("intermediate date must not be saved", saved, []);
keys(input, "5"); input.value = "2026-11-15"; fire("change", input);
expect("still typing", saved, []);
fire("focusout", input);
expect("saved once on leaving the field", saved, ["2026-11-15"]);
expect("flags cleared", [input.dataset.typing, input.dataset.restoredEdit], [undefined, undefined]);

// Enter commits a typed date.
saved.length = 0; input = makeInput("");
keys(input, "20261203"); input.value = "2026-12-03"; fire("change", input);
fire("keydown", input, { key: "Enter" });
expect("Enter saves the typed date once", saved, ["2026-12-03"]);

// Backspace on a stored date: the cleared value waits for blur instead of being saved.
saved.length = 0; input = makeInput("2026-05-10");
fire("keydown", input, { key: "Backspace" }); input.value = ""; fire("change", input);
expect("Backspace must not clear the stored date at once", saved, []);

// Escape drops a typed edit.
saved.length = 0; input = makeInput("2026-05-10");
keys(input, "2027"); input.value = "2027-05-10"; fire("change", input);
fire("keydown", input, { key: "Escape" });
expect("Escape discards", [saved, input.value], [[], "2026-05-10"]);

// A calendar pick has no keystroke before it and saves immediately.
saved.length = 0; input = makeInput("");
input.value = "2026-12-24"; fire("change", input);
expect("picked date saves at once", saved, ["2026-12-24"]);

// Other fields are unaffected.
saved.length = 0; input = makeInput("12"); input.type = "number";
keys(input, "15"); input.value = "15"; fire("change", input);
expect("number field saves on change", saved, ["15"]);

expect("incomplete date detected", dateEntryIncomplete({ type: "date", validity: { badInput: true } }), true);
expect("complete date", dateEntryIncomplete({ type: "date", validity: { badInput: false } }), false);
expect("detached text input", dateEntryIncomplete({ type: "text", validity: { badInput: true } }), false);
console.log("OK");
"""
        self.assertIn("OK", self.run_node(script))

    def test_save_refuses_incomplete_and_implausible_dates(self):
        save = self.block("    async function saveMemberField(input) {", "    async function postTogglePaid(payload) {")
        self.assertLess(save.index("dateEntryIncomplete(input)"), save.index("normalizeFieldValue(field, input.value)"))
        normalize = self.block("    function normalizeFieldValue(field, raw) {", "    function dateTypingKey(key) {")
        script = normalize + """
function fails(value) { try { normalizeFieldValue("activation_date", value); return false; } catch (_) { return true; } }
if (!fails("0002-11-15") || !fails("2100-01-01") || !fails("2026-02-30")) throw new Error("bad date accepted");
if (normalizeFieldValue("activation_date", "2026-11-15") !== "2026-11-15") throw new Error("good date refused");
if (normalizeFieldValue("activation_date", "") !== null) throw new Error("empty must clear");
console.log("OK");
"""
        self.assertIn("OK", self.run_node(script))
        self.assertEqual(self.html.count('type="date" min="2000-01-01" max="2099-12-31"'), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
