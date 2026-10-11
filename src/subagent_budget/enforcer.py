"""Transcript scanning and aggregation for subagent-budget.

Scans Claude Code transcript JSONL (``~/.claude/projects/*/*.jsonl``) for
Task (subagent) tool_use blocks. Honest limitation: the parent transcript
does not contain the subagent's own token usage, so ``sync`` records one
ledger entry per invocation with an estimated token count (configurable via
``sync_estimate_tokens``). Exact figures come from ``record --tokens``.
"""

from __future__ import annotations

import glob
import json
import os

from . import store


def _iter_jsonl(path):
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
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
                yield obj


def _content_blocks(entry):
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def find_invocations(transcript_path):
    """Yield dicts for every Task tool_use in one transcript file."""
    invocations = []
    for entry in _iter_jsonl(transcript_path):
        if entry.get("type") != "assistant":
            continue
        for block in _content_blocks(entry):
            if block.get("type") != "tool_use":
                continue
            if str(block.get("name") or "").lower() != "task":
                continue
            tool_input = block.get("input") or {}
            if not isinstance(tool_input, dict):
                tool_input = {}
            invocations.append({
                "tool_use_id": block.get("id") or "",
                "agent": str(tool_input.get("description") or "unnamed"),
                "agent_type": str(tool_input.get("subagent_type") or ""),
                "session_id": entry.get("sessionId") or "",
                "timestamp": entry.get("timestamp") or "",
                "transcript_path": transcript_path,
            })
    return invocations


def scan_projects(projects_dir):
    """Return all Task invocations across every transcript under projects_dir."""
    pattern = os.path.join(os.path.expanduser(projects_dir), "*", "*.jsonl")
    invocations = []
    for path in sorted(glob.glob(pattern)):
        invocations.extend(find_invocations(path))
    return invocations


def sync_new_invocations(config, records):
    """Build ledger records for invocations not yet recorded (idempotent).

    Dedup key: transcript path + tool_use id, stored in ``sync_key``.
    """
    seen = {
        rec.get("sync_key")
        for rec in records
        if isinstance(rec, dict) and rec.get("sync_key")
    }
    estimate = config.get("sync_estimate_tokens") or 0
    model = config.get("model") or "sonnet"
    new_records = []
    for inv in scan_projects(config.get("projects_dir") or ""):
        key = "%s::%s" % (inv["transcript_path"], inv["tool_use_id"])
        if key in seen:
            continue
        seen.add(key)
        new_records.append({
            "kind": "invocation",
            "source": "sync",
            "estimated": True,
            "sync_key": key,
            "agent": inv["agent"],
            "agent_type": inv["agent_type"],
            "session_id": inv["session_id"],
            "tokens": int(estimate),
            "cost_usd": store.estimate_cost_usd(estimate, model, config),
            "model": model,
            "note": "estimated from sync_estimate_tokens; use "
                    "`record --tokens` for exact figures",
        })
    return new_records


def aggregate(records, config):
    """Aggregate ledger records per agent name -> row dicts."""
    rows = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        name = rec.get("agent") or "(unknown)"
        row = rows.setdefault(name, {
            "agent": name,
            "runs": 0,
            "tokens": 0.0,
            "cost_usd": 0.0,
            "estimated_runs": 0,
        })
        row["runs"] += 1
        if rec.get("estimated"):
            row["estimated_runs"] += 1
        try:
            row["tokens"] += float(rec.get("tokens") or 0)
        except (TypeError, ValueError):
            pass
        try:
            row["cost_usd"] += float(rec.get("cost_usd") or 0)
        except (TypeError, ValueError):
            pass
    # Attach the applicable budget rule and remaining headroom.
    for row in rows.values():
        # Find a representative type for rule matching.
        agent_type = ""
        for rec in records:
            if isinstance(rec, dict) and rec.get("agent") == row["agent"] \
                    and rec.get("agent_type"):
                agent_type = rec["agent_type"]
                break
        decision = store.check_budget(config, row["agent"], agent_type, records)
        row["pattern"] = decision["pattern"]
        row["max_tokens"] = decision["max_tokens"]
        row["max_usd"] = decision["max_usd"]
        row["remaining"] = decision["remaining"]
        row["over"] = not decision["allowed"]
    return [rows[k] for k in sorted(rows)]


def import_rbg_records(rbg_ledger_path):
    """Convert resume-budget-guard ledger lines into our ledger records.

    Keeps the original ``cost_usd`` and ``ts``; names the agent
    ``rbg:<session_key>`` so resume spend counts toward budgets.
    """
    converted = []
    for rec in store.read_ledger(rbg_ledger_path):
        try:
            cost = float(rec.get("cost_usd") or 0)
        except (TypeError, ValueError):
            continue
        if cost <= 0:
            continue
        session_key = rec.get("session_key") or rec.get("session_id") or "unknown"
        converted.append({
            "kind": "rbg_import",
            "source": "import-rbg",
            "agent": "rbg:%s" % session_key,
            "agent_type": "resume-budget-guard",
            "session_id": rec.get("session_id") or "",
            "tokens": 0,
            "cost_usd": round(cost, 4),
            "ts": rec.get("ts"),
            "sync_key": "rbg::%s::%s" % (rec.get("ts"), session_key),
        })
    return converted
