"""
Read Cursor local SQLite databases and derive per-turn interaction records.

Logic is adapted from jeremypot/cursortrack (MIT): bubbleId keys, composer
workspace mapping, ai_code_hashes attribution layers.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from tracker.paths import CursorPaths


def repo_key(path: str) -> str:
    return path.lower().replace("\\", "/").rstrip("/")


def _normalise_path(raw: str) -> Path | None:
    try:
        s = raw.replace("\\", "/")
        if s.startswith("/") and len(s) > 2 and s[2] == ":":
            s = s[1:]
        if len(s) >= 2 and s[1] == ":":
            s = s[0].upper() + s[1:]
        return Path(s)
    except Exception:
        return None


def find_git_root(path_str: str) -> str | None:
    try:
        p = _normalise_path(path_str)
        if not p:
            return None
        cur = p if p.is_dir() else p.parent
        visited: set[str] = set()
        while cur != cur.parent:
            key = str(cur).lower()
            if key in visited:
                break
            visited.add(key)
            if (cur / ".git").exists():
                return str(cur)
            cur = cur.parent
    except Exception:
        pass
    return None


def _find_child_git_root(folder: Path) -> Path | None:
    try:
        if not folder.is_dir():
            return None
        candidates = [d for d in folder.iterdir() if d.is_dir() and (d / ".git").exists()]
        if len(candidates) == 1:
            return candidates[0]
    except Exception:
        pass
    return None


def decode_vscode_uri(uri: str) -> Path | None:
    try:
        s = urllib.parse.unquote(uri)
        for prefix in ("file:///", "file://"):
            if s.startswith(prefix):
                s = s[len(prefix) :]
                break
        if len(s) >= 2 and s[1] == ":":
            s = s[0].upper() + s[1:]
        return Path(s)
    except Exception:
        return None


def bubble_created_ms(blob: dict[str, Any]) -> int | None:
    v = blob.get("createdAt")
    if v is None:
        return None
    if isinstance(v, dict):
        # rare nested shapes
        for k in ("$date", "date", "value"):
            if k in v:
                v = v[k]
                break
        else:
            return None
    if isinstance(v, (int, float)):
        n = int(v)
        if n > 10_000_000_000:
            return n
        return n * 1000
    s = str(v).strip()
    if s.isdigit():
        n = int(s)
        return n if n > 10_000_000_000 else n * 1000
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def read_bubble_tokens(state_db: Path) -> dict[str, Any]:
    """
    Per-request input tokens from type-1 bubbles; per-conv delta output totals.
    """
    if not state_db.exists():
        return {"by_request": {}, "by_conv": {}}

    try:
        con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            ("bubbleId:%",),
        ).fetchall()
        con.close()
    except Exception:
        return {"by_request": {}, "by_conv": {}}

    by_request: dict[str, dict[str, Any]] = {}
    conv_sequences: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 3:
            continue
        conv_id = parts[1]
        try:
            blob = json.loads(row["value"])
        except Exception:
            continue
        if blob.get("type") != 1:
            continue
        cws = blob.get("contextWindowStatusAtCreation")
        if not isinstance(cws, dict):
            continue
        tokens_used = int(cws.get("tokensUsed", 0) or 0)
        if not tokens_used:
            continue
        model = ""
        mi = blob.get("modelInfo")
        if isinstance(mi, dict):
            model = str(mi.get("modelName") or "")
        req_id = blob.get("requestId")
        created_at = blob.get("createdAt") or ""
        user_text_len = len(blob.get("text") or "")
        created_at_ms: int | None = None
        if created_at:
            try:
                if isinstance(created_at, (int, float)):
                    created_at_ms = int(created_at)
                else:
                    dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    created_at_ms = int(dt.timestamp() * 1000)
            except Exception:
                pass
        if req_id:
            by_request[str(req_id)] = {
                "input_tokens": tokens_used,
                "model": model,
                "conv_id": conv_id,
                "created_at_ms": created_at_ms,
            }
        conv_sequences[conv_id].append(
            {
                "ts": created_at,
                "tokens": tokens_used,
                "user_text_len": user_text_len,
                "model": model,
                "req_id": str(req_id) if req_id else "",
            }
        )

    by_conv: dict[str, dict[str, Any]] = {}
    for conv_id, seq in conv_sequences.items():
        seq.sort(key=lambda x: str(x["ts"]))
        total_input = sum(int(s["tokens"]) for s in seq)
        total_output = 0
        model = ""
        for i, s in enumerate(seq):
            if s["model"]:
                model = str(s["model"])
            if i + 1 < len(seq):
                delta = int(seq[i + 1]["tokens"]) - int(s["tokens"])
                if delta > 0:
                    user_msg_tokens = int(seq[i + 1]["user_text_len"]) // 4
                    total_output += max(0, delta - user_msg_tokens)
        by_conv[conv_id] = {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "model": model,
        }

    return {"by_request": by_request, "by_conv": by_conv}


@dataclass
class WorkspaceConvMap:
    """conversation_id -> git root, workspace folder id, and optional title."""

    conv_to_root: dict[str, str]
    conv_to_workspace_id: dict[str, str]
    conv_to_title: dict[str, str]
    # How conv_to_root was inferred: workspace-db | global-composer-headers | global-composer-data
    conv_to_attribution_source: dict[str, str] = field(default_factory=dict)


def _composer_display_title(comp: dict[str, Any]) -> str:
    """Session title from composer metadata (field names vary by Cursor version)."""
    for key in ("name", "title", "sessionName", "chatTitle", "displayName"):
        v = comp.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _item_table_json(db_path: Path, key: str) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = con.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
        con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    try:
        data = json.loads(row[0])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def path_from_uri_obj(uri: Any) -> Path | None:
    """Resolve Cursor/VS Code uri object or string to a local Path (best effort)."""
    if uri is None:
        return None
    if isinstance(uri, str):
        return decode_vscode_uri(uri)
    if isinstance(uri, dict):
        fp = uri.get("fsPath") or uri.get("path")
        if isinstance(fp, str) and fp.strip():
            return _normalise_path(fp)
        ext = uri.get("external")
        if isinstance(ext, str) and ext.strip():
            return decode_vscode_uri(ext)
    return None


def folder_to_repo_root(folder: Path) -> str:
    """Workspace folder path -> git root or folder string (same rules as workspace attribution)."""
    r = find_git_root(str(folder))
    if r:
        return r
    child = _find_child_git_root(folder)
    return str(child) if child else str(folder)


def resolve_root_from_workspace_storage(workspace_storage: Path, ws_hash: str) -> str | None:
    """Look up workspaceStorage/<id>/workspace.json and return a git root or folder path."""
    ws_dir = workspace_storage / ws_hash
    wj = ws_dir / "workspace.json"
    if not wj.exists():
        return None
    try:
        data = json.loads(wj.read_text(encoding="utf-8"))
    except Exception:
        return None
    uri = data.get("folder") or data.get("workspace")
    if not uri:
        return None
    if isinstance(uri, str):
        folder = decode_vscode_uri(uri)
    else:
        folder = path_from_uri_obj(uri)
    if folder is None:
        return None
    return folder_to_repo_root(folder)


def conversation_ids_from_bubble_keys(state_db: Path) -> set[str]:
    """Distinct composer / conversation ids present in global bubbleId:* keys."""
    out: set[str] = set()
    if not state_db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT key FROM cursorDiskKV WHERE key LIKE ?",
            ("bubbleId:%",),
        ).fetchall()
        con.close()
    except Exception:
        return out
    for (key,) in rows:
        parts = str(key).split(":")
        if len(parts) >= 2:
            out.add(parts[1])
    return out


def enrich_workspace_map_from_global_headers(
    global_db: Path,
    workspace_storage: Path,
    ws: WorkspaceConvMap,
) -> None:
    """
    Cursor 3.0+: composer list and workspaceIdentifier live in global ItemTable
    composer.composerHeaders — merge into ws.conv_to_* when workspace DB no longer lists allComposers.
    """
    data = _item_table_json(global_db, "composer.composerHeaders")
    if not data:
        return
    for comp in data.get("allComposers", []):
        if not isinstance(comp, dict):
            continue
        cid = comp.get("composerId")
        if not cid:
            continue
        cid_s = str(cid)
        title = _composer_display_title(comp)
        if title and cid_s not in ws.conv_to_title:
            ws.conv_to_title[cid_s] = title
        if cid_s in ws.conv_to_root:
            continue
        wi = comp.get("workspaceIdentifier")
        root: str | None = None
        ws_id_hint = ""
        if isinstance(wi, dict):
            wid = wi.get("id")
            if isinstance(wid, str) and wid.strip():
                ws_id_hint = wid.strip()
                root = resolve_root_from_workspace_storage(workspace_storage, ws_id_hint)
            if root is None:
                u = wi.get("uri")
                p = path_from_uri_obj(u)
                if p is not None:
                    root = folder_to_repo_root(p)
        if root:
            ws.conv_to_root[cid_s] = root
            ws.conv_to_attribution_source[cid_s] = "global-composer-headers"
            if ws_id_hint:
                ws.conv_to_workspace_id[cid_s] = ws_id_hint


def _git_root_from_context_blob(blob: dict[str, Any]) -> str | None:
    """Try repo path from composerData.context file/folder selections."""
    ctx = blob.get("context")
    if not isinstance(ctx, dict):
        return None
    for key in ("fileSelections", "folderSelections", "terminalSelections"):
        items = ctx.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            u = item.get("uri")
            p = path_from_uri_obj(u)
            if p is None:
                continue
            sp = str(p)
            r = find_git_root(sp)
            if r:
                return r
            if p.is_dir():
                return folder_to_repo_root(p)
    return None


def _load_composer_data_blob(con: sqlite3.Connection, cid: str) -> dict[str, Any] | None:
    key = f"composerData:{cid}"
    for table in ("cursorDiskKV", "ItemTable"):
        try:
            row = con.execute(f"SELECT value FROM {table} WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error:
            continue
        if not row or not row[0]:
            continue
        try:
            d = json.loads(row[0])
            return d if isinstance(d, dict) else None
        except Exception:
            continue
    return None


def enrich_roots_from_composer_data_for_conv_ids(
    global_db: Path,
    ws: WorkspaceConvMap,
    conv_ids: set[str],
) -> None:
    """For conversations still without a workspace mapping, mine composerData:* context for git roots."""
    if not global_db.exists() or not conv_ids:
        return
    try:
        con = sqlite3.connect(f"file:{global_db}?mode=ro", uri=True)
    except Exception:
        return
    try:
        for cid in conv_ids:
            if cid in ws.conv_to_root:
                continue
            blob = _load_composer_data_blob(con, cid)
            if not blob:
                continue
            root = _git_root_from_context_blob(blob)
            if root:
                ws.conv_to_root[cid] = root
                ws.conv_to_attribution_source[cid] = "global-composer-data"
    finally:
        con.close()


def read_global_composer_titles(state_db: Path) -> dict[str, str]:
    """conversation_id -> title from global User/state.vscdb ItemTable, if present."""
    out: dict[str, str] = {}
    if not state_db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        row = con.execute(
            "SELECT value FROM ItemTable WHERE key = 'composer.composerData'",
        ).fetchone()
        con.close()
    except Exception:
        return out
    if not row or not row[0]:
        return out
    try:
        data = json.loads(row[0])
        for comp in data.get("allComposers", []):
            cid = comp.get("composerId")
            if not cid:
                continue
            t = _composer_display_title(comp)
            if t:
                out[str(cid)] = t
    except Exception:
        pass
    return out


def read_workspace_attribution(workspace_storage: Path) -> WorkspaceConvMap:
    conv_to_root: dict[str, str] = {}
    conv_to_workspace_id: dict[str, str] = {}
    conv_to_title: dict[str, str] = {}
    conv_to_attribution_source: dict[str, str] = {}

    if not workspace_storage.exists():
        return WorkspaceConvMap(conv_to_root, conv_to_workspace_id, conv_to_title, conv_to_attribution_source)

    for ws_dir in workspace_storage.iterdir():
        if not ws_dir.is_dir():
            continue
        ws_id = ws_dir.name
        wj = ws_dir / "workspace.json"
        folder: Path | None = None
        if wj.exists():
            try:
                data = json.loads(wj.read_text(encoding="utf-8"))
                uri = data.get("folder")
                if uri:
                    folder = decode_vscode_uri(uri)
            except Exception:
                pass
        if folder is None:
            continue
        root = find_git_root(str(folder))
        if root is None:
            child = _find_child_git_root(folder)
            root = str(child) if child else str(folder)

        ws_db = ws_dir / "state.vscdb"
        if not ws_db.exists():
            continue
        try:
            con = sqlite3.connect(f"file:{ws_db}?mode=ro", uri=True)
            row = con.execute(
                "SELECT value FROM ItemTable WHERE key = 'composer.composerData'",
            ).fetchone()
            con.close()
        except Exception:
            continue
        if not row or not row[0]:
            continue
        try:
            data = json.loads(row[0])
            for comp in data.get("allComposers", []):
                cid = comp.get("composerId")
                if not cid:
                    continue
                cid_s = str(cid)
                title = _composer_display_title(comp)
                if title and cid_s not in conv_to_title:
                    conv_to_title[cid_s] = title
                if cid_s not in conv_to_root:
                    conv_to_root[cid_s] = root
                    conv_to_workspace_id[cid_s] = ws_id
                    conv_to_attribution_source[cid_s] = "workspace-db"
        except Exception:
            continue

    return WorkspaceConvMap(conv_to_root, conv_to_workspace_id, conv_to_title, conv_to_attribution_source)


def read_ai_tracking(
    ai_db: Path,
    bubble_tokens: dict[str, Any],
    since_ts_ms: int | None,
) -> list[dict[str, Any]]:
    if not ai_db.exists():
        return []

    try:
        con = sqlite3.connect(f"file:{ai_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        query = """
            SELECT conversationId, fileName, requestId, model, timestamp
            FROM ai_code_hashes
            WHERE conversationId IS NOT NULL
              AND fileName IS NOT NULL
              AND source = 'composer'
        """
        params: list[Any] = []
        if since_ts_ms:
            query += " AND timestamp >= ?"
            params.append(since_ts_ms)
        query += " ORDER BY timestamp"
        rows = con.execute(query, params).fetchall()
        con.close()
    except Exception:
        return []

    by_req = bubble_tokens.get("by_request", {})
    by_conv_tokens = bubble_tokens.get("by_conv", {})

    conv_files: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "roots": defaultdict(int),
            "model": None,
            "request_ids": set(),
            "files": 0,
            "first_ts": None,
            "last_ts": None,
        }
    )

    for row in rows:
        cid = row["conversationId"]
        fn = row["fileName"]
        root = find_git_root(fn)
        if root is None:
            root = "__unattributed__"
        d = conv_files[cid]
        d["roots"][repo_key(root)] += 1
        d["model"] = row["model"] or d["model"]
        if row["requestId"]:
            d["request_ids"].add(row["requestId"])
        d["files"] += 1
        ts = row["timestamp"]
        if d["first_ts"] is None or (ts and ts < d["first_ts"]):
            d["first_ts"] = ts
        if d["last_ts"] is None or (ts and ts > d["last_ts"]):
            d["last_ts"] = ts

    results: list[dict[str, Any]] = []
    for cid, d in conv_files.items():
        dominant_key = max(d["roots"], key=d["roots"].__getitem__)
        real_root: str = dominant_key
        if dominant_key != "__unattributed__":
            for row in rows:
                if row["conversationId"] != cid:
                    continue
                r = find_git_root(row["fileName"])
                if r and repo_key(r) == dominant_key:
                    real_root = r
                    break

        input_tokens = sum(
            by_req[rid]["input_tokens"] for rid in d["request_ids"] if rid in by_req
        )
        conv_tok = by_conv_tokens.get(cid, {})
        if not input_tokens:
            input_tokens = int(conv_tok.get("input_tokens", 0) or 0)
        output_tokens = int(conv_tok.get("output_tokens", 0) or 0)

        results.append(
            {
                "conversationId": cid,
                "repo_path": real_root,
                "model": d["model"] or "unknown",
                "requests": len(d["request_ids"]) or 1,
                "files": d["files"],
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "has_real_input": input_tokens > 0,
                "first_ts": d["first_ts"],
                "last_ts": d["last_ts"],
                "layer": "commit-linked",
            }
        )
    return results


def read_bubbles(
    state_db: Path,
    since_ts_ms: int | None,
    known_conv_ids: set[str],
    bubble_tokens: dict[str, Any],
    ws: WorkspaceConvMap,
) -> list[dict[str, Any]]:
    if not state_db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            ("bubbleId:%",),
        ).fetchall()
        con.close()
    except Exception:
        return []

    conv_data: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "roots": defaultdict(int),
            "model": None,
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "has_real_input": False,
            "first_ts": None,
            "last_ts": None,
        }
    )

    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 3:
            continue
        cid = parts[1]
        if cid in known_conv_ids:
            continue
        try:
            blob = json.loads(row["value"])
        except Exception:
            continue

        created_at = blob.get("createdAt")
        created_ms = bubble_created_ms(blob)
        if since_ts_ms and created_ms is not None and created_ms < since_ts_ms:
            continue

        d = conv_data[cid]
        tc = blob.get("tokenCount") or {}
        if isinstance(tc, dict):
            d["input_tokens"] += int(tc.get("inputTokens", 0) or 0)
            d["output_tokens"] += int(tc.get("outputTokens", 0) or 0)

        if blob.get("type") == 1:
            cws = blob.get("contextWindowStatusAtCreation")
            if isinstance(cws, dict) and int(cws.get("tokensUsed", 0) or 0) > 0:
                d["input_tokens"] += int(cws["tokensUsed"])
                d["has_real_input"] = True

        mi = blob.get("modelInfo") or {}
        if isinstance(mi, dict) and (mi.get("modelName") or mi.get("name")):
            d["model"] = mi.get("modelName") or mi.get("name")

        if created_ms is not None:
            if d["first_ts"] is None or created_ms < d["first_ts"]:
                d["first_ts"] = created_ms
            if d["last_ts"] is None or created_ms > d["last_ts"]:
                d["last_ts"] = created_ms

        if cid in ws.conv_to_root:
            ws_root = ws.conv_to_root[cid]
            d["roots"][repo_key(ws_root)] += 10

        files: list[str] = []
        for key in ("relevantFiles", "recentlyViewedFiles"):
            val = blob.get(key)
            if isinstance(val, list):
                files.extend(str(x) for x in val if isinstance(x, str))

        for f in files:
            root = find_git_root(f)
            if root:
                d["roots"][repo_key(root)] += 1

        if blob.get("type") == 2:
            d["requests"] += 1

    by_conv_tokens = bubble_tokens.get("by_conv", {})
    for cid in list(conv_data.keys()):
        cd = conv_data[cid]
        if not cd["has_real_input"] and cid in by_conv_tokens:
            ct = by_conv_tokens[cid]
            cd["input_tokens"] += int(ct.get("input_tokens", 0) or 0)
            if cd["input_tokens"] > 0:
                cd["has_real_input"] = True
            if not cd["model"] and ct.get("model"):
                cd["model"] = ct["model"]

    results: list[dict[str, Any]] = []
    for cid, d in conv_data.items():
        if d["roots"]:
            dominant_key = max(d["roots"], key=d["roots"].__getitem__)
            real_root = dominant_key
            if cid in ws.conv_to_root and repo_key(ws.conv_to_root[cid]) == dominant_key:
                real_root = ws.conv_to_root[cid]
            elif dominant_key != "__unattributed__":
                # try to find a real path from bubble files
                for row in rows:
                    p = row["key"].split(":")
                    if len(p) < 3 or p[1] != cid:
                        continue
                    try:
                        b = json.loads(row["value"])
                    except Exception:
                        continue
                    for key in ("relevantFiles", "recentlyViewedFiles"):
                        val = b.get(key)
                        if not isinstance(val, list):
                            continue
                        for f in val:
                            if not isinstance(f, str):
                                continue
                            gr = find_git_root(f)
                            if gr and repo_key(gr) == dominant_key:
                                real_root = gr
                                break
            if cid in ws.conv_to_root and repo_key(ws.conv_to_root[cid]) == repo_key(str(real_root)):
                src = ws.conv_to_attribution_source.get(cid, "")
                if src == "global-composer-headers":
                    layer = "global-composer-headers"
                elif src == "global-composer-data":
                    layer = "global-composer-data"
                else:
                    layer = "workspace-sqlite"
            else:
                layer = "bubble-context"
        else:
            if cid in ws.conv_to_root:
                real_root = ws.conv_to_root[cid]
                src = ws.conv_to_attribution_source.get(cid, "")
                if src == "global-composer-headers":
                    layer = "global-composer-headers"
                elif src == "global-composer-data":
                    layer = "global-composer-data"
                else:
                    layer = "workspace-sqlite"
            else:
                real_root = "__unattributed__"
                layer = "bubble-no-files"

        results.append(
            {
                "conversationId": cid,
                "repo_path": real_root,
                "model": d["model"] or "unknown",
                "requests": int(d["requests"] or 0),
                "files": 0,
                "input_tokens": int(d["input_tokens"] or 0),
                "output_tokens": int(d["output_tokens"] or 0),
                "has_real_input": bool(d["has_real_input"]),
                "first_ts": d["first_ts"],
                "last_ts": d["last_ts"],
                "layer": layer,
            }
        )
    return results


def merge_conv_attribution(layer1: list[dict[str, Any]], layer2: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """conversationId -> repo_path, layer."""
    out: dict[str, dict[str, str]] = {}
    for c in layer1:
        cid = c.get("conversationId")
        if cid:
            out[str(cid)] = {"repo_path": str(c.get("repo_path", "__unattributed__")), "layer": str(c.get("layer", ""))}
    for c in layer2:
        cid = c.get("conversationId")
        if not cid:
            continue
        cid = str(cid)
        if cid not in out:
            out[cid] = {"repo_path": str(c.get("repo_path", "__unattributed__")), "layer": str(c.get("layer", ""))}
    return out


def _output_est_for_user_turn(
    seq_type1: list[dict[str, Any]], turn_index: int
) -> tuple[int, int]:
    """Delta-based output estimate and raw context-window delta between consecutive type-1 bubbles.

    Returns (output_tokens_est, context_window_delta). The raw delta is an
    honest upper bound on per-turn token consumption (user message, model
    output, tool calls, tool results, embedded file content). The output
    estimate additionally subtracts the next user message length.
    """
    if turn_index < 0 or turn_index >= len(seq_type1):
        return 0, 0
    cur = seq_type1[turn_index]
    nxt = seq_type1[turn_index + 1] if turn_index + 1 < len(seq_type1) else None
    if not nxt:
        return 0, 0
    t0 = int(cur.get("tokens_used") or 0)
    t1 = int(nxt.get("tokens_used") or 0)
    delta = t1 - t0
    if delta <= 0:
        return 0, 0
    user_next = int(nxt.get("user_text_len") or 0) // 4
    return max(0, delta - user_next), delta


def _tool_call_chars(blob: dict[str, Any]) -> int:
    """Characters of a type-2 bubble's tool-call payload.

    Cursor stores agent tool calls (including file edits via StrReplace/Write,
    shell commands, reads, searches) in ``toolFormerData`` with the invocation
    arguments and the returned result as strings. Summing these is a direct,
    real measurement of tool-call volume that plain message text never captures.
    """
    tf = blob.get("toolFormerData")
    if not isinstance(tf, dict):
        return 0
    total = 0
    for key in ("rawArgs", "params", "result"):
        v = tf.get(key)
        if isinstance(v, str):
            total += len(v)
    return total


def iter_interactions_from_bubbles(
    state_db: Path,
    conv_attribution: dict[str, dict[str, str]],
    conv_workspace_id: dict[str, str],
    conv_titles: dict[str, str],
    since_ts_ms: int | None,
) -> Iterator[dict[str, Any]]:
    """
    Yield one dict per user submission (type-1 bubble) as an interaction turn.
    """
    if not state_db.exists():
        return
    try:
        con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            ("bubbleId:%",),
        ).fetchall()
        con.close()
    except Exception:
        return

    by_conv: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 3:
            continue
        cid = parts[1]
        try:
            blob = json.loads(row["value"])
        except Exception:
            continue
        by_conv[cid].append((row["key"], blob))

    for conv_id, items in by_conv.items():
        items.sort(key=lambda kv: (bubble_created_ms(kv[1]) or 0, kv[0]))

        # Pass 1: group bubbles into turns (one type-1 user bubble plus the
        # type-2 assistant bubbles that follow it) and build the COMPLETE
        # type-1 sequence first. Building the full sequence up front is what
        # lets the delta between a turn and the next turn be computed
        # correctly; doing it inline previously meant the "next" turn was
        # never present yet, so every delta collapsed to zero.
        turns: list[dict[str, Any]] = []
        type1_seq: list[dict[str, Any]] = []
        cur_turn: dict[str, Any] | None = None

        for _key, blob in items:
            t = blob.get("type")
            if t == 1:
                cws = blob.get("contextWindowStatusAtCreation")
                tokens_used = int(cws.get("tokensUsed", 0) or 0) if isinstance(cws, dict) else 0
                type1_seq.append(
                    {
                        "tokens_used": tokens_used,
                        "user_text_len": len(str(blob.get("text") or "")),
                    }
                )
                cur_turn = {
                    "user": blob,
                    "seq_index": len(type1_seq) - 1,
                    "assistant_chars": 0,
                    "tool_call_chars": 0,
                }
                turns.append(cur_turn)
            elif t == 2 and cur_turn is not None:
                cur_turn["assistant_chars"] += len(str(blob.get("text") or ""))
                cur_turn["tool_call_chars"] += _tool_call_chars(blob)

        # Pass 2: emit one record per turn with correct, fully-populated metrics.
        for turn in turns:
            turn_user = turn["user"]
            idx = int(turn["seq_index"])
            out_est, cw_delta = _output_est_for_user_turn(type1_seq, idx)
            attr = conv_attribution.get(conv_id, {})
            repo_path = attr.get("repo_path", "__unattributed__")
            layer = attr.get("layer", "unknown")
            ws_id = conv_workspace_id.get(conv_id, "")
            cws = turn_user.get("contextWindowStatusAtCreation") or {}
            input_tok = int(cws.get("tokensUsed", 0) or 0) if isinstance(cws, dict) else 0
            mi = turn_user.get("modelInfo") or {}
            model = ""
            if isinstance(mi, dict):
                model = str(mi.get("modelName") or mi.get("name") or "")
            ts_ms = bubble_created_ms(turn_user) or 0
            if since_ts_ms and ts_ms and ts_ms < since_ts_ms:
                continue
            tool_chars = int(turn["tool_call_chars"])
            yield {
                "conversation_id": conv_id,
                "conversation_title": conv_titles.get(conv_id, ""),
                "turn_index": idx,
                "timestamp_ms": ts_ms,
                "model": model,
                "repo_path": repo_path,
                "source_layer": layer,
                "workspace_id": ws_id,
                "request_id": str(turn_user.get("requestId") or ""),
                "input_tokens": input_tok,
                "output_tokens_est": out_est,
                "context_window_delta": cw_delta,
                "event_type": "paired_turn",
                "message_chars_user": len(str(turn_user.get("text") or "")),
                "message_chars_assistant": int(turn["assistant_chars"]),
                "tool_call_chars": tool_chars,
                "tool_call_tokens_est": tool_chars // 4,
            }


def load_raw_interactions(paths: CursorPaths, since_ts_ms: int | None) -> tuple[list[dict[str, Any]], WorkspaceConvMap]:
    """Full scan: attribution + flattened interaction rows."""
    bubble_tokens = read_bubble_tokens(paths.state_vscdb)
    bubble_conv_ids = conversation_ids_from_bubble_keys(paths.state_vscdb)
    ws = read_workspace_attribution(paths.workspace_storage)
    enrich_workspace_map_from_global_headers(
        paths.state_vscdb,
        paths.workspace_storage,
        ws,
    )
    enrich_roots_from_composer_data_for_conv_ids(paths.state_vscdb, ws, bubble_conv_ids)
    layer1 = read_ai_tracking(paths.ai_tracking_db, bubble_tokens, None)
    known = {str(c["conversationId"]) for c in layer1 if c.get("conversationId")}
    layer2 = read_bubbles(paths.state_vscdb, None, known, bubble_tokens, ws)
    conv_attr = merge_conv_attribution(layer1, layer2)
    conv_titles: dict[str, str] = dict(ws.conv_to_title)
    for cid, title in read_global_composer_titles(paths.state_vscdb).items():
        if cid not in conv_titles or not conv_titles[cid]:
            conv_titles[cid] = title
    rows = list(
        iter_interactions_from_bubbles(
            paths.state_vscdb,
            conv_attr,
            ws.conv_to_workspace_id,
            conv_titles,
            since_ts_ms,
        )
    )
    return rows, ws
