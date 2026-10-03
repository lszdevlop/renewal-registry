"""Archive contract: only synthetic source-only HTTP/CLI sandboxes."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_nonrenewal_http_cli as harness


class ArchiveTests(unittest.TestCase):
    root: Path
    port: int
    setUp = harness.IntegrationTests.setUp
    start = harness.IntegrationTests.start
    stop = harness.IntegrationTests.stop
    tearDown = harness.IntegrationTests.tearDown
    api = harness.IntegrationTests.api
    run_cli = harness.IntegrationTests.run_cli

    def archive(self, domain='team.test'):
        status, result = self.api('/api/domain-management', {
            'action': 'archive', 'domain': domain,
            'confirm_domain': domain, 'reason': '封号'})
        self.assertEqual(status, 200, result)
        return result

    def test_archived_notes_only_and_mutation_guards(self):
        self.archive()
        path = self.root/'data/domains/team.test/members.json'
        snapshot_path = path.with_name('archive_snapshot.json')
        snapshot = snapshot_path.read_bytes()
        for endpoint, fields in [
            ('toggle-paid', {'month':'2026-10','paid':True}),
            ('add-member', {'username':'New','email':'new@team.test'}),
            ('delete-member', {}),
            *[('update-member', {field:value,'notes':'must not persist'}) for field,value in
              [('price',900),('billing_day',2),('activation_date','2026-10-01'),
               ('status','inactive'),('payments',[]),('members',[]),('unknown',True)]],
        ]:
            before = path.read_bytes()
            status, body = self.api('/api/'+endpoint, {'domain':'team.test','id':'sample',**fields})
            self.assertEqual(status,400,(endpoint,fields,body))
            self.assertIn('封存',body['error'])
            self.assertEqual(path.read_bytes(),before)
        for action, extra in [('delete',{'confirm':True}),('rename',{'new_domain':'new.test','confirm_clear':True}),('set_billing_day',{'billing_day':9})]:
            status, body = self.api('/api/domain-management',{'action':action,'domain':'team.test','confirm_domain':'team.test',**extra})
            self.assertEqual(status,400,body)
            self.assertIn('封存',body['error'])
        status, body = self.api('/api/update-member',{'domain':'team.test','id':'sample','notes':'retained note'})
        self.assertEqual(status,200,body)
        self.assertEqual(json.loads(path.read_text())['members'][0]['notes'],'retained note')
        self.assertNotIn('archive_snapshot',json.loads(path.read_text())['meta'])
        self.assertEqual(snapshot_path.read_bytes(),snapshot)
        commands = [
            ('set','sample','--price','900'), ('set','sample','--notes','bad','--day','2'),
            ('paid','2026-10','sample'), ('unpaid','2026-10','sample'),
            ('remove','sample'), ('defaults','--day','2','--apply'),
            ('add','--username','New','--email','new@team.test'),
            ('sync','missing.json'), ('export-csv',),
        ]
        for command in commands:
            before = path.read_bytes()
            result = self.run_cli('renewal_cli.py','--domain','team.test',*command)
            self.assertNotEqual(result.returncode,0,command)
            self.assertIn('封存',result.stderr,command)
            self.assertEqual(path.read_bytes(),before)
        result = self.run_cli('renewal_cli.py','--domain','team.test','set','sample','--notes','CLI note')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(path.read_text())['members'][0]['notes'],'CLI note')
        self.assertEqual(snapshot_path.read_bytes(),snapshot)
        self.assertEqual(self.run_cli('renewal_cli.py','--domain','team.test','list').returncode,0)

    def test_mixed_import_rejects_before_any_catalog_or_roster_write(self):
        self.archive()
        catalog = self.root/'data/domain_catalog.json'
        active = self.root/'data/domains/lsznode.de/members.json'
        archived = self.root/'data/domains/team.test/members.json'
        before = {p:p.read_bytes() for p in (catalog,active,archived)}
        packages = [
            {'domain':'auto','users':[
                {'id':'a','username':'A','email':'a@lsznode.de'},
                {'id':'b','username':'B','email':'b@lsznode.de'},
                {'id':'c','username':'C','email':'c@team.test'}]},
            {'domain':'auto','users':[
                {'id':'admin','username':'Owner','email':'admin@new.test'},
                {'id':'c','username':'C','email':'c@team.test'}]},
            {'domain':'team.test','users':[{'id':'c','username':'C','email':'c@public.test'}]},
        ]
        for package in packages:
            status, body = self.api('/api/sync-export',package)
            self.assertEqual(status,400,body)
            self.assertIn('封存',body['error'])
            for path, content in before.items(): self.assertEqual(path.read_bytes(),content)
            self.assertFalse((self.root/'data/domains/new.test').exists())
            self.assertFalse((self.root.parent/'claude-export-hub/data/domains/new.test').exists())
        self.archive('lsznode.de')
        status,body=self.api('/api/sync-export',{'domain':'auto','users':[{'id':'u','email':'u@public.test'}]})
        self.assertEqual(status,400,body)

    def test_unknown_archive_and_confirmation_have_no_filesystem_effects(self):
        for body in [
            {'action':'archive','domain':'new.test','confirm_domain':'new.test'},
            {'action':'archive','domain':'team.test'},
            {'action':'archive','domain':'team.test','confirm_domain':'TEAM.TEST'},
            {'action':'archive','domain':'team.test','confirm_domain':'team.test','reason':'other'},
        ]:
            status,result=self.api('/api/domain-management',body)
            self.assertEqual(status,400,result)
        self.assertFalse((self.root/'data/domains/new.test').exists())
        self.assertFalse((self.root.parent/'claude-export-hub/data/domains/new.test').exists())
        self.assertFalse((self.root/'data/domains/team.test/archive_snapshot.json').exists())

    def test_interrupted_catalog_publish_recovers_and_invalidates_domains_cache(self):
        # A snapshot is the durable archive commit, catalog is its projection.
        self.api('/api/domains')
        self.api('/api/data?domain=team.test')
        self.api('/api/finance-summary')
        result=self.run_cli('-c', '''
from unittest.mock import patch
import server
manager=server.DOMAIN_MANAGER
with patch.object(manager, '_write_state', side_effect=OSError('simulated disk error')):
    try: manager.archive('team.test', confirm_domain='team.test')
    except OSError: pass
    else: raise AssertionError('failure injection did not fire')
assert manager.is_archived('team.test')
''')
        self.assertEqual(result.returncode,0,result.stderr)
        snapshot_path=self.root/'data/domains/team.test/archive_snapshot.json'
        before=snapshot_path.read_bytes()
        row=next(r for r in self.api('/api/domains')[1]['domains'] if r['id']=='team.test')
        self.assertTrue(row['archived'])
        self.assertEqual(self.api('/api/toggle-paid',{'domain':'team.test','id':'sample','month':'2026-10'})[0],400)
        self.assertIn('archive_snapshot',self.api('/api/data?domain=team.test')[1]['data']['meta'])
        self.archive()
        self.assertEqual(snapshot_path.read_bytes(),before)
        row=next(r for r in json.loads((self.root/'data/domain_catalog.json').read_text())['domains'] if r['id']=='team.test')
        self.assertTrue(row['archived'])

    def test_snapshot_dates_stable_keys_and_direct_aggregate_skip(self):
        result=self.run_cli('-c', '''
import json
from datetime import date
from unittest.mock import patch
import server, finance_metrics as fm
p=server.data_path('team.test')
data=json.loads(p.read_text())
data['members']=[
 {'id':'keep-ID','username':'Id','email':'id@team.test','billing_day':31,'price':300,'created_at':'2026-01-15'},
 {'username':'Mail','email':'Mixed@team.test','billing_day':1,'price':900},
 {'username':'Exact Name','billing_day':2,'price':None}]
p.write_text(json.dumps(data))
with patch.object(fm,'today_cn',return_value=date(2026,2,28)):
 server.DOMAIN_MANAGER.archive('team.test',confirm_domain='team.test')
snap=server.load_data('team.test')['meta']['archive_snapshot']
assert snap['as_of_date']=='2026-02-28'
assert set(snap['members'])=={'id:keep-ID','email:mixed@team.test','username:Exact Name'}
expected=fm.member_metrics(data['members'][0],date(2026,2,28))
assert snap['members']['id:keep-ID']['normal_profit']==round(expected['profit']['total'],4)
assert snap['members']['id:keep-ID']['history_profit']==round(expected['history']['total'],4)
with patch.object(fm,'today_cn',return_value=date(2028,7,15)):
 server.DOMAIN_MANAGER.archive('team.test',confirm_domain='team.test')
 assert server.load_data('team.test')['meta']['archive_snapshot']==snap
 with patch.object(fm,'member_metrics',side_effect=AssertionError('archived metrics evaluated')):
  summary=fm.aggregate_domains(server.load_data,['team.test'])
 assert all(summary['totals'][key]==0 for key in ('normal_daily','normal_profit','history_profit'))
''')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_http_cli_concurrent_writes_observe_archive_boundary(self):
        import concurrent.futures
        self.assertEqual(self.run_cli('nonrenewal_loss.py','enable').returncode,0)
        def operation(i):
            if i % 3 == 0:
                return self.api('/api/update-member',{'domain':'team.test','id':'sample','price':800+i})[0]
            if i % 3 == 1:
                return self.run_cli('renewal_cli.py','--domain','team.test','set','sample','--price',str(800+i)).returncode
            return self.api('/api/domain-management',{'action':'archive','domain':'team.test','confirm_domain':'team.test'})[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results=list(pool.map(operation,range(24)))
        self.assertTrue(all(code in (0,1,200,400) for code in results),results)
        path=self.root/'data/domains/team.test/members.json'
        before=path.read_bytes()
        snapshot=self.api('/api/data?domain=team.test')[1]['data']['meta']['archive_snapshot']
        result=self.run_cli('-c', '''
import server,finance_metrics as fm
from datetime import date
x=server.load_data('team.test'); s=x['meta']['archive_snapshot']
m=fm.member_metrics(x['members'][0],date.fromisoformat(s['as_of_date']))
assert s['members']['id:sample']['normal_profit']==round(m['profit']['total'],4)
''')
        self.assertEqual(result.returncode,0,result.stderr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(operation,range(16)))
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.api('/api/nonrenewal-loss')[1]['count'],0)
        self.assertEqual(self.api('/api/data?domain=team.test')[1]['data']['meta']['archive_snapshot'],snapshot)

    def test_snapshot_write_failure_does_not_publish_archive(self):
        result=self.run_cli('-c', '''
import server,domain_catalog
from unittest.mock import patch
m=server.DOMAIN_MANAGER
before=m.catalog_path.read_bytes()
with patch.object(domain_catalog,'_atomic_write_json',side_effect=OSError('snapshot write failed')):
 try: m.archive('team.test',confirm_domain='team.test')
 except OSError: pass
 else: raise AssertionError('failure injection did not fire')
assert not m.is_archived('team.test')
assert not m.archive_snapshot_path('team.test').exists()
assert m.catalog_path.read_bytes()==before
''')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_filename_routing_preflight_and_new_domain_with_archived_default(self):
        import io
        import zipfile
        import urllib.request
        import urllib.error
        self.archive()
        active=self.root/'data/domains/lsznode.de/members.json'
        before=active.read_bytes()
        content=io.BytesIO()
        with zipfile.ZipFile(content,'w') as z:
            z.writestr('users.json',json.dumps([
                {'id':'a','username':'A','email':'admin@lsznode.de'},
                {'id':'b','username':'B','email':'b@lsznode.de'},
                {'id':'c','username':'C','email':'c@public.test'}]))
        req=urllib.request.Request(f'http://127.0.0.1:{self.port}/api/sync-export?domain=auto',
            data=content.getvalue(),headers={'Content-Type':'application/zip',
            'Content-Disposition':'attachment; filename="team.test.zip"'})
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req,timeout=10)
        self.assertEqual(err.exception.code,400)
        err.exception.close()
        self.assertEqual(active.read_bytes(),before)
        self.archive('lsznode.de')
        status,body=self.api('/api/sync-export',{'domain':'auto','users':[
            {'id':'a','username':'Admin','email':'admin@new.test'},
            {'id':'p','username':'Public','email':'p@public.test'}]})
        self.assertEqual(status,200,body)
        self.assertEqual(body['domains_touched'],['new.test'])

    def test_missing_archived_roster_never_recreates_empty_shell(self):
        self.archive()
        path=self.root/'data/domains/team.test/members.json'
        path.unlink()  # synthetic recovery scenario only
        status,body=self.api('/api/data?domain=team.test')
        self.assertNotEqual(status,200,body)
        self.assertFalse(path.exists())
        result=self.run_cli('renewal_cli.py','--domain','team.test','list')
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(path.exists())

    def test_archived_csv_and_low_level_save_cannot_bypass_protection(self):
        self.archive()
        import urllib.request
        import urllib.error
        csv = ('Name,Email,Role,Status,Seat Tier\r\n'
               'Owner,owner@team.test,Primary Owner,Active,Standard\r\n'
               'New,new@public.test,User,Active,Standard\r\n').encode()
        path=self.root/'data/domains/team.test/members.json'
        before=path.read_bytes()
        req=urllib.request.Request(f'http://127.0.0.1:{self.port}/api/sync-export?domain=auto',data=csv,
            headers={'Content-Type':'text/csv','Content-Disposition':'attachment; filename="members.csv"'})
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req,timeout=10)
        self.assertEqual(err.exception.code,400)
        err.exception.close()
        self.assertEqual(path.read_bytes(),before)
        result=self.run_cli('-c','''
import server,renewal_cli
for loader,saver in [(server.load_data,server.save_data),(renewal_cli.load,renewal_cli.save)]:
 d=loader('team.test'); d['members'][0]['price']=1
 for kwargs in ({},{'notes_only':True}):
  try: saver(d,'team.test',**kwargs)
  except ValueError as e: assert '封存' in str(e)
  else: raise AssertionError('save bypassed archive')
''')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(path.read_bytes(),before)

    def test_ambiguous_or_conflicting_notes_identity_is_rejected_without_write(self):
        self.archive()
        path=self.root/'data/domains/team.test/members.json'
        before=path.read_bytes()
        status,body=self.api('/api/update-member',{'domain':'team.test','id':'sample',
            'email':'different@team.test','notes':'must not save'})
        self.assertIn(status,(400,404),body)
        self.assertEqual(path.read_bytes(),before)

    def test_snapshot_idempotent_exclusion_and_default_readable(self):
        # Book a real synthetic loss before archive; must never disappear or grow.
        self.assertEqual(self.run_cli('nonrenewal_loss.py', 'enable').returncode, 0)
        self.assertEqual(self.api('/api/add-member', {'domain':'team.test',
            'username':'Departed','email':'departed@team.test','price':300})[0], 200)
        self.assertEqual(self.api('/api/delete-member', {'domain':'team.test',
            'email':'departed@team.test'})[0], 200)
        loss_path = self.root/'data/nonrenewal_loss.json'
        ledger_before = loss_path.read_bytes()
        canonical = self.root/'data/domains/team.test/members.json'
        before = canonical.read_bytes()
        pre = self.api('/api/finance-summary?force=1')[1]
        domain_pre = next(d for d in pre['domains'] if d['domain']=='team.test')
        self.api('/api/data?domain=team.test')  # warm response cache before metadata change
        first = self.archive()
        self.assertEqual(canonical.read_bytes(), before)
        snapshot_path = canonical.with_name('archive_snapshot.json')
        snapshot_bytes = snapshot_path.read_bytes()
        data = self.api('/api/data?domain=team.test')[1]['data']
        self.assertEqual(data['members'], json.loads(before)['members'])
        snapshot = data['meta']['archive_snapshot']
        self.assertEqual(snapshot['reason'], '封号')
        self.assertEqual(snapshot['members']['id:sample']['normal_profit'], domain_pre['normal_profit'])
        for key in ('normal_daily','normal_profit','history_profit'):
            self.assertEqual(snapshot['totals'][key], domain_pre[key])
        row = next(d for d in self.api('/api/domains')[1]['domains'] if d['id']=='team.test')
        self.assertTrue(row['archived'])
        self.assertEqual(row['archive_reason'], '封号')
        self.assertEqual(row['archive_summary']['member_n'], 1)
        self.assertEqual(row['archived_at'], snapshot['archived_at'])
        self.archive()
        self.assertEqual(snapshot_path.read_bytes(), snapshot_bytes)
        self.assertEqual(canonical.read_bytes(), before)
        self.assertEqual(loss_path.read_bytes(), ledger_before)
        post = self.api('/api/finance-summary')[1]
        for key in ('normal_daily','normal_profit','history_profit'):
            self.assertAlmostEqual(post['totals'][key], domain_pre[key], places=4)
        self.assertEqual(post['totals']['nonrenewal_loss'], pre['totals']['nonrenewal_loss'])
        self.archive('lsznode.de')
        self.assertEqual(self.api('/api/data')[0], 200)
        self.assertEqual(len(self.api('/api/data')[1]['data']['members']), 1)
        post = self.api('/api/finance-summary')[1]
        for key in ('normal_daily','normal_profit','history_profit'):
            self.assertEqual(post['totals'][key], 0)
        self.stop(); self.start()
        self.assertEqual(self.api('/api/data?domain=team.test')[1]['data']['meta']['archive_snapshot'], snapshot)
        self.assertEqual(snapshot_path.read_bytes(), snapshot_bytes)


if __name__ == '__main__':
    unittest.main(verbosity=2)
