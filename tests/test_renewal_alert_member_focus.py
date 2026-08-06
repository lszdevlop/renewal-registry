#!/usr/bin/env python3
"""Contract: clicking a renewal reminder precisely focuses its member row."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "index.html").read_text(encoding="utf-8")


def main() -> None:
    required = {
        "dedicated exact focus state": "let focusedMemberKey = null",
        "stable identity helper": "function memberFocusKey(",
        "alert carries exact member key": 'data-member-key="${esc(x.memberKey)}"',
        "alert carries domain": 'data-domain="${esc(x.domain)}"',
        "clickable semantics": 'role="button" tabindex="0"',
        "focus action": "async function focusRenewalMember(",
        "switches domain": "await switchDomain(domain)",
        "clears text search": '$("q").value = ""',
        "clears paid filter": '$("filter").value = "all"',
        "exact row filter": "memberFocusKey(m) !== focusedMemberKey",
        "click delegation": '$("renewal-alert-track").addEventListener("click"',
        "keyboard delegation": '$("renewal-alert-track").addEventListener("keydown"',
        "scrolls to table": '$("member-table-shell").scrollIntoView',
    }
    missing = [name for name, token in required.items() if token not in HTML]
    assert not missing, "missing reminder-to-member focus contracts: " + ", ".join(missing)
    print("PASS renewal reminder click precisely focuses one member row")


if __name__ == "__main__":
    main()
