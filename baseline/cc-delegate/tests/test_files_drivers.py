import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import sys
import unittest
from unittest.mock import patch

import cc_delegate as cc
from tests import temp_workspace


class FileWorkspaceDrivers(unittest.TestCase):
    def setUp(self):
        self.root = temp_workspace(self)
        self.project = self.root / "notes"
        self.project.mkdir()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        env = dict(HOME=str(self.root), PATH=os.defpath, TMPDIR=str(self.project),
                   CC_DELEGATE_HOME=str(self.root / "state"), GIT_CEILING_DIRECTORIES=str(self.root),
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
            env[key] = str(self.root / key.lower())
        self.stack.enter_context(patch.dict(os.environ, env, clear=True))
        self.stack.enter_context(patch.object(cc.shutil, "which", return_value=str(self.root / "fake-cli")))
        self.stack.enter_context(patch.object(cc.ClaudeSDK, "interpreter", return_value=sys.executable))
        self.stack.enter_context(patch.object(cc, "seatbelt_available", return_value=True))

    def preview(self, worker, backend, expected=0):
        args = ["run", "--caller", "codex", "--to", worker, "--backend", backend,
                "--mode", "write", "--workspace", "files", "--task", "fixture", "--model", "test",
                "--project-dir", str(self.project), "--dry-run"]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cc.main(args), expected, out.getvalue())
        return Path(json.loads(out.getvalue().splitlines()[-1].partition("=")[2]))

    def test_codex_exec_and_daemon(self):
        for backend in ("exec", "daemon"):
            with self.subTest(backend=backend):
                d = self.preview("codex", backend)
                cmd = shlex.split((d / "command.txt").read_text())
                cwd_flag = "-C" if backend == "exec" else "--cwd"
                self.assertEqual(Path(cmd[cmd.index(cwd_flag) + 1]), d / "work")
                if backend == "exec":
                    self.assertIn("sandbox_workspace_write.writable_roots=" + json.dumps([str(d / "scratch")]), cmd)
                    self.assertIn("sandbox_workspace_write.exclude_slash_tmp=true", cmd)
                    self.assertIn("sandbox_workspace_write.exclude_tmpdir_env_var=true", cmd)
                else:
                    self.assertEqual([cmd[i + 1] for i, v in enumerate(cmd) if v == "--writable"],
                                     [str(d / "scratch")])
                self.assertNotIn(str(self.project), cmd)

    def test_agy_and_claude_seatbelt(self):
        for worker, backend in (("agy", "exec"), ("agy", "acp"), ("claude", "print"), ("claude", "sdk")):
            with self.subTest(worker=worker, backend=backend):
                d = self.preview(worker, backend)
                cmd = shlex.split((d / "command.txt").read_text())
                work, scratch = d / "work", d / "scratch"
                profile = d / "sandbox.sb"
                text = profile.read_text()
                _, allows = text.split("(deny file-write*)\n(allow file-write*\n")
                allows, _, denials = allows.partition("\n(deny file-write* ")
                roots = {Path(json.loads(path)) for path in re.findall(r'\(subpath (.+?)\)', allows)}
                expected = {work, scratch, Path("/dev").resolve()}
                if worker == "agy":
                    expected.add(self.root / ".gemini")
                    self.assertNotIn("(regex ", allows)
                else:
                    config = self.root / ".claude"
                    states = ("sessions session-env shell-snapshots state cache file-history todos plans "
                              "debug statsig telemetry backups paste-cache ide").split()
                    expected.update(config / state for state in states)
                    expected.add(config / "projects" / cc.claude_project_name(work))
                    pattern, = [json.loads(s) for s in re.findall(r'^    \(regex (".*")\)', allows, re.M)]
                    self.assertRegex(str(self.root / ".claude.json"), pattern)
                    self.assertRegex(str(self.root / ".claude.json.lock"), pattern)
                    self.assertNotRegex(str(self.root / ".claude.json/child"), pattern)
                self.assertEqual(roots, expected)
                self.assertIn(f"(require-all (subpath {cc.dumps(str(d.parent))}) "
                              f"(require-not (subpath {cc.dumps(str(work))})) "
                              f"(require-not (subpath {cc.dumps(str(scratch))})))", denials)
                for root in (work, scratch):
                    self.assertIn(f"(deny file-write* (literal {cc.dumps(str(root))}))", denials)
                self.assertIn(f"(deny file-write* (subpath {cc.dumps(str(self.project))}))", denials)
                if backend == "sdk":
                    launcher = Path(cmd[cmd.index("--cli-path") + 1]).read_text()
                    self.assertIn(shlex.join([str(cc.SEATBELT), "-f", str(profile)]), launcher)
                elif backend == "acp":
                    self.assertEqual(cmd[cmd.index("--profile") + 1], str(profile))
                else:
                    self.assertEqual(cmd[:3], [str(cc.SEATBELT), "-f", str(profile)])
                if worker == "agy" and backend == "exec":
                    self.assertEqual(Path(cmd[cmd.index("--log-file") + 1]).parent, scratch)
                env = cc.driver_for(worker, backend).environment((scratch,), "files")
                self.assertEqual((env["HOME"], env["TMPDIR"]), (str(self.root), str(scratch)))
                self.assertEqual({k: v for k, v in env.items() if k.startswith("XDG_")},
                                 {k: v for k, v in os.environ.items() if k.startswith("XDG_")})
                if worker == "claude":
                    self.assertNotIn("CLAUDE_CONFIG_DIR", env)
                    self.assertEqual(env["CLAUDE_CODE_TMPDIR"], str(scratch))

    def test_source_and_receipts_under_state_dirs_stay_protected(self):
        for worker, state in (("agy", self.root / ".gemini"), ("claude", self.root / "custom" / "cache")):
            with self.subTest(worker=worker), patch.dict(os.environ, CC_DELEGATE_HOME=str(state),
                                                         CLAUDE_CONFIG_DIR=str(self.root / "custom")):
                self.project = state / "notes"
                self.project.mkdir(parents=True)
                d = self.preview(worker, "exec" if worker == "agy" else "print")
                text = (d / "sandbox.sb").read_text()
                self.assertIn(f"    (subpath {cc.dumps(str(state))})", text)
                self.assertIn(f"(deny file-write* (subpath {cc.dumps(str(self.project))}))", text)
                self.assertIn(f"(deny file-write* (require-all (subpath {cc.dumps(str(d.parent))})", text)
                self.assertIn(f"(deny file-write* (subpath {cc.dumps(str(state / 'delegate-log.jsonl'))}))", text)

    def test_agy_print_requires_os_sandbox(self):
        with patch.object(cc, "seatbelt_available", return_value=False):
            d = self.preview("agy", "exec", expected=1)
        self.assertEqual(json.loads((d / "meta.json").read_text())["reason"], "os_sandbox_unavailable")
