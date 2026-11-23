"""Parallel-write probe against the managed Codex daemon.

Creates a throwaway git repo, one local clone (git dir named .git-<i>) per thread on its own branch, then starts
N threads at once. Each thread edits the same shared file plus its own file and commits.
Thread 0 is also asked to try writing into the main checkout, which the sandbox should block.

usage: python3 daemon_write_probe.py N WORKDIR SOCKET
"""
import asyncio, json, subprocess, sys, time
from pathlib import Path
import websockets

N, WORK, SOCK = int(sys.argv[1]), Path(sys.argv[2]).resolve(), sys.argv[3]
t0 = time.time()
now = lambda: round(time.time() - t0, 1)
git = lambda cwd, *a: subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True).stdout.strip()

# --- setup -------------------------------------------------------------------------------
repo = WORK / "repo"
subprocess.run(["rm", "-rf", str(WORK)], check=True)
repo.mkdir(parents=True)
git(repo, "init", "-q", "-b", "main")
(repo / "shared.txt").write_text("line from main\n")
git(repo, "add", ".")
git(repo, "-c", "user.name=probe", "-c", "user.email=probe@example.invalid", "commit", "-qm", "base")
trees, git_dirs = [], []
for i in range(N):
    # Codex keeps any directory named .git read-only even inside writable roots, so give each task
    # its own clone whose git dir has a different name (same trick as cc_delegate's .git-<task_id>).
    wt, gd = WORK / f"wt-{i}", WORK / f".git-{i}"
    subprocess.run(["git", "clone", "-q", "--local", "--no-hardlinks", f"--separate-git-dir={gd}",
                    "-b", "main", str(repo), str(wt)], check=True)
    git(wt, "checkout", "-q", "-b", f"task-{i}")
    trees.append(wt)
    git_dirs.append(str(gd))

def prompt(i):
    p = (f"You are in a git worktree. Do exactly this, using shell commands:\n"
         f"1. Append the line `edited by task-{i}` to shared.txt.\n"
         f"2. Create a file named task-{i}.txt containing `hello from task-{i}`.\n"
         f"3. Run `sleep 10`.\n"
         f"4. Commit both files: git -c user.name=task-{i} -c user.email=t{i}@example.invalid "
         f"commit -am 'task-{i}' after `git add task-{i}.txt`.\n")
    if i == 0:
        p += (f"5. Try once to create the file {repo}/escape.txt with `touch`. "
              f"Do not retry or work around a failure.\n")
    return p + "Finally reply with one line: the commit hash, and for step 5 (if any) whether it succeeded."


async def main():
    async with websockets.unix_connect(SOCK, "ws://localhost/", max_size=None) as ws:
        pending, events, counter, finals = {}, [], [0], {}

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
                m = msg.get("method")
                if "id" in msg and m is None and msg["id"] in pending:
                    pending.pop(msg["id"]).set_result(msg)
                elif "id" in msg and m:
                    events.append((now(), "server_request", m, None))
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": msg["id"],
                                              "error": {"code": -32601, "message": "probe declines"}}))
                elif m in ("turn/started", "turn/completed"):
                    p = msg["params"]
                    events.append((now(), m, p.get("threadId"), (p.get("turn") or {}).get("status")))
                elif m == "item/completed":
                    item = msg["params"].get("item", {})
                    if item.get("type") == "agentMessage":
                        finals[msg["params"].get("threadId")] = item.get("text", "")[:200]

        rt = asyncio.create_task(reader())
        await request("initialize", {"clientInfo": {"name": "cc-delegate-probe", "version": "0.0.1"},
                                     "capabilities": {"experimentalApi": False}})
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "initialized"}))
        threads = []
        for wt in trees:
            r = await request("thread/start", {"cwd": str(wt), "model": "gpt-6.2-astra",
                                               "approvalPolicy": "never", "ephemeral": True})
            threads.append(r["thread"]["id"])
        await asyncio.gather(*[request("turn/start", {
            "threadId": t, "input": [{"type": "text", "text": prompt(i), "text_elements": []}],
            "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [git_dirs[i]],
                              "networkAccess": False, "excludeSlashTmp": True, "excludeTmpdirEnvVar": True}})
            for i, t in enumerate(threads)])
        deadline = time.time() + 300
        while time.time() < deadline and len({e[2] for e in events if e[1] == "turn/completed"} & set(threads)) < N:
            await asyncio.sleep(0.5)
        await asyncio.sleep(1)
        rt.cancel()

        starts = {e[2]: e[0] for e in events if e[1] == "turn/started"}
        ends = {e[2]: (e[0], e[3]) for e in events if e[1] == "turn/completed"}
        for i, t in enumerate(threads):
            print(f"thread {i}: started {starts.get(t)}s completed {ends.get(t)} reply: {finals.get(t)!r}")
        print("server requests:", [e for e in events if e[1] == "server_request"])
        if len(ends) == N:
            wall = max(v[0] for v in ends.values()) - min(starts.values())
            serial = sum(ends[t][0] - starts[t] for t in threads)
            print(f"wall {wall:.1f}s vs sum {serial:.1f}s -> {'PARALLEL' if wall < serial * 0.7 else 'SERIAL-ish'}")

    # --- verify --------------------------------------------------------------------------
    print("\n== verification")
    print("main branch log:", git(repo, "log", "--oneline", "main"))
    print("main checkout status:", git(repo, "status", "--porcelain") or "(clean)")
    print("escape.txt exists in main checkout:", (repo / "escape.txt").exists())
    for i in range(N):
        b = f"task-{i}"
        git(repo, "fetch", "-q", str(trees[i]), f"{b}:{b}")
        files = git(repo, "diff", "--name-only", "main", b).split()
        shared = git(repo, "show", f"{b}:shared.txt").splitlines()
        foreign = [l for l in shared if l.startswith("edited by") and l != f"edited by task-{i}"]
        print(f"{b}: commits ahead={git(repo, 'rev-list', '--count', f'main..{b}')} files={files} "
              f"shared.txt={shared} foreign_edits={foreign} worktree_dirty={bool(git(trees[i], 'status', '--porcelain'))}")

asyncio.run(main())
