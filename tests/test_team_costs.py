#!/usr/bin/env python3
"""Team actual-cost ledger: parsing, import, renewals, view math and HTTP API (synthetic data only)."""
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

import team_costs  # noqa: E402

DOMAINS = {"alpha.test", "beta.test"}
SHEET = """﻿,,,,,,
,Claude Team 续费与订阅管理看板 (11月),,,,,
,月度总支出,,托管团队数,,续费进度概览,
,"$6,045.00",,4 个团队,,待续: 4 笔 | 已续: 0 笔,
,,,,,,
,团队名称,所属域名,月度续费金额,每月账单日/续费日,绑定卡号,是否已续费
,Alpha Team,alpha.test,$420.00,每月 4 日,•••• 1111 (Bank A),FALSE
,Big Team,alpha.test,"$5,550.00",每月 31 日,•••• 2222 (Bank B),TRUE
,Twin Team,beta.test,$25.00,每月 20 日,•••• 3333 (Bank C),FALSE
,Twin Team,beta.test,$50.00,每月 21 日,,FALSE
,,,,,,
,合计,,"$6,045.00",-,-,-
"""


class ParsingTests(unittest.TestCase):
    def test_sheet_csv_layout(self):
        rows = team_costs.parse_sheet_csv(SHEET, DOMAINS)
        self.assertEqual([r["name"] for r in rows], ["Alpha Team", "Big Team", "Twin Team", "Twin Team"])
        self.assertEqual(rows[1]["monthly_cost_usd"], Decimal("5550.00"))
        self.assertEqual(rows[1]["billing_day"], 31)
        self.assertEqual(rows[0]["card_label"], "•••• 1111 (Bank A)")
        self.assertTrue(rows[1]["renewed"])
        self.assertFalse(rows[0]["renewed"])
        self.assertEqual(sum(r["monthly_cost_usd"] for r in rows), Decimal("6045.00"))

    def test_any_bad_row_rejects_whole_file(self):
        bad = SHEET.replace("$25.00", "abc").replace("beta.test,$50.00", "unknown.test,$50.00")
        with self.assertRaises(ValueError) as ctx:
            team_costs.parse_sheet_csv(bad, DOMAINS)
        self.assertIn("未导入任何数据", str(ctx.exception))
        self.assertIn("unknown.test", str(ctx.exception))
        with self.assertRaises(ValueError):
            team_costs.parse_sheet_csv("a,b\n1,2\n", DOMAINS)

    def test_field_validation(self):
        with self.assertRaises(ValueError):
            team_costs.parse_card_label("4111 1111 1111 1111")
        self.assertEqual(team_costs.parse_card_label("•••• 2492 (Revolut)"), "•••• 2492 (Revolut)")
        for bad in (True, 0, 32, 4.5, "每月 x 日", None):
            with self.assertRaises(ValueError):
                team_costs.parse_billing_day(bad)
        self.assertEqual(team_costs.parse_billing_day("每月 7 日"), 7)
        self.assertEqual(team_costs.parse_billing_day(7.0), 7)
        for bad in (True, "-1", "nan", "", None):
            with self.assertRaises(ValueError):
                team_costs.parse_money_usd(bad)
        self.assertEqual(team_costs.parse_money_usd("$1,234.565"), Decimal("1234.57"))

    def test_next_due_clamps_short_months_and_rolls_over(self):
        self.assertEqual(team_costs.next_due(31, date(2026, 2, 10)), date(2026, 2, 28))
        self.assertEqual(team_costs.next_due(31, date(2028, 2, 10)), date(2028, 2, 29))
        self.assertEqual(team_costs.next_due(4, date(2026, 11, 4)), date(2026, 11, 4))
        self.assertEqual(team_costs.next_due(4, date(2026, 12, 5)), date(2027, 1, 4))
        self.assertEqual(team_costs.next_due(30, date(2026, 1, 31)), date(2026, 2, 28))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="team-costs-")
        self.store = team_costs.TeamCostStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_preview_commit_idempotent_and_update(self):
        preview = self.store.import_csv(SHEET, DOMAINS, dry_run=True, month="2026-11")
        self.assertEqual(preview["summary"], {"add": 4, "update": 0, "unchanged": 0, "mark_renewed": 1})
        self.assertFalse(self.store.path.exists(), "dry run must not write")
        self.store.import_csv(SHEET, DOMAINS, dry_run=False, month="2026-11")
        doc = self.store.load()
        self.assertEqual(len(doc["teams"]), 4)
        big = next(t for t in doc["teams"] if t["name"] == "Big Team")
        self.assertEqual(big["renewals"]["2026-11"]["amount_usd"], "5550.00")
        again = self.store.import_csv(SHEET, DOMAINS, dry_run=False)
        self.assertEqual(again["summary"]["unchanged"], 4)
        self.assertEqual(len(self.store.load()["teams"]), 4)
        changed = self.store.import_csv(SHEET.replace("$420.00", "$450.00"), DOMAINS, dry_run=False)
        self.assertEqual(changed["summary"]["update"], 1)
        alpha = next(t for t in self.store.load()["teams"] if t["name"] == "Alpha Team")
        self.assertEqual(alpha["monthly_cost_usd"], "450.00")
        dup = SHEET.replace(",Twin Team,beta.test,$50.00,每月 21 日", ",Twin Team,beta.test,$50.00,每月 20 日")
        with self.assertRaises(ValueError):
            self.store.import_csv(dup, DOMAINS, dry_run=True)

    def test_crud_renewal_and_view(self):
        self.store.import_csv(SHEET, DOMAINS, dry_run=False)
        teams = {(t["name"], t["billing_day"]): t for t in self.store.load()["teams"]}
        alpha = teams[("Alpha Team", 4)]
        with self.assertRaises(ValueError):
            self.store.create({"name": "alpha team", "domain": "alpha.test", "monthly_cost_usd": 1, "billing_day": 4}, DOMAINS)
        self.store.set_renewal(alpha["id"], "2026-11", True)
        self.store.set_renewal(teams[("Twin Team", 21)]["id"], "2026-11", True, amount_usd="48")
        self.store.update(teams[("Twin Team", 20)]["id"], {"active": False}, DOMAINS)
        with self.assertRaises(team_costs.TeamNotFound):
            self.store.set_renewal("team-missing", "2026-11", True)
        with self.assertRaises(ValueError):
            self.store.set_renewal(alpha["id"], "2026-13", True)
        with self.assertRaises(ValueError):
            self.store.delete(alpha["id"], "other")

        view = team_costs.build_view(
            self.store.load(), month="2026-11", today=date(2026, 11, 1),
            catalog_domains=["alpha.test", "beta.test", "gamma.test"],
            member_revenue_cny={"alpha.test": {"revenue_cny": 50000, "priced": 40}, "beta.test": {"revenue_cny": 300, "priced": 2}},
        )
        s = view["summary"]
        self.assertEqual(s["active_count"], 3)
        self.assertEqual(s["monthly_total_usd"], "6020.00")            # inactive Twin($25) excluded
        self.assertEqual(s["monthly_total_cny"], "40936.00")           # 6020 × 6.8
        self.assertEqual((s["renewed_count"], s["renewed_amount_usd"]), (2, "468.00"))
        self.assertEqual((s["pending_count"], s["pending_amount_usd"]), (1, "5550.00"))
        # Alpha (Nov 4) is renewed for November, so only nothing else falls in 0..7 days.
        self.assertEqual(view["due_soon"], [])
        self.assertEqual(s["member_revenue_cny"], "50300.00")
        self.assertEqual(s["margin_cny"], "9364.00")
        domains = {d["domain"]: d for d in view["domains"]}
        self.assertEqual(domains["alpha.test"]["margin_cny"], str(Decimal("50000.00") - Decimal("40596.00")))
        self.assertEqual(domains["beta.test"]["margin_cny"], "-40.00")    # 300 − 50×6.8
        self.assertEqual(domains["gamma.test"]["team_count"], 0)
        big = next(t for t in view["teams"] if t["name"] == "Big Team")
        self.assertEqual((big["next_due"], big["month_due_date"]), ("2026-11-30", "2026-11-30"))

        alpha_view = team_costs.build_view(self.store.load(), month="2026-11", today=date(2026, 11, 28), catalog_domains=list(DOMAINS))
        self.assertEqual([x["name"] for x in alpha_view["due_soon"]], ["Big Team", "Alpha Team"])  # Nov 30, then Dec 4 unpaid
        self.store.set_renewal(alpha["id"], "2026-11", False)
        self.assertNotIn("2026-11", next(t for t in self.store.load()["teams"] if t["id"] == alpha["id"])["renewals"])
        removed = self.store.delete(alpha["id"], alpha["id"])
        self.assertEqual(removed["name"], "Alpha Team")
        self.assertEqual(len(self.store.load()["teams"]), 3)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="team-costs-http-")
        self.root = Path(self.tmp.name) / "renewal-registry"
        self.root.mkdir()
        for path in SOURCE.glob("*.py"):
            shutil.copy2(path, self.root / path.name)
        for name in ("index.html", "team-costs.html"):
            shutil.copy2(SOURCE / name, self.root / name)
        catalog = self.root / "data/domain_catalog.json"
        catalog.parent.mkdir()
        catalog.write_text(json.dumps({"default": "alpha.test", "domains": [
            {"id": "alpha.test", "label": "alpha.test", "billing_day": 4},
            {"id": "beta.test", "label": "beta.test", "billing_day": 20}]}))
        for domain, price in (("alpha.test", 300), ("beta.test", 150)):
            p = self.root / f"data/domains/{domain}/members.json"
            p.parent.mkdir(parents=True)
            p.write_text(json.dumps({"meta": {"domain": domain}, "members": [
                {"id": "m1", "username": "M1", "email": f"m1@{domain}", "billing_day": 1, "price": price, "payments": []},
                {"id": "m2", "username": "M2", "email": f"m2@{domain}", "billing_day": 1, "price": 999, "status": "inactive", "payments": []}]}))
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
        self.tmp.cleanup()

    def call(self, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                ctype = r.headers.get("Content-Type", "")
                raw = r.read()
                return r.status, (json.loads(raw) if "json" in ctype else raw.decode())
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_api_flow_and_pages(self):
        status, page = self.call("/team-costs.html")
        self.assertEqual(status, 200)
        self.assertIn("Team 实际成本", page)
        status, index = self.call("/")
        self.assertIn('href="/team-costs.html"', index)

        status, empty = self.call("/api/team-costs?month=2026-11")
        self.assertEqual(status, 200)
        self.assertEqual(empty["summary"]["team_count"], 0)
        self.assertEqual(empty["summary"]["member_revenue_cny"], "450.00")   # inactive member excluded

        status, preview = self.call("/api/team-costs/import", {"csv": SHEET, "month": "2026-11", "dry_run": True})
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["summary"]["add"], 4)
        self.assertEqual(self.call("/api/team-costs?month=2026-11")[1]["summary"]["team_count"], 0)
        status, done = self.call("/api/team-costs/import", {"csv": SHEET, "month": "2026-11", "dry_run": False})
        self.assertEqual(status, 200, done)

        status, view = self.call("/api/team-costs?month=2026-11")
        self.assertEqual(view["summary"]["monthly_total_usd"], "6045.00")
        self.assertEqual(view["summary"]["renewed_count"], 1)
        alpha = next(t for t in view["teams"] if t["name"] == "Alpha Team")

        self.assertEqual(self.call("/api/team-costs/renewal", {"id": alpha["id"], "month": "2026-11", "renewed": True})[0], 200)
        self.assertEqual(self.call("/api/team-costs/renewal", {"id": alpha["id"], "month": "2026-11", "renewed": "yes"})[0], 400)
        self.assertEqual(self.call("/api/team-costs/renewal", {"id": "team-nope", "month": "2026-11", "renewed": True})[0], 404)
        status, upd = self.call("/api/team-costs/update", {"id": alpha["id"], "card_label": "4111111111111111"})
        self.assertEqual(status, 400)
        self.assertIn("完整卡号", upd["error"])
        status, upd = self.call("/api/team-costs/update", {"id": alpha["id"], "notes": "季度结算"})
        self.assertEqual((status, upd["team"]["notes"]), (200, "季度结算"))
        status, added = self.call("/api/team-costs/add", {"name": "New Team", "domain": "beta.test", "monthly_cost_usd": "75", "billing_day": 9})
        self.assertEqual(status, 200, added)
        self.assertEqual(self.call("/api/team-costs/add", {"name": "X", "domain": "nope.test", "monthly_cost_usd": "1", "billing_day": 9})[0], 400)
        self.assertEqual(self.call("/api/team-costs/delete", {"id": added["team"]["id"], "confirm_id": "wrong"})[0], 400)
        self.assertEqual(self.call("/api/team-costs/delete", {"id": added["team"]["id"], "confirm_id": added["team"]["id"]})[0], 200)
        self.assertEqual(self.call("/api/team-costs/unknown", {})[0], 404)
        self.assertEqual(self.call("/api/team-costs?month=2026-13")[0], 400)

        final = self.call("/api/team-costs?month=2026-11")[1]
        self.assertEqual(final["summary"]["renewed_count"], 2)
        self.assertEqual(len(final["teams"]), 4)

        # The browser flow runs against this same sandbox server when Playwright is available.
        if os.environ.get("MEMBER_BAN_PLAYWRIGHT_MODULE"):
            self.run_browser_inline_edit_and_domain_filter()

        # A corrupt ledger is a server-side storage error, not a bad request.
        (self.root / "data/team_costs.json").write_text("{broken")
        self.assertEqual(self.call("/api/team-costs?month=2026-11")[0], 500)
        self.assertEqual(self.call("/api/team-costs/renewal", {"id": alpha["id"], "month": "2026-11", "renewed": False})[0], 500)


BROWSER_PROBE = r'''
const assert = require("node:assert/strict");
const { chromium } = require(process.env.MEMBER_BAN_PLAYWRIGHT_MODULE);
const BASE = process.env.TEAM_COSTS_BASE;
(async () => {
  const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"],
    ...(process.env.MEMBER_BAN_CHROMIUM_EXECUTABLE ? { executablePath: process.env.MEMBER_BAN_CHROMIUM_EXECUTABLE } : {}) });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(8000);
    const errors = [], updates = [];
    page.on("pageerror", e => errors.push(e.message));
    // Slow every in-place save down so edits overlap with the reload it triggers.
    await page.route("**/api/team-costs/update", async route => {
      updates.push(route.request().postDataJSON());
      await new Promise(r => setTimeout(r, 400));
      await route.continue();
    });
    const api = async path => (await page.request.get(BASE + path)).json();
    const team = async name => (await api("/api/team-costs?month=2026-11")).teams.find(t => t.name === name);
    const rows = () => page.locator("#team-body tr[data-row]");
    const cell = (name, field) => page.locator(`#team-body tr[data-row]:has(input[data-field="name"][value="${name}"]) [data-field="${field}"]`);
    const settle = () => page.waitForFunction(() => !document.querySelector("#team-body .saving"));

    await page.goto(BASE + "/team-costs.html");
    await page.waitForFunction(() => document.querySelectorAll("#team-body tr[data-row]").length === 4);

    // 1. Domain filter: comparison row toggles, chips switch, URL hash persists.
    await page.locator('#domain-body tr[data-domain="beta.test"]').click();
    assert.equal(await rows().count(), 2, "domain row click did not filter");
    assert.ok(page.url().endsWith("#domain=beta.test"));
    assert.equal(await page.locator("#domain-chips .chip.active").getAttribute("data-domain"), "beta.test");
    assert.match(await page.locator('#domain-body tr[data-domain="beta.test"]').getAttribute("class"), /selected/);
    await page.locator('#domain-body tr[data-domain="beta.test"]').click();
    assert.equal(await rows().count(), 4, "second click did not clear the filter");
    await page.locator('#domain-chips .chip[data-domain="alpha.test"]').click();
    assert.equal(await rows().count(), 2);
    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll("#team-body tr[data-row]").length === 2);
    assert.equal(await page.locator("#f-domain").inputValue(), "alpha.test", "hash filter not restored on reload");
    await page.locator('#domain-chips .chip[data-domain=""]').click();
    assert.equal(await rows().count(), 4);

    // 2. Edit in place; leaving the cell saves exactly once.
    await cell("Alpha Team", "name").click();
    await page.keyboard.press("Control+A");
    await page.keyboard.type("Alpha Renamed");
    await page.keyboard.press("Tab");
    await settle();
    assert.equal(updates.length, 1);
    assert.deepEqual(updates[0], { id: (await team("Alpha Renamed")).id, name: "Alpha Renamed" });

    // 3. Typing elsewhere while a save is in flight survives the reload and is saved once.
    await cell("Alpha Renamed", "monthly_cost_usd").fill("450");
    await cell("Big Team", "notes").click();           // blur -> slow save of the cost starts
    await page.keyboard.type("abc");
    await settle();
    assert.equal(await cell("Big Team", "notes").inputValue(), "abc", "reload dropped typed notes");
    assert.equal(await page.evaluate(() => document.activeElement?.dataset.field), "notes", "focus lost on reload");
    await page.keyboard.type("XYZ");
    await page.locator("#f-q").click();
    await settle();
    assert.equal((await team("Alpha Renamed")).monthly_cost_usd, "450.00");
    assert.equal((await team("Big Team")).notes, "abcXYZ");
    assert.equal(updates.filter(u => "notes" in u).length, 1, "notes saved more than once");

    // 4. Invalid input is rejected client-side and reverted; nothing is sent.
    const before = updates.length;
    await cell("Alpha Renamed", "billing_day").fill("40");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.getElementById("msg").classList.contains("err"));
    assert.equal(await cell("Alpha Renamed", "billing_day").inputValue(), "4");
    // 5. Escape restores the stored value without saving.
    await cell("Alpha Renamed", "card_label").click();
    await page.keyboard.type(" changed");
    await page.keyboard.press("Escape");
    assert.equal(await cell("Alpha Renamed", "card_label").inputValue(), "•••• 1111 (Bank A)");
    assert.equal(updates.length, before, "invalid or escaped edits were sent");

    // 6. Changing the domain select saves immediately and moves the team.
    const twin = page.locator('#team-body tr[data-row]:has(input[data-field="monthly_cost_usd"][value="25.00"]) select[data-field="domain"]');
    await twin.selectOption("alpha.test");
    await settle();
    const moved = (await api("/api/team-costs?month=2026-11")).teams.find(t => t.name === "Twin Team" && t.billing_day === 20);
    assert.equal(moved.domain, "alpha.test");
    assert.equal(await page.locator('#domain-chips .chip[data-domain="alpha.test"] .count').innerText(), "3");

    assert.deepEqual(errors, []);
    console.log("PASS Chromium: domain filter (row/chip/hash), in-place edit autosave, overlap-safe reload, validation, escape, domain move");
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
'''


def _browser_check(self):
    result = subprocess.run(["node", "-e", BROWSER_PROBE], capture_output=True, text=True, timeout=120,
                            env={**os.environ, "TEAM_COSTS_BASE": f"http://127.0.0.1:{self.port}"})
    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
    print(result.stdout.strip())


HttpTests.run_browser_inline_edit_and_domain_filter = _browser_check


if __name__ == "__main__":
    unittest.main(verbosity=2)
