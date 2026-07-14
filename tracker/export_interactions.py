"""
CLI: export Cursor interactions to CSV with checkpoint dedupe, sample file, and stats.
Optionally join Cost and Total Tokens from a Cursor dashboard usage-events CSV.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

from tracker import __version__
from tracker.csv_store import (
    CSV_FIELDNAMES,
    CheckpointStore,
    ExportStats,
    compute_stats,
    migrate_interactions_csv_schema,
    ms_to_iso,
    print_stats_json,
    read_interactions_csv,
    rewrite_interactions_csv,
    write_interactions_csv,
    write_sample_csv,
)
from tracker.cursor_sources import load_raw_interactions
from tracker.paths import CursorPaths, default_cursor_paths
from tracker.repo_attribution import git_remote_origin, interaction_id, load_attribution_rules, resolve_attribution
from tracker.usage_billing import (
    DEFAULT_USAGE_MATCH_TOLERANCE_MS,
    apply_usage_costs_to_rows,
    load_usage_events,
    resolve_usage_csv_path,
)


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required. pip install pyyaml")
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _resolve_paths(cfg: dict[str, Any]) -> CursorPaths:
    defaults = default_cursor_paths()
    return CursorPaths(
        state_vscdb=Path(cfg["state_vscdb"]) if cfg.get("state_vscdb") else defaults.state_vscdb,
        workspace_storage=Path(cfg["workspace_storage"]) if cfg.get("workspace_storage") else defaults.workspace_storage,
        ai_tracking_db=Path(cfg["ai_tracking_db"]) if cfg.get("ai_tracking_db") else defaults.ai_tracking_db,
    )


def _build_csv_row(
    raw: dict[str, Any],
    *,
    remote_cache: dict[str, str],
    rules: dict[str, Any],
) -> dict[str, Any]:
    conv = str(raw["conversation_id"])
    req = str(raw.get("request_id", ""))
    ts_ms = int(raw.get("timestamp_ms", 0) or 0)
    model = str(raw.get("model", "") or "")
    iid = interaction_id(conv, req, ts_ms, model, int(raw.get("turn_index", 0)))

    repo_path = str(raw.get("repo_path", "__unattributed__"))
    if repo_path not in remote_cache:
        remote_cache[repo_path] = git_remote_origin(repo_path)
    remote = remote_cache[repo_path]

    ws_id = str(raw.get("workspace_id", "") or "")
    rk, rurl, rule_id, conf = resolve_attribution(
        conv,
        repo_path,
        ws_id,
        remote,
        rules,
    )
    if rk == "__ignored__":
        return {}

    return {
        "interaction_id": iid,
        "conversation_id": conv,
        "conversation_title": str(raw.get("conversation_title", "") or ""),
        "turn_index": int(raw.get("turn_index", 0)),
        "timestamp_utc": ms_to_iso(ts_ms),
        "model": model,
        "repo_key": rk,
        "repo_path": repo_path,
        "repo_remote_url": rurl or remote,
        "workspace_id": ws_id,
        "source_layer": str(raw.get("source_layer", "")),
        "input_tokens": int(raw.get("input_tokens", 0) or 0),
        "output_tokens_est": int(raw.get("output_tokens_est", 0) or 0),
        "context_window_delta": int(raw.get("context_window_delta", 0) or 0),
        "event_type": str(raw.get("event_type", "paired_turn")),
        "message_chars_user": int(raw.get("message_chars_user", 0) or 0),
        "message_chars_assistant": int(raw.get("message_chars_assistant", 0) or 0),
        "tool_call_chars": int(raw.get("tool_call_chars", 0) or 0),
        "tool_call_tokens_est": int(raw.get("tool_call_tokens_est", 0) or 0),
        "Cost": "",
        "Total Tokens": "",
        "attribution_rule_id": rule_id,
        "attribution_confidence": f"{conf:.4f}",
        "_timestamp_ms": ts_ms,
        "_conversation_id": conv,
    }


def _resolve_usage_csv(
    cfg: dict[str, Any],
    base: Path,
    cli_usage_csv: Path | None,
) -> Path | None:
    try:
        if cli_usage_csv is not None:
            return resolve_usage_csv_path(cli_usage_csv, Path.cwd())
        raw = cfg.get("usage_events_csv")
        if not raw:
            return None
        return resolve_usage_csv_path(str(raw), base)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return None


def _apply_usage_costs(
    interactions_csv: Path,
    usage_csv: Path,
    *,
    tolerance_ms: int,
    dry_run: bool,
    sample_csv: Path | None = None,
    sample_head: int = 0,
    sample_tail: int = 0,
) -> dict[str, Any]:
    events = load_usage_events(usage_csv)
    rows = read_interactions_csv(interactions_csv)
    if not rows and not interactions_csv.exists():
        raise FileNotFoundError(f"Interactions CSV not found: {interactions_csv}")
    enriched, stats = apply_usage_costs_to_rows(rows, events, tolerance_ms=tolerance_ms)
    if not dry_run:
        if interactions_csv.exists():
            migrate_interactions_csv_schema(interactions_csv)
        rewrite_interactions_csv(interactions_csv, enriched)
        if sample_csv is not None and (sample_head > 0 or sample_tail > 0):
            write_sample_csv(sample_csv, enriched, head=sample_head, tail=sample_tail)
    return {
        "usage_csv": str(usage_csv),
        "interactions_csv": str(interactions_csv),
        "usage_events": stats.usage_events,
        "interactions": stats.interactions,
        "matched": stats.matched,
        "unmatched_usage_events": stats.unmatched_usage,
        "unmatched_interactions": stats.unmatched_interactions,
        "tolerance_ms": stats.tolerance_ms,
        "dry_run": dry_run,
    }


def run_export(
    cfg_path: Path,
    *,
    dry_run: bool,
    usage_csv: Path | None = None,
    apply_usage_only: bool = False,
) -> int:
    cfg = _load_yaml(cfg_path)
    base = cfg_path.parent.resolve()
    paths = _resolve_paths(cfg)

    out_dir = Path(cfg.get("output_dir", "./cursor-exports"))
    if not out_dir.is_absolute():
        out_dir = (base / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    interactions_csv = Path(cfg.get("interactions_csv", "interactions.csv"))
    if not interactions_csv.is_absolute():
        interactions_csv = out_dir / interactions_csv

    sample_csv = Path(cfg.get("sample_csv", "interactions_sample.csv"))
    if not sample_csv.is_absolute():
        sample_csv = out_dir / sample_csv

    checkpoint_path = Path(cfg.get("checkpoint_db", "interactions_checkpoint.sqlite3"))
    if not checkpoint_path.is_absolute():
        checkpoint_path = out_dir / checkpoint_path

    usage_path = _resolve_usage_csv(cfg, base, usage_csv)
    tolerance_ms = int(cfg.get("usage_match_tolerance_ms", DEFAULT_USAGE_MATCH_TOLERANCE_MS) or DEFAULT_USAGE_MATCH_TOLERANCE_MS)
    head_n = int(cfg.get("sample_head_rows", 50))
    tail_n = int(cfg.get("sample_tail_rows", 0))

    if apply_usage_only:
        if usage_path is None:
            print(
                "Error: --apply-usage-only needs a usage export. Set usage_events_csv "
                "(file, directory, or glob like ../usage-events*) or pass --usage-csv.",
                file=sys.stderr,
            )
            return 1
        try:
            result = _apply_usage_costs(
                interactions_csv,
                usage_path,
                tolerance_ms=tolerance_ms,
                dry_run=dry_run,
                sample_csv=sample_csv,
                sample_head=head_n,
                sample_tail=tail_n,
            )
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps({"usage_cost_join": result}, indent=2))
        return 0

    rules_path = cfg.get("attribution_rules_path")
    rules: dict[str, Any] = {}
    if rules_path:
        rp = Path(rules_path)
        if not rp.is_absolute():
            rp = (base / rp).resolve()
        rules = load_attribution_rules(rp)

    since_ms = cfg.get("since_timestamp_ms")
    since_ms = int(since_ms) if since_ms is not None else None

    raw_rows, _ws = load_raw_interactions(paths, since_ms)
    remote_cache: dict[str, str] = {}
    built: list[dict[str, Any]] = []
    for raw in raw_rows:
        row = _build_csv_row(raw, remote_cache=remote_cache, rules=rules)
        if not row:
            continue
        built.append(row)

    # History span over full extracted set (before dedupe)
    conv_all = [r["_conversation_id"] for r in built]
    min_ms, max_ms, distinct_conv = compute_stats(
        [{"timestamp_ms": r["_timestamp_ms"]} for r in built],
        conv_all,
    )

    ck = CheckpointStore(checkpoint_path)
    new_rows: list[dict[str, Any]] = []
    skipped = 0
    for r in built:
        iid = r["interaction_id"]
        if ck.has(iid):
            skipped += 1
            continue
        new_rows.append({k: v for k, v in r.items() if not k.startswith("_")})

    try:
        if interactions_csv.exists() and not dry_run:
            migrate_interactions_csv_schema(interactions_csv)
        appended, _ = write_interactions_csv(interactions_csv, new_rows, dry_run=dry_run)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not dry_run and (head_n > 0 or tail_n > 0):
        # Sample from newly written rows only; usage join may refresh sample below.
        write_sample_csv(sample_csv, new_rows, head=head_n, tail=tail_n)

    if not dry_run and new_rows:
        ck.add_many([r["interaction_id"] for r in new_rows])

    stats = ExportStats(
        rows_written=appended,
        rows_skipped_seen=skipped,
        total_interactions_in_run=len(built),
        min_ts_ms=min_ms,
        max_ts_ms=max_ms,
        distinct_conversations=distinct_conv,
    )
    sample_path = sample_csv if not dry_run and (head_n > 0 or tail_n > 0) else None
    print_stats_json(stats, sample_path, interactions_csv)

    if usage_path is not None:
        try:
            result = _apply_usage_costs(
                interactions_csv,
                usage_path,
                tolerance_ms=tolerance_ms,
                dry_run=dry_run,
                sample_csv=sample_csv if not dry_run else None,
                sample_head=head_n,
                sample_tail=tail_n,
            )
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(json.dumps({"usage_cost_join": result}, indent=2))

    if dry_run:
        # show a preview of up to 5 rows as JSON (subset of fields)
        preview = []
        for r in new_rows[:5]:
            preview.append({k: r.get(k) for k in CSV_FIELDNAMES})
        print(json.dumps({"dry_run_preview": preview, "would_write_count": len(new_rows)}, indent=2))

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Cursor interactions to CSV")
    parser.add_argument("--config", type=Path, default=Path("config/tracker_config.yaml"), help="Path to tracker_config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Do not write files or update checkpoint")
    parser.add_argument(
        "--usage-csv",
        type=Path,
        default=None,
        help="Cursor dashboard usage-events CSV (adds Cost and Total Tokens via timestamp match)",
    )
    parser.add_argument(
        "--apply-usage-only",
        action="store_true",
        help="Only join Cost / Total Tokens from usage CSV onto existing interactions.csv (skip Cursor DB export)",
    )
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if yaml is None:
        print("Error: PyYAML is required (pip install pyyaml)", file=sys.stderr)
        return 1
    if not args.config.exists():
        print(f"Error: config not found: {args.config}", file=sys.stderr)
        print("Copy config/tracker_config.example.yaml to config/tracker_config.yaml", file=sys.stderr)
        return 1
    return run_export(
        args.config,
        dry_run=bool(args.dry_run),
        usage_csv=args.usage_csv,
        apply_usage_only=bool(args.apply_usage_only),
    )


if __name__ == "__main__":
    raise SystemExit(main())
