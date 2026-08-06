#!/usr/bin/env python3
"""续费看板：累计盈利 / 应退差价逻辑 + render 路径性能专项。

不写生产数据。前端公式用 Python 复刻（与 index.html computeMemberMetrics 对齐）。
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.request
from calendar import monthrange
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from finance_metrics import aggregate_domains, member_metrics  # noqa: E402

BASE = "http://127.0.0.1:8765"
USD = 6.8
STD_COST = 27 * USD
PRO_COST = 127 * USD
THR = 800
DAYS = 30

PASS = FAIL = 0


def record(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"[PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def last_start(day: int, today: date) -> date:
    y, m = today.year, today.month
    this_day = min(day, monthrange(y, m)[1])
    this_start = date(y, m, this_day)
    if today >= this_start:
        return this_start
    if m == 1:
        py, pm = y - 1, 12
    else:
        py, pm = y, m - 1
    return date(py, pm, min(day, monthrange(py, pm)[1]))


def scan_pay(m: dict):
    last = None
    amt = None
    for p in m.get("payments") or []:
        if not p or not p.get("paid") or not p.get("month"):
            continue
        if last is None or str(p["month"]) > str(last):
            last = p["month"]
            a = p.get("amount")
            try:
                a = float(a) if a is not None and a != "" else None
            except (TypeError, ValueError):
                a = None
            amt = a if a is not None and a >= 0 else None
    return last, amt


def metrics(m: dict, today: date | None = None):
    today = today or date.today()
    day = m.get("billing_day")
    price = m.get("price")
    last_m, last_a = scan_pay(m)
    out = {"lastPaidMonth": last_m, "profit": None, "refund": None}
    if price is not None and price != "":
        price = float(price)
        cost = PRO_COST if price >= THR else STD_COST
        monthly = price - cost
        daily = monthly / DAYS
        out["profit"] = {
            "total": daily * today.day,
            "days": today.day,
            "daily": daily,
            "monthly": monthly,
            "cost": cost,
            "start": today.replace(day=1).isoformat(),
            "tier": "pro" if price >= THR else "std",
        }
    if day is None:
        return out
    try:
        day = int(day)
    except (TypeError, ValueError):
        return out
    if not 1 <= day <= 31:
        return out
    start = last_start(day, today)
    used = (today - start).days + 1
    remain = max(0, DAYS - used)

    base = None
    src = None
    bm = None
    if last_a is not None:
        base, src, bm = last_a, "paid", last_m
    elif price is not None and price != "":
        base, src = float(price), "price"
    if base is not None:
        out["refund"] = {
            "refund": base * remain / DAYS,
            "usedDays": used,
            "remainDays": remain,
            "baseAmount": base,
            "baseSource": src,
            "baseMonth": bm,
            "start": start.isoformat(),
        }
    return out


def timed(fn, n=40):
    xs = []
    # warmup
    fn()
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        xs.append((time.perf_counter() - t0) * 1000)
    return xs


def p95(xs):
    arr = sorted(xs)
    return arr[max(0, int(len(arr) * 0.95) - 1)]


def stats(xs):
    arr = sorted(xs)
    return (
        f"n={len(arr)} min={min(arr):.3f}ms p50={statistics.median(arr):.3f}ms "
        f"p95={p95(arr):.3f}ms max={max(arr):.3f}ms mean={statistics.mean(arr):.3f}ms"
    )


def main() -> int:
    print("== profit/refund + render path performance ==")

    # HTML contract
    with urllib.request.urlopen(BASE + "/", timeout=5) as r:
        html = r.read().decode()
    record("页面含累计盈利/应退金额", "累计盈利" in html and "应退金额" in html)
    record(
        "顶部已移除封号单日/累计",
        'class="kpi-card ban-daily"' not in html
        and 'class="kpi-card ban-profit"' not in html
        and 'id="kpi-ban-daily"' not in html
        and 'id="kpi-ban-profit"' not in html,
    )
    record("顶部明确总累计盈利", "总累计盈利" in html and "跨周期长期毛利" in html)
    record("页面含单次预计算", "computeMemberMetrics" in html and "makeTodayCtx" in html)
    record("无旧双算路径", "function profitInfo" not in html and "function refundInfo" not in html)
    record("汇率 6.8", "PROFIT_USD_CNY = 6.8" in html or "const PROFIT_USD_CNY = 6.8" in html)

    # unit rules
    today = date(2026, 7, 26)
    m = metrics({"price": 260, "billing_day": 1, "payments": []}, today)
    record(
        "标准档自然月盈利(1号→26日)",
        m["profit"] is not None
        and abs(m["profit"]["total"] - (260 - STD_COST) / 30 * 26) < 1e-6
        and m["profit"]["tier"] == "std",
        f"total={m['profit']['total']:.4f}",
    )
    record(
        "应退用售价回退",
        m["refund"] is not None and abs(m["refund"]["refund"] - 260 * 4 / 30) < 1e-6,
        f"refund={m['refund']['refund']:.4f} remain={m['refund']['remainDays']}",
    )
    m2 = metrics(
        {
            "price": 999,
            "billing_day": 1,
            "payments": [{"month": "2026-07", "paid": True, "amount": 200}],
        },
        today,
    )
    record(
        "应退优先实缴 amount",
        m2["refund"]["baseSource"] == "paid" and abs(m2["refund"]["refund"] - 200 * 4 / 30) < 1e-6,
        str(m2["refund"]),
    )
    m3 = metrics({"price": 1300, "billing_day": 20, "payments": []}, today)
    record(
        "高级档阈值>=800",
        m3["profit"]["tier"] == "pro" and abs(m3["profit"]["cost"] - PRO_COST) < 1e-9,
        f"cost={m3['profit']['cost']}",
    )
    m4 = metrics({"price": 260, "billing_day": 30, "payments": []}, today)
    # 续费日仍用于应退周期，但不影响自然月盈利
    record(
        "续费日不影响自然月盈利起点",
        m4["profit"]["start"] == "2026-07-01" and m4["profit"]["days"] == 26,
        f"start={m4['profit']['start']} days={m4['profit']['days']}",
    )
    reset_day = date(2026, 7, 30)
    m4_reset = member_metrics({"price": 260, "billing_day": 30, "payments": []}, reset_day)
    record(
        "续费日当天自然月盈利不重置",
        m4_reset["profit"]["start"] == "2026-07-01"
        and m4_reset["profit"]["days"] == 30
        and abs(m4_reset["profit"]["total"] - (260 - STD_COST)) < 1e-6,
        f"start={m4_reset['profit']['start']} days={m4_reset['profit']['days']}",
    )
    cycle_members = [
        {"price": 260, "billing_day": 1, "payments": []},
        {"price": 300, "billing_day": 20, "payments": []},
    ]
    agg = aggregate_domains(lambda _domain: {"members": cycle_members}, ["demo"], today=today)
    expected_month = sum(member_metrics(m, today)["profit"]["total"] for m in cycle_members)
    record(
        "不封号累计等于所有用户当前自然月毛利之和",
        abs(agg["totals"]["normal_profit"] - expected_month) < 1e-4,
        f"actual={agg['totals']['normal_profit']:.4f} expected={expected_month:.4f}",
    )
    record(
        "服务端不再产出封号盈利字段",
        "ban_daily" not in agg["totals"]
        and "ban_profit" not in agg["totals"]
        and "ban_n" not in agg["totals"]
        and all("ban_daily" not in d and "ban_profit" not in d and "ban_n" not in d for d in agg["domains"]),
    )
    m5 = metrics({"price": 260, "payments": []}, today)
    record("无续费日仍计算自然月盈利但不计算应退", m5["profit"] is not None and m5["refund"] is None)
    m6 = metrics({"billing_day": 1, "payments": []}, today)
    record("无价格无盈利", m6["profit"] is None)
    # unpaid still counts profit (default paid assumption)
    m7 = metrics({"price": 260, "billing_day": 1, "payments": []}, today)
    record("未缴仍可算盈利", m7["profit"] is not None)

    # live domain aggregate
    with urllib.request.urlopen(BASE + "/api/data?domain=lsznode.de", timeout=10) as r:
        payload = json.loads(r.read().decode())
    members = payload["data"]["members"]
    p_sum = r_sum = 0.0
    n_p = n_r = 0
    tday = date.today()
    for m in members:
        if m.get("status") == "inactive":
            continue
        x = metrics(m, tday)
        if x["profit"]:
            p_sum += x["profit"]["total"]
            n_p += 1
        if x["refund"]:
            r_sum += x["refund"]["refund"]
            n_r += 1
    record("实盘可算盈利人数>0", n_p > 0, f"n={n_p} sum={p_sum:.2f}")
    record("实盘可算应退人数>0", n_r > 0, f"n={n_r} sum={r_sum:.2f}")
    record("应退合计非负", r_sum >= 0, f"{r_sum:.2f}")

    # performance: single-pass metrics over N members
    def one_pass(ms):
        s_p = s_r = 0.0
        for m in ms:
            x = metrics(m, tday)
            if x["profit"]:
                s_p += x["profit"]["total"]
            if x["refund"]:
                s_r += x["refund"]["refund"]
        return s_p, s_r

    # synthetic scale: replicate roster to ~2k
    big = (members * ((2000 // max(len(members), 1)) + 1))[:2000]
    xs = timed(lambda: one_pass(big), n=30)
    record("性能 2000人单遍 metrics p95<20ms", p95(xs) < 20, stats(xs))
    xs65 = timed(lambda: one_pass(members), n=50)
    record("性能 实盘名单单遍 p95<5ms", p95(xs65) < 5, stats(xs65))

    # API read performance under current data size
    def api_read():
        with urllib.request.urlopen(BASE + "/api/data?domain=lsznode.de", timeout=5) as r:
            r.read()

    xs_api = timed(api_read, n=30)
    record("性能 GET /api/data p95<50ms", p95(xs_api) < 50, stats(xs_api))

    # HTML payload size (static, should stay reasonable)
    with urllib.request.urlopen(BASE + "/", timeout=5) as r:
        body = r.read()
    record("首页体积 < 200KB", len(body) < 200_000, f"{len(body)} bytes")

    print("\n" + "=" * 60)
    print(f"TOTAL: {PASS + FAIL}  PASS: {PASS}  FAIL: {FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
