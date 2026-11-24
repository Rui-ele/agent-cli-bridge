import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

import cc_delegate as cc
from tests import test_delegate as fixtures


class FilesCLI(unittest.TestCase):
    setUp = fixtures.DelegateCase.setUp
    worker = fixtures.DelegateCase.worker
    run_task = fixtures.DelegateCase.run_task

    def edit(self):
        (self.project / "edit.txt").write_text("before\n")
        (self.project / "remove.txt").write_text("gone\n")
        self.worker("codex", "sys.stdin.read()\n"
                    "Path('edit.txt').write_text('after\\n')\n"
                    "Path('remove.txt').unlink()\n"
                    "Path('added').mkdir()\n"
                    "Path('added/note.txt').write_text('new\\n')\n"
                    f"print(json.dumps({fixtures.ANSWER!r}))")
        return self.run_task("--mode", "write", "--workspace", "files")

    def cli(self, *args, expected=0):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(cc.main(list(args)), expected, output.getvalue())
        return output.getvalue()

    def test_stage_apply_rollback_prune(self):
        d, result = self.edit()
        self.assertEqual(result["file_changes"], {"added": ["added/note.txt"], "deleted": ["remove.txt"],
                                                 "modified": ["edit.txt"]})
        self.assertEqual(result["changed_files"], ["added/note.txt", "edit.txt", "remove.txt"])
        for path in ("added/note.txt", "edit.txt", "remove.txt"):
            self.assertIn(path, self.receipt)
        self.assertIn("pending_apply", self.cli("status", d.name))
        self.assertIn("pending_apply", self.cli("prune", "--older-than", "0"))
        self.assertTrue((self.project / "remove.txt").exists())
        self.assertEqual((self.project / "edit.txt").read_text(), "before\n")
        self.assertFalse((self.project / "added").exists())
        report = (d / "report.md").read_text()
        self.assertIn("-before", report)
        self.assertIn("+after", report)
        self.cli("apply", d.name)
        self.assertIn('workspace_phase="applied"', self.cli("status", d.name))
        self.assertFalse((self.project / "remove.txt").exists())
        self.assertEqual((self.project / "added/note.txt").read_text(), "new\n")
        self.assertEqual((self.project / "edit.txt").read_text(), "after\n")
        self.cli("rollback", d.name)
        self.assertIn("rolled_back", self.cli("status", d.name))
        self.assertEqual((self.project / "edit.txt").read_text(), "before\n")
        self.assertEqual((self.project / "remove.txt").read_text(), "gone\n")
        self.assertFalse((self.project / "added").exists())
        self.cli("prune")
        self.assertTrue(d.exists())
        self.cli("prune", "--older-than", "0")
        self.assertFalse(d.exists())

    def test_mark_discarded_releases_pending_folder(self):
        d, _ = self.edit()
        self.cli("mark", d.name, "--outcome", "discarded")
        self.cli("prune", "--older-than", "0")
        self.assertFalse(d.exists())

    def test_retention_uses_frozen_delivery_and_date_of_apply(self):
        d, _ = self.edit()
        shutil.rmtree(d / "work")
        self.assertIn("pending_apply", self.cli("prune", "--older-than", "0"))
        self.cli("apply", d.name)
        meta = json.loads((d / "meta.json").read_text())
        meta["finished_at"] = "2000-01-01T00:00:00+00:00"
        cc.save_json(d / "meta.json", meta)
        self.cli("prune")
        self.assertTrue(d.exists())
        with patch.object(time, "time", return_value=time.time() + 8 * 86400):
            self.cli("prune")
        self.assertFalse(d.exists())

    def test_symlink_and_capacity_block_before_launch(self):
        note = self.project / "note"
        note.write_text("before")
        link = self.project / "alias"
        link.symlink_to(note)
        for reason in ("symlink", "total size"):
            with self.subTest(reason=reason), patch.dict(os.environ, CC_DELEGATE_FILES_MAX_BYTES="1"):
                if reason == "symlink":
                    os.environ["CC_DELEGATE_FILES_MAX_BYTES"] = "1024"
                d, result = self.run_task("--mode", "write", "--workspace", "files", expected=1)
                self.assertEqual(result["status"], "blocked")
                self.assertIn(reason, result["reason"])
                self.assertFalse((d / "work").exists())
                self.assertFalse((d / "events.jsonl").exists())
            link.unlink(missing_ok=True)

    def test_modes_are_explicit(self):
        _, result = self.run_task("--mode", "write", expected=1)
        self.assertEqual(result["reason"], "write_requires_git_head")
        _, result = self.run_task("--workspace", "files", expected=1)
        self.assertEqual(result["reason"], "files_workspace_requires_write_mode")
        cc.git(self.project, "init", "-q")
        _, result = self.run_task("--workspace", "files", "--mode", "write", expected=1)
        self.assertEqual(result["reason"], "files_workspace_requires_non_git_directory")

    def test_second_writer_refused_during_worker_run(self):
        ready = self.root / "ready"
        release = self.root / "release"
        self.worker("codex", f"sys.stdin.read()\nPath({str(ready)!r}).touch()\n"
                    f"while not Path({str(release)!r}).exists(): time.sleep(0.03)\n"
                    f"print(json.dumps({fixtures.ANSWER!r}))")
        cmd = [sys.executable, cc.__file__, "run", "--to", "codex", "--model", "fake",
               "--cc-session", str(self.session), "--project-dir", str(self.project), "--task", "edit",
               "--mode", "write", "--workspace", "files"]
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        with subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as first:
            try:
                deadline = time.monotonic() + 8
                while not ready.exists() and first.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.03)
                self.assertTrue(ready.is_file())
                _, result = self.run_task("--mode", "write", "--workspace", "files", expected=1)
                self.assertEqual(result["reason"], "files_workspace_busy")
            finally:
                release.touch()
                output, _ = first.communicate(timeout=8)
            self.assertEqual(first.returncode, 0, output)
