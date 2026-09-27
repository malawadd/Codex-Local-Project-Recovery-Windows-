# Troubleshooting

## Python 3 was not found

Install Python 3 for Windows and make sure either `py` or `python` is available in PowerShell:

```powershell
py -3 --version
```

or:

```powershell
python --version
```

No third-party Python packages are required.

## PowerShell blocks the `.ps1` file

You can run it for the current invocation without changing the machine-wide execution policy:

```powershell
powershell -ExecutionPolicy Bypass -File .\Diagnose.ps1
```

For repair:

```powershell
powershell -ExecutionPolicy Bypass -File .\Repair.ps1
```

## Repair says ChatGPT.exe / Codex.exe is still running

Closing the window may not terminate every background Desktop process.

Open Task Manager and end the remaining `ChatGPT.exe` / `Codex.exe` processes, then rerun repair.

Do not edit the JSON while the app is still holding an in-memory copy of the state.

## SQLite integrity is not `ok`

Stop. This utility is not a SQLite corruption repair tool.

Do not run `repair`. Preserve your `.codex` directory and seek a database-specific recovery path.

## SQLite has projects, but diagnosis says project ID mapping is missing/ambiguous

This utility uses `project_idempotency_keys` as the bridge from the durable SQLite project UUID to the Desktop/legacy project ID used in the global-state JSON.

If a project has no usable mapping, or multiple mappings cannot be disambiguated safely, the utility refuses to invent an ID.

Preserve the diagnosis and inspect older `.codex-global-state.json.tmp-*` snapshots or report the issue upstream.

## Existing orphan assignments block repair

The global-state JSON contains assignments to project IDs that are not present in the current SQLite mapping.

The safest default is to stop:

```text
--orphan-policy error
```

After reviewing them, either keep them:

```powershell
.\Repair.ps1 --orphan-policy keep
```

or remove only the broken associations:

```powershell
.\Repair.ps1 --orphan-policy drop
```

Dropping an association does not delete the thread/session itself.

## Duplicate root projects

If two projects point at the same folder, both project definitions are kept.

The default leaves ambiguous, currently-unassigned chats alone:

```powershell
.\Repair.ps1 --duplicate-policy leave
```

To assign those chats to the oldest duplicate:

```powershell
.\Repair.ps1 --duplicate-policy oldest
```

To assign them to the newest duplicate:

```powershell
.\Repair.ps1 --duplicate-policy newest
```

Existing explicit assignments are never moved by the duplicate policy.

## Projects return but some chats stay under Recents

This can be expected. The tool only auto-assigns chats when their surviving `cwd` unambiguously matches a known project root and the row looks like a top-level user conversation.

Unmatched or ambiguous threads are intentionally left alone.

Run `Diagnose.ps1` and inspect:

- unmatched top-level threads;
- duplicate-root ambiguous threads;
- thread-source/source summaries.

## A known top-level source is being skipped

By default the utility recognizes modern `thread_source=user` rows and legacy `source=vscode` / `source=cli` rows.

If you know another exact `source` string is a normal top-level conversation source, include it explicitly:

```powershell
.\Repair.ps1 --include-source desktop
```

Do not broadly whitelist JSON subagent sources.

## The repair says success, but the UI still says “No projects”

Keep the backup and do not repeatedly recreate projects.

This can indicate a different Desktop-side catalog/rendering problem rather than missing global-state metadata. Capture a redacted diagnosis and compare with current upstream Codex issues.

## Roll back

Fully close Codex / ChatGPT Desktop and run:

```powershell
.\Restore-Backup.ps1 'C:\Users\YOU\CodexProjectRecoveryBackups\<backup-folder>'
```

The utility restores the global-state JSON only. It never changed SQLite.
