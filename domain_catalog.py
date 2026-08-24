#!/usr/bin/env python3
"""Persistent domain catalog and dual-platform domain lifecycle operations."""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DomainCatalogError(ValueError):
    pass


_DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_domain_id(raw: str | None) -> str:
    domain = str(raw or "").strip().lower()
    if not domain or not _DOMAIN_RE.fullmatch(domain):
        raise DomainCatalogError(f"域名格式无效: {raw!r}（示例: team.example.com）")
    return domain


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


class DomainCatalogManager:
    def __init__(
        self,
        *,
        catalog_path: Path,
        renewal_root: Path,
        hub_root: Path,
        backup_root: Path,
        initial_domains: list[str],
        default_domain: str,
    ) -> None:
        self.catalog_path = Path(catalog_path)
        self.renewal_root = Path(renewal_root)
        self.hub_root = Path(hub_root)
        self.backup_root = Path(backup_root)
        self.lock_path = self.catalog_path.with_suffix(self.catalog_path.suffix + ".lock")
        self.default_domain = normalize_domain_id(default_domain)
        self.initial_domains = [normalize_domain_id(x) for x in initial_domains]
        if self.default_domain not in self.initial_domains:
            self.initial_domains.insert(0, self.default_domain)
        self._ensure_catalog()

    @contextmanager
    def _lock(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _domain_data_locks(self, domain: str):
        """Wait for in-flight writes/imports on both products before destructive ops."""
        renewal_lock = self._renewal_domain_dir(domain) / ".registry.lock"
        hub_lock = self._hub_domain_dir(domain) / ".store.lock"
        renewal_lock.parent.mkdir(parents=True, exist_ok=True)
        hub_lock.parent.mkdir(parents=True, exist_ok=True)
        with renewal_lock.open("a+") as rf, hub_lock.open("a+") as hf:
            fcntl.flock(rf.fileno(), fcntl.LOCK_EX)
            try:
                fcntl.flock(hf.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(hf.fileno(), fcntl.LOCK_UN)
            finally:
                fcntl.flock(rf.fileno(), fcntl.LOCK_UN)

    def _ensure_catalog(self) -> None:
        if self.catalog_path.exists():
            return
        with self._lock():
            if not self.catalog_path.exists():
                self._write_state(self.initial_domains)

    @staticmethod
    def _normalize_billing_day(raw: Any) -> int | None:
        if raw is None or raw == "":
            return None
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 31:
            raise DomainCatalogError("账单日必须为空或 1-31 的整数")
        return raw

    def _write_state(self, domains: list[str | dict[str, Any]]) -> dict[str, Any]:
        # Preserve forward-compatible top-level metadata already present on disk.
        state: dict[str, Any] = {}
        if self.catalog_path.exists():
            try:
                current = json.loads(self.catalog_path.read_text(encoding="utf-8"))
                if isinstance(current, dict):
                    state = dict(current)
            except (OSError, json.JSONDecodeError) as exc:
                raise DomainCatalogError(f"域名目录读取失败: {exc}") from exc

        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in domains:
            source = raw if isinstance(raw, dict) else {"id": raw}
            domain = normalize_domain_id(source.get("id"))
            if domain in seen:
                continue
            seen.add(domain)
            row = dict(source)
            label = row.get("label")
            row.update({
                "id": domain,
                "label": str(label).strip() if label is not None and str(label).strip() else domain,
                "billing_day": self._normalize_billing_day(row.get("billing_day")),
            })
            unique.append(row)
        if self.default_domain not in seen:
            unique.insert(0, {"id": self.default_domain, "label": self.default_domain, "billing_day": None})
        state.update({
            "version": 1,
            "default": self.default_domain,
            "updated_at": now_iso(),
            "domains": unique,
        })
        _atomic_write_json(self.catalog_path, state)
        return state

    def read(self) -> dict[str, Any]:
        self._ensure_catalog()
        try:
            state = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DomainCatalogError(f"域名目录读取失败: {exc}") from exc
        domains = state.get("domains") if isinstance(state, dict) else None
        if not isinstance(domains, list):
            raise DomainCatalogError("域名目录格式错误")
        rows = []
        for item in domains:
            if not isinstance(item, dict):
                continue
            domain = normalize_domain_id(item.get("id"))
            billing_day = self._normalize_billing_day(item.get("billing_day"))
            label = item.get("label")
            row = dict(item)
            row.update({
                "id": domain,
                "label": str(label).strip() if label is not None and str(label).strip() else domain,
                "billing_day": billing_day,
            })
            rows.append(row)
        normalized_state = dict(state)
        normalized_state.update({
            "version": 1,
            "default": self.default_domain,
            "updated_at": state.get("updated_at") or now_iso(),
            "domains": rows,
        })
        return normalized_state

    def domain_ids(self) -> list[str]:
        return [x["id"] for x in self.read()["domains"]]

    def normalize_known(self, raw: str | None) -> str:
        if raw is None or str(raw).strip() == "":
            return self.default_domain
        domain = normalize_domain_id(raw)
        ids = self.domain_ids()
        if domain not in ids:
            raise DomainCatalogError(f"未知域: {domain}（可选: {', '.join(ids)}）")
        return domain

    def _renewal_domain_dir(self, domain: str) -> Path:
        return self.renewal_root / "data/domains" / domain

    def _hub_domain_dir(self, domain: str) -> Path:
        return self.hub_root / "data/domains" / domain

    def _create_renewal_shell(self, domain: str) -> None:
        root = self._renewal_domain_dir(domain)
        root.mkdir(parents=True, exist_ok=True)
        payload = {
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
        _atomic_write_json(root / "members.json", payload)

    def _create_hub_shell(self, domain: str) -> None:
        root = self._hub_domain_dir(domain)
        (root / "uploads").mkdir(parents=True, exist_ok=True)
        payload = {
            "meta": {
                "domain": domain,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "upload_count": 0,
                "uploads": [],
                "source_note": f"域 {domain}：上传 Claude 导出 zip 后增量合并",
            },
            "users": {},
            "conversations": {},
            "memories": {},
            "projects": {},
            "design_chats": {},
            "profiles": {},
        }
        _atomic_write_json(root / "store.json", payload)

    def _create_shells(self, domain: str) -> None:
        self._create_renewal_shell(domain)
        self._create_hub_shell(domain)

    def _backup_domain(self, domain: str, action: str) -> Path:
        stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
        dest = self.backup_root / f"domain-{action}-{domain}-{stamp}"
        renewal_src = self._renewal_domain_dir(domain)
        hub_src = self._hub_domain_dir(domain)
        if renewal_src.exists():
            shutil.copytree(renewal_src, dest / "renewal-registry" / domain)
        if hub_src.exists():
            shutil.copytree(hub_src, dest / "claude-export-hub" / domain)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "MANIFEST.json").write_text(
            json.dumps({"domain": domain, "action": action, "created_at": now_iso()}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return dest

    def add(self, raw_domain: str) -> dict[str, Any]:
        domain = normalize_domain_id(raw_domain)
        with self._lock():
            state = self.read()
            rows = state["domains"]
            ids = [d["id"] for d in rows]
            if domain in ids:
                raise DomainCatalogError(f"域名已存在: {domain}")
            try:
                self._create_shells(domain)
                self._write_state(rows + [{"id": domain, "label": domain, "billing_day": None}])
            except Exception:
                shutil.rmtree(self._renewal_domain_dir(domain), ignore_errors=True)
                shutil.rmtree(self._hub_domain_dir(domain), ignore_errors=True)
                raise
        return {"action": "add", "domain": domain, "catalog": self.read()}

    def rename(self, raw_old: str, raw_new: str, *, confirm_clear: bool) -> dict[str, Any]:
        old = normalize_domain_id(raw_old)
        new = normalize_domain_id(raw_new)
        if not confirm_clear:
            raise DomainCatalogError("修改域名会清除旧域全部数据，请明确确认")
        if old == self.default_domain:
            raise DomainCatalogError("默认域不可修改")
        with self._lock():
            state = self.read()
            rows = state["domains"]
            ids = [d["id"] for d in rows]
            if old not in ids:
                raise DomainCatalogError(f"未知域: {old}")
            if new in ids:
                raise DomainCatalogError(f"域名已存在: {new}")
            with self._domain_data_locks(old):
                backup = self._backup_domain(old, "rename")
                try:
                    self._create_shells(new)
                    # Publish the new catalog first. New requests reject old immediately;
                    # existing old-domain writers remain blocked by the locks above.
                    self._write_state([
                        {"id": new, "label": new, "billing_day": None} if row["id"] == old else row
                        for row in rows
                    ])
                    shutil.rmtree(self._renewal_domain_dir(old), ignore_errors=True)
                    shutil.rmtree(self._hub_domain_dir(old), ignore_errors=True)
                except Exception:
                    self._write_state(rows)
                    shutil.rmtree(self._renewal_domain_dir(new), ignore_errors=True)
                    shutil.rmtree(self._hub_domain_dir(new), ignore_errors=True)
                    self._restore_backup(backup, old)
                    raise
        return {"action": "rename", "old_domain": old, "domain": new, "backup_path": str(backup), "catalog": self.read()}

    def delete(self, raw_domain: str, *, confirm: bool) -> dict[str, Any]:
        domain = normalize_domain_id(raw_domain)
        if not confirm:
            raise DomainCatalogError("删除域名会永久清除全部数据，请明确确认")
        if domain == self.default_domain:
            raise DomainCatalogError("默认域不可删除")
        with self._lock():
            state = self.read()
            rows = state["domains"]
            ids = [d["id"] for d in rows]
            if domain not in ids:
                raise DomainCatalogError(f"未知域: {domain}")
            with self._domain_data_locks(domain):
                backup = self._backup_domain(domain, "delete")
                try:
                    self._write_state([row for row in rows if row["id"] != domain])
                    shutil.rmtree(self._renewal_domain_dir(domain), ignore_errors=True)
                    shutil.rmtree(self._hub_domain_dir(domain), ignore_errors=True)
                except Exception:
                    self._write_state(rows)
                    self._restore_backup(backup, domain)
                    raise
        return {"action": "delete", "domain": domain, "backup_path": str(backup), "catalog": self.read()}

    def set_billing_day(self, raw_domain: str, billing_day: Any) -> dict[str, Any]:
        domain = normalize_domain_id(raw_domain)
        billing_day = self._normalize_billing_day(billing_day)
        with self._lock():
            state = self.read()
            rows = state["domains"]
            if domain not in {row["id"] for row in rows}:
                raise DomainCatalogError(f"未知域: {domain}")
            updated: dict[str, Any] | None = None
            next_rows = []
            for row in rows:
                next_row = dict(row)
                if row["id"] == domain:
                    next_row["billing_day"] = billing_day
                    updated = next_row
                next_rows.append(next_row)
            self._write_state(next_rows)
        assert updated is not None
        return {"action": "set_billing_day", **updated, "catalog": self.read()}

    def _restore_backup(self, backup: Path, domain: str) -> None:
        rsrc = backup / "renewal-registry" / domain
        hsrc = backup / "claude-export-hub" / domain
        if rsrc.exists():
            shutil.copytree(rsrc, self._renewal_domain_dir(domain), dirs_exist_ok=True)
        if hsrc.exists():
            shutil.copytree(hsrc, self._hub_domain_dir(domain), dirs_exist_ok=True)


def make_live_manager(renewal_root: Path | None = None) -> DomainCatalogManager:
    renewal = Path(renewal_root or Path(__file__).resolve().parent)
    workspace = renewal.parent
    return DomainCatalogManager(
        catalog_path=renewal / "data/domain_catalog.json",
        renewal_root=renewal,
        hub_root=workspace / "claude-export-hub",
        backup_root=workspace / "backups/domain-management",
        initial_domains=["lsznode.de", "328001.xyz", "peaceai.de"],
        default_domain="lsznode.de",
    )
