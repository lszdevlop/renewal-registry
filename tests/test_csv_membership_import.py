#!/usr/bin/env python3
"""CSV contract, source-only sandbox; never import the production module/store."""
import copy
import io
import csv
import json
import shutil
import threading
import unittest
import urllib.request
import urllib.error
from test_upload_streaming_queue import load_sandbox_server

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


if __name__ == '__main__':
    unittest.main()
