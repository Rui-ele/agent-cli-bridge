"""Probe two step-3 questions against the managed Codex daemon.

1. Are cc-delegate's named, non-ephemeral threads listed (what `codex agents` browses)?
2. Can `externalAgentConfig/import` turn a Claude Code transcript into a Codex thread
   that actually remembers the conversation?

usage: python3 probe.py SOCKET TASK_ID_FRAGMENT CLAUDE_SESSION_JSONL SESSION_CWD QUESTION
"""
import asyncio, json, os, sys, time
from pathlib import Path
import websockets

SOCK, FRAGMENT, SESSION, CWD, QUESTION = sys.argv[1:6]
t0 = time.time()
now = lambda: round(time.time() - t0, 1)


async def main():
    async with websockets.unix_connect(SOCK, "ws://localhost/", max_size=None) as ws:
        pending, notes, counter = {}, [], [0]

        async def request(method, params, timeout=120):
            counter[0] += 1
            rid = counter[0]
            fut = asyncio.get_running_loop().create_future()
            pending[rid] = fut
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
            msg = await asyncio.wait_for(fut, timeout)
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg["result"]

        async def reader():
            async for raw in ws:
                msg = json.loads(raw)
                if "id" in msg and "method" not in msg and msg["id"] in pending:
                    pending.pop(msg["id"]).set_result(msg)
                elif "id" in msg and "method" in msg:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                              "error": {"code": -32601, "message": "probe declines"}}))
                else:
                    notes.append(msg)

        rt = asyncio.create_task(reader())
        await request("initialize", {"clientInfo": {"name": "cc-delegate-probe", "version": "0.0.1"},
                                     "capabilities": {"experimentalApi": False}})
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "initialized"}))

        # 1. visibility
        listed = await request("thread/list", {"limit": 50, "sortKey": "updated_at"})
        rows = listed.get("data") or listed.get("threads") or []
        named = [r for r in rows if FRAGMENT in json.dumps(r)]
        print(f"[{now()}s] thread/list newest 50: {len(rows)} rows, {len(named)} mention {FRAGMENT!r}")
        for r in named[:3]:
            print("   ", {k: r.get(k) for k in ("id", "name", "cwd", "ephemeral", "source") if k in r})
        if rows:
            print("    row keys:", sorted(rows[0]))

        # 2. import
        src = str(Path(SESSION).resolve())
        await request("externalAgentConfig/import", {"migrationItems": [{
            "itemType": "SESSIONS", "description": f"Transfer Claude session {Path(src).name}", "cwd": None,
            "details": {"plugins": [], "sessions": [{"path": src, "cwd": CWD, "title": None}],
                        "mcpServers": [], "hooks": [], "subagents": [], "commands": []}}]})
        while not any(n.get("method") == "externalAgentConfig/import/completed" for n in notes):
            if now() > 300:
                raise TimeoutError("import did not complete")
            await asyncio.sleep(0.5)
        done = next(n for n in notes if n.get("method") == "externalAgentConfig/import/completed")
        print(f"[{now()}s] import completed:", json.dumps(done["params"], ensure_ascii=False)[:600])
        ledger = Path(os.getenv("CODEX_HOME", "~/.codex")).expanduser() / "external_agent_session_imports.json"
        # The source may still be growing (a live session), so match on path and take the newest.
        recs = [r for r in json.loads(ledger.read_text()).get("records", []) if r.get("source_path") == src]
        thread = recs[-1]["imported_thread_id"] if recs else None
        print(f"[{now()}s] imported thread:", thread)
        if not thread:
            return
        read = await request("thread/read", {"threadId": thread, "includeTurns": True})
        turns = read["thread"].get("turns") or []
        print(f"[{now()}s] imported thread has {len(turns)} turn(s)")

        # Ask the imported thread something only the transcript knows.
        await request("thread/resume", {"threadId": thread, "approvalPolicy": "never", "sandbox": "read-only"})
        before = len(notes)
        await request("turn/start", {"threadId": thread, "sandboxPolicy": {"type": "readOnly"},
                                     "input": [{"type": "text", "text": QUESTION, "text_elements": []}]})
        answer, usage = None, None
        while now() < 600:
            for n in notes[before:]:
                p = n.get("params", {})
                if p.get("threadId") != thread:
                    continue
                if n.get("method") == "item/completed" and p["item"].get("type") == "agentMessage":
                    answer = p["item"]["text"]
                if n.get("method") == "thread/tokenUsage/updated":
                    usage = p["tokenUsage"].get("last") or p["tokenUsage"].get("total")
                if n.get("method") == "turn/completed":
                    print(f"[{now()}s] turn {p['turn']['status']}; usage={usage}")
                    print("answer:", answer)
                    rt.cancel()
                    return
            before = len(notes)
            await asyncio.sleep(0.5)

asyncio.run(main())
