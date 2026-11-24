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
                self.assertIn("(deny file-write*)", text)
                roots = {Path(json.loads(path)) for path in re.findall(r'\(subpath (.+?)\)', text)}
                self.assertEqual(roots, {work, scratch, Path("/dev").resolve()})
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
                self.assertEqual((env["HOME"], env["TMPDIR"]), (str(scratch), str(scratch)))
                if worker == "claude":
                    self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(scratch / ".claude"))

    def test_agy_print_requires_os_sandbox(self):
        with patch.object(cc, "seatbelt_available", return_value=False):
            d = self.preview("agy", "exec", expected=1)
        self.assertEqual(json.loads((d / "meta.json").read_text())["reason"], "os_sandbox_unavailable")
