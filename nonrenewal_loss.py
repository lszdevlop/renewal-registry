"""Permanent nonrenewal snapshots and recoverable domain-JSON outbox.

Caller holds the domain transaction lock during delete_members. Lock order is
catalog -> domain -> ledger. Ledger reconciliation never takes a domain lock:
atomic roster replacement provides a consistent outbox snapshot.
"""
from calendar import monthrange
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo
import copy
import fcntl
import json
import os
import tempfile
import threading
import uuid

_LOCK = threading.RLock()


def calculate_loss(member, team_billing_day, today):
    if type(team_billing_day) is not int or not 1 <= team_billing_day <= 31:
        raise ValueError('请先设置域名 Team 账单日（1-31），无法计算预计删除损失')
    try:
        price = Decimal(str(member.get('price')))
    except (InvalidOperation, ValueError):
        raise ValueError('请先设置有效成员售价，无法计算预计删除损失') from None
    if not price.is_finite() or price < 0:
        raise ValueError('请先设置有效成员售价，无法计算预计删除损失')
    day = team_billing_day
    start = today.replace(day=min(day, monthrange(today.year, today.month)[1]))
    if today < start:
        y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        start = today.replace(year=y, month=m, day=min(day, monthrange(y, m)[1]))
    y, m = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    end = today.replace(year=y, month=m, day=min(day, monthrange(y, m)[1]))
    cost = Decimal('863.60') if price >= 800 else Decimal('183.60')
    remaining, days = (end - today).days, (end - start).days
    amount = (cost * remaining / days).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    return {'amount_cny': str(amount), 'cost_cny': str(cost),
            'price_cny': str(price), 'tier': 'pro' if price >= 800 else 'std',
            'fx': '6.8', 'cost_usd': '127' if price >= 800 else '27',
            'remaining_days': remaining, 'cycle_days': days,
            'cycle_start': start.isoformat(), 'cycle_end': end.isoformat(),
            'deletion_day': today.isoformat(), 'team_billing_day': day}


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, allow_nan=False, indent=2)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(name): os.unlink(name)


class LossStore:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'data/nonrenewal_loss.json'

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _LOCK, self.path.with_suffix('.lock').open('a+') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(f, fcntl.LOCK_UN)

    def _read(self):
        if not self.path.exists(): return {'version': 1, 'enabled_at': None, 'events': []}
        return json.loads(self.path.read_text(encoding='utf-8'))

    def _write(self, state):
        _atomic_json(self.path, state)

    def enable(self):
        with self._lock():
            s = self._read()
            if not s['enabled_at']:
                s['enabled_at'] = datetime.now(timezone.utc).isoformat()
                self._write(s)
            return s['enabled_at']

    @staticmethod
    def _merge(state, events):
        index = {e['event_id']: e for e in state['events']}
        changed = False
        for event in events:
            eid = event['event_id']
            if eid in index:
                if index[eid] != event:
                    raise ValueError('不续费损失记录冲突，拒绝覆盖不可变明细')
            else:
                state['events'].append(copy.deepcopy(event))
                index[eid] = event
                changed = True
        return changed

    def reconcile(self):
        with self._lock():
            state = self._read()
            changed = False
            for path in sorted((self.root / 'data/domains').glob('*/members.json')):
                try: data = json.loads(path.read_text(encoding='utf-8'))
                except FileNotFoundError: continue  # concurrent catalog removal, pre-flushed
                changed = self._merge(state, data.get('loss_events', [])) or changed
            if changed: self._write(state)
            return state

    def summary(self):
        state = self.reconcile()
        total = sum((Decimal(e['amount_cny']) for e in state['events']), Decimal('0.00'))
        return {'enabled_at': state['enabled_at'], 'count': len(state['events']),
                'total_cny': format(total, '.2f'), 'events': copy.deepcopy(state['events'])}

    def delete_members(self, data, members, domain, team_billing_day, save):
        # Validate membership and the entire batch before touching in-memory/disk state.
        if not members or len({id(m) for m in members}) != len(members):
            raise ValueError('删除目标为空或重复')
        if any(not any(m is x for x in data.get('members', [])) for m in members):
            raise ValueError('成员已删除或目标不唯一，请刷新')
        with self._lock():
            state = self._read()
            if not state['enabled_at']: raise ValueError('不续费损失登记尚未启用，暂不可删除成员')
            now = datetime.now(timezone.utc)
            today = now.astimezone(ZoneInfo('Asia/Shanghai')).date()
            events = [{**calculate_loss(m, team_billing_day, today),
                       'event_id': uuid.uuid4().hex, 'domain': domain,
                       'member_id': m.get('id'), 'username': m.get('username'),
                       'email': m.get('email'), 'deleted_at': now.isoformat(),
                       'enabled_at': state['enabled_at']} for m in members]
            updated = copy.deepcopy(data)
            updated['members'] = [copy.deepcopy(m) for m in data.get('members', [])
                                  if not any(m is target for target in members)]
            updated.setdefault('loss_events', []).extend(events)
            save(updated)  # single atomic roster+outbox commit; failure creates no ledger entry
            self._merge(state, updated['loss_events'])
            # The canonical roster/outbox commit has succeeded. Never report a
            # failed deletion just because the recoverable ledger projection failed.
            warning = None
            try:
                self._write(state)
            except OSError:
                warning = '成员已删除，损失明细已安全保存；汇总台账暂未同步，请刷新明细重试同步，勿重复删除。'
            data.clear(); data.update(updated)
            response_events = copy.deepcopy(events)
            if warning:
                for event in response_events:
                    event['warning'] = warning
            return response_events


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='永久不续费损失台账')
    parser.add_argument('command', choices=['enable', 'summary'])
    args = parser.parse_args(argv)
    store = LossStore(Path(__file__).resolve().parent)
    if args.command == 'enable':
        store.enable()
    print(json.dumps({'ok': True, **store.summary()}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
