# Changelog

## 1.0.0 — 2026-09-27

- Initial public package.
- Read-only diagnosis of project/root/thread metadata.
- Recovery of Desktop `local-projects`, `project-order`, thread assignments, and workspace-root hints.
- SQLite opened read-only; no SQLite writes.
- Timestamped external backups with SHA-256 manifest.
- UTF-8-without-BOM atomic JSON writes.
- Duplicate-root policies: `leave`, `oldest`, `newest`.
- Existing explicit duplicate-project assignments are preserved.
- Orphan-assignment safety policy.
- Custom `CODEX_HOME` and database override support.
- Restore helper and synthetic unit tests.
