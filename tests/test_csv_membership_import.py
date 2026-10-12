#!/usr/bin/env python3
"""CSV contract, source-only sandbox; never import the production module/store."""
import copy
import io
import csv
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from test_upload_streaming_queue import SOURCE, load_sandbox_server

HEADER = ['Name', 'Email', 'Role', 'Status', 'Seat Tier']
ROWS = [['', 'boss@csv-team.example', 'Primary Owner', 'Active', 'Premium'],
        ['Doe, Jane', 'jane@lsznode.de', 'User', 'Active', 'Premium'],
        ['', 'blank@public.example', 'User', 'Active', 'Standard'],
        ['Admin', 'admin@other.example', 'Admin', 'Active', 'Standard']]


def encoded(rows=ROWS, header=HEADER):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return ('\ufeff' + out.getvalue()).encode('utf-8')


class CSVTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.server = load_sandbox_server()

    @classmethod
    def tearDownClass(cls):
        cls.server.UPLOAD_PARSE_QUEUE.shutdown()
        shutil.rmtree(cls.root, ignore_errors=True)
        shutil.rmtree(cls.root.parent / (cls.root.name + '-unused'), ignore_errors=True)

    def parse(self, blob):
        path = self.root / 'test.csv'
        path.write_bytes(blob)
        return self.server.extract_users_from_upload_path('test.CSV', path)

    def test_validation_rejects_before_any_catalog_write(self):
        before = (self.root / 'data/domain_catalog.json').read_bytes()
        cases = [encoded(ROWS[1:]), encoded(ROWS + [['', 'owner2@csv-team.example', 'Primary Owner', 'Active', '']]),
                 encoded(ROWS + [ROWS[1]]), encoded(ROWS, HEADER[:-1]),
                 encoded([ROWS[0], ['x', 'bad email@example.com', 'User', 'Active', '']]),
                 encoded([ROWS[0], ['x', 'x@example.com', 'User', '', '']]),
                 encoded([ROWS[0], ['x', 'x@example.com', 'Guest', 'Active', '']]),
                 encoded([ROWS[0]]), b'Name,Email,Role,Status,Seat Tier\n"unterminated',
                 b'\xff', encoded([ROWS[0], ['x', 'a..b@example.com', 'User', 'Active', '']])]
        for blob in cases:
            with self.subTest(blob=blob[:20]):
                with self.assertRaises(ValueError):
                    self.parse(blob)
                self.assertEqual((self.root / 'data/domain_catalog.json').read_bytes(), before)

    def test_sync_preserves_finance_and_stores_metadata_only(self):
        data = {'meta': {}, 'members': [{'id': 'same', 'username': 'old', 'email': 'jane@lsznode.de',
                 'billing_day': 8, 'price': 120, 'status': 'inactive', 'payments': [{'month': '2026-09', 'paid': True}], 'notes': 'keep'}]}
        users = self.parse(encoded())[0:1]
        result = self.server.sync_members_from_users(data, users)
        self.assertEqual(result['updated'], ['Doe, Jane'])
        member = data['members'][0]
        self.assertEqual(member['billing_day'], 8)
        self.assertEqual(member['price'], 120)
        self.assertEqual(member['payments'][0]['paid'], True)
        self.assertEqual(member['status'], 'inactive')
        self.assertEqual(member['notes'], 'keep')
        self.assertEqual(member['source_metadata']['seat_tier'], 'Premium')

    def test_parser_owner_and_only_users(self):
        users = self.parse(encoded())
        self.assertEqual(users.team_domain, 'csv-team.example')
        self.assertEqual(len(users), 2)
        self.assertEqual(users[0]['username'], 'Doe, Jane')
        self.assertEqual(users[1]['username'], 'blank')
        self.assertEqual(users.skipped_count, 0)
        self.assertEqual(users[0]['id'], '')
        self.assertEqual(users.team_domain, 'csv-team.example')
        self.assertEqual(users[0]['source_membership'], {
            'format': 'claude_members_csv', 'role': 'User', 'status': 'Active',
            'seat_tier': 'Premium', 'team_domain': 'csv-team.example'})


class MultiCSVImportTests(unittest.TestCase):
    """HTTP sandbox: one multipart request may carry several membership CSVs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='csv-multi-')
        self.root = Path(self.tmp.name) / 'renewal-registry'
        self.root.mkdir()
        for path in SOURCE.glob('*.py'):
            shutil.copy2(path, self.root / path.name)
        shutil.copy2(SOURCE / 'index.html', self.root / 'index.html')
        catalog = self.root / 'data/domain_catalog.json'
        catalog.parent.mkdir()
        catalog.write_text(json.dumps({
            'default': 'lsznode.de',
            'domains': [{'id': 'lsznode.de', 'label': 'lsznode.de', 'billing_day': None}],
        }))
        members = self.root / 'data/domains/lsznode.de/members.json'
        members.parent.mkdir(parents=True)
        members.write_text(json.dumps({'meta': {'domain': 'lsznode.de'}, 'members': []}))
        self.log = (self.root / 'http.log').open('a')
        self.proc = subprocess.Popen(
            [sys.executable, '-u', '-c',
             "import server; h=server.OptimizedThreadingHTTPServer(('127.0.0.1',0),server.Handler); print(h.server_port,flush=True); h.serve_forever()"],
            cwd=self.root, stdout=subprocess.PIPE, stderr=self.log, text=True)
        self.port = int(self.proc.stdout.readline().strip())

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.log.close()
        self.tmp.cleanup()

    def post_files(self, files):
        boundary = '----csvmulti'
        chunks = []
        for name, blob in files:
            chunks.append(
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
                f'Content-Type: text/csv\r\n\r\n'.encode() + blob + b'\r\n')
        chunks.append(f'--{boundary}--\r\n'.encode())
        body = b''.join(chunks)
        request = urllib.request.Request(
            f'http://127.0.0.1:{self.port}/api/sync-export?domain=auto&auto_domain=1',
            data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            payload = json.load(exc)
            exc.close()
            return exc.code, payload

    def roster(self, domain):
        return json.loads((self.root / f'data/domains/{domain}/members.json').read_text())['members']

    def test_two_csv_files_merge_without_dropping_disjoint_members(self):
        first = encoded([
            ['', 'boss@lsznode.de', 'Primary Owner', 'Active', ''],
            ['Ada', 'ada@gmail.com', 'User', 'Active', 'Standard'],
        ])
        second = encoded([
            ['', 'boss@lsznode.de', 'Primary Owner', 'Active', ''],
            ['Bea', 'bea@gmail.com', 'User', 'Active', 'Standard'],
        ])
        status, body = self.post_files([('a.csv', first), ('b.csv', second)])
        self.assertEqual(status, 200, body)
        self.assertEqual(body['file_count'], 2)
        emails = {member['email'] for member in self.roster('lsznode.de')}
        self.assertEqual(emails, {'ada@gmail.com', 'bea@gmail.com'})
        self.assertEqual(body['added'], ['Ada@lsznode.de', 'Bea@lsznode.de'])
        self.assertEqual(body['unchanged'], [])

    def test_same_email_across_files_updates_once(self):
        first = encoded([
            ['', 'boss@lsznode.de', 'Primary Owner', 'Active', ''],
            ['Ada', 'ada@gmail.com', 'User', 'Active', 'Standard'],
        ])
        second = encoded([
            ['', 'boss@lsznode.de', 'Primary Owner', 'Active', ''],
            ['Ada Renamed', 'ada@gmail.com', 'User', 'Active', 'Premium'],
        ])
        status, body = self.post_files([('a.csv', first), ('b.csv', second)])
        self.assertEqual(status, 200, body)
        members = self.roster('lsznode.de')
        self.assertEqual([member['email'] for member in members], ['ada@gmail.com'])
        self.assertEqual(members[0]['username'], 'Ada Renamed')
        self.assertEqual(members[0]['source_metadata']['seat_tier'], 'Premium')
        self.assertEqual(body['added'], ['Ada@lsznode.de'])
        self.assertEqual(body['updated'], ['Ada Renamed@lsznode.de'])

    def test_same_team_duplicate_email_rejects_entire_batch(self):
        first = encoded([
            ['', 'boss@batch-a.example', 'Primary Owner', 'Active', ''],
            ['Ada', 'ada@gmail.com', 'User', 'Active', 'Standard'],
            ['Ada Again', 'ada@gmail.com', 'User', 'Active', 'Standard'],
        ])
        second = encoded([
            ['', 'boss@batch-a.example', 'Primary Owner', 'Active', ''],
            ['Bea', 'bea@gmail.com', 'User', 'Active', 'Standard'],
        ])
        catalog = (self.root / 'data/domain_catalog.json').read_bytes()
        status, body = self.post_files([('a.csv', first), ('b.csv', second)])
        self.assertEqual(status, 400, body)
        self.assertIn('重复', body['error'])
        self.assertFalse((self.root / 'data/domains/batch-a.example').exists())
        self.assertEqual((self.root / 'data/domain_catalog.json').read_bytes(), catalog)
        self.assertEqual(self.roster('lsznode.de'), [])

    def test_invalid_second_file_writes_nothing(self):
        good = encoded([
            ['', 'boss@batch-b.example', 'Primary Owner', 'Active', ''],
            ['Ada', 'ada@gmail.com', 'User', 'Active', 'Standard'],
        ])
        bad = b'Name,Email\r\nnot,a,membership,csv\r\n'
        catalog = (self.root / 'data/domain_catalog.json').read_bytes()
        status, body = self.post_files([('good.csv', good), ('bad.csv', bad)])
        self.assertEqual(status, 400, body)
        self.assertFalse((self.root / 'data/domains/batch-b.example').exists())
        self.assertEqual((self.root / 'data/domain_catalog.json').read_bytes(), catalog)

    def test_two_new_teams_are_created_and_routed_by_owner(self):
        left = encoded([
            ['', 'boss@left-team.example', 'Primary Owner', 'Active', ''],
            ['Ada', 'ada@gmail.com', 'User', 'Active', 'Standard'],
        ])
        right = encoded([
            ['', 'boss@right-team.example', 'Primary Owner', 'Active', ''],
            ['Bea', 'bea@gmail.com', 'User', 'Active', 'Standard'],
        ])
        status, body = self.post_files([('left.csv', left), ('right.csv', right)])
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body['domains_touched']), {'left-team.example', 'right-team.example'})
        self.assertEqual({m['email'] for m in self.roster('left-team.example')}, {'ada@gmail.com'})
        self.assertEqual({m['email'] for m in self.roster('right-team.example')}, {'bea@gmail.com'})
        self.assertTrue((self.root.parent / 'claude-export-hub/data/domains/left-team.example').exists())

    def test_mixed_zip_and_csv_multi_select_is_rejected(self):
        csv_blob = encoded()
        status, body = self.post_files([('members.csv', csv_blob), ('export.zip', b'PK\x03\x04not-a-zip')])
        self.assertEqual(status, 400, body)
        self.assertIn('仅支持成员 CSV', body['error'])

    def test_single_csv_still_imports(self):
        status, body = self.post_files([('members.csv', encoded())])
        self.assertEqual(status, 200, body)
        self.assertNotIn('file_count', body)
        self.assertEqual(body['domain'], 'csv-team.example')
        self.assertEqual(len(self.roster('csv-team.example')), 2)


if __name__ == '__main__':
    unittest.main()
