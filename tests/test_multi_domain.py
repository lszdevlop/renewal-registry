#!/usr/bin/env python3
"""多域续费登记：功能 + 性能专项测试。

覆盖：
- /api/domains 列表
- 双域数据隔离（读/写/改/删/缴费）
- 查询筛选逻辑（按域）
- 前端域缓存相关 HTML 契约
- API 读/写/查询延迟与串行吞吐

会临时改写两个域的 members.json，结束时强制恢复备份。
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOMAINS_DIR = ROOT / "data" / "domains"
LEGACY = ROOT / "data" / "members.json"
BASE = "http://127.0.0.1:8765"
DOMAINS = ["lsznode.de", "killclaude.de", "328001.xyz", "peaceai.de"]

PASS = 0
FAIL = 0


def now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


def record(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        status = "PASS"
    else:
        FAIL += 1
        status = "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def http_json(method: str, path: str, body: dict | None = None, timeout: float = 8.0):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            ms = (time.perf_counter() - t0) * 1000
            payload = json.loads(raw) if raw else {}
            return resp.status, payload, ms
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        ms = (time.perf_counter() - t0) * 1000
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return e.code, payload, ms


def domain_path(domain: str) -> Path:
    return DOMAINS_DIR / domain / "members.json"


def load_domain(domain: str) -> dict:
    return json.loads(domain_path(domain).read_text(encoding="utf-8"))


def save_domain(domain: str, data: dict) -> None:
    p = domain_path(domain)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def backup_all() -> dict[str, dict]:
    snaps = {}
    for d in DOMAINS:
        snaps[d] = deepcopy(load_domain(d))
    if LEGACY.exists():
        snaps["__legacy__"] = deepcopy(json.loads(LEGACY.read_text(encoding="utf-8")))
    return snaps


def restore_all(snaps: dict[str, dict]) -> None:
    for d in DOMAINS:
        save_domain(d, snaps[d])
    if "__legacy__" in snaps:
        LEGACY.write_text(
            json.dumps(snaps["__legacy__"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def timed_loop(fn, n: int = 30) -> list[float]:
    return [fn() for _ in range(n)]


def stats(ms_list: list[float]) -> str:
    arr = sorted(ms_list)
    p50 = statistics.median(arr)
    p95 = arr[max(0, int(len(arr) * 0.95) - 1)]
    return (
        f"n={len(arr)} min={min(arr):.2f}ms p50={p50:.2f}ms "
        f"p95={p95:.2f}ms max={max(arr):.2f}ms mean={statistics.mean(arr):.2f}ms"
    )


def p95(ms_list: list[float]) -> float:
    arr = sorted(ms_list)
    return arr[max(0, int(len(arr) * 0.95) - 1)]


def main() -> int:
    tag = now_tag()
    print(f"== multi-domain functional + performance tests @ {tag} ==")
    snaps = backup_all()
    baseline_counts = {d: len(snaps[d].get("members") or []) for d in DOMAINS}
    print("baseline counts:", baseline_counts)

    try:
        # ---------- health / frontend contract ----------
        req = urllib.request.Request(BASE + "/")
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode("utf-8")
            home_ms = (time.perf_counter() - t0) * 1000
            st = resp.status
        record("首页 HTTP 200", st == 200, f"{home_ms:.1f}ms")
        record(
            "首页含域标签",
            "domain-tabs" in html
            and "killclaude.de" in html
            and "killclaude.de" in html
            and "328001.xyz" in html,
        )
        record("首页含 DOMAIN_CACHE", "DOMAIN_CACHE" in html and "prefetchOtherDomains" in html)
        record("首页含 switchSeq", "switchSeq" in html)

        # ---------- domains API ----------
        st, payload, ms = http_json("GET", "/api/domains")
        record("GET /api/domains ok", st == 200 and payload.get("ok") is True, f"{ms:.1f}ms")
        ids = [d["id"] for d in payload.get("domains", [])]
        record("域列表顺序", ids == DOMAINS, str(ids))
        counts = {d["id"]: d["member_count"] for d in payload.get("domains", [])}
        record(
            "域人数与磁盘一致",
            all(counts.get(d) == baseline_counts[d] for d in DOMAINS),
            str(counts),
        )

        # ---------- read all domains ----------
        st1, d1, ms1 = http_json("GET", "/api/data?domain=lsznode.de")
        st2, d2, ms2 = http_json("GET", "/api/data?domain=killclaude.de")
        record("读 lsznode.de", st1 == 200 and d1.get("ok") and isinstance(d1.get("data"), dict), f"{ms1:.1f}ms")
        record("读 killclaude.de", st2 == 200 and d2.get("ok") and isinstance(d2.get("data"), dict), f"{ms2:.1f}ms")
        record(
            "lsznode 成员数正确",
            len(d1["data"].get("members") or []) == baseline_counts["lsznode.de"],
            str(len(d1["data"].get("members") or [])),
        )
        record(
            "55500678 初始成员数正确",
            len(d2["data"].get("members") or []) == baseline_counts["killclaude.de"],
            str(len(d2["data"].get("members") or [])),
        )
        record("未知域拒绝", http_json("GET", "/api/data?domain=no.such")[0] == 400)

        # ---------- create on each domain ----------
        u_lsz = f"zz_md_lsz_{tag}"
        u_camp = f"zz_md_camp_{tag}"
        st, add1, ms = http_json(
            "POST",
            "/api/add-member",
            {
                "domain": "lsznode.de",
                "username": u_lsz,
                "email": f"{u_lsz}@lsznode.de",
                "billing_day": 8,
                "price": 101.5,
                "notes": "md-lsz",
            },
        )
        record(
            "lsz 新增成功",
            st == 200 and add1.get("ok") and add1.get("domain") == "lsznode.de",
            f"{ms:.1f}ms {add1.get('username')}",
        )
        st, add2, ms = http_json(
            "POST",
            "/api/add-member",
            {
                "domain": "killclaude.de",
                "username": u_camp,
                "email": f"{u_camp}@killclaude.de",
                "billing_day": 15,
                "price": 88,
                "notes": "md-camp",
            },
        )
        record(
            "camp 新增成功",
            st == 200 and add2.get("ok") and add2.get("domain") == "killclaude.de",
            f"{ms:.1f}ms",
        )

        # same username on different domains should be allowed
        st, add_cross, _ms = http_json(
            "POST",
            "/api/add-member",
            {
                "domain": "killclaude.de",
                "username": u_lsz,
                "email": f"{u_lsz}@killclaude.de",
                "notes": "same-name-other-domain",
            },
        )
        record("跨域同名允许", st == 200 and add_cross.get("ok") is True, f"{st} {add_cross.get('error')}")

        disk_lsz = load_domain("lsznode.de")
        disk_camp = load_domain("killclaude.de")
        lsz_users = {m["username"] for m in disk_lsz["members"]}
        camp_users = {m["username"] for m in disk_camp["members"]}
        record("隔离: lsz 有 lsz探针", u_lsz in lsz_users)
        record("隔离: lsz 无 camp探针", u_camp not in lsz_users)
        record("隔离: camp 有 camp探针", u_camp in camp_users)
        record("隔离: camp 有跨域同名", u_lsz in camp_users)

        # ---------- update / query / paid ----------
        st, upd, ms = http_json(
            "POST",
            "/api/update-member",
            {
                "domain": "killclaude.de",
                "username": u_camp,
                "billing_day": 21,
                "price": 120,
                "notes": "camp-updated",
            },
        )
        record("camp 修改成功", st == 200 and upd.get("ok"), f"{ms:.1f}ms")
        camp_m = next(m for m in load_domain("killclaude.de")["members"] if m["username"] == u_camp)
        record(
            "camp 修改落盘",
            camp_m.get("billing_day") == 21
            and float(camp_m.get("price")) == 120.0
            and camp_m.get("notes") == "camp-updated",
            str({k: camp_m.get(k) for k in ("billing_day", "price", "notes")}),
        )
        lsz_m = next(m for m in load_domain("lsznode.de")["members"] if m["username"] == u_lsz)
        record("修改不串域", lsz_m.get("notes") == "md-lsz" and float(lsz_m.get("price")) == 101.5)

        st, paid, ms = http_json(
            "POST",
            "/api/toggle-paid",
            {
                "domain": "killclaude.de",
                "username": u_camp,
                "month": "2026-07",
                "paid": True,
                "note": "md-paid",
            },
        )
        record("camp 标记已缴", st == 200 and paid.get("paid") is True, f"{ms:.1f}ms")
        camp_m = next(m for m in load_domain("killclaude.de")["members"] if m["username"] == u_camp)
        record(
            "camp payments 落盘",
            any(p.get("month") == "2026-07" and p.get("paid") for p in camp_m.get("payments") or []),
        )
        lsz_m = next(m for m in load_domain("lsznode.de")["members"] if m["username"] == u_lsz)
        record(
            "缴费不串域",
            not any(p.get("month") == "2026-07" and p.get("paid") for p in lsz_m.get("payments") or []),
        )

        st, camp_data, ms = http_json("GET", "/api/data?domain=killclaude.de")
        members = camp_data["data"]["members"]
        q_hit = [
            m
            for m in members
            if u_camp.lower() in f"{m.get('username', '')} {m.get('email', '')}".lower()
        ]
        record("查询用户名命中", len(q_hit) == 1, f"hits={len(q_hit)} {ms:.1f}ms")
        paid_hit = [
            m
            for m in members
            if any(p.get("month") == "2026-07" and p.get("paid") for p in (m.get("payments") or []))
        ]
        record("查询当月已缴含探针", any(m["username"] == u_camp for m in paid_hit), f"paid_n={len(paid_hit)}")

        st, miss, _ms = http_json(
            "POST",
            "/api/update-member",
            {"domain": "lsznode.de", "username": u_camp, "notes": "should-fail"},
        )
        record("错域修改返回404", st == 404, f"{st} {miss.get('error')}")

        # ---------- delete isolation ----------
        st, dele, ms = http_json(
            "POST",
            "/api/delete-member",
            {"domain": "killclaude.de", "username": u_camp},
        )
        record("camp 删除成功", st == 200 and dele.get("ok"), f"{ms:.1f}ms")
        camp_users = {m["username"] for m in load_domain("killclaude.de")["members"]}
        lsz_users = {m["username"] for m in load_domain("lsznode.de")["members"]}
        record("删除后 camp 无探针", u_camp not in camp_users)
        record("删除不伤 lsz 探针", u_lsz in lsz_users)

        for who, domain in [(u_lsz, "lsznode.de"), (u_lsz, "killclaude.de")]:
            http_json("POST", "/api/delete-member", {"domain": domain, "username": who})

        # ---------- performance: read ----------
        lsz_ms = timed_loop(lambda: http_json("GET", "/api/data?domain=lsznode.de")[2], 40)
        camp_ms = timed_loop(lambda: http_json("GET", "/api/data?domain=killclaude.de")[2], 40)
        domains_ms = timed_loop(lambda: http_json("GET", "/api/domains")[2], 40)
        legacy_ms = timed_loop(lambda: http_json("GET", "/data/members.json?domain=lsznode.de")[2], 30)
        record("性能 读 lsz p95<50ms", p95(lsz_ms) < 50, stats(lsz_ms))
        record("性能 读 camp p95<50ms", p95(camp_ms) < 50, stats(camp_ms))
        record("性能 读 domains p95<50ms", p95(domains_ms) < 50, stats(domains_ms))
        record("性能 读 legacy compat p95<50ms", p95(legacy_ms) < 50, stats(legacy_ms))

        switch_ms = []
        for i in range(40):
            domain = DOMAINS[i % 2]
            switch_ms.append(http_json("GET", f"/api/data?domain={domain}")[2])
        record("性能 域切换读 p95<50ms", p95(switch_ms) < 50, stats(switch_ms))

        # ---------- performance: write ----------
        w_user = f"zz_md_perf_{tag}"
        st, addp, ms = http_json(
            "POST",
            "/api/add-member",
            {
                "domain": "killclaude.de",
                "username": w_user,
                "email": f"{w_user}@killclaude.de",
                "price": 1,
                "billing_day": 1,
            },
        )
        record("性能夹具新增", st == 200 and addp.get("ok"), f"{ms:.1f}ms")

        upd_ms = [
            http_json(
                "POST",
                "/api/update-member",
                {
                    "domain": "killclaude.de",
                    "username": w_user,
                    "notes": f"perf-{i}",
                    "price": 1 + (i % 5),
                },
            )[2]
            for i in range(30)
        ]
        record("性能 修改 p95<80ms", p95(upd_ms) < 80, stats(upd_ms))

        toggle_ms = [
            http_json(
                "POST",
                "/api/toggle-paid",
                {
                    "domain": "killclaude.de",
                    "username": w_user,
                    "month": "2026-07",
                    "paid": bool(i % 2),
                    "note": "perf-toggle",
                },
            )[2]
            for i in range(30)
        ]
        record("性能 缴费切换 p95<80ms", p95(toggle_ms) < 80, stats(toggle_ms))

        c_ms = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [
                ex.submit(http_json, "GET", f"/api/data?domain={DOMAINS[i % 2]}")
                for i in range(40)
            ]
            for fut in as_completed(futs):
                stc, payload, msc = fut.result()
                if stc != 200 or not payload.get("ok"):
                    raise RuntimeError(f"concurrent read failed: {stc} {payload}")
                c_ms.append(msc)
        record("性能 并发读(8x40) p95<100ms", p95(c_ms) < 100, stats(c_ms))

        q_ms = []
        for _ in range(30):
            stq, payload, ms_net = http_json("GET", "/api/data?domain=lsznode.de")
            t0 = time.perf_counter()
            members = payload["data"]["members"]
            q = "chu"
            _hits = [
                m
                for m in members
                if q in f"{m.get('username', '')} {m.get('email', '')}".lower()
            ]
            _paid = [
                m
                for m in members
                if any(p.get("month") == "2026-07" and p.get("paid") for p in (m.get("payments") or []))
            ]
            cpu = (time.perf_counter() - t0) * 1000
            q_ms.append(ms_net + cpu)
        record("性能 读+查询 p95<50ms", p95(q_ms) < 50, stats(q_ms))

        http_json("POST", "/api/delete-member", {"domain": "killclaude.de", "username": w_user})

    finally:
        restore_all(snaps)
        ok_restore = True
        details = []
        for d in DOMAINS:
            cur = load_domain(d)
            base = snaps[d]
            same_n = len(cur.get("members") or []) == len(base.get("members") or [])
            leaked = [
                m.get("username")
                for m in (cur.get("members") or [])
                if str(m.get("username") or "").startswith("zz_md_")
            ]
            if not same_n or leaked:
                ok_restore = False
            details.append(f"{d}:n={len(cur.get('members') or [])} leak={leaked}")
        record("测试后数据已恢复", ok_restore, "; ".join(details))

    print("\n" + "=" * 60)
    print(f"TOTAL: {PASS + FAIL}  PASS: {PASS}  FAIL: {FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
