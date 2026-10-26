#!/usr/bin/env python3
"""Team 实际成本台账：每个 Claude Team 的月度续费金额（美元）、账单日与按月续费记录。

独立于会员数据与估算盈利：不读写 members.json，不改 finance_metrics 的任何口径。
金额以 Decimal 保存为两位小数字符串（美元）；人民币只在展示时按 USD_CNY 换算。
一个域名可以对应多个 Team；Team 以内部 id 区分，同名不同账单日的 Team 互不影响。
"""

from __future__ import annotations

import copy
import csv
import fcntl
import io
import json
import os
import re
import tempfile
import threading
import uuid
from calendar import monthrange
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from finance_metrics import USD_CNY, today_cn

STORE_VERSION = 1
CENT = Decimal("0.01")
MAX_MONTHLY_USD = Decimal("1000000")
NAME_MAX = 80
CARD_MAX = 40
NOTES_MAX = 500
REMINDER_DAYS = 7
# A card label may carry the last four digits; a longer digit run looks like a full card number.
_CARD_DIGIT_RUN = re.compile(r"\d{5,}")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")

_THREAD_LOCK = threading.RLock()


class TeamNotFound(KeyError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def empty_store() -> dict:
    return {"version": STORE_VERSION, "currency": "USD", "updated_at": None, "teams": []}


# ---------- field parsing ----------

def parse_money_usd(value: Any) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("月度金额必填，须为非负数字")
    text = str(value).strip().replace("$", "").replace(",", "").replace("USD", "").strip()
    if not text:
        raise ValueError("月度金额必填，须为非负数字")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"月度金额无法识别: {value}") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("月度金额须为非负数字")
    if amount > MAX_MONTHLY_USD:
        raise ValueError("月度金额过大")
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_billing_day(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("账单日须为 1-31 的整数")
    if isinstance(value, int):
        day = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError("账单日须为 1-31 的整数")
        day = int(value)
    else:
        match = re.fullmatch(r"\s*(?:每月\s*)?(\d{1,2})\s*(?:日|号)?\s*", str(value))
        if not match:
            raise ValueError(f"账单日无法识别: {value}")
        day = int(match.group(1))
    if not 1 <= day <= 31:
        raise ValueError("账单日须为 1-31 的整数")
    return day


def parse_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise ValueError("团队名称必填")
    if len(name) > NAME_MAX:
        raise ValueError(f"团队名称不能超过 {NAME_MAX} 个字符")
    return name


def parse_domain(value: Any, known_domains: set[str] | None) -> str:
    domain = str(value or "").strip().lower()
    if not domain or not _DOMAIN_RE.match(domain):
        raise ValueError(f"域名无效: {value}")
    if known_domains is not None and domain not in known_domains:
        raise ValueError(f"域名不在会员系统目录中: {domain}")
    return domain


def parse_card_label(value: Any) -> str:
    label = str(value or "").strip()
    if len(label) > CARD_MAX:
        raise ValueError(f"付款卡标签不能超过 {CARD_MAX} 个字符")
    if _CARD_DIGIT_RUN.search(label.replace(" ", "")):
        raise ValueError("付款卡只能保存尾号和银行（例如 •••• 2492 (Revolut)），不能保存完整卡号")
    return label


def parse_notes(value: Any) -> str:
    notes = str(value or "")
    if len(notes) > NOTES_MAX:
        raise ValueError(f"备注不能超过 {NOTES_MAX} 个字符")
    return notes


def parse_month(value: Any) -> str:
    month = str(value or "").strip()
    if not _MONTH_RE.match(month):
        raise ValueError("月份格式须为 YYYY-MM")
    return month


def money_str(amount: Decimal) -> str:
    return format(amount.quantize(CENT, rounding=ROUND_HALF_UP), "f")


def to_cny(amount_usd: Decimal) -> Decimal:
    return (amount_usd * Decimal(str(USD_CNY))).quantize(CENT, rounding=ROUND_HALF_UP)


# ---------- dates ----------

def due_date(billing_day: int, year: int, month: int) -> date:
    return date(year, month, min(int(billing_day), monthrange(year, month)[1]))


def next_due(billing_day: int, today: date) -> date:
    """Next renewal date on or after today; short months clamp to their last day."""
    this_month = due_date(billing_day, today.year, today.month)
    if this_month >= today:
        return this_month
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return due_date(billing_day, year, month)


# ---------- CSV import (Google Sheets export) ----------

_HEADER_ALIASES = {
    "name": ("团队名称", "team", "team name", "name"),
    "domain": ("所属域名", "域名", "domain"),
    "monthly_cost_usd": ("月度续费金额", "月度金额", "monthly cost", "monthly_cost_usd", "amount"),
    "billing_day": ("每月账单日/续费日", "账单日", "续费日", "billing day", "billing_day"),
    "card_label": ("绑定卡号", "付款卡", "card", "card_label"),
    "renewed": ("是否已续费", "已续费", "renewed"),
}


def _match_header(cells: list[str]) -> dict[str, int] | None:
    normalized = [c.strip().lower() for c in cells]
    columns: dict[str, int] = {}
    for field, aliases in _HEADER_ALIASES.items():
        for idx, cell in enumerate(normalized):
            if cell in aliases:
                columns[field] = idx
                break
    required = {"name", "domain", "monthly_cost_usd", "billing_day"}
    return columns if required <= set(columns) else None


def parse_sheet_csv(text: str, known_domains: set[str] | None) -> list[dict]:
    """Parse the cost sheet CSV (title rows above the header are skipped).

    Rows stop at the first blank row or the 合计/total row after the header.
    Any invalid row rejects the whole file before anything is written.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("CSV 内容为空")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    header_at = columns = None
    for idx, cells in enumerate(rows):
        columns = _match_header(cells)
        if columns:
            header_at = idx
            break
    if header_at is None or columns is None:
        raise ValueError("未找到表头：需要包含 团队名称 / 所属域名 / 月度续费金额 / 每月账单日")
    parsed: list[dict] = []
    errors: list[str] = []

    def cell(cells: list[str], field: str) -> str:
        i = columns.get(field)
        return cells[i].strip() if i is not None and i < len(cells) else ""

    for line_no, cells in enumerate(rows[header_at + 1:], start=header_at + 2):
        if not any(c.strip() for c in cells):
            break
        name_cell = cell(cells, "name")
        if name_cell in {"合计", "总计", "total", "Total"} or (not name_cell and not cell(cells, "domain")):
            break
        try:
            renewed_raw = cell(cells, "renewed").lower()
            parsed.append({
                "line": line_no,
                "name": parse_name(name_cell),
                "domain": parse_domain(cell(cells, "domain"), known_domains),
                "monthly_cost_usd": parse_money_usd(cell(cells, "monthly_cost_usd")),
                "billing_day": parse_billing_day(cell(cells, "billing_day")),
                "card_label": parse_card_label(cell(cells, "card_label")),
                "renewed": renewed_raw in {"true", "1", "yes", "是", "已续", "已续费", "✓", "☑"},
            })
        except ValueError as e:
            errors.append(f"第 {line_no} 行: {e}")
    if errors:
        raise ValueError("CSV 校验失败，未导入任何数据：\n" + "\n".join(errors[:20]))
    if not parsed:
        raise ValueError("CSV 中没有 Team 数据行")
    return parsed


def import_key(name: str, domain: str, billing_day: int) -> tuple[str, str, int]:
    return name.strip().casefold(), domain, int(billing_day)


# ---------- store ----------

class TeamCostStore:
    def __init__(self, root: Path):
        self.path = Path(root) / "data" / "team_costs.json"
        self.lock_path = Path(root) / "data" / "team_costs.lock"

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _THREAD_LOCK:
            with open(self.lock_path, "a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def load(self) -> dict:
        if not self.path.exists():
            return empty_store()
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("teams"), list):
            raise RuntimeError("team_costs.json 结构损坏")
        return doc

    def _save(self, doc: dict) -> None:
        doc["version"] = STORE_VERSION
        doc["currency"] = "USD"
        doc["updated_at"] = now_iso()
        text = json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    @staticmethod
    def _find(doc: dict, team_id: Any) -> dict:
        tid = str(team_id or "").strip()
        for team in doc["teams"]:
            if team.get("id") == tid:
                return team
        raise TeamNotFound(f"Team 不存在: {tid}")

    @staticmethod
    def _new_team(fields: dict) -> dict:
        stamp = now_iso()
        return {
            "id": "team-" + uuid.uuid4().hex[:12],
            "name": fields["name"],
            "domain": fields["domain"],
            "monthly_cost_usd": money_str(fields["monthly_cost_usd"]),
            "billing_day": fields["billing_day"],
            "card_label": fields.get("card_label", ""),
            "notes": fields.get("notes", ""),
            "active": True,
            "renewals": {},
            "created_at": stamp,
            "updated_at": stamp,
        }

    def create(self, payload: dict, known_domains: set[str] | None) -> dict:
        fields = {
            "name": parse_name(payload.get("name")),
            "domain": parse_domain(payload.get("domain"), known_domains),
            "monthly_cost_usd": parse_money_usd(payload.get("monthly_cost_usd")),
            "billing_day": parse_billing_day(payload.get("billing_day")),
            "card_label": parse_card_label(payload.get("card_label")),
            "notes": parse_notes(payload.get("notes")),
        }
        with self._locked():
            doc = self.load()
            key = import_key(fields["name"], fields["domain"], fields["billing_day"])
            if any(import_key(t["name"], t["domain"], t["billing_day"]) == key for t in doc["teams"]):
                raise ValueError("同一域名下已存在同名且同账单日的 Team")
            team = self._new_team(fields)
            doc["teams"].append(team)
            self._save(doc)
            return copy.deepcopy(team)

    def update(self, team_id: Any, payload: dict, known_domains: set[str] | None) -> dict:
        editable = {"name", "domain", "monthly_cost_usd", "billing_day", "card_label", "notes", "active"}
        changes = {k: payload[k] for k in editable if k in payload}
        if not changes:
            raise ValueError("没有可更新的字段")
        parsed: dict[str, Any] = {}
        if "name" in changes:
            parsed["name"] = parse_name(changes["name"])
        if "domain" in changes:
            parsed["domain"] = parse_domain(changes["domain"], known_domains)
        if "monthly_cost_usd" in changes:
            parsed["monthly_cost_usd"] = money_str(parse_money_usd(changes["monthly_cost_usd"]))
        if "billing_day" in changes:
            parsed["billing_day"] = parse_billing_day(changes["billing_day"])
        if "card_label" in changes:
            parsed["card_label"] = parse_card_label(changes["card_label"])
        if "notes" in changes:
            parsed["notes"] = parse_notes(changes["notes"])
        if "active" in changes:
            if not isinstance(changes["active"], bool):
                raise ValueError("active 须为 JSON boolean")
            parsed["active"] = changes["active"]
        with self._locked():
            doc = self.load()
            team = self._find(doc, team_id)
            candidate = {**team, **parsed}
            key = import_key(candidate["name"], candidate["domain"], candidate["billing_day"])
            if any(t is not team and import_key(t["name"], t["domain"], t["billing_day"]) == key for t in doc["teams"]):
                raise ValueError("同一域名下已存在同名且同账单日的 Team")
            team.update(parsed)
            team["updated_at"] = now_iso()
            self._save(doc)
            return copy.deepcopy(team)

    def delete(self, team_id: Any, confirm_id: Any) -> dict:
        if str(confirm_id or "") != str(team_id or ""):
            raise ValueError("确认 id 必须与删除的 Team id 一致")
        with self._locked():
            doc = self.load()
            team = self._find(doc, team_id)
            doc["teams"] = [t for t in doc["teams"] if t is not team]
            self._save(doc)
            return copy.deepcopy(team)

    def set_renewal(self, team_id: Any, month: Any, renewed: Any, amount_usd: Any = None, note: Any = "") -> dict:
        month = parse_month(month)
        if not isinstance(renewed, bool):
            raise ValueError("renewed 须为 JSON boolean")
        note = parse_notes(note)
        with self._locked():
            doc = self.load()
            team = self._find(doc, team_id)
            renewals = team.setdefault("renewals", {})
            if renewed:
                amount = parse_money_usd(team["monthly_cost_usd"] if amount_usd in (None, "") else amount_usd)
                renewals[month] = {"renewed": True, "amount_usd": money_str(amount), "note": note, "recorded_at": now_iso()}
            else:
                renewals.pop(month, None)
            team["updated_at"] = now_iso()
            self._save(doc)
            return copy.deepcopy(team)

    def import_csv(self, text: str, known_domains: set[str] | None, *, dry_run: bool, month: Any = None) -> dict:
        rows = parse_sheet_csv(text, known_domains)
        renew_month = parse_month(month) if month not in (None, "") else None
        with self._locked():
            doc = self.load()
            by_key = {import_key(t["name"], t["domain"], t["billing_day"]): t for t in doc["teams"]}
            seen: set[tuple] = set()
            plan = []
            for row in rows:
                key = import_key(row["name"], row["domain"], row["billing_day"])
                if key in seen:
                    raise ValueError(f"第 {row['line']} 行与前面的行重复（同名、同域名、同账单日）")
                seen.add(key)
                existing = by_key.get(key)
                cost = money_str(row["monthly_cost_usd"])
                if existing is None:
                    action = "add"
                elif existing["monthly_cost_usd"] != cost or (row["card_label"] and existing.get("card_label") != row["card_label"]):
                    action = "update"
                else:
                    action = "unchanged"
                mark = bool(renew_month and row["renewed"])
                plan.append({**row, "monthly_cost_usd": cost, "action": action, "mark_renewed": mark})
            summary = {a: sum(1 for p in plan if p["action"] == a) for a in ("add", "update", "unchanged")}
            summary["mark_renewed"] = sum(1 for p in plan if p["mark_renewed"])
            if not dry_run:
                for item in plan:
                    key = import_key(item["name"], item["domain"], item["billing_day"])
                    team = by_key.get(key)
                    if team is None:
                        team = self._new_team({**item, "monthly_cost_usd": Decimal(item["monthly_cost_usd"])})
                        doc["teams"].append(team)
                        by_key[key] = team
                    elif item["action"] == "update":
                        team["monthly_cost_usd"] = item["monthly_cost_usd"]
                        if item["card_label"]:
                            team["card_label"] = item["card_label"]
                        team["updated_at"] = now_iso()
                    if item["mark_renewed"] and renew_month not in team.setdefault("renewals", {}):
                        team["renewals"][renew_month] = {
                            "renewed": True, "amount_usd": team["monthly_cost_usd"],
                            "note": "表格导入", "recorded_at": now_iso(),
                        }
                self._save(doc)
            for item in plan:
                item.pop("line", None)
            return {"dry_run": dry_run, "month": renew_month, "summary": summary, "rows": plan}


# ---------- read view ----------

def build_view(
    doc: dict,
    *,
    month: str,
    today: date | None = None,
    catalog_domains: list[str],
    member_revenue_cny: dict[str, dict] | None = None,
) -> dict:
    """Teams with computed due dates plus month progress and per-domain real margin.

    member_revenue_cny maps domain -> {"revenue_cny": float, "priced": int}; it is the
    sum of current monthly member prices, shown beside actual cost without changing
    the board's estimated-profit KPIs.
    """
    month = parse_month(month)
    today = today or today_cn()
    member_revenue_cny = member_revenue_cny or {}
    catalog = set(catalog_domains)
    teams_out = []
    total = renewed_amount = pending_amount = due_soon_amount = Decimal("0")
    renewed_count = pending_count = 0
    due_soon = []
    per_domain: dict[str, dict] = {}
    for team in doc.get("teams", []):
        cost = Decimal(team["monthly_cost_usd"])
        nd = next_due(team["billing_day"], today)
        due_month = f"{nd.year:04d}-{nd.month:02d}"
        record = (team.get("renewals") or {}).get(month)
        due_record = (team.get("renewals") or {}).get(due_month)
        y, m = (int(x) for x in month.split("-"))
        item = {
            **copy.deepcopy(team),
            "monthly_cost_cny": money_str(to_cny(cost)),
            "in_catalog": team["domain"] in catalog,
            "next_due": nd.isoformat(),
            "days_until_due": (nd - today).days,
            "due_month": due_month,
            "due_month_renewed": bool(due_record and due_record.get("renewed")),
            "month_due_date": due_date(team["billing_day"], y, m).isoformat(),
            "month_record": record,
            "month_renewed": bool(record and record.get("renewed")),
        }
        teams_out.append(item)
        if not team.get("active", True):
            continue
        total += cost
        bucket = per_domain.setdefault(team["domain"], {"team_count": 0, "cost_usd": Decimal("0")})
        bucket["team_count"] += 1
        bucket["cost_usd"] += cost
        if item["month_renewed"]:
            renewed_count += 1
            renewed_amount += Decimal(record.get("amount_usd") or team["monthly_cost_usd"])
        else:
            pending_count += 1
            pending_amount += cost
        if 0 <= item["days_until_due"] <= REMINDER_DAYS and not item["due_month_renewed"]:
            due_soon.append({"id": team["id"], "name": team["name"], "domain": team["domain"],
                             "next_due": item["next_due"], "days_until_due": item["days_until_due"],
                             "monthly_cost_usd": team["monthly_cost_usd"]})
            due_soon_amount += cost
    due_soon.sort(key=lambda x: (x["days_until_due"], x["name"]))
    teams_out.sort(key=lambda t: (t["billing_day"], t["domain"], t["name"].casefold()))

    domains_out = []
    revenue_total = Decimal("0")
    for domain in sorted(set(per_domain) | catalog):
        bucket = per_domain.get(domain, {"team_count": 0, "cost_usd": Decimal("0")})
        rev = member_revenue_cny.get(domain) or {}
        revenue = Decimal(str(rev.get("revenue_cny") or 0)).quantize(CENT, rounding=ROUND_HALF_UP)
        cost_cny = to_cny(bucket["cost_usd"])
        margin = revenue - cost_cny
        if domain in catalog:
            revenue_total += revenue
        domains_out.append({
            "domain": domain,
            "in_catalog": domain in catalog,
            "team_count": bucket["team_count"],
            "cost_usd": money_str(bucket["cost_usd"]),
            "cost_cny": money_str(cost_cny),
            "member_revenue_cny": money_str(revenue),
            "priced_members": int(rev.get("priced") or 0),
            "margin_cny": money_str(margin),
            "margin_rate": (float((margin / revenue * 100).quantize(Decimal("0.1"))) if revenue > 0 else None),
        })
    domains_out.sort(key=lambda d: (-Decimal(d["cost_usd"]), d["domain"]))
    total_cny = to_cny(total)
    margin_total = revenue_total - total_cny
    active_count = sum(1 for t in doc.get("teams", []) if t.get("active", True))
    return {
        "month": month,
        "today": today.isoformat(),
        "usd_cny": USD_CNY,
        "reminder_days": REMINDER_DAYS,
        "updated_at": doc.get("updated_at"),
        "summary": {
            "team_count": len(doc.get("teams", [])),
            "active_count": active_count,
            "monthly_total_usd": money_str(total),
            "monthly_total_cny": money_str(total_cny),
            "renewed_count": renewed_count,
            "renewed_amount_usd": money_str(renewed_amount),
            "pending_count": pending_count,
            "pending_amount_usd": money_str(pending_amount),
            "due_soon_count": len(due_soon),
            "due_soon_amount_usd": money_str(due_soon_amount),
            "member_revenue_cny": money_str(revenue_total),
            "margin_cny": money_str(margin_total),
            "margin_rate": (float((margin_total / revenue_total * 100).quantize(Decimal("0.1"))) if revenue_total > 0 else None),
        },
        "due_soon": due_soon,
        "domains": domains_out,
        "teams": teams_out,
    }
