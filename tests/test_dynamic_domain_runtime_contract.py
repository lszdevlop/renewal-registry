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
        "renewal manager billing day": (renewal_server, 'action == "set_billing_day"'),
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
        "domain billing day selector": (renewal_html, 'data-action="domain-billing-day"'),
        "domain billing day empty option": (renewal_html, '<option value="">账单日 未设置</option>'),
        "domain billing day API call": (renewal_html, 'action: "set_billing_day"'),
        "domain billing day rollback": (renewal_html, "select.value = previousValue"),
        "domain billing day refresh": (renewal_html, "loadDomains({ force: true })"),
        "domain select click isolation": (renewal_html, 'event.target.closest(\'[data-action="domain-billing-day"]\')'),
        "hub fallback no fixed extra domains": (hub_html, "loadDomains"),
    }
    missing = [name for name, (text, token) in required.items() if token not in text]
    if missing:
        raise SystemExit("FAIL missing dynamic domain runtime contracts: " + ", ".join(missing))
    forbidden = {
        "renewal server static DOMAIN_CATALOG": (renewal_server, "DOMAIN_CATALOG"),
        "renewal server static DOMAIN_IDS": (renewal_server, "DOMAIN_IDS"),
        "renewal CLI static DOMAIN_CATALOG": (renewal_cli, "DOMAIN_CATALOG"),
        "renewal CLI static DOMAIN_IDS": (renewal_cli, "DOMAIN_IDS"),
        "finance static catalog import": ((RENEWAL / "finance_metrics.py").read_text(encoding="utf-8"), "DOMAIN_CATALOG"),
        "activation script static IDs": ((RENEWAL / "scripts/init_activation_dates_202607.py").read_text(encoding="utf-8"), "DOMAIN_IDS"),
    }
    present = [name for name, (text, token) in forbidden.items() if token in text]
    if present:
        raise SystemExit("FAIL static domain business traversal remains: " + ", ".join(present))
    if '</button><select data-action="domain-billing-day"' not in renewal_html:
        raise SystemExit("FAIL billing day select must be rendered as a sibling after the domain switch button")
    print("PASS dynamic domain runtime contracts", len(required))


if __name__ == "__main__":
    main()
