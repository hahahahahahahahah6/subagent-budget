#!/usr/bin/env python3
"""subagent-budget CLI: hard token/$ budget caps for subagents.

Budgets are enforced by hooks (the launch is blocked), not just displayed.

Exit codes: 0 ok, 2 usage/config error, 3 blocked (budget exceeded).
Stdlib only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, enforcer, store


def _print_json(obj):
    print(json.dumps(obj, indent=2, sort_keys=True))


def _fmt_int(value):
    try:
        return "%d" % int(round(float(value)))
    except (TypeError, ValueError):
        return "?"


# ---------------------------------------------------------------- init/set

def cmd_init(args):
    path = store.config_path()
    if os.path.exists(path) and not args.force:
        print("config already exists at %s (use --force to overwrite)" % path)
        return store.EXIT_USAGE
    cfg = store.default_config()
    if args.default_tokens is not None:
        cfg["default_budget"]["max_tokens"] = int(args.default_tokens)
    if args.default_usd is not None:
        cfg["default_budget"]["max_usd"] = float(args.default_usd)
    store.save_config(cfg)
    print("wrote %s" % path)
    print("default budget: %s tokens / $%.2f"
          % (_fmt_int(cfg["default_budget"]["max_tokens"]),
             cfg["default_budget"]["max_usd"]))
    print("add per-pattern budgets with: subagent-budget set-budget "
          "--pattern 'Explore*' --tokens 200000 --usd 10")
    return store.EXIT_OK


def cmd_set_budget(args):
    cfg = store.load_config()
    rule = {
        "pattern": args.pattern,
        "max_tokens": int(args.tokens) if args.tokens is not None else None,
        "max_usd": float(args.usd) if args.usd is not None else None,
    }
    updated = False
    for i, existing in enumerate(cfg.get("budgets") or []):
        if isinstance(existing, dict) and existing.get("pattern") == args.pattern:
            cfg["budgets"][i] = rule
            updated = True
            break
    if not updated:
        if args.position == "first":
            cfg["budgets"].insert(0, rule)
        else:
            cfg["budgets"].append(rule)
    store.save_config(cfg)
    print("%s budget for pattern %r: %s tokens / %s"
          % ("updated" if updated else "added", args.pattern,
             _fmt_int(rule["max_tokens"]) if rule["max_tokens"] is not None else "unlimited",
             ("$%.2f" % rule["max_usd"]) if rule["max_usd"] is not None else "unlimited"))
    return store.EXIT_OK


def cmd_budgets(args):
    cfg = store.load_config()
    if args.json:
        _print_json({"default_budget": cfg["default_budget"],
                     "budgets": cfg["budgets"]})
        return store.EXIT_OK
    print("default: %s tokens / $%.2f"
          % (_fmt_int(cfg["default_budget"].get("max_tokens")),
             cfg["default_budget"].get("max_usd") or 0))
    for rule in cfg.get("budgets") or []:
        print("  %-24s %s tokens / %s" % (
            rule.get("pattern"),
            _fmt_int(rule.get("max_tokens")) if rule.get("max_tokens") is not None else "unlimited",
            ("$%.2f" % rule.get("max_usd")) if rule.get("max_usd") is not None else "unlimited",
        ))
    return store.EXIT_OK


# ---------------------------------------------------------------- record

def cmd_record(args):
    cfg = store.load_config()
    tokens = args.tokens
    if args.input_tokens is not None or args.output_tokens is not None:
        tokens = (args.input_tokens or 0) + (args.output_tokens or 0)
    if args.cost is not None:
        cost = float(args.cost)
    else:
        cost = store.estimate_cost_usd(tokens, args.model or cfg.get("model"), cfg)
    rec = store.append_ledger({
        "kind": "manual",
        "source": "record",
        "agent": args.agent,
        "agent_type": args.type or "",
        "session_id": args.session or "",
        "tokens": int(tokens or 0),
        "input_tokens": args.input_tokens,
        "output_tokens": args.output_tokens,
        "cache_read_tokens": args.cache_read,
        "cache_write_tokens": args.cache_write,
        "model": args.model or cfg.get("model"),
        "cost_usd": cost,
        "note": args.note or "",
    })
    decision = store.check_budget(cfg, args.agent, args.type or "",
                                  store.read_ledger())
    print("recorded %s tokens ($%.4f%s) for agent %r"
          % (_fmt_int(rec["tokens"]), rec["cost_usd"],
             " estimated" if args.cost is None else "",
             args.agent))
    if not decision["allowed"]:
        print("WARNING: budget now exceeded: %s" % "; ".join(decision["reasons"]),
              file=sys.stderr)
    return store.EXIT_OK


# ---------------------------------------------------------------- sync

def cmd_sync(args):
    cfg = store.load_config()
    records = store.read_ledger()
    new_records = enforcer.sync_new_invocations(cfg, records)
    for rec in new_records:
        store.append_ledger(rec)
    print("sync: %d new subagent invocation(s) recorded "
          "(%s tokens each, estimated; pricing as of %s)"
          % (len(new_records), _fmt_int(cfg.get("sync_estimate_tokens")),
             cfg.get("pricing_as_of")))
    if args.json:
        _print_json({"added": len(new_records), "records": new_records})
    return store.EXIT_OK


# ---------------------------------------------------------------- report

def cmd_report(args):
    cfg = store.load_config()
    rows = enforcer.aggregate(store.read_ledger(), cfg)
    if args.json:
        _print_json({"rows": rows})
        return store.EXIT_OK
    if not rows:
        print("ledger is empty; run `subagent-budget sync` or `record` first")
        return store.EXIT_OK
    header = "%-28s %6s %12s %10s %16s %10s" % (
        "AGENT", "RUNS", "TOKENS", "COST", "REMAINING", "STATUS")
    print(header)
    print("-" * len(header))
    for row in rows:
        rem = row["remaining"]
        rem_parts = []
        if row["max_tokens"] is not None:
            rem_parts.append("%s tok" % _fmt_int(rem.get("tokens", 0)))
        if row["max_usd"] is not None:
            rem_parts.append("$%.2f" % rem.get("usd", 0))
        budget = row["pattern"] or "default"
        print("%-28s %6d %12s %10s %16s %10s" % (
            row["agent"][:28], row["runs"], _fmt_int(row["tokens"]),
            "$%.2f" % row["cost_usd"], ", ".join(rem_parts) or "-",
            "OVER (%s)" % budget if row["over"] else "ok"))
    return store.EXIT_OK


# ---------------------------------------------------------------- check

def cmd_check(args):
    cfg = store.load_config()
    decision = store.check_budget(cfg, args.agent, args.type or "",
                                  store.read_ledger())
    if args.json:
        _print_json(decision)
    else:
        _render_decision(decision)
    return store.EXIT_BLOCKED if not decision["allowed"] else store.EXIT_OK


def _render_decision(decision):
    print("agent: %r (type %r)" % (decision["agent"], decision["agent_type"]))
    print("budget: %s" % (decision["pattern"] or "default"))
    print("used: %s tokens / $%.2f"
          % (_fmt_int(decision["used_tokens"]), decision["used_cost_usd"]))
    limits = []
    if decision["max_tokens"] is not None:
        limits.append("%s tokens" % _fmt_int(decision["max_tokens"]))
    if decision["max_usd"] is not None:
        limits.append("$%.2f" % decision["max_usd"])
    print("limit: %s" % (" / ".join(limits) if limits else "none"))
    if decision["allowed"]:
        print("status: ALLOWED")
    else:
        print("status: BLOCKED")
        for reason in decision["reasons"]:
            print("  - %s" % reason)


# ---------------------------------------------------------------- hook

def _read_hook_stdin():
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _identity_from_hook(data, args):
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    agent = (args.agent or tool_input.get("description")
             or data.get("description") or "unnamed")
    agent_type = (args.type or tool_input.get("subagent_type")
                  or data.get("subagent_type") or "")
    return str(agent), str(agent_type)


def cmd_hook(args):
    """Claude Code hook entrypoint.

    --event pre:  check budgets before the subagent launches; exit 2 blocks.
    --event post: record a finished run's token usage into the ledger.
    """
    cfg = store.load_config()
    data = _read_hook_stdin()
    agent, agent_type = _identity_from_hook(data, args)

    if args.event == "post":
        tokens = args.tokens
        if tokens is None:
            usage = data.get("usage") or {}
            if isinstance(usage, dict):
                tokens = (usage.get("input_tokens") or 0) + \
                         (usage.get("output_tokens") or 0)
        tokens = int(tokens or 0)
        cost = store.estimate_cost_usd(tokens, cfg.get("model"), cfg)
        store.append_ledger({
            "kind": "hook",
            "source": "hook-post",
            "agent": agent,
            "agent_type": agent_type,
            "session_id": data.get("session_id") or "",
            "tokens": tokens,
            "cost_usd": cost,
            "model": cfg.get("model"),
        })
        print("recorded post-hook: %s tokens for %r" % (_fmt_int(tokens), agent))
        return store.EXIT_OK

    # --event pre: enforce.
    decision = store.check_budget(cfg, agent, agent_type, store.read_ledger())
    if args.json:
        decision = dict(decision)
        decision["decision"] = "block" if not decision["allowed"] else "approve"
        _print_json(decision)
    if decision["allowed"]:
        if not args.json:
            rem = decision["remaining"]
            bits = []
            if decision["max_tokens"] is not None:
                bits.append("%s tokens left" % _fmt_int(rem.get("tokens", 0)))
            if decision["max_usd"] is not None:
                bits.append("$%.2f left" % rem.get("usd", 0))
            print("subagent-budget: launch allowed for %r%s"
                  % (agent, (" (" + ", ".join(bits) + ")") if bits else ""))
        return store.EXIT_OK
    reason = ("subagent-budget blocked launch of %r (%s): %s"
              % (agent, agent_type or "unknown type",
                 "; ".join(decision["reasons"])))
    print(reason, file=sys.stderr)
    # Exit 2 = blocking error in Claude Code hooks; stderr is shown to Claude.
    return 2


# ---------------------------------------------------------------- import-rbg / reset

def cmd_import_rbg(args):
    default_rbg = os.path.expanduser("~/.resume-budget-guard/ledger.jsonl")
    path = args.path or default_rbg
    converted = enforcer.import_rbg_records(path)
    records = store.read_ledger()
    seen = {r.get("sync_key") for r in records if isinstance(r, dict)}
    added = 0
    for rec in converted:
        if rec["sync_key"] in seen:
            continue
        store.append_ledger(rec)
        seen.add(rec["sync_key"])
        added += 1
    print("import-rbg: %d record(s) imported from %s "
          "(agent names prefixed 'rbg:')" % (added, path))
    return store.EXIT_OK


def cmd_reset(args):
    if not args.confirm:
        print("refusing: pass --confirm to wipe the ledger", file=sys.stderr)
        return store.EXIT_USAGE
    path = store.ledger_path()
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    print("ledger cleared: %s" % path)
    return store.EXIT_OK


# ---------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(
        prog="subagent-budget",
        description="Hard token/$ budget caps for subagents, enforced by hooks.")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)

    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="write the default config file")
    s.add_argument("--default-tokens", type=int, default=None)
    s.add_argument("--default-usd", type=float, default=None)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("set-budget", help="add/update a per-pattern budget")
    s.add_argument("--pattern", required=True,
                   help="glob matched against agent name and subagent type")
    s.add_argument("--tokens", type=int, default=None)
    s.add_argument("--usd", type=float, default=None)
    s.add_argument("--position", choices=["first", "last"], default="last",
                   help="rule priority (first match wins)")
    s.set_defaults(func=cmd_set_budget)

    s = sub.add_parser("budgets", help="list configured budgets")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_budgets)

    s = sub.add_parser("record", help="record one subagent run's usage")
    s.add_argument("--agent", required=True)
    s.add_argument("--type", default="")
    s.add_argument("--tokens", type=int, default=0)
    s.add_argument("--input-tokens", type=int, default=None)
    s.add_argument("--output-tokens", type=int, default=None)
    s.add_argument("--cache-read", type=int, default=None)
    s.add_argument("--cache-write", type=int, default=None)
    s.add_argument("--model", default=None)
    s.add_argument("--cost", type=float, default=None,
                   help="exact USD; skips rate estimation")
    s.add_argument("--session", default="")
    s.add_argument("--note", default="")
    s.set_defaults(func=cmd_record)

    s = sub.add_parser("sync", help="scan Claude Code transcripts for Task calls")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("report", help="per-agent totals vs budgets")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("check", help="evaluate budgets for an agent (no hook)")
    s.add_argument("--agent", required=True)
    s.add_argument("--type", default="")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("hook", help="Claude Code hook entrypoint")
    s.add_argument("--event", choices=["pre", "post"], required=True)
    s.add_argument("--agent", default=None,
                   help="override identity (else read from hook stdin JSON)")
    s.add_argument("--type", default=None)
    s.add_argument("--tokens", type=int, default=None,
                   help="post-event: tokens to record")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("import-rbg",
                       help="import a resume-budget-guard ledger")
    s.add_argument("--path", default=None)
    s.set_defaults(func=cmd_import_rbg)

    s = sub.add_parser("reset", help="wipe the ledger")
    s.add_argument("--confirm", action="store_true")
    s.set_defaults(func=cmd_reset)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
