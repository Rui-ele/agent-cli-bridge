"""Probe: can the managed Codex app-server daemon run several threads at the same time?

Connects through `codex app-server proxy`, starts N read-only ephemeral threads,
starts one turn on each without waiting, and records when each turn starts/completes.
"""
import json, subprocess, sys, threading, time

N = int(sys.argv[1]) if len(sys.argv) > 1 else 3
CWD = sys.argv[2]
SLEEP = 20
t0 = time.time()
proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
lock = threading.Lock()
pending, events, next_id = {}, [], [0]


def now():
    return round(time.time() - t0, 1)


def send(obj):
    with lock:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()


def request(method, params):
    with lock:
        next_id[0] += 1
        rid = next_id[0]
    ev = threading.Event()
    pending[rid] = [ev, None]
    send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    if not ev.wait(120):
        raise TimeoutError(method)
    msg = pending.pop(rid)[1]
    if "error" in msg:
        raise RuntimeError(f"{method}: {msg['error']}")
    return msg["result"]


def reader():
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" in msg and msg["id"] in pending and "method" not in msg:
            pending[msg["id"]][1] = msg
            pending[msg["id"]][0].set()
        elif "id" in msg and "method" in msg:  # server -> client request (approval etc.)
            events.append((now(), "server_request", msg["method"], None))
            send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "probe declines"}})
        elif msg.get("method") in ("turn/started", "turn/completed", "thread/started", "error"):
            p = msg.get("params", {})
            tid = p.get("threadId") or (p.get("thread") or {}).get("id")
            status = (p.get("turn") or {}).get("status")
            events.append((now(), msg["method"], tid, status))


threading.Thread(target=reader, daemon=True).start()
request("initialize", {"clientInfo": {"name": "cc-delegate-probe", "version": "0.0.1"},
                       "capabilities": {"experimentalApi": False, "optOutNotificationMethods": [
                           "item/agentMessage/delta", "item/reasoning/summaryTextDelta",
                           "item/reasoning/summaryPartAdded", "item/reasoning/textDelta"]}})
send({"jsonrpc": "2.0", "method": "initialized"})

threads = []
for i in range(N):
    r = request("thread/start", {"cwd": CWD, "model": "gpt-6.2-astra", "approvalPolicy": "never",
                                 "sandbox": "read-only", "ephemeral": True})
    threads.append(r["thread"]["id"])
print(f"[{now()}s] started threads:", threads, flush=True)

# Fire all turns without waiting for the previous one to finish.
results = [None] * N

def fire(i, tid):
    results[i] = request("turn/start", {"threadId": tid, "input": [{
        "type": "text", "text_elements": [],
        "text": f"Run the shell command `sleep {SLEEP}` exactly once, then reply with only: done-{i}"}]})

ts = [threading.Thread(target=fire, args=(i, t)) for i, t in enumerate(threads)]
for t in ts:
    t.start()
for t in ts:
    t.join()

deadline = time.time() + 240
while time.time() < deadline:
    done = {e[2] for e in events if e[1] == "turn/completed"}
    if all(t in done for t in threads):
        break
    time.sleep(0.5)

proc.terminate()
print(json.dumps({"threads": threads, "events": events}, ensure_ascii=False, indent=1))
starts = {e[2]: e[0] for e in events if e[1] == "turn/started"}
ends = {e[2]: e[0] for e in events if e[1] == "turn/completed"}
for i, t in enumerate(threads):
    print(f"thread {i}: started {starts.get(t)}s  completed {ends.get(t)}s")
if len(ends) == N:
    wall = max(ends.values()) - min(starts.values())
    serial = sum(ends[t] - starts[t] for t in threads)
    print(f"wall {wall:.1f}s vs sum of turns {serial:.1f}s -> {'PARALLEL' if wall < serial * 0.7 else 'SERIAL-ish'}")
