#!/usr/bin/env python3
"""UI contract after the single-stylesheet refresh; static checks plus node, no live data."""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
TEAMS = (ROOT / "team-costs.html").read_text(encoding="utf-8")
STYLE = INDEX.split("<style>", 1)[1].split("</style>", 1)[0]
SCRIPT = INDEX.split("<script>", 1)[1].rsplit("</script>", 1)[0]


def tokens(css: str) -> dict[str, str]:
    block = css.split(":root {", 1)[1].split("}", 1)[0]
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", block))


def extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    # Skip the parameter list first: it may contain a destructuring `{ ... }`.
    depth, i = 0, source.index("(", start)
    while True:
        depth += {"(": 1, ")": -1}.get(source[i], 0)
        if depth == 0:
            break
        i += 1
    depth = 0
    for i in range(source.index("{", i), len(source)):
        depth += {"{": 1, "}": -1}.get(source[i], 0)
        if depth == 0:
            return source[start:i + 1]
    raise AssertionError(f"unterminated function {name}")


class StylesheetTests(unittest.TestCase):
    def test_one_token_block_and_no_second_theme_layer(self):
        self.assertEqual(STYLE.count(":root {"), 1, "a second :root block means a second theme layer")
        for legacy in ("#2563eb", "#eff6ff", "#dc2626", "IBM Plex Mono", "background-size: 28px"):
            self.assertNotIn(legacy, STYLE, f"legacy theme value {legacy} is back")
        self.assertLess(STYLE.count("\n"), 800, "stylesheet grew past its budget; merge instead of layering")

    def test_both_pages_share_the_same_tokens(self):
        board, teams = tokens(STYLE), tokens(TEAMS.split("<style>", 1)[1])
        shared = set(board) & set(teams)
        self.assertGreaterEqual(len(shared), 20)
        self.assertEqual({k: board[k] for k in shared}, {k: teams[k] for k in shared})

    def test_phone_block_comes_last_and_restores_touch_targets(self):
        phone = STYLE.rindex("@media (max-width: 640px)")
        for earlier in ("@media (max-width: 1350px)", "@media (max-width: 920px)", "@media (max-width: 560px)"):
            self.assertLess(STYLE.index(earlier), phone, earlier)
        self.assertIn("input, select, button { min-height: 44px; }", STYLE[phone:])
        self.assertIn("input, select, textarea, button { min-height: 44px; }", TEAMS)

    def test_messages_are_fixed_toasts(self):
        self.assertRegex(STYLE, r"\.toast-stack \{\s*position: fixed;")
        self.assertRegex(INDEX, r'<div class="toast-stack">\s*<div id="err"[^>]*></div>\s*<div id="okmsg"[^>]*></div>\s*</div>')
        for name in ("showErr", "showOk"):
            self.assertNotIn("scrollIntoView", extract_function(SCRIPT, name), f"{name} must not scroll the page")
        self.assertRegex(TEAMS, r"\.msg \{[^}]*position: fixed;")

    def test_row_actions_have_one_call_to_action(self):
        quiet = re.search(r"\.pay-btn\.paid, \.pay-btn\.danger \{([^}]*)\}", STYLE)
        self.assertIsNotNone(quiet, "undo and delete must share the quiet style")
        self.assertIn("background: transparent", quiet.group(1))
        self.assertRegex(STYLE, r"\.pay-btn\.unpaid \{[^}]*var\(--accent\)")
        self.assertIn('rows="1"', extract_function(SCRIPT, "render"), "notes start as one line so rows stay compact")


class BehaviourTests(unittest.TestCase):
    def node(self, body: str) -> None:
        subprocess.run(["node", "-e", body], check=True, timeout=60)

    def test_reminder_chips_markup_and_fold(self):
        js = "\n".join([
            "const assert=require('node:assert/strict');",
            "const els={};const $=id=>els[id]||(els[id]={hidden:false,textContent:'',innerHTML:'',attrs:{},",
            " classList:{set:new Set(),toggle(n,v){v?this.set.add(n):this.set.delete(n)}},setAttribute(k,v){this.attrs[k]=v}});",
            "const esc=s=>String(s).replace(/[&<>\"']/g,c=>'&#'+c.charCodeAt(0)+';');",
            "let PHONE_QUERY={matches:false},focusedMemberKey=null,CURRENT_DOMAIN='a.test';",
            "const document={querySelectorAll:()=>[]};",
            "let alerts=[];function buildRenewalAlerts(){return alerts}",
            extract_function(SCRIPT, "syncRenewalAlertSelection"),
            SCRIPT[SCRIPT.index("const REMINDER_FOLD_PHONE"):SCRIPT.index("function syncRenewalAlertSelection")],
            extract_function(SCRIPT, "renderRenewalAlerts"),
            "const mk=(n,days)=>({username:'u'+n+'<b>',domain:'a.test',memberKey:'id:'+n,days,ym:'2026-11'});",
            "alerts=[mk(0,0),mk(1,1),mk(2,2),mk(3,3),mk(4,4)];renderRenewalAlerts([]);",
            "const html=$('renewal-alert-track').innerHTML;",
            "assert.equal((html.match(/class=\"renewal-alert-item/g)||[]).length,5);",
            "assert.equal((html.match(/renewal-alert-item is-urgent\"/g)||[]).length,2,'today and tomorrow are urgent');",
            "assert.equal((html.match(/renewal-alert-item is-soon\"/g)||[]).length,2,'2-3 days are soon');",
            "assert.match(html,/<span class=\"ra-days\">今天<\\/span>/);assert.match(html,/<span class=\"ra-days\">4 天<\\/span>/);",
            "assert.ok(!html.includes('<b>'),'usernames must be escaped');",
            "assert.equal($('renewal-alert-count').textContent,'5');assert.equal($('renewal-alert-count').hidden,false);",
            "assert.equal($('renewal-alert-more').hidden,true,'five reminders never fold on a wide screen');",
            "PHONE_QUERY.matches=true;renderRenewalAlerts([]);",
            "assert.equal($('renewal-alert-more').hidden,false);assert.equal($('renewal-alert-more').textContent,'还有 2 条');",
            "assert.ok($('renewal-alert-track').classList.set.has('folded'));",
            "PHONE_QUERY.matches=false;alerts=Array.from({length:20},(_,i)=>mk(i,5));renderRenewalAlerts([]);",
            "assert.equal($('renewal-alert-more').textContent,'还有 4 条');",
            "alerts=[];renderRenewalAlerts([]);",
            "assert.equal($('renewal-alert-count').hidden,true);assert.equal($('renewal-alert-more').hidden,true);",
        ])
        self.node(js)
        self.assertIn(".renewal-alert-track.folded .renewal-alert-item:nth-child(n+17)", STYLE)
        self.assertIn(".renewal-alert-track.folded .renewal-alert-item:nth-child(n+4)", STYLE)

    def test_page_state_reports_real_load_result(self):
        self.assertIn('id="page-state"', INDEX)
        self.assertNotIn("数据服务正常", INDEX, "the header pill must not be a fixed claim")
        js = "\n".join([
            "const assert=require('node:assert/strict');const el={className:'',textContent:''};const $=()=>el;",
            extract_function(SCRIPT, "setPageState"),
            "setPageState('ok');assert.deepEqual([el.className,el.textContent],['page-state','数据已同步']);",
            "setPageState('stale');assert.equal(el.className,'page-state is-warn');",
            "setPageState('down');assert.deepEqual([el.className,el.textContent],['page-state is-bad','数据服务不可用']);",
        ])
        self.node(js)
        load = extract_function(SCRIPT, "tryAutoLoad")
        for call in ('setPageState("ok")', 'setPageState("stale")', 'setPageState("down")'):
            self.assertIn(call, load)

    def test_protected_default_domain_follows_the_catalog(self):
        self.assertNotIn('d.id !== "lsznode.de"', SCRIPT)
        self.assertIn("DOMAINS.filter(d => d.id !== DEFAULT_DOMAIN)", SCRIPT)
        self.assertIn("if (boot.default) DEFAULT_DOMAIN = boot.default;", SCRIPT)
        self.assertIn("if (data.default) DEFAULT_DOMAIN = data.default;", SCRIPT)
        self.assertIn('id="default-domain-name"', INDEX)

    def test_filtered_roster_offers_a_way_back(self):
        self.assertIn('id="clear-filter"', INDEX)
        self.assertIn('$("clear-filter").addEventListener("click", () => applyStatQuickFilter("all"))', SCRIPT)
        self.assertIn('clearBtn.hidden = !(focusedMemberKey || statQuickFilter !== "all")', SCRIPT)


if __name__ == "__main__":
    unittest.main()
