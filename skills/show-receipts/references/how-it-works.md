# How show-receipts works

## Contents
- The ledger
- Hooks
- Claim detection
- Checks per claim type
- Run mode (agents without hooks)
- Limits: what this does and doesn't protect against
- Privacy

## The ledger

`.receipts/ledger.jsonl` at the project root (the nearest parent with `.git` or `.receipts`, or
`$RECEIPTS_ROOT`). One JSON object per line:

```json
{"id": "R17", "ts": "2026-10-20T14:03:11+00:00", "session": "…", "source": "hook",
 "tool": "Bash", "command": "pytest -q", "exit": 0,
 "output_sha256": "…", "tail": "…last 30 lines…", "prev": "<hash of R16>", "hash": "…"}
```

File edits record `file` (relative path) and `file_sha256` after the write. In a git repo,
every command receipt also records `tree`: HEAD plus the git blob id of every code file that
differs from it. Staleness compares that snapshot with the worktree at check time, so edits made
through `sed -i`, a python heredoc or a code generator count too, a commit with no content change
doesn't, and an edit that was later reverted isn't stale. Build by-products (`__pycache__`,
`.pyc`, `node_modules`, coverage, caches) and docs are ignored. `source` is `hook`
(Claude Code hook), `run` (`receipts.py run`) or another tool's name.

Each entry's `hash` is the SHA-256 of the entry (without `hash`) including `prev`, the previous
entry's hash. `receipts.py audit` recomputes the chain; editing, deleting or inserting a line
breaks it. `.receipts/.gitignore` keeps the ledger out of commits.

## Hooks

| Event | Command | What it does |
|---|---|---|
| SessionStart | `hook session` | adds a 5-line protocol to the context, so the behavior holds even when the skill isn't loaded |
| PreToolUse (Bash, Write, Edit, MultiEdit, NotebookEdit) | `hook pre` | denies writes to `.receipts/` |
| PostToolUse (same tools) | `hook post` | records the receipt, tells the agent its ID via `additionalContext` |
| PostToolUseFailure (same tools) | `hook failure` | records a failed command with its real exit code |
| Stop | `hook stop` | verifies `last_assistant_message`; blocks once if a claim fails |

Read-only tools (Read, Grep, Glob, web search) aren't recorded: they don't change state and
would only add noise. Background Bash commands get `exit: null`, so they can't back a pass claim
until re-run in the foreground.

The Stop hook checks every turn, including turns where nothing ran (a success claim with an empty
ledger is the most blatant case). A turn with no claims produces no output.
`RECEIPTS_DISABLE=1` turns every hook off. When it blocks
and the agent stops again (`stop_hook_active`), it doesn't block a second time; it shows the
badge with the failure count, so the user sees it and the agent can't get stuck in a loop.

## Claim detection

The report is split into units: sentences, bullets and table rows. Fenced code blocks are dropped
first (pasted output isn't a claim). A unit is:

- a **disclosure** if it states a limitation: not verified, didn't run, couldn't, unable,
  skipped, untested, ⚠, ❌, ยังไม่, ไม่ได้ (a literal "0 failed" doesn't count)
- a **tests claim**: tests/specs/suite/pytest/jest/… near pass/passed/green/succeeded, or
  "N passed", or Thai เทส…ผ่าน
- a **build claim**: build/compile/typecheck/lint/mypy/tsc/… near pass/clean/succeeded/no errors
- a **commit claim**: commit/pushed/merged near a hex SHA containing a digit
- a **file claim**: created/added/wrote/generated near a backticked path
- a **done claim**: fixed/resolved/works/verified/done/implemented, or Thai แก้แล้ว/เสร็จแล้ว

Citations are `R<n>` after `[`, `(`, backtick, `|`, `,`, `;`, `/`, "receipt " or 🧾, so a stray
"R2" in prose (e.g. Cloudflare R2) isn't read as one.

## Checks per claim type

**tests / build**: uses the cited receipts that are commands (prefers ones matching the test or
build command patterns); if none are cited, uses the latest matching command in this session.
Fails if:
- no matching run this session
- the receipt's exit code isn't 0, or is unknown (background/interrupted)
- a later run of the same kind failed
- a code file differs now from when the run happened (stale; see `tree` above). Outside git,
  falls back to Edit/Write receipts made after the run.
- (tests) the output tail reports failures, or the claimed count differs from "N passed", or the
  claim says "41/42"

An uncited claim backed by a good run is *unverified* (warning, suggests the ID), not failed.

**commit**: `git cat-file -e <sha>^{commit}`; outside a git repo it's unverified.

**file**: the path exists relative to the project root.

**done**: verified if it cites existing receipts, unverified otherwise. Never blocks.

## Run mode (agents without hooks)

`receipts.py run -- <cmd>` runs the command (single string → shell, several args → exec),
streams combined output, records the receipt with `source: run` and the real exit code, and
exits with the command's code. In Claude Code, the Bash hook skips commands that are themselves
`receipts.py run`, so nothing is recorded twice. Set `RECEIPTS_SESSION` to group receipts; the
default is `local`. Finish with `receipts.py verify -m report.md`.

## Limits: what this does and doesn't protect against

It catches the common failure: an agent that honestly believes, or carelessly says, something it
never checked. It is not a security boundary against a model trying to forge evidence: an agent
with a shell could, in principle, append to the ledger through some path the PreToolUse
pattern doesn't catch. The hash chain makes that detectable with `audit`, but only if the forger
doesn't recompute it. For adversarial settings, use signed receipts (e.g. Ed25519 + an external
verifier).

The claim patterns are heuristics in English and Thai. A claim worded unusually may pass
unchecked; it will still show up as uncited in `.receipts/last-verify.md` if it uses a done-word.
Exit code 0 means the command succeeded, not that it tested the right thing: a reviewer should
still glance at *which* command a receipt points to. The table makes that a five-second check.

## Privacy

`tail` stores the last 30 lines (≤3000 chars) of each command's output, which may include
secrets printed to the terminal. The ledger stays local and is gitignored. Delete `.receipts/`
any time; nothing is sent anywhere.
