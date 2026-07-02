# ya-cursor-tracker

Export Cursor AI **interactions** (one row per user submission / model turn context) to **append-only CSV**, with a SQLite checkpoint so reruns stay idempotent. Works on **Windows, Linux, and macOS** by resolving Cursor’s local SQLite paths from the environment (overridable in YAML).

Inspired by [cursortrack](https://github.com/jeremypot/cursortrack) (attribution layers and bubble parsing); this project stores **CSV + checkpoint** instead of a usage dashboard.

## Requirements

- Python **3.10+**
- [PyYAML](https://pyyaml.org/) (`pip install -r requirements.txt`)
- Cursor installed and used at least once (so local DBs exist)

## Quick start

```bash
pip install -r requirements.txt
# Optional: pip install -e ".[dev]" for pytest
cp config/tracker_config.example.yaml config/tracker_config.yaml   # if you removed the default
python -m tracker --config config/tracker_config.yaml
# equivalent: python -m tracker.export_interactions --config config/tracker_config.yaml
```

Each run prints JSON stats: rows written, skipped (already checkpointed), total interactions seen, min/max timestamps, distinct conversations, and paths to the main CSV and sample CSV.

- **`--dry-run`**: no CSV writes, no checkpoint updates; prints a short preview of rows that would be appended.

Outputs default to `./cursor-exports/` (relative to the **config file’s directory**).

## Git post-commit hook (optional)

From your git repo root (after installing this package so `python -m tracker` is available):

```bash
python -m tracker.install_hook --repo .
```

This installs a **fail-open** `.git/hooks/post-commit` that runs the exporter; failures never block commits.

For periodic catch-ups, schedule the same command as cron / Task Scheduler.

## Configuration

- **`config/tracker_config.yaml`**: output paths, sample row counts, optional `since_timestamp_ms`, optional overrides for `state_vscdb`, `workspace_storage`, `ai_tracking_db`.
- **`config/attribution_rules.example.yaml`**: copy to e.g. `attribution_rules.yaml`, reference from tracker config via `attribution_rules_path`. Supports manual conversation overrides, workspace hints, path prefix mappings, and repo URL aliases.

## How attribution works

1. **commit-linked**: `~/.cursor/ai-tracking/ai-code-tracking.db` (`composer` rows) → git root from touched files.
2. **workspace-sqlite** (`workspace-db`): `workspaceStorage/*/state.vscdb` `composer.composerData` `allComposers` → workspace folder → git root (Cursor ≤2.6 style).
3. **global-composer-headers**: global `globalStorage/state.vscdb` `ItemTable` key `composer.composerHeaders` — `allComposers[].workspaceIdentifier` links each `composerId` to a workspace hash or folder URI (Cursor **3.0+** central index; see [cursaves](https://github.com/Callum-Ward/cursaves/blob/main/docs/how-cursor-stores-chats.md)).
4. **global-composer-data**: same global DB, `composerData:{composerId}` in `cursorDiskKV` / `ItemTable` — `context.fileSelections` / `folderSelections` / `terminalSelections` URIs → git root when (1)–(3) did not map the chat.
5. **bubble-context** / **bubble-no-files**: `bubbleId:*` blobs — `relevantFiles` / `recentlyViewedFiles`, or no resolvable path.

The CSV **`source_layer`** reflects which mechanism applied for that conversation’s bubble-derived attribution pass (`global-composer-headers` and `global-composer-data` appear when the newer global sources supplied the workspace/repo link).

Per-interaction rows add `repo_remote_url` via `git remote get-url origin` when the resolved path is a local git checkout.

## Data model

CSV columns are defined in `tracker/csv_store.py` (`CSV_FIELDNAMES`). Each row includes **`conversation_title`** when Cursor stores it in composer metadata (`allComposers` in workspace `composer.composerData`, global `composer.composerHeaders`, or legacy global `composer.composerData`).

If you already have an `interactions.csv` from an older schema, either point `interactions_csv` at a new filename or delete the old CSV and checkpoint database so the new header (with `conversation_title`) can be written cleanly.

### Token and volume fields

- **`input_tokens`**: real, measured from `contextWindowStatusAtCreation.tokensUsed` on each user bubble — the cumulative context-window size (in tokens) at the moment that turn was sent. This already includes conversation history, tool results, and embedded file content folded into context. Populated for turns where Cursor recorded the snapshot. To get a conversation total, take the **max** across its turns (the value is cumulative, so summing double-counts).
- **`context_window_delta`**: growth of `tokensUsed` from this turn to the next (no subtraction). It is an honest **upper bound** on tokens consumed for the turn, capturing the user message, model output, tool calls, tool results, and newly embedded files. The final turn of a conversation has no following snapshot and reports `0`.
- **`output_tokens_est`**: same delta, minus an estimate of the next user message length, as a rough output-only figure.
- **`tool_call_chars`**: directly measured characters of agent tool-call payloads (`toolFormerData` `rawArgs` + `params` + `result`) summed across the turn's assistant bubbles. This captures file edits (StrReplace/Write), shell commands, reads, and searches that plain message text misses — typically the dominant share of token volume in agentic turns.
- **`tool_call_tokens_est`**: `tool_call_chars // 4`, a rough token estimate for the tool-call volume.
- **`message_chars_user` / `message_chars_assistant`**: character counts of the user prompt and assistant reply text only (no tool payloads).

### Schema migration

These columns change `CSV_FIELDNAMES`, so the header check rejects any `interactions.csv` written with an older schema. To upgrade, either point `interactions_csv` in your config at a new filename, or delete the old CSV **and** the checkpoint `.sqlite3` file, then re-export from scratch.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

## License

MIT (same spirit as cursortrack; this repository is a separate implementation).
