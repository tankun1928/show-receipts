---
name: show-receipts
description: >-
  Proof-of-done for AI coding agents: stops them from claiming "fixed", "tests pass", "build is
  green" or "committed abc123" without evidence. Hooks record every command, exit code and file
  edit into a tamper-evident ledger (.receipts/), the final report cites receipt IDs like [R12],
  and a Stop hook checks every claim against the ledger and the real git repo. It blocks fake or
  stale test results (code edited after the last run), wrong test counts, nonexistent commits or
  files, and hallucinated APIs (check-symbol). Use whenever the user wants to verify an agent's
  work, catch hallucinated or false success claims, trust but verify, add verification or
  anti-hallucination guardrails, an audit trail of what the agent ran, or says "show me proof",
  "show receipts", "did you actually run the tests?", "it said fixed but it isn't", "stop lying
  about tests". Also use whenever a .receipts/ directory exists in the project or a receipts
  hook message appears. Works with Claude Code, Codex, Cursor, Gemini CLI and other agents.
---

# show-receipts

An agent that says "all tests pass" is reporting on its own work, and nobody checks. The most
common complaint about AI coding agents is output that is *almost* right, and the most damaging
failure is a confident success claim that isn't true. This skill turns "trust me" into "here's
the receipt": what ran is recorded by the harness, not narrated by the model, and the final
report is checked against it.

`scripts/receipts.py` (Python 3.8+, stdlib only) does the recording and checking. Below,
`receipts.py` means that script; use its full path inside this skill's directory.

## 1. Make sure recording is on

Run `python3 receipts.py status`.

- **Hooks found** (or the show-receipts plugin is enabled): recording is automatic. After each
  Bash command you will see a note like `🧾 R12 recorded (exit 0)`. `RECEIPTS_DISABLE=1` turns
  the hooks off for a session.
- **No hooks, Claude Code**: if the user asked for this setup, run `python3 receipts.py install`
  (writes `.claude/settings.local.json` for this project; `--user` for every project). Hooks load
  in the next session, so for the rest of this one, use run mode below.
- **No hooks, any other agent** (Codex, Cursor, Gemini CLI, Aider...): wrap every check you plan
  to cite: `python3 receipts.py run -- pytest -q`. It streams output, records the real exit code
  and prints `🧾 R12 recorded (exit 0)` to stderr.

## 2. While working

- **Verify as far as the environment allows, then go one step further.** The point of receipts
  is more real verification, not more disclaimers. Before you settle for "not verified", try the
  reasonable local route: start the dev server and curl it, spin up the database or service if
  it's installed (look in the usual places, e.g. `/usr/lib/postgresql/*/bin`, docker, a
  `docker-compose.yml`), install a missing test dependency, or write a throwaway script that
  exercises the code path. Each of those runs gets a receipt you can cite.
- **Keep the IDs.** Tests, builds, type checks, linters, a curl against the dev server: each run
  prints its receipt ID. Remember which ID proves what; you'll cite them later.
- **Before using an API you aren't certain exists in the installed version**, check it instead of
  trusting memory:
  `python3 receipts.py check-symbol python requests.Session.mount` or
  `python3 receipts.py check-symbol node "date-fns#formatDistance"`.
  It reports FOUND with signature and version, or MISSING with close-match suggestions. Use the
  project's interpreter (`--python .venv/bin/python`) so you probe what's actually installed. For
  other languages, grep the dependency's installed source; that command is the receipt.
- **The last test run must come after the last code edit.** A green run followed by another edit
  proves nothing about the final code. The Stop hook flags this as *stale*, so re-run before you
  report.
- **Never touch `.receipts/`.** The ledger is hash-chained (`receipts.py audit` detects edits),
  and a PreToolUse hook denies writes to it. If a receipt is inconvenient, the claim is what
  needs to change.

## 3. The final report

Every success claim cites the receipt that proves it, and anything you could not verify says so.
A confident claim that turns out false costs the reviewer far more than an honest "not
verified", but a gap is only acceptable once you've genuinely tried. When you disclose one, say
what you tried and what's blocking you (e.g. "needs production credentials"), so the reviewer
knows the gap is real and how to close it.

End the report with a receipts table:

```markdown
## Receipts
| Claim | Evidence | Status |
|---|---|---|
| Off-by-one in `paginate()` fixed | R14 edit src/pagination.py | ✅ |
| All 42 tests pass | R17 `pytest -q` exit 0, after last edit | ✅ |
| Type check clean | R18 `mypy src` exit 0 | ✅ |
| Email lookup works on Postgres 16 | R21 local throwaway DB, `python3 -m unittest` exit 0 | ✅ |
| Fast enough on the production table (40M rows) | — | ⚠️ not verified: needs prod replica access |
```

Citation format: `[R17]` inline or the ID at the start of the Evidence cell. Write disclosures
plainly ("not verified", "didn't run", "couldn't test") so the checker reads them as disclosures
and the reviewer sees them as gaps.

Keep claims exact. "42 tests pass" must match the output's count; "all tests pass" must not cite a
run of a single file; "committed a3f8c21" must name a commit that exists.

## 4. When the Stop hook pushes back

If you get `show-receipts: your report makes claims the recorded evidence does not support`, each
line names a claim and why it failed. For each one, either run the missing check now and cite
the new receipt, or rewrite the claim as not verified. Then write the **whole** final report
again with the corrections: the user only sees your last message, so a reply addressed to the
hook ("I think the hook flagged...") leaves them without a report. Don't argue with the checker or rephrase
a claim just to dodge the patterns: the user sees the badge and the report in
`.receipts/last-verify.md`, and a dodged claim reads as worse than an honest gap. The hook
blocks only once per turn, so it can't trap you in a loop.

If the checker is wrong (a false positive), say so in one line with the reason, and the user
can see both sides.

## What the checker verifies

| Claim in the report | Check |
|---|---|
| tests / specs pass, N passed | a test-command receipt this session, exit 0, no later code edit, no later failing run, count matches output |
| build / lint / typecheck clean | same, for build-type commands |
| committed / pushed `<sha>` | `git cat-file -e <sha>^{commit}` |
| created / added `` `path` `` | the file exists |
| fixed / done / works | warns if no receipt is cited (doesn't block) |
| `[R99]` | the receipt exists |

Edits to docs (`.md`, `.txt`, images) don't make test runs stale. Details, patterns and
limits: `references/how-it-works.md`.

## Other commands

- `receipts.py log [-n 20] [--all]`: recent receipts in this session
- `receipts.py show R12`: full receipt (command, exit, output tail, hashes)
- `receipts.py verify -m report.md [--json]`: check a report by hand (exit 1 on failures); use
  this at the end of a task in agents without a Stop hook
- `receipts.py audit`: verify the ledger hash chain
- `receipts.py uninstall [--user]`: remove the hooks, keep the ledger
