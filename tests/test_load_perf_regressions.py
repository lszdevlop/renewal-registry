#!/usr/bin/env python3
"""Load/perceived-speed regressions: finance request dedupe, stale GET guard, optimistic
writes, add-member double submit, reminder placeholder. Real inline JS under a DOM mock."""
import json
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
 value:['filter','member-status-filter'].includes(id)?'all':'',_text:'',innerHTML:'',
 get textContent(){return this._text},set textContent(v){this._text=String(v);this.innerHTML=this._text},
 disabled:false,hidden:false,style:{},dataset:{},attrs:{},listeners:{},
 classList:{add(){},remove(){},toggle(){}},
 addEventListener(type,fn){this.listeners[type]=fn},
 getAttribute(k){return this.attrs[k]??null},setAttribute(k,v){this.attrs[k]=v},
 querySelectorAll(){return []},querySelector(){return null},focus(){},scrollIntoView(){},
 });return elements.get(id);
}
const context=vm.createContext({assert,console,Intl,URL,CSS:{escape:x=>x},JSON,
 localStorage:{getItem:()=>null,setItem(){}},
 document:{getElementById:element,querySelectorAll:()=>[],querySelector:()=>null},
 window:{location:{origin:'http://perf.test'},confirm:()=>true},
 setTimeout:()=>0,clearTimeout(){},requestIdleCallback(){},
 fetch:async()=>{throw Error('unexpected network request')},
});
'''
FIXTURE = r'''
DOMAINS=[{id:'live.test',billing_day:3,member_count:1},{id:'other.test',billing_day:3,member_count:1}];
const alice={id:'alice-id',username:'Alice',email:'alice@live.test',status:'active',price:100,billing_day:3,notes:'',payments:[]};
DOMAIN_CACHE['live.test']={meta:{domain:'live.test'},members:[alice]};
DOMAIN_CACHE['other.test']={meta:{domain:'other.test'},members:[{...alice,id:'bob-id',username:'Bob',email:'bob@other.test'}]};
CURRENT_DOMAIN='live.test';DATA=DOMAIN_CACHE[CURRENT_DOMAIN];apiWritable=true;
$('month').value='2026-11';refreshAllRenewalAlerts=async()=>{};
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}}
const ok=body=>({ok:true,json:async()=>body});
const bad=(error,status=400)=>({ok:false,status,json:async()=>({ok:false,error})});
const tick=async()=>{for(let i=0;i<8;i++) await null;};
FINANCE_CACHE={ok:true,totals:{normal_daily:0,normal_profit:0,history_profit:0,nonrenewal_loss:'0.00'}};FINANCE_CACHE_AT=Date.now();
'''


def run_js(body):
    code = HARNESS + '\nvm.runInContext(' + json.dumps(SCRIPT) + ',context);\n'
    code += 'vm.runInContext(' + json.dumps(FIXTURE + '\n(async()=>{\n' + body + '\n})()')
    code += ',context).catch(e=>{console.error(e);process.exitCode=1});\n'
    result = subprocess.run(['node', '-e', code], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)


class LoadPerfTests(unittest.TestCase):
    def test_finance_requests_are_deduplicated_and_backed_off(self):
        run_js(r'''
let calls=0;const d=deferred();
fetch=async(url)=>{assert.equal(url,'/api/finance-summary');calls++;return d.promise};
FINANCE_CACHE=null;FINANCE_CACHE_AT=0;
for(let i=0;i<5;i++) renderGlobalKpi();
assert.equal(calls,1,'concurrent KPI renders must share one request');
d.resolve(ok({ok:true,totals:{normal_daily:1,normal_profit:2,history_profit:3,nonrenewal_loss:'0.00'},nonrenewal_enabled_at:'2026-09-12T04:48:17.996178+00:00'}));
await tick();await tick();
assert.equal($('kpi-normal-daily').textContent.length>0,true);
assert.match($('nonrenewal-loss-meta').textContent,/启用于 2026-09-12 04:48 UTC$/,'enable time must be formatted, not raw ISO');
// Failure: one attempt, then a 30s backoff with no further requests.
FINANCE_CACHE=null;FINANCE_CACHE_AT=0;calls=0;
fetch=async()=>{calls++;throw Error('down')};
await loadFinanceCache();await loadFinanceCache();renderGlobalKpi();renderGlobalKpi();
await tick();
assert.equal(calls,1,'failing endpoint must not be hammered on every render');
''')

    def test_stale_roster_response_cannot_overwrite_a_newer_local_write(self):
        run_js(r'''
const d=deferred();
fetch=async(url)=>{assert.match(url,/^\/api\/data\?domain=live\.test$/,'no cache-busting query, so ETag revalidation can work');return d.promise};
const pending=fetchDomainData('live.test',{force:true});
alice.price=250;noteLocalWrite('live.test');          // a save landed meanwhile
d.resolve(ok({ok:true,domain:'live.test',data:{meta:{domain:'live.test'},members:[{...alice,price:100}]}}));
const result=await pending;
assert.equal(result.members[0].price,250,'older snapshot overwrote the local write');
assert.equal(DOMAIN_CACHE['live.test'].members[0].price,250);
// Without an intervening write the fresh snapshot is applied.
const d2=deferred();fetch=async()=>d2.promise;
const pending2=fetchDomainData('live.test',{force:true});
d2.resolve(ok({ok:true,data:{meta:{domain:'live.test'},members:[{...alice,price:300}]}}));
assert.equal((await pending2).members[0].price,300);
''')

    def test_toggle_paid_is_optimistic_and_rolls_back_on_failure(self):
        run_js(r'''
render();
const d=deferred();let posts=0;
fetch=async(url,opts)=>{assert.equal(url,'/api/toggle-paid');posts++;return d.promise};
const pending=togglePaid('alice-id',false);
assert.equal(isPaid(alice,'2026-11'),true,'tag must flip before the server answers');
assert.match($('tbody').innerHTML,/标记未缴/);
togglePaid('alice-id',true);                     // second click while in flight is ignored
assert.equal(posts,1);
d.resolve(bad('disk full',500));
await pending;await tick();
assert.equal(isPaid(alice,'2026-11'),false,'failed toggle must be rolled back');
assert.match($('err').textContent,/已恢复/);
// Success path keeps the server record.
const d2=deferred();fetch=async()=>d2.promise;
const p2=togglePaid('alice-id',false);
d2.resolve(ok({ok:true,username:'Alice',month:'2026-11',paid:true,payment:{month:'2026-11',paid:true,amount:100,note:'看板一键标记'}}));
await p2;
assert.equal(isPaid(alice,'2026-11'),true);
''')

    def test_field_save_is_optimistic_and_restores_previous_value_on_error(self):
        run_js(r'''
render();
const input=$('price-input');input.attrs={'data-id':'alice-id','data-field':'price'};input.value='300';
const d=deferred();
fetch=async(url)=>{assert.equal(url,'/api/update-member');return d.promise};
const pending=saveMemberField(input);
assert.equal(alice.price,300,'model must update before the server answers');
d.resolve(bad('price rejected'));
await pending;
assert.equal(alice.price,100,'failed save must restore the previous value');
assert.equal(input.value,'100');
assert.match($('err').textContent,/已恢复原值/);
const d2=deferred();fetch=async()=>d2.promise;input.value='260';
const p2=saveMemberField(input);
d2.resolve(ok({ok:true,username:'Alice',member:{...alice,price:260}}));
await p2;
assert.equal(alice.price,260);
''')

    def test_add_member_cannot_double_submit_and_reminder_placeholder_stays_until_data(self):
        run_js(r'''
$('add-username').value='Newbie';$('add-email').value='newbie@live.test';
const d=deferred();let posts=0;
fetch=async(url)=>{assert.equal(url,'/api/add-member');posts++;return d.promise};
const first=addMember();
const second=addMember();                         // Enter key + button click race
assert.equal(posts,1,'add-member posted twice');
d.resolve(ok({ok:true,domain:'live.test',username:'Newbie',email:'newbie@live.test',member:{id:'new-id',username:'Newbie',email:'newbie@live.test',price:null,billing_day:null,payments:[]}}));
await first;await second;
assert.equal(DOMAIN_CACHE['live.test'].members.length,2);
// Reminder bar keeps its placeholder until at least one roster is cached.
for(const k of Object.keys(DOMAIN_CACHE)) delete DOMAIN_CACHE[k];
$('renewal-alert-track').innerHTML='正在计算当前域续费提醒…';
renderCachedRenewalAlerts();
assert.equal($('renewal-alert-track').innerHTML,'正在计算当前域续费提醒…','must not claim "no renewals" before data arrives');
''')

    def test_bootstrap_payload_seeds_caches_and_kpi(self):
        run_js(r'''
for(const k of Object.keys(DOMAIN_CACHE)) delete DOMAIN_CACHE[k];
FINANCE_CACHE=null;DOMAINS=[];
applyBootstrap({ok:true,default:'other.test',domains:[{id:'other.test',billing_day:3,member_count:1},{id:'live.test',billing_day:4,member_count:1}],
 data:{'other.test':{meta:{domain:'other.test'},members:[{id:'b',username:'Bob',price:1,billing_day:1,payments:[]}]},
       'live.test':{meta:{domain:'live.test',load_error:'corrupt'},members:[]}},
 finance:{ok:true,totals:{normal_daily:9,normal_profit:8,history_profit:7,nonrenewal_loss:'0.00'}}});
assert.equal(DOMAINS.length,2);
assert.ok(DOMAIN_CACHE['other.test'],'bootstrap rosters must be cached');
assert.equal(DOMAIN_CACHE['live.test'],undefined,'a domain the server could not load must not be cached as empty');
assert.equal(FINANCE_CACHE.totals.normal_daily,9);
assert.ok(FINANCE_CACHE_AT>0);
''')


if __name__ == '__main__':
    unittest.main(verbosity=2)
