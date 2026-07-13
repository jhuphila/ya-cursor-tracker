"""
Join Cursor dashboard usage-events CSV costs onto interaction rows.

Dashboard exports have no conversation_id; we match each usage event to at most
one interaction by timestamp (with optional model compatibility), and copy the
Cost cell verbatim (e.g. Free, Included, 0.96). Unmatched interactions keep an
empty Cost — never invent billing values.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


DEFAULT_USAGE_MATCH_TOLERANCE_MS = 30_000

# Suffixes seen on dashboard Model vs local bubble modelInfo.modelName
_MODEL_SUFFIXES = (
    "-thinking-high",
    "-thinking-medium",
    "-thinking-low",
    "-fast",
    "-medium",
    "-xhigh",
    "-high",
    "-low",
)


@dataclass(frozen=True)
class UsageEvent:
    ts_ms: int
    model: str
    cost: str
    kind: str
    row_index: int


@dataclass(frozen=True)
class UsageMatchStats:
    usage_events: int
    interactions: int
    matched: int
    unmatched_usage: int
    unmatched_interactions: int
    tolerance_ms: int


def parse_iso_to_ms(value: str) -> int | None:
    s = (value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def normalize_model_name(model: str) -> str:
    m = (model or "").strip().lower()
    if not m or m in ("default", "auto"):
        return m
    changed = True
    while changed:
        changed = False
        for suf in _MODEL_SUFFIXES:
            if m.endswith(suf) and len(m) > len(suf):
                m = m[: -len(suf)]
                changed = True
                break
    return m


def models_compatible(usage_model: str, interaction_model: str) -> bool:
    u = normalize_model_name(usage_model)
    i = normalize_model_name(interaction_model)
    # Dashboard "auto" often pairs with blank / default local model names.
    if u == "auto":
        return True
    if not u or not i:
        return False
    if i in ("auto", "default"):
        return True
    if u == i:
        return True
    return u.startswith(i) or i.startswith(u)


def load_usage_events(path: Path) -> list[UsageEvent]:
    """Parse a Cursor dashboard usage-events CSV. Requires Date + Cost columns."""
    if not path.exists():
        raise FileNotFoundError(f"Usage events CSV not found: {path}")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            return []
        fields = {f.strip(): f for f in reader.fieldnames if f}
        date_key = fields.get("Date") or fields.get("date")
        cost_key = fields.get("Cost") or fields.get("cost")
        model_key = fields.get("Model") or fields.get("model")
        kind_key = fields.get("Kind") or fields.get("kind")
        if not date_key or not cost_key:
            raise ValueError(
                f"Usage CSV must include Date and Cost columns; got {list(reader.fieldnames)}"
            )
        out: list[UsageEvent] = []
        for idx, row in enumerate(reader):
            ts = parse_iso_to_ms(str(row.get(date_key, "") or ""))
            cost = str(row.get(cost_key, "") or "").strip()
            if ts is None or not cost:
                continue
            out.append(
                UsageEvent(
                    ts_ms=ts,
                    model=str(row.get(model_key, "") or "").strip() if model_key else "",
                    cost=cost,
                    kind=str(row.get(kind_key, "") or "").strip() if kind_key else "",
                    row_index=idx,
                )
            )
    return out


def match_usage_costs(
    interactions: Sequence[dict[str, Any]],
    events: Sequence[UsageEvent],
    *,
    tolerance_ms: int = DEFAULT_USAGE_MATCH_TOLERANCE_MS,
) -> tuple[dict[str, str], UsageMatchStats]:
    """
    Greedy 1:1 match: each usage event and each interaction used at most once.

    Score = |Δt| plus a penalty when models are incompatible, so close
    timestamp+model pairs win over pure timestamp collisions.
    Returns map interaction_id -> Cost string (verbatim from the usage CSV).
    """
    inter_meta: list[tuple[str, int, str]] = []
    for row in interactions:
        iid = str(row.get("interaction_id") or "")
        if not iid:
            continue
        ts = parse_iso_to_ms(str(row.get("timestamp_utc") or ""))
        if ts is None:
            raw = row.get("_timestamp_ms")
            try:
                ts = int(raw) if raw is not None else None
            except (TypeError, ValueError):
                ts = None
        if ts is None:
            continue
        inter_meta.append((iid, ts, str(row.get("model") or "")))

    candidates: list[tuple[int, int, int, int]] = []
    # (score, abs_delta, event_idx, inter_idx)
    for ei, ev in enumerate(events):
        for ii, (_iid, its, imodel) in enumerate(inter_meta):
            delta = abs(ev.ts_ms - its)
            if delta > tolerance_ms:
                continue
            penalty = 0 if models_compatible(ev.model, imodel) else 5_000
            candidates.append((delta + penalty, delta, ei, ii))
    candidates.sort()

    used_e: set[int] = set()
    used_i: set[int] = set()
    costs: dict[str, str] = {}
    for _score, _delta, ei, ii in candidates:
        if ei in used_e or ii in used_i:
            continue
        used_e.add(ei)
        used_i.add(ii)
        iid = inter_meta[ii][0]
        costs[iid] = events[ei].cost

    stats = UsageMatchStats(
        usage_events=len(events),
        interactions=len(inter_meta),
        matched=len(costs),
        unmatched_usage=len(events) - len(used_e),
        unmatched_interactions=len(inter_meta) - len(used_i),
        tolerance_ms=tolerance_ms,
    )
    return costs, stats


def apply_usage_costs_to_rows(
    rows: list[dict[str, Any]],
    events: Sequence[UsageEvent],
    *,
    tolerance_ms: int = DEFAULT_USAGE_MATCH_TOLERANCE_MS,
    clear_unmatched: bool = True,
) -> tuple[list[dict[str, Any]], UsageMatchStats]:
    """Return copies of rows with Cost set from matched usage events."""
    costs, stats = match_usage_costs(rows, events, tolerance_ms=tolerance_ms)
    out: list[dict[str, Any]] = []
    for row in rows:
        r = dict(row)
        iid = str(r.get("interaction_id") or "")
        if iid in costs:
            r["Cost"] = costs[iid]
        elif clear_unmatched or "Cost" not in r:
            r["Cost"] = ""
        out.append(r)
    return out, stats


def _parse_usage_events_name_date(path: Path) -> datetime | None:
    """Extract YYYY-MM-DD (or YYYYMMDD) from usage-events-<date>... filenames."""
    stem = path.stem
    lower = stem.lower()
    if not lower.startswith("usage-events"):
        return None
    rest = stem[len("usage-events") :].lstrip("-_. ")
    if not rest:
        return None
    # Prefer ISO date prefix: usage-events-2026-07-13 or usage-events-2026-07-13-extra
    for length, fmt in ((10, "%Y-%m-%d"), (8, "%Y%m%d")):
        chunk = rest[:length]
        if len(chunk) < length:
            continue
        try:
            return datetime.strptime(chunk, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def usage_events_recency_key(path: Path) -> tuple[float, float]:
    """
    Sort key for 'most recent' usage export.

    Filename date (usage-events-2026-07-13) outranks an older dated name even if
    mtime is stale; undated usage-events*.csv files fall back to mtime only.
    """
    mtime = 0.0
    try:
        mtime = path.stat().st_mtime
    except OSError:
        pass
    named = _parse_usage_events_name_date(path)
    if named is not None:
        return (named.timestamp(), mtime)
    return (mtime, mtime)


def iter_usage_events_candidates(directory: Path) -> list[Path]:
    """Files in directory whose name starts with usage-events (any suffix)."""
    if not directory.is_dir():
        return []
    out: list[Path] = []
    try:
        for p in directory.iterdir():
            if p.is_file() and p.name.lower().startswith("usage-events"):
                out.append(p)
    except OSError:
        return []
    return out


def pick_newest_usage_events_csv(candidates: Sequence[Path]) -> Path | None:
    files = [p for p in candidates if p.is_file()]
    if not files:
        return None
    return max(files, key=usage_events_recency_key)


def resolve_usage_csv_path(raw: str | Path, base: Path) -> Path:
    """
    Resolve a usage-events export path.

    Accepts:
    - an existing file
    - a directory (picks the newest ``usage-events*`` file inside)
    - a glob containing ``*`` / ``?``
    - a path whose basename starts with ``usage-events`` but is missing: search
      that parent directory for any ``usage-events*`` and pick the newest
      (so ``usage-events-2026-07-13.csv`` still works after you drop a newer export)
    """
    text = str(raw).strip()
    if not text:
        raise FileNotFoundError("usage_events_csv path is empty")

    # Glob relative to base
    if any(ch in text for ch in "*?"):
        pattern = text
        matches = sorted(
            (p.resolve() for p in base.glob(pattern) if p.is_file()),
            key=usage_events_recency_key,
        )
        # also try cwd-style if base.glob found nothing and pattern is not absolute
        if not matches:
            matches = sorted(
                (p.resolve() for p in Path().glob(pattern) if p.is_file()),
                key=usage_events_recency_key,
            )
        chosen = pick_newest_usage_events_csv(matches)
        if chosen is None:
            raise FileNotFoundError(f"No usage-events files matched pattern: {text}")
        return chosen

    p = Path(text)
    if not p.is_absolute():
        p = (base / p).resolve()

    if p.is_file():
        return p

    if p.is_dir():
        chosen = pick_newest_usage_events_csv(iter_usage_events_candidates(p))
        if chosen is None:
            raise FileNotFoundError(f"No usage-events* files found in directory: {p}")
        return chosen

    # Missing path: if it looks like a usage-events export, pick newest in parent
    if p.name.lower().startswith("usage-events"):
        chosen = pick_newest_usage_events_csv(iter_usage_events_candidates(p.parent))
        if chosen is not None:
            return chosen

    raise FileNotFoundError(f"Usage events CSV not found: {p}")
