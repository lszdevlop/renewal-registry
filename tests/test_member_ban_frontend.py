#!/usr/bin/env python3
"""Actual inline JavaScript + fully intercepted Chromium; synthetic rosters only."""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

HTML = (Path(__file__).resolve().parents[1] / 'index.html').read_text()
SCRIPT_MATCH = re.search(r'<script>(.*?)</script>', HTML, re.S)
assert SCRIPT_MATCH, 'missing inline frontend script'
SCRIPT = SCRIPT_MATCH.group(1).rsplit('    (async () => {', 1)[0]
HARNESS = r'''
const assert=require('node:assert/strict'),vm=require('node:vm');
const elements=new Map();
function element(id){
 if(!elements.has(id))elements.set(id,{
 value:['filter','member-status-filter'].includes(id)?'all':'',textContent:'',innerHTML:'',
 disabled:false,hidden:false,style:{},dataset:{},attrs:{},listeners:{},
 classList:{add(){},remove(){},toggle(){}},
 addEventListener(type,fn){this.listeners[type]=fn},
 getAttribute(k){return this.attrs[k]??null},setAttribute(k,v){this.attrs[k]=v},
 querySelectorAll(){return []},focus(){},scrollIntoView(){},
 });return elements.get(id);
}
const context=vm.createContext({assert,console,Intl,URL,CSS:{escape:x=>x},
 localStorage:{getItem:()=>null,setItem(){}},
 document:{getElementById:element,querySelectorAll:()=>[],querySelector:()=>null},
 window:{location:{origin:'http://member-ban.test'},confirm:()=>true},
 setTimeout:()=>0,clearTimeout(){},requestIdleCallback(){},
 fetch:async()=>{throw Error('unexpected network request')},
});
'''
FIXTURE = r'''
DOMAINS=[{id:'live.test',billing_day:3},{id:'other.test',billing_day:3},{id:'archive.test',archived:true}];
const normal={id:'normal-id',username:'Same Name',email:'normal@example.test',status:'active',price:300,billing_day:3,notes:'normal note',payments:[]};
const banned={...normal,id:'banned-id',username:'Banned Member',email:'banned@example.test',status:'banned',banned_at:'2026-10-02T08:00:00+08:00',notes:'retained',ban_snapshot:{banned_at:'2026-10-02T08:00:00+08:00',reason:'封号',as_of_date:'2026-10-02',totals:{normal_daily:1.23,normal_profit:-45.67,history_profit:890.12}}};
DOMAIN_CACHE['live.test']={meta:{},members:[normal,banned]};
DOMAIN_CACHE['other.test']={meta:{},members:[{...normal,id:'other-id',username:'Other Member'}]};
DOMAIN_CACHE['archive.test']={meta:{archive_snapshot:{members:{'id:inherited-id':{normal_profit:12.34},'id:banned-id':{normal_profit:999}}}},members:[{...normal,id:'inherited-id',username:'Inherited Member'},banned]};
CURRENT_DOMAIN='live.test';DATA=DOMAIN_CACHE[CURRENT_DOMAIN];
$('month').value='2026-10';renderGlobalKpi=()=>{};
'''


def run_js(body):
    code = HARNESS + '\nvm.runInContext(' + json.dumps(SCRIPT) + ',context);\n'
    code += 'vm.runInContext(' + json.dumps(FIXTURE + '\n(async()=>{\n' + body + '\n})()')
    code += ',context).catch(e=>{console.error(e);process.exitCode=1});\n'
    result = subprocess.run(['node', '-e', code], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)


class MemberBanFrontendTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('MEMBER_BAN_PLAYWRIGHT_MODULE'), 'set MEMBER_BAN_PLAYWRIGHT_MODULE for browser sandbox')
    def test_browser_confirm_cancel_ban_switch_reload_notes_filters_mobile(self):
        probe = r'''
const fs=require('node:fs'),assert=require('node:assert/strict');
const {chromium}=require(process.env.MEMBER_BAN_PLAYWRIGHT_MODULE),html=fs.readFileSync(0,'utf8');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox'],...(process.env.MEMBER_BAN_CHROMIUM_EXECUTABLE?{executablePath:process.env.MEMBER_BAN_CHROMIUM_EXECUTABLE}:{})});
 try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(8000);
 const errors=[],writes=[];page.on('pageerror',e=>errors.push(e.message));
 const normal={id:'stable-id',username:'Same Name',email:'normal@example.test',status:'active',price:300,billing_day:new Date().getDate(),activation_date:'2026-01-01',notes:'retained',payments:[]};
 const snapshot={banned_at:'2026-10-02T08:00:00+08:00',reason:'封号',as_of_date:'2026-10-02',totals:{normal_daily:1.23,normal_profit:45.67,history_profit:89.01}};
 const live={meta:{},members:[normal,{...normal,id:'twin-id',email:'twin@example.test'}]};
 const other={meta:{},members:[{...normal,id:'other-id',username:'Other Member'}]};
 const archived={meta:{archive_snapshot:{members:{'id:inherited-id':{normal_profit:12.34}}}},members:[{...normal,id:'inherited-id',username:'Inherited Member',email:'inherited@example.test'}]};
 const rosters={'live.test':live,'other.test':other,'archive.test':archived};
 const catalog={ok:true,default:'live.test',domains:[{id:'archive.test',archived:true},{id:'live.test',billing_day:3},{id:'other.test',billing_day:3}]};
 let finishBan,banReceived;const received=new Promise(resolve=>{banReceived=resolve});
 await page.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());const json=body=>route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  if(url.origin!=='http://member-ban.test')return route.abort();
  if(req.method()!=='GET'){
   const body=req.postDataJSON();writes.push({path:url.pathname,body});
   if(url.pathname==='/api/ban-member'){
    assert.deepEqual(body,{domain:'live.test',id:'stable-id',confirm_id:'stable-id',reason:'封号'});
    banReceived();await new Promise(resolve=>{finishBan=resolve});
    normal.status='banned';normal.banned_at=snapshot.banned_at;normal.ban_snapshot=snapshot;
    return json({ok:true,domain:'live.test',data:live});
   }
   assert.equal(url.pathname,'/api/update-member');assert.deepEqual(Object.keys(body).sort(),['domain','id','notes']);assert.equal(body.domain,'live.test');assert.equal(body.id,'stable-id');
   normal.notes=body.notes;return json({ok:true,member:normal});
  }
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
  if(url.pathname==='/api/domains')return json(catalog);
  if(url.pathname==='/api/data')return json(rosters[url.searchParams.get('domain')]);
  if(url.pathname==='/api/finance-summary')return json({ok:true,totals:{normal_daily:2,normal_profit:3,history_profit:4,nonrenewal_loss:0}});
  return route.abort();
 });
 await page.goto('http://member-ban.test/');
 await page.waitForFunction(()=>document.querySelectorAll('#tbody tr[data-id]').length===2);
 await page.waitForFunction(()=>document.querySelector('#renewal-alert-track').textContent.includes('Other Member'));
 assert.equal(writes.length,0,'startup must not auto-ban');
 const row=()=>page.locator('#tbody tr[data-id="stable-id"]');
 assert.equal(await row().locator('.member-status').innerText(),'正常');
 assert.deepEqual(await page.locator('button.domain-tab').evaluateAll(es=>es.map(e=>e.dataset.domain)),['live.test','other.test','archive.test']);
 assert.equal(await page.locator('#member-status-filter option').evaluateAll(es=>es.map(e=>e.value)).then(x=>x.join(',')),'all,normal,banned');
 let dialogs=0;page.on('dialog',async d=>{dialogs++;await d.dismiss()});
 const actions=row().locator('.actions-cell button');
 const sizes=await actions.evaluateAll(es=>es.map(e=>({width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height,wrap:getComputedStyle(e).whiteSpace,overflow:e.scrollWidth>e.clientWidth})));
 assert.equal(sizes.length,2);assert.equal(sizes[0].width,sizes[1].width);
 for(const s of sizes){assert.equal(s.wrap,'nowrap');assert.equal(s.overflow,false);assert.ok(s.height<=34)}
 await row().locator('[data-action="ban-member"]').focus();await page.keyboard.press('Enter');await received;
 assert.equal(await row().locator('[data-action="ban-member"]').isDisabled(),true);
 await page.locator('button.domain-tab[data-domain="other.test"]').click();
 await page.waitForFunction(()=>document.querySelector('#tbody').textContent.includes('Other Member'));
 finishBan();await page.waitForFunction(()=>document.querySelector('#okmsg').textContent.includes('已标记被封'));
 assert.ok((await page.locator('#tbody').innerText()).includes('Other Member'),'late response painted wrong domain');
 assert.equal(await page.locator('#kpi-normal-profit').innerText(),'¥3.00');
 assert.ok(!(await page.locator('#renewal-alert-track').innerHTML()).includes('id:stable-id'));
 await page.locator('#member-status-filter').selectOption('banned');
 // An archived domain can win an equal-score match; narrow to the exact email.
 await page.locator('#q').fill('normal@example.test');
 await page.waitForFunction(()=>document.querySelector('#tbody tr[data-id="stable-id"]'));
 await page.locator('#q').fill('');
 assert.equal(await page.locator('#tbody tr[data-id]').count(),1);
 assert.equal(await row().locator('.member-status').innerText(),'被封');
 assert.ok((await row().innerText()).includes('¥45.67'));assert.ok((await row().innerText()).includes('— 已被封'));
 for(const s of ['[data-field="price"]','[data-field="billing_day"]','[data-field="activation_date"]','[data-action="toggle-paid"]','[data-action="delete-member"]'])assert.equal(await row().locator(s).isDisabled(),true,s);
 assert.equal(await row().locator('[data-action="ban-member"]').count(),0);
 const note=row().locator('[data-field="notes"]');await note.fill('edited sandbox note');await note.blur();
 await page.waitForFunction(()=>document.querySelector('#tbody textarea[data-id="stable-id"]').value==='edited sandbox note'&&!document.querySelector('#tbody textarea[data-id="stable-id"]').disabled);
 assert.equal(normal.notes,'edited sandbox note');assert.equal(writes.length,2);
 await page.locator('#member-status-filter').selectOption('normal');assert.equal(await page.locator('#tbody tr[data-id]').count(),1);assert.equal(await page.locator('#tbody tr').getAttribute('data-id'),'twin-id');
 await page.locator('#member-status-filter').selectOption('banned');await page.locator('#q').fill('Inherited Member');
 await page.waitForFunction(()=>document.querySelector('#tbody').textContent.includes('Inherited Member'));
 assert.equal(await page.locator('#tbody .member-status').innerText(),'被封·域封存');assert.equal(await page.locator('#tbody [data-action="ban-member"]').count(),0);
 await page.locator('#q').fill('');await page.locator('#member-status-filter').selectOption('all');await page.locator('button.domain-tab[data-domain="live.test"]').click();
 await page.reload();await page.waitForFunction(()=>document.querySelector('#tbody tr[data-id="stable-id"] .member-status')?.textContent==='被封');
 assert.equal(await row().locator('textarea').inputValue(),'edited sandbox note');assert.ok((await row().innerText()).includes('¥45.67'));
 assert.ok((await page.locator('[data-stat-filter="all"]').innerText()).includes('正常 1 · 被封 1'));
 assert.equal(await page.locator('[data-stat-filter="unpaid"] .v').innerText(),'1');
 await page.setViewportSize({width:390,height:844});
 const selectBox=await page.locator('#member-status-filter').boundingBox();assert.ok(selectBox.x>=0&&selectBox.x+selectBox.width<=390);
 const button=page.locator('#tbody tr[data-id="twin-id"] [data-action="ban-member"]');await button.scrollIntoViewIfNeeded();const box=await button.boundingBox();assert.ok(box.height>=44);
 assert.equal(await page.locator('body').evaluate(e=>getComputedStyle(e).backgroundColor),'rgb(244, 246, 242)');
 assert.equal(await page.locator('body').evaluate(e=>getComputedStyle(e).fontFamily.includes('Noto Sans SC')),true);
 assert.deepEqual(errors,[]);assert.equal(writes.length,2);assert.equal(dialogs,0,'ban must not open a dialog');
 console.log('PASS Chromium sandbox: no-dialog action, aligned buttons, stable ID, switch race, frozen reload, notes-only, status/search, inherited archive, mobile');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', probe], input=HTML, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())

    def test_new_banned_roster_immediately_clears_cached_reminder(self):
        run_js(r'''
$('renewal-alert-track').innerHTML='Banned Member';
setData({meta:{},members:[banned]},'live.test');
assert.ok(!$('renewal-alert-track').innerHTML.includes('Banned Member'),'fresh banned roster left stale reminder');
''')

    def test_ban_action_confirmation_stable_id_failure_and_switch_race(self):
        run_js(r'''
render();assert.match($('tbody').innerHTML,/data-action="ban-member"/);
let writes=[],release;
window.confirm=()=>{throw Error('ban must not open a confirmation dialog')};
fetch=async(url,options)=>{writes.push({url,body:JSON.parse(options.body)});return new Promise(resolve=>{release=resolve})};
await banMember(normal.username,'live.test');assert.equal(writes.length,0,'display-name fallback must not ban');
await banMember(banned.id,'live.test');await banMember('inherited-id','archive.test');assert.equal(writes.length,0);
DATA.members.push({...normal,id:'inactive-id',status:'inactive'});
await banMember('inactive-id','live.test');assert.equal(writes.length,0);
let refreshed=false;loadFinanceCache=async options=>{assert.equal(options.force,true);refreshed=true;return null};
const pending=banMember(normal.id,'live.test');
assert.equal(writes.length,1);
assert.equal(writes[0].url,'/api/ban-member');
assert.equal(JSON.stringify(writes[0].body),JSON.stringify({domain:'live.test',id:'normal-id',confirm_id:'normal-id',reason:'封号'}));
// A second click while awaiting confirmation response cannot send another write.
await banMember(normal.id,'live.test');assert.equal(writes.length,1);
await switchDomain('other.test');const otherData=DATA,otherTable=$('tbody').innerHTML;
const updated={meta:{server_enriched:true},members:[{...normal,status:'banned',ban_snapshot:banned.ban_snapshot},banned]};
release({ok:true,json:async()=>({ok:true,domain:'live.test',data:updated})});await pending;
assert.equal(CURRENT_DOMAIN,'other.test');assert.equal(DATA,otherData);assert.equal($('tbody').innerHTML,otherTable);
assert.equal(DOMAIN_CACHE['live.test'],updated);assert.equal(DOMAIN_CACHE['live.test'].members[0].status,'banned');assert.equal(refreshed,true);
await switchDomain('live.test');assert.ok(!$('tbody').innerHTML.includes('data-action="ban-member"'));assert.match($('tbody').innerHTML,/-¥45\.67/);
// Rejections preserve all local fields and leave the action retryable.
await switchDomain('other.test');const unchanged=JSON.stringify(DATA.members);
fetch=async()=>({ok:false,status:409,json:async()=>({ok:false,error:'conflict'})});
await banMember('other-id','other.test');assert.equal(JSON.stringify(DATA.members),unchanged);assert.match($('err').textContent,/conflict/);
fetch=async()=>{throw Error('offline')};await banMember('other-id','other.test');assert.match($('err').textContent,/offline/);
''')

    def test_status_filter_cross_domain_counts_and_manual_payment_history(self):
        run_js(r'''
DATA.members.push({...normal,id:'paid-id',username:'Paid Normal',payments:[{month:'2026-10',paid:true}]},
 {...normal,id:'inactive-id',username:'Inactive Member',status:'inactive'});
banned.price=null;banned.billing_day=null;
banned.payments=[{month:'2026-10',paid:true}];
render();
assert.match($('stats').innerHTML,/正常 2 · 被封 1 · 停用 1/);
const count=key=>$('stats').innerHTML.match(new RegExp('data-stat-filter="'+key+'"[\\s\\S]*?class="v"[^>]*>(\\d+)'))[1];
assert.equal(count('all'),'4');assert.equal(count('paid'),'1');assert.equal(count('unpaid'),'1');assert.equal(count('unset'),'0');
$('member-status-filter').value='banned';render();
assert.match($('tbody').innerHTML,/Banned Member/);assert.ok(!$('tbody').innerHTML.includes('Same Name'));
$('filter').value='paid';render();assert.match($('tbody').innerHTML,/Banned Member/,'manual payment filter preserves history');
$('filter').value='unpaid';render();assert.ok(!$('tbody').innerHTML.includes('Banned Member'));
$('filter').value='all';$('member-status-filter').value='normal';render();
assert.ok(!$('tbody').innerHTML.includes('Banned Member'));assert.ok(!$('tbody').innerHTML.includes('Inactive Member'));
assert.equal(memberMatchesQuery(DOMAIN_CACHE['archive.test'].members[0],'','all','2026-10','normal','archive.test'),false);
assert.equal(memberMatchesQuery(DOMAIN_CACHE['archive.test'].members[0],'','all','2026-10','banned','archive.test'),true);
CURRENT_DOMAIN='other.test';DATA=DOMAIN_CACHE[CURRENT_DOMAIN];
assert.equal(pickBestDomainForQuery('banned member','all','2026-10','banned'),'live.test');
$('member-status-filter').value='banned';$('q').value='inherited';render();
assert.equal(CURRENT_DOMAIN,'archive.test');assert.match($('tbody').innerHTML,/Inherited Member/);
// Quick cards are counts for the current Team, not historical paid filters.
CURRENT_DOMAIN='live.test';DATA=DOMAIN_CACHE[CURRENT_DOMAIN];applyStatQuickFilter('paid');
assert.equal($('member-status-filter').value,'all');assert.match($('tbody').innerHTML,/Paid Normal/);
assert.ok(!$('tbody').innerHTML.includes('Banned Member'));
applyStatQuickFilter('all');assert.match($('tbody').innerHTML,/Inactive Member/);
$('member-status-filter').value='banned';await focusRenewalMember('live.test','id:normal-id');
assert.equal($('member-status-filter').value,'all');assert.match($('tbody').innerHTML,/Same Name/);
''')

    def test_banned_row_notes_only_frozen_profit_and_no_reminders(self):
        run_js(r'''
DATA.members=[banned];
const original=JSON.stringify(banned.ban_snapshot);
computeMemberMetrics=()=>{throw Error('banned member recalculated live profit')};
computeNonrenewalLoss=()=>{throw Error('banned member calculated loss preview')};
for(const when of ['2026-10-03','2035-01-01']){
 makeTodayCtx=()=>({date:when});render();
 const row=$('tbody').innerHTML;
 assert.match(row,/-¥45\.67/);assert.match(row,/— 已被封/);assert.match(row,/被封/);
 for(const field of ['price','billing_day','activation_date'])assert.match(row.match(new RegExp('<input[^>]*data-field="'+field+'"[^>]*>'))[0],/disabled/);
 for(const action of ['toggle-paid','delete-member'])assert.match(row.match(new RegExp('<button[^>]*data-action="'+action+'"[^>]*>'))[0],/disabled/);
 assert.ok(!row.match(/<textarea[^>]*>/)[0].includes('disabled'));
 assert.ok(!row.includes('data-action="ban-member"'));
 assert.equal(JSON.stringify(banned.ban_snapshot),original);
}
const writes=[];fetch=async(url,options)=>{writes.push({url,body:JSON.parse(options.body)});return {ok:true,json:async()=>({ok:true})}};
for(const field of ['price','billing_day','activation_date','notes']){
 const input=$('fake-'+field);input.attrs={'data-id':banned.id,'data-field':field};input.value=field==='notes'?'edited note':field==='activation_date'?'2026-10-03':'5';
 await saveMemberField(input);
}
await togglePaid(banned.id,false);await deleteMember(banned.id,banned.username);
assert.equal(writes.length,1);assert.equal(writes[0].body.notes,'edited note');assert.equal(banned.notes,'edited note');
assert.equal(buildRenewalAlerts([{...banned,domain:'live.test'}],new Date(2026,9,3)).length,0);
renderCachedRenewalAlerts();assert.ok(!$('renewal-alert-track').innerHTML.includes('Banned Member'));
await refreshAllRenewalAlerts();assert.ok(!$('renewal-alert-track').innerHTML.includes('Banned Member'));
// Individual snapshots take precedence even after the owning Team is archived.
CURRENT_DOMAIN='archive.test';DATA=DOMAIN_CACHE[CURRENT_DOMAIN];render();
assert.match($('tbody').innerHTML,/-¥45\.67/);assert.match($('tbody').innerHTML,/¥12\.34/);
assert.match($('tbody').innerHTML,/被封·域封存/);
assert.ok(!$('tbody').innerHTML.includes('data-action="ban-member"'));
CURRENT_DOMAIN='live.test';DATA=DOMAIN_CACHE[CURRENT_DOMAIN];
for(const value of [0,null,undefined]){
 banned.ban_snapshot.totals.normal_profit=value;render();
 assert.ok(!$('tbody').innerHTML.includes('NaN'));
 assert.ok(!$('tbody').innerHTML.includes('-¥45.67'));
 if(value===0)assert.match($('tbody').innerHTML,/¥0\.00/);
}
delete banned.ban_snapshot;render();assert.ok(!$('tbody').innerHTML.includes('-¥45.67'));
''')


if __name__ == '__main__':
    unittest.main(verbosity=2)
