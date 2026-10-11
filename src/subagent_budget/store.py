"""Config, ledger, pricing, and budget-rule matching for subagent-budget.

Ledger format (JSONL, one record per line) is deliberately compatible with
resume-budget-guard's ledger: every record carries ``ts`` (ISO-8601 UTC),
``kind``, and ``cost_usd``. Extra fields used here: ``agent`` (agent name),
``agent_type``, ``tokens`` (total), ``session_id``, ``source``, ``estimated``.
"""

from __future__ import annotations

import copy
import datetime
import fnmatch
import json
import os

#: Env override for the app home (used by tests and power users).
ENV_HOME = "SUBAGENT_BUDGET_HOME"

#: USD per million tokens. Estimates only (as of 2026-10-01); override in config.
PRICING_AS_OF = "2026-10-01"
MODEL_RATES = {
    "sonnet": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "opus": {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    "haiku": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
}

#: Default per-invocation token estimate used by `sync`, taken from the
#: aidiveyt measurement: median first-request size of a subagent run was
#: 47,117 tokens. Rough by design; override with `sync_estimate_tokens`.
DEFAULT_SYNC_ESTIMATE_TOKENS = 47117

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_BLOCKED = 3


def app_dir():
    return os.environ.get(ENV_HOME) or os.path.expanduser(
        "~/.config/subagent-budget"
    )


def config_path():
    return os.path.join(app_dir(), "config.json")


def ledger_path():
    return os.path.join(app_dir(), "ledger.jsonl")


def _ensure_dir():
    os.makedirs(app_dir(), exist_ok=True)


def default_config():
    return {
        "version": 1,
        "default_budget": {"max_tokens": 1_000_000, "max_usd": 50.0},
        "budgets": [],
        "model": "sonnet",
        "model_rates": copy.deepcopy(MODEL_RATES),
        "pricing_as_of": PRICING_AS_OF,
        "sync_estimate_tokens": DEFAULT_SYNC_ESTIMATE_TOKENS,
        "projects_dir": os.path.expanduser("~/.claude/projects"),
    }


def load_config():
    """Load config merged over defaults. Never raises on bad files."""
    cfg = default_config()
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    for key in ("default_budget", "budgets", "model", "sync_estimate_tokens",
                "projects_dir", "pricing_as_of", "version"):
        if key in raw:
            cfg[key] = raw[key]
    if isinstance(raw.get("model_rates"), dict):
        merged = copy.deepcopy(MODEL_RATES)
        for model, rates in raw["model_rates"].items():
            if isinstance(rates, dict):
                merged[str(model)] = dict(rates)
        cfg["model_rates"] = merged
    return cfg


def save_config(cfg):
    _ensure_dir()
    with open(config_path(), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")


def read_ledger(path=None):
    """Return list of ledger records; missing/corrupt lines are skipped."""
    records = []
    path = path or ledger_path()
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return records
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


def append_ledger(record, path=None):
    _ensure_dir()
    record = dict(record)
    record.setdefault(
        "ts", datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    with open(path or ledger_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def rates_for(model, config):
    """Pick the rate table for a model name (opus/sonnet/haiku substring)."""
    rates = config.get("model_rates") or {}
    m = (model or "").lower()
    for key in ("opus", "sonnet", "haiku"):
        if key in m and isinstance(rates.get(key), dict):
            return rates[key]
    default = rates.get(config.get("model") or "sonnet") or {}
    return default or MODEL_RATES["sonnet"]


def estimate_cost_usd(tokens, model=None, config=None):
    """Rough USD for a token total: 70% input / 30% output split. Estimate."""
    cfg = config or default_config()
    rates = rates_for(model, cfg)
    try:
        per_mtok = 0.7 * float(rates.get("input", 0)) + 0.3 * float(rates.get("output", 0))
    except (TypeError, ValueError):
        per_mtok = 0.0
    return round(float(tokens or 0) / 1_000_000 * per_mtok, 4)


def _matches(pattern, agent_name, agent_type):
    pat = (pattern or "").lower()
    for value in (agent_name, agent_type):
        if value and fnmatch.fnmatchcase(str(value).lower(), pat):
            return True
    return False


def match_rule(config, agent_name, agent_type):
    """Return (index, rule) of the first matching budget rule, or (None, default).

    Rules are matched against both the agent name and the subagent type,
    case-insensitively, using glob patterns. First match wins.
    """
    for i, rule in enumerate(config.get("budgets") or []):
        if not isinstance(rule, dict):
            continue
        if _matches(rule.get("pattern"), agent_name, agent_type):
            return i, rule
    return None, config.get("default_budget") or {}


def _limit(rule, key):
    val = (rule or {}).get(key)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def rule_cumulative(config, rule_index, rule, agent_name, agent_type, records):
    """Cumulative (tokens, cost_usd) in the scope a rule enforces.

    A named rule accumulates every record matching its pattern (so a
    ``Explore*`` cap covers all agents whose name or type matches).
    The default budget accumulates per agent name.
    """
    tokens = 0.0
    cost = 0.0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rule_index is None:
            in_scope = (rec.get("agent") or "") == (agent_name or "")
        else:
            in_scope = _matches(
                rule.get("pattern"), rec.get("agent"), rec.get("agent_type")
            )
        if not in_scope:
            continue
        try:
            tokens += float(rec.get("tokens") or 0)
        except (TypeError, ValueError):
            pass
        try:
            cost += float(rec.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
    return tokens, cost


def check_budget(config, agent_name, agent_type, records):
    """Enforce budgets. Returns a decision dict.

    ``allowed`` is False when any applicable limit is exceeded; ``reasons``
    explains why in plain language.
    """
    rule_index, rule = match_rule(config, agent_name, agent_type)
    used_tokens, used_cost = rule_cumulative(
        config, rule_index, rule, agent_name, agent_type, records
    )
    max_tokens = _limit(rule, "max_tokens")
    max_usd = _limit(rule, "max_usd")
    reasons = []
    if max_tokens is not None and used_tokens >= max_tokens:
        reasons.append(
            "token budget exceeded: used %s / %s tokens"
            % (_fmt_int(used_tokens), _fmt_int(max_tokens))
        )
    if max_usd is not None and used_cost >= max_usd:
        reasons.append(
            "spend budget exceeded: used $%.2f / $%.2f"
            % (used_cost, max_usd)
        )
    remaining = {}
    if max_tokens is not None:
        remaining["tokens"] = max(0.0, max_tokens - used_tokens)
    if max_usd is not None:
        remaining["usd"] = max(0.0, max_usd - used_cost)
    return {
        "allowed": not reasons,
        "reasons": reasons,
        "agent": agent_name,
        "agent_type": agent_type,
        "rule_index": rule_index,
        "pattern": rule.get("pattern") if rule_index is not None else None,
        "used_tokens": used_tokens,
        "used_cost_usd": used_cost,
        "max_tokens": max_tokens,
        "max_usd": max_usd,
        "remaining": remaining,
    }


def _fmt_int(value):
    return "%d" % int(round(value))
