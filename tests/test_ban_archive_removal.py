#!/usr/bin/env python3
"""Banned-member and archived-domain features are fully removed."""
from __future__ import annotations

import json
import shutil
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from test_upload_streaming_queue import load_sandbox_server


def main() -> None:
    root, server = load_sandbox_server()
    try:
        catalog = {
            "version": 1,
            "default": "team.test",
            "domains": [
                {"id": "team.test", "label": "team.test", "billing_day": 8},
                {"id": "old.test", "label": "old.test", "billing_day": 8, "archived": True,
                 "archived_at": "2026-10-01T00:00:00Z", "archive_reason": "封号"},
            ],
        }
        (root / "data/domain_catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
        (root / "data/nonrenewal_loss.json").write_text(json.dumps({
            "enabled_at": "2026-10-01T00:00:00+00:00", "events": [],
        }), encoding="utf-8")
        domain = root / "data/domains/team.test"
        domain.mkdir(parents=True)
        member = {
            "id": "banned-1", "username": "Banned", "email": "banned@example.com",
            "status": "banned", "price": 100, "billing_day": 8, "payments": [],
            "banned_at": "2026-10-01T00:00:00Z",
            "ban_snapshot": {"reason": "封号", "totals": {"normal_profit": 1}},
        }
        roster = {"meta": {"domain": "team.test"}, "members": [member]}
        (domain / "members.json").write_text(json.dumps(roster), encoding="utf-8")

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            ban = urllib.request.Request(
                base + "/api/ban-member",
                data=json.dumps({"domain": "team.test", "id": "x", "confirm_id": "x"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            try:
                urllib.request.urlopen(ban, timeout=5)
            except urllib.error.HTTPError as exc:
                assert exc.code == 404, exc.code
            else:
                raise AssertionError("ban endpoint still exists")

            archive = urllib.request.Request(
                base + "/api/domain-management",
                data=json.dumps({"action": "archive", "domain": "team.test", "confirm_domain": "team.test"}).encode(),
                headers={"Content-Type": "application/json", "Host": "127.0.0.1", "Origin": "http://127.0.0.1"},
            )
            try:
                urllib.request.urlopen(archive, timeout=5)
            except urllib.error.HTTPError as exc:
                assert exc.code == 400, exc.code
            else:
                raise AssertionError("archive action still exists")

            delete = urllib.request.Request(
                base + "/api/delete-member",
                data=json.dumps({"domain": "team.test", "id": "banned-1"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(delete, timeout=5) as response:
                    body = json.load(response)
            except urllib.error.HTTPError as exc:
                raise AssertionError(exc.read().decode()) from exc
            assert body["ok"] is True, body
            saved = json.loads((domain / "members.json").read_text(encoding="utf-8"))
            assert saved["members"] == [], saved
        finally:
            httpd.shutdown()
            httpd.server_close()

        page = (root / "index.html").read_text(encoding="utf-8")
        assert 'id="member-status-filter"' not in page
        assert 'data-action="ban-member"' not in page
        assert 'id="archive-banner"' not in page
        source = (root / "server.py").read_text(encoding="utf-8")
        assert "def _handle_ban_member" not in source
        assert "def archive(" not in (root / "domain_catalog.py").read_text(encoding="utf-8")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("PASS ban and archive removal")


if __name__ == "__main__":
    main()
