import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import claude_sdk_worker as worker
import cc_delegate as cc
from tests import temp_workspace
from tests.fixtures.fake_claude_sdk import ResultMessage, StreamEvent


class SdkAccounting(unittest.TestCase):
    def test_cost_uses_latest_reported_total_even_after_error(self):
        events = cc.WorkerEvents(cc.driver_for("claude", "sdk"))
        for price in (0.01, 0.03, None):
            events.observe(dict(type="sdk.turn_result", usage={"input_tokens": 5}, total_cost_usd=price))
        events.observe(dict(type="result", is_error=True, result="connection lost"))
        self.assertEqual(events.usage["total_tokens"], 15)
        self.assertEqual(events.usage["cost_usd"], 0.03)


class InterruptCorrelation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        args = SimpleNamespace(run_dir=temp_workspace(self), keep_open=False)
        self.session = worker.Session(args, None)
        self.session.client = AsyncMock()
        self.session.outstanding.update(("first", "steer"))
        quiet = patch.object(worker, "emit")
        quiet.start()
        self.addCleanup(quiet.stop)

    def result(self, ids=None, origin=None):
        msg = ResultMessage("success", "session", "answer", None, False,
                            terminal_reason="aborted_streaming", user_message_uuids=ids)
        if origin is not None:
            msg.origin = {"kind": origin}
        return self.session.result(msg)

    async def test_only_matching_ids_release_replacement(self):
        ack = await self.session.command(dict(action="interrupt", message="replacement"))
        for ids, origin in [(None, "background"), (["stale"], "human"), (["steer"], "human")]:
            self.assertFalse(await self.result(ids, origin))
            self.session.client.query.assert_not_awaited()
            self.assertTrue(self.session.interrupting)
        self.assertEqual(self.session.outstanding, {"first"})
        self.assertFalse(await self.result(["first"]))
        self.assertEqual(self.session.outstanding, {ack["message_uuid"]})
        self.assertFalse(self.session.interrupting)
        self.session.client.query.assert_awaited_once()

    async def test_ambiguous_foreground_interrupt_does_not_guess(self):
        await self.session.command(dict(action="interrupt"))
        with self.assertRaisesRegex(RuntimeError, "sdk_result_missing_message_ids"):
            await self.result(origin="human")
        self.assertEqual(self.session.outstanding, {"first", "steer"})

    async def test_partial_interrupt_times_out_despite_unrelated_activity(self):
        with patch.object(worker, "CORRELATION_TIMEOUT", 0):
            await self.session.command(dict(action="interrupt", message="replacement"))
            await self.result(["first"])
            self.session.progress(StreamEvent({}, "session"))
            with self.assertRaisesRegex(RuntimeError, "sdk_result_timeout"):
                await self.session.controls()
        self.session.client.query.assert_not_awaited()

    async def test_unidentified_interrupt_result_waits_for_remaining_id(self):
        with patch.object(worker, "CORRELATION_TIMEOUT", 0):
            await self.session.command(dict(action="interrupt", message="replacement"))
            await self.result(["first"])
            for ids, origin in ((None, None), ([], None), ([], "human")):
                with self.subTest(ids=ids, origin=origin):
                    self.assertFalse(await self.result(ids, origin))
                    self.assertEqual(self.session.outstanding, {"steer"})
                    self.session.client.query.assert_not_awaited()
            with self.assertRaisesRegex(RuntimeError, "sdk_result_timeout"):
                await self.session.controls()

    async def test_legacy_human_interrupt_without_uuid_list(self):
        await self.session.command(dict(action="interrupt"))
        await self.result(["first"])
        self.assertTrue(await self.result(None, "human"))
        self.assertEqual(self.session.last_result["result"], "turn_interrupted")

    async def test_blocked_query_clears_idle_marker_before_sending(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_send(envelope):
            started.set()
            await release.wait()

        self.session.client.query.side_effect = blocked_send
        self.session.outstanding.clear()
        self.session.idle = True
        self.session.idle_path.touch()
        sending = asyncio.create_task(self.session.command(dict(action="queue", message="follow-up")))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertFalse(self.session.idle_path.exists())
            self.assertFalse(self.session.idle)
        finally:
            release.set()
            await sending
