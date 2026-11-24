"""Offline stand-in for the subset of claude_agent_sdk used by the bridge."""
import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace


class ClaudeAgentOptions(SimpleNamespace):
    pass


class PermissionResultAllow(SimpleNamespace):
    behavior = "allow"


class PermissionResultDeny(SimpleNamespace):
    behavior = "deny"


@dataclass
class SystemMessage:
    subtype: str
    data: dict


@dataclass
class TextBlock:
    text: str


@dataclass
class AssistantMessage:
    content: list
    model: str
    parent_tool_use_id: str | None = None
    error: str | None = None


@dataclass
class StreamEvent:
    event: dict
    session_id: str
    uuid: str = "event"
    parent_tool_use_id: str | None = None


@dataclass
class ResultMessage:
    subtype: str
    session_id: str
    result: str
    structured_output: dict | None
    is_error: bool
    usage: dict = field(default_factory=lambda: {"input_tokens": 9, "output_tokens": 7})
    total_cost_usd: float = 0.003
    duration_ms: int = 10
    duration_api_ms: int = 5
    num_turns: int = 1
    terminal_reason: str = "completed"
    user_message_uuid: str | None = None
    user_message_uuids: list[str] | None = None
    model_usage: dict | None = None
    permission_denials: list | None = None


def trace(event, **details):
    with Path(os.environ["SDK_TRACE"]).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": event, **details}) + "\n")


class ClaudeSDKClient:
    def __init__(self, options):
        self.options = options
        self.session = getattr(options, "resume", None) or "synthetic-sdk-session"
        self.prompts = asyncio.Queue()
        self.interrupted = asyncio.Event()
        self.first = True

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()

    async def connect(self, prompt=None):
        trace("connect", session=self.session, cwd=str(self.options.cwd), model=self.options.model,
              cli_path=str(self.options.cli_path), permission_mode=self.options.permission_mode)
        if prompt is not None:
            await self.query(prompt)

    async def disconnect(self):
        trace("disconnect")

    async def query(self, prompt, session_id="default"):
        async for message in prompt:
            content = message["message"]["content"]
            trace("query", prompt=content, uuid=message["uuid"])
            await self.prompts.put((content, message["uuid"]))

    async def interrupt(self):
        trace("interrupt")
        self.interrupted.set()

    async def receive_messages(self):
        yield SystemMessage("init", {"session_id": self.session, "model": self.options.model})
        while True:
            prompt, first_id = await self.prompts.get()
            consumed = [first_id]
            first, self.first = self.first, False
            scenario = os.environ.get("SDK_SCENARIO", "normal")
            if not first and scenario == "fail_followup":
                raise RuntimeError("simulated follow-up failure")
            yield StreamEvent({"type": "content_block_delta", "index": 0,
                               "delta": {"type": "text_delta", "text": "live SDK delta"}}, self.session)
            if first and scenario == "stream":
                trace("active")
                while not Path(os.environ["SDK_RELEASE"]).exists():
                    await asyncio.sleep(.01)
            yield AssistantMessage([TextBlock("reading fixture")], self.options.model)
            if first and scenario in ("wait", "merge", "missing_ids", "aborted_success"):
                trace("active")
                gate = Path(os.environ["SDK_RELEASE"])
                while not self.interrupted.is_set() and not gate.exists():
                    await asyncio.sleep(.01)
                if scenario == "merge" and not self.prompts.empty():
                    prompt, added_id = self.prompts.get_nowait()
                    consumed.append(added_id)
            if first and scenario == "approval":
                result = await self.options.can_use_tool(
                    "Bash", {"command": "fixture-command"},
                    SimpleNamespace(tool_use_id="tool-1", signal=None, suggestions=[]))
                trace("decision", behavior=result.behavior)
            if scenario == "write":
                path = Path(self.options.cwd)
                (path / "answer.txt").write_text("fixture answer\n")
                subprocess.run(["git", "add", "answer.txt"], check=True, cwd=path)
                subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                "commit", "-qm", "Add synthetic answer"], check=True, cwd=path)
            interrupted = self.interrupted.is_set()
            self.interrupted.clear()
            failed = (interrupted and scenario != "aborted_success") or scenario == "error"
            if failed:
                reply = "turn interrupted" if interrupted else "simulated SDK failure"
                structured = None
            else:
                reply = "first reply" if first and len(consumed) == 1 else f"follow up: {prompt}"
                structured = dict(status="ok", summary=[reply], uncertainties=[], report_markdown=reply)
            trace("result", is_error=failed)
            yield ResultMessage("error_during_execution" if failed else "success", self.session,
                                reply, structured, failed,
                                terminal_reason="aborted_tools" if interrupted else "completed",
                                user_message_uuid=None if scenario == "missing_ids" else consumed[-1],
                                user_message_uuids=None if scenario == "missing_ids" else consumed)

    async def receive_response(self):
        async for msg in self.receive_messages():
            yield msg
            if isinstance(msg, ResultMessage):
                return
