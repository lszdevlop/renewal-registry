#!/usr/bin/env python3
"""续费登记看板：多域静态文件 + 缴费切换 / 字段编辑 / 导出包同步 API。"""

from __future__ import annotations

import cgi
import copy
import fcntl
import gzip
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from domain_catalog import DomainCatalogError, make_live_manager

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOMAINS_DIR = DATA_DIR / "domains"
# Legacy single-file path (pre multi-domain). Still written as mirror of default domain.
LEGACY_DATA_PATH = DATA_DIR / "members.json"
HOST = "0.0.0.0"
PORT = 8765

# Canonical domains shown as board tabs (order = tab order).
DOMAIN_CATALOG = [
    {"id": "lsznode.de", "label": "lsznode.de"},
    {"id": "328001.xyz", "label": "328001.xyz"},
    {"id": "peaceai.de", "label": "peaceai.de"}]
DEFAULT_DOMAIN = DOMAIN_CATALOG[0]["id"]
DOMAIN_IDS = {d["id"] for d in DOMAIN_CATALOG}
_DOMAIN_LOCKS = {d: threading.RLock() for d in DOMAIN_IDS}
JSON_BODY_MAX_BYTES = 1024 * 1024
DOMAIN_MANAGER = make_live_manager(ROOT)


def get_domain_catalog() -> list[dict]:
    return DOMAIN_MANAGER.read()["domains"]


def get_domain_ids() -> set[str]:
    return {d["id"] for d in get_domain_catalog()}

# File-versioned read caches. The mtime/size key means writes from the CLI or
# another process invalidate automatically without coordination.
_DOMAIN_CACHE_LOCK = threading.RLock()
_DOMAIN_DATA_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
_DOMAIN_RESPONSE_CACHE: dict[str, tuple[tuple[int, int], bytes, bytes, str]] = {}
_INDEX_CACHE_LOCK = threading.RLock()
_INDEX_RESPONSE_CACHE: tuple[tuple[int, int], bytes, bytes, str] | None = None
_DOMAINS_RESPONSE_CACHE: tuple[tuple[tuple[int, int], ...], bytes, bytes, str] | None = None

# Finance KPI cache (hourly on the hour). Board reads this instead of re-scanning all members.
_FINANCE_CACHE_LOCK = threading.RLock()
_FINANCE_RECOMPUTE_LOCK = threading.Lock()
_FINANCE_CACHE_MEM: dict | None = None
_FINANCE_REFRESH_THREAD: threading.Thread | None = None
_FINANCE_MUTATION_THREAD: threading.Thread | None = None
_FINANCE_STOP = threading.Event()
_FINANCE_REFRESH_EVENT = threading.Event()
_FINANCE_REASON_LOCK = threading.Lock()
_FINANCE_PENDING_REASON = "mutation"


@contextmanager
def domain_transaction(domain: str):
    """Serialize load→mutate→save across HTTP threads and CLI processes."""
    d = normalize_domain(domain)
    lock_path = domain_dir(d) / ".registry.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = _DOMAIN_LOCKS.setdefault(d, threading.RLock())
    with lock, lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


# Import sync helpers from CLI module (same directory)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from renewal_cli import (  # noqa: E402
    load_users_json_bytes,
    sync_members_from_users,
)
from finance_metrics import (  # noqa: E402
    aggregate_domains,
    cache_path as finance_cache_path,
    read_cache as read_finance_cache_file,
    write_cache as write_finance_cache_file,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def finance_cache_file() -> Path:
    return finance_cache_path(DATA_DIR)


def recompute_finance_cache(*, reason: str = "manual") -> dict:
    """Scan all domains once, write data/finance_cache.json, update memory."""
    # Startup/hourly/API/mutation refreshes share one scan/write lane.
    with _FINANCE_RECOMPUTE_LOCK:
        domain_ids = [d["id"] for d in get_domain_catalog()]

        def _load(did: str) -> dict:
            return load_data(did)

        payload = aggregate_domains(_load, domain_ids)
        payload["reason"] = reason
        payload["computed_at"] = now_iso()
        path = finance_cache_file()
        write_finance_cache_file(path, payload)
        global _FINANCE_CACHE_MEM
        with _FINANCE_CACHE_LOCK:
            _FINANCE_CACHE_MEM = payload
        print(f"[renewal] finance cache refreshed reason={reason} as_of_date={payload.get('as_of_date')}")
        return payload


def get_finance_cache(*, force: bool = False) -> dict:
    global _FINANCE_CACHE_MEM
    if force:
        return recompute_finance_cache(reason="force")
    with _FINANCE_CACHE_LOCK:
        if _FINANCE_CACHE_MEM is not None:
            return _FINANCE_CACHE_MEM
    disk = read_finance_cache_file(finance_cache_file())
    if disk:
        with _FINANCE_CACHE_LOCK:
            _FINANCE_CACHE_MEM = disk
        return disk
    return recompute_finance_cache(reason="cold")


def schedule_finance_refresh(reason: str = "mutation") -> None:
    """Signal one shared worker; burst writes collapse into one recompute."""
    global _FINANCE_PENDING_REASON
    with _FINANCE_REASON_LOCK:
        _FINANCE_PENDING_REASON = reason
    _FINANCE_REFRESH_EVENT.set()


def _finance_refresh_loop() -> None:
    while not _FINANCE_STOP.is_set():
        if not _FINANCE_REFRESH_EVENT.wait(1.0):
            continue
        # True debounce: wait until writes have been quiet for 400ms.
        while not _FINANCE_STOP.wait(0.4):
            _FINANCE_REFRESH_EVENT.clear()
            if not _FINANCE_REFRESH_EVENT.wait(0.4):
                break
        if _FINANCE_STOP.is_set():
            break
        with _FINANCE_REASON_LOCK:
            reason = _FINANCE_PENDING_REASON
        try:
            recompute_finance_cache(reason=reason)
        except Exception as e:
            print(f"[renewal] finance cache refresh failed: {e}")


def _seconds_until_next_hour() -> float:
    now = datetime.now(timezone.utc)
    nxt = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return max(1.0, (nxt - now).total_seconds())


def _finance_hourly_loop() -> None:
    # Align first wait to next clock hour, then every 3600s.
    while not _FINANCE_STOP.is_set():
        wait = _seconds_until_next_hour()
        if _FINANCE_STOP.wait(wait):
            break
        try:
            recompute_finance_cache(reason="hourly")
        except Exception as e:
            print(f"[renewal] hourly finance cache failed: {e}")


def start_finance_scheduler() -> None:
    global _FINANCE_REFRESH_THREAD, _FINANCE_MUTATION_THREAD
    if _FINANCE_REFRESH_THREAD and _FINANCE_REFRESH_THREAD.is_alive():
        return
    _FINANCE_STOP.clear()
    _FINANCE_REFRESH_EVENT.clear()
    # Warm cache on boot in background so HTTP starts immediately.
    threading.Thread(
        target=lambda: recompute_finance_cache(reason="startup"),
        name="finance-startup",
        daemon=True,
    ).start()
    _FINANCE_REFRESH_THREAD = threading.Thread(
        target=_finance_hourly_loop, name="finance-hourly", daemon=True
    )
    _FINANCE_REFRESH_THREAD.start()
    _FINANCE_MUTATION_THREAD = threading.Thread(
        target=_finance_refresh_loop, name="finance-refresh-worker", daemon=True
    )
    _FINANCE_MUTATION_THREAD.start()


def empty_registry(domain: str) -> dict:
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


def normalize_domain(raw: str | None) -> str:
    domain = (raw or "").strip().lower()
    if not domain:
        return DEFAULT_DOMAIN
    # allow path-safe aliases
    domain = domain.replace(" ", "")
    domain_ids = get_domain_ids()
    if domain not in domain_ids:
        # tolerate bare host without trailing slash etc.
        for d in domain_ids:
            if domain == d.lower():
                return d
        raise ValueError(f"unknown domain: {raw!r}; known: {', '.join(sorted(domain_ids))}")
    return domain


def domain_dir(domain: str) -> Path:
    return DOMAINS_DIR / domain


def data_path(domain: str) -> Path:
    return domain_dir(domain) / "members.json"


def _file_version(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_mtime_ns, st.st_size


def invalidate_domain_cache(domain: str) -> None:
    with _DOMAIN_CACHE_LOCK:
        _DOMAIN_DATA_CACHE.pop(domain, None)
        _DOMAIN_RESPONSE_CACHE.pop(domain, None)


def domain_response_payload(domain: str) -> tuple[bytes, bytes, str]:
    """Return identity/gzip/etag variants cached by on-disk file version."""
    domain = normalize_domain(domain)
    path = ensure_domain_file(domain)
    version = _file_version(path)
    with _DOMAIN_CACHE_LOCK:
        cached = _DOMAIN_RESPONSE_CACHE.get(domain)
        if cached and cached[0] == version:
            return cached[1], cached[2], cached[3]
    data = load_data(domain)
    body = json.dumps(
        {"ok": True, "domain": domain, "data": data},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    zipped = gzip.compress(body, compresslevel=5)
    etag = '"' + hashlib.sha256(body).hexdigest()[:24] + '"'
    with _DOMAIN_CACHE_LOCK:
        _DOMAIN_RESPONSE_CACHE[domain] = (version, body, zipped, etag)
    return body, zipped, etag


def index_response_payload() -> tuple[bytes, bytes, str]:
    global _INDEX_RESPONSE_CACHE
    path = ROOT / "index.html"
    version = _file_version(path)
    with _INDEX_CACHE_LOCK:
        cached = _INDEX_RESPONSE_CACHE
        if cached and cached[0] == version:
            return cached[1], cached[2], cached[3]
    body = path.read_bytes()
    zipped = gzip.compress(body, compresslevel=5)
    etag = '"' + hashlib.sha256(body).hexdigest()[:24] + '"'
    with _INDEX_CACHE_LOCK:
        _INDEX_RESPONSE_CACHE = (version, body, zipped, etag)
    return body, zipped, etag


def domains_response_payload() -> tuple[bytes, bytes, str]:
    global _DOMAINS_RESPONSE_CACHE
    catalog_path = DOMAIN_MANAGER.catalog_path
    versions = (_file_version(catalog_path),) + tuple(
        _file_version(ensure_domain_file(d["id"])) for d in get_domain_catalog()
    )
    with _DOMAIN_CACHE_LOCK:
        cached = _DOMAINS_RESPONSE_CACHE
        if cached and cached[0] == versions:
            return cached[1], cached[2], cached[3]
    body = json.dumps(
        {"ok": True, "default": DEFAULT_DOMAIN, "domains": list_domains()},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    zipped = gzip.compress(body, compresslevel=5)
    etag = '"' + hashlib.sha256(body).hexdigest()[:24] + '"'
    with _DOMAIN_CACHE_LOCK:
        _DOMAINS_RESPONSE_CACHE = (versions, body, zipped, etag)
    return body, zipped, etag


def ensure_domain_file(domain: str) -> Path:
    path = data_path(domain)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Migrate legacy single-file into default domain once.
        if domain == DEFAULT_DOMAIN and LEGACY_DATA_PATH.exists():
            blob = LEGACY_DATA_PATH.read_text(encoding="utf-8")
            try:
                obj = json.loads(blob)
            except json.JSONDecodeError:
                obj = empty_registry(domain)
            obj.setdefault("meta", {})["domain"] = domain
            path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            path.write_text(
                json.dumps(empty_registry(domain), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    return path


def load_data(domain: str | None = None) -> dict:
    domain = normalize_domain(domain)
    path = ensure_domain_file(domain)
    version = _file_version(path)
    with _DOMAIN_CACHE_LOCK:
        cached = _DOMAIN_DATA_CACHE.get(domain)
        if cached and cached[0] == version:
            # Callers mutate registries before save; never expose the cached object.
            return copy.deepcopy(cached[1])
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("meta", {})["domain"] = domain
    with _DOMAIN_CACHE_LOCK:
        _DOMAIN_DATA_CACHE[domain] = (version, data)
    return copy.deepcopy(data)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def save_data(data: dict, domain: str | None = None) -> None:
    domain = normalize_domain(domain or (data.get("meta") or {}).get("domain") or DEFAULT_DOMAIN)
    data.setdefault("meta", {})["domain"] = domain
    data["meta"]["updated_at"] = now_iso()
    ensure_domain_file(domain)
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    _atomic_write_text(data_path(domain), text)
    if domain == DEFAULT_DOMAIN:
        _atomic_write_text(LEGACY_DATA_PATH, text)
    invalidate_domain_cache(domain)
    # Keep top KPI cache reasonably fresh without blocking the request path.
    schedule_finance_refresh(reason=f"save:{domain}")


def list_domains() -> list[dict]:
    out = []
    for d in get_domain_catalog():
        did = d["id"]
        try:
            data = load_data(did)
            count = len(data.get("members") or [])
            updated = (data.get("meta") or {}).get("updated_at")
        except Exception:
            count = 0
            updated = None
        out.append(
            {
                "id": did,
                "label": d["label"],
                "member_count": count,
                "updated_at": updated,
                "data_path": f"data/domains/{did}/members.json",
                "default": did == DEFAULT_DOMAIN,
            }
        )
    return out


def extract_domain(payload: dict | None = None, query: dict | None = None) -> str:
    """Resolve domain from JSON body, query string, or default."""
    if payload and payload.get("domain"):
        return normalize_domain(str(payload.get("domain")))
    if query and query.get("domain"):
        val = query["domain"][0] if isinstance(query["domain"], list) else query["domain"]
        return normalize_domain(str(val))
    return DEFAULT_DOMAIN


def find_member(data: dict, key: str) -> dict | None:
    key_l = (key or "").strip().lower()
    if not key_l:
        return None
    for m in data.get("members", []):
        if (m.get("id") or "").lower() == key_l:
            return m
        if (m.get("username") or "").lower() == key_l:
            return m
        if (m.get("email") or "").lower() == key_l:
            return m
    return None


def is_paid_month(member: dict, year_month: str) -> bool:
    return any(p.get("month") == year_month and p.get("paid") for p in (member.get("payments") or []))


def set_paid(member: dict, year_month: str, paid: bool, amount=None, note: str = "") -> dict:
    payments = member.setdefault("payments", [])
    existing = next((p for p in payments if p.get("month") == year_month), None)
    if paid:
        rec = {
            "month": year_month,
            "paid": True,
            "amount": amount if amount is not None else member.get("price"),
            "note": note or (existing.get("note") if existing else "") or "看板一键标记",
            "recorded_at": now_iso(),
        }
        if existing:
            existing.update(rec)
        else:
            payments.append(rec)
    else:
        member["payments"] = [p for p in payments if p.get("month") != year_month]
    member["updated_at"] = now_iso()
    return next((p for p in (member.get("payments") or []) if p.get("month") == year_month), None) or {
        "month": year_month,
        "paid": False,
    }


def parse_billing_day(value):
    if value is None or value == "":
        return None
    try:
        day = int(value)
    except (TypeError, ValueError):
        raise ValueError("billing_day must be integer 1-31 or empty")
    if not 1 <= day <= 31:
        raise ValueError("billing_day must be 1-31 or empty")
    return day


def parse_activation_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("activation_date must be YYYY-MM-DD or empty")
    text = str(value).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("activation_date must be YYYY-MM-DD or empty")
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError("activation_date must be a valid YYYY-MM-DD date or empty") from exc


def parse_price(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("price must be a finite non-negative number or empty")
    try:
        price = float(value)
    except (TypeError, ValueError):
        raise ValueError("price must be a finite non-negative number or empty")
    if not math.isfinite(price) or price < 0:
        raise ValueError("price must be a finite non-negative number or empty")
    return price


def parse_month(value) -> str:
    month = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("month must be YYYY-MM")
    if not 1 <= int(month[5:7]) <= 12:
        raise ValueError("month must be YYYY-MM with month 01-12")
    return month


def parse_amount(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("amount must be a finite non-negative number or empty")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError("amount must be a finite non-negative number or empty")
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("amount must be a finite non-negative number or empty")
    return amount


def update_member_fields(member: dict, payload: dict) -> dict:
    changed = {}
    if "activation_date" in payload:
        activation_date = parse_activation_date(payload.get("activation_date"))
        member["activation_date"] = activation_date
        changed["activation_date"] = activation_date
    if "billing_day" in payload:
        day = parse_billing_day(payload.get("billing_day"))
        member["billing_day"] = day
        changed["billing_day"] = day
    if "price" in payload:
        price = parse_price(payload.get("price"))
        member["price"] = price
        changed["price"] = price
    if "notes" in payload:
        member["notes"] = str(payload.get("notes") or "")
        changed["notes"] = member["notes"]
    if not changed:
        raise ValueError("no editable fields provided (activation_date/billing_day/price/notes)")
    member["updated_at"] = now_iso()
    return changed


def create_member(data: dict, payload: dict) -> dict:
    username = str(payload.get("username") or "").strip()
    email = str(payload.get("email") or "").strip()
    if not username:
        raise ValueError("username is required")
    if not email:
        raise ValueError("email is required")
    if "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError("email format looks invalid")

    for m in data.get("members", []):
        if (m.get("username") or "").lower() == username.lower():
            raise ValueError(f"username already exists: {username}")
        if (m.get("email") or "").lower() == email.lower():
            raise ValueError(f"email already exists: {email}")

    activation_date = parse_activation_date(payload.get("activation_date")) if "activation_date" in payload else None
    billing_day = parse_billing_day(payload.get("billing_day")) if "billing_day" in payload else None
    price = parse_price(payload.get("price")) if "price" in payload else None

    member = {
        "id": str(payload.get("id") or f"manual-{uuid.uuid4().hex}"),
        "username": username,
        "email": email,
        "role": payload.get("role") or "member",
        "activation_date": activation_date,
        "billing_day": billing_day,
        "price": price,
        "status": "active",
        "notes": str(payload.get("notes") or ""),
        "payments": [],
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    existing_ids = {(m.get("id") or "") for m in data.get("members", [])}
    if member["id"] in existing_ids:
        member["id"] = f"manual-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"

    data.setdefault("members", []).append(member)
    return member


def delete_member(data: dict, who: str) -> dict:
    """Remove member by id/username/email. Permanent; payments history goes with it."""
    who = (who or "").strip()
    if not who:
        raise ValueError("id/username/email is required")
    members = data.setdefault("members", [])
    target = find_member(data, who)
    if not target:
        raise KeyError(f"member not found: {who}")
    tid = target.get("id")
    tuser = (target.get("username") or "").lower()
    temail = (target.get("email") or "").lower()
    kept = []
    removed = None
    for m in members:
        match = False
        if tid and m.get("id") == tid:
            match = True
        elif tuser and (m.get("username") or "").lower() == tuser:
            match = True
        elif temail and (m.get("email") or "").lower() == temail:
            match = True
        if match and removed is None:
            removed = m
            continue
        kept.append(m)
    if removed is None:
        raise KeyError(f"member not found: {who}")
    data["members"] = kept
    return removed


def _truthy(val) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    return str(val).strip().lower() in {"1", "true", "yes", "on", "y"}


def extract_users_from_upload(filename: str, blob: bytes) -> list[dict[str, str]]:
    """Parse Claude export zip or users.json bytes into normalized users."""
    name = (filename or "").lower()
    if name.endswith(".zip") or (len(blob) >= 2 and blob[:2] == b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                names = zf.namelist()
                cand = None
                for n in names:
                    base = n.rstrip("/").split("/")[-1]
                    if base == "users.json" and not n.endswith("/"):
                        cand = n
                        if n == "users.json" or n.count("/") == 0:
                            break
                if not cand:
                    raise ValueError("zip 内未找到 users.json（请上传 Claude Team 导出包）")
                return load_users_json_bytes(zf.read(cand))
        except zipfile.BadZipFile as e:
            raise ValueError(f"无效 zip: {e}") from e

    # raw users.json (or members-like array — normalize will accept common shapes)
    return load_users_json_bytes(blob)


def infer_domain_from_email(email: str | None, fallback: str) -> str:
    """Map member email host to a catalog domain when possible."""
    host = ""
    if email and "@" in email:
        host = email.rsplit("@", 1)[-1].strip().lower()
    domain_ids = get_domain_ids()
    if host in domain_ids:
        return host
    # rare: subdomain.example.com where catalog has example.com
    for did in domain_ids:
        if host.endswith("." + did):
            return did
    return fallback


def infer_fallback_domain(filename: str | None, users: list[dict], hint: str | None) -> str:
    """Pick fallback domain: explicit hint > filename contains domain id > majority email host > default."""
    if hint and str(hint).strip().lower() not in {"", "auto", "smart", "*"}:
        try:
            return normalize_domain(str(hint))
        except Exception:
            pass
    name = (filename or "").lower()
    domain_ids = get_domain_ids()
    for did in sorted(domain_ids, key=len, reverse=True):
        if did.lower() in name:
            return did
    counts: dict[str, int] = {}
    for u in users or []:
        email = (u.get("email") or "").strip().lower()
        if "@" not in email:
            continue
        host = email.rsplit("@", 1)[-1]
        if host in domain_ids:
            counts[host] = counts.get(host, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return DEFAULT_DOMAIN


def group_users_by_domain(
    users: list[dict[str, str]],
    *,
    filename: str | None = None,
    domain_hint: str | None = None,
) -> tuple[dict[str, list[dict[str, str]]], str]:
    """Split export users into per-catalog-domain buckets for smart import."""
    fallback = infer_fallback_domain(filename, users, domain_hint)
    groups: dict[str, list[dict[str, str]]] = {d: [] for d in get_domain_ids()}
    # preserve catalog order later; also allow only used keys
    used: dict[str, list[dict[str, str]]] = {}
    for u in users or []:
        d = infer_domain_from_email(u.get("email"), fallback)
        used.setdefault(d, []).append(u)
    # drop empty
    return used, fallback


class OptimizedThreadingHTTPServer(ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True
    allow_reuse_address = True


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        if os.environ.get("RENEWAL_ACCESS_LOG", "").lower() in {"1", "true", "yes", "on"}:
            print(f"[renewal] {self.address_string()} {fmt % args}")

    def _send_bytes(
        self,
        code: int,
        body: bytes,
        *,
        content_type: str,
        cache_control: str = "no-store",
        gzip_body: bytes | None = None,
        etag: str | None = None,
    ) -> None:
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_control)
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            return
        accepts_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        payload = gzip_body if accepts_gzip and gzip_body is not None else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Connection", "keep-alive")
        if gzip_body is not None:
            self.send_header("Vary", "Accept-Encoding")
        if accepts_gzip and gzip_body is not None:
            self.send_header("Content-Encoding", "gzip")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        zipped = gzip.compress(body, compresslevel=5) if len(body) >= 1024 else None
        self._send_bytes(
            code,
            body,
            content_type="application/json; charset=utf-8",
            gzip_body=zipped,
        )

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > JSON_BODY_MAX_BYTES:
            raise ValueError(f"json body too large (limit {JSON_BODY_MAX_BYTES} bytes)")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError("json body must be an object")
        return payload

    def _read_body_bytes(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_OPTIONS(self) -> None:
        self._send_json(405, {"ok": False, "error": "cross-origin requests are not enabled"})

    def do_HEAD(self) -> None:
        # Use the same allowlist/API routing as GET, but _send_bytes omits the body.
        self.do_GET()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path == "/api/domains":
            try:
                body, zipped, etag = domains_response_payload()
                self._send_bytes(
                    200,
                    body,
                    content_type="application/json; charset=utf-8",
                    cache_control="private, max-age=0, must-revalidate",
                    gzip_body=zipped,
                    etag=etag,
                )
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path in {"/api/data", "/api/members"}:
            try:
                domain = extract_domain(query=qs)
                body, zipped, etag = domain_response_payload(domain)
                self._send_bytes(
                    200,
                    body,
                    content_type="application/json; charset=utf-8",
                    cache_control="private, max-age=0, must-revalidate",
                    gzip_body=zipped,
                    etag=etag,
                )
            except ValueError as e:
                self._send_json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path in {"/api/finance-summary", "/api/finance-cache"}:
            try:
                force = _truthy((qs.get("force") or [""])[0]) if qs.get("force") else False
                cache = get_finance_cache(force=force)
                self._send_json(200, cache if cache.get("ok") else {"ok": True, **cache})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        # Compat: /data/members.json?domain=xxx serves that domain's file content
        if path == "/data/members.json":
            try:
                domain = extract_domain(query=qs)
                data = load_data(domain)
                self._send_json(200, data)
            except ValueError as e:
                self._send_json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        # Static allowlist: the board needs only index.html; data is served via APIs above.
        if path in {"/", "/index.html"}:
            body, zipped, etag = index_response_payload()
            self._send_bytes(
                200,
                body,
                content_type="text/html; charset=utf-8",
                cache_control="no-cache, must-revalidate",
                gzip_body=zipped,
                etag=etag,
            )
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)

        if path == "/api/sync-export":
            self._handle_sync_export(qs)
            return

        if path in {"/api/finance-refresh", "/api/finance-summary/refresh"}:
            try:
                cache = recompute_finance_cache(reason="api")
                self._send_json(200, cache)
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/domain-management":
            try:
                origin = (self.headers.get("Origin") or "").strip()
                host = (self.headers.get("Host") or "").strip()
                if origin and urlparse(origin).netloc != host:
                    raise DomainCatalogError("域名管理仅允许同源页面操作")
                payload = self._read_json()
                action = str(payload.get("action") or "").strip().lower()
                domain_text = str(payload.get("domain") or "").strip().lower()
                if action == "add":
                    result = DOMAIN_MANAGER.add(domain_text)
                elif action == "rename":
                    if str(payload.get("confirm_domain") or "").strip().lower() != domain_text:
                        raise DomainCatalogError("确认文本必须与原域名完全一致")
                    result = DOMAIN_MANAGER.rename(
                        domain_text,
                        str(payload.get("new_domain") or ""),
                        confirm_clear=payload.get("confirm_clear") is True,
                    )
                elif action == "delete":
                    if str(payload.get("confirm_domain") or "").strip().lower() != domain_text:
                        raise DomainCatalogError("确认文本必须与删除域名完全一致")
                    result = DOMAIN_MANAGER.delete(
                        domain_text,
                        confirm=payload.get("confirm") is True,
                    )
                else:
                    raise DomainCatalogError("action 仅支持 add / rename / delete")
                with _DOMAIN_CACHE_LOCK:
                    global _DOMAINS_RESPONSE_CACHE
                    _DOMAINS_RESPONSE_CACHE = None
                    _DOMAIN_DATA_CACHE.clear()
                    _DOMAIN_RESPONSE_CACHE.clear()
                schedule_finance_refresh(reason=f"domain:{action}")
                self._send_json(200, {"ok": True, **result})
            except (DomainCatalogError, ValueError) as e:
                self._send_json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": f"域名管理失败: {e}"})
            return

        try:
            payload = self._read_json()
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return
        except ValueError as e:
            self._send_json(413 if "too large" in str(e) else 400, {"ok": False, "error": str(e)})
            return

        if path == "/api/toggle-paid":
            self._handle_toggle_paid(payload, qs)
            return
        if path == "/api/update-member":
            self._handle_update_member(payload, qs)
            return
        if path == "/api/add-member":
            self._handle_add_member(payload, qs)
            return
        if path == "/api/delete-member":
            self._handle_delete_member(payload, qs)
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def _handle_sync_export(self, qs: dict) -> None:
        """Accept Claude Team export zip or users.json and merge into domain members.json.

        Content types:
        - multipart/form-data field `file` (preferred from board UI)
        - application/zip / application/octet-stream raw body
        - application/json body that is either users array or {users:[...], mark_missing?, domain?}
        Query/form flag mark_missing=1 marks seats gone from export.
        domain via query/form/json (default lsznode.de).
        """
        MAX_BYTES = 500 * 1024 * 1024  # 500MB — Claude Team export zip often 50–200MB+
        try:
            ctype = (self.headers.get("Content-Type") or "").lower()
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BYTES:
                mb = MAX_BYTES // (1024 * 1024)
                self._send_json(
                    413,
                    {
                        "ok": False,
                        "error": f"file too large (>{MAX_BYTES} bytes / {mb}MB)。Claude 导出 zip 过大时请拆分或只上传 users.json；当前上限 {mb}MB。",
                    },
                )
                return

            filename = "upload.bin"
            blob: bytes | None = None
            mark_missing = False
            domain_hint = None
            if "mark_missing" in qs:
                mark_missing = _truthy(qs["mark_missing"][0])
            if "domain" in qs:
                domain_hint = qs["domain"][0]

            if "multipart/form-data" in ctype:
                # FieldStorage needs a file-like body; consume Content-Length ourselves.
                env = {
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": str(length),
                }
                form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=env)
                if "mark_missing" in form:
                    mark_missing = _truthy(form.getvalue("mark_missing"))
                if "domain" in form:
                    domain_hint = form.getvalue("domain") or domain_hint
                item = form["file"] if "file" in form else None
                if item is None:
                    # fallback: first file field
                    for key in form.keys():
                        candidate = form[key]
                        if getattr(candidate, "filename", None):
                            item = candidate
                            break
                if item is None or not getattr(item, "filename", None):
                    self._send_json(400, {"ok": False, "error": "multipart 需包含 file 字段（zip 或 users.json）"})
                    return
                filename = Path(item.filename).name or "upload.bin"
                blob = item.file.read()
            elif "application/json" in ctype:
                raw = self._read_body_bytes()
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError) as e:
                    self._send_json(400, {"ok": False, "error": f"invalid json: {e}"})
                    return
                if isinstance(payload, list):
                    users = load_users_json_bytes(raw)
                    filename = "users.json"
                    blob = None
                elif isinstance(payload, dict):
                    mark_missing = mark_missing or _truthy(payload.get("mark_missing"))
                    if payload.get("domain"):
                        domain_hint = payload.get("domain")
                    if isinstance(payload.get("users"), list):
                        users = load_users_json_bytes(json.dumps(payload["users"]).encode("utf-8"))
                        filename = "users.json"
                        blob = None
                    else:
                        self._send_json(
                            400,
                            {"ok": False, "error": "JSON 需为 users 数组，或 {users:[...], mark_missing?, domain?}"},
                        )
                        return
                else:
                    self._send_json(400, {"ok": False, "error": "unsupported json body"})
                    return
            else:
                blob = self._read_body_bytes()
                # try filename from Content-Disposition if present
                cd = self.headers.get("Content-Disposition") or ""
                m = re.search(r'filename="?([^";]+)"?', cd)
                if m:
                    filename = Path(m.group(1)).name

            if blob is not None:
                if not blob:
                    self._send_json(400, {"ok": False, "error": "empty upload body"})
                    return
                users = extract_users_from_upload(filename, blob)

            if not users:
                self._send_json(400, {"ok": False, "error": "导出中没有有效用户记录"})
                return

            # Smart domain routing: split users by email host when domain is auto/missing,
            # or when form explicitly requests auto (board default).
            auto_mode = False
            if domain_hint is None or str(domain_hint).strip() == "":
                auto_mode = True
            elif str(domain_hint).strip().lower() in {"auto", "smart", "*"}:
                auto_mode = True
            if "auto_domain" in qs and _truthy(qs["auto_domain"][0]):
                auto_mode = True

            groups, fallback = group_users_by_domain(
                users, filename=filename, domain_hint=None if auto_mode else domain_hint
            )
            if not auto_mode:
                # Force single-domain path when caller pins a concrete domain.
                only = normalize_domain(domain_hint)
                groups = {only: users}
                fallback = only

            domain_results = []
            total_added: list[str] = []
            total_updated: list[str] = []
            total_unchanged: list[str] = []
            total_missing: list[str] = []
            primary_domain = None
            primary_data = None
            primary_before = 0
            primary_after = 0

            # Prefer importing into domains with more users first for primary response payload.
            ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            for domain, chunk in ordered:
                if not chunk:
                    continue
                with domain_transaction(domain):
                    data = load_data(domain)
                    before = len(data.get("members", []))
                    summary = sync_members_from_users(
                        data,
                        chunk,
                        mark_missing=mark_missing,
                        source_note=f"synced from upload:{filename}",
                    )
                    save_data(data, domain)
                    after_data = load_data(domain)
                after = len(after_data.get("members", []))
                domain_results.append(
                    {
                        "domain": domain,
                        "export_user_count": len(chunk),
                        "member_count_before": before,
                        "member_count": after,
                        "added": summary["added"],
                        "updated": summary["updated"],
                        "unchanged": summary["unchanged"],
                        "missing": summary["missing"],
                        "data": after_data,
                    }
                )
                total_added.extend(f"{n}@{domain}" for n in summary["added"])
                total_updated.extend(f"{n}@{domain}" for n in summary["updated"])
                total_unchanged.extend(summary["unchanged"])
                total_missing.extend(f"{n}@{domain}" for n in summary["missing"])
                if primary_domain is None:
                    primary_domain = domain
                    primary_data = after_data
                    primary_before = before
                    primary_after = after

            if not domain_results:
                self._send_json(400, {"ok": False, "error": "导出中没有有效用户记录"})
                return

            self._send_json(
                200,
                {
                    "ok": True,
                    "auto_domain": auto_mode,
                    "fallback_domain": fallback,
                    "domain": primary_domain,
                    "domains_touched": [r["domain"] for r in domain_results],
                    "domain_results": [
                        {k: v for k, v in r.items() if k != "data"} for r in domain_results
                    ],
                    # Keep full data for each domain so board can refresh all caches.
                    "domains_data": {r["domain"]: r["data"] for r in domain_results},
                    "filename": filename,
                    "mark_missing": mark_missing,
                    "export_user_count": len(users),
                    "member_count_before": primary_before,
                    "member_count": primary_after,
                    "added": total_added,
                    "updated": total_updated,
                    "unchanged": total_unchanged,
                    "missing": total_missing,
                    "data": primary_data,
                },
            )
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_toggle_paid(self, payload: dict, qs: dict) -> None:
        who = payload.get("id") or payload.get("username") or payload.get("email") or ""
        try:
            month = parse_month(payload.get("month"))
            if "paid" in payload and not isinstance(payload["paid"], bool):
                raise ValueError("paid must be a JSON boolean")
            amount = parse_amount(payload.get("amount", None))
            note = str(payload.get("note") or "")
            domain = extract_domain(payload, qs)
            with domain_transaction(domain):
                data = load_data(domain)
                member = find_member(data, who)
                if not member:
                    self._send_json(404, {"ok": False, "error": f"member not found: {who}"})
                    return
                target_paid = payload["paid"] if "paid" in payload else not is_paid_month(member, month)
                payment = set_paid(member, month, target_paid, amount=amount, note=note)
                save_data(data, domain)
            self._send_json(
                200,
                {
                    "ok": True,
                    "domain": domain,
                    "id": member.get("id"),
                    "username": member.get("username"),
                    "email": member.get("email"),
                    "month": month,
                    "paid": bool(target_paid),
                    "payment": payment,
                    "member": member,
                },
            )
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_update_member(self, payload: dict, qs: dict) -> None:
        who = payload.get("id") or payload.get("username") or payload.get("email") or ""
        try:
            domain = extract_domain(payload, qs)
            with domain_transaction(domain):
                data = load_data(domain)
                member = find_member(data, who)
                if not member:
                    self._send_json(404, {"ok": False, "error": f"member not found: {who}"})
                    return
                changed = update_member_fields(member, payload)
                save_data(data, domain)
            self._send_json(
                200,
                {
                    "ok": True,
                    "domain": domain,
                    "id": member.get("id"),
                    "username": member.get("username"),
                    "email": member.get("email"),
                    "changed": changed,
                    "member": member,
                },
            )
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_add_member(self, payload: dict, qs: dict) -> None:
        try:
            domain = extract_domain(payload, qs)
            with domain_transaction(domain):
                data = load_data(domain)
                member = create_member(data, payload)
                save_data(data, domain)
            self._send_json(
                200,
                {
                    "ok": True,
                    "domain": domain,
                    "id": member.get("id"),
                    "username": member.get("username"),
                    "email": member.get("email"),
                    "member": member,
                },
            )
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})

    def _handle_delete_member(self, payload: dict, qs: dict) -> None:
        who = payload.get("id") or payload.get("username") or payload.get("email") or ""
        try:
            domain = extract_domain(payload, qs)
            with domain_transaction(domain):
                data = load_data(domain)
                before = len(data.get("members", []))
                removed = delete_member(data, who)
                save_data(data, domain)
            self._send_json(
                200,
                {
                    "ok": True,
                    "domain": domain,
                    "id": removed.get("id"),
                    "username": removed.get("username"),
                    "email": removed.get("email"),
                    "member_count_before": before,
                    "member_count": len(data.get("members", [])),
                    "removed": removed,
                },
            )
        except KeyError as e:
            self._send_json(404, {"ok": False, "error": str(e)})
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
        except Exception as e:
            self._send_json(500, {"ok": False, "error": str(e)})


def main() -> None:
    # Ensure all persistent catalog shells exist on boot.
    for d in get_domain_catalog():
        ensure_domain_file(d["id"])
    start_finance_scheduler()
    httpd = OptimizedThreadingHTTPServer((HOST, PORT), Handler)
    print(f"renewal registry serving on http://{HOST}:{PORT} domains={','.join(get_domain_ids())}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
