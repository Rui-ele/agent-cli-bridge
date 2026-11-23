import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import cc_delegate as cc
from tests import temp_workspace


class AcpCase(unittest.TestCase):
    def setUp(self):
        self.root = temp_workspace(self)
        self.project = self.root / "project"
        self.bin = self.root / "bin"
        for path in (self.project, self.bin):
            path.mkdir()
        self.trace = self.root / "rpc.jsonl"
        self.ready = self.root / "ready"
        self.session = self.root / "synthetic.jsonl"
        self.session.touch()
        self.sandbox = self.bin / "sandbox-exec"
        self.script(self.sandbox, "import os, sys, json\nfrom pathlib import Path\n"
                    "Path(os.environ['ACP_TRACE']).with_suffix('.argv').write_text(json.dumps(sys.argv))\n"
                    "assert sys.argv[1] == '-f'\nos.execvp(sys.argv[3], sys.argv[3:])")
        self.script(self.bin / "agy", (Path(__file__).parent / "fixtures/fake_acp.py").read_text())
        self.script(self.bin / "codex", "import sys\nsys.exit(19)")
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(cc, "SEATBELT", self.sandbox))
        self.stack.enter_context(patch.object(cc.sys, "platform", "darwin"))
        self.stack.enter_context(patch.dict(os.environ, dict(
            PATH=str(self.bin) + os.pathsep + os.environ["PATH"], HOME=str(self.root),
            CC_DELEGATE_HOME=str(self.root / "cc"), CC_DELEGATE_AGY_BACKEND="acp",
            CC_DELEGATE_CODEX_BACKEND="exec", CODEX_HOME=str(self.root / "codex"),
            TMPDIR=str(self.root / "tmp"), ACP_TRACE=str(self.trace), ACP_READY=str(self.ready),
            GIT_CEILING_DIRECTORIES=str(self.root), GIT_CONFIG_NOSYSTEM="1")))
        (self.root / "tmp").mkdir()

    def script(self, path, body):
        path.write_text(f"#!{sys.executable}\n{body}\n")
        path.chmod(0o700)

    def run_task(self, *args, model="synthetic", fallback=False, expected=0):
        argv = ["run", "--to", "agy", "--model", model, "--project-dir", str(self.project),
                "--cc-session", str(self.session), "--task", "test ACP", *args]
        if not fallback:
            argv.append("--no-fallback")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cc.main(argv)
        d = Path(json.loads(out.getvalue().splitlines()[-1].split("=", 1)[1]))
        if rc != expected:
            events = d / "events.jsonl"
            self.fail(events.read_text() if events.exists() else out.getvalue())
        return d, json.loads((d / "result.json").read_text())

    def config(self):
        return {m["params"]["configId"]: m["params"]["value"] for m in cc.records(self.trace)
                if m.get("method") == "session/set_config_option"}

    def check_permission(self, d, kind, option):
        event = next(e for e in cc.records(d / "events.jsonl") if e["type"] == "permission.resolved")
        self.assertEqual((event["tool_call_id"], event["decision"]), ("cmd", kind))
        response = next(m for m in cc.records(self.trace) if "result" in m)
        self.assertEqual(response["result"]["outcome"], dict(outcome="selected", optionId=option))

    def profile_paths(self, profile):
        head, filters = profile.split("(allow file-write*\n")
        self.assertEqual(head, "(version 1)\n(allow default)\n(deny file-write*)\n")
        self.assertTrue(filters.endswith(")\n"))
        return {json.loads(re.fullmatch(r"    \(subpath (.+)\)", line)[1]) for line in filters[:-2].splitlines()}

    def common_paths(self):
        return {str(p.resolve()) for p in (self.root / ".gemini", self.root / "tmp", Path("/private/var/folders"),
                                          Path("/dev"))}

    def test_readonly_turn(self):
        d, meta = self.run_task(model="synthetic-high")
        self.assertEqual((meta["backend"], meta["status"], meta["worker_session_id"]), ("acp", "ok", "fixture-new"))
        self.assertEqual(meta["worker_usage"], dict(input_tokens=10, output_tokens=6, cached_read_tokens=2,
                                                   thought_tokens=3, total_tokens=16))
        self.assertEqual((d / "report.md").read_text(), "fixture reply")
        self.assertEqual(self.config(), {"model": "synthetic-high", "mode": "plan"})
        msg = next(m for m in cc.records(self.trace) if m.get("method") == "session/prompt")
        self.assertEqual(json.loads(msg["params"]["prompt"][0]["text"].splitlines()[-1]), cc.SCHEMA)
        self.check_permission(d, "reject_once", "no")
        item = next(e["item"] for e in cc.records(d / "events.jsonl")
                    if e["type"] == "item.completed" and e["item"].get("id") == "cmd")
        self.assertEqual((item["command"], item["status"], item["exit_code"]), ("git status", "failed", 1))
        self.assertEqual(self.profile_paths((d / "sandbox.sb").read_text()), self.common_paths())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.running_status(d, meta)
        self.assertIn('command="git status" exit_code=1', out.getvalue())
        self.assertIn("fixture reply", out.getvalue())

    def test_write_turn(self):
        cc.git(self.project, "init", "-q")
        cc.git(self.project, "-c", "user.name=fixture", "-c", "user.email=f@example.invalid",
               "commit", "--allow-empty", "-qm", "base")
        del os.environ["CC_DELEGATE_AGY_BACKEND"]
        d, meta = self.run_task("--mode", "write", model="synthetic-low")
        self.assertEqual((meta["backend"], meta["backend_source"]), ("acp", "auto"))
        self.assertEqual(self.config(), {"model": "synthetic", "thought_level": "low", "mode": "accept-edits"})
        self.check_permission(d, "allow_once", "yes")
        clone = Path(meta["worktree"])
        expected = {str(clone), str(clone.parent / (".git-" + d.name)), str(clone.parent / (".tmp-" + d.name))}
        self.assertEqual(self.profile_paths((d / "sandbox.sb").read_text()), self.common_paths() | expected)
        argv = json.loads(self.trace.with_suffix(".argv").read_text())
        self.assertEqual(argv, [str(self.sandbox), "-f", str(d / "sandbox.sb"), "agy", "acp"])

    def test_unstructured_reply(self):
        with patch.dict(os.environ, {"ACP_TEXT": "plain text from fixture"}):
            d, meta = self.run_task()
        self.assertEqual((meta["status"], meta["uncertainties"]), ("partial", ["invalid_worker_response"]))
        self.assertEqual((d / "report.md").read_text(), "plain text from fixture")

    def test_profile_path_escaping_and_realpath(self):
        directory = self.root / 'a"b\\c'
        directory.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(directory, target_is_directory=True)
        data = [alias, self.root / ".gemini"]
        for mode in ("ro", "write"):
            profile = cc.seatbelt_profile(mode, self.project, [alias], data)
            expected = self.common_paths() | {str(directory)}
            if mode == "write":
                expected.add(str(self.project))
            self.assertEqual(self.profile_paths(profile), expected)
            self.assertEqual(profile.count("(subpath "), len(expected))

    def test_resume_and_effort_override(self):
        for load in ("", "1"):
            with self.subTest(load=load), patch.dict(os.environ, {"ACP_LOAD": load}):
                self.trace.unlink(missing_ok=True)
                d, meta = self.run_task("--resume", "old-session", "--effort", "high", model="synthetic-low")
                self.assertEqual(meta["worker_session_id"], "old-session")
                self.assertFalse(meta["loop_warning"])
                self.assertEqual(self.config()["thought_level"], "high")
                methods = [m.get("method") for m in cc.records(self.trace)]
                self.assertEqual(methods[1], "session/resume")
                self.assertEqual("session/load" in methods, bool(load))
                self.assertNotIn("old command", (d / "events.jsonl").read_text())
                self.assertEqual(json.loads((d / "last.txt").read_text())["report_markdown"], "fixture reply")

    def test_unknown_model_or_effort(self):
        for model, args, options in (("missing", [], "synthetic, synthetic-high"),
                                     ("synthetic", ["--effort", "medium"], "low, high")):
            with self.subTest(model=model):
                d, meta = self.run_task(*args, model=model, expected=2)
                self.assertEqual(meta["reason"], "worker_failed")
                self.assertIn("available: " + options, (d / "events.jsonl").read_text())
                self.assertNotIn("session/prompt", self.trace.read_text())

    def test_sandbox_unavailable_and_explicit_exec(self):
        for blocked in (patch.object(cc.sys, "platform", "linux"), patch.object(cc, "SEATBELT", self.bin / "absent")):
            with blocked:
                _, meta = self.run_task(fallback=True, expected=1)
                self.assertEqual((meta["status"], meta["reason"]), ("blocked", "os_sandbox_unavailable"))
                self.assertEqual(meta["backend_source"], "env")
                self.assertFalse(self.trace.exists())
        d, meta = self.run_task("--backend", "exec", "--dry-run")
        self.assertNotIn("backend", meta)
        self.assertEqual(shlex.split((d / "command.txt").read_text())[0], "agy")

    def test_automatic_backend_without_seatbelt(self):
        del os.environ["CC_DELEGATE_AGY_BACKEND"]
        cc.git(self.project, "init", "-q")
        cc.git(self.project, "-c", "user.name=fixture", "-c", "user.email=f@example.invalid",
               "commit", "--allow-empty", "-qm", "base")
        for unavailable in (patch.object(cc.sys, "platform", "linux"),
                            patch.object(cc, "SEATBELT", self.bin / "absent")):
            with unavailable:
                d, meta = self.run_task("--mode", "write", "--dry-run")
                self.assertEqual((meta["backend_source"], meta["backend_fallback"]),
                                 ("auto", "os_sandbox_unavailable"))
                self.assertEqual(shlex.split((d / "command.txt").read_text())[:2], ["agy", "--input-format"])
                _, meta = self.run_task("--mode", "write", "--backend", "acp", expected=1)
                self.assertEqual((meta["backend_source"], meta["reason"]), ("cli", "os_sandbox_unavailable"))
                self.assertNotIn("backend_fallback", meta)
        d, meta = self.run_task("--dry-run")
        self.assertEqual(meta["backend_source"], "auto")
        self.assertNotIn("backend_fallback", meta)
        self.assertEqual(shlex.split((d / "command.txt").read_text())[:2], ["agy", "--input-format"])

    def codex_retry(self):
        self.script(self.bin / "codex", "import sys\nfrom pathlib import Path\nsys.stdin.read()\n"
                    "assert sys.argv[1] == 'exec'\nPath(sys.argv[sys.argv.index('-o')+1]).write_text("
                    """'{"status":"ok","summary":[],"uncertainties":[],"report_markdown":"retry"}')""")

    def test_non_end_turn_redispatch(self):
        self.codex_retry()
        with patch.dict(os.environ, {"ACP_STOP": "max_tokens"}):
            d, meta = self.run_task(fallback=True)
        self.assertEqual((meta["worker"], meta["backend"], meta["fallback_reason"]), ("codex", "exec", "worker_failed"))
        first = json.loads((d.parent / meta["fallback_from"] / "result.json").read_text())
        self.assertNotEqual(first["exit_code"], 0)
        self.assertEqual((first["backend"], first["status"]), ("acp", "outcome_unknown"))
        self.assertEqual(first["worker_usage"]["total_tokens"], 16)

    def test_redispatch_follows_codex_backend_env(self):
        self.codex_retry()
        for value in ("daemon", "bogus"):
            with self.subTest(value=value), patch.dict(os.environ, {"ACP_STOP": "max_tokens",
                                                                    "CC_DELEGATE_CODEX_BACKEND": value}):
                d, meta = self.run_task(fallback=True)
                run_meta = json.loads((d / "meta.json").read_text())
                self.assertEqual((meta["worker"], meta["backend"], meta["status"]), ("codex", "exec", "ok"))
                # No daemon socket exists in the sandboxed CODEX_HOME, so the daemon choice degrades to exec.
                self.assertEqual(run_meta.get("backend_fallback"),
                                 "daemon_unavailable" if value == "daemon" else None)

    def test_stall_redispatch(self):
        self.codex_retry()
        with patch.dict(os.environ, {"ACP_WAIT": "reply"}), patch.object(cc, "start_net_monitor", return_value=None) as net:
            d, meta = self.run_task("--stall-seconds", "1", fallback=True)
        self.assertEqual((meta["worker"], meta["fallback_reason"]), ("codex", "stalled"))
        first = d.parent / meta["fallback_from"]
        pid = next(e["pid"] for e in cc.records(first / "events.jsonl") if e.get("type") == "agent.started")
        self.assertEqual(net.call_args_list[0].args, (pid,))
        self.assertTrue(self.ready.exists())
        self.assertEqual(list(cc.records(self.trace))[-1]["method"], "session/cancel")

    def test_cancel_notification_and_deadline(self):
        for sig, behavior in ((signal.SIGTERM, "ignore"), (signal.SIGINT, "reply")):
            with self.subTest(sig=sig), patch.dict(os.environ, {"ACP_WAIT": behavior}):
                self.trace.unlink(missing_ok=True)
                self.ready.unlink(missing_ok=True)
                d, meta = self.run_task("--dry-run")
                (d / "result.json").unlink()
                cmd = shlex.split((d / "command.txt").read_text())
                with (d / "events.jsonl").open("w") as log:
                    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                            stderr=subprocess.PIPE, start_new_session=True)
                try:
                    deadline = time.monotonic() + 5
                    while proc.poll() is None and time.monotonic() < deadline:
                        if self.ready.exists() and "fixture reply" in (d / "events.jsonl").read_text():
                            break
                        time.sleep(.02)
                    self.assertTrue(self.ready.exists())
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        self.assertEqual(cc.main(["status", d.name]), 0)
                    self.assertIn('backend="acp"', out.getvalue())
                    self.assertIn('command="git status" exit_code=1', out.getvalue())
                    message = json.loads(out.getvalue().split("agent_message=")[1])
                    self.assertEqual(json.loads(message)["report_markdown"], "fixture reply")
                    with self.assertRaisesRegex(cc.Blocked, "steer_requires_daemon_backend"):
                        cc.steer(d, meta, "follow up")
                    os.killpg(proc.pid, sig)
                    _, err = proc.communicate(timeout=7)
                    self.assertNotEqual(proc.returncode, 0, err)
                    self.assertEqual(list(cc.records(self.trace))[-1],
                                     dict(jsonrpc="2.0", method="session/cancel", params=dict(sessionId="fixture-new")))
                finally:
                    if proc.poll() is None:
                        proc.terminate()
                    proc.communicate(timeout=7)
