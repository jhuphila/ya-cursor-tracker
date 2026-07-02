"""Resolve repo_key from heuristics + optional YAML rules + git remote."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore


def _slug(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[\s.]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def git_remote_origin(repo_path: str, timeout_sec: float = 3.0) -> str:
    if not repo_path or repo_path == "__unattributed__":
        return ""
    p = Path(repo_path)
    if not (p / ".git").exists():
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(p), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        if proc.returncode != 0:
            return ""
        return (proc.stdout or "").strip()
    except Exception:
        return ""


def _canonical_from_remote_url(url: str) -> str:
    """Derive owner/repo style key from common git remote URLs."""
    u = url.strip()
    if not u:
        return ""
    # git@github.com:org/repo.git
    m = re.match(r"git@[^:]+:(.+?)(?:\.git)?$", u)
    if m:
        return m.group(1).replace("\\", "/").rstrip("/")
    # https://github.com/org/repo
    m = re.match(r"https?://[^/]+/(.+?)(?:\.git)?/?$", u)
    if m:
        return m.group(1).rstrip("/")
    return _slug(u)


def load_attribution_rules(path: Path | None) -> dict[str, Any]:
    if not path or not path.exists():
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is required to load attribution rules. Install pyyaml.")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def resolve_attribution(
    conversation_id: str,
    repo_path: str,
    workspace_id: str,
    remote_url: str,
    rules: dict[str, Any],
) -> tuple[str, str, str, float]:
    """
    Returns (repo_key, repo_remote_url, attribution_rule_id, confidence).
    """
    remote_url = remote_url or ""
    if not rules:
        rk = _canonical_from_remote_url(remote_url) if remote_url else ""
        if not rk and repo_path and repo_path != "__unattributed__":
            rk = repo_path.lower().replace("\\", "/").rstrip("/")
        if not rk:
            rk = "__unattributed__"
        return rk, remote_url, "heuristic_default", 0.5

    emit_trace = bool(rules.get("emit_resolution_trace"))
    _ = emit_trace  # reserved for CSV column population

    manual = rules.get("manual_overrides") or {}
    if conversation_id in manual:
        v = str(manual[conversation_id])
        return v, remote_url, "manual_override", 1.0

    ws_hints = rules.get("workspace_hints") or {}
    if workspace_id and workspace_id in ws_hints:
        v = str(ws_hints[workspace_id])
        return v, remote_url, "workspace_hint", 0.95

    # Remote URL alias groups: canonical -> list of aliases
    aliases = rules.get("repo_aliases") or {}
    if isinstance(aliases, dict) and remote_url:
        ru_norm = remote_url.rstrip("/").lower()
        for canonical, lst in aliases.items():
            if not isinstance(lst, list):
                continue
            for a in lst:
                if str(a).rstrip("/").lower() == ru_norm:
                    return str(canonical), remote_url, "repo_alias", 0.95

    # Path prefix mappings
    for pm in rules.get("path_mappings") or []:
        if not isinstance(pm, dict):
            continue
        rid = str(pm.get("id", "path_map"))
        prefix = pm.get("remote_prefix") or pm.get("local_prefix") or ""
        if not prefix or not repo_path:
            continue
        rp = repo_path.replace("\\", "/")
        if rp.startswith(str(prefix)):
            seg_idx = int(pm.get("repo_key_from_segment_index", -1))
            stripped = rp[len(str(prefix)) :].lstrip("/")
            parts = [p for p in stripped.split("/") if p]
            if 0 <= seg_idx < len(parts):
                key = parts[seg_idx]
                return key, remote_url, rid, float(pm.get("priority", 50)) / 100.0

    ignore = rules.get("ignore_paths") or []
    for ign in ignore:
        if ign and repo_path.replace("\\", "/").startswith(str(ign).rstrip("/")):
            return "__ignored__", remote_url, "ignore_paths", 1.0

    rk = _canonical_from_remote_url(remote_url) if remote_url else ""
    if not rk and repo_path and repo_path != "__unattributed__":
        rk = repo_path.lower().replace("\\", "/").rstrip("/")
    if not rk:
        rk = "__unattributed__"
    pol = str(rules.get("default_policy", "keep_with_unknown"))
    if rk == "__unattributed__" and pol == "drop":
        return "", remote_url, "default_drop", 0.0
    return rk, remote_url, "heuristic_fallback", 0.5


def interaction_id(
    conversation_id: str,
    request_id: str,
    timestamp_ms: int,
    model: str,
    turn_index: int,
) -> str:
    raw = f"{conversation_id}|{request_id}|{timestamp_ms}|{model}|{turn_index}".encode(
        "utf-8",
        errors="replace",
    )
    return hashlib.sha256(raw).hexdigest()
