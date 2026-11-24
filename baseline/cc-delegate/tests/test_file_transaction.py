import hashlib
import json
import os
import unittest
from unittest import mock

import file_transaction as tx
from tests import temp_workspace


class FileTransactionTests(unittest.TestCase):
    def setUp(self):
        root = temp_workspace(self)
        self.project = root / "project"
        self.project.mkdir()
        self.run = root / "run"
        self.delivery = self.run / "delivery"
        self.delivery.mkdir(parents=True)
        info = self.project.stat()
        self.meta = {"project_dir": str(self.project), "project_identity": [info.st_dev, info.st_ino]}
        self.changes = {}
        self.add_change("modify", b"old", b"new", 0o640, 0o600)
        self.add_change("remove", b"remove", None)
        self.add_change("folder/sub/add", None, b"\xff")

    def add_change(self, rel, before, after, old_mode=0o644, new_mode=0o644):
        change = {}
        for side, content, root, mode in (("before", before, self.project, old_mode),
                                          ("after", after, self.delivery, new_mode)):
            change[side] = None
            if content is not None:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                path.chmod(mode)
                change[side] = {"type": "file", "mode": mode, "size": len(content),
                                "sha256": hashlib.sha256(content).hexdigest()}
        self.changes[rel] = change
        (self.run / "changes.json").write_text(json.dumps(self.changes))

    def act(self, action):
        return tx.transact(self.run, self.meta, action)

    def test_apply_and_rollback(self):
        for _ in range(2):
            self.assertEqual(self.act("apply"), "applied")
        self.assertEqual((self.project / "modify").read_bytes(), b"new")
        self.assertEqual((self.project / "modify").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.project / "remove").exists())
        self.assertEqual((self.project / "folder/sub/add").read_bytes(), b"\xff")
        self.assertEqual((self.run / "backup/remove").read_bytes(), b"remove")
        for _ in range(2):
            self.assertEqual(self.act("rollback"), "rolled_back")
        self.assertEqual((self.project / "modify").read_bytes(), b"old")
        self.assertEqual((self.project / "modify").stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.project / "remove").read_bytes(), b"remove")
        self.assertFalse((self.project / "folder").exists())

    def test_apply_lists_all_conflicts_before_changes(self):
        (self.project / "remove").chmod(0o600)
        added = self.project / "folder/sub/add"
        added.parent.mkdir(parents=True)
        added.write_bytes(b"\xff")
        with self.assertRaises(tx.TransactionError) as error:
            self.act("apply")
        self.assertIn("remove", str(error.exception))
        self.assertIn("folder/sub/add", str(error.exception))
        self.assertEqual((self.project / "modify").read_bytes(), b"old")
        self.assertEqual(added.read_bytes(), b"\xff")
        self.assertFalse((self.run / "backup").exists())

    def test_rollback_detects_later_edits_before_restoring(self):
        self.act("apply")
        added = self.project / "folder/sub/add"
        added.write_bytes(b"later edit")
        with self.assertRaisesRegex(tx.TransactionError, "folder/sub/add"):
            self.act("rollback")
        self.assertEqual((self.project / "modify").read_bytes(), b"new")
        self.assertFalse((self.project / "remove").exists())
        self.assertEqual(added.read_bytes(), b"later edit")

    def test_restart_after_replace_before_log(self):
        replace = os.replace

        def interrupted(*args, **kwargs):
            replace(*args, **kwargs)
            raise KeyboardInterrupt

        for action, active, final in (("apply", "applying", "applied"),
                                       ("rollback", "rolling_back", "rolled_back")):
            with mock.patch.object(tx.os, "replace", side_effect=interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    self.act(action)
            self.assertEqual(tx.phase(self.run, None), active)
            ino = (self.project / "modify").stat().st_ino
            self.assertEqual(self.act(action), final)
            self.assertEqual((self.project / "modify").stat().st_ino, ino)
        self.assertEqual((self.project / "modify").read_bytes(), b"old")
        self.assertFalse((self.project / "folder").exists())

    def test_restart_does_not_overwrite_subsequent_edits(self):
        real_unlink = os.unlink

        def interrupted(path, **kwargs):
            if path == "remove":
                raise KeyboardInterrupt
            real_unlink(path, **kwargs)

        with mock.patch.object(tx.os, "unlink", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.act("apply")
        (self.project / "modify").write_bytes(b"old")
        with self.assertRaisesRegex(tx.TransactionError, "modify"):
            self.act("apply")
        self.assertTrue((self.project / "remove").is_file())

    def test_symlink_in_destination_parent_rejected(self):
        outside = self.run.parent / "outside"
        outside.mkdir()
        (self.project / "folder").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(tx.TransactionError, "folder/sub/add"):
            self.act("apply")
        self.assertEqual((self.project / "modify").read_bytes(), b"old")
        self.assertEqual(list(outside.iterdir()), [])

    def test_rollback_keeps_directory_with_unrelated_file(self):
        self.act("apply")
        extra = self.project / "folder/sub/user"
        extra.write_text("user file")
        self.act("rollback")
        self.assertEqual(extra.read_text(), "user file")
        self.assertFalse((self.project / "folder/sub/add").exists())

    def test_rollback_keeps_replacement_directory(self):
        self.act("apply")
        sub = self.project / "folder/sub"
        saved = self.run / "original-dir"
        sub.rename(saved)
        sub.mkdir()
        (saved / "add").rename(sub / "add")
        self.act("rollback")
        self.assertTrue(sub.is_dir())
        self.assertEqual(list(sub.iterdir()), [])

    def test_changed_payload_refused_before_touching_originals(self):
        (self.delivery / "folder/sub/add").write_bytes(b"corrupt")
        with self.assertRaisesRegex(tx.TransactionError, "content changed"):
            self.act("apply")
        self.assertEqual((self.project / "modify").read_bytes(), b"old")

    def test_replaced_root_is_rejected(self):
        self.project.rename(self.project.with_name("old-root"))
        self.project.mkdir()
        with self.assertRaisesRegex(tx.TransactionError, "directory replaced"):
            self.act("apply")
        self.assertEqual(list(self.project.iterdir()), [])
