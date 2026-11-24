import hashlib
import json
import os
import unittest
from unittest.mock import patch

import cc_delegate as cc
import file_workspace as files
from tests import temp_workspace


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        root = temp_workspace(self)
        self.project, self.run = root / "project", root / "run"
        self.project.mkdir()
        self.run.mkdir()

    def prepare(self):
        return files.prepare(self.project, self.run, cc.sensitive)

    def compare(self):
        return files.compare(self.run, cc.sensitive)

    def test_manifest_modes_and_frozen_change_report(self):
        (self.project / "sub").mkdir()
        original = self.project / "sub/a"
        original.write_text("original\n")
        original.chmod(0o750)
        (self.project / "remove").write_text("old")
        (self.project / "keep").write_text("same\n")
        work = self.prepare()
        (work / "sub/a").write_text("edited\n")
        (work / "remove").unlink()
        (work / "bin").write_bytes(b"hidden binary contents\0")
        (work / "large").write_bytes(b"x" * (files.TEXT_LIMIT + 1))
        groups, diff = files.compare(self.run, cc.sensitive, report_prefix="Worker summary.")
        report = diff.read_text()
        self.assertTrue(report.startswith("Worker summary.\n\n## Workspace diff"))
        self.assertEqual(groups, dict(added=["bin", "large"], deleted=["remove"], modified=["sub/a"]))
        baseline = json.loads((self.run / "baseline.json").read_text())
        self.assertEqual(baseline["sub/a"], dict(type="file", mode=0o750, size=9,
                         sha256=hashlib.sha256(b"original\n").hexdigest()))
        self.assertEqual(original.read_text(), "original\n")
        self.assertIn("-original\n", report)
        self.assertIn("+edited\n", report)
        self.assertIn(hashlib.sha256(b"hidden binary contents\0").hexdigest(), report)
        self.assertNotIn("hidden binary contents", report)
        self.assertLess(len(report), 4000)
        (work / "sub/a").write_text("later change\n")
        self.assertEqual((self.run / "delivery/sub/a").read_text(), "edited\n")
        self.assertFalse((self.run / "delivery/keep").exists())

    def test_clone_failure_falls_back_to_independent_byte_copies(self):
        (self.project / "note").write_text("original")
        with patch.object(files, "_fclonefileat", return_value=-1) as clone:
            work = self.prepare()
        self.assertEqual(clone.call_count, 2)
        (work / "note").write_text("after")
        self.assertEqual((self.project / "note").read_text(), "original")
        self.assertEqual((self.run / "base/note").read_text(), "original")

    def test_special_sensitive_and_linked_paths_block_before_copy(self):
        target = self.project / "note"
        target.write_text("example")
        for kind in ("symlink", "fifo", "hardlink", ".env", ".git"):
            with self.subTest(kind=kind):
                path = self.project / kind
                if kind == "symlink":
                    path.symlink_to(target)
                elif kind == "fifo":
                    os.mkfifo(path)
                elif kind == "hardlink":
                    os.link(target, path)
                else:
                    path.touch()
                with self.assertRaises(files.WorkspaceError):
                    self.prepare()
                self.assertEqual(list(self.run.iterdir()), [])
                path.unlink()

    def test_limits_reject_before_copy(self):
        for name in ("a", "b"):
            (self.project / name).write_text("abc")
        for var, reason in (("CC_DELEGATE_FILES_MAX_COUNT", "file count"),
                            ("CC_DELEGATE_FILES_MAX_BYTES", "total size")):
            with self.subTest(var=var), patch.dict(os.environ, {var: "1"}):
                with self.assertRaisesRegex(files.WorkspaceError, reason):
                    self.prepare()
                self.assertEqual(list(self.run.iterdir()), [])

    def test_dry_run_checks_tree_without_snapshots(self):
        (self.project / "note").touch()
        self.assertEqual(files.prepare(self.project, self.run, cc.sensitive, dry_run=True), self.run / "work")
        self.assertEqual(list(self.run.iterdir()), [])
        (self.project / "link").symlink_to(self.project)
        with self.assertRaisesRegex(files.WorkspaceError, "symlink"):
            files.prepare(self.project, self.run, cc.sensitive, dry_run=True)
        self.assertEqual(list(self.run.iterdir()), [])

    def test_non_utf8_name_cannot_enter_journal(self):
        with self.assertRaisesRegex(files.WorkspaceError, "UTF-8"):
            files._check_aliases(["name\udcff"])

    def test_worker_links_and_type_changes_cannot_enter_delivery(self):
        (self.project / "note").write_text("original")
        work = self.prepare()
        (work / "sub").symlink_to(self.project, target_is_directory=True)
        with self.assertRaisesRegex(files.WorkspaceError, "symlink"):
            self.compare()
        (work / "sub").unlink()
        (work / "note").unlink()
        (work / "note").mkdir()
        with self.assertRaisesRegex(files.WorkspaceError, "file/directory"):
            self.compare()
        self.assertFalse((self.run / "changes.json").exists())

    def test_case_only_directory_rename_does_not_change_delivery_target(self):
        (self.project / "notes").mkdir()
        (self.project / "notes" / "old").touch()
        work = self.prepare()
        (work / "notes").rename(work / "Notes")
        (work / "Notes/old").rename(work / "Notes/new")
        with self.assertRaisesRegex(files.WorkspaceError, "differ only by case"):
            self.compare()
        self.assertFalse((self.run / "changes.json").exists())
