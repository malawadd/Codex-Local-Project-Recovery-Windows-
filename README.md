# Codex Local Project Recovery (Windows)

**Unofficial community recovery utility. Not an OpenAI-supported repair tool.**

This package is for a specific Codex Desktop / ChatGPT Desktop failure mode on Windows:

- the **Projects** section suddenly shows **No projects** or loses some/all local projects;
- the chats are still visible in **Recents** or otherwise still exist;
- the local source folders still exist;
- `state_*.sqlite` still contains project/root/thread metadata;
- the Desktop global-state JSON lost or corrupted its project catalog and thread-to-project associations.

The tool diagnoses that state and, when it can do so safely, rebuilds the **Desktop JSON project catalog and thread associations** from the surviving SQLite metadata.

## Safety model

The repair intentionally follows a conservative model:

- **SQLite is opened read-only.** The utility never updates `state_*.sqlite`.
- It does **not** edit session history or source repositories.
- Before any repair, it creates a timestamped backup in:

  `%USERPROFILE%\CodexProjectRecoveryBackups\`

- It writes only:

  - `.codex-global-state.json`
  - `.codex-global-state.json.bak`

- Writes are atomic and UTF-8 without a BOM.
- It refuses to repair while `ChatGPT.exe` or `Codex.exe` still appears to be running, because an in-memory Desktop state can overwrite a repaired file when the app exits.
- Threads that cannot be matched safely are left alone rather than guessed.

## Requirements

- Windows 10/11
- Python 3 (standard library only — no `pip install` required)
- A local Codex state directory, normally `%USERPROFILE%\.codex`

If `CODEX_HOME` is set, the tool uses that automatically. You can also override it from the command line.

## Quick start

### 1. Diagnose first

Open PowerShell in the extracted folder and run:

```powershell
.\Diagnose.ps1
```

This is read-only. It also writes a **redacted** text report to your Desktop.

Look for:

- `SQLite projects: N` where `N > 0`
- a healthy `Integrity: ok`
- fewer `local-projects entries` than SQLite projects
- a recovery plan that can restore projects

### 2. Fully exit Codex / ChatGPT Desktop

Close the window, then verify `ChatGPT.exe` / `Codex.exe` is no longer running in Task Manager.

### 3. Repair

```powershell
.\Repair.ps1
```

The script prints a plan and asks you to type:

```text
REPAIR
```

It then creates a backup and replaces the global-state JSON.

### 4. Reopen Codex

Check the **Projects** section before creating or deleting any projects.

## Duplicate project roots

Sometimes re-adding a missing folder creates a second project record pointing at the same path. The utility **keeps all project records in the sidebar catalog**, but it must decide what to do with currently-unassigned chats that match more than one project with the same root.

The default is safest:

```powershell
.\Repair.ps1 --duplicate-policy leave
```

That restores both projects but leaves ambiguous chats unassigned.

To attach ambiguous chats to the oldest duplicate project:

```powershell
.\Repair.ps1 --duplicate-policy oldest
```

Or to the newest duplicate project:

```powershell
.\Repair.ps1 --duplicate-policy newest
```

**Existing explicit assignments are always preserved.** The duplicate policy is applied only to chats that are currently unassigned.

## Orphaned existing assignments

An existing global-state assignment may reference a project ID that no longer exists in the SQLite project mapping. By default, repair stops rather than guessing:

```text
--orphan-policy error
```

After reviewing the diagnosis, you can explicitly choose:

```powershell
.\Repair.ps1 --orphan-policy keep
```

or:

```powershell
.\Repair.ps1 --orphan-policy drop
```

`drop` only removes the broken project association; it does **not** delete the underlying chat.

## Dry-run repair

To run the same repair planner without writing anything:

```powershell
.\Repair.ps1 --dry-run
```

## Additional legacy thread sources

The utility automatically recognizes:

- modern user threads: `thread_source = user`
- older top-level threads with `source = vscode`
- older top-level threads with `source = cli`

It deliberately skips subagents/internal rows.

If your diagnosis shows another known top-level source that you want to include, add it explicitly:

```powershell
.\Repair.ps1 --include-source desktop
```

Do not add a source unless you have inspected it and know it represents normal user conversations.

## Custom `CODEX_HOME`

```powershell
.\Diagnose.ps1 --codex-home 'D:\MyCodexState'
.\Repair.ps1 --codex-home 'D:\MyCodexState'
```

The Python CLI can also be used directly:

```powershell
py -3 .\codex_project_recovery.py diagnose --redact
py -3 .\codex_project_recovery.py repair
```

## Restoring a backup

First fully exit Codex / ChatGPT Desktop.

Then:

```powershell
.\Restore-Backup.ps1 "$env:USERPROFILE\CodexProjectRecoveryBackups\20260927-120000-before-repair"
```

The restore command rolls back only the global-state JSON files. The repair utility never modifies SQLite, so SQLite is intentionally not rolled back.

Before restoring, the tool also backs up the current state so you can undo the rollback if needed.

## What the recovery reconstructs

When supported metadata is available, the utility rebuilds:

- `local-projects`
- `project-order`
- `thread-project-assignments`
- `thread-workspace-root-hints`
- removes newly-assigned threads from `projectless-thread-ids` when that field exists

Project definitions come from the durable SQLite tables:

- `projects`
- `project_roots`
- `project_idempotency_keys`

User/top-level chats are matched to project roots using their surviving `cwd`. The most-specific/deepest matching project root wins. Duplicate-root matches are handled by the explicit policy described above.

## When **not** to use this tool

Do not run repair if:

- `PRAGMA integrity_check` is not `ok`;
- SQLite no longer contains your project definitions/roots;
- the problem concerns cloud/ChatGPT Projects rather than **local Codex projects**;
- the diagnosis reports an unsupported schema or ambiguous project ID mapping;
- you have not made sure Codex / ChatGPT Desktop is fully closed.

The tool will refuse many of these states automatically.

## Privacy

Diagnostic output can contain local folder paths. `Diagnose.ps1` uses `--redact`, replacing your user-home prefix with `%USERPROFILE%` in the saved report. Review any report before posting it publicly.

Never upload your complete `.codex` directory or session history to a public issue unless you have intentionally reviewed and sanitized it.

## Why this utility exists

As of September 2026, multiple public OpenAI Codex issue reports describe Windows Desktop states where durable local project/session data survives while the sidebar/global-state project registry or assignment migration is lost or reset. Examples:

- https://github.com/openai/codex/issues/42739
- https://github.com/openai/codex/issues/42867
- https://github.com/openai/codex/issues/46987
- https://github.com/openai/codex/issues/47764

Those reports are useful background, but this package is independent and unofficial.

## Files in this package

- `codex_project_recovery.py` — recovery engine
- `Diagnose.ps1` — recommended read-only first step; saves a redacted report
- `Repair.ps1` — recommended repair wrapper
- `Restore-Backup.ps1` — rollback helper
- `Diagnose.bat` / `Repair.bat` — simple Command Prompt wrappers
- `Self-Test.ps1` — runs the included unit tests
- `docs/TROUBLESHOOTING.md` — common failure cases
- `docs/TECHNICAL_NOTES.md` — recovery algorithm and data model
- `tests/` — synthetic tests; no real Codex data is included
- `LICENSE` — MIT license

## Disclaimer

This utility manipulates local application metadata and is provided without warranty. Always keep the automatically-created backup until you have verified your projects and chats after reopening Codex.
