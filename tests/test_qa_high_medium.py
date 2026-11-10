"""Behavioral regressions for the November backend QA; synthetic source-only fixtures."""
import json, os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
SOURCE = Path(__file__).resolve().parents[1]
PRELUDE = '''
import concurrent.futures, copy, http.client, json, socket, threading, time
from pathlib import Path
from unittest.mock import patch
import server as s
import renewal_cli as cli
h = None
def serve():
    global h
    h = s.OptimizedThreadingHTTPServer(('127.0.0.1',0),s.Handler)
    threading.Thread(target=h.serve_forever,daemon=True).start()
    return h.server_port
def call(port,path,body=None,headers=None):
    c=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
    c.request('POST' if body is not None else 'GET',path,body,headers or {})
    r=c.getresponse(); result=(r.status,r.read(),dict(r.getheaders())); c.close(); return result
'''
class Regressions(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='renewal-qa-fix-')
        self.root=Path(self.tmp.name)/'renewal-registry'; self.root.mkdir()
        for p in SOURCE.iterdir():
            if p.suffix in ('.py','.html'): shutil.copy2(p,self.root/p.name)
        data=self.root/'data'; data.mkdir()
        (data/'domain_catalog.json').write_text(json.dumps({'default':'alpha.test','domains':[{'id':d,'label':d,'billing_day':8} for d in ('alpha.test','beta.test')]}))
        for d in ('alpha.test','beta.test'):
            p=data/'domains'/d/'members.json'; p.parent.mkdir(parents=True)
            p.write_text(json.dumps({'meta':{'domain':d},'members':[{'id':'old','email':'old@'+d,'username':'old','price':300,'billing_day':8,'status':'active','payments':[]}]}))
    def tearDown(self): self.tmp.cleanup()
    def run_code(self,code):
        p=subprocess.run([sys.executable,'-c',PRELUDE+'\n'+code],cwd=self.root,capture_output=True,text=True,timeout=25)
        self.assertEqual(p.returncode,0,p.stdout+'\n'+p.stderr)
    def test_B1_other_domain_missing_unchanged(self):
        self.run_code('''
x=s.Handler.__new__(s.Handler)
x._import_one_upload([{'id':'a','email':'admin@alpha.test','username':'a'},{'id':'b','email':'b@beta.test','username':'b'}],filename='alpha.test.zip',mark_missing=True,domain_hint='auto',auto_requested=True)
assert s.load_data('beta.test')['members'][0]['status']=='active'
assert s.load_data('alpha.test')['members'][0]['status']=='missing_in_export'
''')
    def test_B2_manual_upgrade_preserves_payments(self):
        self.run_code('''
d={'meta':{},'members':[{'id':'manual-123','email':'x@example.com','username':'x','price':77,'payments':[{'paid':True}]}]}
cli.sync_members_from_users(d,[{'id':'uuid-real','email':'x@example.com','username':'x'}])
assert len(d['members'])==1 and d['members'][0]['id']=='uuid-real'
assert d['members'][0]['price']==77 and d['members'][0]['payments']==[{'paid':True}]
try: cli.sync_members_from_users(d,[{'id':'other-real','email':'x@example.com'}]); assert False
except ValueError: pass
''')
    def test_B3_identityless_rejected_atomically(self):
        self.run_code('''
d=s.load_data(); old=copy.deepcopy(d)
try: cli.sync_members_from_users(d,[{'id':'ok','email':'ok@example.com'},{'username':'unstable'}]); assert False
except ValueError: pass
assert d==old
''')
    def test_B4_csv_conflicting_explicit_domain(self):
        self.run_code('''
u=cli.CSVMembershipUsers([{'id':'','email':'x@example.com','username':'x'}],'beta.test')
x=s.Handler.__new__(s.Handler)
try: x._import_one_upload(u,filename='members.csv',mark_missing=False,domain_hint='alpha.test',auto_requested=False); assert False
except ValueError: pass
assert len(s.load_data('alpha.test')['members'])==1
''')
    def test_B4_cli_owner_route(self):
        self.run_code('''
p=Path('members.csv'); p.write_text('Name,Email,Role,Status,Seat Tier\\nOwner,owner@beta.test,Primary Owner,Active,Standard\\nUser,x@example.com,User,Active,Standard\\n')
cli.main(['sync',str(p)])
assert len(s.load_data('alpha.test')['members'])==1
assert len(s.load_data('beta.test')['members'])==2
try: cli.main(['--domain','alpha.test','sync',str(p)]); assert False
except (ValueError,SystemExit): pass
''')
    def test_B17_idle_header_timeout(self):
        self.run_code('''
s.REQUEST_TIMEOUT_SECONDS=.2
p=serve(); c=socket.create_connection(('127.0.0.1',p),timeout=2); c.sendall(b'GET / HTTP/1.1\\r\\nHost: x\\r\\n')
time.sleep(.5); assert c.recv(1)==b''; c.close()
''')
    def test_B17_connection_admission(self):
        self.run_code('''
s.MAX_CONNECTIONS=2; s.REQUEST_TIMEOUT_SECONDS=3
p=serve(); sockets=[]
for _ in range(2):
 c=socket.create_connection(('127.0.0.1',p),timeout=1); c.sendall(b'GET / HTTP/1.1\\r\\n'); sockets.append(c)
time.sleep(.1)
c=socket.create_connection(('127.0.0.1',p),timeout=1)
try: assert c.recv(1)==b''
except ConnectionResetError: pass
for x in sockets+[c]: x.close()
''')
    def test_B18_json_size_rejected_before_read(self):
        self.run_code('''
p=serve(); c=http.client.HTTPConnection('127.0.0.1',p,timeout=2)
c.putrequest('POST','/api/sync-export'); c.putheader('Content-Type','application/json'); c.putheader('Content-Length',100*1024*1024); c.endheaders()
r=c.getresponse(); assert r.status==413; r.read(); c.close()
''')
    def test_B18_json_uses_parse_queue(self):
        self.run_code('''
p=serve()
with patch.object(s.UPLOAD_PARSE_QUEUE,'submit',wraps=s.UPLOAD_PARSE_QUEUE.submit) as submit:
 code,raw,_=call(p,'/api/sync-export?domain=alpha.test',b'{"users":[{"uuid":"new","email_address":"new@example.com"}]}',{'Content-Type':'application/json'})
 assert code==200,(code,raw)
 assert submit.call_count==1,submit.call_count
''')
    def test_B18_negative_length(self):
        self.run_code('''
p=serve(); code,_,_=call(p,'/api/sync-export',b'',{'Content-Type':'application/json','Content-Length':'-1'}); assert code==400
''')
    def test_B23_multipart_missing_boundary(self):
        self.run_code('''
p=serve(); body=b'--x\\r\\nContent-Disposition: form-data; name="file"; filename="users.json"\\r\\n\\r\\n[]'
code,_,_=call(p,'/api/sync-export',body,{'Content-Type':'multipart/form-data; boundary=x'}); assert code==400
''')
    def test_B20_bootstrap_single_flight(self):
        self.run_code('''
s.get_finance_cache(); real=s.gzip.compress
with patch.object(s.gzip,'compress',side_effect=lambda *a,**kw:(time.sleep(.05),real(*a,**kw))[1]) as compress:
 with concurrent.futures.ThreadPoolExecutor(12) as ex: rows=list(ex.map(lambda _:s.bootstrap_response_payload(),range(12)))
 assert len({r[2] for r in rows})==1
 assert compress.call_count==1,compress.call_count
''')
    def test_B19_bootstrap_retries_stale_finance(self):
        self.run_code('''
s.get_finance_cache(); real=s.get_finance_cache; calls=0
s.now_iso=lambda:'2026-11-05T00:00:00Z'
def racing():
 global calls
 f=real(); calls+=1
 if calls==1:
  d=s.load_data(); d['members'][0]['price']=900; s.save_data(d)
 return f
with patch.object(s,'get_finance_cache',side_effect=racing): body,_,_=s.bootstrap_response_payload()
obj=json.loads(body); assert obj['data']['alpha.test']['members'][0]['price']==900
assert obj['finance']==real(), 'mixed source versions in bootstrap'
''')
    def test_B20_domain_single_flight_and_mutation_isolation(self):
        self.run_code('''
original=s.load_data(); private=s.load_data(); private['members'][0]['price']=999
assert s.load_data()==original
real=s.gzip.compress
with patch.object(s.gzip,'compress',side_effect=lambda *a,**kw:(time.sleep(.03),real(*a,**kw))[1]) as compress:
 with concurrent.futures.ThreadPoolExecutor(12) as ex: rows=list(ex.map(lambda _:s.domain_response_payload('alpha.test'),range(12)))
 assert len({r[2] for r in rows})==1 and compress.call_count==1
''')
    def test_B19_same_second_finance_revision(self):
        self.run_code('''
s.now_iso=lambda:'2026-11-05T00:00:00Z'
s.recompute_finance_cache(reason='same'); old=s.bootstrap_response_payload()
real=s.aggregate_domains
def changed(*a,**kw):
 result=real(*a,**kw); result['totals']['normal_daily']=12345; return result
with patch.object(s,'aggregate_domains',side_effect=changed): s.recompute_finance_cache(reason='same')
new=s.bootstrap_response_payload()
assert json.loads(new[0])['finance']['totals']['normal_daily']==12345
assert old[2]!=new[2]
''')
    def test_B18_pathological_json_count_is_bounded(self):
        self.run_code('''
p=serve(); raw=b'['+b'{},'*50000+b'{}]'
code,_,_=call(p,'/api/sync-export',raw,{'Content-Type':'application/json'}); assert code==400
''')
    def test_B18_json_limit_cannot_be_bypassed_by_upload_type(self):
        self.run_code('\nimport zipfile\np=Path(\'large.json\');p.write_text(\'[{"uuid":"u","full_name":"\'+\'x\'*(3*1024*1024)+\'"}]\')\ntry:s.extract_users_from_upload_path(p.name,p);assert False\nexcept s.UploadTooLargeError:pass\nz=Path(\'large.zip\')\nwith zipfile.ZipFile(z,\'w\',zipfile.ZIP_DEFLATED) as out:out.write(p,\'users.json\')\ntry:s.extract_users_from_upload_path(z.name,z);assert False\nexcept s.UploadTooLargeError:pass\n')
    def test_B19_catalog_change_during_bootstrap(self):
        self.run_code('''
real=s.get_domain_catalog; calls=0
def racing():
 global calls
 result=real();calls+=1
 if calls==1:
  s.DOMAIN_MANAGER.add('gamma.test')
 return result
with patch.object(s,'get_domain_catalog',side_effect=racing):body,_,_=s.bootstrap_response_payload()
obj=json.loads(body)
assert {x['id'] for x in obj['domains']}==set(obj['data'])
assert 'gamma.test' in obj['data']
''')
    def test_B19_atomic_replace_same_mtime_and_size(self):
        self.run_code('''
import os
original=s.load_data();p=s.data_path('alpha.test');st=p.stat()
text=p.read_text().replace('300','900');tmp=p.with_suffix('.swap');tmp.write_text(text)
os.utime(tmp,ns=(st.st_atime_ns,st.st_mtime_ns));os.replace(tmp,p)
assert s.load_data()['members'][0]['price']==900
''')
    def test_B20_streamed_write_is_atomic_and_preserves_input(self):
        self.run_code("\nd=s.load_data(); before=copy.deepcopy(d);s.save_data(d)\nassert d==before\npath=s.data_path('alpha.test');committed=path.read_bytes()\nassert not (s.DATA_DIR/'members.json').exists(), 'legacy mirror must not be written'\nbroken=copy.deepcopy(d);broken['members'][0]['price']=float('nan')\ntry:s.save_data(broken);assert False\nexcept ValueError:pass\nassert path.read_bytes()==committed\nassert not list(path.parent.glob('.*.tmp'))\nwith patch.object(s.json,'dumps',side_effect=AssertionError('whole document serialization')):\n d['members'][0]['notes']='streamed';s.save_data(d)\nassert s.load_data()['members'][0]['notes']=='streamed'\n")
    def test_B20_ledger_cache_external_updates_and_orphans(self):
        self.run_code("""
s.load_data('alpha.test');s.load_data('beta.test')
with patch.object(s.json,'load',side_effect=AssertionError('cached roster reparsed')):
 assert s.loss_store().summary()['count']==0
path=s.data_path('alpha.test');data=json.loads(path.read_text())
data['loss_events']=[{'event_id':'external','amount_cny':'3.25'}]
tmp=path.with_suffix('.swap');tmp.write_text(json.dumps(data));tmp.replace(path)
orphan=path.parent.parent/'orphan.test'/'members.json';orphan.parent.mkdir()
orphan.write_text(json.dumps({'members':[],'loss_events':[{'event_id':'orphan','amount_cny':'2.00'}]}))
assert s.loss_store().summary()['total_cny']=='5.25'
assert s.loss_store().summary()['count']==2
orphan.write_text('{broken')
assert s.loss_store().summary()['total_cny']=='5.25'
""")
if __name__=='__main__': unittest.main(verbosity=2)
