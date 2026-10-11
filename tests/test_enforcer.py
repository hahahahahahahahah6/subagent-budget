"""Tests for transcript scanning, sync, aggregation, and rbg import."""

import json
import os
import tempfile
import unittest

from subagent_budget import enforcer, store


def _transcript_line(entry):
    return json.dumps(entry)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ[store.ENV_HOME] = self.tmp.name
        self.addCleanup(os.environ.pop, store.ENV_HOME, None)

    def make_transcript(self, name, entries):
        proj = os.path.join(self.tmp.name, "projects", "proj")
        os.makedirs(proj, exist_ok=True)
        path = os.path.join(proj, name)
        with open(path, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(_transcript_line(e) + "\n")
        return path

    def task_entry(self, tool_id, description, subagent_type, session="sess-1"):
        return {
            "type": "assistant",
            "sessionId": session,
            "timestamp": "2026-10-10T00:00:00Z",
            "message": {
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Task",
                    "input": {"subagent_type": subagent_type,
                               "description": description,
                               "prompt": "do it"},
                }],
            },
        }

    def user_entry(self, text="ok"):
        return {"type": "user", "sessionId": "sess-1",
                "message": {"role": "user", "content": text}}


class FindInvocationsTest(Base):
    def test_finds_task_calls(self):
        path = self.make_transcript("s1.jsonl", [
            self.user_entry(),
            self.task_entry("tu1", "Find auth refresh", "Explore"),
            self.task_entry("tu2", "Write tests", "general-purpose"),
        ])
        invs = enforcer.find_invocations(path)
        self.assertEqual(len(invs), 2)
        self.assertEqual(invs[0]["agent"], "Find auth refresh")
        self.assertEqual(invs[0]["agent_type"], "Explore")
        self.assertEqual(invs[0]["tool_use_id"], "tu1")

    def test_ignores_non_task_tool_use(self):
        path = self.make_transcript("s2.jsonl", [{
            "type": "assistant", "sessionId": "s",
            "message": {"role": "assistant", "content": [{
                "type": "tool_use", "id": "r1", "name": "Read",
                "input": {"file_path": "/x"}}]},
        }])
        self.assertEqual(enforcer.find_invocations(path), [])

    def test_bad_lines_skipped(self):
        proj = os.path.join(self.tmp.name, "projects", "proj")
        os.makedirs(proj, exist_ok=True)
        path = os.path.join(proj, "s3.jsonl")
        with open(path, "w") as fh:
            fh.write("not json at all\n")
            fh.write(_transcript_line(self.task_entry("tu9", "d", "t")) + "\n")
        self.assertEqual(len(enforcer.find_invocations(path)), 1)

    def test_missing_file_returns_empty(self):
        self.assertEqual(enforcer.find_invocations("/nope/x.jsonl"), [])


class ScanProjectsTest(Base):
    def test_scans_all_sessions(self):
        self.make_transcript("a.jsonl", [self.task_entry("t1", "d1", "Explore")])
        self.make_transcript("b.jsonl", [self.task_entry("t2", "d2", "Plan")])
        invs = enforcer.scan_projects(os.path.join(self.tmp.name, "projects"))
        self.assertEqual(len(invs), 2)

    def test_missing_projects_dir(self):
        self.assertEqual(
            enforcer.scan_projects(os.path.join(self.tmp.name, "nope")), [])


class SyncTest(Base):
    def test_sync_records_invocations(self):
        cfg = store.load_config()
        cfg["projects_dir"] = os.path.join(self.tmp.name, "projects")
        self.make_transcript("a.jsonl", [self.task_entry("t1", "d1", "Explore")])
        new = enforcer.sync_new_invocations(cfg, [])
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["agent"], "d1")
        self.assertEqual(new[0]["tokens"], cfg["sync_estimate_tokens"])
        self.assertTrue(new[0]["estimated"])
        self.assertGreater(new[0]["cost_usd"], 0)

    def test_sync_is_idempotent(self):
        cfg = store.load_config()
        cfg["projects_dir"] = os.path.join(self.tmp.name, "projects")
        self.make_transcript("a.jsonl", [self.task_entry("t1", "d1", "Explore")])
        first = enforcer.sync_new_invocations(cfg, [])
        second = enforcer.sync_new_invocations(cfg, first)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])


class AggregateTest(Base):
    def test_per_agent_totals_and_over_flag(self):
        cfg = store.load_config()
        cfg["budgets"] = [{"pattern": "Explore*", "max_tokens": 100}]
        records = [
            {"agent": "Explore a", "agent_type": "Explore", "tokens": 60,
             "cost_usd": 0.5},
            {"agent": "Explore a", "agent_type": "Explore", "tokens": 60,
             "cost_usd": 0.5},
            {"agent": "other", "tokens": 10, "cost_usd": 0.1},
        ]
        rows = enforcer.aggregate(records, cfg)
        by_name = {r["agent"]: r for r in rows}
        self.assertEqual(by_name["Explore a"]["runs"], 2)
        self.assertEqual(by_name["Explore a"]["tokens"], 120)
        self.assertTrue(by_name["Explore a"]["over"])
        self.assertFalse(by_name["other"]["over"])
        self.assertEqual(by_name["Explore a"]["pattern"], "Explore*")
        self.assertIsNone(by_name["other"]["pattern"])  # default


class ImportRbgTest(Base):
    def test_converts_rbg_ledger(self):
        rbg = os.path.join(self.tmp.name, "rbg.jsonl")
        with open(rbg, "w") as fh:
            fh.write(json.dumps({
                "ts": "2026-10-09T00:00:00+00:00",
                "session_key": "day", "session_id": "s1",
                "kind": "invocation", "cost_usd": 2.5}) + "\n")
        converted = enforcer.import_rbg_records(rbg)
        self.assertEqual(len(converted), 1)
        self.assertEqual(converted[0]["agent"], "rbg:day")
        self.assertEqual(converted[0]["cost_usd"], 2.5)
        self.assertEqual(converted[0]["kind"], "rbg_import")

    def test_skips_costless_lines(self):
        rbg = os.path.join(self.tmp.name, "rbg2.jsonl")
        with open(rbg, "w") as fh:
            fh.write(json.dumps({"kind": "note"}) + "\n")
        self.assertEqual(enforcer.import_rbg_records(rbg), [])


if __name__ == "__main__":
    unittest.main()
