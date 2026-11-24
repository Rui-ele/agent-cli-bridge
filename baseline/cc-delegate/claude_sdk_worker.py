"""Private worker for cc-delegate's optional Anthropic Claude Agent SDK backend."""
import argparse
import asyncio
from collections import Counter, deque
from contextlib import suppress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid


CORRELATION_TIMEOUT = 120


def emit(kind, **fields):
    print(json.dumps(dict(type=kind, **fields), ensure_ascii=False), flush=True)


def publish(path, value):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        try:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.replace(f.name, path)
        finally:
            Path(f.name).unlink(missing_ok=True)


class Session:
    def __init__(self, args, sdk):
        self.args = args
        self.sdk = sdk
        self.control = args.run_dir / "sdk-control"
        self.control.mkdir(mode=0o700, exist_ok=True)
        self.permissions = {}
        self.lock = asyncio.Lock()
        self.outstanding = set()
        self.queued = deque()
        self.interrupting = self.cancelling = self.closed = False
        self.turn = 0
        self.session_id = None
        self.correlation_deadline = None
        self.usage = Counter()
        self.denials = []
        self.last_result = None
        self.idle = False
        self.idle_path = self.control / "idle.json"

    async def permission(self, tool_name, input_data, context):
        if self.args.approval != "ask" or self.cancelling or self.interrupting:
            return self.sdk.PermissionResultDeny(message="Tool has no approval for this task.")
        self.correlation_deadline = None
        key = uuid.uuid4().hex
        request = dict(request_id=key, tool_name=tool_name, input=input_data,
                       tool_use_id=getattr(context, "tool_use_id", None),
                       description=getattr(context, "description", None))
        decision = asyncio.get_running_loop().create_future()
        self.permissions[key] = decision
        path = self.control / f"pending-{key}.json"
        try:
            publish(path, dict(type="sdk.permission", **request))
            emit("sdk.permission", **request)
            if await decision:
                return self.sdk.PermissionResultAllow(updated_input=input_data)
            return self.sdk.PermissionResultDeny(message="Caller declined this tool call.")
        finally:
            self.permissions.pop(key, None)
            path.unlink(missing_ok=True)

    def decline_pending(self):
        for permission in self.permissions.values():
            if not permission.done():
                permission.set_result(False)

    async def send(self, text, key):
        async def envelope():
            yield {"type": "user", "uuid": key, "message": {"role": "user", "content": text},
                   "parent_tool_use_id": None}
        if self.idle:
            self.idle = False
            self.idle_path.unlink(missing_ok=True)
        await self.client.query(envelope())
        self.outstanding.add(key)
        emit("sdk.input", message_uuid=key, after_result=self.turn)

    def finish(self):
        self.closed = True
        self.idle_path.unlink(missing_ok=True)
        emit("result", **self.last_result)

    async def command(self, request):
        action = request.get("action")
        if self.closed:
            raise ValueError("task_finished")
        if action in ("approve", "deny"):
            pending = self.permissions.get(request.get("request_id"))
            if pending is None or pending.done():
                raise ValueError("permission_not_pending")
            pending.set_result(action == "approve")
            return dict(status="approved" if action == "approve" else "denied")
        if self.cancelling:
            raise ValueError("task_cancelling")
        if action == "finish":
            if not self.idle:
                raise ValueError("turn_active")
            self.finish()
            return dict(status="finished")
        if action == "cancel":
            self.cancelling = True
            if self.idle:
                self.finish()
            else:
                try:
                    await self.client.interrupt()
                finally:
                    self.decline_pending()
            return dict(status="cancel_requested")
        if action not in ("steer", "queue", "interrupt"):
            raise ValueError("unsupported_control")
        text = request.get("message")
        if (action != "interrupt" or text is not None) and (not isinstance(text, str) or not text.strip()):
            raise ValueError("message_required")
        key = str(uuid.uuid4())
        if self.idle:
            if action == "interrupt":
                raise ValueError("no_active_turn")
            await self.send(text, key)
            return dict(status="submitted", message_uuid=key)
        if action == "interrupt":
            if self.interrupting:
                raise ValueError("already_interrupting")
            self.interrupting = True
            try:
                await self.client.interrupt()
            except BaseException:
                self.interrupting = False
                raise
            self.decline_pending()
            self.correlation_deadline = asyncio.get_running_loop().time() + CORRELATION_TIMEOUT
            if text:
                self.queued.appendleft((text, key))
            return dict(status="interrupt_requested", message_uuid=key if text else None)
        if action == "queue":
            self.queued.append((text, key))
            return dict(status="queued", message_uuid=key)
        if self.interrupting:
            raise ValueError("turn_interrupting")
        # The CLI reports up to 64 UUIDs per result.
        if len(self.outstanding) >= 64:
            raise ValueError("too_many_pending_inputs")
        await self.send(text, key)
        return dict(status="submitted", message_uuid=key)

    async def controls(self):
        while not self.closed:
            if (self.correlation_deadline is not None
                    and asyncio.get_running_loop().time() >= self.correlation_deadline):
                raise RuntimeError("sdk_result_timeout: the CLI did not report a result for all submitted messages")
            requests = []
            for path in self.control.glob("req-*.json"):
                if re.fullmatch(r"req-[0-9a-f]{32}\.json", path.name):
                    with suppress(FileNotFoundError):
                        requests.append((path.stat().st_mtime_ns, path))
            for _, path in sorted(requests):
                key = path.stem[4:]
                claimed = self.control / f"processing-{key}.json"
                async with self.lock:
                    try:
                        path.rename(claimed)
                    except FileNotFoundError:
                        continue
                    try:
                        request = json.loads(claimed.read_text())
                        if not isinstance(request, dict):
                            raise ValueError("invalid_control")
                        ack = dict(ok=True, **await self.command(request))
                    except (TypeError, ValueError, OSError, RuntimeError) as exc:
                        ack = dict(ok=False, status=str(exc) or type(exc).__name__)
                    publish(self.control / f"ack-{key}.json", ack)
                    claimed.unlink()
            await asyncio.sleep(.05)

    def progress(self, message):
        kind = type(message).__name__
        if not self.interrupting and kind in ("StreamEvent", "AssistantMessage", "UserMessage"):
            self.correlation_deadline = None
        if kind == "SystemMessage" and message.subtype == "init":
            self.session_id = message.data.get("session_id")
            emit("system", subtype="init", session_id=self.session_id, model=message.data.get("model"))
        elif kind == "StreamEvent":
            delta = message.event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                emit("sdk.progress", text=delta["text"])
        elif kind in ("AssistantMessage", "UserMessage"):
            blocks = []
            for block in message.content if isinstance(message.content, list) else []:
                typ = type(block).__name__
                if typ == "TextBlock":
                    blocks.append(dict(type="text", text=block.text))
                elif typ == "ToolUseBlock":
                    blocks.append(dict(type="tool_use", id=block.id, name=block.name, input=block.input))
                elif typ == "ToolResultBlock":
                    blocks.append(dict(type="tool_result", tool_use_id=block.tool_use_id,
                                       content=block.content, is_error=block.is_error))
            if blocks:
                emit("assistant" if kind == "AssistantMessage" else "user", message=dict(content=blocks))

    async def result(self, msg):
        self.turn += 1
        self.session_id = msg.session_id
        self.denials.extend(getattr(msg, "permission_denials", None) or [])
        counts = msg.usage if isinstance(msg.usage, dict) else {}
        for field in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            self.usage[field] += counts.get(field) or 0
        reported_ids = getattr(msg, "user_message_uuids", None)
        covered = list(reported_ids or [])
        first = getattr(msg, "user_message_uuid", None)
        if first and first not in covered:
            covered.append(first)
        emit("sdk.turn_result", turn=self.turn, session_id=msg.session_id, user_message_uuids=covered,
             subtype=msg.subtype, is_error=msg.is_error, terminal_reason=getattr(msg, "terminal_reason", None),
             result=msg.result, structured_output=msg.structured_output, usage=msg.usage,
             total_cost_usd=msg.total_cost_usd, modelUsage=getattr(msg, "model_usage", None) or {})
        if covered or reported_ids is not None:
            matched = self.outstanding.intersection(covered)
            if not matched:
                return False
        else:
            origin = getattr(msg, "origin", None) or {}
            if origin.get("kind") not in (None, "human") or not self.outstanding:
                return False
            if self.interrupting and origin.get("kind") is None:
                # Background results can arrive before the interrupted prompt is acknowledged.
                return False
            if len(self.outstanding) != 1:
                raise RuntimeError("sdk_result_missing_message_ids: unable to correlate overlapping inputs")
            # Older CLIs omit the UUID even for a single foreground prompt.
            matched = self.outstanding.copy()
        self.outstanding.difference_update(matched)
        terminal = getattr(msg, "terminal_reason", None)
        interrupted = terminal in ("aborted_tools", "aborted_streaming") or (not terminal and self.interrupting)
        stop_requested = self.interrupting
        if not self.cancelling:
            if self.outstanding:
                # Interruption may discard further results. Don't send a replacement until
                # every prompt is accounted for, even when the CLI returned a UUID list.
                if self.interrupting or reported_ids is None:
                    self.correlation_deadline = asyncio.get_running_loop().time() + CORRELATION_TIMEOUT
                return False
            self.interrupting = False
            self.correlation_deadline = None
            if self.queued:
                await self.send(*self.queued.popleft())
                return False
        errors = getattr(msg, "errors", None) or []
        failed = msg.is_error or interrupted or stop_requested or self.cancelling
        result_text = msg.result or "; ".join(map(str, errors))
        if interrupted:
            result_text = "turn_interrupted"
        elif stop_requested:
            result_text = f"interrupt_requested_but_turn_ended: {terminal}"
        self.last_result = dict(subtype=msg.subtype, is_error=failed, session_id=msg.session_id,
                                usage=self.usage, total_cost_usd=msg.total_cost_usd,
                                modelUsage=getattr(msg, "model_usage", None) or {},
                                result=result_text, structured_output=None if failed else msg.structured_output,
                                permission_denials=self.denials)
        if self.args.keep_open and not self.cancelling:
            self.idle = True
            structured = self.last_result["structured_output"]
            summary = structured.get("summary") if isinstance(structured, dict) else None
            summary = [s[:200] for s in summary[:8] if isinstance(s, str)] if isinstance(summary, list) else []
            status = structured.get("status", "partial") if isinstance(structured, dict) else "partial"
            state = dict(turn=self.turn, session_id=self.session_id, status="error" if failed else status,
                         summary=summary or [result_text[:200]])
            publish(self.idle_path, state)
            emit("sdk.idle", **state)
            return False
        self.finish()
        return True

    async def receive(self):
        async for message in self.client.receive_messages():
            if type(message).__name__ == "ResultMessage":
                async with self.lock:
                    if await self.result(message):
                        return
            else:
                self.progress(message)
        raise RuntimeError("sdk_stream_closed_before_final_result")

    async def run(self, prompt):
        args = self.args
        tools = ["Read", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"]
        if args.mode == "write":
            tools += ["Edit", "Write"]
        options = self.sdk.ClaudeAgentOptions(
            cwd=args.cwd, cli_path=args.cli_path, model=args.model, resume=args.resume,
            effort=args.effort, add_dirs=args.add_dir, include_partial_messages=True,
            tools=tools, allowed_tools=tools if args.approval == "auto" else [],
            permission_mode="default", can_use_tool=self.permission, setting_sources=[], verbatim_prompts=True,
            settings='{"sandbox":{"enabled":false}}', strict_mcp_config=True,
            output_format={"type": "json_schema", "schema": json.loads((args.run_dir / "schema.json").read_text())},
        )
        async with self.sdk.ClaudeSDKClient(options=options) as self.client:
            await self.send(prompt, str(uuid.uuid4()))
            tasks = [asyncio.create_task(self.receive()), asyncio.create_task(self.controls())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                self.closed = True
                self.idle_path.unlink(missing_ok=True)
                self.decline_pending()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


async def run_with_supervisor(args, sdk, prompt):
    if os.getppid() != args.supervisor_pid:
        return
    session = asyncio.create_task(Session(args, sdk).run(prompt))
    try:
        while os.getppid() == args.supervisor_pid:
            done, _ = await asyncio.wait([session], timeout=.25)
            if done:
                return session.result()
    finally:
        if not session.done():
            session.cancel()
            await asyncio.gather(session, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--supervisor-pid", required=True, type=int)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--cli-path", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", required=True, choices=("ro", "write"))
    parser.add_argument("--resume")
    parser.add_argument("--effort")
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument("--approval", choices=("auto", "ask"), default="auto")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        try:
            import claude_agent_sdk
        except ModuleNotFoundError as exc:
            if exc.name != "claude_agent_sdk":
                raise
            raise RuntimeError("Claude Agent SDK missing: set CC_DELEGATE_SDK_PYTHON to the Python "
                               "environment containing claude-agent-sdk (see README)") from exc
        asyncio.run(run_with_supervisor(args, claude_agent_sdk, sys.stdin.read()))
    except (Exception, KeyboardInterrupt) as exc:
        emit("result", subtype="error_during_execution", is_error=True, result=str(exc) or type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
