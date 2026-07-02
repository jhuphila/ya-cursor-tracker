"""Resolve Cursor application data paths on Windows, Linux, and macOS."""

from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import NamedTuple


class CursorPaths(NamedTuple):
    """Default locations for Cursor SQLite databases."""

    state_vscdb: Path
    workspace_storage: Path
    ai_tracking_db: Path


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def default_cursor_paths() -> CursorPaths:
    """
    Best-effort defaults. Override via tracker_config.yaml when Cursor
    uses non-standard install locations.
    """
    system = platform.system()
    home = _home()

    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            appdata = str(home / "AppData" / "Roaming")
        base = Path(appdata) / "Cursor" / "User"
    elif system == "Darwin":
        base = home / "Library" / "Application Support" / "Cursor" / "User"
    else:
        # Linux and other Unix: XDG_CONFIG_HOME or ~/.config
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            base = Path(xdg) / "Cursor" / "User"
        else:
            base = home / ".config" / "Cursor" / "User"

    state = base / "globalStorage" / "state.vscdb"
    ws = base / "workspaceStorage"
    # ai-tracking lives under ~/.cursor on all platforms Cursor supports
    ai = home / ".cursor" / "ai-tracking" / "ai-code-tracking.db"

    return CursorPaths(
        state_vscdb=state,
        workspace_storage=ws,
        ai_tracking_db=ai,
    )
