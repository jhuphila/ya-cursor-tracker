"""Unit tests for composerData workspaceIdentifier attribution."""
from __future__ import annotations

import json
from pathlib import Path

from tracker.cursor_sources import (
    WorkspaceConvMap,
    _root_from_workspace_identifier,
    enrich_roots_from_composer_data_for_conv_ids,
)


def test_root_from_workspace_identifier_uri(tmp_path: Path) -> None:
    root, hint = _root_from_workspace_identifier(
        {
            "id": "abc123",
            "uri": {"fsPath": str(tmp_path / "proj"), "scheme": "file"},
        },
        tmp_path / "missing-storage",
    )
    assert hint == "abc123"
    assert root is not None
    assert "proj" in root.replace("\\", "/")


def test_root_from_workspace_storage_json(tmp_path: Path) -> None:
    ws_hash = "deadbeef"
    ws_dir = tmp_path / ws_hash
    ws_dir.mkdir()
    folder = tmp_path / "my-repo"
    folder.mkdir()
    (ws_dir / "workspace.json").write_text(
        json.dumps({"folder": folder.as_uri()}),
        encoding="utf-8",
    )
    root, hint = _root_from_workspace_identifier({"id": ws_hash}, tmp_path)
    assert hint == ws_hash
    assert root is not None
    assert Path(root).resolve() == folder.resolve()


def test_enrich_composer_data_reads_name_and_workspace(tmp_path: Path) -> None:
    import sqlite3

    global_db = tmp_path / "state.vscdb"
    con = sqlite3.connect(global_db)
    con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    cid = "11111111-2222-3333-4444-555555555555"
    folder = tmp_path / "hiv-agent"
    folder.mkdir()
    blob = {
        "composerId": cid,
        "name": "HIV evaluation task report",
        "workspaceIdentifier": {
            "id": "ws1",
            "uri": {"fsPath": str(folder), "scheme": "file"},
        },
        "context": {"fileSelections": [], "folderSelections": [], "terminalSelections": []},
    }
    con.execute(
        "INSERT INTO cursorDiskKV (key, value) VALUES (?, ?)",
        (f"composerData:{cid}", json.dumps(blob)),
    )
    con.commit()
    con.close()

    ws = WorkspaceConvMap({}, {}, {}, {})
    enrich_roots_from_composer_data_for_conv_ids(global_db, tmp_path / "ws", ws, {cid})
    assert ws.conv_to_title[cid] == "HIV evaluation task report"
    assert cid in ws.conv_to_root
    assert "hiv-agent" in ws.conv_to_root[cid].replace("\\", "/")
    assert ws.conv_to_attribution_source[cid] == "global-composer-data"
    assert ws.conv_to_workspace_id[cid] == "ws1"
