#!/usr/bin/env python3
"""前端关键逻辑单测：筛选切换 / 字段规范化 / 搜索。

不依赖浏览器，直接复刻 index.html 中的核心 JS 规则（Python 版）。
"""

from __future__ import annotations

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}" + (f" — {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


PROFIT_PRO_THRESHOLD = 800


def is_paid(m, ym):
    return any(p.get("month") == ym and p.get("paid") for p in (m.get("payments") or []))


def member_plan_tier(m):
    price = m.get("price")
    try:
        n = float(price)
    except (TypeError, ValueError):
        return "none"
    if n != n:  # NaN
        return "none"
    return "pro" if n >= PROFIT_PRO_THRESHOLD else "std"


def format_plan_counts(std, pro, none=0):
    text = f"标准 {std} · 高级 {pro}"
    if none:
        text += f" · 未定价 {none}"
    return text


def paid_unpaid_plan_counts(members, ym):
    paid_std = paid_pro = paid_none = 0
    unpaid_std = unpaid_pro = unpaid_none = 0
    paid = unpaid = 0
    for m in members:
        if m.get("status") == "inactive":
            continue
        tier = member_plan_tier(m)
        if is_paid(m, ym):
            paid += 1
            if tier == "pro":
                paid_pro += 1
            elif tier == "std":
                paid_std += 1
            else:
                paid_none += 1
        else:
            unpaid += 1
            if tier == "pro":
                unpaid_pro += 1
            elif tier == "std":
                unpaid_std += 1
            else:
                unpaid_none += 1
    return {
        "paid": paid,
        "unpaid": unpaid,
        "paid_meta": format_plan_counts(paid_std, paid_pro, paid_none),
        "unpaid_meta": format_plan_counts(unpaid_std, unpaid_pro, unpaid_none),
        "paid_std": paid_std,
        "paid_pro": paid_pro,
        "unpaid_std": unpaid_std,
        "unpaid_pro": unpaid_pro,
        "paid_none": paid_none,
        "unpaid_none": unpaid_none,
    }


def filter_members(members, ym, q="", filt="all"):
    q = (q or "").strip().lower()
    out = []
    for m in members:
        if q:
            blob = f"{m.get('username','')} {m.get('email','')}".lower()
            if q not in blob:
                continue
        paid_m = is_paid(m, ym)
        missing_cfg = m.get("billing_day") is None or m.get("price") is None
        if filt == "paid":
            if not paid_m:
                continue
        elif filt == "unpaid":
            if paid_m or m.get("status") == "inactive":
                continue
        elif filt == "unset":
            if not missing_cfg:
                continue
        out.append(m)
    return out


def normalize_field(field, raw):
    v = str(raw or "").strip()
    if field == "billing_day":
        if v == "":
            return None
        n = float(v)
        if not n.is_integer() or not (1 <= int(n) <= 31):
            raise ValueError("续费日须为 1-31 的整数，或留空")
        return int(n)
    if field == "price":
        if v == "":
            return None
        n = float(v)
        if n != n or n < 0:  # NaN or negative
            raise ValueError("价格须为非负数字，或留空")
        return n
    if field == "notes":
        return str(raw or "")
    raise ValueError("unknown field")


def main():
    ym = "2026-07"
    members = [
        {"username": "Alice", "email": "a@x.com", "billing_day": 1, "price": 100, "status": "active",
         "payments": [{"month": "2026-07", "paid": True}]},
        {"username": "Bob", "email": "b@x.com", "billing_day": None, "price": None, "status": "active",
         "payments": []},
        {"username": "Carol", "email": "c@x.com", "billing_day": 5, "price": 80, "status": "active",
         "payments": [{"month": "2026-08", "paid": True}]},  # other month paid
        {"username": "Dan", "email": "d@x.com", "billing_day": 2, "price": 50, "status": "inactive",
         "payments": []},
    ]

    # filter switches
    check("筛选 all=4", len(filter_members(members, ym, filt="all")) == 4)
    paid = filter_members(members, ym, filt="paid")
    check("筛选 paid 仅 Alice", [m["username"] for m in paid] == ["Alice"], str([m["username"] for m in paid]))
    unpaid = filter_members(members, ym, filt="unpaid")
    check("筛选 unpaid 不含 Alice/inactive Dan", set(m["username"] for m in unpaid) == {"Bob", "Carol"}, str([m["username"] for m in unpaid]))
    unset = filter_members(members, ym, filt="unset")
    check("筛选 unset 仅 Bob", [m["username"] for m in unset] == ["Bob"])

    # switching filters is pure recompute
    a = filter_members(members, ym, filt="paid")
    b = filter_members(members, ym, filt="unpaid")
    c = filter_members(members, ym, filt="paid")
    check("筛选切换可逆 paid->unpaid->paid", [m["username"] for m in a] == [m["username"] for m in c])

    # search
    check("搜索用户名", [m["username"] for m in filter_members(members, ym, q="bob")] == ["Bob"])
    check("搜索邮箱", [m["username"] for m in filter_members(members, ym, q="c@x.com")] == ["Carol"])
    check("搜索无命中", filter_members(members, ym, q="zzz") == [])

    # month isolation
    check("他月已缴在当月算未缴", not is_paid(members[2], "2026-07") and is_paid(members[2], "2026-08"))

    check("售价799为标准", member_plan_tier({"price": 799}) == "std")
    check("售价800为高级", member_plan_tier({"price": 800}) == "pro")
    check("未填价格为未定价", member_plan_tier({"price": None}) == "none")
    plan_members = [
        {"username": "S1", "price": 260, "status": "active", "payments": [{"month": ym, "paid": True}]},
        {"username": "P1", "price": 1300, "status": "active", "payments": [{"month": ym, "paid": True}]},
        {"username": "S2", "price": 240, "status": "active", "payments": []},
        {"username": "P2", "price": 900, "status": "active", "payments": []},
        {"username": "N1", "price": None, "status": "active", "payments": []},
        {"username": "X1", "price": 1300, "status": "inactive", "payments": []},
    ]
    counts = paid_unpaid_plan_counts(plan_members, ym)
    check("已缴标准/高级人数", counts["paid_std"] == 1 and counts["paid_pro"] == 1, str(counts))
    check("未缴标准/高级/未定价人数", counts["unpaid_std"] == 1 and counts["unpaid_pro"] == 1 and counts["unpaid_none"] == 1, str(counts))
    check("停用成员不计入档位统计", counts["paid"] == 2 and counts["unpaid"] == 3)
    check("已缴档位文案", counts["paid_meta"] == "标准 1 · 高级 1")
    check("未缴档位文案含未定价", counts["unpaid_meta"] == "标准 1 · 高级 1 · 未定价 1")

    # normalize fields
    check("续费日空->None", normalize_field("billing_day", "") is None)
    check("续费日15", normalize_field("billing_day", "15") == 15)
    try:
        normalize_field("billing_day", "0")
        check("续费日0非法", False)
    except ValueError:
        check("续费日0非法", True)
    try:
        normalize_field("billing_day", "32")
        check("续费日32非法", False)
    except ValueError:
        check("续费日32非法", True)

    check("价格空->None", normalize_field("price", "  ") is None)
    check("价格12.5", normalize_field("price", "12.5") == 12.5)
    try:
        normalize_field("price", "-3")
        check("价格负数非法(前端)", False)
    except ValueError:
        check("价格负数非法(前端)", True)

    check("备注保留文本", normalize_field("notes", " hello ") == " hello ")
    check("备注空串", normalize_field("notes", "") == "")

    # add form required rule
    def can_add(username, email):
        return bool((username or "").strip()) and bool((email or "").strip())

    check("新增必填拦截空用户名", can_add("", "a@b.com") is False)
    check("新增必填拦截空邮箱", can_add("u", "") is False)
    check("新增双填通过", can_add("u", "a@b.com") is True)

    print()
    print(f"TOTAL: {PASS+FAIL} PASS: {PASS} FAIL: {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
