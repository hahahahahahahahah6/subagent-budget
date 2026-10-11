# subagent-budget

Hard budget caps for subagents — token **and** dollar limits enforced by
hooks, not just displayed on a dashboard.

## The problem

Subagents are the silent half of your Claude Code bill. In one real-world
measurement ([aidiveyt](https://github.com)), a month of usage — 455
sessions, 2,631 subagent runs — showed subagents consuming **48.1% of all
tokens**, with the median subagent's first request alone weighing in at
**47,117 tokens**. Nobody asked for that spend; it just happened in the
background.

Simon Willison put it bluntly: *"hard budget caps, please, now."*

Dashboards don't fix this. If a cap can't stop a launch, it's a suggestion.
`subagent-budget` wires into Claude Code's hook system so an over-budget
subagent is **refused at launch** — non-zero exit, reason on stderr, the
agent sees exactly why.

## How it differs

| | subagent-ledger | **subagent-budget** |
|---|---|---|
| Dimensions | tokens only | **tokens + dollars** |
| History | cleared when the session ends | **cross-session JSONL ledger** |
| Enforcement | none — display only | **hooks hard-block the launch** |
| Pricing | — | built-in model price table (overridable) |
| Resume-aware | — | **imports resume-budget-guard ledgers** |

## Install

```bash
pip install subagent-budget
```

Stdlib only, zero dependencies. Python 3.9+.

## Quickstart

```bash
# 1. Create the config (~/.config/subagent-budget/config.json)
subagent-budget init --default-tokens 1000000 --default-usd 50

# 2. Cap expensive agent types (glob matched against name AND subagent type)
subagent-budget set-budget --pattern "Explore*" --tokens 200000 --usd 10
subagent-budget set-budget --pattern "researcher" --tokens 500000 --usd 25

# 3. Backfill usage from Claude Code transcripts
subagent-budget sync

# 4. Record an exact run (e.g. from --output-format json)
subagent-budget record --agent "Explore auth code" --type Explore --tokens 47117

# 5. See where every agent stands
subagent-budget report
```

```
AGENT                        RUNS       TOKENS       COST        REMAINING     STATUS
Explore auth code               3       141351      $0.93   58649 tok, $9.07        ok
deep research dive              9       423000      $2.79        OVER (researcher)
```

## Claude Code hooks setup

This is the part that makes caps **hard**. Add to your Claude Code settings
(`~/.claude/settings.json`):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Task",
        "hooks": [
          {
            "type": "command",
            "command": "subagent-budget hook --event pre"
          }
        ]
      }
    ]
  }
}
```

Claude Code pipes the hook input JSON (tool name, `subagent_type`,
`description`) into the command's stdin. `subagent-budget` looks up the
cumulative usage for the matching budget rule; if a limit is exceeded it
exits with code **2** (Claude Code's blocking-hook signal) and the reason
goes to stderr, which Claude sees:

```
subagent-budget blocked launch of 'Explore auth code' (Explore):
token budget exceeded: used 210,441 / 200,000 tokens
```

Optional: record exact post-run usage with a `PostToolUse` hook on `Task`:

```json
{
  "matcher": "Task",
  "hooks": [
    { "type": "command", "command": "subagent-budget hook --event post --tokens $USAGE" }
  ]
}
```

(`--tokens` there is whatever your accounting pipeline measured; without it
the hook records 0 and `sync`/`record` remain the sources of truth.)

Manual check without hooks:

```bash
subagent-budget check --agent "Explore auth code" --type Explore
# exit 3 = over budget
```

## Budgets

- Rules are glob patterns matched (case-insensitively) against both the
  agent's description and its subagent type. **First match wins** — put
  specific patterns before broad ones (`--position first`).
- A named rule's usage accumulates across *all* agents matching its pattern
  (a shared pool). The default budget accumulates per agent name.
- Either dimension may be left unlimited: `--tokens` or `--usd` alone.
- Config lives at `~/.config/subagent-budget/config.json`; the ledger at
  `~/.config/subagent-budget/ledger.jsonl` and is **never cleared** at
  session end.

## Working with resume-budget-guard

The ledger format is intentionally compatible with
[resume-budget-guard](https://github.com/hahahahahahahahah6/resume-budget-guard)
(records carry `ts`, `kind`, `cost_usd`). If you already track resume spend
there, fold it into the same budgets:

```bash
subagent-budget import-rbg
# imports ~/.resume-budget-guard/ledger.jsonl as agent "rbg:<session-key>"
```

Imported spend counts toward the matching budget rules, so a resume can no
longer silently reset what a subagent cap was guarding. Idempotent — re-run
anytime.

## Honest limitations

- **Transcript sync is estimated.** A parent transcript does not contain a
  subagent's own token usage, so `sync` records one ledger entry per Task
  invocation using `sync_estimate_tokens` (default 47,117 — the measured
  median first-request size). Override it in config, or use
  `record --tokens` for exact figures.
- **Dollar amounts are estimates** from a built-in price table (as of
  2026-10-01). Override `model_rates` in config; pass `--cost` to record an
  exact figure.
- **A hook can only block what it sees.** The `pre` hook reads Claude Code's
  hook stdin; identity comes from `description`/`subagent_type`. If you spawn
  subagents outside Claude Code's Task tool, record them manually.
- Enforcement is per-machine (local ledger). It won't stop a second machine
  from spending.

## License

MIT
