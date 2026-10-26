#!/usr/bin/env python3
"""Phone layout: member/team rows become cards, 44px targets, folded KPI/filters, no overflow.

The structural checks always run; the Chromium pass runs when MEMBER_BAN_PLAYWRIGHT_MODULE is set.
"""
import os
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
TEAMS = (ROOT / "team-costs.html").read_text(encoding="utf-8")

PROBE = r'''
const fs=require('node:fs'),assert=require('node:assert/strict');
const {chromium}=require(process.env.MEMBER_BAN_PLAYWRIGHT_MODULE);
const index=fs.readFileSync(process.env.INDEX_HTML,'utf8'),teams=fs.readFileSync(process.env.TEAMS_HTML,'utf8');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox'],...(process.env.MEMBER_BAN_CHROMIUM_EXECUTABLE?{executablePath:process.env.MEMBER_BAN_CHROMIUM_EXECUTABLE}:{})});
 try{
 const members=Array.from({length:12},(_,i)=>({id:'m'+i,username:'Member '+i,email:`m${i}@live.test`,status:'active',price:260,billing_day:(i%28)+1,notes:i%2?'note':'',payments:[]}));
 const live={meta:{domain:'live.test'},members};
 const finance={ok:true,totals:{normal_daily:1,normal_profit:2,history_profit:3,nonrenewal_loss:'0.00'},nonrenewal_enabled_at:'2026-09-12T04:48:17+00:00'};
 const teamsView={ok:true,month:'2026-11',today:'2026-10-26',usd_cny:6.8,reminder_days:7,summary:{team_count:2,active_count:2,monthly_total_usd:'620.00',monthly_total_cny:'4216.00',renewed_count:0,renewed_amount_usd:'0.00',pending_count:2,pending_amount_usd:'620.00',due_soon_count:0,due_soon_amount_usd:'0.00',member_revenue_cny:'3120.00',margin_cny:'-1096.00',margin_rate:-35.1},due_soon:[],
  domains:[{domain:'live.test',in_catalog:true,team_count:2,cost_usd:'620.00',cost_cny:'4216.00',member_revenue_cny:'3120.00',priced_members:12,margin_cny:'-1096.00',margin_rate:-35.1}],
  teams:[{id:'t1',name:'Alpha Team',domain:'live.test',monthly_cost_usd:'420.00',monthly_cost_cny:'2856.00',billing_day:4,card_label:'•••• 1111 (Bank)',notes:'',active:true,renewals:{},in_catalog:true,next_due:'2026-11-04',days_until_due:9,due_month:'2026-11',due_month_renewed:false,month_due_date:'2026-11-04',month_record:null,month_renewed:false},
         {id:'t2',name:'Beta Team',domain:'live.test',monthly_cost_usd:'200.00',monthly_cost_cny:'1360.00',billing_day:20,card_label:'',notes:'x',active:true,renewals:{},in_catalog:true,next_due:'2026-11-20',days_until_due:25,due_month:'2026-11',due_month_renewed:false,month_due_date:'2026-11-20',month_record:null,month_renewed:false}]};
 const page=await browser.newPage({viewport:{width:375,height:812},isMobile:true,hasTouch:true,deviceScaleFactor:2});
 page.setDefaultTimeout(8000);
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url());const json=b=>route.fulfill({contentType:'application/json',body:JSON.stringify(b)});
  if(url.origin!=='http://mobile.test')return route.abort();
  if(route.request().method()==='POST'&&url.pathname==='/api/toggle-paid'){const b=route.request().postDataJSON();return json({ok:true,id:b.id,username:'Member',month:b.month,paid:b.paid,payment:{month:b.month,paid:true,amount:260,note:'看板一键标记'}});}
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:index});
  if(url.pathname==='/team-costs.html')return route.fulfill({contentType:'text/html',body:teams});
  if(url.pathname==='/api/bootstrap')return json({ok:true,default:'live.test',domains:[{id:'live.test',billing_day:3,member_count:12}],data:{'live.test':live},finance});
  if(url.pathname==='/api/team-costs')return json(teamsView);
  if(url.pathname==='/api/finance-summary')return json(finance);
  if(url.pathname==='/api/domains')return json({ok:true,default:'live.test',domains:[{id:'live.test',billing_day:3}]});
  if(url.pathname==='/api/data')return json(live);
  return route.abort();
 });
 const metrics=()=>page.evaluate(()=>{
  const tr=document.querySelector('#tbody tr[data-id]');
  const controls=[...document.querySelectorAll('#tbody button, #tbody input, #tbody textarea, #tbody select')].map(e=>e.getBoundingClientRect()).filter(b=>b.width&&b.height);
  return {
   theadDisplay:getComputedStyle(document.querySelector('table.board thead')).display,
   rowDisplay:getComputedStyle(tr).display,
   rows:document.querySelectorAll('#tbody tr[data-id]').length,
   small:controls.filter(b=>b.height<44).length, controls:controls.length,
   pageOverflow:document.documentElement.scrollWidth>window.innerWidth,
   tableTop:Math.round(document.querySelector('.table-shell').getBoundingClientRect().top+scrollY),
   kpiGridDisplay:getComputedStyle(document.querySelector('.global-kpi-grid')).display,
   filterDisplay:getComputedStyle(document.getElementById('filter').closest('label')).display,
   importDisplay:getComputedStyle(document.getElementById('export-file').closest('label')).display,
   labelShown:getComputedStyle(document.querySelector('#tbody td[data-label="价格"]'),'::before').content,
   inputFont:parseFloat(getComputedStyle(document.querySelector('#tbody .edit-input')).fontSize),
  };
 });
 await page.goto('http://mobile.test/');
 await page.waitForFunction(()=>document.querySelectorAll('#tbody tr[data-id]').length===12);
 let m=await metrics();
 assert.equal(m.theadDisplay,'none','phone must hide the table header');
 assert.equal(m.rowDisplay,'grid','phone rows must render as cards');
 assert.equal(m.small,0,`${m.small}/${m.controls} controls under 44px on phone`);
 assert.equal(m.pageOverflow,false,'phone layout overflows horizontally');
 assert.equal(m.kpiGridDisplay,'none','KPI panel should start folded on phones');
 assert.equal(m.filterDisplay,'none','paid filter should be folded on phones');
 assert.equal(m.importDisplay,'none','import should be folded on phones');
 assert.equal(m.labelShown,'"价格"','card cells must show their field label');
 assert.ok(m.inputFont>=16,'inputs must be ≥16px so iOS does not zoom');
 assert.ok(m.tableTop<1.6*812,`roster starts ${m.tableTop}px down; must be reachable within ~1.5 screens`);
 // Domain strip gets its own full-width line; toggle and delete share size and baseline; the located card has no stray inset bar.
 const geo=await page.evaluate(()=>{
  const r=e=>e.getBoundingClientRect();
  const tabs=r(document.querySelector('.domain-tabs')),section=r(document.querySelector('.domain-section')),manage=r(document.querySelector('.domain-manage-btn'));
  const tr=document.querySelector('#tbody tr[data-id="m1"]');tr.classList.add('member-focused');
  const tog=r(tr.querySelector('[data-action="toggle-paid"]')),del=r(tr.querySelector('[data-action="delete-member"]'));
  const date=tr.querySelector('input[type=date]');
  return {tabsW:tabs.width,sectionW:section.width,tabsBelowManage:tabs.top>=manage.bottom-1,tog:[tog.top,tog.height,tog.width].map(Math.round),del:[del.top,del.height,del.width].map(Math.round),
   nameShadow:getComputedStyle(tr.querySelector('td')).boxShadow,dateW:Math.round(r(date).width)};
 });
 assert.ok(geo.tabsW>=geo.sectionW-2,`domain strip is ${geo.tabsW}px of ${geo.sectionW}px; it must not be squeezed beside the label`);
 assert.ok(geo.tabsBelowManage,'domain strip must sit on its own line under the manage button');
 assert.deepEqual(geo.tog,geo.del,`toggle ${geo.tog} and delete ${geo.del} must have the same top/height/width`);
 assert.equal(geo.nameShadow,'none','located card must not carry the desktop inset bar');
 assert.ok(geo.dateW>=125,`date input ${geo.dateW}px is too narrow for an Android picker`);
 await page.locator('#kpi-toggle').click();
 await page.locator('#toolbar-more').click();
 m=await metrics();
 assert.notEqual(m.kpiGridDisplay,'none','KPI toggle must expand');
 assert.notEqual(m.filterDisplay,'none','toolbar toggle must reveal the filter');
 assert.equal(await page.evaluate(()=>localStorage.getItem('renewal.kpiCollapsed')),'0','KPI choice must be remembered');
 // A paid toggle still works from a card.
 await page.locator('#tbody tr[data-id="m1"] button[data-action="toggle-paid"]').click();
 await page.waitForFunction(()=>document.querySelector('#tbody tr[data-id="m1"] .tag').textContent.includes('已缴'));
 // Desktop width is untouched.
 await page.setViewportSize({width:1280,height:900});
 await page.waitForTimeout(200);
 m=await metrics();
 assert.notEqual(m.theadDisplay,'none','desktop must keep the table header');
 assert.equal(m.rowDisplay,'table-row');
 assert.notEqual(m.filterDisplay,'none','desktop must not fold filters');

 // Team cost page on a phone.
 await page.setViewportSize({width:375,height:812});
 await page.goto('http://mobile.test/team-costs.html');
 await page.waitForFunction(()=>document.querySelectorAll('#team-body tr[data-row]').length===2);
 const t=await page.evaluate(()=>{
  const controls=[...document.querySelectorAll('#team-body button, #team-body input, #team-body select')].map(e=>e.getBoundingClientRect()).filter(b=>b.width&&b.height);
  return {theadDisplay:getComputedStyle(document.querySelector('table.teams thead')).display,rowDisplay:getComputedStyle(document.querySelector('#team-body tr[data-row]')).display,small:controls.filter(b=>b.height<44).length,controls:controls.length,pageOverflow:document.documentElement.scrollWidth>window.innerWidth,label:getComputedStyle(document.querySelector('#team-body td[data-label="账单日"]'),'::before').content};
 });
 assert.equal(t.theadDisplay,'none');assert.equal(t.rowDisplay,'grid');
 assert.equal(t.small,0,`${t.small}/${t.controls} team controls under 44px`);
 assert.equal(t.pageOverflow,false,'team page overflows on phone');
 assert.equal(t.label,'"账单日"');
 assert.deepEqual(errors,[]);
 console.log('PASS Chromium phone: member cards, 44px targets, folded KPI/filters, team cards, desktop unchanged');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
'''


class MobileLayoutTests(unittest.TestCase):
    def test_markup_carries_card_labels_and_phone_rules(self):
        for label in ("用户名", "邮箱", "开通日期", "续费日", "价格", "当月缴费", "一键切换", "最近已缴月", "累计盈利", "预计删除损失", "备注", "操作"):
            self.assertIn(f'data-label="{label}"', INDEX, label)
        self.assertIn("@media (max-width: 640px)", INDEX)
        self.assertRegex(INDEX, r"table\.board tbody tr \{\s*display: grid")
        self.assertIn('id="kpi-toggle"', INDEX)
        self.assertIn('id="toolbar-more"', INDEX)
        for label in ("团队名称", "域名", "账单日", "付款卡", "备注", "操作"):
            self.assertIn(f'data-label="{label}"', TEAMS, label)
        self.assertRegex(TEAMS, r"table\.teams tbody tr \{\s*display: grid")

    @unittest.skipUnless(os.environ.get("MEMBER_BAN_PLAYWRIGHT_MODULE"), "set MEMBER_BAN_PLAYWRIGHT_MODULE for browser sandbox")
    def test_browser_phone_layout(self):
        result = subprocess.run(["node", "-e", PROBE], capture_output=True, text=True, timeout=120,
                                env={**os.environ, "INDEX_HTML": str(ROOT / "index.html"), "TEAMS_HTML": str(ROOT / "team-costs.html")})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
