"""Tests for config, ledger, pricing, and budget matching."""

import json
import os
import tempfile
import unittest

from subagent_budget import store


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ[store.ENV_HOME] = self.tmp.name
        self.addCleanup(os.environ.pop, store.ENV_HOME, None)

    def write_records(self, records):
        with open(store.ledger_path(), "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec) + "\n")


class ConfigTest(Base):
    def test_defaults_when_no_file(self):
        cfg = store.load_config()
        self.assertEqual(cfg["default_budget"]["max_tokens"], 1_000_000)
        self.assertEqual(cfg["budgets"], [])

    def test_roundtrip(self):
        cfg = store.load_config()
        cfg["budgets"].append({"pattern": "Explore*", "max_tokens": 5})
        store.save_config(cfg)
        again = store.load_config()
        self.assertEqual(again["budgets"][0]["pattern"], "Explore*")

    def test_corrupt_file_falls_back_to_defaults(self):
        store._ensure_dir()
        with open(store.config_path(), "w") as fh:
            fh.write("{not json")
        cfg = store.load_config()
        self.assertEqual(cfg["budgets"], [])

    def test_model_rates_merge(self):
        cfg = store.load_config()
        cfg["model_rates"]["sonnet"]["input"] = 9.99
        store.save_config(cfg)
        again = store.load_config()
        self.assertEqual(again["model_rates"]["sonnet"]["input"], 9.99)
        # untouched tables survive the merge
        self.assertEqual(again["model_rates"]["opus"]["input"], 15.00)


class LedgerTest(Base):
    def test_append_and_read(self):
        store.append_ledger({"kind": "manual", "agent": "a", "tokens": 10})
        records = store.read_ledger()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["agent"], "a")
        self.assertIn("ts", records[0])

    def test_corrupt_lines_skipped(self):
        with open(store.ledger_path(), "w") as fh:
            fh.write('{"kind": "ok"}\n')
            fh.write("garbage line\n")
            fh.write('{"kind": "ok2"}\n')
        self.assertEqual(len(store.read_ledger()), 2)

    def test_missing_ledger_reads_empty(self):
        self.assertEqual(store.read_ledger(), [])


class MatchTest(Base):
    def test_first_match_wins(self):
        cfg = store.load_config()
        cfg["budgets"] = [
            {"pattern": "Explore*", "max_tokens": 100},
            {"pattern": "*", "max_tokens": 999},
        ]
        idx, rule = store.match_rule(cfg, "Explore the auth code", "Explore")
        self.assertEqual(idx, 0)
        self.assertEqual(rule["max_tokens"], 100)

    def test_matches_type_when_name_differs(self):
        cfg = store.load_config()
        cfg["budgets"] = [{"pattern": "researcher", "max_tokens": 50}]
        idx, rule = store.match_rule(cfg, "deep dive task", "Researcher")
        self.assertEqual(idx, 0)

    def test_no_match_returns_default(self):
        cfg = store.load_config()
        idx, rule = store.match_rule(cfg, "whatever", "General")
        self.assertIsNone(idx)
        self.assertEqual(rule["max_tokens"], 1_000_000)


class CheckTest(Base):
    def _cfg(self):
        cfg = store.load_config()
        cfg["budgets"] = [{"pattern": "Explore*", "max_tokens": 100,
                           "max_usd": 1.0}]
        return cfg

    def test_allowed_under_budget(self):
        self.write_records([{"agent": "Explore x", "agent_type": "Explore",
                             "tokens": 10, "cost_usd": 0.01}])
        d = store.check_budget(self._cfg(), "Explore x", "Explore",
                               store.read_ledger())
        self.assertTrue(d["allowed"])
        self.assertEqual(d["reasons"], [])

    def test_blocked_on_tokens(self):
        self.write_records([{"agent": "Explore x", "agent_type": "Explore",
                             "tokens": 100, "cost_usd": 0.01}])
        d = store.check_budget(self._cfg(), "Explore x", "Explore",
                               store.read_ledger())
        self.assertFalse(d["allowed"])
        self.assertTrue(any("token" in r for r in d["reasons"]))

    def test_blocked_on_usd(self):
        self.write_records([{"agent": "Explore x", "agent_type": "Explore",
                             "tokens": 10, "cost_usd": 5.0}])
        d = store.check_budget(self._cfg(), "Explore x", "Explore",
                               store.read_ledger())
        self.assertFalse(d["allowed"])
        self.assertTrue(any("spend" in r for r in d["reasons"]))

    def test_pattern_scope_accumulates_across_agents(self):
        self.write_records([
            {"agent": "Explore a", "agent_type": "Explore",
             "tokens": 60, "cost_usd": 0.1},
            {"agent": "Explore b", "agent_type": "Explore",
             "tokens": 60, "cost_usd": 0.1},
        ])
        d = store.check_budget(self._cfg(), "Explore c", "Explore",
                               store.read_ledger())
        self.assertFalse(d["allowed"])  # 120 > 100 shared cap

    def test_default_scope_is_per_agent(self):
        cfg = store.load_config()  # default budget, no rules
        self.write_records([
            {"agent": "alpha", "tokens": 900_000, "cost_usd": 1.0},
            {"agent": "beta", "tokens": 900_000, "cost_usd": 1.0},
        ])
        d = store.check_budget(cfg, "beta", "", store.read_ledger())
        self.assertTrue(d["allowed"])  # only beta's own 900k counts

    def test_unlimited_dimension(self):
        cfg = store.load_config()
        cfg["budgets"] = [{"pattern": "*", "max_tokens": None, "max_usd": 1.0}]
        self.write_records([{"agent": "x", "tokens": 10**9, "cost_usd": 0.5}])
        d = store.check_budget(cfg, "x", "", store.read_ledger())
        self.assertTrue(d["allowed"])


class PricingTest(Base):
    def test_estimate_cost(self):
        cfg = store.load_config()
        # 1M tokens, sonnet 70/30 split: 0.7*3 + 0.3*15 = 6.6
        self.assertAlmostEqual(
            store.estimate_cost_usd(1_000_000, "sonnet", cfg), 6.6)

    def test_rates_for_model_substring(self):
        cfg = store.load_config()
        self.assertEqual(
            store.rates_for("claude-opus-4-6", cfg)["input"], 15.00)
        self.assertEqual(
            store.rates_for("claude-haiku-4-5", cfg)["output"], 5.00)


if __name__ == "__main__":
    unittest.main()
