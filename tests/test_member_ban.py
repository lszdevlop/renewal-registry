"""Per-member ban: source-only random-port HTTP/CLI tests with synthetic rosters."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_nonrenewal_http_cli as harness


class MemberBanTests(unittest.TestCase):
    root: Path
    port: int
    setUp = harness.IntegrationTests.setUp
    start = harness.IntegrationTests.start
    stop = harness.IntegrationTests.stop
    tearDown = harness.IntegrationTests.tearDown
    api = harness.IntegrationTests.api
    run_cli = harness.IntegrationTests.run_cli

    def ban(self, domain='team.test', member_id='sample'):
        status, body = self.api('/api/ban-member', {
            'domain': domain, 'id': member_id, 'confirm_id': member_id, 'reason': '封号'})
        self.assertEqual(status, 200, body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['domain'], domain)
        return body['data']

    def test_http_snapshot_preserves_member_and_excludes_all_profit(self):
        before = self.api('/api/data?domain=team.test')[1]['data']
        finance = self.api('/api/finance-summary?force=1')[1]
        contribution = next(d for d in finance['domains'] if d['domain'] == 'team.test')
        self.assertEqual(self.run_cli('nonrenewal_loss.py', 'enable').returncode, 0)
        ledger = self.root/'data/nonrenewal_loss.json'
        ledger_before = ledger.read_bytes()
        data = self.ban()
        member = data['members'][0]
        self.assertEqual(member['status'], 'banned')
        for field, value in before['members'][0].items():
            if field not in ('status', 'updated_at'):
                self.assertEqual(member[field], value)
        self.assertEqual(data['meta']['archived'], False)
        snap = member['ban_snapshot']
        self.assertEqual(snap['banned_at'], member['banned_at'])
        self.assertTrue(member['banned_at'].endswith('Z'))
        self.assertEqual(snap['reason'], '封号')
        self.assertEqual(snap['as_of_date'], finance['as_of_date'])
        for key in ('normal_daily', 'normal_profit', 'history_profit'):
            self.assertEqual(snap['totals'][key], contribution[key])
        path = self.root/'data/domains/team.test/members.json'
        saved = path.read_bytes()
        self.assertEqual(self.ban(), data)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.api('/api/data?domain=team.test')[1]['data'], data)
        post = self.api('/api/finance-summary')[1]
        for key in ('normal_daily', 'normal_profit', 'history_profit'):
            self.assertAlmostEqual(post['totals'][key], finance['totals'][key] - contribution[key], places=4)
        self.assertEqual(post['totals']['member_n'], 1)
        self.assertEqual(ledger.read_bytes(), ledger_before)
        self.stop(); self.start()
        self.assertEqual(self.api('/api/data?domain=team.test')[1]['data'], data)

    def test_notes_only_http_cli_and_authoritative_save_guards(self):
        self.ban()
        path = self.root/'data/domains/team.test/members.json'
        blocked = [
            ('toggle-paid', {'month': '2026-10', 'paid': True}),
            ('delete-member', {}),
            *[('update-member', {field: value, 'notes': 'not saved'}) for field, value in
              [('price', 800), ('billing_day', 1), ('activation_date', None),
               ('status', 'active'), ('payments', []), ('ban_snapshot', {}), ('unknown', True)]],
            ('add-member', {'username': 'Other', 'email': 'other@team.test'}),
        ]
        for endpoint, fields in blocked:
            before = path.read_bytes()
            status, body = self.api('/api/'+endpoint, {'domain': 'team.test', 'id': 'sample', **fields})
            self.assertEqual(status, 400, (endpoint, fields, body))
            self.assertEqual(path.read_bytes(), before)
        for command in [
            ('set', 'sample', '--price', '800'), ('set', 'sample', '--status', 'active'),
            ('set', 'sample', '--day', '1', '--notes', 'no'), ('paid', '2026-10', 'sample'),
            ('unpaid', '2026-10', 'sample'), ('remove', 'sample'),
            ('add', '--id', 'sample', '--username', 'Different', '--email', 'different@team.test')]:
            before = path.read_bytes()
            result = self.run_cli('renewal_cli.py', '--domain', 'team.test', *command)
            self.assertNotEqual(result.returncode, 0, (command, result.stdout))
            self.assertEqual(path.read_bytes(), before)
        frozen = json.loads(path.read_text())['members'][0]['ban_snapshot']
        self.assertEqual(self.api('/api/update-member', {'domain': 'team.test', 'id': 'sample', 'notes': 'HTTP'})[0], 200)
        result = self.run_cli('renewal_cli.py', '--domain', 'team.test', 'set', 'sample', '--notes', 'CLI')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(path.read_text())['members'][0]['notes'], 'CLI')
        self.assertEqual(json.loads(path.read_text())['members'][0]['ban_snapshot'], frozen)
        result = self.run_cli('-c', '''
import server, renewal_cli
for loader, saver in [(server.load_data, server.save_data), (renewal_cli.load, renewal_cli.save)]:
 for change in ('price', 'status', 'snapshot', 'delete', 'duplicate', 'new-ban'):
  d=loader('team.test')
  if change=='price': d['members'][0]['price']=1
  if change=='status': d['members'][0]['status']='active'
  if change=='snapshot': d['members'][0]['ban_snapshot']['totals']['normal_profit']=1
  if change=='delete': d['members']=[]
  if change=='duplicate': d['members'].append(dict(d['members'][0], id='new', status='active'))
  if change=='new-ban': d['members'].append({'id':'new','status':'banned'})
  for kwargs in ({}, {'notes_only':True}):
   try: saver(d,'team.test',**kwargs)
   except ValueError: pass
   else: raise AssertionError((saver.__name__,change))
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in ('board', 'unpaid'):
            args = (command,) if command == 'board' else (command, '2026-10')
            result = self.run_cli('renewal_cli.py', '--domain', 'team.test', *args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('sample@team.test', result.stdout)

    def test_direct_status_bypass_rejected_and_exact_confirmation(self):
        path = self.root/'data/domains/team.test/members.json'
        before = path.read_bytes()
        for fields in [{'status': 'banned'}, {'status': 'banned', 'notes': 'not saved'},
                       {'ban_snapshot': {}, 'notes': 'not saved'}, {'banned_at': '2026-01-01', 'notes': 'no'}]:
            status, body = self.api('/api/update-member', {'domain': 'team.test', 'id': 'sample', **fields})
            self.assertEqual(status, 400, body)
            self.assertEqual(path.read_bytes(), before)
        command = self.run_cli('renewal_cli.py', '--domain', 'team.test', 'set', 'sample', '--status', 'banned')
        self.assertNotEqual(command.returncode, 0)
        self.assertIn('/api/ban-member', command.stderr)
        for fields in [ {'id':'Sample','confirm_id':'Sample'}, {'id':'sample','confirm_id':'SAMPLE'},
                        {'id':'sample','confirm_id':'sample','reason':'other'}, {'id':'sample'},
                        {'id':'sample@team.test','confirm_id':'sample@team.test'}]:
            status, body = self.api('/api/ban-member', {'domain':'team.test', **fields})
            self.assertIn(status, (400, 404), body)
            self.assertEqual(path.read_bytes(), before)

    def test_loss_pure_function_rejects_banned_and_does_not_book(self):
        self.ban()
        result = self.run_cli('-c', '''
from datetime import date
import server,finance_metrics,nonrenewal_loss
m=server.load_data('team.test')['members'][0]
assert finance_metrics.member_metrics(m,date(2030,1,1)) is None
for day in (None,31):
 try: nonrenewal_loss.calculate_loss(m,day,date(2030,1,1))
 except ValueError as exc: assert '封号' in str(exc)
 else: raise AssertionError('banned loss estimated')
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.api('/api/nonrenewal-loss')[1]['count'], 0)

    def test_import_preflight_atomic_for_mixed_existing_and_new_teams(self):
        self.ban()
        paths = [self.root/'data/domain_catalog.json',
                 self.root/'data/domains/team.test/members.json',
                 self.root/'data/domains/lsznode.de/members.json']
        before = {p: p.read_bytes() for p in paths}
        for users in [
            [{'id':'normal-new','username':'Normal','email':'normal@lsznode.de'},
             {'id':'another','username':'Another','email':'another@lsznode.de'},
             {'id':'sample','username':'Sample','email':'sample@team.test'}],
            [{'id':'admin','username':'Admin','email':'admin@new.test'},
             {'id':'sample','username':'Renamed','email':'sample@team.test'}],
            [{'id':'admin','username':'Admin','email':'admin@new.test'},
             {'id':'sample','username':'Renamed','email':'different@team.test'}],
            [{'id':'admin','username':'Admin','email':'admin@new.test'},
             {'id':'different','username':'Renamed','email':'sample@team.test'}],
        ]:
            status, body = self.api('/api/sync-export', {'domain':'auto','users':users})
            self.assertEqual(status, 400, body)
            for p, content in before.items(): self.assertEqual(p.read_bytes(), content)
            self.assertFalse((self.root/'data/domains/new.test').exists())
            self.assertFalse((self.root.parent/'claude-export-hub/data/domains/new.test').exists())
        # CSV uses email identity without an upstream id.
        import urllib.request
        import urllib.error
        csv = ('Name,Email,Role,Status,Seat Tier\r\n'
               'Owner,owner@team.test,Primary Owner,Active,Standard\r\n'
               'Sample,sample@team.test,User,Active,Standard\r\n')
        request = urllib.request.Request(f'http://127.0.0.1:{self.port}/api/sync-export?domain=auto',
            data=csv.encode(), headers={'Content-Type':'text/csv',
            'Content-Disposition':'attachment; filename="members.csv"'})
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(err.exception.code,400)
        err.exception.close()
        export = self.root/'synthetic.json'
        export.write_text(json.dumps([{'id':'sample','username':'Sample','email':'sample@team.test'}]))
        result = self.run_cli('renewal_cli.py','--domain','team.test','sync',str(export),'--mark-missing')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('封号',result.stderr)
        for p, content in before.items(): self.assertEqual(p.read_bytes(), content)
        # Non-touching imports remain allowed and missing markers never unban.
        status, body = self.api('/api/sync-export', {'domain':'team.test','mark_missing':True,
            'users':[{'id':'new-seat','username':'New Seat','email':'new-seat@team.test'}]})
        self.assertEqual(status,200,body)
        retained = next(m for m in body['data']['members'] if m['id']=='sample')
        self.assertEqual(retained,json.loads(before[paths[1]])['members'][0])
        status, body = self.api('/api/sync-export', {'domain':'auto','users':[
            {'id':'admin','username':'Admin','email':'admin@new.test'},
            {'id':'fresh','username':'Fresh','email':'fresh@lsznode.de'}]})
        self.assertEqual(status,200,body)
        self.assertEqual(set(body['domains_touched']),{'new.test','lsznode.de'})

    def test_snapshot_calendar_and_later_archive_keep_individual_frozen_values(self):
        result = self.run_cli('-c', '''
import copy,json
from datetime import date
from unittest.mock import patch
import server,finance_metrics as fm
p=server.data_path('team.test'); data=json.loads(p.read_text())
data['members'][0].update(billing_day=31,price=300,created_at='2026-01-15',
    payments=[{'month':'2026-02','paid':True,'amount':300,'recorded_at':'2026-02-01T00:00:00Z'}])
p.write_text(json.dumps(data)); original=copy.deepcopy(data['members'][0])
with patch.object(fm,'today_cn',return_value=date(2026,2,28)):
 server.DOMAIN_MANAGER.ban_member('team.test','sample',confirm_id='sample')
frozen=server.load_data('team.test')['members'][0]
expected=fm.member_metrics(original,date(2026,2,28))
assert frozen['ban_snapshot']['as_of_date']=='2026-02-28'
assert frozen['ban_snapshot']['totals']['normal_profit']==round(expected['profit']['total'],4)
assert frozen['ban_snapshot']['totals']['history_profit']==round(expected['history']['total'],4)
with patch.object(fm,'today_cn',return_value=date(2028,7,15)):
 server.DOMAIN_MANAGER.ban_member('team.test','sample',confirm_id='sample')
 assert server.load_data('team.test')['members'][0]==frozen
 server.DOMAIN_MANAGER.archive('team.test',confirm_domain='team.test')
archived=server.load_data('team.test')
assert archived['members'][0]==frozen
snap=archived['meta']['archive_snapshot']
assert all(snap['totals'][key]==0 for key in ('normal_daily','normal_profit','history_profit'))
assert snap['members']['id:sample']['normal_profit']==frozen['ban_snapshot']['totals']['normal_profit']
assert snap['members']['id:sample']['history_profit']==frozen['ban_snapshot']['totals']['history_profit']
''')
        self.assertEqual(result.returncode,0,result.stderr)
        path = self.root/'data/domains/team.test/members.json'
        snapshot_path = path.with_name('archive_snapshot.json')
        snapshot_bytes = snapshot_path.read_bytes()
        status, body = self.api('/api/ban-member',{'domain':'team.test','id':'sample','confirm_id':'sample'})
        self.assertEqual(status,400,body)
        self.assertIn('封存',body['error'])
        self.assertEqual(self.api('/api/update-member',{'domain':'team.test','id':'sample','notes':'retained'})[0],200)
        self.assertEqual(snapshot_path.read_bytes(),snapshot_bytes)

    def test_concurrent_ban_writes_snapshot_boundary_and_booked_loss_retention(self):
        import concurrent.futures
        self.assertEqual(self.run_cli('nonrenewal_loss.py','enable').returncode,0)
        self.assertEqual(self.api('/api/add-member',{'domain':'team.test','username':'Departed',
            'email':'departed@team.test','price':300})[0],200)
        self.assertEqual(self.api('/api/delete-member',{'domain':'team.test','email':'departed@team.test'})[0],200)
        ledger = self.root/'data/nonrenewal_loss.json'
        ledger_before = ledger.read_bytes()
        def operation(i):
            if i % 3 == 0:
                return self.api('/api/ban-member',{'domain':'team.test','id':'sample','confirm_id':'sample'})[0]
            if i % 3 == 1:
                return self.api('/api/update-member',{'domain':'team.test','id':'sample','price':300+i})[0]
            return self.run_cli('renewal_cli.py','--domain','team.test','set','sample','--price',str(300+i)).returncode
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            statuses = list(pool.map(operation, range(24)))
        self.assertTrue(all(code in (0,1,200,400) for code in statuses),statuses)
        result = self.run_cli('-c', '''
import copy
from datetime import date
import server,finance_metrics as fm
m=server.load_data('team.test')['members'][0]; assert m['status']=='banned'
s=m['ban_snapshot']; original=copy.deepcopy(m); original['status']='active'
metrics=fm.member_metrics(original,date.fromisoformat(s['as_of_date']))
assert s['totals']['normal_profit']==round(metrics['profit']['total'],4)
assert s['totals']['history_profit']==round(metrics['history']['total'],4)
''')
        self.assertEqual(result.returncode,0,result.stderr)
        before = (self.root/'data/domains/team.test/members.json').read_bytes()
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(operation, range(24)))
        self.assertEqual((self.root/'data/domains/team.test/members.json').read_bytes(),before)
        self.assertEqual(self.api('/api/nonrenewal-loss')[1]['count'],1)
        self.assertEqual(ledger.read_bytes(),ledger_before)
        self.assertEqual(self.api('/api/domain-management',{'action':'archive','domain':'team.test','confirm_domain':'team.test'})[0],200)
        self.assertEqual(ledger.read_bytes(),ledger_before)

    def test_default_mirror_and_defaults_do_not_mutate_frozen_member(self):
        self.ban('lsznode.de')
        canonical = self.root/'data/domains/lsznode.de/members.json'
        mirror = self.root/'data/members.json'
        self.assertEqual(canonical.read_bytes(),mirror.read_bytes())
        before = json.loads(canonical.read_text())['members'][0]
        result = self.run_cli('renewal_cli.py','--domain','lsznode.de','defaults','--day','2','--price','900','--apply')
        self.assertEqual(result.returncode,0,result.stderr)  # only fills empty fields; none here
        self.assertEqual(json.loads(canonical.read_text())['members'][0],before)
        self.assertEqual(canonical.read_bytes(),mirror.read_bytes())
        self.assertEqual(self.api('/api/update-member',{'domain':'lsznode.de','id':'sample','notes':'mirror note'})[0],200)
        self.assertEqual(canonical.read_bytes(),mirror.read_bytes())
        # An empty field must never be default-filled after ban.
        self.assertEqual(self.api('/api/add-member',{'domain':'team.test','username':'Empty',
            'email':'empty@team.test','id':'empty'})[0],200)
        self.ban('team.test','empty')
        path = self.root/'data/domains/team.test/members.json'
        saved = path.read_bytes()
        result = self.run_cli('renewal_cli.py','--domain','team.test','defaults','--day','2','--price','900','--apply')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(path.read_bytes(),saved)

    def test_snapshot_write_failure_and_default_mirror_recovery(self):
        result = self.run_cli('-c', '''
import json
from unittest.mock import patch
import server,domain_catalog
manager=server.DOMAIN_MANAGER
p=server.data_path('team.test'); before=p.read_bytes()
with patch.object(domain_catalog,'_atomic_write_json',side_effect=OSError('disk error')):
 try: manager.ban_member('team.test','sample',confirm_id='sample')
 except OSError: pass
 else: raise AssertionError('failure injection missed')
assert p.read_bytes()==before
real=domain_catalog._atomic_write_json
mirror=server.ROOT/'data/members.json'
def fail_mirror(path,data):
 if path==mirror: raise OSError('mirror write failed')
 return real(path,data)
with patch.object(domain_catalog,'_atomic_write_json',side_effect=fail_mirror):
 try: manager.ban_member('lsznode.de','sample',confirm_id='sample')
 except OSError: pass
 else: raise AssertionError('failure injection missed')
canonical=server.data_path('lsznode.de'); frozen=canonical.read_bytes()
assert json.loads(frozen)['members'][0]['status']=='banned'
manager.ban_member('lsznode.de','sample',confirm_id='sample')
assert canonical.read_bytes()==frozen
assert mirror.read_bytes()==frozen
# Missing original snapshot must fail closed instead of inventing a new amount.
data=json.loads(p.read_text()); data['members'][0]['status']='banned'
p.write_text(json.dumps(data))
try: manager.ban_member('team.test','sample',confirm_id='sample')
except ValueError as exc: assert '快照' in str(exc)
else: raise AssertionError('missing snapshot silently accepted')
''')
        self.assertEqual(result.returncode,0,result.stderr)

    def test_legacy_duplicate_name_normal_member_remains_editable(self):
        path = self.root/'data/domains/team.test/members.json'
        data = json.loads(path.read_text())
        data['members'].append({**data['members'][0], 'id':'twin', 'email':'twin@team.test'})
        path.write_text(json.dumps(data))
        self.ban()
        status, body = self.api('/api/update-member',{'domain':'team.test','id':'twin','price':300})
        self.assertEqual(status,200,body)
        self.assertEqual(body['member']['price'],300)

    def test_import_duplicate_banned_name_rejects_before_new_catalog(self):
        self.ban()
        catalog = self.root/'data/domain_catalog.json'
        path = self.root/'data/domains/team.test/members.json'
        before = {p:p.read_bytes() for p in (catalog,path)}
        status, body = self.api('/api/sync-export',{'domain':'auto','users':[
            {'id':'admin','username':'Owner','email':'admin@fresh.test'},
            {'id':'normal','username':'Normal','email':'normal@fresh.test'},
            {'id':'new-id','username':'Sample','email':'new@team.test'}]})
        self.assertEqual(status,400,body)
        for p,content in before.items(): self.assertEqual(p.read_bytes(),content)
        self.assertFalse((self.root/'data/domains/fresh.test').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
