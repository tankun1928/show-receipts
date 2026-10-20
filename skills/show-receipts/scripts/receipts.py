#!/usr/bin/env python3
"""show-receipts: an evidence ledger for AI coding agents.

An agent can't be its own witness. Hooks record what actually ran (command,
exit code, output hash, files written) into .receipts/ledger.jsonl, and
`verify` checks the agent's final report against that ledger and the real
repo state, so "tests pass" means a recorded test run passed after the last
code edit.

Subcommands:
  hook pre|post|failure|stop   Claude Code hook entry points (JSON on stdin)
  run -- CMD...                run a command and record a receipt (agents without hooks)
  verify [--message FILE]      check a report's claims against the ledger
  log [-n N] [--all]           list recent receipts
  show ID                      print one receipt
  check-symbol LANG TARGET     check that an API exists in the installed dependency
  install [--user]             add the hooks to Claude Code settings
  uninstall [--user]           remove them
  status                       is the ledger / are the hooks active here?
  audit                        verify the ledger's hash chain

Stdlib only, Python 3.8+.
"""

import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

VERSION = "0.1.0"
LEDGER_DIR = ".receipts"
LEDGER_FILE = "ledger.jsonl"
REPORT_FILE = "last-verify.md"
TAIL_LINES = 30
TAIL_CHARS = 3000
GENESIS = "0" * 64

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
RECORD_TOOLS = {"Bash"} | WRITE_TOOLS
HOOK_MATCHER = "Bash|Write|Edit|MultiEdit|NotebookEdit"

# Edits to these don't make an earlier test run stale.
NON_CODE_EXT = {".md", ".markdown", ".rst", ".txt", ".adoc", ".png", ".jpg", ".jpeg",
                ".gif", ".svg", ".webp", ".ico", ".pdf", ".csv", ".log"}
# Build/test by-products: a test run rewriting these doesn't change the code under test.
IGNORED_EXT = {".pyc", ".pyo", ".class", ".o", ".obj"}
IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".venv",
                "venv", "node_modules", ".nyc_output", "coverage", "htmlcov", ".git", LEDGER_DIR,
                "target", ".next", ".turbo", ".gradle", ".dart_tool"}
TREE_LIMIT = 400

TEST_CMD = re.compile(
    r"(?i)(\bpytest\b|\bpy\.test\b|-m\s+unittest\b|\btox\b|\bnox\b|\bjest\b|\bvitest\b|"
    r"\bmocha\b|\bava\b|\bkarma\b|playwright\s+test|cypress\s+run|\bgo\s+test\b|"
    r"\bcargo\s+(test|nextest)\b|\b(npm|pnpm|yarn|bun)\s+(run\s+)?test\b|\bnpm\s+t\b|"
    r"\b(bun|deno)\s+test\b|\brspec\b|\brake\s+test\b|\bphpunit\b|\bpest\b|"
    r"\bdotnet\s+test\b|\bmvnw?\b.*\btest\b|\bgradlew?\b.*\btest\b|\bctest\b|"
    r"\bmake\s+(test|check)\b|\bmix\s+test\b|\bswift\s+test\b|\bflutter\s+test\b|"
    r"\bbats\b|\btest_\w+\.py\b|\w+_test\.\w+\b|\w+\.test\.\w+\b|\w+\.spec\.\w+\b)")
BUILD_CMD = re.compile(
    r"(?i)(\bbuild\b|\bcompile\w*|\btsc\b|\bmypy\b|\bpyright\b|\beslint\b|\bruff\b|"
    r"\bflake8\b|\bpylint\b|\bclippy\b|\bcargo\s+check\b|\bgo\s+vet\b|\bmake\b|"
    r"\bgcc\b|\bg\+\+|\bclang\b|\bjavac\b|\bwebpack\b|\blint\b|\btypecheck\b|"
    r"\bpy_compile\b|\bcompileall\b)")

# Claim detection. A "unit" is a sentence, bullet or table row of the report.
TEST_CLAIM = [
    re.compile(r"(?i)\b(tests?|specs?|test suite|suite|pytest|jest|vitest|mocha|go test|"
               r"cargo test|npm test|rspec|phpunit|unit tests?|e2e)\b[^.\n|]{0,60}?"
               r"\b(pass(es|ed|ing)?|green|succeed(s|ed)?|all ok)\b"),
    re.compile(r"(?i)\b\d+\s*(/\s*\d+\s*)?(tests?\s+)?(passed|passing)\b"),
    re.compile(r"(?i)\b(pass(es|ed)?|green)\b[^.\n|]{0,30}\b(tests?|suite|specs?)\b"),
    re.compile(r"(เทส|test|ทดสอบ)[^\n|]{0,25}ผ่าน"),
]
BUILD_CLAIM = [
    re.compile(r"(?i)\b(build|builds|compil\w*|type-?check\w*|tsc|lint\w*|mypy|pyright|"
               r"eslint|ruff|clippy)\b[^.\n|]{0,40}?\b(pass\w*|succeed\w*|success\w*|clean|"
               r"green|no (errors|warnings|issues))\b"),
    re.compile(r"(?i)\b(builds|compiles)\s+(fine|cleanly|successfully|without errors)\b"),
    re.compile(r"(build|บิลด์|คอมไพล์)[^\n|]{0,25}ผ่าน"),
]
COMMIT_CLAIM = re.compile(
    r"(?i)\b(commit(ted|s)?|pushed|merged|cherry-picked|sha)\b[^.\n|]{0,40}?`?\b([0-9a-f]{7,40})\b`?")
FILE_EXTS = ("py pyi ipynb js mjs cjs jsx ts tsx vue svelte json jsonc yaml yml toml ini cfg conf env md mdx rst txt "
             "html htm css scss sass less go rs java kt kts scala rb php cs fs c h cc cpp hpp m mm swift dart "
             "ex exs erl lua r jl sh bash zsh ps1 bat sql prisma graphql gql proto xml gradle lock csv tsv "
             "svg png jpg jpeg gif webp ico pdf tf hcl dockerfile").split()
FILE_CLAIM = re.compile(
    r"(?i)\b(created|added|wrote|written|generated|saved|new file)\b[^\n|]{0,60}?"
    r"`((?:[\w.-]+/)*[\w.-]*[A-Za-z_][\w.-]*\.(?:%s))`" % "|".join(sorted(FILE_EXTS, key=len, reverse=True)))
DONE_CLAIM = re.compile(
    r"(?i)\b(fixed(?=\s*($|[.,;:!)]|\s+(the|it|this|that|these|those|in|by|a|an|both|all|and|now|"
    r"with|so|bugs?|issues?|problems?|errors?|crash\w*|typos?|regressions?)\b))|resolved|works( now)?|"
    r"working( now)?|verified|confirmed|done|completed?|"
    r"implemented|all set)\b|แก้แล้ว|แก้ไขแล้ว|เสร็จแล้ว|เรียบร้อย|ใช้งานได้")
# A unit that states a limitation is a disclosure, not a success claim.
DISCLOSURE = re.compile(
    r"(?i)(not verified|unverified|not yet verified|couldn'?t|could not|cannot|can'?t|"
    r"unable to|didn'?t|did not|not run|wasn'?t|was not|haven'?t|have not|hasn'?t|"
    r"skipped|untested|not tested|no evidence|todo|⚠|❌|✗|ยังไม่|ไม่ได้|ไม่สามารถ)")
# Reporting a failure honestly ("41 passed, 1 failed", "still fails") is not a success claim.
NEGATIVE_RESULT = re.compile(
    r"(?i)\b[1-9]\d*\s+(tests?\s+)?(failed|failing|failures?|errors?)\b|\bstill\s+(fail\w*|broken|red)\b|"
    r"\b(do|does|did)(n'?t| not)\s+pass|\bnot\s+(all\s+)?pass\w*|\b(is|are)(n'?t| not)\s+passing\b|\bfails\b|"
    r"\b\d+\s+of\s+\d+\s+(tests?\s+)?(fail|failed|failing)\b|\bnot\s+(green|passing|clean)\b|"
    r"\b(is|are)(n'?t| not)\s+(green|clean|passing)\b|ไม่ผ่าน|ยังพัง")
# Future, conditional or hedged wording isn't a claim that something happened.
HEDGED = re.compile(
    r"(?i)\b(should|will|would|could|might|may|expected to)\s+(now\s+)?(pass|work|build|be green)\b|"
    r"\b(so|until|once|if|when|before|after|to make)\s+(that\s+)?(the\s+|all\s+)?(tests?|suite|build|it)\b[^.\n]{0,25}\bpass|"
    r"น่าจะ|ควรจะ")
CITATION = re.compile(r"(?:(?<=[\[(`|,;/])|(?<=[\[(`|,;/]\s)|(?<=receipt\s)|(?<=receipts\s)|(?<=🧾))R(\d{1,7})\b")
CLAIM_COUNT = re.compile(r"(?i)\b(\d+)\s*(?:/\s*(\d+)\s*)?(?:tests?\s+)?(?:passed|passing|pass)\b|\b(\d+)\s*/\s*(\d+)\b")
TAIL_PASSED = re.compile(r"(?i)\b(\d+)\s+(?:tests?\s+)?passed\b|\bpass(?:ed)?:?\s+(\d+)\b|\bTests run:\s*(\d+)")
TAIL_FAILED = re.compile(r"(?i)\b(\d+)\s+(?:tests?\s+)?(failed|failures?|errors?)\b|\bfail(?:ed)?:?\s+(\d+)\b")


# --------------------------------------------------------------------------- ledger

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def sha256_file(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def tail(text):
    lines = text.rstrip("\n").splitlines()[-TAIL_LINES:]
    out = "\n".join(lines)
    return out[-TAIL_CHARS:]


def project_root(start=None):
    env = os.environ.get("RECEIPTS_ROOT")
    if env:
        return Path(env).resolve()
    p = Path(start or os.getcwd()).resolve()
    for d in [p, *p.parents]:
        if (d / LEDGER_DIR).is_dir() or (d / ".git").exists():
            return d
    return p


def relpath(path, root):
    try:
        return str(Path(path).resolve().relative_to(root))
    except (ValueError, OSError):
        return str(path)


def entry_hash(entry):
    body = {k: v for k, v in entry.items() if k != "hash"}
    return sha256_text(json.dumps(body, sort_keys=True, ensure_ascii=False))


def receipt_num(rid):
    try:
        return int(str(rid).lstrip("R"))
    except ValueError:
        return 0


class Ledger:
    def __init__(self, root):
        self.root = Path(root)
        self.dir = self.root / LEDGER_DIR
        self.path = self.dir / LEDGER_FILE

    def exists(self):
        return self.path.exists()

    def entries(self):
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out

    def get(self, rid):
        for e in self.entries():
            if e.get("id") == rid:
                return e
        return None

    def append(self, entry):
        self.dir.mkdir(parents=True, exist_ok=True)
        gitignore = self.dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("# local evidence for show-receipts; not meant to be committed\n*\n")
        with open(self.path, "a+", encoding="utf-8") as f:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.seek(0)
                lines = [l for l in f.read().splitlines() if l.strip()]
                prev, n = GENESIS, 0
                if lines:
                    try:
                        last = json.loads(lines[-1])
                        prev, n = last.get("hash", GENESIS), receipt_num(last.get("id"))
                    except ValueError:
                        n = len(lines)
                rec = {"id": "R%d" % (n + 1)}
                rec.update(entry)
                rec["prev"] = prev
                rec["hash"] = entry_hash(rec)
                f.seek(0, 2)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
            finally:
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_UN)
        return rec


def current_session(entries):
    for e in reversed(entries):
        if e.get("session"):
            return e["session"]
    return None


def describe(e):
    if e.get("command") is not None:
        return e["command"]
    if e.get("file"):
        return "%s %s" % (e.get("tool", "edit").lower(), e["file"])
    return e.get("tool", "?")


# --------------------------------------------------------------------------- hooks

def is_self_run(cmd):
    return bool(re.search(r"receipts\.py[\"']?\s+run\b", cmd))


def hook_post(data, failed=False):
    tool = data.get("tool_name", "")
    if tool not in RECORD_TOOLS:
        return
    root = project_root(data.get("cwd"))
    ti = data.get("tool_input") or {}
    entry = {"ts": now(), "session": data.get("session_id", ""), "source": "hook", "tool": tool}
    if tool == "Bash":
        cmd = ti.get("command", "")
        if is_self_run(cmd):
            return  # `receipts.py run` writes its own receipt with the real exit code
        entry["command"] = cmd
        if failed:
            err = data.get("error") or ""
            m = re.match(r"\s*Exit code (\d+)", err)
            entry["exit"] = int(m.group(1)) if m else None
            if data.get("is_interrupt"):
                entry["interrupted"] = True
            output = err
        else:
            tr = data.get("tool_response")
            if isinstance(tr, dict):
                output = (tr.get("stdout") or "")
                if tr.get("stderr"):
                    output += "\n" + tr["stderr"]
                interrupted = tr.get("interrupted")
            else:
                output, interrupted = str(tr or ""), False
            if ti.get("run_in_background"):
                entry["exit"], entry["background"] = None, True
            elif interrupted:
                entry["exit"], entry["interrupted"] = None, True
            else:
                entry["exit"] = 0
        entry["output_sha256"] = sha256_text(output)
        entry["tail"] = tail(output)
        entry["tree"] = worktree_state(root)
    else:
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        entry["file"] = relpath(path, root)
        entry["file_sha256"] = sha256_file(path)
        entry["exit"] = None if failed else 0
        if failed:
            entry["error"] = (data.get("error") or "")[:500]
    rec = Ledger(root).append(entry)
    if tool == "Bash":
        note = "🧾 %s recorded (exit %s). Cite it as [%s] when you report this result." % (
            rec["id"], rec["exit"] if rec["exit"] is not None else "unknown", rec["id"])
    else:
        note = "🧾 %s %s %s" % (rec["id"], tool.lower(), rec["file"])
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": data.get("hook_event_name") or ("PostToolUseFailure" if failed else "PostToolUse"),
        "additionalContext": note}}))


LEDGER_TAMPER = re.compile(
    r"(>>?|\btee\b(\s+-a)?)\s*['\"]?[^\s;|&]*\.receipts/"
    r"|\b(rm|mv|cp|truncate|dd|shred|chmod|ln|sed\s+-i\S*|perl\s+-\S*i)\b[^;|&]*\.receipts\b"
    r"|\b(python[\d.]*|perl|ruby|node|php)\b[^;|&]*\.receipts/[^;|&]*?(open\(|write|>>|appendFile|unlink|remove)")


SESSION_PROTOCOL = """show-receipts is active: hooks record every command and edit as a receipt (🧾 R12).
- Verify for real, as far as this machine allows: run the tests, start the dev server or a locally installed database/service, write a quick script. Look before concluding something isn't available.
- Re-run checks after your last code edit; a green run before an edit proves nothing.
- In the final report, cite the receipt for each success claim, e.g. "all 42 tests pass [R17]", and state plainly what you could not verify and why. End with a short Receipts table (Claim | Evidence | Status).
- A Stop hook checks claims against the ledger and git. Never write to .receipts/."""


def hook_session(data):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": SESSION_PROTOCOL}}))


def hook_pre(data):
    """Keep the agent from writing its own evidence."""
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    reason = None
    if tool in WRITE_TOOLS:
        p = ti.get("file_path") or ti.get("notebook_path") or ""
        if LEDGER_DIR in Path(p).parts:
            reason = "The receipts ledger is written by hooks only."
    elif tool == "Bash":
        cmd = ti.get("command", "")
        if LEDGER_TAMPER.search(cmd):
            reason = "Commands that modify .receipts/ are blocked; the ledger is written by hooks only."
    if reason:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason + " Read it with `receipts.py log` / `receipts.py show ID`."}}))


def last_assistant_from_transcript(path):
    if not path or not os.path.exists(path):
        return ""
    text = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue
            content = (d.get("message") or {}).get("content")
            parts = [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
            if parts:
                text = "\n".join(parts)
    return text


def hook_stop(data):
    root = project_root(data.get("cwd"))
    ledger = Ledger(root)
    msg = data.get("last_assistant_message")
    if msg is None:
        msg = last_assistant_from_transcript(data.get("transcript_path"))
    result = verify(msg or "", ledger, session=data.get("session_id") or None)
    if not result.findings:
        return
    result.write_report(ledger)
    if result.failures and not data.get("stop_hook_active"):
        print(json.dumps({"decision": "block", "reason": result.block_reason()}))
    else:
        print(json.dumps({"systemMessage": result.badge()}))


# --------------------------------------------------------------------------- verify

class Finding:
    def __init__(self, status, kind, claim, detail, receipts=()):
        self.status = status  # verified | unverified | disclosed | failed
        self.kind = kind
        self.claim = claim
        self.detail = detail
        self.receipts = list(receipts)


ICON = {"verified": "✅", "unverified": "⚠️", "disclosed": "ℹ️", "failed": "❌"}


class Result:
    def __init__(self, findings, session):
        self.findings = findings
        self.session = session

    @property
    def failures(self):
        return [f for f in self.findings if f.status == "failed"]

    def counts(self):
        c = {"verified": 0, "unverified": 0, "disclosed": 0, "failed": 0}
        for f in self.findings:
            c[f.status] += 1
        return c

    def badge(self):
        c = self.counts()
        s = "🧾 receipts: %d verified · %d unverified · %d disclosed" % (
            c["verified"], c["unverified"], c["disclosed"])
        if c["failed"]:
            s += " · %d FAILED (see %s/%s)" % (c["failed"], LEDGER_DIR, REPORT_FILE)
        return s

    def table(self):
        rows = ["| | Claim | Evidence |", "|---|---|---|"]
        for f in self.findings:
            claim = f.claim.replace("|", "\\|").strip()
            if len(claim) > 90:
                claim = claim[:87] + "..."
            rows.append("| %s | %s | %s |" % (ICON[f.status], claim, f.detail.replace("|", "\\|")))
        return "\n".join(rows)

    def block_reason(self):
        lines = ["show-receipts: your report makes claims the recorded evidence does not support."]
        for f in self.failures:
            lines.append('✗ "%s" — %s' % (f.claim.strip()[:120], f.detail))
        lines.append("")
        lines.append("Fix each one: run the check now (the hook records it as a receipt) and cite its ID "
                     "like [R12], or correct the claim and say plainly what is not verified. "
                     "If a flag is a false positive, say so in one line. Do not edit .receipts/.")
        lines.append("Then write your complete final report again: the user sees only your last message, "
                     "so don't reply to this note, replace the report.")
        return "\n".join(lines)

    def write_report(self, ledger):
        ledger.dir.mkdir(parents=True, exist_ok=True)
        body = "# show-receipts verification\n\n%s\n\n%s\n" % (self.badge(), self.table())
        (ledger.dir / REPORT_FILE).write_text(body, encoding="utf-8")

    def to_json(self):
        return {"session": self.session, "counts": self.counts(),
                "findings": [{"status": f.status, "kind": f.kind, "claim": f.claim,
                              "detail": f.detail, "receipts": f.receipts} for f in self.findings]}


def split_units(message):
    text = re.sub(r"```.*?```", "\n", message, flags=re.S)  # pasted output is not a claim
    units = []
    for line in text.splitlines():
        s = line.strip()
        if not s or re.fullmatch(r"\|?[\s:|-]+\|?", s):
            continue
        if s.startswith("|"):
            if re.search(r"(?i)^\|\s*(claim|status|check)\s*\|", s):
                continue  # header row
            units.append(s)
            continue
        s = re.sub(r"^([-*+]|\d+[.)])\s+", "", s)
        units.extend(p for p in re.split(r"(?<=[.!?])\s+(?=[A-Z\[(`])", s) if p.strip())
    return units


def cited_ids(unit):
    return ["R" + m for m in CITATION.findall(unit)]


def is_disclosure(unit):
    cleaned = re.sub(r"(?i)\b0\s+(failed|failures|errors)\b", "", unit)
    return bool(DISCLOSURE.search(cleaned) or NEGATIVE_RESULT.search(cleaned))


def git_has_commit(root, sha):
    try:
        r = subprocess.run(["git", "-C", str(root), "cat-file", "-e", sha + "^{commit}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0:
        return True
    inside = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-dir"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return False if inside.returncode == 0 else None


def is_code_path(path):
    p = Path(path)
    if any(part in IGNORED_DIRS for part in p.parts):
        return False
    ext = p.suffix.lower()
    return ext not in NON_CODE_EXT and ext not in IGNORED_EXT


def later_code_edits(entries, after_num):
    return [e for e in entries
            if receipt_num(e.get("id")) > after_num and e.get("file") and is_code_path(e["file"])]


def git(cwd, *args):
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def blob_ids(top, paths):
    paths = [p for p in paths if (Path(top) / p).is_file()]
    out = {}
    for i in range(0, len(paths), 200):
        chunk = paths[i:i + 200]
        r = git(top, "hash-object", "--", *chunk)
        if r:
            out.update(zip(chunk, r.split()))
    return out


def worktree_state(root):
    """Git blob ids of every code file that differs from HEAD, so any later edit is detectable
    no matter which tool made it (Edit, sed, a python heredoc, a code generator...)."""
    top = (git(root, "rev-parse", "--show-toplevel") or "").strip()
    if not top:
        return None
    status = git(top, "status", "--porcelain", "-uall", "-z")
    if status is None:
        return None
    head = (git(top, "rev-parse", "--verify", "-q", "HEAD") or "").strip() or None
    dirty, items, i = [], status.split("\0"), 0
    while i < len(items):
        item = items[i]
        i += 1
        if len(item) < 4:
            continue
        if item[0] in "RC":
            i += 1  # with -z the rename source follows as its own field
        if is_code_path(item[3:]):
            dirty.append(item[3:])
    dirty = dirty[:TREE_LIMIT]
    blobs = blob_ids(top, dirty)
    return {"head": head, "files": {p: blobs.get(p, "deleted") for p in dirty}}


def changed_since(root, then):
    """Code files whose content differs now from when the receipt was taken."""
    now = worktree_state(root)
    if not now or not then:
        return []
    top = (git(root, "rev-parse", "--show-toplevel") or "").strip()
    paths = set(then["files"]) | set(now["files"])
    if then.get("head") and now.get("head") and then["head"] != now["head"]:
        diff = git(top, "diff", "--name-only", then["head"], now["head"]) or ""
        paths |= {p for p in diff.splitlines() if is_code_path(p)}
    if not paths:
        return []
    current = blob_ids(top, paths)
    base = {}
    clean_then = [p for p in paths if p not in then["files"]]
    if then.get("head") and clean_then:
        for line in (git(top, "ls-tree", "-r", then["head"], "--", *clean_then) or "").splitlines():
            meta, _, path = line.partition("\t")
            base[path] = meta.split()[2]
    changed = []
    for p in sorted(paths):
        before = then["files"].get(p) or base.get(p, "deleted")
        if before != current.get(p, "deleted"):
            changed.append(p)
    return changed


def count_check(unit, rec):
    """Compare '42 tests pass' in the claim with '41 passed, 1 failed' in the output."""
    t = rec.get("tail") or ""
    failed = [int(next(g for g in m if g and g.isdigit())) for m in TAIL_FAILED.findall(t)]
    if any(n > 0 for n in failed):
        return "%s output reports failures (%s)" % (rec["id"], ", ".join(
            m.group(0) for m in TAIL_FAILED.finditer(t) if re.search(r"[1-9]", m.group(0))))
    claimed = None
    m = CLAIM_COUNT.search(unit)
    if m:
        claimed = int(m.group(1) or m.group(3))
    passed = [int(next(g for g in mm if g)) for mm in TAIL_PASSED.findall(t)]
    if claimed is not None and passed and claimed not in passed and (m.group(2) or m.group(4) or "") == "":
        return "claim says %d but %s output says %s passed" % (claimed, rec["id"], passed[-1])
    if m and (m.group(2) or m.group(4)) and passed:
        total = int(m.group(2) or m.group(4))
        if claimed != total:
            return "claim reports %d/%d, which is not all passing" % (claimed, total)
    return None


def check_run_claim(kind, unit, ids, entries, by_id, session_entries, root):
    pattern = TEST_CMD if kind == "tests" else BUILD_CMD
    label = "test" if kind == "tests" else "build/lint"
    runs = [e for e in session_entries if e.get("command") is not None]
    cited = [by_id[i] for i in ids if i in by_id and by_id[i].get("command") is not None]
    if ids and not cited:
        cited_files = [by_id[i] for i in ids if i in by_id]
        if cited_files:
            return Finding("failed", kind, unit, "cites %s, which is a file edit, not a %s run" % (
                ", ".join(ids), label), ids)
    if not cited:
        matching = [e for e in runs if pattern.search(e["command"])]
        if not matching:
            return Finding("failed", kind, unit, "no receipt shows a %s run this session" % label)
        last = matching[-1]
        problem = run_problem(kind, unit, last, entries, runs, pattern, root)
        if problem:
            return Finding("failed", kind, unit, problem, [last["id"]])
        return Finding("unverified", kind, unit, "uncited, but %s `%s` (exit 0) supports it; cite [%s]" % (
            last["id"], short(last["command"]), last["id"]), [last["id"]])
    relevant = [e for e in cited if pattern.search(e["command"])] or cited
    for rec in relevant:
        problem = run_problem(kind, unit, rec, entries, runs, pattern, root)
        if problem:
            return Finding("failed", kind, unit, problem, [r["id"] for r in relevant])
    rec = relevant[-1]
    return Finding("verified", kind, unit, "%s `%s` exit 0" % (rec["id"], short(rec["command"])),
                   [r["id"] for r in relevant])


def run_problem(kind, unit, rec, entries, runs, pattern, root):
    if rec.get("exit") is None:
        return "%s has no exit code (%s); re-run it in the foreground" % (
            rec["id"], "background" if rec.get("background") else "interrupted")
    if rec["exit"] != 0:
        return "%s `%s` exited %s" % (rec["id"], short(rec["command"]), rec["exit"])
    n = receipt_num(rec["id"])
    later_fail = [e for e in runs if receipt_num(e["id"]) > n and pattern.search(e["command"])
                  and e.get("exit") not in (0, None)]
    if later_fail:
        e = later_fail[-1]
        return "a later run %s `%s` exited %s" % (e["id"], short(e["command"]), e["exit"])
    if rec.get("tree") is not None:
        changed = changed_since(root, rec["tree"])
        if changed:
            more = " (+%d more)" % (len(changed) - 3) if len(changed) > 3 else ""
            return "stale: %s changed after %s; re-run" % (", ".join(changed[:3]) + more, rec["id"])
        return count_check(unit, rec) if kind == "tests" else None
    edits = [e for e in later_code_edits(entries, n) if e.get("session") == rec.get("session")]
    if edits:
        files = sorted({e["file"] for e in edits})
        return "stale: %s changed after %s (%s); re-run" % (", ".join(files[:3]), rec["id"], edits[-1]["id"])
    if kind == "tests":
        return count_check(unit, rec)
    return None


def short(cmd, n=60):
    cmd = " ".join(cmd.split())
    return cmd if len(cmd) <= n else cmd[: n - 3] + "..."


def verify(message, ledger, session=None):
    entries = ledger.entries()
    session = session or current_session(entries)
    by_id = {e.get("id"): e for e in entries}
    session_entries = [e for e in entries if e.get("session") == session] if session else entries
    findings = []
    for unit in split_units(message):
        ids = cited_ids(unit)
        missing = [i for i in ids if i not in by_id]
        if missing:
            findings.append(Finding("failed", "citation", unit,
                                    "cites %s, but no such receipt exists" % ", ".join(missing), ids))
            continue
        if unit.rstrip(" *_`|").endswith("?"):
            continue  # a question isn't a claim
        if is_disclosure(unit):
            if any(p.search(unit) for p in TEST_CLAIM + BUILD_CLAIM) or DONE_CLAIM.search(unit) \
                    or re.search(r"(?i)verif|test|check|ตรวจ", unit):
                findings.append(Finding("disclosed", "limit", unit, "stated as not verified", ids))
            continue
        matched = False
        if HEDGED.search(unit) and not ids:
            if any(p.search(unit) for p in TEST_CLAIM + BUILD_CLAIM) or DONE_CLAIM.search(unit):
                findings.append(Finding("unverified", "hedged", unit, "hedged claim, no receipt"))
            continue
        for kind, patterns in (("tests", TEST_CLAIM), ("build", BUILD_CLAIM)):
            if any(p.search(unit) for p in patterns):
                findings.append(check_run_claim(kind, unit, ids, entries, by_id, session_entries, ledger.root))
                matched = True
                break
        for m in COMMIT_CLAIM.finditer(unit):
            sha = m.group(3)
            if not re.search(r"\d", sha):
                continue
            matched = True
            ok = git_has_commit(ledger.root, sha)
            if ok is None:
                findings.append(Finding("unverified", "commit", unit, "not a git repo; cannot check %s" % sha))
            elif ok:
                findings.append(Finding("verified", "commit", unit, "commit %s exists" % sha))
            else:
                findings.append(Finding("failed", "commit", unit, "commit %s does not exist in this repo" % sha))
        for m in FILE_CLAIM.finditer(unit):
            path = m.group(2)
            matched = True
            p = Path(path) if os.path.isabs(path) else ledger.root / path
            if p.exists():
                findings.append(Finding("verified", "file", unit, "`%s` exists" % path))
            else:
                findings.append(Finding("failed", "file", unit, "`%s` does not exist" % path))
        if not matched and DONE_CLAIM.search(unit):
            if ids:
                findings.append(Finding("verified", "done", unit, "backed by %s" % ", ".join(ids), ids))
            else:
                findings.append(Finding("unverified", "done", unit, "no receipt cited"))
    return Result(findings, session)


# --------------------------------------------------------------------------- check-symbol

PY_PROBE = r"""
import importlib, inspect, json, sys, difflib
target = sys.argv[1]
parts = target.split(".")
mod, i = None, len(parts)
err = None
while i > 0:
    try:
        mod = importlib.import_module(".".join(parts[:i])); break
    except ModuleNotFoundError as e:
        err = str(e); i -= 1
    except Exception as e:
        print(json.dumps({"status": "error", "error": "%s: %s" % (type(e).__name__, e)})); sys.exit(0)
if mod is None:
    print(json.dumps({"status": "no-module", "error": err})); sys.exit(0)
version = None
try:
    from importlib import metadata
    for dist in metadata.packages_distributions().get(parts[0], [parts[0]]):
        try:
            version = "%s %s" % (dist, metadata.version(dist)); break
        except Exception:
            pass
except Exception:
    pass
obj, path = mod, parts[:i]
for name in parts[i:]:
    if not hasattr(obj, name):
        names = [n for n in dir(obj) if not n.startswith("_") or name.startswith("_")]
        print(json.dumps({"status": "missing", "found": ".".join(path), "missing": name, "version": version,
                          "suggestions": difflib.get_close_matches(name, names, n=5, cutoff=0.5)})); sys.exit(0)
    obj = getattr(obj, name); path.append(name)
sig = None
try:
    sig = str(inspect.signature(obj))
except Exception:
    pass
src = None
try:
    src = "%s:%s" % (inspect.getsourcefile(obj), inspect.getsourcelines(obj)[1])
except Exception:
    pass
print(json.dumps({"status": "found", "symbol": ".".join(path), "kind": type(obj).__name__,
                  "signature": sig, "source": src, "version": version}))
"""

NODE_PROBE = r"""
const [pkg, sym] = process.argv.slice(1);
const out = (o) => { console.log(JSON.stringify(o)); process.exit(0); };
let m;
try { m = await import(pkg); }
catch (e) {
  try { const { createRequire } = await import('node:module'); m = createRequire(process.cwd() + '/')(pkg); }
  catch (e2) { out({ status: 'no-module', error: String(e2.message || e2).split('\n')[0] }); }
}
let obj = m, path = [];
for (const name of (sym ? sym.split('.') : [])) {
  let next = obj == null ? undefined : obj[name];
  if (next === undefined && path.length === 0 && m && m.default) next = m.default[name];
  if (next === undefined) {
    const names = obj == null ? [] : Object.keys(obj).concat(m && m.default && path.length === 0 ? Object.keys(m.default) : []);
    out({ status: 'missing', found: [pkg, ...path].join('.'), missing: name, available: names.slice(0, 60) });
  }
  obj = next; path.push(name);
}
out({ status: 'found', symbol: [pkg, ...path].join('.'), kind: typeof obj,
      arity: typeof obj === 'function' ? obj.length : null });
"""


def node_pkg_version(root, pkg):
    name = pkg if pkg.startswith("@") else pkg.split("/")[0]
    if pkg.startswith("@"):
        name = "/".join(pkg.split("/")[:2])
    for d in [root, *root.parents]:
        pj = d / "node_modules" / name / "package.json"
        if pj.exists():
            try:
                return "%s %s" % (name, json.loads(pj.read_text()).get("version"))
            except ValueError:
                return name
    return None


def check_symbol(lang, target, python=None):
    root = project_root()
    if lang in ("py", "python"):
        exe = python or os.environ.get("RECEIPTS_PYTHON") or sys.executable
        r = subprocess.run([exe, "-c", PY_PROBE, target], capture_output=True, text=True, cwd=str(root), timeout=60)
        try:
            info = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            info = {"status": "error", "error": (r.stderr or r.stdout).strip()[-500:]}
    elif lang in ("js", "ts", "node"):
        if "#" in target:
            pkg, sym = target.split("#", 1)
        elif target.startswith("@"):
            bits = target.split(".")
            pkg, sym = bits[0], ".".join(bits[1:])
        else:
            pkg, _, sym = target.partition(".")
        r = subprocess.run(["node", "--input-type=module", "-e", NODE_PROBE, pkg, sym],
                           capture_output=True, text=True, cwd=str(root), timeout=60)
        try:
            info = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            info = {"status": "error", "error": (r.stderr or r.stdout).strip()[-500:]}
        info["version"] = node_pkg_version(root, pkg)
        if info.get("status") == "missing":
            info["suggestions"] = difflib.get_close_matches(info["missing"], info.pop("available", []), n=5, cutoff=0.5)
    else:
        print("unsupported language %r (use python or node); for others, grep the installed "
              "dependency source and cite that receipt" % lang, file=sys.stderr)
        return 2
    status = info.get("status")
    ver = (" [%s]" % info["version"]) if info.get("version") else ""
    if status == "found":
        extra = info.get("signature") or ("arity %s" % info["arity"] if info.get("arity") is not None else "")
        print("FOUND %s%s  %s %s" % (info["symbol"], ver, info.get("kind", ""), extra or ""))
        if info.get("source"):
            print("  defined at %s" % info["source"])
        return 0
    if status == "missing":
        print("MISSING %s has no attribute %r%s" % (info["found"], info["missing"], ver))
        if info.get("suggestions"):
            print("  did you mean: %s" % ", ".join(info["suggestions"]))
        return 1
    if status == "no-module":
        print("NOT INSTALLED %s (%s)" % (target, info.get("error", "")))
        return 2
    print("ERROR %s" % info.get("error"))
    return 3


# --------------------------------------------------------------------------- install

def hook_command(sub):
    return '"%s" "%s" hook %s' % (sys.executable, os.path.abspath(__file__), sub)


def hooks_block():
    def h(sub):
        return [{"type": "command", "command": hook_command(sub), "timeout": 15}]
    return {
        "SessionStart": [{"hooks": h("session")}],
        "PreToolUse": [{"matcher": HOOK_MATCHER, "hooks": h("pre")}],
        "PostToolUse": [{"matcher": HOOK_MATCHER, "hooks": h("post")}],
        "PostToolUseFailure": [{"matcher": HOOK_MATCHER, "hooks": h("failure")}],
        "Stop": [{"hooks": h("stop")}],
    }


def settings_path(user):
    if user:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"
    return project_root() / ".claude" / "settings.local.json"


def is_ours(group):
    return any("receipts.py" in (h.get("command") or "") and " hook " in (h.get("command") or "")
               for h in group.get("hooks", []))


def strip_ours(settings):
    hooks = settings.get("hooks") or {}
    for event in list(hooks):
        hooks[event] = [g for g in hooks[event] if not is_ours(g)]
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    return settings


def load_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError:
            sys.exit("%s is not valid JSON; fix it first" % path)
    return {}


def cmd_install(user):
    path = settings_path(user)
    settings = strip_ours(load_json(path))
    hooks = settings.setdefault("hooks", {})
    for event, groups in hooks_block().items():
        hooks.setdefault(event, []).extend(groups)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    if not user:
        Ledger(project_root()).dir.mkdir(exist_ok=True)
        gi = Ledger(project_root()).dir / ".gitignore"
        if not gi.exists():
            gi.write_text("# local evidence for show-receipts; not meant to be committed\n*\n")
    print("show-receipts hooks installed in %s" % path)
    print("They take effect in the next Claude Code session (or after /hooks reload).")
    return 0


def cmd_uninstall(user):
    path = settings_path(user)
    if not path.exists():
        print("nothing to remove in %s" % path)
        return 0
    settings = strip_ours(load_json(path))
    path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print("show-receipts hooks removed from %s (the .receipts/ ledger is left in place)" % path)
    return 0


def cmd_status():
    root = project_root()
    ledger = Ledger(root)
    entries = ledger.entries()
    print("show-receipts %s" % VERSION)
    print("project root : %s" % root)
    print("ledger       : %s (%d receipts)" % (ledger.path if ledger.exists() else "none yet", len(entries)))
    found = []
    cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    for p in (cfg / "settings.json", root / ".claude" / "settings.json", root / ".claude" / "settings.local.json"):
        if p.exists():
            s = load_json(p)
            if any(is_ours(g) for gs in (s.get("hooks") or {}).values() for g in gs):
                found.append(str(p))
            if any("show-receipts" in k and v for k, v in (s.get("enabledPlugins") or {}).items()):
                found.append("%s (plugin enabled)" % p)
    print("hooks        : %s" % (", ".join(found) if found else
                                 "not found in settings (fine if installed as a plugin; otherwise run `install`)"))
    if entries:
        last = entries[-1]
        print("last receipt : %s %s" % (last.get("id"), short(describe(last))))
    return 0


# --------------------------------------------------------------------------- other commands

def cmd_run(argv):
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("usage: receipts.py run -- CMD [ARGS...]", file=sys.stderr)
        return 2
    shell = len(argv) == 1
    cmd_text = argv[0] if shell else subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(
        _shquote(a) for a in argv)
    proc = subprocess.Popen(argv[0] if shell else argv, shell=shell, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace")
    chunks = []
    for line in proc.stdout:
        sys.stdout.write(line)
        chunks.append(line)
    code = proc.wait()
    output = "".join(chunks)
    rec = Ledger(project_root()).append({
        "ts": now(), "session": os.environ.get("RECEIPTS_SESSION", "local"), "source": "run",
        "tool": "Bash", "command": cmd_text, "exit": code,
        "output_sha256": sha256_text(output), "tail": tail(output), "tree": worktree_state(project_root())})
    print("🧾 %s recorded (exit %d)" % (rec["id"], code), file=sys.stderr)
    return code


def _shquote(a):
    return a if re.fullmatch(r"[\w@%+=:,./-]+", a) else "'" + a.replace("'", "'\"'\"'") + "'"


def cmd_verify(args):
    ledger = Ledger(project_root())
    if args.message and args.message != "-":
        message = Path(args.message).read_text(encoding="utf-8")
    else:
        message = sys.stdin.read()
    result = verify(message, ledger, session=args.session)
    if args.json:
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
    else:
        print(result.badge())
        if result.findings:
            print()
            print(result.table())
    if ledger.exists():
        result.write_report(ledger)
    return 1 if result.failures else 0


def cmd_log(args):
    entries = Ledger(project_root()).entries()
    if not args.all:
        s = current_session(entries)
        entries = [e for e in entries if e.get("session") == s]
    for e in entries[-args.n:]:
        code = e.get("exit")
        mark = "✓" if code == 0 else ("?" if code is None else "✗%s" % code)
        print("%-6s %-4s %-6s %s" % (e.get("id"), mark, e.get("tool", ""), short(describe(e), 100)))
    return 0


def cmd_show(rid):
    rec = Ledger(project_root()).get(rid if rid.startswith("R") else "R" + rid)
    if not rec:
        print("no receipt %s" % rid, file=sys.stderr)
        return 1
    print(json.dumps(rec, ensure_ascii=False, indent=2))
    return 0


def cmd_audit():
    entries = Ledger(project_root()).entries()
    prev = GENESIS
    for e in entries:
        if e.get("prev") != prev or entry_hash(e) != e.get("hash"):
            print("ledger chain broken at %s: entry was edited, removed or inserted" % e.get("id"))
            return 1
        prev = e["hash"]
    print("ledger intact: %d receipts, chain verified" % len(entries))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run":
        return cmd_run(argv[1:])
    p = argparse.ArgumentParser(prog="receipts.py", description="Evidence ledger for AI coding agents.")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd")
    h = sub.add_parser("hook")
    h.add_argument("event", choices=["session", "pre", "post", "failure", "stop"])
    v = sub.add_parser("verify")
    v.add_argument("--message", "-m", help="file with the report (default: stdin)")
    v.add_argument("--session")
    v.add_argument("--json", action="store_true")
    lg = sub.add_parser("log")
    lg.add_argument("-n", type=int, default=20)
    lg.add_argument("--all", action="store_true", help="all sessions")
    sh = sub.add_parser("show")
    sh.add_argument("id")
    cs = sub.add_parser("check-symbol")
    cs.add_argument("lang", help="python | node")
    cs.add_argument("target", help="python: pkg.mod.Name.attr   node: pkg#export.path or pkg.export")
    cs.add_argument("--python", help="interpreter to probe with (default: the one running this script)")
    for name in ("install", "uninstall"):
        ip = sub.add_parser(name)
        ip.add_argument("--user", action="store_true", help="~/.claude/settings.json instead of this project")
    sub.add_parser("status")
    sub.add_parser("audit")
    sub.add_parser("run", help="run -- CMD...: run a command and record a receipt")
    args = p.parse_args(argv)

    if args.cmd == "hook":
        if os.environ.get("RECEIPTS_DISABLE") == "1":
            return 0
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            return 0
        try:
            {"session": hook_session, "pre": hook_pre, "post": hook_post, "stop": hook_stop,
             "failure": lambda d: hook_post(d, failed=True)}[args.event](data)
        except Exception as e:  # a broken hook must never break the agent's session
            print("show-receipts hook error: %s" % e, file=sys.stderr)
        return 0
    if args.cmd == "verify":
        return cmd_verify(args)
    if args.cmd == "log":
        return cmd_log(args)
    if args.cmd == "show":
        return cmd_show(args.id)
    if args.cmd == "check-symbol":
        return check_symbol(args.lang, args.target, args.python)
    if args.cmd == "install":
        return cmd_install(args.user)
    if args.cmd == "uninstall":
        return cmd_uninstall(args.user)
    if args.cmd == "status":
        return cmd_status()
    if args.cmd == "audit":
        return cmd_audit()
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
