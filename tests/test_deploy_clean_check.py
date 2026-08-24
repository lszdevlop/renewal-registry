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


if __name__ == "__main__":
    main()