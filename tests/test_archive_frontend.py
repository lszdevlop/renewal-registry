#!/usr/bin/env python3
"""Run the real inline JS against synthetic archived domains; never use live APIs."""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

HTML = (Path(__file__).resolve().parents[1] / 'index.html').read_text()
SCRIPT_MATCH = re.search(r'<script>(.*?)</script>', HTML, re.S)
assert SCRIPT_MATCH, 'missing inline frontend script'
SCRIPT = SCRIPT_MATCH.group(1)
# Do not run the startup network requests. All function bodies/event handlers stay real.
SCRIPT = SCRIPT.rsplit('    (async () => {', 1)[0]
HARNESS = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: id === 'filter' ? 'all' : '', textContent: '', innerHTML: '',
    disabled: false, hidden: false, style: {}, dataset: {}, attrs: {}, listeners: {},
    classList: {add(){}, remove(){}, toggle(){}},
    addEventListener(type, fn){this.listeners[type]=fn},
    getAttribute(k){return this.attrs[k] ?? null}, setAttribute(k,v){this.attrs[k]=v},
    querySelectorAll(){return []}, focus(){}, scrollIntoView(){},
  });
  return elements.get(id);
}
const context = vm.createContext({assert, console, Intl, URL, CSS:{escape:x=>x},
  localStorage:{getItem:()=>null,setItem(){}},
  document:{getElementById:element,querySelectorAll:()=>[],querySelector:()=>null},
  window:{location:{origin:'http://archive.test'},confirm:()=>true},
  setTimeout:()=>0,clearTimeout(){},requestIdleCallback(){},
  fetch:async()=>{throw Error('unexpected network request')},
});
'''
FIXTURE = r'''
DOMAINS = [
 {id:'frozen.test', label:'Frozen', archived:true, archived_at:'2026-10-02T08:00:00+08:00',
  archive_reason:'封号', billing_day:3, archive_summary:{normal_daily:1.23,normal_profit:45.67,history_profit:890.12,member_n:1,as_of_date:'2026-10-02'}},
 {id:'live.test',label:'Live',billing_day:3},
];
const frozenMember = {id:'f1',username:'Frozen Member',email:'frozen@example.test',
 status:'active',price:300,billing_day:3,activation_date:'2026-01-01',notes:'retained',payments:[]};
DOMAIN_CACHE['frozen.test']={meta:{archive_snapshot:{archived_at:'2026-10-02T08:00:00+08:00',reason:'封号',
 totals:{normal_daily:1.23,normal_profit:45.67,history_profit:890.12},
 members:{'id:f1':{normal_profit:45.67,history_profit:890.12}}}},members:[frozenMember]};
DOMAIN_CACHE['live.test']={meta:{},members:[{...frozenMember,id:'l1',username:'Live Member'}]};
CURRENT_DOMAIN='frozen.test'; DATA=DOMAIN_CACHE[CURRENT_DOMAIN];
renderGlobalKpi=()=>{};
'''


def run_js(body):
    code = HARNESS + '\nvm.runInContext(' + json.dumps(SCRIPT) + ',context);\n'
    code += 'vm.runInContext(' + json.dumps(FIXTURE + '\n(async()=>{\n' + body + '\n})()')
    code += ',context).catch(e=>{console.error(e);process.exitCode=1});\n'
    result = subprocess.run(['node', '-e', code], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)


class ArchiveFrontendTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('ARCHIVE_PLAYWRIGHT_MODULE'), 'set ARCHIVE_PLAYWRIGHT_MODULE for browser sandbox')
    def test_browser_archive_notes_navigation_search_mobile(self):
        browser_probe = r'''
const fs=require('node:fs'),assert=require('node:assert/strict');
const {chromium}=require(process.env.ARCHIVE_PLAYWRIGHT_MODULE);
const html=fs.readFileSync(0,'utf8');
(async()=>{
 const browser=await chromium.launch({headless:true,
   ...(process.env.ARCHIVE_CHROMIUM_EXECUTABLE?{executablePath:process.env.ARCHIVE_CHROMIUM_EXECUTABLE}:{}),args:['--no-sandbox']});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[],writes=[],reads=[];
  page.on('pageerror',e=>errors.push(e.message));
  const member={id:'f1',username:'Frozen Member',email:'frozen@example.test',billing_day:new Date().getDate(),price:300,status:'active',notes:'retained',payments:[]};
  const frozen={meta:{archive_snapshot:{archived_at:'2026-10-02T08:00:00+08:00',reason:'封号',totals:{normal_daily:1.23,normal_profit:45.67,history_profit:890.12},members:{'id:f1':{normal_profit:45.67,history_profit:890.12}}}},members:[member]};
  const live={meta:{},members:[{...member,id:'l1',username:'Live Member',email:'live@example.test'}]};
  const catalog={ok:true,default:'frozen.test',domains:[{id:'frozen.test',archived:true,archive_reason:'封号',billing_day:3,member_count:1},{id:'live.test',billing_day:3,member_count:1}]};
  await page.route('**/*',async route=>{
   const req=route.request(),url=new URL(req.url());
   const json=body=>route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
   if(url.origin!=='http://archive.test')return route.abort();
   if(req.method()!=='GET'){
    const body=req.postDataJSON();writes.push({path:url.pathname,body});
    assert.equal(url.pathname,'/api/update-member');assert.deepEqual(Object.keys(body).sort(),['domain','id','notes']);
    assert.equal(body.domain,'frozen.test');member.notes=body.notes;
    return json({ok:true,member:{...member}});
   }
   reads.push(url.pathname+url.search);
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
   if(url.pathname==='/api/domains')return json(catalog);
   if(url.pathname==='/api/data')return json(url.searchParams.get('domain')==='frozen.test'?frozen:live);
   if(url.pathname==='/api/finance-summary')return json({ok:true,totals:{normal_daily:2,normal_profit:3,history_profit:4,nonrenewal_loss:0}});
   return route.abort();
  });
  await page.goto('http://archive.test/');
  await page.waitForFunction(()=>document.querySelector('#tbody').textContent.includes('Frozen Member'));
  await page.waitForFunction(()=>document.querySelector('#renewal-alert-track').textContent.includes('Live Member'));
  assert.equal(await page.locator('#archive-banner').isVisible(),true);
  assert.ok((await page.locator('#archive-banner').innerText()).includes('不计入顶部'));
  assert.equal(await page.locator('#kpi-normal-profit').innerText(),'¥3.00');
  assert.ok(!(await page.locator('#renewal-alert-track').innerText()).includes('Frozen Member'));
  for(const selector of ['[data-field="activation_date"]','[data-field="billing_day"]','[data-field="price"]','[data-action="toggle-paid"]','[data-action="delete-member"]','#toggle-add','#add-submit','select[data-domain="frozen.test"]'])
    assert.equal(await page.locator(selector).isDisabled(),true,selector);
  assert.equal(await page.locator('#export-file').isEnabled(),true);
  const note=page.locator('textarea[data-field="notes"]');
  assert.equal(await note.isEnabled(),true);
  await note.fill('sandbox note');await note.blur();
  await page.waitForFunction(()=>document.querySelector('textarea[data-field="notes"]').value==='sandbox note'&&!document.querySelector('textarea[data-field="notes"]').disabled);
  assert.equal(writes.length,1);assert.equal(member.notes,'sandbox note');
  assert.ok((await page.locator('#tbody').innerText()).includes('¥45.67'));
  assert.ok((await page.locator('#tbody').innerText()).includes('— 已封存'));
  const countBefore=reads.length;
  await page.locator('button.domain-tab[data-domain="live.test"]').click();
  await page.waitForFunction(()=>document.querySelector('#tbody').textContent.includes('Live Member'));
  assert.equal(await page.locator('#archive-banner').isVisible(),false);
  assert.equal(await page.locator('#toggle-add').isEnabled(),true);
  await page.locator('#q').fill('Frozen Member');
  await page.waitForFunction(()=>document.querySelector('#tbody').textContent.includes('Frozen Member'));
  assert.equal(reads.length,countBefore,'cached tab switch/search made a network request');
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.locator('#archive-banner').isVisible(),true);
  const box=await page.locator('#archive-banner').boundingBox();assert.ok(box.x>=0&&box.x+box.width<=390);
  assert.ok((await page.locator('.domain-tab-shell.is-archived').innerText()).includes('已封存 · 封号'));
  assert.equal(await page.locator('body').evaluate(e=>getComputedStyle(e).backgroundColor),'rgb(244, 246, 242)','preserve existing light theme');
  assert.deepEqual(errors,[]);
  console.log('PASS browser sandbox: locked controls, note-only write, frozen profit, reminders, cache navigation, search, mobile white theme');
 } finally {await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', browser_probe], input=HTML, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())

    def test_snapshot_stable_keys_null_negative_and_future_dates(self):
        run_js(r'''
const samples=[
 {id:'by-id',username:'same',email:'same@example.test'},
 {username:'same',email:' Mixed@Example.Test '},
 {username:' OnlyName '},
];
const keys=['id:by-id','email:mixed@example.test','username:OnlyName'];
DATA.members=samples;
DATA.meta.archive_snapshot.members=Object.fromEntries(keys.map((k,i)=>[k,{normal_profit:[0,-12.34,null][i]}]));
const frozen=JSON.stringify(DATA.meta.archive_snapshot);
for(const when of ['2026-10-03','2027-01-01','2030-12-31']) {
 makeTodayCtx=()=>({date:when});
 computeMemberMetrics=()=>{throw Error('frozen metrics recalculated')};
 render();
 assert.match($('tbody').innerHTML,/¥0\.00/);
 assert.match($('tbody').innerHTML,/-¥12\.34/);
 assert.ok(!$('tbody').innerHTML.includes('NaN'));
 assert.equal(JSON.stringify(DATA.meta.archive_snapshot),frozen);
}
DATA.meta.archive_snapshot.reason='<img src=x onerror=alert(1)>';
DATA.meta.archive_snapshot.archived_at='<script>bad</script>';
renderArchiveBanner();
assert.ok(!$('archive-banner').innerHTML.includes('<img'));
assert.ok(!$('archive-banner').innerHTML.includes('<script>'));
''')

    def test_render_frozen_rows_and_archive_banner_without_live_calculation(self):
        run_js(r'''
computeMemberMetrics=()=>{throw Error('archived row recalculated live profit')};
computeNonrenewalLoss=()=>{throw Error('archived row calculated deletion loss')};
render();
assert.match($('tbody').innerHTML,/¥45\.67/,'snapshot member profit missing');
assert.match($('tbody').innerHTML,/— 已封存/,'archived deletion preview');
assert.equal($('archive-banner').hidden,false);
for (const text of ['封号','2026-10-02T08:00:00+08:00','¥1.23','¥45.67','¥890.12','不计入顶部'])
 assert.ok($('archive-banner').innerHTML.includes(text),'banner missing '+text);
// Null/missing snapshot money is unavailable, never zero or a live fallback.
delete DATA.meta.archive_snapshot.members['id:f1'];
render();
assert.ok(!$('tbody').innerHTML.includes('¥45.67'));
// The catalog alone must lock/freeze a cached roster before its snapshot arrives.
delete DATA.meta.archive_snapshot;
render();
assert.match($('tbody').innerHTML,/已封存/);
assert.ok(!$('tbody').innerHTML.includes('¥45.67'));
''')

    def test_archive_controls_locked_but_notes_search_and_import_available(self):
        run_js(r'''
renderDomainTabs(); render();
assert.match($('domain-tabs').innerHTML,/已封存 · 封号/);
assert.match($('domain-tabs').innerHTML,/domain-tab-shell[^\"]*is-archived/);
const tab=$('domain-tabs').innerHTML.match(/<button[^>]*data-domain="frozen.test"[^>]*>/)[0];
assert.ok(!tab.includes('disabled'),'archived tab remains clickable');
const selector=$('domain-tabs').innerHTML.match(/<select[^>]*data-domain="frozen.test"[^>]*>/)[0];
assert.match(selector,/disabled/);
for (const field of ['activation_date','billing_day','price']) {
 const input=$('tbody').innerHTML.match(new RegExp('<input[^>]*data-field="'+field+'"[^>]*>'))[0];
 assert.match(input,/disabled/,field+' remains editable');
}
for (const action of ['toggle-paid','delete-member']) {
 const button=$('tbody').innerHTML.match(new RegExp('<button[^>]*data-action="'+action+'"[^>]*>'))[0];
 assert.match(button,/disabled/,action+' remains enabled');
}
assert.ok(!$('tbody').innerHTML.match(/<textarea[^>]*>/)[0].includes('disabled'),'notes editable');
assert.equal($('toggle-add').disabled,true);
assert.equal($('add-submit').disabled,true);
assert.equal($('export-file').disabled,false,'global auto import usable');
assert.ok(!$('domain-rename-old').innerHTML.includes('frozen.test'));
assert.ok(!$('domain-delete-name').innerHTML.includes('frozen.test'));
CURRENT_DOMAIN='live.test'; DATA=DOMAIN_CACHE[CURRENT_DOMAIN];
render();
assert.equal($('toggle-add').disabled,false);
assert.equal($('add-submit').disabled,false);
assert.equal($('archive-banner').hidden,true);
assert.ok(!$('tbody').innerHTML.includes('已封存'));
assert.equal(pickBestDomainForQuery('frozen member','all','2026-10'),'frozen.test','archived members searchable');
await switchDomain('frozen.test');
assert.equal(CURRENT_DOMAIN,'frozen.test');
assert.match($('tbody').innerHTML,/¥45\.67/);
''')

    def test_mutation_handlers_block_archive_except_notes(self):
        run_js(r'''
const writes=[];
fetch=async(url,options)=>{writes.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>({ok:true})}};
for(const field of ['activation_date','billing_day','price']) {
 const input=$('fake-'+field); input.attrs={'data-id':'f1','data-field':field}; input.value=field==='activation_date'?'2026-10-03':'5';
 await saveMemberField(input);
}
await togglePaid('f1',false);
await deleteMember('f1','Frozen Member','frozen.test');
$('add-username').value='New';$('add-email').value='new@example.test';
await addMember();
setAddPanel(true);
assert.equal($('add-panel').style.display,'none','archive add panel opened');
for(const action of ['rename','delete','set_billing_day']) {
 try {await manageDomain({action,domain:'frozen.test',new_domain:'new.test',billing_day:5})}catch(e){}
}
const select=$('fake-select');select.attrs={'data-domain':'frozen.test'};select.value='5';select.closest=()=>select;
await $('domain-tabs').listeners.change({target:select});
assert.equal(writes.length,0,'archived write handlers sent a mutation');
const notes=$('fake-notes');notes.attrs={'data-id':'f1','data-field':'notes'};notes.value='new note';
await saveMemberField(notes);
assert.equal(writes.length,1);
assert.equal(writes[0].url,'/api/update-member');
assert.equal(JSON.stringify(writes[0].body),JSON.stringify({domain:'frozen.test',id:'f1',notes:'new note'}));
assert.equal(frozenMember.notes,'new note');
assert.equal(DATA.meta.archive_snapshot.members['id:f1'].normal_profit,45.67);
''')

    def test_catalog_refresh_immediately_freezes_cached_rows_and_reminders(self):
        run_js(r'''
const archivedRow={...DOMAINS[0]};
DOMAINS[0]={id:'frozen.test',billing_day:3};
delete DATA.meta.archive_snapshot;
render();
fetch=async()=>({ok:true,json:async()=>({ok:true,domains:[archivedRow,DOMAINS[1]],default:'live.test'})});
await loadDomains({force:true});
assert.equal($('toggle-add').disabled,true,'catalog refresh left stale writable table');
assert.match($('tbody').innerHTML,/— 已封存/);
assert.ok(!$('renewal-alert-track').innerHTML.includes('Frozen Member'));
''')

    def test_snapshot_arrival_updates_badge_and_removes_old_reminders(self):
        run_js(r'''
DOMAINS[0].archived=false;
DOMAINS[0].member_count=1; DOMAINS[1].member_count=1;
const snapshot=DATA.meta.archive_snapshot;
delete DATA.meta.archive_snapshot;
renderDomainTabs();
$('renewal-alert-track').innerHTML='Frozen Member';
setData({...DATA,meta:{archive_snapshot:snapshot}},'frozen.test');
assert.match($('domain-tabs').innerHTML,/已封存 · 封号/,'unchanged count hid snapshot archive badge');
assert.ok(!$('renewal-alert-track').innerHTML.includes('Frozen Member'),'snapshot arrival left an active reminder');
''')

    def test_reminders_exclude_catalog_and_snapshot_archives_on_every_path(self):
        run_js(r'''
const today=new Date(2026,9,3);
assert.equal(buildRenewalAlerts([frozenMember],today).length,0,'current-domain quick path leaked archived reminder');
assert.deepEqual(Array.from(buildRenewalAlerts([
 {...frozenMember,domain:'frozen.test'},
 {...frozenMember,id:'l1',domain:'live.test'},
],today), x=>x.domain),['live.test'],'mixed-domain alert builder');
// Old member metadata must never override the owning domain supplied by the loader.
DOMAIN_CACHE['live.test'].members[0].domain='frozen.test';
let shown=[];
renderRenewalAlerts=members=>{shown=members};
await refreshAllRenewalAlerts();
assert.deepEqual(Array.from(shown,x=>x.domain),['live.test'],'all-domain path must not pass archived members');
DOMAINS[0].archived=false;
assert.equal(buildRenewalAlerts([{...frozenMember,domain:'frozen.test'}],today).length,0,'snapshot must fail closed if catalog is stale');
await refreshAllRenewalAlerts();
assert.deepEqual(Array.from(shown,x=>x.domain),['live.test'],'snapshot-only archived roster was forwarded');
''')


if __name__ == '__main__':
    unittest.main(verbosity=2)
