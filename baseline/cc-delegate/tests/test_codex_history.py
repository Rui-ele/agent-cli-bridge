import unittest

import cc_delegate as cc
from tests import temp_workspace


def legacy(role, text):
    return {"type": role + "_message", "message": text}


def item(kind, **fields):
    return {"type": "item_completed", "item": {"type": kind, **fields}}


def modern(role, *texts):
    return item(role + "Message", content=[{"type": "text", "text": s} for s in texts])


class CodexHistoryCase(unittest.TestCase):
    def setUp(self):
        self.path = temp_workspace(self) / "rollout.jsonl"

    def write(self, *events):
        self.path.write_text("".join(cc.dumps({"type": "event_msg", "payload": e}) + "\n" for e in events))

    def prompt(self, tier):
        history = cc.load_codex_session(self.path, tier)
        return cc.package("task", history, [], tier, "ro", 100_000, "codex")[0]

    def test_rollbacks_precede_context_selection(self):
        start = {"type": "task_started"}
        undo = {"type": "thread_rolled_back", "num_turns": 1}
        self.write(start, legacy("user", "oldest"), start, legacy("user", "survivor"),
                   start, legacy("user", "deleted one"), modern("User", "deleted steer"),
                   start, legacy("user", "deleted two"), start,
                   item("CommandExecution", id="gone", command=["echo", "deleted"], aggregated_output="deleted"),
                   start, undo, undo,
                   start, modern("User", "replacement one"), modern("User", "new steer"),
                   start, modern("User", "replacement two"))
        for tier in ("scoped", "session", "full"):
            with self.subTest(tier=tier):
                prompt = self.prompt(tier)
                self.assertNotIn("deleted", prompt)
                for s in ("survivor", "replacement one", "new steer", "replacement two"):
                    self.assertIn(s, prompt)
                self.assertEqual("oldest" in prompt, tier != "scoped")

    def test_legacy_rollbacks_and_invalid_counts(self):
        self.write(legacy("user", "oldest"), legacy("agent", "first reply"),
                   legacy("user", "gone"), legacy("agent", "gone too"),
                   {"type": "thread_rolled_back", "num_turns": 1})
        self.assertIn("first reply", self.prompt("full"))
        self.assertNotIn("gone", self.prompt("full"))
        for count, reason in ((6, "no_conversation"), (None, "invalid_codex_rollback"),
                              (True, "invalid_codex_rollback"), (-1, "invalid_codex_rollback")):
            with self.subTest(count=count):
                self.write(legacy("user", "gone"), {"type": "thread_rolled_back", "num_turns": count})
                with self.assertRaisesRegex(cc.Blocked, reason):
                    self.prompt("session")

    def test_mixed_messages_pair_once_without_reordering(self):
        self.write({"type": "task_started"},
                   legacy("user", "first"), legacy("user", "steer"), modern("User", "first"),
                   modern("Agent", "one", " reply"), legacy("agent", "one reply"),
                   legacy("agent", "one reply"), modern("Agent", "more"), legacy("agent", "last"))
        messages = cc.load_codex_session(self.path, "full")
        self.assertEqual([(role, [b["text"] for b in blocks]) for role, blocks in messages],
                         [("user", ["first"]), ("user", ["steer"]), ("assistant", ["one", " reply"]),
                          ("assistant", ["one reply"]), ("assistant", ["more"]), ("assistant", ["last"])])
