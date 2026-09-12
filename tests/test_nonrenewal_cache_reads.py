"""Warm finance reads must not parse unchanged rosters or ledger."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class CacheTests(unittest.TestCase):
    def test_warm_read_skips_reconcile_but_external_write_invalidates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for src in ROOT.glob('*.py'):
                shutil.copy2(src, root / src.name)
            script = '''
import json
from unittest.mock import patch
import server
from nonrenewal_loss import LossStore
for d in server.get_domain_catalog(): server.ensure_domain_file(d['id'])
LossStore(server.ROOT).enable()
first=server.recompute_finance_cache(reason='test')
with patch.object(LossStore,'summary',side_effect=AssertionError('warm read rescanned')):
    assert server.get_finance_cache() == first
path=server.data_path(server.DEFAULT_DOMAIN)
data=json.loads(path.read_text())
data['meta']['external_write']='changed'
path.write_text(json.dumps(data))
original=LossStore.summary
calls=[]
def counted(self):
    calls.append(1)
    return original(self)
with patch.object(LossStore,'summary',counted):
    server.get_finance_cache()
assert calls, 'external writes must trigger reconciliation'
'''
            result = subprocess.run([sys.executable, '-c', script], cwd=root, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == '__main__': unittest.main(verbosity=2)
