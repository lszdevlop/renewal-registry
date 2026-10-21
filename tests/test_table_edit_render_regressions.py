#!/usr/bin/env python3
"""Table re-render regressions: new member visible, live edits survive saves, debounced search.

Synthetic rosters only; every network request is intercepted.
"""
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
 value:id==='filter'?'all':'',_text:'',innerHTML:'',
 get textContent(){return this._text},set textContent(v){this._text=String(v);this.innerHTML=this._text},
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
 window:{location:{origin:'http://table-edit.test'},confirm:()=>true},
 setTimeout:()=>0,clearTimeout(){},requestIdleCallback(){},
 fetch:async()=>{throw Error('unexpected network request')},
});
'''
FIXTURE = r'''
DOMAINS=[{id:'live.test',billing_day:3,member_count:1}];
const alice={id:'alice-id',username:'Alice',email:'alice@example.test',status:'active',price:300,billing_day:3,notes:'',payments:[]};
DOMAIN_CACHE['live.test']={meta:{},members:[alice]};
CURRENT_DOMAIN='live.test';DATA=DOMAIN_CACHE[CURRENT_DOMAIN];apiWritable=true;
$('month').value='2026-10';renderGlobalKpi=()=>{};refreshAllRenewalAlerts=async()=>{};
'''


def run_js(body):
    code = HARNESS + '\nvm.runInContext(' + json.dumps(SCRIPT) + ',context);\n'
    code += 'vm.runInContext(' + json.dumps(FIXTURE + '\n(async()=>{\n' + body + '\n})()')
    code += ',context).catch(e=>{console.error(e);process.exitCode=1});\n'
    result = subprocess.run(['node', '-e', code], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)


class TableEditRenderTests(unittest.TestCase):
    def test_added_member_is_rendered_even_when_filters_would_hide_it(self):
        run_js(r'''
render();
assert.ok($('tbody').innerHTML.includes('Alice'));
$('q').value='alice'; statQuickFilter='paid'; focusedMemberKey='id:alice-id';
const newbie={id:'new-id',username:'Newbie',email:'newbie@example.test',status:'active',price:260,billing_day:9,notes:'',payments:[]};
fetch=async(url)=>{assert.equal(url,'/api/add-member');return {ok:true,json:async()=>({ok:true,domain:'live.test',username:'Newbie',email:newbie.email,member:newbie})}};
$('add-username').value='Newbie';$('add-email').value=newbie.email;
await addMember();
assert.ok($('tbody').innerHTML.includes('Newbie'),'new member missing from table');
assert.ok($('tbody').innerHTML.includes('Alice'),'filters were not cleared');
assert.match($('stats').innerHTML,/当前域成员<\/div><div class="v">2</,'member count card not refreshed');
assert.equal($('q').value,'');assert.equal(statQuickFilter,'all');assert.equal(focusedMemberKey,null);
''')

    def test_search_typing_is_debounced_but_dropdowns_apply_immediately(self):
        run_js(r'''
const timers=[];setTimeout=(fn,ms)=>{timers.push({fn,ms});return timers.length};
let renders=0;const realRender=render;render=()=>{renders++;realRender()};
statQuickFilter='unpaid';
for(const ch of ['a','al','ali']){$('q').value=ch;$('q').listeners.input();}
assert.equal(renders,0,'search rendered on every keystroke');
assert.equal(statQuickFilter,'all','quick filter must reset immediately');
assert.equal(timers.at(-1).ms,180);
timers.at(-1).fn();
assert.equal(renders,1);
$('filter').listeners.input();
assert.equal(renders,2,'dropdown filter must apply immediately');
''')

    def test_notes_autosize_reads_layout_once_per_render(self):
        run_js(r'''
const log=[];
const mk=h=>({style:new Proxy({},{set(t,k,v){log.push('w');t[k]=v;return true}}),get scrollHeight(){log.push('r');return h}});
autosizeNotesBatch([mk(20),mk(60),mk(500)]);
assert.equal(log.join(''),'wwwrrrwww','reads and writes interleave (forced reflow per row)');
''')

    @unittest.skipUnless(os.environ.get('MEMBER_BAN_PLAYWRIGHT_MODULE'), 'set MEMBER_BAN_PLAYWRIGHT_MODULE for browser sandbox')
    def test_browser_live_edit_survives_concurrent_save_render(self):
        probe = r'''
const fs=require('node:fs'),assert=require('node:assert/strict');
const {chromium}=require(process.env.MEMBER_BAN_PLAYWRIGHT_MODULE),html=fs.readFileSync(0,'utf8');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox'],...(process.env.MEMBER_BAN_CHROMIUM_EXECUTABLE?{executablePath:process.env.MEMBER_BAN_CHROMIUM_EXECUTABLE}:{})});
 try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(8000);
 const errors=[],writes=[];page.on('pageerror',e=>errors.push(e.message));
 const a={id:'a-id',username:'Alpha',email:'a@example.test',status:'active',price:300,billing_day:3,notes:'',payments:[]};
 const b={id:'b-id',username:'Beta',email:'b@example.test',status:'active',price:302,billing_day:4,notes:'',payments:[]};
 const c={id:'c-id',username:'Gamma',email:'c@example.test',status:'active',price:null,billing_day:5,notes:'',payments:[]};
 const live={meta:{},members:[a,b,c]};
 await page.route('**/*',async route=>{
  const req=route.request(),url=new URL(req.url());const json=body=>route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  if(url.origin!=='http://table-edit.test')return route.abort();
  if(req.method()!=='GET'){
   const body=req.postDataJSON();writes.push(body);
   assert.equal(url.pathname,'/api/update-member');
   await new Promise(r=>setTimeout(r,400));
   const m=live.members.find(x=>x.id===body.id);for(const k of Object.keys(body))if(!['domain','id'].includes(k))m[k]=body[k];
   return json({ok:true,username:m.username,member:m});
  }
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
  if(url.pathname==='/api/domains')return json({ok:true,default:'live.test',domains:[{id:'live.test',billing_day:3}]});
  if(url.pathname==='/api/data')return json(live);
  if(url.pathname==='/api/finance-summary')return json({ok:true,totals:{normal_daily:0,normal_profit:0,history_profit:0,nonrenewal_loss:0}});
  return route.abort();
 });
 await page.goto('http://table-edit.test/');
 await page.waitForFunction(()=>document.querySelectorAll('#tbody tr[data-id]').length===3);
 const field=(id,f)=>page.locator(`#tbody .edit-input[data-id="${id}"][data-field="${f}"]`);
 // 1) Save Alpha's price, then type into Beta's notes while that save is in flight.
 await field('a-id','price').fill('310');await field('b-id','notes').click();
 await page.keyboard.type('abc');
 await page.waitForFunction(()=>document.querySelector('.edit-input[data-id="a-id"][data-field="price"]').value==='310'&&!document.querySelector('.edit-input[data-id="a-id"][data-field="price"]').disabled);
 await page.waitForTimeout(100);
 assert.equal(await field('b-id','notes').inputValue(),'abc','in-flight save render dropped typed notes');
 assert.equal(await page.evaluate(()=>document.activeElement?.dataset.field),'notes','focus lost after render');
 await page.keyboard.type('XYZ');
 assert.equal(await field('b-id','notes').inputValue(),'abcXYZ');
 // 2) Change Beta's day, Tab to its price and type before the save returns.
 await field('b-id','billing_day').fill('9');await page.keyboard.press('Tab');
 await page.keyboard.press('Control+A');await page.keyboard.type('777');
 await page.waitForFunction(()=>document.querySelector('.edit-input[data-id="b-id"][data-field="billing_day"]').value==='9'&&!document.querySelector('.edit-input[data-id="b-id"][data-field="billing_day"]').disabled);
 await page.waitForTimeout(100);
 assert.equal(await field('b-id','price').inputValue(),'777','in-flight save render dropped typed price');
 // Leaving the restored field without further typing must still save it.
 await page.locator('#q').click();
 await page.waitForFunction(()=>document.querySelector('.edit-input[data-id="b-id"][data-field="price"]').value==='777'&&!document.querySelector('.edit-input[data-id="b-id"][data-field="price"]').disabled);
 await page.waitForTimeout(500);
 assert.ok(writes.some(w=>w.id==='b-id'&&w.price===777),'restored price was never saved: '+JSON.stringify(writes));
 assert.ok(writes.some(w=>w.id==='b-id'&&w.notes==='abcXYZ'),'restored notes were never saved: '+JSON.stringify(writes));
 assert.equal(writes.filter(w=>w.id==='b-id'&&w.price===777).length,1,'restored edit saved twice');
 // 3) In the 'missing day/price' view, completing Gamma's price removes the row while
 //    its notes are being typed; the typed notes must still be saved exactly once.
 await page.locator('[data-stat-filter="unset"]').click();
 await page.waitForFunction(()=>document.querySelectorAll('#tbody tr[data-id]').length===1);
 await field('c-id','price').fill('200');await field('c-id','notes').click();await page.keyboard.type('note!');
 await page.waitForFunction(()=>!document.querySelector('#tbody tr[data-id="c-id"]'));
 await page.waitForTimeout(600);
 assert.equal(writes.filter(w=>w.id==='c-id'&&w.notes==='note!').length,1,'notes typed into a row that left the view were lost: '+JSON.stringify(writes));
 assert.equal(c.notes,'note!');
 assert.deepEqual(errors,[]);
 console.log('PASS Chromium: live edits survive concurrent save renders and are saved once');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run(['node', '-e', probe], input=HTML, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        print(result.stdout.strip())


if __name__ == '__main__':
    unittest.main(verbosity=2)
