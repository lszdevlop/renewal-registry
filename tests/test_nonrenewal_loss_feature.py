"""Direct unittest convention; synthetic fixtures only."""
import sys, unittest
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

class LossTests(unittest.TestCase):
    def test_team_cycle_preview(self):
        import nonrenewal_loss as loss
        m={'id':'m1','price':100,'billing_day':10}
        result=loss.calculate_loss(m, 20, date(2026,2,10))
        self.assertEqual(result['amount_cny'], '59.23')
        self.assertEqual(result['cycle_days'],31)
        self.assertEqual(result['remaining_days'],10)
        self.assertEqual(result['cost_cny'],'183.60')
        self.assertEqual(result['cycle_start'],'2026-01-20')
        self.assertEqual(result['cycle_end'],'2026-02-20')

    def test_invalid_configuration_fails_clearly(self):
        import nonrenewal_loss as loss
        for price,day in [(None,20),('',20),('NaN',20),('Infinity',20),(-1,20),(True,20),(100,None),(100,0),(100,32),(100,True),(100,1.5)]:
            with self.subTest(price=price,day=day), self.assertRaisesRegex(ValueError,'售价|Team'):
                loss.calculate_loss({'price':price},day,date(2026,2,10))

class PersistenceTests(unittest.TestCase):
    def test_enabled_delete_atomic_outbox_and_permanent_details(self):
        import tempfile, json
        import nonrenewal_loss as loss
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); path=root/'data/domains/team.test/members.json'
            path.parent.mkdir(parents=True)
            member={'id':'id1','username':'Example','email':'example@team.test','price':100,'billing_day':1}
            data={'meta':{'domain':'team.test'},'members':[member]}
            path.write_text(json.dumps(data))
            store=loss.LossStore(root)
            self.assertEqual(store.summary()['total_cny'],'0.00')
            self.assertIsNone(store.summary()['enabled_at'])
            activation=store.enable()
            self.assertEqual(store.enable(),activation)
            saved=[]
            def save(obj):
                saved.append(obj)
                path.write_text(json.dumps(obj))
            events=store.delete_members(data,[member],'team.test',20,save)
            persisted=json.loads(path.read_text())
            self.assertEqual(persisted['members'],[])
            self.assertEqual(persisted['loss_events'],events)
            self.assertEqual(len(events),1)
            self.assertEqual(events[0]['username'],'Example')
            self.assertEqual(events[0]['domain'],'team.test')
            self.assertEqual(loss.LossStore(root).summary()['total_cny'],events[0]['amount_cny'])
            self.assertEqual(store.summary()['count'],1)
            self.assertEqual(len(saved),1)

    def test_failure_recovery_idempotency_and_recreated_identity(self):
        import tempfile, json
        from unittest.mock import patch
        import nonrenewal_loss as loss
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); path=root/'data/domains/team.test/members.json'; path.parent.mkdir(parents=True)
            member={'id':'id1','username':'Example','price':800}
            data={'members':[member]}; path.write_text(json.dumps(data))
            store=loss.LossStore(root); store.enable()
            def save(obj): path.write_text(json.dumps(obj))
            # Before the roster commit, failure must not book or mutate input.
            with self.assertRaises(OSError):
                store.delete_members(data,[member],'team.test',31,lambda _: (_ for _ in ()).throw(OSError('disk full')))
            self.assertEqual(data,{'members':[member]})
            self.assertEqual(store.summary()['count'],0)
            # After commit, failed root-ledger flush is recovered from the atomic outbox.
            with patch.object(store,'_write',side_effect=OSError('ledger unavailable')):
                result = store.delete_members(data,[member],'team.test',31,save)
                self.assertIn('warning', result[0])
            self.assertEqual(json.loads(path.read_text())['members'],[])
            recovered=loss.LossStore(root)
            self.assertEqual(recovered.summary()['count'],1)
            self.assertEqual(recovered.summary()['count'],1)
            persisted=json.loads(path.read_text())
            with self.assertRaises(ValueError): recovered.delete_members(persisted,[member],'team.test',31,save)
            # Reimport and delete the same identity on the same day is a NEW real deletion.
            persisted['members']=[member]; save(persisted)
            recovered.delete_members(persisted,[member],'team.test',31,save)
            self.assertEqual(recovered.summary()['count'],2)
            # Domain directory may disappear, but lifetime details remain.
            import shutil
            shutil.rmtree(path.parent)
            self.assertEqual(loss.LossStore(root).summary()['count'],2)

if __name__=='__main__': unittest.main(verbosity=2)
