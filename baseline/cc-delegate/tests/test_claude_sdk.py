import contextlib
import io
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import cc_delegate as cc
from tests import temp_workspace


class ClaudeSdkCase(unittest.TestCase):
    def setUp(self):
        self.root = temp_workspace(self)
        self.project = self.root / "project"
        self.bin = self.root / "bin"
        self.modules = self.root / "modules"
        for path in (self.project, self.bin, self.modules):
            path.mkdir()
        self.trace = self.root / "sdk.jsonl"
        self.release = self.root / "release"
        fixtures = Path(__file__).parent / "fixtures"
        shutil.copyfile(fixtures / "fake_claude_sdk.py", self.modules / "claude_agent_sdk.py")
        self.python = self.bin / "sdk-python"
        # Production uses isolated mode. Only this test interpreter sees the synthetic SDK.
        self.python.write_text(f"#!{sys.executable}\nimport os, sys\n"
                               f"os.environ['PYTHONPATH']={str(self.modules)!r}\n"
                               "os.execv(sys.executable, [sys.executable, *[a for a in sys.argv[1:] if a != '-I']])\n")
        self.sandbox = self.bin / "sandbox-exec"
        self.sandbox.write_text(f"#!{sys.executable}\nimport os, sys\n"
                                "assert sys.argv[1] == '-f'\nos.execvp(sys.argv[3], sys.argv[3:])\n")
        self.claude = self.bin / "claude"
        self.claude.write_text(f"#!{sys.executable}\n" + (fixtures / "fake_claude.py").read_text())
        for path in (self.python, self.sandbox, self.claude):
            path.chmod(0o700)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {
            "HOME": str(self.root), "TMPDIR": str(self.root), "CC_DELEGATE_HOME": str(self.root / "state"),
            "CC_DELEGATE_SDK_PYTHON": str(self.python), "SDK_TRACE": str(self.trace),
            "SDK_RELEASE": str(self.release), "FIXTURE_TRACE": str(self.root / "print-trace"),
            "PATH": str(self.bin) + os.pathsep + os.defpath, "GIT_CEILING_DIRECTORIES": str(self.root),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        }, clear=True))
        self.stack.enter_context(patch.object(cc, "SEATBELT", self.sandbox))
        self.stack.enter_context(patch.object(cc.sys, "platform", "darwin"))
        self.stack.enter_context(patch.object(cc, "start_net_monitor", return_value=None))
        self.runs = cc.home() / "delegate-runs"
        self.argv = ["run", "--caller", "codex", "--to", "claude", "--project-dir", str(self.project),
                     "--model", "fixture-model", "--task", "synthetic task"]

    def call(self, *args, expected=0):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cc.main(list(args))
        self.assertEqual(rc, expected, out.getvalue() + err.getvalue())
        return out.getvalue() + err.getvalue()

    def result(self, directory):
        return json.loads((directory / "result.json").read_text())

    def run_task(self, *args, expected=0):
        output = self.call(*self.argv, *args, expected=expected)
        directory = Path(json.loads(output.splitlines()[-1].split("=", 1)[1]))
        return directory, self.result(directory)

    def events(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def test_default_backend_model_resume_and_cc_compatibility(self):
        directory, result = self.run_task("--resume", "earlier-session")
        self.assertEqual((result["caller"], result["backend"], result["worker_session_id"], result["status"]),
                         ("codex", "sdk", "earlier-session", "ok"))
        self.assertEqual((directory / "report.md").read_text(), "first reply")
        self.assertIn("reading fixture", (directory / "events.jsonl").read_text())
        self.assertIn("live SDK delta", (directory / "events.jsonl").read_text())
        connected = next(e for e in self.events() if e["event"] == "connect")
        self.assertEqual((connected["model"], connected["cwd"]), ("fixture-model", str(self.project)))
        profile = directory / "sandbox.sb"
        launcher = Path(connected["cli_path"])
        self.assertTrue(launcher.is_relative_to(directory))
        self.assertIn(str(self.sandbox), launcher.read_text())
        self.assertIn(str(profile), launcher.read_text())
        self.assertIn(str(self.claude), launcher.read_text())
        self.assertIn(f"(deny file-write* (subpath {json.dumps(str(self.project))}))", profile.read_text())
        self.assertIn(f"(deny file-write* (require-all (subpath {json.dumps(str(directory.parent))}) "
                      "(require-not (subpath ", profile.read_text())
        _, cc_result = self.run_task("--caller", "claude")
        self.assertEqual(cc_result["backend"], "print")
        self.assertTrue((self.root / "print-trace").is_file())

    def test_write_lands_only_in_named_branch(self):
        cc.git(self.project, "init", "-q", "-b", "main")
        cc.git(self.project, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
               "commit", "--allow-empty", "-qm", "base")
        cc.git(self.project, "branch", "feature")
        with patch.dict(os.environ, {"SDK_SCENARIO": "write",
                                     "CC_DELEGATE_SDK_PYTHON": os.path.relpath(self.python)}):
            directory, result = self.run_task("--mode", "write", "--onto", "feature")
        self.assertEqual((result["status"], result["changed_files"]), ("ok", ["answer.txt"]))
        self.assertEqual(cc.git(self.project, "show", "feature:answer.txt"), "fixture answer\n")
        self.assertFalse((self.project / "answer.txt").exists())
        self.assertIn(str(result["worktree"]), (directory / "sandbox.sb").read_text())

    def test_interpreter_errors_fail_before_cloning(self):
        nonexec = self.bin / "not-executable"
        nonexec.touch()
        (self.modules / "claude_agent_sdk.py").write_text("raise ImportError('fixture')\n")
        for path, reason in [(self.bin / "missing", "executable"), (nonexec, "executable"),
                             (self.python, "cannot import")]:
            with self.subTest(path=path), patch.dict(os.environ, {"CC_DELEGATE_SDK_PYTHON": str(path)}):
                _, result = self.run_task("--mode", "write", expected=1)
                self.assertEqual(result["status"], "failed")
                self.assertIn(reason, result["reason"])
                self.assertIsNone(result["worktree"])
                self.assertFalse(cc.clone_root(self.project).exists())

    def test_sdk_zero_exit_without_terminal_frame(self):
        self.python.write_text(self.python.read_text().replace(
            "os.execv(", "if '-c' not in sys.argv: sys.exit(0)\nos.execv("))
        _, result = self.run_task(expected=2)
        self.assertEqual(result["status"], "outcome_unknown")
        self.assertIn("sdk_worker_missing_terminal_result", result["summary"][0])

    def test_final_ack_published_between_caller_checks(self):
        directory = self.root / "run"
        control = directory / "sdk-control"
        control.mkdir(parents=True)
        exists = Path.exists

        def publish_then_finish(path):
            if path.name.startswith("ack-") and not exists(path):
                cc.save_json(path, {"ok": True, "status": "finished"})
                cc.save_json(directory / "result.json", {"status": "ok"})
                return False
            return exists(path)

        with patch.object(Path, "exists", publish_then_finish):
            self.assertEqual(cc.sdk_request(directory, "finish")["status"], "finished")

    def test_orphan_disconnects_and_prune_preserves_dirty_clone(self):
        cc.git(self.project, "init", "-q", "-b", "main")
        cc.git(self.project, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
               "commit", "--allow-empty", "-qm", "base")
        with self.running("normal", "--keep-open", "--mode", "write") as (directory, proc):
            self.wait_for((directory / "sdk-control/idle.json").is_file, proc)
            tree = Path(json.loads((directory / "meta.json").read_text())["worktree"])
            self.assertIn("runs=0", self.call("prune", "--older-than", "0"))
            draft = tree / "draft"
            draft.write_text("unsaved work")
            proc.kill()
            proc.wait(timeout=5)
            end = time.monotonic() + 5
            while time.monotonic() < end:
                preview = self.call("prune", "--older-than", "0", "--dry-run")
                if "uncommitted_changes" in preview:
                    break
                time.sleep(.05)
            self.assertIn("uncommitted_changes", preview)
            self.assertIn("disconnect", [e["event"] for e in self.events()])
            self.assertIn("uncommitted_changes", self.call("prune", "--older-than", "0"))
            self.assertEqual(draft.read_text(), "unsaved work")
            draft.unlink()
            self.assertIn("runs=1", self.call("prune", "--older-than", "0"))
            self.assertFalse(directory.exists() or tree.exists())

    def test_sdk_failure_surfaces_without_switching_to_print(self):
        with patch.dict(os.environ, {"SDK_SCENARIO": "error"}):
            _, result = self.run_task(expected=2)
        self.assertEqual(result["backend"], "sdk")
        self.assertNotEqual(result["status"], "ok")
        self.assertIn("simulated SDK failure", " ".join(result["summary"]))
        self.assertFalse((self.root / "print-trace").exists())

    @contextlib.contextmanager
    def running(self, scenario, *args):
        script = ("import sys; from pathlib import Path; import cc_delegate as c; "
                  "c.sys.platform='darwin'; c.SEATBELT=Path(sys.argv.pop(1)); "
                  "c.start_net_monitor=lambda pid:None; sys.exit(c.main())")
        proc = subprocess.Popen([sys.executable, "-c", script, str(self.sandbox), *self.argv, *args],
                                cwd=Path(cc.__file__).parent, env=dict(os.environ, SDK_SCENARIO=scenario),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(select.select([proc.stdout], [], [], 10)[0], "no SDK startup receipt")
            line = shlex.split(proc.stdout.readline())
            fields = dict(field.split("=", 1) for field in line)
            self.assertEqual(fields["status"], "starting")
            self.assertRegex(fields["task_id"], cc.TASK_ID)
            yield self.runs / fields["task_id"], proc
        finally:
            self.release.touch()
            if proc.poll() is None:
                proc.terminate()
            proc.communicate(timeout=10)

    def wait_for(self, probe, proc):
        end = time.monotonic() + 10
        while time.monotonic() < end:
            value = probe()
            if value:
                return value
            if proc.poll() is not None:
                self.fail(f"worker exited before expected event: {proc.communicate()}")
            time.sleep(.025)
        self.fail("timed out waiting for fixture event")

    def finish(self, proc, expected=0):
        out, err = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, expected, out + err)

    def active(self):
        return any(e["event"] == "active" for e in self.events())

    def test_status_during_partial_message(self):
        with self.running("stream") as (directory, proc):
            self.wait_for(lambda: "live SDK delta" in self.call("status", directory.name), proc)
            self.assertFalse((directory / "result.json").exists())
            self.release.touch()
            self.finish(proc)

    def test_queue_waits_for_next_turn(self):
        with self.running("wait") as (directory, proc):
            self.wait_for(self.active, proc)
            status = self.call("status", directory.name)
            self.assertIn('status="running"', status)
            self.assertIn("reading fixture", status)
            self.call("queue", directory.name, "--message", "queued instruction api_key=fake-secret-canary")
            self.assertEqual(sum(e["event"] == "query" for e in self.events()), 1)
            self.release.touch()
            self.finish(proc)
            self.assertIn("queued instruction", (directory / "report.md").read_text())
            self.assertNotIn("fake-secret-canary", self.trace.read_text())
            self.call("queue", directory.name, "--message", "too late", expected=1)

    def check_steer(self, scenario, expected_turns):
        with self.running(scenario) as (directory, proc):
            self.wait_for(self.active, proc)
            self.call("steer", directory.name, "--message", "additional direction")
            self.assertEqual(sum(e["event"] == "query" for e in self.events()), 2)
            self.release.touch()
            self.finish(proc)
            self.assertIn("additional direction", (directory / "report.md").read_text())
            self.assertEqual(sum(e["event"] == "result" for e in self.events()), expected_turns)
            turns = [e for e in cc.records(directory / "events.jsonl") if e.get("type") == "sdk.turn_result"]
            self.assertEqual(len(turns), expected_turns)
            usage = self.result(directory)["worker_usage"]
            self.assertEqual((usage["total_tokens"], usage["cost_usd"]), (16 * expected_turns, 0.003))

    def test_steer_merged_into_current_turn(self):
        self.check_steer("merge", 1)

    def test_steer_answered_on_later_turn(self):
        self.check_steer("wait", 2)

    def test_ambiguous_steer_result_is_not_reported_as_success(self):
        with self.running("missing_ids") as (directory, proc):
            self.wait_for(self.active, proc)
            self.call("steer", directory.name, "--message", "overlapping instruction")
            self.release.touch()
            self.finish(proc, expected=2)
            result = self.result(directory)
            self.assertEqual(result["status"], "outcome_unknown")
            self.assertIn("sdk_result_missing_message_ids", " ".join(result["summary"]))

    def test_interrupt_followup_keeps_the_live_session(self):
        with self.running("wait") as (directory, proc):
            self.wait_for(self.active, proc)
            self.call("interrupt", directory.name, "--message", "replacement instruction")
            self.finish(proc)
            self.assertIn("replacement instruction", (directory / "report.md").read_text())
            events = self.events()
            self.assertEqual(sum(e["event"] == "connect" for e in events), 1)
            self.assertIn("interrupt", [e["event"] for e in events])

    def test_cancel_interrupts_sdk_client(self):
        with self.running("wait") as (directory, proc):
            self.wait_for(self.active, proc)
            self.call("cancel", directory.name, "--wait", "3")
            self.finish(proc, expected=2)
            self.assertEqual(self.result(directory)["status"], "cancelled")
            self.assertIn("interrupt", [e["event"] for e in self.events()])

    def test_bare_interrupt_with_success_subtype_is_not_a_completed_task(self):
        with self.running("aborted_success") as (directory, proc):
            self.wait_for(self.active, proc)
            self.call("interrupt", directory.name)
            self.finish(proc, expected=2)
            result = self.result(directory)
            self.assertEqual(result["status"], "outcome_unknown")
            self.assertIn("turn_interrupted", " ".join(result["summary"]))

    def test_keep_open_resumes_idle_client_and_finishes(self):
        with self.running("wait", "--keep-open") as (directory, proc):
            self.wait_for(self.active, proc)
            self.call("finish", directory.name, expected=1)
            self.release.touch()
            idle = directory / "sdk-control/idle.json"
            self.wait_for(idle.is_file, proc)
            self.assertIn('state="idle"', self.call("status", directory.name))
            self.assertFalse((directory / "result.json").exists())
            self.call("queue", directory.name, "--message", "second turn after idle")
            self.wait_for(lambda: idle.exists() and json.loads(idle.read_text()).get("turn") == 2, proc)
            self.call("finish", directory.name)
            self.finish(proc)
            self.assertEqual(self.result(directory)["worker_usage"]["total_tokens"], 32)
            self.assertIn("second turn after idle", (directory / "report.md").read_text())
            self.assertEqual(sum(e["event"] == "connect" for e in self.events()), 1)

    def test_cancel_while_session_idle(self):
        with self.running("normal", "--keep-open") as (directory, proc):
            self.wait_for((directory / "sdk-control/idle.json").is_file, proc)
            self.call("cancel", directory.name, "--wait", "2")
            self.finish(proc, expected=2)
            self.assertEqual(self.result(directory)["status"], "cancelled")
            self.assertEqual(self.result(directory)["worker_usage"]["total_tokens"], 16)

    def test_failed_followup_preserves_usage_and_idle_does_not_stall(self):
        with self.running("fail_followup", "--keep-open", "--stall-seconds", "2") as (directory, proc):
            self.wait_for((directory / "sdk-control/idle.json").is_file, proc)
            time.sleep(2.7)
            self.call("queue", directory.name, "--message", "second turn")
            self.finish(proc, expected=2)
            result = self.result(directory)
            self.assertIn("simulated follow-up failure", " ".join(result["summary"]))
            usage = result["worker_usage"]
            self.assertEqual((usage["total_tokens"], usage["cost_usd"]), (16, 0.003))

    def test_stall_timer_pauses_until_approval(self):
        with self.running("approval", "--approval", "ask", "--stall-seconds", "2") as (directory, proc):
            pending = self.wait_for(lambda: next((directory / "sdk-control").glob("pending-*.json"), None), proc)
            time.sleep(2.7)
            self.call("approve", directory.name, "--request-id", pending.stem.removeprefix("pending-"))
            self.finish(proc)

    def approval(self, command, expected_behavior):
        with self.running("approval", "--approval", "ask") as (directory, proc):
            pending = self.wait_for(lambda: next((directory / "sdk-control").glob("pending-*.json"), None), proc)
            request_id = pending.stem.removeprefix("pending-")
            status = self.call("status", directory.name)
            self.assertIn(request_id, status)
            self.assertIn("Bash", status)
            self.call(command, directory.name, "--request-id", "0" * 32, expected=1)
            self.call(command, directory.name, "--request-id", request_id)
            self.finish(proc)
            self.assertIn({"event": "decision", "behavior": expected_behavior}, self.events())

    def test_approval_allowed(self):
        self.approval("approve", "allow")

    def test_approval_denied(self):
        self.approval("deny", "deny")
