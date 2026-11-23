"""Probe `agy acp`: what a session offers and how tools and permissions show up.

Starts `agy acp`, initializes without client fs/terminal capabilities (the agent uses its own
tools), opens a session, prints its modes and config options, then sends one prompt and logs
every update type and permission request. Permission requests get the answer given by
--answer (reject by default), so the probe never lets the agent change anything unasked.

usage: python3 probe.py CWD PROMPT [--mode MODE_ID] [--config ID=VALUE ...] [--answer reject|allow]
"""
import argparse, json, subprocess, sys, threading, time

p = argparse.ArgumentParser()
p.add_argument("cwd")
p.add_argument("prompt")
p.add_argument("--mode")
p.add_argument("--config", action="append", default=[])
p.add_argument("--answer", choices=("reject", "allow"), default="reject")
p.add_argument("--flag", action="append", default=[], help="extra agy acp flag, e.g. --sandbox")
p.add_argument("--seatbelt", help="run agy acp under sandbox-exec with this profile file")
p.add_argument("--client-fs", action="store_true", help="advertise fs read/write; log and decline calls")
args = p.parse_args()

t0 = time.time()
now = lambda: f"{time.time() - t0:5.1f}s"
proc = subprocess.Popen([*(["sandbox-exec", "-f", args.seatbelt] if args.seatbelt else []), "agy", "acp", *args.flag], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, text=True, bufsize=1)
pending, lock, counter = {}, threading.Lock(), [0]
updates, text = [], []


def send(msg):
    with lock:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        proc.stdin.flush()


def request(method, params, timeout=600):
    counter[0] += 1
    rid, ev = counter[0], threading.Event()
    pending[rid] = [ev, None]
    send({"id": rid, "method": method, "params": params})
    if not ev.wait(timeout):
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
            print(now(), "non-JSON:", line[:120].rstrip())
            continue
        if "id" in msg and "method" not in msg and msg["id"] in pending:
            pending[msg["id"]][1] = msg
            pending[msg["id"]][0].set()
        elif msg.get("method") == "session/request_permission":
            prm = msg["params"]
            opts = prm["options"]
            print(now(), "PERMISSION", json.dumps(prm["toolCall"], ensure_ascii=False)[:300])
            print("      options:", [(o["optionId"], o["kind"]) for o in opts])
            want = "allow_once" if args.answer == "allow" else "reject_once"
            pick = next((o for o in opts if o["kind"] == want), None)
            outcome = {"outcome": "selected", "optionId": pick["optionId"]} if pick else {"outcome": "cancelled"}
            send({"id": msg["id"], "result": {"outcome": outcome}})
        elif "id" in msg and "method" in msg:
            print(now(), "CLIENT REQUEST (declined):", msg["method"], json.dumps(msg.get("params"))[:200])
            send({"id": msg["id"], "error": {"code": -32601, "message": "not supported by probe"}})
        elif msg.get("method") == "session/update":
            u = msg["params"]["update"]
            kind = u.get("sessionUpdate")
            updates.append(kind)
            if kind == "agent_message_chunk":
                text.append((u.get("content") or {}).get("text", ""))
            elif kind in ("tool_call", "tool_call_update"):
                print(now(), kind, json.dumps({k: u.get(k) for k in ("toolCallId", "title", "kind", "status")},
                                              ensure_ascii=False)[:250])
            elif kind not in ("agent_thought_chunk",):
                print(now(), kind, json.dumps(u, ensure_ascii=False)[:250])
        else:
            print(now(), "other:", json.dumps(msg)[:200])


threading.Thread(target=reader, daemon=True).start()
init = request("initialize", {"protocolVersion": 1, "clientInfo": {"name": "cc-delegate-probe", "version": "0"},
                              "clientCapabilities": {"fs": {"readTextFile": args.client_fs, "writeTextFile": args.client_fs},
                                                     "terminal": False}})
print(now(), "initialize:", json.dumps(init, ensure_ascii=False)[:900])
sess = request("session/new", {"cwd": args.cwd, "mcpServers": []})
sid = sess["sessionId"]
print(now(), "session/new id:", sid)
print("      modes:", json.dumps(sess.get("modes"), ensure_ascii=False)[:700])
for opt in sess.get("configOptions") or []:
    print("      config:", json.dumps({k: opt.get(k) for k in ("id", "name", "type", "currentValue")}, ensure_ascii=False),
          "values:", [o.get("value") for o in (opt.get("options") or [])][:12])
if args.mode:
    print(now(), "set_mode:", request("session/set_mode", {"sessionId": sid, "modeId": args.mode}))
for kv in args.config:
    k, v = kv.split("=", 1)
    print(now(), "set_config_option", k, "->",
          json.dumps(request("session/set_config_option", {"sessionId": sid, "configId": k, "value": v}))[:200])
res = request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": args.prompt}]})
print(now(), "prompt result:", res)
print("update kinds:", sorted(set(updates)))
print("reply:", "".join(text)[:600])
proc.terminate()
