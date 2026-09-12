#!/usr/bin/env python3
"""ExecStartPre cleanliness check rejects tracked dirt, ignores runtime data."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]


def run(repo: Path):
    return subprocess.run(
        ["python3", str(repo / "scripts/check_deploy_clean.py")],
        cwd=repo, text=True, capture_output=True,
    )


def main() -> None:
    td = Path(tempfile.mkdtemp(prefix="deploy-clean-test-"))
    try:
        (td / "scripts").mkdir()
        shutil.copy2(SRC / "scripts/check_deploy_clean.py", td / "scripts/check_deploy_clean.py")
        (td / ".gitignore").write_text("data/\n", encoding="utf-8")
        (td / "tracked.txt").write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=td, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=td, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=td, check=True)
        subprocess.run(["git", "add", "."], cwd=td, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=td, check=True)

        clean = run(td)
        assert clean.returncode == 0, clean.stderr + clean.stdout
        (td / "data/runtime.json").parent.mkdir()
        (td / "data/runtime.json").write_text("runtime\n", encoding="utf-8")
        ignored = run(td)
        assert ignored.returncode == 0, ignored.stderr + ignored.stdout
        (td / "untracked.py").write_text("print('new')\n", encoding="utf-8")
        untracked = run(td)
        assert untracked.returncode != 0
        assert "untracked.py" in untracked.stderr + untracked.stdout
        (td / "untracked.py").unlink()
        (td / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        dirty = run(td)
        assert dirty.returncode != 0
        assert "tracked.txt" in dirty.stderr + dirty.stdout
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("PASS deploy cleanliness check")


def test_gitignore_blocks_common_claude_export_pii_paths() -> None:
    sensitive = (
        "users.json", "sub/users.json",
        "conversations.json", "nested/conversations.json",
        "memories.json", "nested/memories.json",
        "projects/private.json", "nested/projects/private.json",
        "design_chats/private.json", "nested/design_chats/private.json",
        "export.json", "nested/claude-export.json",
    )
    # Exercise the actual ignore rules without requiring or altering a source .git.
    with tempfile.TemporaryDirectory(prefix="deploy-ignore-test-") as temp:
        repo = Path(temp)
        shutil.copy2(SRC / ".gitignore", repo / ".gitignore")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        for rel in sensitive:
            result = subprocess.run(
                ["git", "check-ignore", "-q", "--no-index", rel], cwd=repo
            )
            assert result.returncode == 0, rel
        assert subprocess.run(
            ["git", "check-ignore", "-q", "--no-index", "tests/projects/fixture.py"], cwd=repo
        ).returncode == 1
    print("PASS Claude export PII paths are ignored")


if __name__ == "__main__":
    main()
    test_gitignore_blocks_common_claude_export_pii_paths()