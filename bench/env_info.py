"""Environment metadata capture -- gathered once per benchmark session and
attached to every result record so a run is reproducible without having to
re-derive it per case.
"""
from __future__ import annotations

import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional


def collect_env_metadata(repo_root: Path) -> dict:
    return {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": _git(repo_root, ["rev-parse", "HEAD"]),
        "git_dirty": _git_dirty(repo_root),
    }


def _git(repo_root: Path, args: list[str]) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _git_dirty(repo_root: Path) -> Optional[bool]:
    out = _git(repo_root, ["status", "--porcelain"])
    return None if out is None else bool(out)
