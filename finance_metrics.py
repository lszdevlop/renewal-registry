#!/usr/bin/env python3
"""全站盈利统计：与看板 index.html 口径对齐，供整点缓存。

不封号（normal）：日毛利=(售价−成本¥)/30，周期累计=日毛利×min(续费日起已用天数, 30)
历史盈利（history）：不封号长期 = 日毛利 × 自「按续费日对齐的起点」至今天的天数
  锚点优先 created_at / 最早 payment → 回推到该日所在续费周期起算日（billing_day）
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from decimal import Decimal
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

USD_CNY = 6.8
COST_STD_CNY = 27 * USD_CNY
COST_PRO_CNY = 127 * USD_CNY
PRO_THRESHOLD = 800
DAYS_PER_MONTH = 30
CN_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai has no DST


def today_cn() -> date:
    return datetime.now(CN_TZ).date()


def parse_iso_date(raw: Any) -> date | None:
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if "T" in s:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is not None:
                dt = dt.astimezone(CN_TZ)
            return dt.date()
        return date.fromisoformat(s[:10])
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None


def last_cycle_start(billing_day: int, today: date) -> date:
    y, m = today.year, today.month
    this_day = min(int(billing_day), monthrange(y, m)[1])
    this_start = date(y, m, this_day)
    if today >= this_start:
        return this_start
    if m == 1:
        py, pm = y - 1, 12
    else:
        py, pm = y, m - 1
    return date(py, pm, min(int(billing_day), monthrange(py, pm)[1]))


def history_start_date(
    member: dict,
    billing_day: int,
    cycle_start: date,
    today: date,
) -> date:
    """历史盈利起点：按续费日对齐。

    1) 锚点优先 created_at → 最早 payment.recorded_at → 今天
    2) 将锚点回推到「该日所在续费周期」的起算日（last_cycle_start(billing_day, anchor)）
    这样不会出现「进名单日晚于本周期续费日 → 历史天数 < 本周期天数」。
    """
    anchor = None
    c = parse_iso_date(member.get("created_at"))
    if c:
        anchor = c
    earliest_pay = None
    for p in member.get("payments") or []:
        if not isinstance(p, dict):
            continue
        rd = parse_iso_date(p.get("recorded_at"))
        if rd and (earliest_pay is None or rd < earliest_pay):
            earliest_pay = rd
    if earliest_pay and (anchor is None or earliest_pay < anchor):
        anchor = earliest_pay
    if anchor is None:
        return cycle_start
    if anchor > today:
        anchor = today
    floor = date(2024, 1, 1)
    if anchor < floor:
        anchor = floor
    # Snap to billing-day cycle start containing/on-or-before anchor
    start = last_cycle_start(billing_day, anchor)
    if start > today:
        start = cycle_start
    if start < floor:
        start = floor
    return start


def member_metrics(member: dict, today: date | None = None) -> dict[str, Any] | None:
    if not member or member.get("status") == "inactive":
        return None
    today = today or today_cn()
    day_raw = member.get("billing_day")
    price_raw = member.get("price")
    try:
        day = int(day_raw) if day_raw is not None and day_raw != "" else None
    except (TypeError, ValueError):
        day = None
    try:
        price = float(price_raw) if price_raw is not None and price_raw != "" else None
    except (TypeError, ValueError):
        price = None
    valid_day = day is not None and 1 <= day <= 31
    cycle_start = last_cycle_start(day, today) if valid_day else None
    cycle_elapsed = (today - cycle_start).days + 1 if cycle_start else None
    cycle_used = min(cycle_elapsed, DAYS_PER_MONTH) if cycle_elapsed is not None else None
    remain = max(0, DAYS_PER_MONTH - cycle_elapsed) if cycle_elapsed is not None else None

    out: dict[str, Any] = {
        "used_days": cycle_used,
        "elapsed_days": cycle_elapsed,
        "remain_days": remain,
        "start": cycle_start.isoformat() if cycle_start else None,
        "profit": None,
        "history": None,
    }

    if valid_day and cycle_start and price is not None and math.isfinite(price) and price >= 0:
        cost = COST_PRO_CNY if price >= PRO_THRESHOLD else COST_STD_CNY
        monthly = price - cost
        daily = monthly / DAYS_PER_MONTH
        out["profit"] = {
            "daily": daily,
            "total": daily * cycle_used,
            "days": cycle_used,
            "elapsed_days": cycle_elapsed,
            "start": cycle_start.isoformat(),
            "monthly": monthly,
            "cost": cost,
            "tier": "pro" if price >= PRO_THRESHOLD else "std",
        }
        hist_start = history_start_date(member, day, cycle_start, today)
        hist_days = max(0, (today - hist_start).days + 1)
        out["history"] = {
            "daily": daily,
            "total": daily * hist_days,
            "days": hist_days,
            "start": hist_start.isoformat(),
            "cost": cost,
            "tier": "pro" if price >= PRO_THRESHOLD else "std",
        }
    return out


def aggregate_domains(
    load_domain_data,
    domain_ids: list[str],
    *,
    today: date | None = None,
    nonrenewal_store=None,
) -> dict[str, Any]:
    today = today or today_cn()
    normal_daily = normal_profit = history_profit = 0.0
    nonrenewal_summary = {"total_cny": "0.00", "count": 0, "enabled_at": None}
    member_n = normal_n = history_n = 0
    per_domain: list[dict[str, Any]] = []

    for did in domain_ids:
        data = load_domain_data(did) or {}
        members = data.get("members") or []
        d_normal_daily = d_normal_profit = d_history = 0.0
        d_member = d_normal_n = d_history_n = 0
        for m in members:
            if not m or m.get("status") == "inactive":
                continue
            d_member += 1
            metrics = member_metrics(m, today)
            if not metrics:
                continue
            if metrics.get("profit"):
                d_normal_daily += metrics["profit"]["daily"]
                d_normal_profit += metrics["profit"]["total"]
                d_normal_n += 1
            if metrics.get("history"):
                d_history += metrics["history"]["total"]
                d_history_n += 1
        member_n += d_member
        normal_n += d_normal_n
        history_n += d_history_n
        normal_daily += d_normal_daily
        normal_profit += d_normal_profit
        history_profit += d_history
        per_domain.append({
            "domain": did,
            "members": d_member,
            "normal_n": d_normal_n,
            "history_n": d_history_n,
            "normal_daily": round(d_normal_daily, 4),
            "normal_profit": round(d_normal_profit, 4),
            "history_profit": round(d_history, 4),
        })

    if nonrenewal_store is not None:
        nonrenewal_summary = nonrenewal_store.summary()

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {
        "ok": True,
        "schema_version": 2,
        "as_of": now,
        "as_of_date": today.isoformat(),
        "timezone": "Asia/Shanghai",
        "refresh": "hourly_on_the_hour",
        "formulas": {
            "normal_daily": "(price - cost_cny) / 30",
            "normal_profit": "normal_daily * min(days since latest billing_day, 30); billing_day clamps to month end",
            "history_profit": "normal_daily * days from billing-day-aligned start (anchor created_at snapped to cycle start) to today",
            "nonrenewal_loss": "lifetime sum of immutable deletion snapshots: cost * remaining Team cycle days / actual Team cycle days; deletion day included, next bill excluded",
            "cost": "price>=800 ? 127*6.8 : 27*6.8",
        },
        "totals": {
            "normal_daily": round(normal_daily, 4),
            "normal_profit": round(normal_profit, 4),
            "history_profit": round(history_profit, 4),
            "nonrenewal_loss": format(Decimal(nonrenewal_summary["total_cny"]), ".2f"),
            "member_n": member_n,
            "normal_n": normal_n,
            "history_n": history_n,
        },
        "domains": per_domain,
        "nonrenewal_count": nonrenewal_summary["count"],
        "nonrenewal_enabled_at": nonrenewal_summary["enabled_at"],
    }


def cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / "finance_cache.json"


def write_cache(path: Path, payload: dict[str, Any]) -> None:
    """Atomic replace via a unique temp file: concurrent writers never share a temp name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def read_cache(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


if __name__ == "__main__":
    import sys
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root))
    from server import get_domain_catalog, load_data  # type: ignore

    def _load(d: str):
        return load_data(d)

    from nonrenewal_loss import LossStore
    payload = aggregate_domains(_load, [d["id"] for d in get_domain_catalog()], nonrenewal_store=LossStore(root))
    out = cache_path(root / "data")
    write_cache(out, payload)
    print(json.dumps(payload["totals"], ensure_ascii=False, indent=2))
    print("wrote", out)
