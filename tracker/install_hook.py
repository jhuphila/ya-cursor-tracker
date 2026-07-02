"""
Install a fail-open git post-commit hook that runs the CSV exporter.

Uses sh + git rev-parse so it works with Git for Windows (bash) and Unix.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path


def hook_body(python_exe: str, config_rel: str) -> str:
    return f"""#!/usr/bin/env sh
# Installed by tracker.install_hook — fail-open (never blocks commits)
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
CONFIG="$ROOT/{config_rel}"
if [ ! -f "$CONFIG" ]; then
  echo "[cursor-tracker] skip: config not found: $CONFIG" >&2
  exit 0
fi
exec "{python_exe}" -m tracker.export_interactions --config "$CONFIG" || exit 0
"""


def install(repo_root: Path, *, config_rel: str = "config/tracker_config.yaml") -> Path:
    git_dir = repo_root / ".git"
    if not git_dir.is_dir():
        raise SystemError(f"Not a git repository (missing .git): {repo_root}")

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    target = hooks_dir / "post-commit"
    body = hook_body(sys.executable, config_rel.replace("\\", "/"))
    target.write_text(body, encoding="utf-8", newline="\n")

    if os.name != "nt":
        mode = target.stat().st_mode
        target.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return target


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Install git post-commit hook for Cursor CSV export")
    p.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="Git repository root (default: cwd)",
    )
    p.add_argument(
        "--config",
        default="config/tracker_config.yaml",
        help="Path to tracker_config.yaml relative to repo root",
    )
    args = p.parse_args(argv)
    try:
        path = install(args.repo.resolve(), config_rel=str(args.config))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Installed post-commit hook -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
