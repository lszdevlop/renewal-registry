#!/usr/bin/env python3
"""Real frontend profit/loss rules and render-path performance, synthetic data only.

Retain the legacy filename for direct-script runners; customer refunds are no
longer a board statistic. Never query a production port or mirror JS in Python.
"""
from __future__ import annotations

from datetime import date
import json
import os
from pathlib import Path
import shutil

import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from finance_metrics import aggregate_domains, member_metrics
from nonrenewal_loss import LossStore


def percentile95(samples):
    return sorted(samples)[max(0, int(len(samples) * 0.95) - 1)]


def run_frontend(html, cases):
    # Execute verbatim source, including constants and shared date/payment helpers.
    # This deliberately excludes DOM/bootstrap code, not any financial logic.
    start = html.index('    const PROFIT_USD_CNY =')
    end = html.index('    function formatProfit(', start)
    source = html[start:end]
    probe = r'''
const assert = require('node:assert/strict');
const {performance} = require('node:perf_hooks');
const cases = CASES;
const results = cases.map(c => {
  const [y,m,d] = c.date.split('-').map(Number);
  const ctx = makeTodayCtx(new Date(y,m-1,d,12));
  const lossCtx = makeLossTodayCtx(new Date(c.date+'T04:00:00Z'));
  return {metrics:computeMemberMetrics(c.member,ctx),
    loss:computeNonrenewalLoss(c.member,{billing_day:c.team_day},lossCtx)};
});
const small = Array.from({length:65},(_,i)=>cases[i%cases.length].member);
const big = Array.from({length:2000},(_,i)=>cases[i%cases.length].member);
function benchmark(members) {
  const times=[];
  for(let round=0; round<60; round++) {
    const start=performance.now();
    const ctx=makeTodayCtx(new Date(2026,6,26,12));
    const lossCtx=makeLossTodayCtx(new Date('2026-07-26T04:00:00Z'));
    let checksum=0;
    for(const m of members) {
      const x=computeMemberMetrics(m,ctx);
      const loss=computeNonrenewalLoss(m,{billing_day:20},lossCtx);
      checksum+=(x.profit?.total || 0)+(loss.ok ? Number(loss.amountCny):0);
    }
    assert(Number.isFinite(checksum));
    if(round>=10) times.push(performance.now()-start);
  }
  return times.sort((a,b)=>a-b);
}
console.log(JSON.stringify({results,small:benchmark(small),big:benchmark(big)}));
'''.replace('CASES', json.dumps(cases))
    proc = subprocess.run(['node', '-e', source + probe], text=True,
                          capture_output=True, timeout=30,
                          env={**os.environ, 'TZ': 'UTC'})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_actual_js_financial_rules_and_performance(html):
    def case(day, price, when='2026-07-26', team_day=20, **extra):
        return {'member': {'billing_day': day, 'price': price, 'payments': [], **extra},
                'date': when, 'team_day': team_day}

    cases = [
        case(1, 260),
        case(1, 999, payments=[{'month':'2026-07','paid':True,'amount':200}]),
        case(20, 1300), case(30, 260),
        case(30, 260, '2026-07-30'), case(31, 260, '2026-02-28'),
        case(30, 260, '2026-08-29'), case(None, 260), case(1, None),
        case(1, 260, team_day=None),
        case(1, 260, '2026-02-10'),
        case(1, 800, '2026-02-20'),
        case(31, 799.99, '2024-02-28', team_day=31),
        case(1, 800, '2026-12-31', team_day=31),
        case(1, 0),
    ]
    out = run_frontend(html, cases)
    for c, result in zip(cases, out['results'], strict=True):
        backend = member_metrics(c['member'], date.fromisoformat(c['date']))
        frontend = result['metrics']
        assert 'refund' not in frontend and 'ban' not in frontend
        if backend['profit'] is None:
            assert frontend['profit'] is None, (c, result)
        else:
            for key in ['total', 'days', 'daily', 'monthly', 'cost']:
                assert abs(frontend['profit'][key] - backend['profit'][key]) < 1e-8, (key, c, result)
            for key in ['start', 'tier']:
                assert frontend['profit'][key] == backend['profit'][key], (key, c, result)
    rows = out['results']
    std_cost = 27 * 6.8
    assert abs(rows[0]['metrics']['profit']['total'] - (260-std_cost)/30*26) < 1e-8
    assert rows[1]['metrics']['lastPaidMonth'] == '2026-07'
    assert rows[1]['loss']['costCny'] == '863.60', 'payment amount must not select loss cost tier'
    assert rows[2]['metrics']['profit']['tier'] == 'pro'
    assert rows[3]['metrics']['profit']['start'] == '2026-06-30'
    assert rows[3]['metrics']['profit']['days'] == 27
    assert rows[4]['metrics']['profit']['days'] == 1
    assert rows[5]['metrics']['profit']['start'] == '2026-02-28'
    assert rows[5]['metrics']['profit']['days'] == 1
    assert rows[6]['metrics']['profit']['elapsedDays'] == 31
    assert rows[6]['metrics']['profit']['days'] == 30
    assert rows[7]['metrics']['profit'] is None and rows[7]['loss']['ok']
    assert rows[8]['metrics']['profit'] is None and not rows[8]['loss']['ok']
    assert rows[9]['metrics']['profit'] is not None and not rows[9]['loss']['ok']
    assert rows[10]['loss']['amountCny'] == '59.23'
    assert rows[10]['loss']['cycleDays'] == 31 and rows[10]['loss']['remainingDays'] == 10
    assert rows[11]['loss']['amountCny'] == '863.60', 'Team bill day includes full new cycle'
    assert rows[12]['loss']['costCny'] == '183.60'
    assert rows[12]['loss']['remainingDays'] == 1 and rows[12]['loss']['cycleDays'] == 29
    assert rows[13]['loss']['cycleEnd'] == '2027-01-31'
    assert rows[14]['metrics']['profit'] is not None and rows[14]['loss']['ok']
    small_p95, big_p95 = percentile95(out['small']), percentile95(out['big'])
    assert small_p95 < 5, f'65 synthetic rows p95={small_p95:.3f}ms'
    assert big_p95 < 20, f'2000 synthetic rows p95={big_p95:.3f}ms'
    print(f'PASS real JS financial cases={len(cases)}; 65 rows p95={small_p95:.3f}ms; 2000 rows p95={big_p95:.3f}ms')


def test_profit_history_and_booked_loss_independence():
    today = date(2026, 7, 26)
    members = [
        {'id':'a', 'price':260, 'billing_day':1, 'payments':[], 'created_at':'2026-06-18'},
        {'id':'b', 'price':300, 'billing_day':20, 'payments':[], 'created_at':'2026-07-24'},
        {'id':'inactive', 'price':999, 'billing_day':1, 'status':'inactive'},
    ]
    load = lambda _domain: {'members':members}
    before = aggregate_domains(load, ['synthetic.test'], today=today)
    daily = (260 - 27*6.8)/30
    daily2 = (300 - 27*6.8)/30
    expected = {'normal_daily': daily+daily2,
                'normal_profit': daily*26+daily2*7,
                'history_profit': daily*56+daily2*7}
    for key, value in expected.items():
        assert abs(before['totals'][key] - value) < 1e-4, (key,before['totals'],value)
    assert before['totals']['nonrenewal_loss'] == '0.00', 'active previews are never booked losses'
    with tempfile.TemporaryDirectory(prefix='profit-loss-ledger-') as td:
        root = Path(td)
        store = LossStore(root)
        store.enable()
        member = {'id':'deleted', 'price':260, 'billing_day':1}
        data = {'members':[member]}
        roster = root/'data/domains/deleted.test/members.json'
        roster.parent.mkdir(parents=True)
        roster.write_text(json.dumps(data))
        store.delete_members(data, [member], 'deleted.test', 20,
                             lambda payload: roster.write_text(json.dumps(payload)))
        assert json.loads(roster.read_text())['members'] == []
        after = aggregate_domains(load, ['synthetic.test'], today=today, nonrenewal_store=store)
        assert after['totals']['nonrenewal_loss'] != '0.00'
        for key in expected:
            assert after['totals'][key] == before['totals'][key], key
        assert after['nonrenewal_count'] == 1
        assert not {'refund','refund_n','ban_daily','ban_profit','ban_n'} & after['totals'].keys()
        assert all(not {'refund','ban_daily','ban_profit','ban_n'} & row.keys() for row in after['domains'])
    print('PASS daily/cycle/history regression and booked loss independence')


def test_isolated_read_performance(html):
    with tempfile.TemporaryDirectory(prefix='profit-http-perf-') as td:
        root = Path(td) / 'renewal-registry'
        root.mkdir()
        for name in ['server.py','renewal_cli.py','domain_catalog.py','finance_metrics.py','nonrenewal_loss.py','index.html']:
            shutil.copy2(ROOT/name, root/name)
        data = root/'data/domains/perf.test'
        data.mkdir(parents=True)
        members = [{'id':f'synthetic-{i}', 'username':f'Synthetic {i}', 'email':f'synthetic-{i}@example.invalid',
                    'billing_day':20, 'price':260, 'payments':[]} for i in range(65)]
        (data/'members.json').write_text(json.dumps({'members':members, 'meta':{'domain':'perf.test'}}))
        (root/'data/domain_catalog.json').write_text(json.dumps({'default':'perf.test', 'domains':[{'id':'perf.test','billing_day':20}]}))
        launch = "import server; h=server.OptimizedThreadingHTTPServer(('127.0.0.1',0),server.Handler); print(h.server_port,flush=True); h.serve_forever()"
        with (root/'server.log').open('w+') as log:
            proc = subprocess.Popen([sys.executable,'-u','-c',launch], cwd=root, stdout=subprocess.PIPE, stderr=log, text=True)
            try:
                port_line = proc.stdout.readline().strip()
                assert port_line.isdecimal(), 'isolated server did not publish its ephemeral port'
                base = f'http://127.0.0.1:{int(port_line)}'
                with urllib.request.urlopen(base+'/', timeout=5) as response:
                    body = response.read()
                assert body.decode() == html
                assert len(body) < 200_000, len(body)
                samples = []
                for _ in range(31):
                    start = time.perf_counter()
                    with urllib.request.urlopen(base+'/api/data?domain=perf.test', timeout=5) as response:
                        payload = json.load(response)
                    assert payload['data']['members'] == members
                    samples.append((time.perf_counter()-start)*1000)
                p95 = percentile95(samples[1:])
                assert p95 < 50, p95
                print(f'PASS isolated HTTP synthetic members=65 p95={p95:.3f}ms; HTML bytes={len(body)}')
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=5)
                proc.stdout.close()


def main():
    html = (ROOT/'index.html').read_text()
    assert '累计盈利' in html and '预计删除损失' in html and '不续费损失合计' in html
    assert 'id="kpi-nonrenewal-loss"' in html and 'id="kpi-refund"' not in html
    assert '应退金额' not in html and 'function refundInfo' not in html and 'function profitInfo' not in html
    assert 'id="kpi-ban-daily"' not in html and 'id="kpi-ban-profit"' not in html
    assert all(token in html for token in ['单日盈利','周期盈利','总累计盈利','跨周期长期毛利'])
    test_actual_js_financial_rules_and_performance(html)
    test_profit_history_and_booked_loss_independence()
    test_isolated_read_performance(html)
    print('PASS profit/loss render performance: all regression sections')


if __name__ == '__main__':
    main()
