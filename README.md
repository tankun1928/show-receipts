<div align="center">

# 🧾 show-receipts

**Proof-of-done for AI coding agents.**
Your agent says *"Fixed! All tests pass."* Make it show the receipts.

Agent skill + hooks for **Claude Code**, Codex, Cursor, Gemini CLI and any agent that runs shell commands.
Catches hallucinated success claims: fake or stale test results, wrong test counts, commits that don't exist, invented APIs.

[Install](#install) · [How it works](#how-it-works) · [What it catches](#what-it-catches) · [FAQ](#faq) · [ภาษาไทย](README.th.md)

</div>

---

```text
● All 42 tests pass [R7] and the fix is committed as a3f8c21.

  Stop hook: show-receipts: your report makes claims the recorded evidence does not support.
  ✗ "All 42 tests pass [R7] and the fix…" — stale: src/cart.py changed after R7; re-run
  ✗ "All 42 tests pass [R7] and the fix…" — commit a3f8c21 does not exist in this repo

● Re-ran the suite after the last edit: 41 passed, 1 failed [R9]. The rounding case still
  fails, so the fix isn't complete. Nothing is committed yet.

  🧾 receipts: 0 verified · 0 unverified · 2 disclosed
```

## Why

AI coding agents grade their own homework. Two-thirds of developers say the biggest problem with
AI output is that it's *"almost right, but not quite"*, and verification is now the bottleneck,
not writing code ([Stack Overflow 2025](https://survey.stackoverflow.co/2025/), [Sonar State of Code 2026](https://www.sonarsource.com/state-of-code-developer-survey-report.pdf)).
The worst version of it: an agent that confidently reports tests passing, or a commit, that never happened.

Prompts like "verify before you claim done" help, but the agent still reports on itself.
**show-receipts takes the agent out of the witness box:**

1. **Hooks record what actually ran.** Every command, exit code, output hash and file edit goes into a
   hash-chained ledger (`.receipts/ledger.jsonl`). The harness writes it, not the model.
2. **Claims cite receipts.** The agent sees `🧾 R12 recorded (exit 0)` after each command and cites
   `[R12]` in its report, ending with a table of what is verified and what isn't.
3. **A Stop hook checks the report** against the ledger and the real repo before the agent can finish.
   Unsupported claims get sent back once, with the reason.

You review a 5-line receipts table instead of re-running everything yourself.

## Install

**Claude Code plugin (recommended: skill + hooks in one step)**

```text
/plugin marketplace add OWNER/show-receipts
/plugin install show-receipts@show-receipts
```

**Skill only, via the skills CLI** (Claude Code, Codex, Cursor, Gemini CLI, OpenCode, …)

```bash
npx skills add OWNER/show-receipts
```

Then, in Claude Code, turn on the hooks for a project (or `--user` for all projects):

```bash
python3 .claude/skills/show-receipts/scripts/receipts.py install
```

**Agents without hooks** (Codex, Cursor, Aider, …): the skill tells the agent to wrap checks with
`receipts.py run -- <cmd>` and finish with `receipts.py verify -m report.md`.

Requirements: Python 3.8+ (stdlib only, no dependencies). Git for commit checks. Node only for `check-symbol node`.

## How it works

```text
 agent runs `pytest -q`
        │
        ▼
 PostToolUse hook ──► .receipts/ledger.jsonl   R17 {cmd, exit 0, output sha256, tail, prev-hash}
        │
        └──► agent sees: 🧾 R17 recorded (exit 0)

 agent writes final report: "All 42 tests pass [R17]"
        │
        ▼
 Stop hook ──► verify claims ──► ✅ pass: badge shown to you
                               └► ❌ fail: agent is sent back once, with the reason
```

## What it catches

| The agent says | show-receipts checks | Caught when |
|---|---|---|
| "All tests pass" | a test run this session, exit 0 | tests were never run, or the run failed |
| "All tests pass [R7]" | no code edits after R7, no later failed run | code changed after the green run (**stale**) |
| "42 tests pass" | the count in the run's output | the output said `41 passed, 1 failed` |
| "Build/lint/typecheck is clean" | a build-type command, exit 0 | same as tests |
| "Committed as a3f8c21" | `git cat-file -e` | the commit doesn't exist |
| "Created `docs/setup.md`" | the file exists | it doesn't |
| "Fixed [R99]" | the receipt exists | invented receipt IDs |
| "Use `requests.Session.retry()`" | `check-symbol` against the *installed* version | hallucinated API, with did-you-mean suggestions |
| "Not verified: needs DATABASE_URL" | counted as **disclosed** | never blocked: honesty is the goal |

Docs-only edits (`.md`, images) don't make test runs stale. The checker understands English and Thai claims.

```bash
$ python3 receipts.py check-symbol python pandas.DataFrame.append
MISSING pandas.DataFrame has no attribute 'append' [pandas 3.0.5]
  did you mean: pipe, shape, apply, sample, astype
```

## Commands

| Command | |
|---|---|
| `receipts.py status` | is recording on here? |
| `receipts.py log [-n 20] [--all]` | recent receipts |
| `receipts.py show R12` | one receipt in full |
| `receipts.py run -- <cmd>` | run + record (agents without hooks) |
| `receipts.py verify -m report.md [--json]` | check a report by hand; exit 1 on failures |
| `receipts.py check-symbol python\|node <target>` | does this API exist in the installed version? |
| `receipts.py audit` | verify the ledger hash chain |
| `receipts.py install / uninstall [--user]` | manage the Claude Code hooks |

## Works well with

show-receipts is the enforcement layer for process skills like
[superpowers](https://github.com/obra/superpowers) (`verification-before-completion`),
[andrej-karpathy-skills](https://github.com/forrestchang/andrej-karpathy-skills) ("goal-driven execution")
and [planning-with-files](https://github.com/OthmanAdi/planning-with-files). Those tell the agent to verify;
show-receipts proves it did.

## FAQ

**Does it slow the agent down?** Barely. Recording a command takes about 0.1 s (Python start-up plus a
`git status` snapshot), next to commands that usually take seconds. The agent sees one short line per
command, and the Stop hook is silent when a turn makes no claims. In our evals the agent spent more time
overall, mostly because it verified more (e.g. starting a local Postgres instead of skipping the check).

**Can it get my agent stuck in a loop?** No. It sends the agent back at most once per turn; after that it
only shows you the badge.

**Can the agent fake receipts?** A PreToolUse hook blocks writes to `.receipts/`, and the hash chain makes
edits detectable with `receipts.py audit`. It's built to catch careless and overconfident claims, which is
the everyday failure. It isn't a security boundary against a deliberately adversarial model; for that, use
signed receipts with an external verifier.

**Does anything leave my machine?** No. No network calls, no telemetry. The ledger is local and gitignored.
It stores the last 30 lines of each command's output, so delete `.receipts/` if that output had secrets in it.

**False positive?** The claim patterns are heuristics. The agent is told to say so in one line, and you see
both. Please [open an issue](../../issues) with the sentence that tripped it.

## Contributing

Bug reports with the exact report sentence and `receipts.py show <id>` output are the most useful thing.
Run the tests with `python3 -m unittest discover tests`.

MIT licensed.

<!-- keywords: claude code skill, agent skills, SKILL.md, verification, verify ai agent, ai hallucination,
hallucinated tests, fake test results, proof of work, evidence, audit trail, trust but verify, guardrails,
claude code hooks, stop hook, codex, cursor, gemini cli, tdd, code review, ai code quality -->
