#!/usr/bin/env python3
"""Contract guards for dynamic domain catalog integration across both products."""
from pathlib import Path

RENEWAL = Path(__file__).resolve().parents[1]
HUB = RENEWAL.parent / "claude-export-hub"


def main() -> None:
    renewal_server = (RENEWAL / "server.py").read_text(encoding="utf-8")
    renewal_cli = (RENEWAL / "renewal_cli.py").read_text(encoding="utf-8")
    renewal_html = (RENEWAL / "index.html").read_text(encoding="utf-8")
    hub_store = (HUB / "export_store.py").read_text(encoding="utf-8")
    hub_server = (HUB / "server.py").read_text(encoding="utf-8")
    hub_html = (HUB / "index.html").read_text(encoding="utf-8")

    required = {
        "renewal runtime catalog": (renewal_server, "get_domain_catalog()"),
        "renewal domain API": (renewal_server, '"/api/domain-management"'),
        "renewal manager add": (renewal_server, 'action == "add"'),
        "renewal manager rename": (renewal_server, 'action == "rename"'),
        "renewal manager delete": (renewal_server, 'action == "delete"'),
        "public email package fallback": (renewal_server, 'email.startswith("admin@")'),
        "admin import auto domain creation": (renewal_server, "ensure_admin_domain_for_auto_import(users)"),
        "auto-created domain response": (renewal_server, '"auto_created_domain": auto_created_domain'),
        "CLI runtime domains": (renewal_cli, "get_domain_catalog()"),
        "hub runtime catalog": (hub_store, "get_domain_catalog()"),
        "hub health runtime catalog": (hub_server, "get_domain_catalog()"),
        "renewal domain manager UI": (renewal_html, 'id="domain-manager"'),
        "renewal add API call": (renewal_html, 'action: "add"'),
        "renewal rename API call": (renewal_html, 'action: "rename"'),
        "renewal delete API call": (renewal_html, 'action: "delete"'),
        "hub fallback no fixed extra domains": (hub_html, "loadDomains"),
    }
    missing = [name for name, (text, token) in required.items() if token not in text]
    if missing:
        raise SystemExit("FAIL missing dynamic domain runtime contracts: " + ", ".join(missing))
    print("PASS dynamic domain runtime contracts", len(required))


if __name__ == "__main__":
    main()
