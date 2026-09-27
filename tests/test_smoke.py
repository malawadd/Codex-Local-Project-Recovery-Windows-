import json
import sqlite3
import sys
import tempfile
from pathlib import Path
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import codex_project_recovery as cpr


class RecoverySmokeTests(unittest.TestCase):
    def make_fixture(self, base: Path):
        db = base / "state_5.sqlite"
        con = sqlite3.connect(db)
        con.executescript(
            """
            CREATE TABLE projects (
                id TEXT PRIMARY KEY,
                name TEXT,
                metadata TEXT,
                position INTEGER,
                created_at_ms INTEGER,
                updated_at_ms INTEGER
            );
            CREATE TABLE project_roots (
                project_id TEXT,
                position INTEGER,
                path TEXT
            );
            CREATE TABLE project_idempotency_keys (
                project_id TEXT,
                key TEXT
            );
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                cwd TEXT,
                archived INTEGER,
                thread_source TEXT,
                source TEXT,
                project_id TEXT
            );
            """
        )
        projects = [
            ("core-a", "Alpha", 0, 1000, 1100, "legacy-a", r"D:\work\alpha"),
            ("core-b-old", "Beta", 1, 2000, 2100, "legacy-b-old", r"D:\work\beta"),
            ("core-b-new", "Beta", 2, 3000, 3100, "legacy-b-new", r"D:\work\beta"),
        ]
        for core, name, pos, created, updated, legacy, root in projects:
            con.execute(
                "INSERT INTO projects(id,name,metadata,position,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?)",
                (core, name, None, pos, created, updated),
            )
            con.execute(
                "INSERT INTO project_roots(project_id,position,path) VALUES(?,?,?)",
                (core, 0, root),
            )
            con.execute(
                "INSERT INTO project_idempotency_keys(project_id,key) VALUES(?,?)",
                (core, legacy),
            )

        rows = [
            ("t-alpha", r"\\?\D:\work\alpha", 0, "user", "vscode", None),
            ("t-beta-old", r"\\?\D:\work\beta", 0, "user", "vscode", None),
            ("t-beta-existing", r"\\?\D:\work\beta", 0, "user", "vscode", None),
            ("t-subagent", r"\\?\D:\work\alpha", 0, "subagent", '{"subagent":true}', None),
            ("t-unmatched", r"\\?\D:\other", 0, "user", "vscode", None),
        ]
        con.executemany(
            "INSERT INTO threads(id,cwd,archived,thread_source,source,project_id) VALUES(?,?,?,?,?,?)",
            rows,
        )
        con.commit()
        con.close()

        state = {
            "local-projects": {
                "legacy-b-new": {
                    "id": "legacy-b-new",
                    "name": "Beta",
                    "rootPaths": [r"D:\work\beta"],
                    "createdAt": 3000,
                    "updatedAt": 3100,
                }
            },
            "project-order": ["legacy-b-new"],
            "thread-project-assignments": {
                "t-beta-existing": "legacy-b-new"
            },
            "thread-workspace-root-hints": {},
            "projectless-thread-ids": [
                "t-alpha",
                "t-beta-old",
                "t-unmatched",
            ],
        }
        return db, state

    def test_duplicate_leave_preserves_existing_and_leaves_ambiguous(self):
        with tempfile.TemporaryDirectory() as td:
            db, state = self.make_fixture(Path(td))
            with cpr.connect_readonly(db) as con:
                catalog = cpr.inspect_catalog(con, state)
                plan = cpr.build_plan(
                    con,
                    state,
                    catalog,
                    duplicate_policy="leave",
                    orphan_policy="error",
                    include_sources=[],
                )
            self.assertEqual(len(plan["projects"]), 3)
            self.assertEqual(plan["existing_pid"]["t-beta-existing"], "legacy-b-new")
            self.assertIn("t-alpha", plan["new_assignment_targets"])
            self.assertNotIn("t-beta-old", plan["new_assignment_targets"])
            self.assertEqual(len(plan["ambiguous"]), 1)
            self.assertNotIn("t-subagent", plan["new_assignment_targets"])

    def test_duplicate_oldest_assigns_unassigned_to_old_but_keeps_existing_new(self):
        with tempfile.TemporaryDirectory() as td:
            db, state = self.make_fixture(Path(td))
            with cpr.connect_readonly(db) as con:
                catalog = cpr.inspect_catalog(con, state)
                plan = cpr.build_plan(
                    con,
                    state,
                    catalog,
                    duplicate_policy="oldest",
                    orphan_policy="error",
                    include_sources=[],
                )
                repaired, metrics = cpr.build_repaired_state(state, plan)
            self.assertEqual(len(repaired["local-projects"]), 3)
            self.assertEqual(
                repaired["thread-project-assignments"]["t-beta-existing"],
                "legacy-b-new",
            )
            self.assertEqual(
                repaired["thread-project-assignments"]["t-beta-old"],
                "legacy-b-old",
            )
            self.assertEqual(
                repaired["thread-project-assignments"]["t-alpha"],
                "legacy-a",
            )
            self.assertNotIn("t-subagent", repaired["thread-project-assignments"])
            self.assertNotIn("t-unmatched", repaired["thread-project-assignments"])
            self.assertEqual(metrics["project_count"], 3)
            self.assertEqual(metrics["assignment_count"], 3)

    def test_utf8_writer_has_no_bom(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            cpr.atomic_write_text(p, json.dumps({"x": 1}) + "\n")
            raw = p.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(json.loads(raw.decode("utf-8"))["x"], 1)


    def test_end_to_end_repair_writes_json_and_backup(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            codex_home = base / ".codex"
            codex_home.mkdir()
            db, state = self.make_fixture(codex_home)
            state_path = codex_home / ".codex-global-state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            args = Namespace(
                codex_home=str(codex_home),
                db=str(db),
                duplicate_policy="oldest",
                orphan_policy="error",
                include_source=[],
                dry_run=False,
                yes=True,
            )
            out = StringIO()
            with patch("pathlib.Path.home", return_value=base), redirect_stdout(out):
                rc = cpr.command_repair(args)
            self.assertEqual(rc, 0, out.getvalue())
            repaired = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(repaired["local-projects"]), 3)
            self.assertEqual(len(repaired["thread-project-assignments"]), 3)
            backups = list((base / "CodexProjectRecoveryBackups").glob("*-before-repair*"))
            self.assertEqual(len(backups), 1)
            self.assertTrue((backups[0] / "manifest.json").exists())



if __name__ == "__main__":
    unittest.main()
