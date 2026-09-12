"""Source-only random-port HTTP/CLI integration; never import a live server."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
import urllib.error

SOURCE = Path(__file__).resolve().parents[1]

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='renewal-loss-')
        self.root = Path(self.tmp.name)/'renewal-registry'; self.root.mkdir()
        for path in SOURCE.glob('*.py'): shutil.copy2(path,self.root/path.name)
        shutil.copy2(SOURCE/'index.html',self.root/'index.html')
        p=self.root/'data/domain_catalog.json'; p.parent.mkdir()
        p.write_text(json.dumps({'default':'lsznode.de','domains':[{'id':'lsznode.de','label':'lsznode.de','billing_day':20},{'id':'team.test','label':'team.test','billing_day':31}]}))
        self.member={'id':'sample','username':'Sample','email':'sample@team.test','billing_day':1,'price':800,'payments':[]}
        for domain in ['lsznode.de','team.test']:
            p=self.root/f'data/domains/{domain}/members.json'; p.parent.mkdir(parents=True)
            p.write_text(json.dumps({'meta':{'domain':domain},'members':[self.member]}))
        self.start()
    def start(self):
        self.log=(self.root/'http.log').open('a')
        self.proc=subprocess.Popen([sys.executable,'-u','-c',"import server; h=server.OptimizedThreadingHTTPServer(('127.0.0.1',0),server.Handler); print(h.server_port,flush=True); h.serve_forever()"],cwd=self.root,stdout=subprocess.PIPE,stderr=self.log,text=True)
        self.port=int(self.proc.stdout.readline().strip())
    def stop(self):
        self.proc.terminate(); self.proc.wait(timeout=10); self.proc.stdout.close(); self.log.close()
    def tearDown(self):
        self.stop(); self.tmp.cleanup()
    def api(self,path,body=None):
        request=urllib.request.Request(f'http://127.0.0.1:{self.port}'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=10) as response: return response.status,json.load(response)
        except urllib.error.HTTPError as e: return e.code,json.load(e)
    def run_cli(self,*args):
        return subprocess.run([sys.executable,*args],cwd=self.root,text=True,capture_output=True,timeout=15)
    def test_api_and_cli_use_permanent_ledger_and_restart(self):
        status,body=self.api('/api/nonrenewal-loss')
        self.assertEqual(status,200); self.assertEqual(body['total_cny'],'0.00'); self.assertIsNone(body['enabled_at'])
        status,body=self.api('/api/delete-member',{'domain':'team.test','id':'sample'})
        self.assertEqual(status,400); self.assertIn('尚未启用',body['error'])
        enabled=self.run_cli('nonrenewal_loss.py','enable')
        self.assertEqual(enabled.returncode,0,enabled.stderr)
        status,body=self.api('/api/delete-member',{'domain':'team.test','id':'sample'})
        self.assertEqual(status,200,body); self.assertIn('loss_event',body)
        event=body['loss_event']; self.assertEqual(event['team_billing_day'],31)
        self.assertEqual(event['cost_cny'],'863.60')
        self.assertEqual(self.api('/api/delete-member',{'domain':'team.test','id':'sample'})[0],404)
        cli=self.run_cli('renewal_cli.py','--domain','lsznode.de','remove','sample')
        self.assertEqual(cli.returncode,0,cli.stderr)
        self.assertNotEqual(self.run_cli('renewal_cli.py','--domain','lsznode.de','remove','sample').returncode,0)
        self.stop(); self.start()
        status,details=self.api('/api/nonrenewal-loss')
        self.assertEqual(status,200); self.assertEqual(details['count'],2)
        self.assertEqual({e['domain'] for e in details['events']},{'team.test','lsznode.de'})
        status,finance=self.api('/api/finance-summary?force=1')
        self.assertEqual(finance['totals']['nonrenewal_loss'],details['total_cny'])
        self.assertNotIn('refund',finance['totals'])
        self.assertEqual(finance['totals']['normal_profit'],0)

    def test_stale_finance_cache_and_cli_eventual_read(self):
        self.stop()
        (self.root/'data/finance_cache.json').write_text(json.dumps({'ok':True,'totals':{'refund':999}}))
        self.start()
        status, summary=self.api('/api/finance-summary')
        self.assertEqual(status,200)
        self.assertNotIn('refund',summary['totals'])
        self.assertEqual(summary['nonrenewal_count'],0)
        self.assertEqual(self.run_cli('nonrenewal_loss.py','enable').returncode,0)
        result=self.run_cli('renewal_cli.py','--domain','team.test','remove','sample')
        self.assertEqual(result.returncode,0,result.stderr)
        status, summary=self.api('/api/finance-summary')
        details=self.api('/api/nonrenewal-loss')[1]
        self.assertEqual(summary['totals']['nonrenewal_loss'],details['total_cny'])
        self.assertEqual(summary['nonrenewal_count'],1)
        self.assertEqual(summary['nonrenewal_enabled_at'],details['enabled_at'])

    def test_configuration_and_batch_failures_remove_nothing(self):
        self.assertEqual(self.run_cli('nonrenewal_loss.py','enable').returncode,0)
        self.assertEqual(self.api('/api/update-member',{'domain':'team.test','id':'sample','price':None})[0],200)
        self.assertEqual(self.api('/api/delete-member',{'domain':'team.test','id':'sample'})[0],400)
        self.assertNotEqual(self.run_cli('renewal_cli.py','--domain','team.test','remove','sample').returncode,0)
        self.assertEqual(len(self.api('/api/data?domain=team.test')[1]['data']['members']),1)
        self.api('/api/update-member',{'domain':'team.test','id':'sample','price':800})
        self.api('/api/domain-management',{'action':'set_billing_day','domain':'team.test','billing_day':None})
        self.assertEqual(self.api('/api/delete-member',{'domain':'team.test','id':'sample'})[0],400)
        for args in [('sample','absent'),('sample','sample'),('sample','sample@team.test')]:
            result=self.run_cli('renewal_cli.py','--domain','lsznode.de','remove',*args)
            self.assertNotEqual(result.returncode,0)
        self.assertEqual(len(self.api('/api/data?domain=lsznode.de')[1]['data']['members']),1)
        self.assertEqual(self.api('/api/nonrenewal-loss')[1]['count'],0)

    def test_duplicate_display_name_and_conflicting_identity_rejected(self):
        self.assertEqual(self.run_cli('nonrenewal_loss.py','enable').returncode,0)
        # Synthetic legacy duplicate-name roster, not possible through add API.
        path=self.root/'data/domains/team.test/members.json'
        data=json.loads(path.read_text())
        data['members'][0]['username']='Twin'
        data['members'].append({**self.member,'username':'Twin','id':'second','email':'second@team.test'})
        path.write_text(json.dumps(data))
        self.assertEqual(self.api('/api/delete-member',{'domain':'team.test','username':'Twin'})[0],400)
        self.assertEqual(self.api('/api/delete-member',{'domain':'team.test','id':'sample','email':'second@team.test'})[0],400)
        self.assertNotEqual(self.run_cli('renewal_cli.py','--domain','team.test','remove','Twin').returncode,0)
        self.assertEqual(self.api('/api/nonrenewal-loss')[1]['count'],0)
        # Exact id removes that seat, not the first same-name row.
        status,body=self.api('/api/delete-member',{'domain':'team.test','id':'second'})
        self.assertEqual(status,200,body)
        self.assertEqual(body['loss_event']['member_id'],'second')
        self.assertEqual(self.api('/api/data?domain=team.test')[1]['data']['members'][0]['id'],'sample')

    def test_concurrent_http_and_cli_replays_book_once(self):
        import concurrent.futures
        self.assertEqual(self.run_cli('nonrenewal_loss.py','enable').returncode,0)
        def request(i):
            if i%2:
                r=self.run_cli('renewal_cli.py','--domain','team.test','remove','sample')
                return r.returncode==0
            return self.api('/api/delete-member',{'domain':'team.test','id':'sample'})[0]==200
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(request,range(24)))
        self.assertEqual(sum(results),1)
        details=self.api('/api/nonrenewal-loss')[1]
        self.assertEqual(details['count'],1)
        self.assertEqual(self.api('/api/data?domain=team.test')[1]['data']['members'],[])


if __name__=='__main__': unittest.main(verbosity=2)
