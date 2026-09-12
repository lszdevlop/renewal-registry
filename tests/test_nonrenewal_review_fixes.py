"""Regression tests for API shape and committed-outbox failure reporting."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nonrenewal_loss import LossStore, _atomic_json

class ReviewFixes(unittest.TestCase):
    def test_committed_outbox_flush_failure_is_success_with_warning(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); store = LossStore(root); store.enable()
            member = {'id':'a', 'price':260}
            data = {'members':[member]}
            path = root/'data/domains/team.test/members.json'
            _atomic_json(path, data)
            with patch.object(store, '_write', side_effect=OSError('disk unavailable')):
                events = store.delete_members(data, [member], 'team.test', 9, lambda d: _atomic_json(path,d))
            self.assertEqual(data['members'], [])
            self.assertIn('warning', events[0])
            self.assertEqual(LossStore(root).summary()['count'],1)
            self.assertNotIn('warning', LossStore(root).summary()['events'][0])

    def test_frontend_real_api_count(self):
        from test_nonrenewal_frontend import run_js
        run_js('''
renderGlobalKpiFromCache({totals:{normal_daily:1,normal_profit:2,history_profit:3,nonrenewal_loss:'12.34'},nonrenewal_count:2,nonrenewal_enabled_at:'2026-09-12'});
assert.match(element('nonrenewal-loss-meta').textContent,/2 条/);
''')

if __name__ == '__main__': unittest.main(verbosity=2)
