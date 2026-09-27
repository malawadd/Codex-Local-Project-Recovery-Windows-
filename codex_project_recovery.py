#!/usr/bin/env python3
"""
Codex Local Project Recovery (Windows)

Community recovery utility for a Codex Desktop failure mode where local projects
vanish from the sidebar while durable SQLite project/thread data still exists.

This tool intentionally never writes to Codex SQLite. Repair only rebuilds the
Desktop global-state JSON catalog/associations after creating backups.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import ntpath
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TOOL_NAME = "Codex Local Project Recovery"
TOOL_VERSION = "1.0.0"
DEFAULT_TOP_LEVEL_SOURCES = {"vscode", "cli"}
KNOWN_ASSIGNMENT_OBJECT_KEYS = {
    "projectKind",
    "projectId",
    "cwd",
    "pendingCoreUpdate",
}


class RecoveryError(RuntimeError):
    pass


class Reporter:
    def __init__(self, redact: bool = False, home: Optional[Path] = None):
        self.lines: List[str] = []
        self.redact = redact
        self.home = home or Path.home()

    def _redact(self, value: Any) -> str:
        text = str(value)
        if not self.redact:
            return text
        home_text = str(self.home)
        if home_text:
            text = text.replace(home_text, "%USERPROFILE%")
            text = text.replace(home_text.lower(), "%USERPROFILE%")
        return text

    def say(self, value: Any = "") -> None:
        text = self._redact(value)
        print(text)
        self.lines.append(text)

    def section(self, title: str) -> None:
        self.say()
        self.say("=" * 72)
        self.say(title)
        self.say("=" * 72)

    def write_report(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def norm_windows_path(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = str(value)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def resolve_codex_home(raw: Optional[str]) -> Tuple[Path, str]:
    if raw:
        return Path(raw).expanduser(), "command line"
    env = os.environ.get("CODEX_HOME")
    if env:
        return Path(env).expanduser(), "CODEX_HOME environment variable"
    return Path.home() / ".codex", "default %USERPROFILE%\\.codex"


def find_state_db(codex_home: Path, raw: Optional[str]) -> Path:
    if raw:
        return Path(raw).expanduser()
    preferred = codex_home / "state_5.sqlite"
    if preferred.exists():
        return preferred
    candidates: List[Tuple[int, Path]] = []
    for p in codex_home.glob("state_*.sqlite"):
        stem = p.stem
        try:
            number = int(stem.split("_", 1)[1])
        except Exception:
            continue
        candidates.append((number, p))
    if not candidates:
        raise RecoveryError(
            f"No state_*.sqlite database was found under {codex_home}."
        )
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def state_candidates(codex_home: Path) -> List[Path]:
    main = codex_home / ".codex-global-state.json"
    bak = codex_home / ".codex-global-state.json.bak"
    tmps = sorted(
        codex_home.glob(".codex-global-state.json.tmp-*"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    result: List[Path] = []
    for p in [main, bak, *tmps]:
        if p.exists() and p not in result:
            result.append(p)
    return result


def load_best_state(codex_home: Path) -> Tuple[Dict[str, Any], Path, List[Tuple[Path, str]]]:
    candidates = state_candidates(codex_home)
    if not candidates:
        raise RecoveryError(
            f"No .codex-global-state.json, .bak, or temp snapshot was found under {codex_home}."
        )
    failures: List[Tuple[Path, str]] = []
    for path in candidates:
        try:
            state = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(state, dict):
                raise ValueError("top-level JSON value is not an object")
            return state, path, failures
        except Exception as e:
            failures.append((path, str(e)))
    detail = "\n".join(f"  {p}: {e}" for p, e in failures)
    raise RecoveryError(
        "All available global-state JSON candidates are unreadable.\n" + detail
    )


def table_columns(con: sqlite3.Connection, table: str) -> List[str]:
    return [row[1] for row in con.execute(f'PRAGMA table_info("{table}")')]


def sqlite_tables(con: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def running_desktop_processes() -> List[str]:
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=15,
        )
    except Exception:
        return []
    names: List[str] = []
    try:
        rows = csv.reader(result.stdout.splitlines())
        for row in rows:
            if not row:
                continue
            name = row[0].strip().lower()
            if name in {"chatgpt.exe", "codex.exe"}:
                names.append(row[0].strip())
    except Exception:
        low = result.stdout.lower()
        for name in ("ChatGPT.exe", "Codex.exe"):
            if name.lower() in low:
                names.append(name)
    return sorted(set(names), key=str.lower)


def inspect_catalog(
    con: sqlite3.Connection,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    tables = sqlite_tables(con)
    required = {"projects", "project_roots", "project_idempotency_keys", "threads"}
    missing = required - tables
    if missing:
        raise RecoveryError(
            "Unsupported SQLite schema. Missing required table(s): "
            + ", ".join(sorted(missing))
        )

    project_cols = set(table_columns(con, "projects"))
    root_cols = set(table_columns(con, "project_roots"))
    key_cols = set(table_columns(con, "project_idempotency_keys"))
    thread_cols = set(table_columns(con, "threads"))

    for label, cols, needed in [
        ("projects", project_cols, {"id", "name", "position"}),
        ("project_roots", root_cols, {"project_id", "path"}),
        ("project_idempotency_keys", key_cols, {"project_id", "key"}),
        ("threads", thread_cols, {"id", "cwd"}),
    ]:
        absent = needed - cols
        if absent:
            raise RecoveryError(
                f"Unsupported {label} schema. Missing column(s): {', '.join(sorted(absent))}"
            )

    created_col = "created_at_ms" if "created_at_ms" in project_cols else None
    updated_col = "updated_at_ms" if "updated_at_ms" in project_cols else None

    project_select = ["id", "name", "position"]
    if created_col:
        project_select.append(created_col)
    if updated_col:
        project_select.append(updated_col)

    projects_raw = con.execute(
        "SELECT " + ", ".join(f'"{c}"' for c in project_select) + " FROM projects"
    ).fetchall()

    roots_raw = con.execute(
        'SELECT project_id, path' + (', position' if "position" in root_cols else '')
        + ' FROM project_roots'
    ).fetchall()

    keys_raw = con.execute(
        'SELECT project_id, "key" AS legacy_key FROM project_idempotency_keys'
    ).fetchall()

    roots_by_core: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for r in roots_raw:
        pos = int(r["position"]) if "position" in r.keys() and r["position"] is not None else 0
        roots_by_core[str(r["project_id"])].append((pos, str(r["path"])))
    for core in roots_by_core:
        roots_by_core[core].sort(key=lambda x: x[0])

    aliases_by_core: Dict[str, List[str]] = defaultdict(list)
    for r in keys_raw:
        aliases_by_core[str(r["project_id"])].append(str(r["legacy_key"]))

    current_local = state.get("local-projects") or {}
    if not isinstance(current_local, dict):
        raise RecoveryError("global-state 'local-projects' is not an object.")
    current_ids = set(str(k) for k in current_local.keys())

    projects: List[Dict[str, Any]] = []
    alias_ambiguities: List[Dict[str, Any]] = []
    missing_aliases: List[str] = []

    for p in projects_raw:
        core_id = str(p["id"])
        aliases = sorted(set(aliases_by_core.get(core_id, [])))
        current_matches = [a for a in aliases if a in current_ids]
        legacy_id: Optional[str]
        if len(aliases) == 1:
            legacy_id = aliases[0]
        elif len(current_matches) == 1:
            legacy_id = current_matches[0]
        elif not aliases:
            legacy_id = None
            missing_aliases.append(core_id)
        else:
            legacy_id = None
            alias_ambiguities.append(
                {
                    "core_id": core_id,
                    "name": p["name"],
                    "aliases": aliases,
                    "current_matches": current_matches,
                }
            )

        roots = [path for _, path in roots_by_core.get(core_id, [])]
        projects.append(
            {
                "core_id": core_id,
                "legacy_id": legacy_id,
                "aliases": aliases,
                "name": str(p["name"] or ""),
                "position": int(p["position"] or 0),
                "created_at_ms": int(p[created_col]) if created_col and p[created_col] is not None else None,
                "updated_at_ms": int(p[updated_col]) if updated_col and p[updated_col] is not None else None,
                "roots": roots,
            }
        )

    projects.sort(
        key=lambda p: (
            p["position"],
            p["created_at_ms"] if p["created_at_ms"] is not None else 0,
            p["name"].lower(),
        )
    )

    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    thread_count = con.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    db_project_assigned = None
    if "project_id" in thread_cols:
        db_project_assigned = con.execute(
            "SELECT COUNT(*) FROM threads WHERE project_id IS NOT NULL AND TRIM(project_id) != ''"
        ).fetchone()[0]

    return {
        "integrity": integrity,
        "tables": tables,
        "project_cols": project_cols,
        "thread_cols": thread_cols,
        "projects": projects,
        "alias_ambiguities": alias_ambiguities,
        "missing_aliases": missing_aliases,
        "thread_count": int(thread_count),
        "db_project_assigned": int(db_project_assigned) if db_project_assigned is not None else None,
        "root_count": len(roots_raw),
    }


def inspect_assignment_schema(state: Dict[str, Any]) -> Dict[str, Any]:
    raw = state.get("thread-project-assignments") or {}
    if not isinstance(raw, dict):
        raise RecoveryError("global-state 'thread-project-assignments' is not an object/null.")

    styles: set[str] = set()
    object_key_sets: set[Tuple[str, ...]] = set()
    unknown_keys: set[str] = set()

    for value in raw.values():
        if isinstance(value, str):
            styles.add("string")
        elif isinstance(value, dict):
            styles.add("object")
            keys = tuple(sorted(str(k) for k in value.keys()))
            object_key_sets.add(keys)
            unknown_keys.update(set(keys) - KNOWN_ASSIGNMENT_OBJECT_KEYS)
            if "projectId" not in value:
                unknown_keys.add("<object-without-projectId>")
        else:
            styles.add("unsupported")

    if not styles:
        style = "string"
    elif styles == {"string"}:
        style = "string"
    elif styles == {"object"}:
        style = "object"
    else:
        style = "mixed/unsupported"

    return {
        "raw": raw,
        "style": style,
        "object_key_sets": object_key_sets,
        "unknown_keys": unknown_keys,
    }


def assignment_project(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        pid = value.get("projectId")
        return str(pid) if pid is not None else None
    return None


def choose_duplicate_project(projects: Sequence[Dict[str, Any]], policy: str) -> Optional[Dict[str, Any]]:
    if len(projects) == 1:
        return projects[0]
    if policy == "leave":
        return None

    def age_key(p: Dict[str, Any]) -> Tuple[int, int, str]:
        timestamp = p["created_at_ms"]
        if timestamp is None:
            timestamp = 0
        return (int(timestamp), int(p["position"]), str(p["legacy_id"]))

    if policy == "oldest":
        return sorted(projects, key=age_key)[0]
    if policy == "newest":
        return sorted(projects, key=age_key)[-1]
    raise RecoveryError(f"Unknown duplicate policy: {policy}")


def fetch_threads(con: sqlite3.Connection, thread_cols: set[str]) -> List[Dict[str, Any]]:
    select_parts = ['"id"', '"cwd"']
    select_parts.append('"archived"' if "archived" in thread_cols else '0 AS archived')
    select_parts.append('"thread_source"' if "thread_source" in thread_cols else 'NULL AS thread_source')
    select_parts.append('"source"' if "source" in thread_cols else 'NULL AS source')
    rows = con.execute(
        "SELECT " + ", ".join(select_parts) + " FROM threads"
    ).fetchall()
    result = []
    for r in rows:
        result.append(
            {
                "id": str(r["id"]),
                "cwd": str(r["cwd"]) if r["cwd"] is not None else None,
                "archived": bool(r["archived"]),
                "thread_source": str(r["thread_source"]) if r["thread_source"] is not None else None,
                "source": str(r["source"]) if r["source"] is not None else None,
            }
        )
    return result


def is_candidate_top_level_thread(thread: Dict[str, Any], allowed_sources: set[str]) -> bool:
    thread_source = thread.get("thread_source")
    source = thread.get("source")
    if thread_source == "user":
        return True
    if thread_source is None and source in allowed_sources:
        return True
    return False


def build_plan(
    con: sqlite3.Connection,
    state: Dict[str, Any],
    catalog: Dict[str, Any],
    duplicate_policy: str,
    orphan_policy: str,
    include_sources: Iterable[str],
) -> Dict[str, Any]:
    projects = catalog["projects"]
    restorable = [p for p in projects if p["legacy_id"]]

    if catalog["alias_ambiguities"]:
        raise RecoveryError(
            "One or more SQLite projects have multiple possible legacy/Desktop IDs and cannot be chosen safely. "
            "Run diagnose and resolve that condition before repair."
        )
    if catalog["missing_aliases"]:
        raise RecoveryError(
            "One or more SQLite projects have no project_idempotency_keys mapping. "
            "This version of the utility will not invent Desktop project IDs."
        )
    if not restorable:
        raise RecoveryError("SQLite contains no restorable mapped projects.")

    assignment_info = inspect_assignment_schema(state)
    if assignment_info["style"] == "mixed/unsupported":
        raise RecoveryError(
            "Existing thread-project-assignments use mixed/unsupported value formats."
        )
    if assignment_info["unknown_keys"]:
        raise RecoveryError(
            "Object-style thread assignments contain unknown fields: "
            + ", ".join(sorted(assignment_info["unknown_keys"]))
            + ". Refusing to guess how to create new entries."
        )

    project_by_id = {str(p["legacy_id"]): p for p in restorable}
    known_ids = set(project_by_id)
    raw_existing = assignment_info["raw"]
    existing_known: Dict[str, Any] = {}
    existing_orphans: Dict[str, Any] = {}
    existing_pid: Dict[str, str] = {}

    for tid, value in raw_existing.items():
        tid = str(tid)
        pid = assignment_project(value)
        if pid in known_ids:
            existing_known[tid] = value
            existing_pid[tid] = str(pid)
        else:
            existing_orphans[tid] = value

    if existing_orphans and orphan_policy == "error":
        raise RecoveryError(
            f"Found {len(existing_orphans)} existing thread assignment(s) whose project ID is not present in the SQLite project mapping. "
            "Use --orphan-policy keep or --orphan-policy drop only after reviewing the diagnosis."
        )

    final_existing: Dict[str, Any] = dict(existing_known)
    if orphan_policy == "keep":
        final_existing.update(existing_orphans)

    root_entries: List[Tuple[str, Dict[str, Any], str]] = []
    duplicate_root_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in restorable:
        for root in p["roots"]:
            normalized = norm_windows_path(root)
            if normalized:
                root_entries.append((normalized, p, root))
                duplicate_root_map[normalized].append(p)
    root_entries.sort(key=lambda x: len(x[0]), reverse=True)

    duplicate_roots = {
        root: rows
        for root, rows in duplicate_root_map.items()
        if len({str(p["legacy_id"]) for p in rows}) > 1
    }

    threads = fetch_threads(con, catalog["thread_cols"])
    thread_by_id = {t["id"]: t for t in threads}
    allowed_sources = set(DEFAULT_TOP_LEVEL_SOURCES)
    allowed_sources.update(str(s) for s in include_sources)

    planned_pid: Dict[str, str] = dict(existing_pid)
    planned_root: Dict[str, str] = {}
    new_assignment_targets: Dict[str, Tuple[str, str]] = {}
    unmatched: List[str] = []
    ambiguous: List[Dict[str, Any]] = []
    skipped_internal = 0
    no_cwd = 0

    for thread in threads:
        tid = thread["id"]
        if tid in planned_pid:
            continue
        if not is_candidate_top_level_thread(thread, allowed_sources):
            skipped_internal += 1
            continue
        cwd = norm_windows_path(thread["cwd"])
        if not cwd:
            no_cwd += 1
            continue

        matches: List[Tuple[str, Dict[str, Any], str]] = []
        best_len: Optional[int] = None
        for normalized_root, project, original_root in root_entries:
            if cwd == normalized_root or cwd.startswith(normalized_root + "\\"):
                root_len = len(normalized_root)
                if best_len is None:
                    best_len = root_len
                if root_len < best_len:
                    break
                matches.append((normalized_root, project, original_root))

        if not matches:
            unmatched.append(tid)
            continue

        # De-duplicate project matches while preserving the root that matched.
        by_project: Dict[str, Tuple[Dict[str, Any], str]] = {}
        for _, p, original_root in matches:
            by_project[str(p["legacy_id"])] = (p, original_root)
        candidate_projects = [item[0] for item in by_project.values()]

        chosen = choose_duplicate_project(candidate_projects, duplicate_policy)
        if chosen is None:
            ambiguous.append(
                {
                    "thread_id": tid,
                    "cwd": thread["cwd"],
                    "projects": [
                        {
                            "id": str(p["legacy_id"]),
                            "name": p["name"],
                            "created_at_ms": p["created_at_ms"],
                        }
                        for p in candidate_projects
                    ],
                }
            )
            continue

        pid = str(chosen["legacy_id"])
        matched_root = by_project[pid][1]
        planned_pid[tid] = pid
        planned_root[tid] = matched_root
        new_assignment_targets[tid] = (pid, matched_root)

    counts: Dict[str, Counter] = {pid: Counter() for pid in known_ids}
    for tid, pid in planned_pid.items():
        thread = thread_by_id.get(tid)
        if pid not in counts:
            continue
        if thread is None:
            counts[pid]["missing_thread_row"] += 1
        elif thread["archived"]:
            counts[pid]["archived"] += 1
        else:
            counts[pid]["active"] += 1

    source_summary = Counter(
        (t["thread_source"], t["source"], t["archived"]) for t in threads
    )

    return {
        "projects": restorable,
        "project_by_id": project_by_id,
        "assignment_info": assignment_info,
        "existing_known": existing_known,
        "existing_orphans": existing_orphans,
        "final_existing": final_existing,
        "existing_pid": existing_pid,
        "planned_pid": planned_pid,
        "planned_root": planned_root,
        "new_assignment_targets": new_assignment_targets,
        "threads": threads,
        "thread_by_id": thread_by_id,
        "duplicate_roots": duplicate_roots,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
        "skipped_internal": skipped_internal,
        "no_cwd": no_cwd,
        "counts": counts,
        "allowed_sources": allowed_sources,
        "source_summary": source_summary,
        "duplicate_policy": duplicate_policy,
        "orphan_policy": orphan_policy,
    }


def make_new_assignment(
    assignment_info: Dict[str, Any],
    project_id: str,
    root: str,
) -> Any:
    style = assignment_info["style"]
    if style == "string":
        return project_id
    if style != "object":
        raise RecoveryError(f"Unsupported assignment style: {style}")

    # All object schemas were checked to use only known keys. Use the first
    # observed key set as the schema for new entries.
    key_sets = assignment_info["object_key_sets"]
    keys = set(next(iter(key_sets))) if key_sets else {"projectId"}
    result: Dict[str, Any] = {}
    if "projectKind" in keys:
        result["projectKind"] = "local"
    if "projectId" in keys:
        result["projectId"] = project_id
    else:
        raise RecoveryError("Object assignment schema has no projectId field.")
    if "cwd" in keys:
        result["cwd"] = root
    if "pendingCoreUpdate" in keys:
        result["pendingCoreUpdate"] = False
    return result


def build_repaired_state(state: Dict[str, Any], plan: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    now_ms = int(time.time() * 1000)
    repaired = json.loads(json.dumps(state))  # deep copy using JSON-compatible structure
    current_local = repaired.get("local-projects") or {}
    if not isinstance(current_local, dict):
        current_local = {}

    local_projects: Dict[str, Any] = {}
    timestamp_fallbacks = 0
    for p in plan["projects"]:
        pid = str(p["legacy_id"])
        existing_obj = current_local.get(pid)
        obj = dict(existing_obj) if isinstance(existing_obj, dict) else {}
        roots = list(p["roots"])
        name = p["name"]
        if not name:
            name = ntpath.basename(roots[0]) if roots else pid
        created = p["created_at_ms"]
        updated = p["updated_at_ms"]
        if created is None:
            created = updated if updated is not None else now_ms
            timestamp_fallbacks += 1
        if updated is None:
            updated = created
            timestamp_fallbacks += 1
        obj.update(
            {
                "id": pid,
                "name": name,
                "rootPaths": roots,
                "createdAt": int(created),
                "updatedAt": int(updated),
            }
        )
        local_projects[pid] = obj

    project_order = [str(p["legacy_id"]) for p in plan["projects"]]

    final_assignments: Dict[str, Any] = dict(plan["final_existing"])
    for tid, (pid, root) in plan["new_assignment_targets"].items():
        final_assignments[tid] = make_new_assignment(
            plan["assignment_info"], pid, root
        )

    hints_raw = repaired.get("thread-workspace-root-hints") or {}
    hints = dict(hints_raw) if isinstance(hints_raw, dict) else {}
    for tid, (pid, root) in plan["new_assignment_targets"].items():
        hints[tid] = root
    # For existing known assignments, add a hint only if one is missing and we
    # can identify a matching project root from the thread cwd.
    for tid, pid in plan["existing_pid"].items():
        if tid in hints:
            continue
        project = plan["project_by_id"].get(pid)
        thread = plan["thread_by_id"].get(tid)
        if not project or not thread:
            continue
        cwd = norm_windows_path(thread["cwd"])
        if not cwd:
            continue
        matching_roots = [
            root for root in project["roots"]
            if (lambda nr: nr and (cwd == nr or cwd.startswith(nr + "\\")))(norm_windows_path(root))
        ]
        if matching_roots:
            hints[tid] = max(matching_roots, key=lambda r: len(norm_windows_path(r) or ""))

    repaired["local-projects"] = local_projects
    repaired["project-order"] = project_order
    repaired["thread-project-assignments"] = final_assignments
    repaired["thread-workspace-root-hints"] = hints

    projectless_before = None
    projectless_after = None
    if "projectless-thread-ids" in repaired:
        raw = repaired.get("projectless-thread-ids") or []
        if isinstance(raw, list):
            projectless_before = len(raw)
            assigned = set(final_assignments)
            raw = [str(tid) for tid in raw if str(tid) not in assigned]
            repaired["projectless-thread-ids"] = raw
            projectless_after = len(raw)

    selected = repaired.get("selected-project")
    if not isinstance(selected, str) or selected not in local_projects:
        if project_order:
            repaired["selected-project"] = project_order[0]

    metrics = {
        "project_count": len(local_projects),
        "assignment_count": len(final_assignments),
        "new_assignment_count": len(plan["new_assignment_targets"]),
        "projectless_before": projectless_before,
        "projectless_after": projectless_after,
        "timestamp_fallbacks": timestamp_fallbacks,
    }
    return repaired, metrics


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".recovery-tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    # Ensure valid JSON before replacement.
    json.loads(temp.read_text(encoding="utf-8"))
    os.replace(temp, path)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".recovery-tmp")
    with temp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def create_backup(codex_home: Path, db_path: Path, reason: str) -> Path:
    root = Path.home() / "CodexProjectRecoveryBackups"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = root / f"{stamp}-{reason}"
    suffix = 1
    while backup.exists():
        backup = root / f"{stamp}-{reason}-{suffix}"
        suffix += 1
    backup.mkdir(parents=True, exist_ok=False)

    files: List[Path] = []
    files.extend(state_candidates(codex_home))
    for p in [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]:
        if p.exists() and p not in files:
            files.append(p)

    manifest: Dict[str, Any] = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "created_at": datetime.now().astimezone().isoformat(),
        "codex_home": str(codex_home),
        "reason": reason,
        "files": [],
    }

    for src in files:
        dest = backup / src.name
        shutil.copy2(src, dest)
        manifest["files"].append(
            {
                "name": src.name,
                "source": str(src),
                "size": dest.stat().st_size,
                "sha256": sha256_file(dest),
            }
        )

    (backup / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return backup


def render_diagnosis(
    reporter: Reporter,
    codex_home: Path,
    codex_home_source: str,
    db_path: Path,
    state: Dict[str, Any],
    state_source: Path,
    state_failures: List[Tuple[Path, str]],
    catalog: Dict[str, Any],
    plan: Optional[Dict[str, Any]],
    plan_error: Optional[str],
) -> None:
    reporter.say(f"{TOOL_NAME} v{TOOL_VERSION}")
    reporter.section("LOCATION")
    reporter.say(f"CODEX_HOME: {codex_home}")
    reporter.say(f"Resolved from: {codex_home_source}")
    reporter.say(f"State source used: {state_source}")
    reporter.say(f"SQLite database: {db_path}")
    if state_failures:
        reporter.say("Earlier state candidate(s) failed to parse:")
        for p, err in state_failures:
            reporter.say(f"  - {p}: {err}")

    reporter.section("DATABASE")
    reporter.say(f"Integrity: {catalog['integrity']}")
    reporter.say(f"SQLite projects: {len(catalog['projects'])}")
    reporter.say(f"SQLite project roots: {catalog['root_count']}")
    reporter.say(f"SQLite threads: {catalog['thread_count']}")
    if catalog["db_project_assigned"] is not None:
        reporter.say(f"SQLite threads with non-null project_id: {catalog['db_project_assigned']}")

    reporter.section("CURRENT DESKTOP GLOBAL STATE")
    local = state.get("local-projects") or {}
    order = state.get("project-order") or []
    assignments = state.get("thread-project-assignments") or {}
    reporter.say(f"local-projects entries: {len(local) if isinstance(local, dict) else 'INVALID TYPE'}")
    reporter.say(f"project-order entries: {len(order) if isinstance(order, list) else 'INVALID TYPE'}")
    reporter.say(f"thread-project-assignments entries: {len(assignments) if isinstance(assignments, dict) else 'INVALID TYPE'}")

    reporter.section("SQLITE PROJECT INVENTORY")
    for p in catalog["projects"]:
        reporter.say(
            f"{p['position']:>3}  {p['name'] or '<unnamed>'} | desktop_id={p['legacy_id'] or '<ambiguous/missing>'} | core_id={p['core_id']}"
        )
        for root in p["roots"]:
            reporter.say(f"       root: {root}")

    if catalog["alias_ambiguities"] or catalog["missing_aliases"]:
        reporter.section("PROJECT ID MAPPING WARNINGS")
        for item in catalog["alias_ambiguities"]:
            reporter.say(
                f"Ambiguous legacy IDs for {item['name']} ({item['core_id']}): {', '.join(item['aliases'])}"
            )
        for core_id in catalog["missing_aliases"]:
            reporter.say(f"Missing legacy/Desktop ID mapping for core project {core_id}")

    # Duplicate roots from catalog even if plan could not be built.
    root_map: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in catalog["projects"]:
        for root in p["roots"]:
            nr = norm_windows_path(root)
            if nr:
                root_map[nr].append(p)
    dupes = {
        root: rows for root, rows in root_map.items()
        if len({p["legacy_id"] for p in rows}) > 1
    }
    reporter.section("DUPLICATE PROJECT ROOTS")
    if not dupes:
        reporter.say("None detected.")
    else:
        for root, rows in dupes.items():
            reporter.say(f"Root: {root}")
            for p in rows:
                reporter.say(
                    f"  - {p['name']} | desktop_id={p['legacy_id']} | created_at_ms={p['created_at_ms']}"
                )

    reporter.section("RECOVERY PLAN (READ-ONLY)")
    if plan_error:
        reporter.say("A safe plan could not be built:")
        reporter.say(plan_error)
    elif plan is not None:
        reporter.say(f"Projects that would be restored: {len(plan['projects'])}")
        reporter.say(f"Existing known assignments preserved: {len(plan['existing_known'])}")
        reporter.say(f"Existing orphan assignments: {len(plan['existing_orphans'])}")
        reporter.say(f"New assignments inferred from cwd: {len(plan['new_assignment_targets'])}")
        reporter.say(f"Final known project assignments: {len(plan['planned_pid'])}")
        reporter.say(f"Ambiguous duplicate-root threads left unassigned: {len(plan['ambiguous'])}")
        reporter.say(f"Unmatched top-level threads left in Recents: {len(plan['unmatched'])}")
        reporter.say(f"Internal/non-user rows skipped: {plan['skipped_internal']}")
        reporter.say(f"Allowed legacy top-level sources: {', '.join(sorted(plan['allowed_sources']))}")

        reporter.say()
        reporter.say("Per-project planned assignment counts:")
        for p in plan["projects"]:
            pid = str(p["legacy_id"])
            c = plan["counts"].get(pid, Counter())
            reporter.say(
                f"  {p['position']:>3}  {p['name'] or '<unnamed>':<45} active={c['active']:<4} archived={c['archived']:<4} missing-row={c['missing_thread_row']:<3}"
            )

        if plan["ambiguous"]:
            reporter.say()
            reporter.say("Ambiguous duplicate-root examples (first 10):")
            for item in plan["ambiguous"][:10]:
                names = ", ".join(
                    f"{p['name']} [{p['id']}]" for p in item["projects"]
                )
                reporter.say(f"  cwd={item['cwd']} -> {names}")

    reporter.section("RESULT")
    reporter.say("Diagnosis only. No files were modified.")


def command_diagnose(args: argparse.Namespace) -> int:
    codex_home, source = resolve_codex_home(args.codex_home)
    reporter = Reporter(redact=args.redact)
    try:
        db_path = find_state_db(codex_home, args.db)
        state, state_source, state_failures = load_best_state(codex_home)
        with connect_readonly(db_path) as con:
            catalog = inspect_catalog(con, state)
            plan = None
            plan_error = None
            try:
                plan = build_plan(
                    con,
                    state,
                    catalog,
                    duplicate_policy=args.duplicate_policy,
                    orphan_policy=args.orphan_policy,
                    include_sources=args.include_source or [],
                )
            except RecoveryError as e:
                plan_error = str(e)
            render_diagnosis(
                reporter,
                codex_home,
                source,
                db_path,
                state,
                state_source,
                state_failures,
                catalog,
                plan,
                plan_error,
            )
    except RecoveryError as e:
        reporter.section("ERROR")
        reporter.say(str(e))
        if args.report:
            reporter.write_report(Path(args.report).expanduser())
        return 2

    if args.report:
        report_path = Path(args.report).expanduser()
        reporter.write_report(report_path)
        reporter.say(f"Report written to: {report_path}")
    return 0


def command_repair(args: argparse.Namespace) -> int:
    codex_home, source = resolve_codex_home(args.codex_home)
    reporter = Reporter(redact=False)
    try:
        db_path = find_state_db(codex_home, args.db)
        state, state_source, state_failures = load_best_state(codex_home)
        with connect_readonly(db_path) as con:
            catalog = inspect_catalog(con, state)
            if catalog["integrity"] != "ok":
                raise RecoveryError(
                    f"SQLite integrity_check returned {catalog['integrity']!r}; repair was not attempted."
                )
            plan = build_plan(
                con,
                state,
                catalog,
                duplicate_policy=args.duplicate_policy,
                orphan_policy=args.orphan_policy,
                include_sources=args.include_source or [],
            )
            repaired, metrics = build_repaired_state(state, plan)

        reporter.say(f"{TOOL_NAME} v{TOOL_VERSION}")
        reporter.section("REPAIR PLAN")
        reporter.say(f"CODEX_HOME: {codex_home} ({source})")
        reporter.say(f"State template: {state_source}")
        if state_failures:
            reporter.say("Note: the primary/earlier state file was unreadable; a valid fallback snapshot is being used as the template.")
        reporter.say(f"SQLite database (read-only): {db_path}")
        reporter.say(f"Projects to write to sidebar catalog: {metrics['project_count']}")
        reporter.say(f"Existing known assignments preserved: {len(plan['existing_known'])}")
        reporter.say(f"New assignments inferred: {metrics['new_assignment_count']}")
        reporter.say(f"Final assignment entries: {metrics['assignment_count']}")
        reporter.say(f"Duplicate-root ambiguous threads left unassigned: {len(plan['ambiguous'])}")
        reporter.say(f"Unmatched top-level threads left in Recents: {len(plan['unmatched'])}")
        reporter.say(f"Duplicate-root policy: {args.duplicate_policy}")
        reporter.say(f"Orphan-assignment policy: {args.orphan_policy}")
        if metrics["timestamp_fallbacks"]:
            reporter.say(f"Warning: {metrics['timestamp_fallbacks']} missing project timestamp value(s) required a numeric fallback.")

        running = running_desktop_processes()
        if running:
            raise RecoveryError(
                "Codex/ChatGPT Desktop still appears to be running: "
                + ", ".join(running)
                + ". Fully exit the app (including background ChatGPT.exe/Codex.exe processes) and rerun repair."
            )

        if args.dry_run:
            reporter.section("DRY RUN")
            reporter.say("No files were modified.")
            return 0

        if not args.yes:
            reporter.say()
            reporter.say("This will create a backup, then atomically replace .codex-global-state.json and its .bak copy.")
            reporter.say("SQLite, session files, and source repositories will NOT be modified.")
            answer = input("Type REPAIR to continue: ").strip()
            if answer != "REPAIR":
                reporter.say("Cancelled. No files were modified.")
                return 1

        main_state = codex_home / ".codex-global-state.json"
        before_main = main_state.read_bytes() if main_state.exists() else b""
        before_hash = sha256_bytes(before_main)

        backup = create_backup(codex_home, db_path, "before-repair")
        reporter.say(f"Backup created: {backup}")

        # Race check after backup creation.
        current_main = main_state.read_bytes() if main_state.exists() else b""
        if sha256_bytes(current_main) != before_hash:
            raise RecoveryError(
                "The primary global-state file changed while the repair was being prepared. "
                "The app may still be running. No repaired state was installed."
            )

        output = json.dumps(repaired, indent=2, ensure_ascii=False) + "\n"
        # Validate our serialization and write backup/shadow first, primary last.
        check = json.loads(output)
        if len(check.get("local-projects") or {}) != metrics["project_count"]:
            raise RecoveryError("Internal validation failed for local-projects count.")
        if len(check.get("thread-project-assignments") or {}) != metrics["assignment_count"]:
            raise RecoveryError("Internal validation failed for assignment count.")

        bak_state = codex_home / ".codex-global-state.json.bak"
        atomic_write_text(bak_state, output)
        atomic_write_text(main_state, output)

        # Post-write validation, including UTF-8 without BOM.
        for path in (main_state, bak_state):
            raw = path.read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                raise RecoveryError(f"Unexpected UTF-8 BOM after write: {path}")
            obj = json.loads(raw.decode("utf-8"))
            if len(obj.get("local-projects") or {}) != metrics["project_count"]:
                raise RecoveryError(f"Post-write project validation failed: {path}")
            if len(obj.get("thread-project-assignments") or {}) != metrics["assignment_count"]:
                raise RecoveryError(f"Post-write assignment validation failed: {path}")

        reporter.section("REPAIR COMPLETED SUCCESSFULLY")
        reporter.say(f"Projects in sidebar catalog: {metrics['project_count']}")
        reporter.say(f"Thread/project assignments: {metrics['assignment_count']}")
        reporter.say(f"Newly reconstructed assignments: {metrics['new_assignment_count']}")
        reporter.say(f"Unmatched top-level threads left untouched: {len(plan['unmatched'])}")
        reporter.say(f"Ambiguous duplicate-root threads left untouched: {len(plan['ambiguous'])}")
        if metrics["projectless_before"] is not None:
            reporter.say(
                f"projectless-thread-ids: {metrics['projectless_before']} -> {metrics['projectless_after']}"
            )
        reporter.say(f"Backup: {backup}")
        reporter.say("SQLite was not modified.")
        reporter.say("You can now reopen Codex/ChatGPT Desktop and check Projects.")
        return 0

    except RecoveryError as e:
        reporter.section("REPAIR ABORTED")
        reporter.say(str(e))
        reporter.say("No SQLite changes were made. If a backup path was printed above, keep it until you verify the app.")
        return 2


def command_restore(args: argparse.Namespace) -> int:
    codex_home, _ = resolve_codex_home(args.codex_home)
    backup = Path(args.backup).expanduser().resolve()
    reporter = Reporter()
    try:
        if not backup.is_dir():
            raise RecoveryError(f"Backup directory does not exist: {backup}")
        manifest_path = backup / "manifest.json"
        if not manifest_path.exists():
            raise RecoveryError("manifest.json was not found in that backup directory.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_main = backup / ".codex-global-state.json"
        source_bak = backup / ".codex-global-state.json.bak"
        if not source_main.exists():
            raise RecoveryError(
                "That backup does not contain .codex-global-state.json. Nothing to restore."
            )

        running = running_desktop_processes()
        if running:
            raise RecoveryError(
                "Codex/ChatGPT Desktop still appears to be running: "
                + ", ".join(running)
                + ". Fully exit it before restoring a backup."
            )

        reporter.say(f"Backup: {backup}")
        reporter.say(f"Restore destination: {codex_home}")
        reporter.say("Only the global-state JSON files will be restored. SQLite will not be rolled back.")
        if not args.yes:
            answer = input("Type RESTORE to continue: ").strip()
            if answer != "RESTORE":
                reporter.say("Cancelled.")
                return 1

        # Back up the current state before rollback.
        db_path = find_state_db(codex_home, args.db)
        pre_restore = create_backup(codex_home, db_path, "before-restore")
        reporter.say(f"Current state backed up first: {pre_restore}")

        atomic_write_bytes(
            codex_home / ".codex-global-state.json",
            source_main.read_bytes(),
        )
        if source_bak.exists():
            atomic_write_bytes(
                codex_home / ".codex-global-state.json.bak",
                source_bak.read_bytes(),
            )

        reporter.section("RESTORE COMPLETED")
        reporter.say("Global-state JSON restored from the selected backup.")
        reporter.say("SQLite was not modified.")
        return 0
    except (RecoveryError, json.JSONDecodeError) as e:
        reporter.section("RESTORE ABORTED")
        reporter.say(str(e))
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose and recover missing local Codex Desktop project sidebar entries "
            "from surviving SQLite project/root/thread metadata."
        )
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--codex-home",
            help="Override CODEX_HOME. Default: CODEX_HOME env var, then %%USERPROFILE%%\\.codex.",
        )
        p.add_argument(
            "--db",
            help="Override the SQLite database path. Default: state_5.sqlite, else highest state_N.sqlite.",
        )
        p.add_argument(
            "--duplicate-policy",
            choices=["leave", "oldest", "newest"],
            default="leave",
            help=(
                "For currently-unassigned threads whose cwd matches multiple projects with the same/deepest root: "
                "leave them unassigned (safest), or attach them to the oldest/newest duplicate project."
            ),
        )
        p.add_argument(
            "--orphan-policy",
            choices=["error", "keep", "drop"],
            default="error",
            help=(
                "How to handle existing assignments whose project ID is absent from SQLite mapping. "
                "Default 'error' refuses repair until reviewed."
            ),
        )
        p.add_argument(
            "--include-source",
            action="append",
            default=[],
            help=(
                "Treat an additional exact threads.source value as a legacy top-level user thread when thread_source is NULL. "
                "Can be repeated. Defaults already include vscode and cli."
            ),
        )

    d = sub.add_parser("diagnose", help="Read-only inspection and recovery plan.")
    common(d)
    d.add_argument("--redact", action="store_true", help="Redact the user-home prefix from printed paths.")
    d.add_argument("--report", help="Also write the diagnosis to a UTF-8 text file.")
    d.set_defaults(func=command_diagnose)

    r = sub.add_parser("repair", help="Back up and rebuild the Desktop project catalog/associations.")
    common(r)
    r.add_argument("--dry-run", action="store_true", help="Build and print the repair plan without writing files.")
    r.add_argument("--yes", action="store_true", help="Skip the interactive REPAIR confirmation.")
    r.set_defaults(func=command_repair)

    b = sub.add_parser("restore", help="Restore global-state JSON from a backup created by this utility.")
    b.add_argument("backup", help="Path to a CodexProjectRecoveryBackups/<timestamp>-... folder.")
    b.add_argument("--codex-home", help="Override CODEX_HOME.")
    b.add_argument("--db", help="Override SQLite path used only for the pre-restore safety backup.")
    b.add_argument("--yes", action="store_true", help="Skip the interactive RESTORE confirmation.")
    b.set_defaults(func=command_restore)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as e:
        # Do not dump a traceback by default: this tool is meant for end users.
        print()
        print("UNEXPECTED ERROR")
        print(str(e))
        print("No SQLite writes are performed by this utility.")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
