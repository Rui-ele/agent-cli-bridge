"""Offline command and settings contracts captured from the original workers."""
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cc_delegate as cc


class DriverSnapshots(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.run_dir = self.root / "task"
        self.gitdir = self.root / "git dir"
        self.scratch = self.root / "scratch"
        self.home = self.root / "home"
        env = patch.dict(os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex")})
        env.start()
        self.addCleanup(env.stop)

    def command(self, worker, backend, mode, **opts):
        defaults = dict(search=False, resume=None, effort=None, include=[])
        args = SimpleNamespace(to=worker, backend=backend, mode=mode, model="test-model", **(defaults | opts))
        roots = (self.gitdir, self.scratch) if mode == "write" else ()
        return cc.worker_command(args, self.cwd, self.run_dir, roots)

    def test_exec_argv(self):
        common = ["-m", "test-model", "--skip-git-repo-check", "--json", "--output-schema",
                  str(self.run_dir / "schema.json"), "-o", str(self.run_dir / "last.txt"),
                  "-c", 'approval_policy="never"']
        writing = ["-c", "sandbox_workspace_write.writable_roots=" +
                   json.dumps([str(self.gitdir), str(self.scratch)]),
                   "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
                   "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true"]
        for mode, search, resume, effort in itertools.product(
                ("ro", "write"), (False, True), (None, "session"), (None, "high")):
            with self.subTest(mode=mode, search=search, resume=resume, effort=effort):
                sandbox = {"ro": "read-only", "write": "workspace-write"}[mode]
                expected = (["codex"] + (["--search"] if search else []) +
                            ["exec", "-s", sandbox, "-C", str(self.cwd)] +
                            (["resume", "-c", f'sandbox_mode="{sandbox}"'] if resume else []) +
                            common + (writing if mode == "write" else []) +
                            (["-c", 'model_reasoning_effort="high"'] if effort else []) +
                            (["--", "session"] if resume else []) + ["-"])
                self.assertEqual(self.command("codex", "exec", mode, search=search,
                                              resume=resume, effort=effort), expected)

    def test_daemon_argv(self):
        for mode, resume in itertools.product(("ro", "write"), (None, "session")):
            with self.subTest(mode=mode, resume=resume):
                expected = [sys.executable, str(Path(cc.__file__).resolve()), "codex-daemon-worker",
                            "--socket", str(self.home / ".codex/app-server-control/app-server-control.sock"),
                            "--cwd", str(self.cwd), "--model", "test-model", "--mode", mode,
                            "--schema", str(self.run_dir / "schema.json"), "--thread-name", "task",
                            "-o", str(self.run_dir / "last.txt")]
                if mode == "write":
                    expected += ["--writable", str(self.gitdir), "--writable", str(self.scratch),
                                 "--tmpdir", str(self.scratch)]
                expected += ["--effort", "high"] + (["--resume", "session"] if resume else []) + ["-"]
                self.assertEqual(self.command("codex", "daemon", mode, resume=resume, effort="high"), expected)

    def test_agy_argv(self):
        dirs = ["--add-dir", str(self.root / "shared"), "--add-dir", str(self.root / "second dir")]
        includes = ["local", "sub/local", "../shared/a", "../second dir/b", "../shared/c"]
        for mode, resume, include in itertools.product(("ro", "write"), (None, "session"), ([], includes)):
            with self.subTest(mode=mode, resume=resume, include=include):
                expected = ["agy", "--input-format", "stream-json", "--output-format", "stream-json",
                            "--model", "test-model", "--json-schema", str(self.run_dir / "schema.json"),
                            "--disable-slash-commands", "--mode", {"ro": "plan", "write": "accept-edits"}[mode],
                            "--settings", str(self.run_dir / "settings.json"), "--print-timeout", "0",
                            "--log-file", str(self.run_dir / "agy.log")]
                expected += ["--sandbox"] if mode == "write" else dirs if include else []
                expected += ["--effort", "high"] + (["--conversation", "session"] if resume else [])
                self.assertEqual(self.command("agy", "daemon", mode, resume=resume, effort="high",
                                              include=include), expected)

    def test_agy_settings(self):
        (self.cwd / ".git").mkdir()
        protected = [f"read_file({self.home / p})" for p in (
            ".aws", ".codex/auth.json", ".config/gcloud", ".docker", ".git-credentials",
            ".gnupg", ".kube", ".netrc", ".npmrc", ".pypirc", ".ssh")]
        common = protected + [
            r"command(regex:(?:.*/)?(?:ba|da|z|k)?sh -c .*\b(?:curl|wget|nc|ncat|netcat|scp|sftp|ssh|rsync)\b.*)",
            r"command(regex:(?:.*/)?env (?:[^\s]*/)?(?:curl|wget|nc|ncat|netcat|scp|sftp|ssh|rsync))",
            r"command(regex:(?:[^\s]*/)?(?:curl|wget|nc|ncat|netcat|scp|sftp|ssh|rsync))",
            "execute_url(*)", "mcp(*)", f"read_file({self.cwd / '.git'})"]
        clone = self.root / "clone"
        for mode, cwd, extra in [
                ("ro", self.cwd, ["command(*)", "write_file(*)"]),
                ("write", clone, [f"read_file({clone / '.git'})", f"read_file({self.cwd})",
                                  f"write_file({self.cwd})"])]:
            with self.subTest(mode=mode):
                self.assertEqual(cc.agy_settings(cwd, mode, self.cwd),
                                 {"permissions": {"deny": sorted(common + extra)}})

    def test_agy_acp_profile_bytes(self):
        self.run_dir.mkdir()
        tmp = self.root / 't"mp\\dir'
        common = [self.home / ".gemini", tmp, Path("/dev"), Path("/private/var/folders")]
        with patch.dict(os.environ, {"TMPDIR": str(tmp)}):
            for mode in ("ro", "write"):
                with self.subTest(mode=mode):
                    self.command("agy", "acp", mode)
                    paths = common + ([self.cwd, self.gitdir, self.scratch] if mode == "write" else [])
                    paths = sorted(str(p.resolve()) for p in paths)
                    golden = ("(version 1)\n(allow default)\n(deny file-write*)\n(allow file-write*\n" +
                              "\n".join("    (subpath " + json.dumps(p, ensure_ascii=False) + ")" for p in paths) + ")\n")
                    self.assertEqual((self.run_dir / "sandbox.sb").read_bytes(), golden.encode())

    def test_claude_argv(self):
        self.run_dir.mkdir()
        cc.save_json(self.run_dir / "schema.json", cc.SCHEMA)
        for mode, resume, effort in itertools.product(("ro", "write"), (None, "session"), (None, "high")):
            with self.subTest(mode=mode, resume=resume, effort=effort):
                expected = [str(cc.SEATBELT), "-f", str(self.run_dir / "sandbox.sb"), "claude", "-p",
                            "--model", "test-model", "--output-format", "stream-json", "--verbose",
                            "--json-schema", (self.run_dir / "schema.json").read_text(),
                            "--permission-prompts", "none", "--settings", '{"sandbox":{"enabled":false}}']
                if mode == "ro":
                    expected += ["--permission-mode", "dontAsk", "--allowedTools", "Read", "Grep", "Glob",
                                 "Bash", "WebSearch", "WebFetch", "--add-dir", str(self.root / "shared")]
                else:
                    expected += ["--permission-mode", "bypassPermissions"]
                expected += (["--effort", "high"] if effort else []) + (["--resume", "session"] if resume else [])
                self.assertEqual(self.command("claude", "print", mode, resume=resume, effort=effort,
                                              include=["local", "../shared/a", "../shared/b"]), expected)
