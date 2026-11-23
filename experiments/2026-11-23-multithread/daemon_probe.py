"""Same probe as multithread_probe.py, but against the managed daemon's WebSocket control socket."""
import asyncio, json, sys, time
import websockets

N = int(sys.argv[1]) if len(sys.argv) > 1 else 3
CWD = sys.argv[2]
SOCK = sys.argv[3]
SLEEP = 20
t0 = time.time()
now = lambda: round(time.time() - t0, 1)


async def main():
    async with websockets.unix_connect(SOCK, "ws://localhost/", max_size=None) as ws:
        pending, events, counter = {}, [], [0]

        async def request(method, params):
            counter[0] += 1
            rid = counter[0]
            fut = asyncio.get_running_loop().create_future()
            pending[rid] = fut
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}))
            msg = await asyncio.wait_for(fut, 120)
            if "error" in msg:
                raise RuntimeError(f"{method}: {msg['error']}")
            return msg["result"]

        async def reader():
            async for raw in ws:
                msg = json.loads(raw)
                if "id" in msg and "method" not in msg and msg["id"] in pending:
                    pending.pop(msg["id"]).set_result(msg)
                elif "id" in msg and "method" in msg:
                    events.append((now(), "server_request", msg["method"], None))
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                              "error": {"code": -32601, "message": "probe declines"}}))
                elif msg.get("method") in ("turn/started", "turn/completed"):
                    p = msg.get("params", {})
                    events.append((now(), msg["method"], p.get("threadId"), (p.get("turn") or {}).get("status")))

        rt = asyncio.create_task(reader())
        await request("initialize", {"clientInfo": {"name": "cc-delegate-probe", "version": "0.0.1"},
                                     "capabilities": {"experimentalApi": False}})
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "initialized"}))
        threads = []
        for _ in range(N):
            r = await request("thread/start", {"cwd": CWD, "model": "gpt-6.2-astra", "approvalPolicy": "never",
                                               "sandbox": "read-only", "ephemeral": True})
            threads.append(r["thread"]["id"])
        await asyncio.gather(*[request("turn/start", {"threadId": t, "input": [{
            "type": "text", "text_elements": [],
            "text": f"Run the shell command `sleep {SLEEP}` exactly once, then reply with only: done-{i}"}]})
            for i, t in enumerate(threads)])
        await asyncio.sleep(5)
        loaded = await request("thread/loaded/list", {})
        mine = [t for t in threads if t in json.dumps(loaded)]
        print(f"[{now()}s] daemon reports {len(mine)}/{N} of our threads loaded", flush=True)
        deadline = time.time() + 240
        while time.time() < deadline and len({e[2] for e in events if e[1] == "turn/completed"} & set(threads)) < N:
            await asyncio.sleep(0.5)
        rt.cancel()
        starts = {e[2]: e[0] for e in events if e[1] == "turn/started"}
        ends = {e[2]: (e[0], e[3]) for e in events if e[1] == "turn/completed"}
        for i, t in enumerate(threads):
            print(f"thread {i}: started {starts.get(t)}s  completed {ends.get(t)}")
        print("server requests:", [e for e in events if e[1] == "server_request"])
        if len(ends) == N:
            wall = max(v[0] for v in ends.values()) - min(starts.values())
            serial = sum(ends[t][0] - starts[t] for t in threads)
            print(f"wall {wall:.1f}s vs sum of turns {serial:.1f}s -> {'PARALLEL' if wall < serial * 0.7 else 'SERIAL-ish'}")

asyncio.run(main())
