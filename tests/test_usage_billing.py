"""Tests for dashboard usage-events Cost / Total Tokens matching."""

from __future__ import annotations

from pathlib import Path

from tracker.usage_billing import (
    UsageEvent,
    apply_usage_costs_to_rows,
    load_usage_events,
    match_usage_costs,
    models_compatible,
    normalize_model_name,
    pick_newest_usage_events_csv,
    resolve_usage_csv_path,
)


def test_normalize_model_strips_dashboard_suffixes() -> None:
    assert normalize_model_name("gpt-5.5-medium") == "gpt-5.5"
    assert normalize_model_name("composer-2.5-fast") == "composer-2.5"
    assert normalize_model_name("claude-sonnet-5-thinking-high") == "claude-sonnet-5"
    assert normalize_model_name("grok-4.5-fast-xhigh") == "grok-4.5"


def test_models_compatible() -> None:
    assert models_compatible("gpt-5.5-medium", "gpt-5.5")
    assert models_compatible("composer-2.5-fast", "composer-2.5")
    assert models_compatible("auto", "")
    assert not models_compatible("gpt-5.5-medium", "composer-2.5")


def test_match_prefers_close_timestamp_and_model() -> None:
    from tracker.usage_billing import parse_iso_to_ms

    interactions = [
        {
            "interaction_id": "a",
            "timestamp_utc": "2026-07-09T20:27:06.516Z",
            "model": "gpt-5.5",
        },
        {
            "interaction_id": "b",
            "timestamp_utc": "2026-07-09T20:14:17.940Z",
            "model": "composer-2.5",
        },
    ]
    events = [
        UsageEvent(
            ts_ms=parse_iso_to_ms("2026-07-09T20:27:06.621Z") or 0,
            model="gpt-5.5-medium",
            cost="0.47",
            total_tokens="285073",
            kind="On-Demand",
            row_index=0,
        ),
        UsageEvent(
            ts_ms=parse_iso_to_ms("2026-07-09T20:14:18.172Z") or 0,
            model="composer-2.5-fast",
            cost="Included",
            total_tokens="221401",
            kind="Included",
            row_index=1,
        ),
    ]
    matched, stats = match_usage_costs(interactions, events, tolerance_ms=30_000)
    assert stats.matched == 2
    assert matched["a"].cost == "0.47"
    assert matched["a"].total_tokens == "285073"
    assert matched["b"].cost == "Included"
    assert matched["b"].total_tokens == "221401"


def test_unmatched_cost_left_blank_not_fabricated() -> None:
    from tracker.usage_billing import parse_iso_to_ms

    rows = [
        {
            "interaction_id": "only",
            "timestamp_utc": "2020-01-01T00:00:00Z",
            "model": "gpt-5.5",
            "conversation_title": "old",
        }
    ]
    events = [
        UsageEvent(
            ts_ms=parse_iso_to_ms("2026-07-09T20:27:06.621Z") or 0,
            model="gpt-5.5-medium",
            cost="0.47",
            total_tokens="285073",
            kind="On-Demand",
            row_index=0,
        ),
    ]
    out, stats = apply_usage_costs_to_rows(rows, events, tolerance_ms=30_000)
    assert stats.matched == 0
    assert out[0]["Cost"] == ""
    assert out[0]["Total Tokens"] == ""


def test_load_usage_events_reads_dashboard_csv(tmp_path: Path) -> None:
    p = tmp_path / "usage.csv"
    p.write_text(
        "Date,Kind,Model,Total Tokens,Cost\n"
        '"2026-07-09T20:27:06.621Z","On-Demand","gpt-5.5-medium","285073","0.47"\n'
        '"2026-07-09T20:14:18.172Z","Included","composer-2.5-fast","221401","Included"\n',
        encoding="utf-8",
    )
    events = load_usage_events(p)
    assert len(events) == 2
    assert events[0].cost == "0.47"
    assert events[0].total_tokens == "285073"
    assert events[1].cost == "Included"
    assert events[1].total_tokens == "221401"


def test_apply_usage_sets_total_tokens() -> None:
    from tracker.usage_billing import parse_iso_to_ms

    rows = [
        {
            "interaction_id": "a",
            "timestamp_utc": "2026-07-09T20:27:06.516Z",
            "model": "gpt-5.5",
        }
    ]
    events = [
        UsageEvent(
            ts_ms=parse_iso_to_ms("2026-07-09T20:27:06.621Z") or 0,
            model="gpt-5.5-medium",
            cost="0.47",
            total_tokens="285073",
            kind="On-Demand",
            row_index=0,
        ),
    ]
    out, stats = apply_usage_costs_to_rows(rows, events, tolerance_ms=30_000)
    assert stats.matched == 1
    assert out[0]["Cost"] == "0.47"
    assert out[0]["Total Tokens"] == "285073"


def test_one_to_one_greedy_no_double_assign() -> None:
    from tracker.usage_billing import parse_iso_to_ms

    interactions = [
        {"interaction_id": "a", "timestamp_utc": "2026-07-09T20:27:06.500Z", "model": "gpt-5.5"},
        {"interaction_id": "b", "timestamp_utc": "2026-07-09T20:27:06.600Z", "model": "gpt-5.5"},
    ]
    events = [
        UsageEvent(
            ts_ms=parse_iso_to_ms("2026-07-09T20:27:06.550Z") or 0,
            model="gpt-5.5-medium",
            cost="0.10",
            total_tokens="1000",
            kind="On-Demand",
            row_index=0,
        ),
    ]
    matched, stats = match_usage_costs(interactions, events, tolerance_ms=30_000)
    assert stats.matched == 1
    assert len(matched) == 1
    assert list(matched.values())[0].cost == "0.10"
    assert list(matched.values())[0].total_tokens == "1000"


def test_pick_newest_usage_events_by_filename_date(tmp_path: Path) -> None:
    older = tmp_path / "usage-events-2026-07-12.csv"
    newer = tmp_path / "usage-events-2026-07-13.csv"
    older.write_text("Date,Cost\n", encoding="utf-8")
    newer.write_text("Date,Cost\n", encoding="utf-8")
    # Give older file a newer mtime so filename date must win
    import os
    import time

    now = time.time()
    os.utime(newer, (now - 1000, now - 1000))
    os.utime(older, (now, now))
    chosen = pick_newest_usage_events_csv([older, newer])
    assert chosen == newer


def test_resolve_usage_csv_directory_picks_newest(tmp_path: Path) -> None:
    (tmp_path / "usage-events-2026-07-12.csv").write_text("Date,Cost\n", encoding="utf-8")
    newest = tmp_path / "usage-events-2026-07-13.csv"
    newest.write_text("Date,Cost\n", encoding="utf-8")
    (tmp_path / "other.csv").write_text("x\n", encoding="utf-8")
    assert resolve_usage_csv_path(tmp_path, tmp_path) == newest.resolve()


def test_resolve_missing_usage_events_name_picks_newest_in_parent(tmp_path: Path) -> None:
    (tmp_path / "usage-events-2026-07-12.csv").write_text("Date,Cost\n", encoding="utf-8")
    newest = tmp_path / "usage-events-2026-07-13.csv"
    newest.write_text("Date,Cost\n", encoding="utf-8")
    missing = tmp_path / "usage-events-2026-07-10.csv"
    assert resolve_usage_csv_path(missing, tmp_path) == newest.resolve()
