#!/usr/bin/env python3
"""Static regression guards for renewal server performance features."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "server.py").read_text(encoding="utf-8")


def main() -> None:
    required = {
        "HTTP/1.1 keep-alive": 'protocol_version = "HTTP/1.1"',
        "larger accept queue": "request_queue_size = 128",
        "daemon request threads": "daemon_threads = True",
        "domain parsed cache": "_DOMAIN_DATA_CACHE",
        "serialized response cache": "_DOMAIN_RESPONSE_CACHE",
        "cache invalidation on save": "invalidate_domain_cache(domain)",
        "single finance refresh worker": "finance-refresh-worker",
        "coalescing event": "_FINANCE_REFRESH_EVENT",
        "serialized finance recompute": "_FINANCE_RECOMPUTE_LOCK",
        "gzip negotiation": '"gzip" in (self.headers.get("Accept-Encoding") or "").lower()',
        "ETag support": 'If-None-Match',
        "quiet access logs": "RENEWAL_ACCESS_LOG",
    }
    missing = [name for name, token in required.items() if token not in SRC]
    if missing:
        raise SystemExit("FAIL missing performance features: " + ", ".join(missing))
    assert 'threading.Thread(target=_run, name="finance-refresh"' not in SRC, "per-write refresh threads must be removed"
    print("PASS performance feature guards:", len(required))


if __name__ == "__main__":
    main()
