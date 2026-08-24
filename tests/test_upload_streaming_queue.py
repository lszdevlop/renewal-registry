#!/usr/bin/env python3
"""Upload bodies land on disk in chunks and parsing has bounded admission."""
from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]


def load_sandbox_server():
    td = Path(tempfile.mkdtemp(prefix="upload-queue-test-"))
    for name in ("server.py", "renewal_cli.py", "domain_catalog.py", "finance_metrics.py", "index.html"):
        shutil.copy2(SRC / name, td / name)
    spec = importlib.util.spec_from_file_location("sandbox_server", td / "server.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    import sys
    sys.path.insert(0, str(td))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(td))
    return td, module


def main() -> None:
    source = (SRC / "server.py").read_text(encoding="utf-8")
    assert "item.file.read()" not in source, "multipart upload must not be read wholly into memory"
    parser_source = source.split("def extract_users_from_upload_path", 1)[1].split("def infer_domain_from_email", 1)[0]
    assert ".read_bytes()" not in parser_source, "landed uploads must parse from file streams, not duplicate whole bytes"
    assert "extract_users_from_upload_path" in source, "parser must consume a landed file path"
    assert "UPLOAD_COPY_CHUNK_BYTES" in source, "streaming copy must use bounded chunks"

    td, server = load_sandbox_server()
    try:
        queue = server.UploadParseQueue(max_workers=1, max_queued=1)
        started = threading.Event()
        release = threading.Event()

        def blocked(value):
            started.set()
            release.wait(5)
            return value

        first = queue.submit(blocked, "first")
        assert started.wait(2), "worker did not start"
        second = queue.submit(lambda: "second")
        try:
            queue.submit(lambda: "overflow")
        except server.UploadQueueFullError as exc:
            assert "队列已满" in str(exc)
        else:
            raise AssertionError("full parsing queue must reject admission")
        release.set()
        assert first.result(timeout=2) == "first"
        assert second.result(timeout=2) == "second"

        failed = queue.submit(lambda: (_ for _ in ()).throw(ValueError("parser failed")))
        try:
            failed.result(timeout=2)
        except ValueError:
            pass
        else:
            raise AssertionError("parser exception must propagate")
        assert queue.submit(lambda: "slot-reused").result(timeout=2) == "slot-reused"
        queue.shutdown()

        server.UPLOAD_EXTRACTED_USERS_MAX_BYTES = 1024
        archive = td / "oversized-users.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("users.json", json.dumps([{"uuid": "u", "full_name": "x" * 4096}]))
        before = set(Path(tempfile.gettempdir()).glob("renewal-users-*.json"))
        try:
            server.extract_users_from_upload_path(archive.name, archive)
        except ValueError as exc:
            assert "解压后" in str(exc) or "过大" in str(exc)
        else:
            raise AssertionError("oversized extracted users.json must be rejected")
        after = set(Path(tempfile.gettempdir()).glob("renewal-users-*.json"))
        assert after == before, after - before
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("PASS upload streaming and bounded parse queue")


if __name__ == "__main__":
    main()