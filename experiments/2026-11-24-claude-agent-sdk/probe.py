"""Exercise real Claude SDK turns with a synthetic, approval-gated file."""
import argparse
import asyncio
from pathlib import Path
import shutil
import tempfile
import time
import uuid

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny, StreamEvent


async def send(client, text, message_id, priority=None):
    async def envelope():
        entry = {"type": "user", "uuid": message_id, "parent_tool_use_id": None,
                 "message": {"role": "user", "content": text}}
        if priority:
            entry["priority"] = priority
        yield entry

    await client.query(envelope())


async def probe(mode, directory):
    started = time.monotonic()

    def log(label, details=""):
        print(f"{mode} {time.monotonic() - started:5.1f}s {label} {details}", flush=True)

    requested = asyncio.Event()
    release = asyncio.Event()
    target = directory / "marker.txt"
    ids = {str(uuid.uuid4()): label for label in ("original", "extra", "resume")}
    original, extra, resumed = ids
    marker = "AMBER-31"
    changed = "VIOLET-82"

    async def approve(name, data, context):
        path = directory / data.get("file_path", "")
        if name != "Write" or path.resolve() != target or data.get("content", "").strip() != marker:
            return PermissionResultDeny(message="Only the named synthetic marker may be created.")
        log("approval requested; file exists", target.exists())
        requested.set()
        await release.wait()
        if mode == "later" or mode == "interrupt":
            log("deny")
            return PermissionResultDeny(message="Fixture denied. Do not retry.")
        log("allow")
        return PermissionResultAllow(updated_input=data)

    options = ClaudeAgentOptions(cwd=directory, cli_path=shutil.which("claude"),
                                 model="haiku", permission_mode="default", tools=["Write"],
                                 can_use_tool=approve, setting_sources=[], strict_mcp_config=True,
                                 verbatim_prompts=True, include_partial_messages=True, max_turns=5)
    prompt = (f"Use Write exactly once to create {target.name} in this directory with just {marker}. "
              f"If allowed, return exactly {marker}. If denied, return DENIED and do not retry.")
    addition = f"Return exactly {changed} as your final text. Do not use any more tools."

    async with ClaudeSDKClient(options) as client:
        results = asyncio.Queue()
        partials = 0

        async def receive():
            nonlocal partials
            async for msg in client.receive_messages():
                if isinstance(msg, StreamEvent):
                    partials += 1
                if isinstance(msg, ResultMessage):
                    labels = [ids.get(i, "unknown") for i in (msg.user_message_uuids or [])]
                    log("result", (msg.subtype, msg.terminal_reason, labels, msg.result, list(msg.model_usage or {})))
                    results.put_nowait(msg)

        reader = asyncio.create_task(receive())
        try:
            await send(client, prompt, original)
            await asyncio.wait_for(requested.wait(), 35)
            if mode == "interrupt":
                await client.interrupt()
                log("interrupt acknowledged")
                release.set()
                first = await asyncio.wait_for(results.get(), 30)
                assert first.terminal_reason in ("aborted_tools", "aborted_streaming"), first.terminal_reason
                await send(client, addition, extra)
                log("follow-up sent after interrupted result")
            else:
                await send(client, addition, extra, "later" if mode == "later" else None)
                log("additional message sent during pending approval")
                release.set()
                first = await asyncio.wait_for(results.get(), 35)
            if mode == "next":
                assert first.result and changed in first.result
                assert set(first.user_message_uuids or []) == {original, extra}
                assert target.read_text().strip() == marker
            else:
                second = await asyncio.wait_for(results.get(), 35)
                assert first.session_id == second.session_id
                assert changed not in (first.result or "") and changed in (second.result or "")
                assert extra in (second.user_message_uuids or [])
                assert not target.exists()
            assert partials > 0
            log("partial frames", partials)
        finally:
            release.set()
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    if mode == "next":
        options.resume = first.session_id
        async with ClaudeSDKClient(options) as client:
            await send(client, "Without tools, repeat the final marker I requested in my previous message.", resumed)
            async for msg in client.receive_response():
                if isinstance(msg, ResultMessage):
                    assert msg.session_id == first.session_id and changed in (msg.result or "")
                    log("new client resumed original session with correct marker")


async def append_during_text(directory):
    options = ClaudeAgentOptions(cwd=directory, cli_path=shutil.which("claude"), model="haiku",
                                 tools=[], setting_sources=[], strict_mcp_config=True,
                                 include_partial_messages=True)
    started = time.monotonic()
    original, extra = str(uuid.uuid4()), str(uuid.uuid4())
    sent = False
    results = []
    async with ClaudeSDKClient(options) as client:
        await send(client, "Write a numbered list of 50 distinct fruits in English. Do not use tools.", original)
        async for message in client.receive_messages():
            if isinstance(message, StreamEvent):
                delta = message.event.get("delta") or {}
                if not sent and delta.get("type") == "text_delta" and delta.get("text"):
                    await send(client, "Respond with just VIOLET-82.", extra)
                    print(f"text {time.monotonic() - started:.1f}s sent after first text delta", flush=True)
                    sent = True
            elif isinstance(message, ResultMessage):
                results.append(message)
                print(f"text {time.monotonic() - started:.1f}s result {len(results)} "
                      f"original={original in (message.user_message_uuids or [])} "
                      f"extra={extra in (message.user_message_uuids or [])}", flush=True)
                if extra in (message.user_message_uuids or []):
                    break
    assert sent and len(results) == 2
    assert "VIOLET-82" not in results[0].result and "VIOLET-82" in results[1].result


async def main(modes):
    for mode in modes:
        with tempfile.TemporaryDirectory(prefix="sdk-turn-") as tmp:
            directory = Path(tmp).resolve()
            scenario = append_during_text(directory) if mode == "text" else probe(mode, directory)
            await asyncio.wait_for(scenario, 90)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", nargs="+", choices=["next", "later", "interrupt", "text"])
    asyncio.run(main(parser.parse_args().mode))
