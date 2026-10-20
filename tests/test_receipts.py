"""End-to-end tests: feed receipts.py the same JSON Claude Code sends its hooks."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "show-receipts" / "scripts" / "receipts.py"
SESSION = "sess-1"


class Repo:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".receipts").mkdir()

    def cli(self, *args, stdin=""):
        env = dict(os.environ, RECEIPTS_ROOT=str(self.root))
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, capture_output=True,
                              text=True, cwd=str(self.root), env=env)

    def hook(self, event, payload):
        payload = dict(payload, session_id=SESSION, cwd=str(self.root))
        r = self.cli("hook", event, stdin=json.dumps(payload))
        return json.loads(r.stdout) if r.stdout.strip() else None

    def bash(self, command, stdout="", exit_code=0):
        if exit_code == 0:
            return self.hook("post", {"hook_event_name": "PostToolUse", "tool_name": "Bash",
                                      "tool_input": {"command": command},
                                      "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False}})
        return self.hook("failure", {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                                     "tool_input": {"command": command},
                                     "error": "Exit code %d\n%s" % (exit_code, stdout)})

    def write(self, rel, content="x = 1\n"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return self.hook("post", {"hook_event_name": "PostToolUse", "tool_name": "Write",
                                  "tool_input": {"file_path": str(p), "content": content},
                                  "tool_response": {"type": "create"}})

    def stop(self, message, active=False):
        return self.hook("stop", {"hook_event_name": "Stop", "stop_hook_active": active,
                                  "last_assistant_message": message})


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()

    def tearDown(self):
        self.r.tmp.cleanup()

    def test_post_hook_records_and_tells_agent_the_id(self):
        out = self.r.bash("pytest -q", "5 passed in 0.1s")
        self.assertIn("R1", out["hookSpecificOutput"]["additionalContext"])
        out = self.r.bash("pytest -q", "1 failed", exit_code=1)
        self.assertIn("exit 1", out["hookSpecificOutput"]["additionalContext"])
        rec = json.loads(self.r.cli("show", "R2").stdout)
        self.assertEqual(rec["exit"], 1)

    def test_audit_detects_tampering(self):
        self.r.bash("pytest -q", "5 passed")
        self.r.bash("pytest -q", "1 failed", exit_code=1)
        self.assertEqual(self.r.cli("audit").returncode, 0)
        path = self.r.root / ".receipts" / "ledger.jsonl"
        path.write_text(path.read_text().replace('"exit": 1', '"exit": 0'))
        self.assertEqual(self.r.cli("audit").returncode, 1)

    def test_pre_hook_blocks_ledger_writes(self):
        out = self.r.hook("pre", {"tool_name": "Bash",
                                  "tool_input": {"command": "echo '{}' >> .receipts/ledger.jsonl"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        out = self.r.hook("pre", {"tool_name": "Write",
                                  "tool_input": {"file_path": str(self.r.root / ".receipts/ledger.jsonl")}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(self.r.hook("pre", {"tool_name": "Bash",
                                              "tool_input": {"command": "cat .receipts/ledger.jsonl"}}))
        self.assertIsNone(self.r.hook("pre", {"tool_name": "Bash",
                                              "tool_input": {"command": "ls -a; ls .receipts 2>/dev/null"}}))
        out = self.r.hook("pre", {"tool_name": "Bash", "tool_input": {
            "command": "python3 - <<'EOF'\nopen('.receipts/ledger.jsonl','a').write('{}')\nEOF"}})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")


class StopHookTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()

    def tearDown(self):
        self.r.tmp.cleanup()

    def test_blocks_test_claim_with_no_test_run(self):
        out = self.r.stop("Fixed the bug. All tests pass.")
        self.assertEqual(out["decision"], "block")
        self.assertIn("no receipt shows a test run", out["reason"])

    def test_passes_cited_green_run(self):
        self.r.write("src/app.py")
        self.r.bash("pytest -q", "12 passed in 0.3s")
        out = self.r.stop("Fixed the off-by-one in `src/app.py` [R1]. All 12 tests pass [R2].")
        self.assertNotIn("decision", out)
        self.assertIn("2 verified", out["systemMessage"])

    def test_blocks_claim_citing_failed_run(self):
        self.r.bash("npm test", "Tests: 1 failed, 4 passed", exit_code=1)
        out = self.r.stop("Tests pass [R1].")
        self.assertEqual(out["decision"], "block")
        self.assertIn("exited 1", out["reason"])

    def test_blocks_stale_run_after_code_edit(self):
        self.r.bash("pytest", "3 passed")
        self.r.write("src/app.py")
        out = self.r.stop("Tests pass [R1].")
        self.assertEqual(out["decision"], "block")
        self.assertIn("stale", out["reason"])

    def test_edit_through_bash_makes_run_stale(self):
        (self.r.root / "app.py").write_text("x = 1\n")
        self.r.bash("python3 -m unittest", "Ran 3 tests\n\nOK")
        (self.r.root / "app.py").write_text("x = 2\n")  # e.g. sed -i, no Edit-tool receipt
        self.r.bash("sed -i 's/1/2/' app.py")
        out = self.r.stop("Tests pass [R1].")
        self.assertEqual(out["decision"], "block")
        self.assertIn("app.py", out["reason"])

    def test_commit_after_green_run_is_not_stale(self):
        (self.r.root / "app.py").write_text("x = 1\n")
        self.r.bash("python3 -m unittest", "Ran 3 tests\n\nOK")
        git = ["git", "-C", str(self.r.root), "-c", "user.email=a@b", "-c", "user.name=t"]
        subprocess.run(git + ["add", "app.py"], check=True)
        subprocess.run(git + ["commit", "-qm", "fix"], check=True)
        out = self.r.stop("Tests pass [R1].")
        self.assertNotIn("decision", out)

    def test_version_number_is_not_a_file(self):
        out = self.r.stop("Added a `2.0.1` entry to the changelog.")
        self.assertTrue(out is None or "decision" not in out)

    def test_works_before_anything_was_recorded(self):
        import shutil
        shutil.rmtree(self.r.root / ".receipts")
        out = self.r.stop("All 12 tests pass and I committed the fix as a3f8c21.")
        self.assertEqual(out["decision"], "block")
        self.assertIn("whole", out["reason"].replace("complete", "whole"))

    def test_session_start_injects_protocol(self):
        out = self.r.hook("session", {"hook_event_name": "SessionStart"})
        self.assertIn("cite the receipt", out["hookSpecificOutput"]["additionalContext"])

    def test_honest_failure_report_is_a_disclosure(self):
        self.r.bash("pytest -q", "41 passed, 1 failed", exit_code=1)
        out = self.r.stop("Re-ran the suite: 41 passed, 1 failed [R1]. test_rounding still fails, "
                          "so the fix isn't complete.")
        self.assertNotIn("decision", out)
        self.assertIn("disclosed", out["systemMessage"])
        self.assertNotIn("FAILED", out["systemMessage"])

    def test_failing_adjective_still_checks_claim(self):
        out = self.r.stop("Fixed the failing test. All tests pass now.")
        self.assertEqual(out["decision"], "block")

    def test_real_world_false_positives(self):
        # sentences from eval runs that used to be flagged wrongly
        self.r.bash("python -m unittest", "FAILED (failures=2)", exit_code=1)
        for msg in [
            "The version bump and changelog entry are in, but the tests are not green, so I wouldn't tag yet.",
            "| All tests pass | R1 | **False: 2 of 4 fail** |",
            "Want me to fix the formatting so the tests pass and then re-run them?",
            "I created a `users` table with `Alice@Example.com` and `bob@example.com`.",
            "Added a `v2.0.1` entry and wrote `https://example.com/docs`.",
        ]:
            out = self.r.stop(msg)
            self.assertTrue(out is None or "decision" not in out, msg)

    def test_hedged_claim_warns(self):
        out = self.r.stop("The tests should pass now.")
        self.assertNotIn("decision", out)
        self.assertIn("1 unverified", out["systemMessage"])

    def test_adjective_fixed_is_not_a_claim(self):
        self.assertIsNone(self.r.stop("A fixed coupon larger than the subtotal now floors at zero."))

    def test_docs_edit_does_not_make_run_stale(self):
        self.r.bash("pytest", "3 passed")
        self.r.write("README.md", "# hi\n")
        out = self.r.stop("Tests pass [R1].")
        self.assertNotIn("decision", out)

    def test_blocks_wrong_test_count(self):
        self.r.bash("pytest", "41 passed in 2s")
        out = self.r.stop("All 42 tests pass [R1].")
        self.assertEqual(out["decision"], "block")
        self.assertIn("41", out["reason"])

    def test_blocks_nonexistent_receipt_and_commit(self):
        out = self.r.stop("Committed a3f8c21 with the fix [R9].")
        self.assertEqual(out["decision"], "block")
        self.assertIn("R9", out["reason"])
        out = self.r.stop("Committed the fix as a3f8c21.")
        self.assertIn("does not exist", out["reason"])

    def test_blocks_missing_created_file(self):
        out = self.r.stop("Created `docs/guide.md` with the setup steps.")
        self.assertIn("does not exist", out["reason"])

    def test_disclosure_is_not_a_claim(self):
        out = self.r.stop("Updated the handler. Integration tests not run: they need DATABASE_URL, "
                          "so this is not verified.")
        self.assertNotIn("decision", out)
        self.assertIn("1 disclosed", out["systemMessage"])

    def test_uncited_but_supported_claim_warns_only(self):
        self.r.bash("go test ./...", "ok  example.com/pkg 0.2s")
        out = self.r.stop("Tests pass.")
        self.assertNotIn("decision", out)
        self.assertIn("1 unverified", out["systemMessage"])

    def test_no_loop_when_stop_hook_active(self):
        out = self.r.stop("All tests pass.", active=True)
        self.assertNotIn("decision", out)
        self.assertIn("FAILED", out["systemMessage"])

    def test_silent_for_chat_without_claims(self):
        self.assertIsNone(self.r.stop("Which database do you want me to use?"))

    def test_table_report(self):
        self.r.bash("pytest -q", "8 passed")
        msg = ("## Receipts\n| Claim | Evidence | Status |\n|---|---|---|\n"
               "| 8 tests pass | R1 `pytest -q` | ✅ |\n"
               "| Works on Windows | — | ⚠️ not verified, no Windows machine |\n")
        out = self.r.stop(msg)
        self.assertNotIn("decision", out)
        self.assertIn("1 verified", out["systemMessage"])
        self.assertIn("1 disclosed", out["systemMessage"])

    def test_thai_claim(self):
        out = self.r.stop("แก้บั๊กเรียบร้อย เทสผ่านหมดแล้ว")
        self.assertEqual(out["decision"], "block")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.r = Repo()

    def tearDown(self):
        self.r.tmp.cleanup()

    def test_run_records_real_exit_code(self):
        r = self.r.cli("run", "--", sys.executable, "-c", "import sys; print('boom'); sys.exit(3)")
        self.assertEqual(r.returncode, 3)
        self.assertIn("R1", r.stderr)
        rec = json.loads(self.r.cli("show", "R1").stdout)
        self.assertEqual(rec["exit"], 3)
        self.assertIn("boom", rec["tail"])

    def test_verify_cli_exit_code(self):
        r = self.r.cli("verify", stdin="All tests pass.")
        self.assertEqual(r.returncode, 1)

    def test_check_symbol_python(self):
        r = self.r.cli("check-symbol", "python", "json.dumps")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.r.cli("check-symbol", "python", "json.dump_pretty")
        self.assertEqual(r.returncode, 1)
        self.assertIn("did you mean", r.stdout)
        r = self.r.cli("check-symbol", "python", "nonexistent_pkg_xyz.thing")
        self.assertEqual(r.returncode, 2)

    def test_check_symbol_node(self):
        r = self.r.cli("check-symbol", "node", "node:path#join")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.r.cli("check-symbol", "node", "node:path#joinAll")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_install_is_idempotent_and_reversible(self):
        settings = self.r.root / ".claude" / "settings.local.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
        self.r.cli("install")
        self.r.cli("install")
        s = json.loads(settings.read_text())
        self.assertEqual(len(s["hooks"]["Stop"]), 1)
        self.assertEqual(s["permissions"]["allow"], ["Bash(ls)"])
        self.r.cli("uninstall")
        s = json.loads(settings.read_text())
        self.assertNotIn("hooks", s)
        self.assertEqual(s["permissions"]["allow"], ["Bash(ls)"])


if __name__ == "__main__":
    unittest.main()
