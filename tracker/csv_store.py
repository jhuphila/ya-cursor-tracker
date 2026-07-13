"""Append-only CSV export, sample slice, and SQLite-backed dedupe checkpoint."""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

CSV_FIELDNAMES: list[str] = [
    "interaction_id",
    "conversation_id",
    "conversation_title",
    "turn_index",
    "timestamp_utc",
    "model",
    "repo_key",
    "repo_path",
    "repo_remote_url",
    "workspace_id",
    "source_layer",
    "input_tokens",
    "output_tokens_est",
    "context_window_delta",
    "event_type",
    "message_chars_user",
    "message_chars_assistant",
    "tool_call_chars",
    "tool_call_tokens_est",
    "Cost",
    "attribution_rule_id",
    "attribution_confidence",
]


@dataclass
class ExportStats:
    rows_written: int
    rows_skipped_seen: int
    total_interactions_in_run: int
    min_ts_ms: int | None
    max_ts_ms: int | None
    distinct_conversations: int


class CheckpointStore:
    """SQLite for O(1) membership checks across platforms."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path), timeout=30)
        con.execute("CREATE TABLE IF NOT EXISTS seen (interaction_id TEXT PRIMARY KEY)")
        con.commit()
        return con

    def has(self, interaction_id: str) -> bool:
        try:
            con = self._conn()
            row = con.execute("SELECT 1 FROM seen WHERE interaction_id = ?", (interaction_id,)).fetchone()
            con.close()
            return row is not None
        except Exception:
            return False

    def add_many(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        con = self._conn()
        con.executemany("INSERT OR IGNORE INTO seen (interaction_id) VALUES (?)", [(i,) for i in ids])
        con.commit()
        con.close()


def ms_to_iso(ms: int) -> str:
    if not ms:
        return ""
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _existing_csv_header_matches(path: Path, expected: Sequence[str]) -> bool:
    """Return True if file is empty or header row matches expected field order."""
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            r = csv.reader(fh)
            row = next(r, None)
    except OSError:
        return True
    if not row:
        return True
    return list(row) == list(expected)


def read_interactions_csv(path: Path) -> list[dict[str, Any]]:
    """Load all interaction rows (any prior header); missing fields become empty."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return [dict(row) for row in reader]


def rewrite_interactions_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    """Overwrite interactions CSV with the current schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in CSV_FIELDNAMES})


def migrate_interactions_csv_schema(path: Path) -> bool:
    """
    If an existing interactions.csv is missing only newly added columns (e.g. Cost),
    rewrite it in place with blanks for those fields. Returns True when a rewrite ran.
    """
    if not path.exists():
        return False
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
    except OSError:
        return False
    if not header:
        return False
    if list(header) == list(CSV_FIELDNAMES):
        return False
    old_set = set(header)
    new_set = set(CSV_FIELDNAMES)
    unexpected = sorted(old_set - new_set)
    if unexpected:
        raise ValueError(
            f"Existing CSV header does not match current schema (unexpected columns {unexpected} in {path}). "
            f"Use a new output path or remove the CSV and checkpoint DB, then re-export."
        )
    if not old_set <= new_set:
        raise ValueError(
            f"Existing CSV header does not match current schema. "
            f"Use a new output path or remove {path} and the checkpoint DB, then re-export."
        )
    rows = read_interactions_csv(path)
    rewrite_interactions_csv(path, rows)
    return True


def write_interactions_csv(
    output_csv: Path,
    rows: Iterable[dict[str, Any]],
    *,
    dry_run: bool,
) -> tuple[int, list[dict[str, Any]]]:
    """Append rows to CSV; returns (count_appended, materialized_row_dicts)."""
    materialized = list(rows)
    if dry_run:
        return 0, materialized

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    new_file = not output_csv.exists()
    if not new_file and not _existing_csv_header_matches(output_csv, CSV_FIELDNAMES):
        migrate_interactions_csv_schema(output_csv)
        if not _existing_csv_header_matches(output_csv, CSV_FIELDNAMES):
            raise ValueError(
                f"Existing CSV header does not match current schema (expected columns including "
                f"'conversation_title', 'context_window_delta', and 'Cost'). Use a new output path or remove "
                f"{output_csv} and the checkpoint DB, then re-export."
            )
    with open(output_csv, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        for r in materialized:
            writer.writerow({k: r.get(k, "") for k in CSV_FIELDNAMES})
    return len(materialized), materialized


def write_sample_csv(path: Path, rows: Sequence[dict[str, Any]], *, head: int, tail: int) -> None:
    if head <= 0 and tail <= 0:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    sample: list[dict[str, Any]] = []
    if head > 0:
        sample.extend(rows[:head])
    if tail > 0 and tail != head:
        sample.extend(rows[-tail:])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        for r in sample:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDNAMES})


def compute_stats(rows: Sequence[dict[str, Any]], conv_ids: Iterable[str]) -> tuple[int | None, int | None, int]:
    ts = [int(r.get("timestamp_ms", 0) or 0) for r in rows if int(r.get("timestamp_ms", 0) or 0)]
    mn = min(ts) if ts else None
    mx = max(ts) if ts else None
    return mn, mx, len(set(conv_ids))


def print_stats_json(stats: ExportStats, sample_path: Path | None, output_csv: Path) -> None:
    payload = {
        "rows_written": stats.rows_written,
        "rows_skipped_seen": stats.rows_skipped_seen,
        "total_interactions_in_run": stats.total_interactions_in_run,
        "min_timestamp_ms": stats.min_ts_ms,
        "max_timestamp_ms": stats.max_ts_ms,
        "distinct_conversations": stats.distinct_conversations,
        "output_csv": str(output_csv),
        "sample_csv": str(sample_path) if sample_path else None,
    }
    print(json.dumps(payload, indent=2))
