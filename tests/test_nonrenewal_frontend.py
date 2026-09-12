"""Execute the real inline frontend JS in Node; synthetic DOM/network only."""
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

HTML = (Path(__file__).resolve().parents[1] / 'index.html').read_text()
SCRIPT = re.search(r'<script>(.*)</script>', HTML, re.S).group(1)
# Exclude only the automatic network bootstrap, not any UI handlers/functions.
SCRIPT = SCRIPT[:SCRIPT.rindex('    (async () => {')] + SCRIPT[SCRIPT.index('    })();', SCRIPT.rindex('    (async () => {')) + len('    })();'):]
HARNESS = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const elements = new Map();
function element(id) {
 if (!elements.has(id)) elements.set(id, {id, value:'', textContent:'', innerHTML:'', hidden:false, open:false, style:{}, events:{},
 classList:{add(){},remove(){},toggle(){}}, addEventListener(k, f){this.events[k]=f},
 querySelector(){return null}, querySelectorAll(){return []}, getAttribute(){return null},
 setAttribute(){}, showModal(){this.open=true}, close(){this.open=false}, focus(){}});
 return elements.get(id);
}
global.document={getElementById:element, querySelector(){return null}, querySelectorAll(){return []}};
global.window={confirm:()=>true, addEventListener(){}};
global.localStorage={getItem:()=>null,setItem(){}};
global.CSS={escape:x=>x};
global.setTimeout=()=>0; global.clearTimeout=()=>{}; global.setInterval=()=>0;
global.fetch=async()=>{throw Error('Unexpected network')};
vm.runInThisContext(SOURCE);
'''

def run_js(probe, tz='UTC'):
    js = HARNESS.replace('SOURCE', json.dumps(SCRIPT)) + '\n(async()=>{\n' + probe + '\n})().catch(e=>{console.error(e);process.exitCode=1});'
    out = subprocess.run(['node', '-e', js], text=True, capture_output=True, env={**os.environ, 'TZ':tz})
    if out.returncode:
        raise AssertionError(out.stdout + out.stderr)


class NonrenewalFrontendTests(unittest.TestCase):
    def test_kpi_uses_settled_decimal_cache_and_no_old_balance(self):
        self.assertIn('不续费损失合计', HTML)
        run_js('''
          DOMAINS = new Proxy([], {get(){throw Error('KPI must not scan domains')}});
          renderGlobalKpiFromCache({totals:{normal_daily:1,normal_profit:2,history_profit:3,nonrenewal_loss:'12.34',nonrenewal_count:2},nonrenewal_enabled_at:'2026-09-12T00:00:00+08:00'});
          assert.equal(element('kpi-nonrenewal-loss').textContent,'¥12.34');
          assert.equal(element('kpi-normal-daily').textContent,'¥1.00');
          assert.equal(element('kpi-normal-profit').textContent,'¥2.00');
          assert.equal(element('kpi-history-profit').textContent,'¥3.00');
          assert.match(element('nonrenewal-loss-meta').textContent,/2/);
        ''')


    def test_team_loss_cycles_rounding_and_missing_config(self):
        run_js('''
          const calc=(day,price,date)=>computeNonrenewalLoss({price,billing_day:2},{billing_day:day},makeLossTodayCtx(new Date(date+'T04:00:00Z')));
          assert.equal(typeof computeNonrenewalLoss,'function');
          let v=calc(15,800,'2026-04-15');
          assert.deepEqual([v.amountCny,v.remainingDays,v.cycleDays,v.cycleStart,v.cycleEnd],['863.60',30,30,'2026-04-15','2026-05-15']);
          v=calc(31,200,'2026-02-28');
          assert.deepEqual([v.amountCny,v.remainingDays,v.cycleDays,v.cycleEnd],['183.60',31,31,'2026-03-31']);
          v=calc(31,200,'2024-02-29');
          assert.deepEqual([v.amountCny,v.remainingDays,v.cycleDays],['183.60',31,31]);
          v=calc(31,800,'2026-02-27');
          assert.deepEqual([v.amountCny,v.remainingDays,v.cycleDays,v.cycleStart],['30.84',1,28,'2026-01-31']);
          v=calc(1,0,'2026-12-31');
          assert.deepEqual([v.amountCny,v.remainingDays,v.cycleDays,v.cycleEnd],['5.92',1,31,'2027-01-01']);
          assert.equal(calc(1,799.99,'2026-09-01').costCny,'183.60');
          for(const day of [null,undefined,'',true,0,32,1.5,'bad']) assert.equal(calc(day,100,'2026-09-12').ok,false);
          for(const price of [null,undefined,'',true,NaN,Infinity,-1,'bad']) assert.equal(calc(1,price,'2026-09-12').ok,false);
          for (let day=1;day<=31;day++) for(let d=1;d<=28;d++) {
            const z=calc(day,800,'2026-02-'+String(d).padStart(2,'0'));
            assert.equal(z.amountCny,(Math.floor((86360*z.remainingDays*2+z.cycleDays)/(2*z.cycleDays))/100).toFixed(2));
          }
        ''')

    def test_shanghai_date_independent_of_profit_timezone(self):
        for tz in ['UTC','America/Los_Angeles','Asia/Tokyo']:
            run_js('''
              const now=new Date('2026-09-11T16:01:00Z');
              const ctx=makeLossTodayCtx(now);
              assert.equal(ctx.todayStr,'2026-09-12');
              const v=computeNonrenewalLoss({price:800,billing_day:1},{billing_day:12},ctx);
              assert.equal(v.amountCny,'863.60');
              const pc=makeTodayCtx(now);
              assert.equal(pc.d,now.getDate());
              const m={price:800,billing_day:1,payments:[{paid:true,month:'2026-09',amount:9999}]};
              const metrics=computeMemberMetrics(m,pc);
              assert.equal(metrics.profit.total,((800-PROFIT_COST_PRO_CNY)/30)*pc.periodByDay[1].profitDays);
              assert.equal(metrics.lastPaidMonth,'2026-09');
            ''',tz=tz)


    def test_row_preview_uses_catalog_and_never_books_loss(self):
        self.assertTrue('预计删除损失' in HTML)
        self.assertFalse(re.search(r'(?i)refund|应退|退款', HTML))
        run_js('''
          CURRENT_DOMAIN='a'; DOMAINS=[{id:'a',billing_day:12}];
          DATA={meta:{},members:[{id:'one',username:'<script>',email:'one@example.test',price:800,billing_day:2,notes:'<b>note</b>'}]};
          DOMAIN_CACHE.a=DATA; element('filter').value='all';
          let kpiCalls=0; renderGlobalKpi=()=>kpiCalls++;
          render();
          let body=element('tbody').innerHTML;
          assert.match(body,/预计删除损失/);
          assert.match(body,/data-domain="a"/);
          assert.match(body,/&lt;script&gt;/);
          assert.ok(body.includes('&lt;b&gt;note&lt;/b&gt;'));
          assert.equal(FINANCE_CACHE,null);
          DOMAINS[0].billing_day=null; render();
          assert.match(element('tbody').innerHTML,/请在域名旁配置/);
          assert.equal(kpiCalls,2);
        ''')

    def test_domain_day_edit_rerenders_current_preview(self):
        run_js('''
          CURRENT_DOMAIN='a'; DOMAINS=[{id:'a',billing_day:12}]; DATA={members:[]};
          let renders=0;
          render=()=>{renders++;}; loadDomains=async()=>{DOMAINS=[{id:'a',billing_day:15}]};
          fetch=async()=>({ok:true,json:async()=>({ok:true})});
          const select=element('team-day'); select.value='15'; select.getAttribute=()=> 'a';
          await element('domain-tabs').events.change({target:{closest:()=>select}});
          assert.equal(renders,1);
          assert.equal(select.disabled,false);
        ''')


    def test_ledger_dialog_real_handlers_escape_empty_error_and_close(self):
        self.assertTrue('<dialog id="nonrenewal-loss-dialog"' in HTML)
        run_js('''
          let payload={ok:true,enabled_at:null,count:0,total_cny:'0.00',events:[]};
          fetch=async(url)=>{assert.equal(url,'/api/nonrenewal-loss');return {ok:true,json:async()=>payload}};
          await element('nonrenewal-loss-card').events.click();
          assert.equal(element('nonrenewal-loss-dialog').open,true);
          assert.match(element('nonrenewal-loss-body').innerHTML,/暂无/);
          assert.match(element('nonrenewal-loss-summary').textContent,/¥0.00/);
          assert.match(element('nonrenewal-loss-enabled').textContent,/尚未启用/);
          element('nonrenewal-loss-close').events.click();
          assert.equal(element('nonrenewal-loss-dialog').open,false);
          const bad='<img src=x onerror=alert(1)>';
          payload={ok:true,enabled_at:bad,count:1,total_cny:'6.12',events:[{domain:bad,email:bad,username:bad,deleted_at:bad,cycle_start:bad,cycle_end:bad,remaining_days:bad,cycle_days:bad,cost_cny:bad,amount_cny:bad}]};
          await element('nonrenewal-loss-card').events.click();
          const body=element('nonrenewal-loss-body').innerHTML;
          assert.ok(!body.includes('<img'));
          assert.ok(body.includes('&lt;img'));
          assert.equal(element('nonrenewal-loss-enabled').textContent,'启用于 '+bad+' · 仅启用后删除入账，不追溯历史；金额固定且不按月清零。');
          fetch=async()=>({ok:false,status:503,json:async()=>({error:bad})});
          await element('nonrenewal-loss-retry').events.click();
          assert.match(element('nonrenewal-loss-error').textContent,/加载失败/);
          assert.equal(element('nonrenewal-loss-body').innerHTML,'');
          assert.equal(element('nonrenewal-loss-retry').disabled,false);
        ''')

    def test_delete_confirmation_captured_domain_refresh_cache(self):
        run_js('''
          DOMAINS=[{id:'a',billing_day:12},{id:'b',billing_day:15}]; CURRENT_DOMAIN='b';
          DOMAIN_CACHE.a={members:[{id:'seat',username:'same',price:800,billing_day:1}]};
          DOMAIN_CACHE.b={members:[{id:'seat',username:'same',price:100,billing_day:1}]}; DATA=DOMAIN_CACHE.b;
          let prompt='',refreshes=0,release,posts=[];
          window.confirm=s=>{prompt=s;return true};
          refreshAllRenewalAlerts=async()=>{}; renderDomainTabs=()=>{}; render=()=>{}; syncDomainCountsFromCache=()=>true;
          fetch=async(url,opts)=>{
            if(url==='/api/delete-member'){posts.push(JSON.parse(opts.body));return await new Promise(r=>release=r);}
            assert.equal(url,'/api/finance-summary?force=1'); refreshes++;
            return {ok:true,json:async()=>({ok:true,totals:{normal_daily:1,normal_profit:2,history_profit:3,nonrenewal_loss:'10.00',nonrenewal_count:1}})};
          };
          const pending=deleteMember('seat','same','a');
          assert.match(prompt,/预计删除损失：¥/); assert.match(prompt,/永久/); assert.match(prompt,/终身累计/);
          CURRENT_DOMAIN='b';
          release({ok:true,json:async()=>({ok:true,id:'seat',member_count:0,loss_event:{amount_cny:'10.00'}})});
          await pending;
          assert.equal(posts[0].domain,'a');
          assert.equal(DOMAIN_CACHE.a.members.length,0);
          assert.equal(DOMAIN_CACHE.b.members.length,1);
          assert.equal(DATA,DOMAIN_CACHE.b);
          assert.equal(refreshes,1);
          assert.equal(element('kpi-nonrenewal-loss').textContent,'¥10.00');
        ''')


if __name__ == '__main__':
    unittest.main(verbosity=2)
