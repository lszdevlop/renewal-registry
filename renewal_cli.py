#!/usr/bin/env python3
"""Claude Team 续费登记 CLI。

用法示例：
  python3 renewal_cli.py list
  python3 renewal_cli.py board
  python3 renewal_cli.py set CHUJUN --day 8 --price 100
  python3 renewal_cli.py paid 2026-07 CHUJUN moshaobo --amount 100 --note 微信
  python3 renewal_cli.py unpaid 2026-07
  python3 renewal_cli.py sync /path/to/export.zip_or_users.json
  python3 renewal_cli.py export-csv
  python3 renewal_cli.py add --username foo --email foo@x.com --day 10 --price 80
"""

from __future__ import annotations

import argparse
import copy
import csv
import fcntl
import json
import math
import os
import re
import sys
import tempfile
import uuid
from contextlib import contextmanager
from calendar import monthrange
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from domain_catalog import DomainCatalogError, make_live_manager

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOMAINS_DIR = DATA_DIR / "domains"
LEGACY_DATA_PATH = DATA_DIR / "members.json"
LEGACY_CSV_PATH = DATA_DIR / "members.csv"

DOMAIN_CATALOG = [
    {"id": "lsznode.de", "label": "lsznode.de"},
    {"id": "328001.xyz", "label": "328001.xyz"},
    {"id": "peaceai.de", "label": "peaceai.de"}]
DEFAULT_DOMAIN = DOMAIN_CATALOG[0]["id"]
DOMAIN_IDS = {d["id"] for d in DOMAIN_CATALOG}
DOMAIN_MANAGER = make_live_manager(ROOT)


def get_domain_catalog() -> list[dict]:
    return DOMAIN_MANAGER.read()["domains"]


def get_domain_ids() -> set[str]:
    return {d["id"] for d in get_domain_catalog()}

# Active domain for this process (set by main() from --domain).
ACTIVE_DOMAIN = DEFAULT_DOMAIN


@contextmanager
def domain_lock(domain: str | None = None):
    d = normalize_domain(domain or ACTIVE_DOMAIN)
    path = DOMAINS_DIR / d / ".registry.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text); f.flush(); os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try: os.unlink(tmp_name)
        except FileNotFoundError: pass


def finite_nonnegative(value, label: str) -> float:
    if isinstance(value, bool):
        raise SystemExit(f"{label} 必须为有限非负数字")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise SystemExit(f"{label} 必须为有限非负数字")
    return number


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_domain(raw: str | None) -> str:
    domain = (raw or "").strip().lower()
    if not domain:
        return DEFAULT_DOMAIN
    domain = domain.replace(" ", "")
    domain_ids = get_domain_ids()
    if domain not in domain_ids:
        raise SystemExit(f"未知域: {raw!r}（可选: {', '.join(sorted(domain_ids))}）")
    return domain


def data_path(domain: str | None = None) -> Path:
    d = normalize_domain(domain or ACTIVE_DOMAIN)
    return DOMAINS_DIR / d / "members.json"


def csv_path(domain: str | None = None) -> Path:
    d = normalize_domain(domain or ACTIVE_DOMAIN)
    return DOMAINS_DIR / d / "members.csv"


def empty_registry(domain: str) -> dict[str, Any]:
    return {
        "meta": {
            "plan": "Claude.ai Team",
            "domain": domain,
            "currency": "CNY",
            "default_price": None,
            "default_billing_day": None,
            "updated_at": now_iso(),
            "source_note": f"域 {domain} 空壳；可导入 Claude 导出 zip 或手动新增成员",
        },
        "members": [],
    }


def ensure_domain_file(domain: str | None = None) -> Path:
    d = normalize_domain(domain or ACTIVE_DOMAIN)
    path = data_path(d)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        if d == DEFAULT_DOMAIN and LEGACY_DATA_PATH.exists():
            try:
                obj = json.loads(LEGACY_DATA_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                obj = empty_registry(d)
            obj.setdefault("meta", {})["domain"] = d
            path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            path.write_text(
                json.dumps(empty_registry(d), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    return path


def load(domain: str | None = None) -> dict[str, Any]:
    d = normalize_domain(domain or ACTIVE_DOMAIN)
    path = ensure_domain_file(d)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("meta", {})["domain"] = d
    return data


def save(data: dict[str, Any], domain: str | None = None) -> None:
    d = normalize_domain(domain or ACTIVE_DOMAIN or (data.get("meta") or {}).get("domain"))
    data.setdefault("meta", {})["domain"] = d
    data["meta"]["updated_at"] = now_iso()
    path = ensure_domain_file(d)
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    atomic_write_text(path, text)
    if d == DEFAULT_DOMAIN:
        atomic_write_text(LEGACY_DATA_PATH, text)
    write_csv(data, d)


def write_csv(data: dict[str, Any], domain: str | None = None) -> None:
    d = normalize_domain(domain or ACTIVE_DOMAIN or (data.get("meta") or {}).get("domain"))
    out = csv_path(d)
    year_month = date.today().strftime("%Y-%m")
    rows = []
    for m in data.get("members", []):
        paid = is_paid_month(m, year_month)
        rows.append(
            {
                "username": m.get("username") or "",
                "email": m.get("email") or "",
                "activation_date": m.get("activation_date") or "",
                "billing_day": m.get("billing_day") if m.get("billing_day") is not None else "",
                "price": m.get("price") if m.get("price") is not None else "",
                "status": m.get("status") or "",
                f"paid_{year_month}": "yes" if paid else "no",
                "last_paid_month": last_paid_month(m) or "",
                "notes": m.get("notes") or "",
                "id": m.get("id") or "",
            }
        )
    fieldnames = list(rows[0].keys()) if rows else [
        "username", "email", "activation_date", "billing_day", "price", "status", "notes", "id"
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    if d == DEFAULT_DOMAIN:
        with LEGACY_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)


def parse_month(s: str | None) -> str:
    if not s:
        return date.today().strftime("%Y-%m")
    s = s.strip()
    if re.fullmatch(r"\d{6}", s):
        s = f"{s[:4]}-{s[4:]}"
    if not re.fullmatch(r"\d{4}-\d{2}", s) or not 1 <= int(s[5:7]) <= 12:
        raise SystemExit(f"月份格式错误: {s}（期望 YYYY-MM，月份 01-12）")
    return s


def parse_activation_date(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise SystemExit("开通日期格式错误（期望 YYYY-MM-DD）")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise SystemExit("开通日期不是有效日期（期望 YYYY-MM-DD）") from exc


def find_members(data: dict[str, Any], keys: list[str]) -> list[dict[str, Any]]:
    found = []
    missing = []
    for key in keys:
        m = find_one(data, key)
        if m is None:
            missing.append(key)
        else:
            found.append(m)
    if missing:
        raise SystemExit(f"未找到成员: {', '.join(missing)}")
    return found


def find_one(data: dict[str, Any], key: str) -> dict[str, Any] | None:
    key_l = key.strip().lower()
    for m in data.get("members", []):
        if (m.get("id") or "").lower() == key_l:
            return m
        if (m.get("username") or "").lower() == key_l:
            return m
        if (m.get("email") or "").lower() == key_l:
            return m
    return None


def find_unique_for_destructive_action(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Resolve ID/email exactly; reject ambiguous display-name matches."""
    key_l = key.strip().lower()
    members = data.get("members", [])
    matches = [m for m in members if (m.get("id") or "").lower() == key_l]
    if not matches:
        matches = [m for m in members if (m.get("email") or "").lower() == key_l]
    if matches:
        if len(matches) > 1:
            raise SystemExit(f"成员标识不唯一，请使用唯一 ID: {key}")
        return matches[0]
    matches = [m for m in members if (m.get("username") or "").lower() == key_l]
    if not matches:
        raise SystemExit(f"未找到成员: {key}")
    if len(matches) > 1:
        choices = ", ".join(f"{m.get('id')} <{m.get('email')}>" for m in matches)
        raise SystemExit(f"用户名匹配多个成员，请改用 ID 或邮箱: {key} -> {choices}")
    return matches[0]


def is_paid_month(member: dict[str, Any], year_month: str) -> bool:
    for p in member.get("payments") or []:
        if p.get("month") == year_month and p.get("paid"):
            return True
    return False


def last_paid_month(member: dict[str, Any]) -> str | None:
    months = sorted(
        {p.get("month") for p in (member.get("payments") or []) if p.get("paid") and p.get("month")}
    )
    return months[-1] if months else None


def due_date_for(member: dict[str, Any], year: int, month: int) -> date | None:
    day = member.get("billing_day")
    if not day:
        return None
    last = monthrange(year, month)[1]
    return date(year, month, min(int(day), last))


def money(v: Any) -> str:
    if v is None or v == "":
        return "—"
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


def cmd_list(data: dict[str, Any], args: argparse.Namespace) -> None:
    ym = parse_month(args.month)
    members = data.get("members", [])
    print(f"成员数: {len(members)} | 查看月份: {ym}")
    print(f"{'用户名':<14} {'邮箱':<28} {'续费日':>6} {'价格':>8} {ym+'已缴':>10} {'状态':<8} 备注")
    print("-" * 100)
    for m in sorted(members, key=lambda x: (x.get("username") or "").lower()):
        paid = "是" if is_paid_month(m, ym) else "否"
        print(
            f"{(m.get('username') or ''):<14} "
            f"{(m.get('email') or ''):<28} "
            f"{str(m.get('billing_day') or '—'):>6} "
            f"{money(m.get('price')):>8} "
            f"{paid:>10} "
            f"{(m.get('status') or ''):<8} "
            f"{(m.get('notes') or '')[:30]}"
        )


def cmd_board(data: dict[str, Any], args: argparse.Namespace) -> None:
    ym = parse_month(args.month)
    y, mo = map(int, ym.split("-"))
    today = date.today()
    members = data.get("members", [])

    unpaid, paid, unset = [], [], []
    for m in members:
        if m.get("status") == "inactive":
            continue
        if is_paid_month(m, ym):
            paid.append(m)
        else:
            unpaid.append(m)
        if m.get("billing_day") is None or m.get("price") is None:
            unset.append(m)

    print(f"=== 续费看板 {ym} ===")
    print(f"今天: {today.isoformat()}")
    print(f"已缴 {len(paid)} | 未缴 {len(unpaid)} | 缺日/价配置 {len(unset)}")
    print()

    print("--- 未缴费（优先处理）---")
    if not unpaid:
        print("（无）")
    else:
        for m in sorted(unpaid, key=lambda x: (x.get("billing_day") or 99, x.get("username") or "")):
            due = due_date_for(m, y, mo)
            due_s = due.isoformat() if due else "续费日未设"
            print(
                f"  - {m.get('username'):<12} {m.get('email'):<28} "
                f"日={m.get('billing_day') or '—'} 价={money(m.get('price'))} 应付日={due_s}"
            )

    print()
    print("--- 已缴费 ---")
    if not paid:
        print("（无）")
    else:
        for m in sorted(paid, key=lambda x: (x.get("username") or "").lower()):
            pay = next(p for p in (m.get("payments") or []) if p.get("month") == ym and p.get("paid"))
            print(
                f"  - {m.get('username'):<12} 金额={money(pay.get('amount') if pay.get('amount') is not None else m.get('price'))} "
                f"记录于={pay.get('recorded_at','')[:10]} {pay.get('note') or ''}"
            )

    if unset:
        print()
        print("--- 待补配置（billing_day / price）---")
        for m in unset:
            print(
                f"  - {m.get('username')}: day={m.get('billing_day')} price={m.get('price')}"
            )


def cmd_set(data: dict[str, Any], args: argparse.Namespace) -> None:
    m = find_one(data, args.who)
    if not m:
        raise SystemExit(f"未找到成员: {args.who}")
    if args.activation_date is not None:
        m["activation_date"] = parse_activation_date(args.activation_date)
    if args.day is not None:
        if not (1 <= args.day <= 31):
            raise SystemExit("billing_day 必须在 1-31")
        m["billing_day"] = args.day
    if args.price is not None:
        m["price"] = finite_nonnegative(args.price, "price")
    if args.status:
        m["status"] = args.status
    if args.notes is not None:
        m["notes"] = args.notes
    if args.email:
        m["email"] = args.email
    if args.username:
        m["username"] = args.username
    m["updated_at"] = now_iso()
    save(data)
    print(
        f"已更新 {m.get('username')}: activation={m.get('activation_date')} day={m.get('billing_day')} price={m.get('price')} "
        f"status={m.get('status')}"
    )


def cmd_paid(data: dict[str, Any], args: argparse.Namespace) -> None:
    ym = parse_month(args.month)
    members = find_members(data, args.who)
    for m in members:
        payments = m.setdefault("payments", [])
        existing = next((p for p in payments if p.get("month") == ym), None)
        amount = finite_nonnegative(args.amount, "amount") if args.amount is not None else m.get("price")
        rec = {
            "month": ym,
            "paid": True,
            "amount": amount,
            "note": args.note or "",
            "recorded_at": now_iso(),
        }
        if existing:
            existing.update(rec)
        else:
            payments.append(rec)
        m["updated_at"] = now_iso()
        print(f"已记缴费: {m.get('username')} {ym} amount={money(amount)}")
    save(data)


def cmd_unpaid(data: dict[str, Any], args: argparse.Namespace) -> None:
    ym = parse_month(args.month)
    if args.who:
        members = find_members(data, args.who)
    else:
        members = [
            m for m in data.get("members", [])
            if m.get("status") != "inactive" and not is_paid_month(m, ym)
        ]
        print(f"{ym} 未缴费名单（{len(members)}）:")
        for m in members:
            print(f"  - {m.get('username')} <{m.get('email')}>")
        return

    for m in members:
        payments = m.setdefault("payments", [])
        payments[:] = [p for p in payments if not (p.get("month") == ym and p.get("paid"))]
        m["updated_at"] = now_iso()
        print(f"已撤销缴费标记: {m.get('username')} {ym}")
    save(data)


def _normalize_export_users(raw: Any) -> list[dict[str, str]]:
    """Accept Claude users.json array or already-normalized list."""
    if not isinstance(raw, list):
        raise ValueError("users.json 格式应为数组")
    out: list[dict[str, str]] = []
    for u in raw:
        if not isinstance(u, dict):
            continue
        # Claude export shape
        if "uuid" in u or "full_name" in u or "email_address" in u:
            out.append(
                {
                    "id": str(u.get("uuid") or ""),
                    "username": str(u.get("full_name") or ""),
                    "email": str(u.get("email_address") or ""),
                }
            )
            continue
        # Already normalized / members-like
        out.append(
            {
                "id": str(u.get("id") or u.get("uuid") or ""),
                "username": str(u.get("username") or u.get("full_name") or ""),
                "email": str(u.get("email") or u.get("email_address") or ""),
            }
        )
    return out


def load_users_json_bytes(blob: bytes) -> list[dict[str, str]]:
    try:
        raw = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"users.json 解析失败: {e}") from e
    return _normalize_export_users(raw)


def extract_users_from_export(path: Path) -> list[dict[str, str]]:
    """从目录 / users.json / Claude 导出 zip 中提取成员。"""
    import zipfile

    path = path.expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"路径不存在: {path}")

    # Claude Team export zip: root users.json
    if path.is_file() and path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
                cand = None
                for n in names:
                    base = n.rstrip("/").split("/")[-1]
                    if base == "users.json" and not n.endswith("/"):
                        cand = n
                        if n == "users.json" or n.count("/") == 0:
                            break
                if not cand:
                    raise SystemExit(f"zip 内未找到 users.json: {path}")
                return load_users_json_bytes(zf.read(cand))
        except zipfile.BadZipFile as e:
            raise SystemExit(f"无效 zip: {e}") from e

    users_file = path
    if path.is_dir():
        cand = path / "users.json"
        if not cand.exists():
            matches = list(path.rglob("users.json"))
            if not matches:
                raise SystemExit(f"目录内未找到 users.json: {path}")
            cand = matches[0]
        users_file = cand
    elif path.is_file() and path.name != "users.json" and path.suffix.lower() == ".json":
        # allow any json path that is users-shaped
        users_file = path

    try:
        return load_users_json_bytes(users_file.read_bytes())
    except ValueError as e:
        raise SystemExit(str(e)) from e


def sync_members_from_users(
    data: dict[str, Any],
    users: list[dict[str, str]],
    *,
    mark_missing: bool = False,
    source_note: str = "synced from export",
) -> dict[str, Any]:
    """Merge export users atomically without losing billing/payment history."""
    working = copy.deepcopy(data)
    members = working.setdefault("members", [])
    by_id = {m.get("id"): m for m in members if m.get("id")}
    by_email = {(m.get("email") or "").lower(): m for m in members if m.get("email")}

    added: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    seen_ids: set[str] = set()
    seen_emails: set[str] = set()

    for u in users:
        uid = (u.get("id") or "").strip()
        email = (u.get("email") or "").strip()
        username = (u.get("username") or "").strip()
        if not uid and not email and not username:
            continue
        if uid:
            seen_ids.add(uid)
        if email:
            seen_emails.add(email.lower())

        id_match = by_id.get(uid) if uid else None
        email_match = by_email.get(email.lower()) if email else None
        if id_match is not None and email_match is not None and id_match is not email_match:
            raise ValueError(f"成员身份冲突: id={uid!r} 与 email={email!r} 指向不同席位")
        if id_match is None and email_match is not None and uid and email_match.get("id") != uid:
            raise ValueError(
                f"成员身份冲突: email={email!r} 已属于 id={email_match.get('id')!r}，不能改为 id={uid!r}"
            )
        m = id_match or email_match

        if m is None:
            m = {
                "id": uid or f"manual-{uuid.uuid4().hex}",
                "username": username or email.split("@")[0],
                "email": email,
                "role": "member",
                "billing_day": working.get("meta", {}).get("default_billing_day"),
                "price": working.get("meta", {}).get("default_price"),
                "status": "active",
                "notes": "",
                "payments": [],
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            members.append(m)
            if m["id"]:
                by_id[m["id"]] = m
            if email:
                by_email[email.lower()] = m
            added.append(m.get("username") or email)
            continue

        changed = False
        if username and m.get("username") != username:
            m["username"] = username
            changed = True
        if email and m.get("email") != email:
            old_email = (m.get("email") or "").lower()
            if old_email and by_email.get(old_email) is m:
                del by_email[old_email]
            m["email"] = email
            by_email[email.lower()] = m
            changed = True
        if uid and m.get("id") != uid:
            old_id = m.get("id")
            if old_id and by_id.get(old_id) is m:
                del by_id[old_id]
            m["id"] = uid
            by_id[uid] = m
            changed = True
        if m.get("status") == "missing_in_export":
            m["status"] = "active"
            changed = True
        if changed:
            m["updated_at"] = now_iso()
            updated.append(m.get("username") or email)
        else:
            unchanged.append(m.get("username") or email)

    missing: list[str] = []
    if mark_missing:
        for m in members:
            mid = m.get("id") or ""
            memail = (m.get("email") or "").lower()
            in_export = (mid and mid in seen_ids) or (memail and memail in seen_emails)
            if not in_export and m.get("status") == "active":
                m["status"] = "missing_in_export"
                m["updated_at"] = now_iso()
                missing.append(m.get("username") or memail)

    meta = working.setdefault("meta", {})
    meta["last_sync_at"] = now_iso()
    if source_note:
        meta["last_sync_note"] = source_note

    result = {
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "missing": missing,
        "export_user_count": len(users),
        "member_count": len(members),
    }
    data.clear()
    data.update(working)
    return result


def cmd_sync(data: dict[str, Any], args: argparse.Namespace) -> None:
    users = extract_users_from_export(Path(args.export_path))
    summary = sync_members_from_users(
        data,
        users,
        mark_missing=bool(args.mark_missing),
        source_note=f"synced from {Path(args.export_path).name}",
    )
    save(data)
    print(
        f"同步完成: +新增 {len(summary['added'])} | ~更新 {len(summary['updated'])} | "
        f"=不变 {len(summary['unchanged'])} | 导出缺失标记 {len(summary['missing'])} | "
        f"成员总数 {summary['member_count']}"
    )
    if summary["added"]:
        print("  新增:", ", ".join(summary["added"]))
    if summary["updated"]:
        print("  更新:", ", ".join(summary["updated"]))
    if summary["missing"]:
        print("  导出中消失:", ", ".join(summary["missing"]))


def cmd_add(data: dict[str, Any], args: argparse.Namespace) -> None:
    if not args.username.strip() or "@" not in args.email or "." not in args.email.split("@")[-1]:
        raise SystemExit("用户名或邮箱格式错误")
    if args.day is not None and not 1 <= args.day <= 31:
        raise SystemExit("billing_day 必须在 1-31")
    if find_one(data, args.email) or find_one(data, args.username):
        raise SystemExit("用户名或邮箱已存在")
    m = {
        "id": args.id or f"manual-{uuid.uuid4().hex}",
        "username": args.username,
        "email": args.email,
        "role": args.role or "member",
        "activation_date": parse_activation_date(args.activation_date),
        "billing_day": args.day,
        "price": finite_nonnegative(args.price, "price") if args.price is not None else None,
        "status": "active",
        "notes": args.notes or "",
        "payments": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    data.setdefault("members", []).append(m)
    save(data)
    print(f"已新增: {m['username']} <{m['email']}>")


def cmd_remove(data: dict[str, Any], args: argparse.Namespace) -> None:
    removed_names = []
    for who in args.who:
        m = find_unique_for_destructive_action(data, who)
        mid = m.get("id")
        data["members"] = [
            x for x in data.get("members", [])
            if x is not m and (not mid or x.get("id") != mid)
        ]
        removed_names.append(m.get("username") or m.get("email") or who)
    save(data)
    print(f"已删除 {len(removed_names)} 人: {', '.join(removed_names)} · 剩余 {len(data.get('members', []))}")


def cmd_export_csv(data: dict[str, Any], args: argparse.Namespace) -> None:
    write_csv(data)
    print(f"已导出: {csv_path()}")


def cmd_defaults(data: dict[str, Any], args: argparse.Namespace) -> None:
    meta = data.setdefault("meta", {})
    if args.day is not None:
        if not (1 <= args.day <= 31):
            raise SystemExit("day 必须 1-31")
        meta["default_billing_day"] = args.day
    if args.price is not None:
        meta["default_price"] = finite_nonnegative(args.price, "price")
    if args.apply:
        for m in data.get("members", []):
            if args.day is not None and m.get("billing_day") is None:
                m["billing_day"] = args.day
                m["updated_at"] = now_iso()
            if args.price is not None and m.get("price") is None:
                m["price"] = finite_nonnegative(args.price, "price")
                m["updated_at"] = now_iso()
    save(data)
    print(
        f"默认: day={meta.get('default_billing_day')} price={meta.get('default_price')} "
        f"apply={bool(args.apply)}"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Claude Team 续费登记（多域）")
    p.add_argument(
        "--domain",
        default=DEFAULT_DOMAIN,
        help=f"目标域（默认 {DEFAULT_DOMAIN}；可选: {', '.join(d['id'] for d in get_domain_catalog())}）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list", help="成员列表")
    s.add_argument("--month", help="YYYY-MM，默认本月")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("board", help="续费看板")
    s.add_argument("--month", help="YYYY-MM，默认本月")
    s.set_defaults(func=cmd_board)

    s = sub.add_parser("set", help="设置成员开通日期/续费日/价格等")
    s.add_argument("who", help="用户名 / 邮箱 / id")
    s.add_argument("--activation-date", help="开通日期 YYYY-MM-DD；传空串可清除")
    s.add_argument("--day", type=int)
    s.add_argument("--price", type=float)
    s.add_argument("--status", choices=["active", "inactive", "missing_in_export"])
    s.add_argument("--notes")
    s.add_argument("--email")
    s.add_argument("--username")
    s.set_defaults(func=cmd_set)

    s = sub.add_parser("paid", help="标记本月已续费")
    s.add_argument("month", help="YYYY-MM")
    s.add_argument("who", nargs="+", help="一个或多个用户名/邮箱")
    s.add_argument("--amount", type=float, help="实收；默认用成员 price")
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_paid)

    s = sub.add_parser("unpaid", help="查看未缴 或 撤销已缴标记")
    s.add_argument("month", help="YYYY-MM")
    s.add_argument("who", nargs="*", help="若提供则撤销这些人的该月已缴")
    s.set_defaults(func=cmd_unpaid)

    s = sub.add_parser("sync", help="从 Claude 导出 zip / 目录 / users.json 同步成员")
    s.add_argument("export_path", help="导出 zip、目录或 users.json 路径")
    s.add_argument("--mark-missing", action="store_true", help="导出中消失的成员标为 missing_in_export")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("add", help="手动新增成员")
    s.add_argument("--username", required=True)
    s.add_argument("--email", required=True)
    s.add_argument("--activation-date", help="开通日期 YYYY-MM-DD")
    s.add_argument("--day", type=int)
    s.add_argument("--price", type=float)
    s.add_argument("--role", default="member")
    s.add_argument("--notes", default="")
    s.add_argument("--id")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("remove", help="删除成员（永久移除名单与缴费记录）")
    s.add_argument("who", nargs="+", help="一个或多个用户名/邮箱/id")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("export-csv", help="导出 CSV")
    s.set_defaults(func=cmd_export_csv)

    s = sub.add_parser("defaults", help="设置默认定价/续费日，可选批量填充空值")
    s.add_argument("--day", type=int)
    s.add_argument("--price", type=float)
    s.add_argument("--apply", action="store_true", help="把空的 day/price 填成默认值")
    s.set_defaults(func=cmd_defaults)

    return p


def main(argv: list[str] | None = None) -> None:
    global ACTIVE_DOMAIN
    parser = build_parser()
    args = parser.parse_args(argv)
    ACTIVE_DOMAIN = normalize_domain(getattr(args, "domain", None))
    read_only = {cmd_list, cmd_board}
    if args.func in read_only:
        args.func(load(ACTIVE_DOMAIN), args)
    else:
        with domain_lock(ACTIVE_DOMAIN):
            try:
                args.func(load(ACTIVE_DOMAIN), args)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
