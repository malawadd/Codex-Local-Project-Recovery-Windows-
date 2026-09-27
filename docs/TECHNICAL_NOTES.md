# Technical notes

## Scope

This tool targets local Codex Desktop project metadata on Windows. It is not intended for cloud ChatGPT Projects or for repairing SQLite corruption.

## Durable sources used

The utility opens the selected `state_*.sqlite` database in SQLite read-only URI mode and requires these tables:

- `projects`
- `project_roots`
- `project_idempotency_keys`
- `threads`

`project_idempotency_keys` is used to recover the Desktop/legacy project ID associated with the durable SQLite project UUID.

## Global-state source selection

The preferred template is:

1. `.codex-global-state.json`
2. `.codex-global-state.json.bak`
3. newest parseable `.codex-global-state.json.tmp-*`

A fallback is used only when an earlier candidate is unreadable. This lets the repair preserve unrelated settings instead of starting from an empty JSON object.

## Thread classification

Existing valid assignments always win and are preserved exactly.

For currently-unassigned rows, automatic matching is limited to:

- `thread_source = user`; or
- `thread_source IS NULL` and `source` is an explicitly allowed legacy top-level source (`vscode` and `cli` by default).

This avoids auto-assigning internal/subagent rows.

## Root matching

Windows paths are normalized with `ntpath`, including removal of the `\\?\` extended-path prefix.

A thread matches when its `cwd` is:

- exactly equal to a project root; or
- a descendant of a project root.

The deepest/most-specific matching root wins.

If the deepest root belongs to multiple project identities, the match is ambiguous. Existing explicit assignments remain unchanged. For unassigned threads, behavior is controlled by:

- `leave` — no inferred assignment;
- `oldest` — choose the project with the oldest creation timestamp/position;
- `newest` — choose the newest.

## Assignment formats

The utility preserves string-style assignments and supports the known object-style fields:

- `projectKind`
- `projectId`
- `cwd`
- `pendingCoreUpdate`

If unknown object fields or mixed assignment encodings are detected, repair refuses to guess.

## State rebuilt

The utility rebuilds or updates:

- `local-projects`
- `project-order`
- `thread-project-assignments`
- `thread-workspace-root-hints`
- `projectless-thread-ids` (only to remove threads that were successfully assigned)
- `selected-project` only when the current selection is invalid

Other global-state fields are preserved from the selected template.

## Write behavior

The utility:

1. verifies SQLite integrity;
2. computes the repair entirely in memory;
3. refuses to continue while the Desktop process is active;
4. creates an external timestamped backup;
5. verifies the global-state file did not change during planning;
6. serializes UTF-8 without BOM;
7. atomically writes `.bak` first and the primary JSON last;
8. reparses and validates the files after writing.

SQLite is never written.

## Backup contents

A repair backup contains all present `.codex-global-state.json*` candidates plus the selected SQLite database and its `-wal` / `-shm` companions when present, along with a SHA-256 manifest.

The SQLite copy is for forensic/safety purposes. The normal rollback command only restores the global-state JSON because the recovery itself never alters SQLite.
