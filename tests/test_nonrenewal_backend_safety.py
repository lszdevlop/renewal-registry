"""Isolated accounting boundaries, write failure and lifecycle regressions."""
import copy
from datetime import date
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from nonrenewal_loss import LossStore, _atomic_json, calculate_loss
from domain_catalog import DomainCatalogManager


class LifecycleTests(unittest.TestCase):
    def test_pending_event_survives_both_lifecycle_operations(self):
        for action in ['rename', 'delete']:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)/'renewal'; root.mkdir()
                manager = DomainCatalogManager(catalog_path=root/'data/domain_catalog.json',
                    renewal_root=root, hub_root=Path(tmp)/'hub', backup_root=Path(tmp)/'backups',
                    initial_domains=['default.test'], default_domain='default.test')
                manager.add('team.test')
                path = root/'data/domains/team.test/members.json'
                member = {'id':'seat', 'username':'Seat', 'price':800}
                data = {'members':[member]}; _atomic_json(path, data)
                store = LossStore(root); store.enable()
                with patch.object(store, '_write', side_effect=OSError('ledger failure')):
                    response = store.delete_members(data, [member], 'team.test', 31, lambda obj: _atomic_json(path,obj))
                    self.assertIn('warning', response[0])
                event = json.loads(path.read_text())['loss_events'][0]
                if action == 'rename': manager.rename('team.test','new.test',confirm_clear=True)
                else: manager.delete('team.test',confirm=True)
                self.assertFalse(path.exists())
                self.assertEqual(LossStore(root).summary()['events'], [event])

    def test_failed_reconcile_blocks_lifecycle_wipe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'renewal'; root.mkdir()
            manager=DomainCatalogManager(catalog_path=root/'data/domain_catalog.json',
                renewal_root=root,hub_root=Path(tmp)/'hub',backup_root=Path(tmp)/'backups',
                initial_domains=['default.test'],default_domain='default.test')
            manager.add('team.test')
            path=root/'data/domains/team.test/members.json'
            with patch.object(LossStore,'reconcile',side_effect=OSError('ledger unavailable')):
                with self.assertRaises(OSError): manager.delete('team.test',confirm=True)
            self.assertTrue(path.exists())
            self.assertIn('team.test',manager.domain_ids())


class FinanceTests(unittest.TestCase):
    def test_profit_formulas_unchanged_and_decimal_loss_not_float(self):
        from finance_metrics import aggregate_domains, member_metrics
        members=[{'price':260,'billing_day':31,'created_at':'2025-12-20','payments':[]},
                 {'price':800,'billing_day':1,'created_at':'2026-01-05','payments':[]}]
        today=date(2026,2,10)
        class Store:
            def summary(self):
                return {'total_cny':'90071992547409.93','count':2,'enabled_at':'2026-02-01T00:00:00Z','events':[]}
        result=aggregate_domains(lambda _: {'members':members}, ['team.test'],today=today,nonrenewal_store=Store())
        expected=[member_metrics(m,today) for m in members]
        self.assertEqual(result['totals']['normal_daily'],round(sum(m['profit']['daily'] for m in expected),4))
        self.assertEqual(result['totals']['normal_profit'],round(sum(m['profit']['total'] for m in expected),4))
        self.assertEqual(result['totals']['history_profit'],round(sum(m['history']['total'] for m in expected),4))
        self.assertEqual(result['totals']['nonrenewal_loss'],'90071992547409.93')
        self.assertEqual(result['nonrenewal_count'],2)
        self.assertEqual(result['nonrenewal_enabled_at'],'2026-02-01T00:00:00Z')
        self.assertNotIn('refund',json.dumps(result))
        self.assertEqual(aggregate_domains(lambda _: {'members':members},['team.test'],today=today)['totals']['nonrenewal_loss'],'0.00')


class CalendarTests(unittest.TestCase):
    def test_month_end_bill_day_and_leap_cycle(self):
        for day, today, start, end, days, remain in [
            (31,date(2026,2,28),'2026-02-28','2026-03-31',31,31),
            (31,date(2026,2,27),'2026-01-31','2026-02-28',28,1),
            (31,date(2024,2,29),'2024-02-29','2024-03-31',31,31),
            (31,date(2024,3,30),'2024-02-29','2024-03-31',31,1),
            (20,date(2026,12,31),'2026-12-20','2027-01-20',31,20)]:
            with self.subTest(today=today):
                result=calculate_loss({'price':800},day,today)
                self.assertEqual((result['cycle_start'],result['cycle_end'],result['cycle_days'],result['remaining_days']),
                    (start,end,days,remain))
                if remain==days: self.assertEqual(result['amount_cny'],'863.60')


if __name__=='__main__': unittest.main(verbosity=2)
