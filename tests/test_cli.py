"""End-to-end CLI tests: init, budgets, record, report, check, hook."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from subagent_budget import cli, store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ[store.ENV_HOME] = self.tmp.name
        self.addCleanup(os.environ.pop, store.ENV_HOME, None)

    def run_cli(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        import sys
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(argv)
        finally:
            sys.stdin = old_stdin
        return code, out.getvalue(), err.getvalue()


class InitTest(Base):
    def test_init_writes_config(self):
        code, out, _ = self.run_cli(["init"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(store.config_path()))

    def test_init_refuses_without_force(self):
        self.run_cli(["init"])
        code, _, _ = self.run_cli(["init"])
        self.assertEqual(code, store.EXIT_USAGE)

    def test_init_custom_defaults(self):
        code, _, _ = self.run_cli(
            ["init", "--default-tokens", "10", "--default-usd", "0.5"])
        self.assertEqual(code, 0)
        cfg = store.load_config()
        self.assertEqual(cfg["default_budget"]["max_tokens"], 10)
        self.assertEqual(cfg["default_budget"]["max_usd"], 0.5)


class BudgetCommandsTest(Base):
    def test_set_and_list(self):
        self.run_cli(["init"])
        code, out, _ = self.run_cli(
            ["set-budget", "--pattern", "Explore*", "--tokens", "100",
             "--usd", "2"])
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli(["budgets", "--json"])
        data = json.loads(out)
        self.assertEqual(data["budgets"][0]["pattern"], "Explore*")

    def test_set_budget_updates_existing_pattern(self):
        self.run_cli(["init"])
        self.run_cli(["set-budget", "--pattern", "x*", "--tokens", "1"])
        self.run_cli(["set-budget", "--pattern", "x*", "--tokens", "2"])
        cfg = store.load_config()
        self.assertEqual(len(cfg["budgets"]), 1)
        self.assertEqual(cfg["budgets"][0]["max_tokens"], 2)


class RecordReportTest(Base):
    def test_record_and_report(self):
        self.run_cli(["init"])
        code, out, _ = self.run_cli(
            ["record", "--agent", "Explore auth", "--type", "Explore",
             "--tokens", "1000"])
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli(["report", "--json"])
        rows = json.loads(out)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tokens"], 1000)
        self.assertGreater(rows[0]["cost_usd"], 0)

    def test_report_empty_ledger(self):
        self.run_cli(["init"])
        code, out, _ = self.run_cli(["report"])
        self.assertEqual(code, 0)
        self.assertIn("empty", out)


class CheckTest(Base):
    def test_check_blocked_exit_code(self):
        self.run_cli(["init", "--default-tokens", "10", "--default-usd", "100"])
        self.run_cli(["record", "--agent", "a1", "--tokens", "50"])
        code, out, err = self.run_cli(["check", "--agent", "a1"])
        self.assertEqual(code, store.EXIT_BLOCKED)
        self.assertIn("BLOCKED", out)


class HookTest(Base):
    def hook_stdin(self, description="Explore auth", subagent_type="Explore"):
        return json.dumps({
            "session_id": "sess-1",
            "tool_name": "Task",
            "tool_input": {"subagent_type": subagent_type,
                           "description": description,
                           "prompt": "go"},
        })

    def test_pre_allows_under_budget(self):
        self.run_cli(["init"])
        code, _, _ = self.run_cli(["hook", "--event", "pre"],
                              stdin=self.hook_stdin())
        self.assertEqual(code, 0)

    def test_pre_blocks_over_budget(self):
        self.run_cli(["init", "--default-tokens", "10", "--default-usd", "100"])
        self.run_cli(["record", "--agent", "Explore auth", "--tokens", "50"])
        code, out, err = self.run_cli(["hook", "--event", "pre"],
                                  stdin=self.hook_stdin())
        self.assertEqual(code, 2)  # Claude Code blocking exit code
        self.assertIn("blocked", err.lower())

    def test_pre_json_decision(self):
        self.run_cli(["init"])
        code, out, _ = self.run_cli(["hook", "--event", "pre", "--json"],
                                stdin=self.hook_stdin())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["decision"], "approve")

    def test_pre_identity_override_args(self):
        self.run_cli(["init"])
        code, _, _ = self.run_cli(
            ["hook", "--event", "pre", "--agent", "manual-agent",
             "--type", "Plan"], stdin="")
        self.assertEqual(code, 0)

    def test_post_records_usage(self):
        self.run_cli(["init"])
        code, _, _ = self.run_cli(
            ["hook", "--event", "post", "--tokens", "1234"],
            stdin=self.hook_stdin())
        self.assertEqual(code, 0)
        records = store.read_ledger()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tokens"], 1234)
        self.assertEqual(records[0]["agent"], "Explore auth")


class SyncCliTest(Base):
    def test_sync_no_projects(self):
        self.run_cli(["init"])
        code, out, _ = self.run_cli(["sync"])
        self.assertEqual(code, 0)
        self.assertIn("0 new", out)


class ResetTest(Base):
    def test_reset_needs_confirm(self):
        code, _, err = self.run_cli(["reset"])
        self.assertEqual(code, store.EXIT_USAGE)

    def test_reset_clears(self):
        self.run_cli(["init"])
        self.run_cli(["record", "--agent", "a", "--tokens", "5"])
        code, _, _ = self.run_cli(["reset", "--confirm"])
        self.assertEqual(code, 0)
        self.assertEqual(store.read_ledger(), [])


if __name__ == "__main__":
    unittest.main()
