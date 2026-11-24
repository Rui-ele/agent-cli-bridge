import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
import shlex
import unittest
from unittest.mock import patch

import cc_delegate as cc
from tests import temp_workspace


class PruneCase(unittest.TestCase):
    def setUp(self):
        self.root = temp_workspace(self)
        env = patch.dict(os.environ, {"CC_DELEGATE_HOME": str(self.root / "home"),
                                      "GIT_CEILING_DIRECTORIES": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        cc.git(self.repo, "init", "-q", "-b", "main")
        for key, value in (("user.name", "fixture"), ("user.email", "fixture@example.invalid")):
            cc.git(self.repo, "config", key, value)
        cc.git(self.repo, "commit", "--allow-empty", "-qm", "base")
        self.base = cc.git(self.repo, "rev-parse", "HEAD").strip()
        self.runs = cc.home() / "delegate-runs"
        self.runs.mkdir(parents=True)
        self.n = 0

    def make_run(self, days=8, clone=False, finished=True):
        self.n += 1
        d = self.runs / f"20260101-000000-codex-{self.n:04x}"
        d.mkdir()
        (d / ".result.lock").touch()
        tree = self.root / ".wt-delegate" / d.name
        if clone:
            tree.parent.mkdir(exist_ok=True)
            cc.isolated_checkout(self.repo, tree, tree.parent / (".git-" + d.name),
                                 "delegate/" + d.name, self.base)
            (tree.parent / (".tmp-" + d.name)).mkdir()
        meta = dict(task_id=d.name, project_dir=str(self.repo), worktree=str(tree) if clone else None,
                    base_sha=self.base, status="running")
        if finished:
            meta.update(status="ok", finished_at=(datetime.now(timezone.utc) - timedelta(days=days)).isoformat())
            cc.save_json(d / "result.json", meta)
        cc.save_json(d / "meta.json", meta)
        return d

    def prune(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cc.main(["prune", *args]), 0)
        return {k: json.loads(v) for k, v in (line.split("=", 1) for line in out.getvalue().splitlines())}

    def test_expiry_and_bad_metadata(self):
        old = self.make_run()
        recent = self.make_run(days=6)
        running = self.make_run(finished=False)
        invalid = self.make_run()
        (invalid / "meta.json").write_text("{")
        ledger = cc.home() / "delegate-log.jsonl"
        ledger.write_text("ledger")
        (self.runs / ".locks").mkdir()
        result = self.prune()
        self.assertEqual((result["runs"], result["skipped"]), (1, 3))
        self.assertFalse(old.exists())
        self.assertTrue(all(d.exists() for d in (recent, running, invalid, self.runs / ".locks")))
        self.assertEqual(ledger.read_text(), "ledger")
        self.assertEqual(self.prune("--older-than", "5")["runs"], 1)

    def test_branches_and_dry_run(self):
        cc.git(self.repo, "checkout", "-qb", "feature")
        cc.git(self.repo, "commit", "--allow-empty", "-qm", "merged into feature")
        merged, unmerged, unchanged = (self.make_run(clone=True) for _ in range(3))
        branches = ["delegate/" + d.name for d in (merged, unmerged, unchanged)]
        cc.git(self.repo, "branch", branches[0])
        cc.git(self.repo, "commit", "--allow-empty", "-qm", "later feature work")
        cc.git(self.repo, "checkout", "-qb", branches[1])
        cc.git(self.repo, "commit", "--allow-empty", "-qm", "unmerged")
        cc.git(self.repo, "checkout", "-qb", branches[2])
        cc.git(self.repo, "commit", "--allow-empty", "-qm", "detached base")
        meta = json.loads((unchanged / "meta.json").read_text())
        meta["base_sha"] = cc.git(self.repo, "rev-parse", "HEAD").strip()
        cc.save_json(unchanged / "meta.json", meta)
        cc.git(self.repo, "checkout", "-q", "main")
        (merged / ".result.lock").unlink()
        before = set(self.root.rglob("*"))
        preview = self.prune("--dry-run")
        self.assertEqual(set(self.root.rglob("*")), before)
        self.assertEqual(preview, dict(dry_run=1, runs=3, clones=3, branches=2, skipped=0,
                                       kept_branches=[branches[1]]))
        refs = cc.git(self.repo, "branch", "--list", "delegate/*")
        for branch in branches:
            self.assertIn(branch, refs)
        self.assertEqual(self.prune(), dict(preview, dry_run=0))
        self.assertEqual(cc.git(self.repo, "branch", "--list", "delegate/*").strip(), branches[1])
        self.assertEqual(list((self.root / ".wt-delegate").iterdir()), [])
        self.assertEqual(list(self.runs.iterdir()), [])

    def test_preserves_unsaved_work_and_receipts(self):
        dirty, unique, clean = (self.make_run(clone=True) for _ in range(3))
        area = cc.clone_root(self.repo)
        (area / dirty.name / "notes").write_text("draft")
        cc.git(area / unique.name, "checkout", "-qb", "local")
        cc.git(area / unique.name, "commit", "--allow-empty", "-qm", "only here")
        cc.git(area / unique.name, "checkout", "-q", "delegate/" + unique.name)
        for flags in (["--dry-run"], []):
            result = self.prune(*flags)
            self.assertEqual(result["kept_clones"], [
                dict(task_id=dirty.name, reason="uncommitted_changes"),
                dict(task_id=unique.name, reason="unique_commits")])
            self.assertEqual((result["clones"], result["runs"]), (1, 1))
            for d in (dirty, unique):
                self.assertTrue((d / "result.json").exists() and (area / d.name).is_dir())
            self.assertEqual((area / clean.name).exists(), bool(flags))

    def test_prune_after_saving_work_on_delegate_branch(self):
        d = self.make_run(clone=True)
        clone = cc.clone_root(self.repo) / d.name
        (clone / "file").write_text("draft")
        cc.git(clone, "add", "file")
        self.assertEqual(self.prune()["kept_clones"][0]["reason"], "uncommitted_changes")
        cc.git(clone, "commit", "-qm", "saved locally")
        self.assertEqual(self.prune()["kept_clones"][0]["reason"], "unique_commits")
        branch = "delegate/" + d.name
        cc.git(self.repo, "fetch", str(clone), f"HEAD:refs/heads/{branch}")
        result = self.prune()
        self.assertEqual((result["runs"], result["clones"], result["kept_branches"]), (1, 1, [branch]))

    def test_prune_ignores_clone_filters_and_retains_dirty_work(self):
        (self.repo / "draft").write_text("base")
        cc.git(self.repo, "add", "draft")
        cc.git(self.repo, "commit", "-qm", "track draft")
        self.base = cc.git(self.repo, "rev-parse", "HEAD").strip()
        d = self.make_run(clone=True)
        clone = cc.clone_root(self.repo) / d.name
        marker = self.root / "filter-ran"
        cc.git(clone, "config", "filter.fixture.clean", "touch " + shlex.quote(str(marker)) + " && cat")
        (clone / ".gitattributes").write_text("draft filter=fixture\n")
        (clone / "draft").write_text("edit")
        self.assertEqual(self.prune()["kept_clones"], [dict(task_id=d.name, reason="uncommitted_changes")])
        self.assertFalse(marker.exists())

    def test_prune_preserves_populated_and_staged_submodules(self):
        cc.git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{self.base},nested")
        cc.git(self.repo, "commit", "-qm", "track gitlink")
        self.base = cc.git(self.repo, "rev-parse", "HEAD").strip()
        populated = self.make_run(clone=True)
        staged = self.make_run(clone=True)
        area = cc.clone_root(self.repo)
        (area / populated.name / "nested" / "draft").write_text("unsaved")
        cc.git(area / staged.name, "rm", "--cached", "nested")
        with cc.clone_view(self.repo, area / staged.name) as (_, inspect, _env):
            paths, stats = cc.diff_info(inspect, self.base)
        self.assertEqual(paths, ["nested"])
        self.assertEqual(stats, [dict(path="nested", added="0", deleted="1")])
        result = self.prune()
        self.assertEqual(result["kept_clones"], [dict(task_id=populated.name, reason="submodule_checkout"),
                                                dict(task_id=staged.name, reason="uncommitted_changes")])

    def test_prune_preserves_linked_worktree_metadata(self):
        d = self.make_run(clone=True)
        clone = cc.clone_root(self.repo) / d.name
        linked = self.root / "linked"
        cc.git(clone, "worktree", "add", "-q", "--detach", str(linked))
        cc.git(linked, "commit", "--allow-empty", "-qm", "detached work")
        self.assertEqual(self.prune()["kept_clones"], [dict(task_id=d.name, reason="linked_worktrees")])
        self.assertEqual(cc.git(linked, "log", "-1", "--format=%s").strip(), "detached work")

    def test_unsafe_paths_and_missing_repo(self):
        outside = self.make_run()
        linked = self.make_run(clone=True)
        normal = self.make_run(clone=True)
        meta = json.loads((outside / "meta.json").read_text())
        target = self.root / outside.name
        target.mkdir()
        meta["worktree"] = str(target)
        cc.save_json(outside / "meta.json", meta)
        area = self.root / ".wt-delegate"
        sidecar = area / (".git-" + linked.name)
        sidecar.rename(self.root / "old-git")
        sidecar.symlink_to(target, target_is_directory=True)
        self.repo.rename(self.root / "moved")
        result = self.prune()
        self.assertEqual(result["unsafe_clones"], [outside.name, linked.name])
        self.assertEqual((result["clones"], result["runs"], result["skipped"]), (0, 2, 3))
        self.assertTrue(target.is_dir() and sidecar.is_symlink() and (area / linked.name).is_dir())
        self.assertTrue((area / normal.name).is_dir() and normal.is_dir())
        self.assertEqual(result["kept_clones"], [dict(task_id=normal.name, reason="inspection_failed")])

    def test_checked_out_branch_and_symbolic_alias_are_kept(self):
        active, aliased = (self.make_run() for _ in range(2))
        branches = ["delegate/" + d.name for d in (active, aliased)]
        cc.git(self.repo, "checkout", "-qb", branches[1])
        cc.git(self.repo, "commit", "--allow-empty", "-qm", "only on delegate")
        cc.git(self.repo, "symbolic-ref", "refs/heads/alias", "refs/heads/" + branches[1])
        cc.git(self.repo, "checkout", "-qb", branches[0], "main")
        result = self.prune()
        self.assertEqual((result["branches"], result["kept_branches"]), (0, branches))
        for branch in branches:
            cc.git(self.repo, "show-ref", "--verify", "refs/heads/" + branch)

    def test_busy_run_or_repository(self):
        d = self.make_run()
        publish = self.repo / ".git" / "cc-delegate-publish.lock"
        for path in (d / ".result.lock", publish):
            with contextlib.ExitStack() as held:
                cc.lock(held, path)
                self.assertEqual(self.prune()["skipped"], 1)
                self.assertTrue(d.is_dir())
        self.assertEqual(self.prune()["runs"], 1)
