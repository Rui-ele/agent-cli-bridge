import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import cc_delegate as cc


class ClaudeCase(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])))
        self.bin = self.root / "bin"
        self.home = self.root / 'home.[a]+("q")\\x'
        self.project = self.root / "project"
        self.tmp = self.root / "tmp"
        for path in (self.bin, self.home, self.project, self.tmp):
            path.mkdir()
        self.session = self.root / "session.jsonl"
        self.session.touch()
        self.trace = self.root / "trace"
        sandbox = self.bin / "sandbox-exec"
        sandbox.write_text(f"#!{sys.executable}\nimport os, sys\nfrom pathlib import Path\n"
                           "assert sys.argv[1] == '-f'\n"
                           "Path(os.environ['FIXTURE_TRACE'] + '.profile').write_text(sys.argv[2])\n"
                           "os.execvp(sys.argv[3], sys.argv[3:])\n")
        self.claude = self.bin / "claude"
        self.claude.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fixtures/fake_claude.py").read_text())
        for path in (sandbox, self.claude):
            path.chmod(0o700)
        self.removed = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_AGENT_SDK_TEST",
                        "CLAUDE_PREVIEW_TEST", "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SESSION_ID")
        self.kept = dict(CLAUDE_CODE_USE_BEDROCK="fixture", CLAUDE_CODE_OAUTH_TOKEN="fake-token",
                         ANTHROPIC_BASE_URL="fixture", CLAUDE_CONFIG_DIR=str(self.home / 'cc."config'))
        stack.enter_context(patch.dict(os.environ, dict(
            PATH=str(self.bin) + os.pathsep + os.defpath, HOME=str(self.home), TMPDIR=str(self.tmp),
            CC_DELEGATE_HOME=str(self.root / "cc"), CC_DELEGATE_DEPTH="1", FIXTURE_TRACE=str(self.trace),
            GIT_CEILING_DIRECTORIES=str(self.root), GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull, **dict.fromkeys(self.removed, "parent"), **self.kept), clear=True))
        stack.enter_context(patch.object(cc, "SEATBELT", sandbox))
        stack.enter_context(patch.object(cc.sys, "platform", "darwin"))
        stack.enter_context(patch.object(cc, "start_net_monitor", return_value=None))
        self.args = ["run", "--to", "claude", "--model", "fixture", "--project-dir", str(self.project),
                     "--cc-session", str(self.session), "--task", "fixture task"]

    def run_task(self, *args, expected=0):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cc.main(self.args + list(args)), expected, out.getvalue())
        self.receipt = out.getvalue()
        d = Path(json.loads(self.receipt.splitlines()[-1].split("=", 1)[1]))
        return d, json.loads((d / "result.json").read_text())

    def check_profile(self, d, config, extra=()):
        self.assertEqual(self.trace.with_suffix(".profile").read_text(), str(d / "sandbox.sb"))
        text = (d / "sandbox.sb").read_text()
        prefix = "(version 1)\n(allow default)\n(deny file-write*)\n(allow file-write*\n"
        self.assertTrue(text.startswith(prefix) and text.endswith(")\n"))
        paths, regexes = [], []
        for line in text[len(prefix):-2].splitlines():
            kind, literal = re.fullmatch(r"    \((subpath|regex) (.*)\)", line).groups()
            (paths if kind == "subpath" else regexes).append(json.loads(literal))
        states = ("projects sessions session-env shell-snapshots state cache file-history todos plans "
                  "debug statsig telemetry backups paste-cache ide").split()
        expected = [config / s for s in states] + [self.tmp, Path("/dev"), Path("/private/var/folders"),
                                                   Path(f"/private/tmp/claude-{os.getuid()}"), *extra]
        self.assertCountEqual(paths, map(str, (p.resolve() for p in expected)))
        self.assertEqual(len(regexes), 2)
        marker = r"^/private/tmp/claude-[0-9a-f]+-cwd$"
        regexes.remove(marker)
        pattern, = regexes
        for suffix in ("", ".lock", ".backup"):
            self.assertTrue(re.match(pattern, str(self.home / (".claude.json" + suffix))))
        for wrong in (self.home / "xclaude.json", self.home / ".claudeXjson", self.root / ".claude.json"):
            self.assertIsNone(re.search(pattern, str(wrong)))
        self.assertIsNone(re.search(pattern, "prefix" + str(self.home / ".claude.json")))

    def test_readonly(self):
        outside = self.root / "file"
        outside.write_text("included")
        d, meta = self.run_task("--effort", "high", "--resume", "old-session", "--include", str(outside))
        self.assertEqual((meta["status"], meta["backend"], meta["worker_session_id"]), ("ok", "print", "old-session"))
        self.assertIn('resume_command="claude --resume old-session"', self.receipt)
        self.assertEqual((meta["context_window"], meta["window_estimated"]), (200000, True))
        self.assertEqual(meta["worker_usage"], dict(input_tokens=5, output_tokens=7, cache_creation_input_tokens=11,
                         cache_read_input_tokens=13, total_tokens=36, cost_usd=0.25,
                         actual_models=["fixture-model", "fixture-secondary"]))
        self.assertEqual(meta["permission_denials"], 1)
        self.assertIn('"permission_denied"', (d / "events.jsonl").read_text())
        self.assertEqual((d / "report.md").read_text(), "structured")
        trace = json.loads(self.trace.read_text())
        self.assertEqual(trace["stdin"], (d / "package.md").read_text())
        self.assertEqual(trace["argv"], ["-p", "--model", "fixture", "--output-format", "stream-json", "--verbose",
                         "--json-schema", (d / "schema.json").read_text(), "--permission-prompts", "none",
                         "--settings", '{"sandbox":{"enabled":false}}', "--permission-mode", "dontAsk",
                         "--allowedTools", "Read", "Grep", "Glob", "Bash", "WebSearch", "WebFetch",
                         "--add-dir", str(self.root), "--effort", "high", "--resume", "old-session"])
        self.assertFalse(set(self.removed).intersection(trace["env"]))
        self.assertEqual({k: trace["env"][k] for k in self.kept}, self.kept)
        self.assertEqual((trace["env"]["CC_DELEGATE_DEPTH"], trace["env"]["TMPDIR"]), ("2", str(self.tmp)))
        self.check_profile(d, Path(self.kept["CLAUDE_CONFIG_DIR"]))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.running_status(d, meta)
        self.assertIn('command="git status" exit_code=1', out.getvalue())
        self.assertIn('agent_message="checking"', out.getvalue())

    def test_write(self):
        cc.git(self.project, "init", "-q")
        cc.git(self.project, "-c", "user.name=fixture", "-c", "user.email=f@example.invalid",
               "commit", "--allow-empty", "-qm", "base")
        del os.environ["CLAUDE_CONFIG_DIR"]
        d, meta = self.run_task("--mode", "write", "--backend", "exec", "--model", "fixture[1m]")
        clone = Path(meta["worktree"])
        scratch = clone.parent / (".tmp-" + d.name)
        self.check_profile(d, self.home / ".claude", [clone, clone.parent / (".git-" + d.name), scratch])
        trace = json.loads(self.trace.read_text())
        self.assertEqual(trace["argv"][-2:], ["--permission-mode", "bypassPermissions"])
        self.assertEqual(trace["env"]["TMPDIR"], str(scratch))
        self.assertEqual((meta["status"], meta["backend"], meta["context_window"], meta["window_estimated"]),
                         ("ok", "print", 1000000, True))

    def test_result_variants(self):
        for scenario in ("error", "max_turns", "text", "loop"):
            failure = scenario in ("error", "max_turns")
            with self.subTest(scenario=scenario), patch.dict(os.environ, {"FIXTURE_SCENARIO": scenario}):
                d, meta = self.run_task("--backend", "print", expected=2 if failure else 0)
                self.assertEqual(meta["worker"], "claude")
                self.assertEqual(meta["status"], "outcome_unknown" if failure else "ok")
                self.assertIn("resume_command", meta)
                self.assertEqual(meta["loop_warning"], scenario == "loop")
                if scenario == "text":
                    self.assertEqual((d / "report.md").read_text(), "fixture report")

    def test_depth(self):
        for worker, choices in cc.DRIVERS.items():
            for driver in choices.values():
                self.assertEqual(driver.environment(())["CC_DELEGATE_DEPTH"], "2")
            with self.subTest(worker=worker), patch.dict(os.environ, {"CC_DELEGATE_MAX_DEPTH": "1"}):
                d, meta = self.run_task("--to", worker, expected=1)
                self.assertEqual((meta["status"], meta["reason"]), ("blocked", "delegation_depth_exceeded"))
                self.assertFalse((d / "command.txt").exists())
        with patch.dict(os.environ, {"CC_DELEGATE_DEPTH": "2"}):
            _, meta = self.run_task(expected=1)
            self.assertEqual(meta["reason"], "delegation_depth_exceeded")
            with patch.object(Path, "mkdir", side_effect=PermissionError), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cc.main(self.args), 1)
                self.assertEqual(err.getvalue().strip(), "delegation_depth_exceeded")

    def test_loop_uses_call_order_arguments_and_result(self):
        def use(i, name, params):
            return dict(type="tool_use", id=str(i), name=name, input=params)

        def result(i, text, error):
            return dict(type="tool_result", tool_use_id=str(i), content=text, is_error=error)

        args = dict(file_path="a", offset=0)
        for varying in ("none", "tool", "params", "content", "error"):
            with self.subTest(varying=varying):
                events = cc.WorkerEvents("claude")
                other = cc.WorkerEvents("claude")
                calls = [use(0, "Read", args), use(1, "Glob" if varying == "tool" else "Read",
                         dict(offset=1 if varying == "params" else 0, file_path="a")), use(2, "Read", args)]
                results = [result(i, "different" if i == 1 and varying == "content" else "same",
                                  i == 1 and varying == "error") for i in (1, 2, 0)]
                events.observe(dict(type="assistant", message=dict(content=calls)))
                for row in results:
                    response = dict(type="user", message=dict(content=[row]))
                    events.observe(response)
                    other.observe(response)
                self.assertEqual(events.loop_warning, varying == "none")
                self.assertFalse(other.loop_warning)

        events = cc.WorkerEvents("claude")
        calls = [use(i, name, args) for i, name in enumerate(("Read", "Glob", "Read", "Read"))]
        events.observe(dict(type="assistant", message=dict(content=calls)))
        events.observe(dict(type="user", message=dict(content=[result(i, "same", False) for i in (1, 0, 2, 3)])))
        self.assertFalse(events.loop_warning)

    def test_unavailable(self):
        for change in (patch.object(cc.sys, "platform", "linux"), patch.object(cc, "SEATBELT", self.bin / "absent")):
            with change:
                _, meta = self.run_task(expected=1)
                self.assertEqual((meta["status"], meta["reason"]), ("blocked", "os_sandbox_unavailable"))
        self.claude.unlink()
        _, meta = self.run_task(expected=1)
        self.assertEqual(meta["status"], "failed")
        self.assertIn("claude", meta["reason"])
        self.assertFalse(self.trace.exists())

    def test_invalid_backend(self):
        for backend in ("daemon", "acp"):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                cc.main(self.args + ["--backend", backend])

    def test_control(self):
        # A separate supervisor keeps its signal handlers on the main thread.
        script = ("import sys; from pathlib import Path; import cc_delegate as c; "
                  "c.sys.platform='darwin'; c.SEATBELT=Path(sys.argv.pop(1)); "
                  "c.start_net_monitor=lambda pid: None; sys.exit(c.main())")
        with patch.dict(os.environ, {"FIXTURE_SCENARIO": "wait"}):
            proc = subprocess.Popen([sys.executable, "-c", script, str(cc.SEATBELT), *self.args],
                                    cwd=Path(cc.__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not self.trace.with_suffix(".ready").exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(self.trace.with_suffix(".ready").exists())
            d = Path(self.trace.with_suffix(".profile").read_text()).parent
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cc.main(["status", d.name]), 0)
                self.assertIn('command="git status" exit_code=1', out.getvalue())
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(cc.main(["steer", d.name, "--message", "next"]), 1)
                    self.assertIn("steer_requires_daemon_backend", err.getvalue())
                self.assertEqual(cc.main(["cancel", d.name, "--wait", "0"]), 0)
            _, err = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 2, err)
            meta = json.loads((d / "result.json").read_text())
            self.assertEqual((meta["status"], meta["exit_code"]), ("cancelled", 0))
            self.assertIn("resume_command", meta)
            self.assertIn(("SIGINT", "sent"), [(r["action"], r["result"]) for r in cc.records(d / "control.jsonl")])
        finally:
            if proc.poll() is None:
                proc.terminate()
            proc.communicate(timeout=20)
