"""Synthetic transcripts and offline CLI doubles. Every writable path is under the checkout."""
import base64
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

import cc_delegate as cc
from tests import temp_workspace

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "cc_delegate.py"
ANSWER = {"status": "ok", "summary": ["完成"], "uncertainties": [], "report_markdown": "# report"}


class FakeDaemon:
    def __init__(self, case, outcome):
        self.case, self.outcome = case, outcome
        self.path = cc.daemon_socket()
        self.calls = {}
        self.error = None
        self.client = None
        self.proc = None
        self.interrupted = threading.Event()

    @contextlib.contextmanager
    def serving(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.ExitStack() as stack:
            listener = stack.enter_context(socket.socket(socket.AF_UNIX))
            try:
                listener.bind(os.path.relpath(self.path))
                stack.callback(self.path.unlink)
                listener.listen()
                listener.settimeout(10)
                accept = lambda: listener.accept()[0]
            except PermissionError:
                # Some sandboxes permit local socketpairs but prohibit binding a pathname.
                peer, self.client = socket.socketpair()
                stack.enter_context(peer)
                stack.enter_context(self.client)
                accept = lambda: peer
            popen = subprocess.Popen

            def launch(cmd, *args, **kwargs):
                if "codex-daemon-worker" not in cmd:
                    return popen(cmd, *args, **kwargs)
                cmd = list(cmd)
                index = cmd.index("--socket") + 1
                self.case.assertEqual(cmd[index], str(self.path))
                # Darwin's sockaddr_un may be shorter than the temporary checkout's absolute path.
                if len(os.fsencode(cmd[index])) >= 104:
                    cmd[index] = os.path.relpath(self.path, kwargs["cwd"])
                if self.client is not None:
                    fd = self.client.fileno()
                    shim = ("import asyncio,runpy,socket,sys;sys.argv=sys.argv[1:];"
                            f"asyncio.open_unix_connection=lambda path: asyncio.open_connection("
                            f"sock=socket.socket(fileno={fd}));runpy.run_path(sys.argv[0],run_name='__main__')")
                    cmd[1:1] = ["-c", shim]
                    kwargs["pass_fds"] = (fd,)
                self.proc = popen(cmd, *args, **kwargs)
                if self.client is not None:
                    self.client.close()
                return self.proc

            def serve(accept):
                try:
                    with accept() as sock, sock.makefile("rb") as stream:
                        sock.settimeout(12)
                        self.script(sock, stream)
                except Exception as exc:
                    self.error = exc

            thread = threading.Thread(target=serve, args=(accept,), daemon=True)
            thread.start()

            async def connect_control(path):
                self.case.assertEqual(Path(path), self.path)
                peer, client = socket.socketpair()
                handler = threading.Thread(target=serve, args=(lambda: peer,), daemon=True)
                handler.start()
                stack.callback(handler.join, 12)
                return await cc.asyncio.open_connection(sock=client)

            with patch.object(subprocess, "Popen", launch):
                if self.outcome == "controlled":
                    stack.enter_context(patch.object(cc.asyncio, "open_unix_connection", connect_control))
                try:
                    yield self
                finally:
                    thread.join(15)
                    self.case.assertFalse(thread.is_alive())
                    if self.error:
                        raise self.error

    def script(self, sock, stream):
        self.case.assertEqual(stream.readline(), b"GET / HTTP/1.1\r\n")
        headers = dict(line.decode().strip().split(": ", 1) for line in iter(stream.readline, b"\r\n"))
        key = headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        accept = base64.b64encode(hashlib.sha1(key.encode()).digest())
        sock.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + (b"invalid" if self.outcome == "bad_upgrade" else accept) + b"\r\n\r\n")
        if self.outcome == "bad_upgrade":
            return

        def read_frame():
            first, length = stream.read(2)
            self.case.assertTrue(length & 128)
            length &= 127
            if length >= 126:
                length = int.from_bytes(stream.read(2 if length == 126 else 8), "big")
            mask = stream.read(4)
            raw = stream.read(length)
            self.case.assertEqual(len(raw), length)
            return first, bytes(b ^ mask[i % 4] for i, b in enumerate(raw))

        def frame(raw, first=129):
            n = len(raw)
            length = bytes([n]) if n < 126 else b"\x7e" + struct.pack("!H", n)
            sock.sendall(bytes([first]) + length + raw)

        def send(msg):
            frame(cc.dumps(msg).encode())

        def receive():
            opcode, raw = read_frame()
            self.case.assertEqual(opcode, 129)
            return json.loads(raw)

        method = None

        def call(*methods):
            nonlocal method
            msg = receive()
            method = msg["method"]
            self.case.assertIn(method, methods)
            self.case.assertEqual(msg["jsonrpc"], "2.0")
            self.calls[msg["method"]] = msg.get("params")
            return msg.get("id")

        rid = call("initialize")
        if self.outcome == "bad_init":
            send({"id": rid, "error": {"code": -1, "message": "fixture init failed"}})
            return
        raw = cc.dumps({"id": rid, "result": {"userAgent": "fixture"}}).encode()
        frame(raw[:6], 1)
        frame(b"alive", 137)
        frame(raw[6:], 128)
        self.case.assertEqual(read_frame(), (138, b"alive"))
        call("initialized")
        rid = call("thread/start", "thread/resume", "turn/steer", "turn/interrupt", "thread/read")
        if method in ("turn/steer", "turn/interrupt", "thread/read"):
            if method == "thread/read":
                result = {"thread": {"turns": [{"id": "u1", "status": "inProgress", "items": [{"id": "cmd"}]},
                                               {"id": "older", "status": "completed", "items": []}]}}
            elif method == "turn/steer" and self.calls[method]["input"][0]["text"] == "reject":
                send({"id": rid, "error": {"code": -42, "message": "fixture steer rejected"}})
                return
            else:
                result = {"turnId": "u1"} if method == "turn/steer" else {}
            send({"id": rid, "result": result})
            if method == "turn/interrupt":
                self.interrupted.set()
            return
        send({"id": rid, "result": {"thread": {"id": "t1"}}})
        rid = call("thread/name/set")
        if "thread/resume" in self.calls:
            send({"id": rid, "result": {}})
        else:
            send({"id": rid, "error": {"code": -1, "message": "name unavailable"}})
        rid = call("turn/start")
        turn = {"id": "u1", "status": "inProgress"}

        def notify(method, **params):
            send({"method": method, "params": {"threadId": "t1", **params}})

        notify("turn/started", turn=turn)
        send({"id": rid, "result": {"turn": turn}})
        send({"id": 1, "method": "item/commandExecution/requestApproval", "params": {}})
        refused = receive()
        self.case.assertEqual((refused["id"], refused["error"]["code"]), (1, -32601))
        if self.outcome == "controlled":
            notify("item/completed", turnId="u1", item={"type": "commandExecution", "id": "cmd",
                                                        "command": "pwd", "exitCode": 7})
            notify("item/completed", turnId="u1", item={"type": "agentMessage", "id": "msg", "text": "中" * 210})
            self.case.assertTrue(self.interrupted.wait(10))
            turn["status"] = "interrupted"
        elif self.outcome in ("interrupt", "ignore_interrupt"):
            self.proc.send_signal(signal.SIGTERM if self.outcome == "interrupt" else signal.SIGINT)
            rid = call("turn/interrupt")
            if self.outcome == "ignore_interrupt":
                started = time.monotonic()
                self.case.assertEqual(read_frame()[0], 136)
                self.case.assertLess(time.monotonic() - started, 6)
                return
            send({"id": rid, "result": {}})
            turn["status"] = "interrupted"
        else:
            command = {"type": "commandExecution", "id": "cmd", "command": "pwd",
                       "exitCode": 0, "aggregatedOutput": "here"}
            notify("item/started", turnId="u1", item=command)
            notify("item/completed", turnId="u1", item=command)
            for i, reply in enumerate(("working", cc.dumps(ANSWER))):
                notify("item/completed", turnId="u1", item={"type": "agentMessage", "id": str(i), "text": reply})
            for total in (18, 25):
                counts = dict(inputTokens=17, cachedInputTokens=4, outputTokens=total-17,
                              reasoningOutputTokens=1, totalTokens=total)
                notify("thread/tokenUsage/updated", turnId="u1", tokenUsage={"total": counts, "last": {}})
            notify("turn/completed", turn={"id": "older", "status": "failed"})
            turn.update(status=self.outcome, error={"message": "fixture failure"} if self.outcome == "failed" else None)
        notify("turn/completed", turn=turn)
        self.case.assertEqual(read_frame()[0], 136)


class DelegateCase(unittest.TestCase):
    def setUp(self):
        self.root = temp_workspace(self)
        self.project = self.root / "project"
        self.project.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.profile = self.root / "home"
        self.profile.mkdir()
        env = patch.dict(os.environ, {"HOME": str(self.profile), "PATH": str(self.bin) + ":" + os.environ["PATH"],
                                      "CC_DELEGATE_HOME": str(self.profile / ".claude"),
                                      "CODEX_HOME": str(self.profile / ".codex"),
                                      "CC_DELEGATE_CODEX_BACKEND": "exec",
                                      "GIT_CEILING_DIRECTORIES": str(self.root),
                                      "GIT_CONFIG_NOSYSTEM": "1", "TMPDIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        for name in ("codex", "agy"):
            self.worker(name, "sys.exit(19)")
        self.session = self.root / "synthetic.jsonl"
        self.session.touch()

    def write_rows(self, rows):
        self.session.write_text("".join(cc.dumps(r) + "\n" for r in rows))

    def worker(self, name, code):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\nimport json, os, sys, time\nfrom pathlib import Path\n" + code)
        path.chmod(0o700)

    def run_task(self, *args, worker="codex", expected=0):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cc.main(["run", "--to", worker, "--model", "synthetic", "--cc-session",
                            str(self.session), "--project-dir", str(self.project), "--task", "验收", *args,
                            *(["--no-fallback"] if worker == "agy" else [])])
        self.assertEqual(expected, code, buf.getvalue())
        lines = buf.getvalue().splitlines()
        self.receipt = buf.getvalue()
        self.assertLessEqual(len(lines), 15)
        d = Path(json.loads(lines[-1].split("=", 1)[1]))
        self.assertTrue(d.is_relative_to(self.root))
        return d, json.loads((d / "result.json").read_text())

    def test_daemon_readonly_receipt_and_events(self):
        self.worker("codex", "sys.exit(19)")
        with FakeDaemon(self, "completed").serving() as server, \
                patch.dict(os.environ, {"CC_DELEGATE_CODEX_BACKEND": "daemon"}):
            d, data = self.run_task("--effort", "high", "--task", "验收" * 12000)
        self.assertEqual(data, json.loads((d / "meta.json").read_text()))
        self.assertEqual((data["status"], data["backend"], data["worker_session_id"]), ("ok", "daemon", "t1"))
        self.assertEqual(data["resume_command"], "codex resume t1")
        self.assertIn('backend="daemon"', self.receipt)
        self.assertIn('resume_command="codex resume t1"', self.receipt)
        self.assertEqual(json.loads((d / "last.txt").read_text()), ANSWER)
        self.assertEqual(data["worker_usage"], dict(input_tokens=17, cached_input_tokens=4, output_tokens=8,
                                                    reasoning_output_tokens=1, total_tokens=25))
        self.assertFalse(server.calls["initialize"]["capabilities"]["experimentalApi"])
        self.assertEqual(server.calls["thread/start"], dict(cwd=str(self.project), model="synthetic",
                         approvalPolicy="never", sandbox="read-only", ephemeral=False))
        self.assertEqual(server.calls["thread/name/set"], dict(threadId="t1", name=d.name))
        turn = server.calls["turn/start"]
        self.assertEqual(turn["sandboxPolicy"], dict(type="readOnly", networkAccess=False))
        self.assertEqual((turn["outputSchema"], turn["effort"]), (cc.SCHEMA, "high"))
        self.assertEqual(turn["input"], [{"type": "text", "text": (d / "package.md").read_text(), "text_elements": []}])
        events = list(cc.records(d / "events.jsonl"))
        cmd = next(e["item"] for e in events if e["type"] == "item.completed")
        self.assertEqual(cmd, dict(id="cmd", type="command_execution", command="pwd", exit_code=0, aggregated_output="here"))
        self.assertIn("server_request.rejected", [e["type"] for e in events])
        self.assertIn("thread.name_failed", [e["type"] for e in events])

    def test_daemon_write_resume(self):
        self.commit_fixture()
        self.worker("codex", "sys.exit(19)")
        with FakeDaemon(self, "completed").serving() as server:
            d, data = self.run_task("--backend", "daemon", "--mode", "write", "--resume", "t1")
        checkout = Path(data["worktree"])
        gitdir, scratch = (str(checkout.parent / (prefix + d.name)) for prefix in (".git-", ".tmp-"))
        self.assertEqual(server.calls["thread/resume"], dict(threadId="t1", model="synthetic", cwd=str(checkout),
                         approvalPolicy="never", sandbox="workspace-write", config={"shell_environment_policy.set.TMPDIR": scratch}))
        self.assertEqual(server.calls["turn/start"]["sandboxPolicy"], dict(type="workspaceWrite",
                         writableRoots=[gitdir, scratch], networkAccess=False, excludeSlashTmp=True,
                         excludeTmpdirEnvVar=True))
        self.assertEqual((data["backend"], data["status"]), ("daemon", "ok"))

    def test_daemon_fallback(self):
        self.worker("codex", "assert sys.stdin.read().endswith('验收')\n"
                    "print(json.dumps(" + repr(ANSWER) + "))")
        for fault in ("missing", "bad_upgrade", "bad_init", "search"):
            with self.subTest(fault=fault), contextlib.ExitStack() as stack:
                if fault in ("bad_upgrade", "bad_init"):
                    stack.enter_context(FakeDaemon(self, fault).serving())
                else:
                    stack.enter_context(patch.object(cc, "daemon_socket", return_value=Path("missing.sock")))
                opts = ["--search"] if fault == "search" else []
                d, data = self.run_task("--backend", "daemon", *opts)
                self.assertEqual(data, json.loads((d / "meta.json").read_text()))
                self.assertEqual((data["backend"], data["status"]), ("exec", "ok"))
                cmd = cc.shlex.split((d / "command.txt").read_text())
                self.assertEqual(cmd[0], "codex")
                self.assertEqual("--search" in cmd, fault == "search")
                if fault == "search":
                    self.assertEqual(data["backend_fallback"], "search_unsupported_on_daemon")
                else:
                    self.assertEqual(data["backend_fallback"], "daemon_unavailable")
                    self.assertTrue(data["backend_fallback_reason"])
                self.assertIn('backend="exec"', self.receipt)
                self.assertNotIn("resume_command", data)

    def test_daemon_failed_and_interrupted_turn(self):
        self.worker("codex", "sys.exit(19)")
        for outcome in ("failed", "interrupt", "ignore_interrupt"):
            with self.subTest(outcome=outcome), FakeDaemon(self, outcome).serving() as server:
                d, data = self.run_task("--backend", "daemon", expected=2)
                self.assertEqual((data["backend"], data["status"], data["exit_code"]), ("daemon", "outcome_unknown", 1))
                self.assertNotIn("backend_fallback", data)
                self.assertEqual(data["resume_command"], "codex resume t1")
                failure = next(e["error"] for e in cc.records(d / "events.jsonl") if e["type"] == "turn.failed")
                if outcome != "failed":
                    self.assertEqual(server.calls["turn/interrupt"], dict(threadId="t1", turnId="u1"))
                    self.assertEqual(failure, "interrupted" if outcome == "interrupt" else "CancelledError")
                else:
                    self.assertEqual(failure["message"], "fixture failure")

    def test_backend_selection(self):
        with patch.dict(os.environ, {"CC_DELEGATE_CODEX_BACKEND": "daemon"}):
            d, data = self.run_task("--backend", "exec", "--dry-run")
            self.assertEqual(data["backend"], "exec")
            self.assertEqual(cc.shlex.split((d / "command.txt").read_text())[0], "codex")
            d, data = self.run_task("--backend", "daemon", "--dry-run", worker="agy")
            self.assertNotIn("backend", data)
            self.assertEqual(cc.shlex.split((d / "command.txt").read_text())[0], "agy")

    def control(self, *args, expected=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cc.main(list(args)), expected, err.getvalue())
        return err.getvalue() if expected else out.getvalue()

    def wait_for_event(self, value):
        deadline = time.monotonic() + 6
        runs = cc.home() / "delegate-runs"
        while time.monotonic() < deadline:
            for path in runs.glob("*/events.jsonl"):
                if value in path.read_text():
                    return path.parent
            time.sleep(.02)
        self.fail(f"missing worker event {value}")

    def test_control_live_daemon(self):
        message = "补" * 200 + "完整消息结尾"

        def control_turn():
            d = self.wait_for_event("agent_message")
            meta = json.loads((d / "meta.json").read_text())
            state = json.loads((d / "daemon.json").read_text())
            self.assertEqual(state, dict(socket=str(server.path), thread_id="t1", turn_id="u1",
                                         worker_pid=meta["worker_pgid"]))
            status = self.control("status", d.name)
            self.assertLessEqual(len(status.splitlines()), 15)
            for field in ('backend="daemon"', 'mode="ro"', 'elapsed_s=', 'command="pwd" exit_code=7',
                          'turn_status="inProgress" item_count=1', 'agent_message="' + "中" * 200 + '"'):
                self.assertIn(field, status)
            self.assertEqual(server.calls["thread/read"], {"threadId": "t1", "includeTurns": True})
            self.assertIn("fixture steer rejected", self.control("steer", d.name, "--message", "reject", expected=1))
            self.assertEqual(len(self.control("steer", d.name, "--message", message).splitlines()), 1)
            self.assertEqual(server.calls["turn/steer"], dict(threadId="t1", expectedTurnId="u1",
                             input=[dict(type="text", text=message, text_elements=[])]))
            self.assertEqual(cc.main(["cancel", d.name]), 0)
            return d

        with FakeDaemon(self, "controlled").serving() as server, ThreadPoolExecutor(1) as pool, \
                contextlib.redirect_stdout(io.StringIO()), patch.object(cc, "TERM_GRACE", .05):
            control = pool.submit(control_turn)
            rc = cc.main(["run", "--to", "codex", "--model", "synthetic", "--cc-session", str(self.session),
                          "--project-dir", str(self.project), "--task", "验收", "--backend", "daemon"])
            d = control.result()
        data = json.loads((d / "result.json").read_text())
        self.assertEqual((rc, data["status"], data["reason"]), (2, "cancelled", "cancelled_by_user"))
        self.assertEqual(data["resume_command"], "codex resume t1")
        self.assertEqual(server.calls["turn/interrupt"], dict(threadId="t1", turnId="u1"))
        self.assertEqual(json.loads((d / "daemon.json").read_text())["turn_status"], "interrupted")
        log = list(cc.records(d / "control.jsonl"))
        self.assertEqual([(r["action"], r["result"]) for r in log],
                         [("steer", "error"), ("steer", "ok"), ("cancel", "requested"), ("interrupt", "ok")])
        self.assertEqual(log[1]["message"], message[:200])
        self.assertTrue(all(r["ts"] for r in log))
        self.control("mark", d.name, "--outcome", "discarded")
        self.assertIn('status="cancelled"', self.control("cancel", d.name))
        self.assertEqual(json.loads((d / "result.json").read_text())["outcome"], "discarded")

    def test_steer_blocked_and_offline_status(self):
        d = cc.home() / "delegate-runs/20261123-120000-codex-abcd"
        d.mkdir(parents=True)
        meta = dict(backend="exec", mode="ro", started_at=cc.now())
        cc.save_json(d / "meta.json", meta)

        def check(reason):
            self.assertEqual(self.control("steer", d.name, "--message", "test", expected=1).strip(), reason)

        check("steer_requires_daemon_backend")
        cc.save_json(d / "meta.json", dict(meta, backend="daemon"))
        check("turn_not_started")
        cc.save_json(d / "daemon.json", dict(socket=str(self.root / "missing"), thread_id="t1", turn_id="u1"))
        (d / "events.jsonl").write_text('{"item":{"type":"command_execution","command":"ls","exit_code":1}}\n{')
        status = self.control("status", d.name)
        self.assertIn('command="ls" exit_code=1', status)
        self.assertIn("daemon_unavailable=", status)
        cc.save_json(d / "result.json", meta)
        check("task_finished")

    def test_cancel_exec_signals_worker(self):
        self.check_cancel_exec(writing=False)

    def test_status_reads_existing_agy_stream(self):
        d = cc.home() / "delegate-runs/20261123-120000-agy-abcd"
        d.mkdir(parents=True)
        cc.save_json(d / "meta.json", dict(worker="agy", mode="ro", started_at=cc.now()))
        rows = [
            {"step_update": {"tool_name": "run_command", "tool_info": {
                "parameters": {"CommandLine": "pwd"}, "exit_code": 0}}},
            {"step_update": {"step_type": "message", "text": "checking"}},
            {"event": "step_update", "step_update": {"tool_name": "run_command", "tool_info": {
                "parameters": {"CommandLine": "git diff --check"}, "exit_code": 1}}},
        ]
        (d / "events.jsonl").write_text("\n".join(cc.dumps(row) for row in rows) + "\n{")
        lines = self.control("status", d.name).splitlines()
        self.assertIn('backend="agy"', lines[0])
        self.assertEqual(lines[1:], ['command="git diff --check" exit_code=1', 'agent_message="checking"'])

    def test_cancel_write_keeps_commit_off_onto(self):
        self.check_cancel_exec(writing=True)

    def check_cancel_exec(self, writing):
        args = []
        setup = ""
        if writing:
            self.commit_fixture()
            cc.git(self.project, "branch", "feature")
            args += ["--mode", "write", "--onto", "feature"]
            setup = """import subprocess, signal
Path("a").write_text("partial work")
subprocess.run(["git","-c","user.name=W","-c","user.email=w@x.invalid","commit","-qam","partial"], check=True)
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps(""" + repr(ANSWER) + "))\n"
        table = self.root / "process-table"
        self.worker("codex", setup + f"Path({str(table)!r}).write_text(f'{{os.getpid()}} {{os.getpgrp()}} ' + ' '.join(sys.argv))\n"
                    "print(json.dumps({'type':'item.completed','item':{'type':'command_execution',"
                    "'command':'fixture_ready','exit_code':0}}), flush=True)\ntime.sleep(20)")
        try:
            subprocess.run(["ps", "-p", str(os.getpid()), "-o", "pid="], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            # In a restricted sandbox the worker supplies its own PID, PGID and argv.
            self.worker("ps", f"assert sys.argv[1:] == ['-axww', '-o', 'pid=,pgid=,command=']\n"
                        f"print(Path({str(table)!r}).read_text())")
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "run", "--to", "codex", "--model", "synthetic",
                                 "--cc-session", str(self.session), "--project-dir", str(self.project),
                                 "--task", "验收", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            d = self.wait_for_event("fixture_ready")
            self.assertIn('command="fixture_ready" exit_code=0', self.control("status", d.name))
            self.control("cancel", d.name, "--wait", "0")
            stdout, stderr = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.terminate()
                proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 2, stderr)
        data = json.loads((d / "result.json").read_text())
        self.assertIn('status="cancelled"', stdout)
        self.assertEqual(data["reason"], "cancelled_by_user")
        self.assertEqual(data["exit_code"], 0 if writing else -signal.SIGINT)
        self.assertEqual(data["worker_pid"], data["worker_pgid"])
        actions = [(r["action"], r["result"]) for r in cc.records(d / "control.jsonl")]
        self.assertEqual(actions, [("cancel", "requested"), ("SIGINT", "sent")])
        if writing:
            self.assertEqual(cc.git(self.project, "rev-parse", "feature").strip(), data["base_sha"])
            self.assertEqual(cc.git(self.project, "show", f"delegate/{d.name}:a"), "partial work")
            self.assertIn("onto_skipped_unsuccessful", data["uncertainties"])

    def test_cancel_requires_matching_process_group(self):
        d = cc.home() / "delegate-runs/20261123-120000-codex-abcd"
        d.mkdir(parents=True)
        cc.save_json(d / "meta.json", dict(backend="exec", worker_pgid=43210))
        self.worker("ps", f"print('43210 43210 unrelated\\n54321 54321 codex {d}/last.txt')")
        with patch.object(os, "killpg") as kill:
            error = self.control("cancel", d.name, "--wait", "0", expected=1)
        self.assertEqual(error.strip(), "worker_not_found")
        kill.assert_not_called()
        self.assertTrue((d / "cancel.requested").exists())
        self.assertEqual(list(cc.records(d / "control.jsonl"))[-1]["result"], "worker_not_found")

    def test_cancel_rechecks_group_before_kill(self):
        d = cc.home() / "delegate-runs/20261123-120000-codex-abcd"
        d.mkdir(parents=True)
        cc.save_json(d / "meta.json", dict(backend="daemon", worker_pgid=43210))
        cc.save_json(d / "daemon.json", dict(socket=str(self.root / "missing"), thread_id="t1", turn_id="u1"))
        match = {43211: f"worker -o {d}/last.txt"}
        for after_term, expected in ((match, 0), ({43210: "another task"}, 1)):
            with self.subTest(expected=expected), patch.object(cc, "wait_result", return_value=False) as wait, \
                    patch.object(cc, "group_commands", side_effect=[match, after_term, after_term]), \
                    patch.object(os, "killpg") as kill:
                self.control("cancel", d.name, "--wait", "0", expected=expected)
                self.assertEqual([call.args[1] for call in wait.call_args_list], [0, 5, 5][:3 - expected])
                signals = [signal.SIGINT] if expected else [signal.SIGINT, signal.SIGTERM, signal.SIGKILL]
                self.assertEqual([call.args for call in kill.call_args_list], [(43210, sig) for sig in signals])
                self.assertTrue(any(r["action"] == "interrupt" and r["result"] == "error"
                                    for r in cc.records(d / "control.jsonl")))

    def test_transcript_and_tiers(self):
        rows = []
        for i in range(5):
            rows += [{"type": "user", "message": {"content": f"question{i}"}},
                     {"type": "assistant", "message": {"id": str(i), "content": [
                         {"type": "text", "text": f"answer{i}"}, {"type": "thinking", "thinking": "hidden"}]}},
                     {"type": "assistant", "message": {"id": str(i), "content": [
                         {"type": "tool_use", "name": "Bash", "input": {"description": "build", "other": "FULL_INPUT"}}]}},
                     {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": str(i),
                         "content": [{"type": "text", "text": f"line{i}\nbody{i}" + "x"*4200},
                                     {"type": "image", "source": {"data": "hidden"}}]}]}}]
        rows.extend([{"type": "user", "isMeta": True, "message": {"content": "hidden"}},
                     {"type": "attachment", "content": "hidden"}, {"type": "api-request-blob", "text": "hidden"},
                     {"type": "cost-state", "cost": 2}, {"type": "cost-state", "cost": 4},
                     {"type": "assistant", "message": {"content": [{"type": "text",
                      "text": "first<system-reminder>hidden\ncontent</system-reminder>last"}]}}])
        self.write_rows(rows)
        with self.session.open("ab") as f:
            f.write(b'{"type":')
        messages, cost = cc.load_session(self.session, True)
        self.assertEqual(cost["cost"], 4)
        self.assertEqual(len([1 for r, _ in messages if r == "assistant"]), 6)
        include = self.project / "excerpt"
        include.write_text("included\n"*5 + "sixth line")
        for tier in ("none", "scoped", "session", "full"):
            p, _ = cc.package("finish", messages, [include], tier, "ro", 100000)
            with self.subTest(tier=tier):
                self.assertTrue(p.endswith("## 当前任务\nfinish"))
                self.assertNotIn("hidden", p)
                self.assertNotIn("sixth line", p)
                # Includes are chosen files, not conversation, so every tier names them.
                self.assertIn("included", p)
                self.assertEqual("question0" in p, tier in ("session", "full"))
                self.assertEqual("question2" in p, tier != "none")
                self.assertEqual("answer1" in p, tier in ("session", "full"))
                self.assertEqual("firstlast" in p, tier != "none")
                self.assertEqual("[tool Bash]" in p, tier in ("session", "full"))
                self.assertEqual("body1" in p, tier == "full")
                self.assertEqual("body2" in p, tier in ("session", "full"))
                self.assertEqual("FULL_INPUT" in p, tier == "full")
                self.assertEqual("[image omitted]" in p, tier == "full")
                if tier == "session":
                    self.assertIn("省略", p)
                    self.assertEqual(p.count("(截断"), 3)

    def test_budget_priority_and_overflow(self):
        messages = [("user", [{"type": "text", "text": "keep-user"}]),
                    ("assistant", [{"type": "text", "text": "a"*900},
                                   {"type": "tool_use", "name": "Read", "input": {"path": "b"*600}}]),
                    ("user", [{"type": "tool_result", "content": "line\n" + "c"*800}])]
        for size, keep_tool, keep_cc in [(1300, True, True), (850, False, True), (350, False, False)]:
            p, _ = cc.package("keep-task", messages, [], "full", "ro", size)
            self.assertLessEqual(len(p), int(size * .95 * .5)*3)
            self.assertIn("keep-user", p)
            self.assertIn("keep-task", p)
            self.assertNotIn("c"*800, p)
            self.assertEqual("b"*600 in p, keep_tool)
            self.assertEqual("a"*900 in p, keep_cc)
        with self.assertRaisesRegex(cc.Blocked, "context_overflow"):
            cc.package("t"*1000, messages, [], "full", "ro", 200)

    def test_redaction_before_preview_and_denied_files(self):
        raw = ("sk-abcdefghijklmnop AKIA1234567890123456 ghp_abcdefghijklmnopqrst "
               "github_pat_abcdefghijklmnopqrst xoxb-0123456789 xoxp-0123456789 "
               "api_key=example token: hello secret=hidden password='with spaces'\n"
               "-----BEGIN RSA PRIVATE KEY-----\nSYNTHETIC\n-----END RSA PRIVATE KEY-----")
        p, n = cc.package(raw, [], [], "none", "ro", 5000)
        self.assertEqual(n, 11)
        self.assertEqual(p.count("[REDACTED]"), n)
        self.assertNotIn("with spaces", p)
        self.assertNotIn("SYNTHETIC", p)
        target = self.project / ".env.test"
        target.touch()
        link = self.project / "notes"
        link.symlink_to(target)
        for path in (link, target, self.project / "id_ed25519", self.project / "credentials.json"):
            with self.assertRaisesRegex(cc.Blocked, "sensitive"):
                cc.safe_file(path)

    def test_session_selection_and_window(self):
        name = cc.re.sub(r"[^a-zA-Z0-9]", "-", str(self.project))
        folder = cc.home() / "projects" / name
        folder.mkdir(parents=True)
        oldest, recent = folder / "a.jsonl", folder / "b.jsonl"
        oldest.touch()
        recent.touch()
        os.utime(oldest, (1, 1))
        self.assertEqual(cc.session_path(self.project, None), recent)
        self.assertEqual(cc.session_path(self.project, "a"), oldest)
        cache = self.profile / ".codex" / "models_cache.json"
        cache.parent.mkdir()
        cc.save_json(cache, {"models": [{"slug": "model", "context_window": 12345}]})
        self.assertEqual(cc.window("codex", "model"), (12345, False))
        self.assertEqual(cc.window("codex", "missing"), (272000, True))
        self.assertEqual(cc.window("agy", "missing"), (200000, True))

    def test_dry_run_and_resume_args(self):
        self.write_rows([{"type": "user", "message": {"content": "old user content"}}])
        for worker in ("codex", "agy"):
            d, data = self.run_task("--dry-run", "--resume", "id", "--effort", "high", "--search", worker=worker)
            self.assertEqual(data["status"], "dry_run")
            self.assertNotIn("old user content", (d / "package.md").read_text())
            cmd = cc.shlex.split((d / "command.txt").read_text())
            self.assertNotIn("验收", cmd)
            if worker == "codex":
                self.assertLess(cmd.index("--search"), cmd.index("exec"))
                self.assertLess(cmd.index("-s"), cmd.index("resume"))
                self.assertEqual(cmd[cmd.index("resume")+1:][:2], ["-c", 'sandbox_mode="read-only"'])
                self.assertEqual(cmd[-2:], ["id", "-"])
            else:
                self.assertIn("--conversation", cmd)
                self.assertNotIn("--continue", cmd)
                self.assertIn("write_file(*)", json.loads((d / "settings.json").read_text())["permissions"]["deny"])

    def test_invalid_schema_and_raw_fallback(self):
        for bad in ({**ANSWER, "changed_files": ["bogus"]}, {**ANSWER, "status": "made-up"},
                    {**ANSWER, "summary": [42]}, {**ANSWER, "summary": "invalid"},
                    {**ANSWER, "uncertainties": "no"}, "broken"):
            raw = json.dumps(bad)
            result = cc.parse_reply(raw)
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["report_markdown"], raw)
        self.worker("agy", "sys.stdin.read()\nprint('malformed stdout')\n")
        d, data = self.run_task(worker="agy")
        self.assertEqual(data["status"], "partial")
        self.assertEqual((d / "report.md").read_text(), "malformed stdout")

    def test_agy_stream_and_mark(self):
        self.write_rows([{"type": "cost-state", "value": 5}])
        self.worker("agy",
                    'packet = json.loads(sys.stdin.read())\n'
                    'assert packet["event"] == "user" and packet["message"]["content"].endswith("验收")\n'
                    f'print(json.dumps({{"event":"result","result":{{"conversation_id":"c1","usage":{{"total_tokens":9}},'
                    f'"status":"SUCCESS","structured_output":{ANSWER!r}}}}}))\n')
        d, data = self.run_task(worker="agy")
        self.assertEqual(data["status"], "ok")
        self.assertGreater(data["worker_pid"], 0)
        self.assertEqual(data["worker_pid"], data["worker_pgid"])
        self.assertEqual(data["worker_session_id"], "c1")
        self.assertEqual(data["worker_usage"]["total_tokens"], 9)
        self.assertEqual((d / "report.md").read_text(), "# report")
        self.assertEqual(data["cc_cost_before"]["value"], 5)
        self.write_rows([{"type": "cost-state", "value": 7}])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cc.main(["mark", d.name, "--outcome", "edited", "--note", "checked"]), 0)
            self.assertEqual(cc.main(["status", d.name]), 0)
        rows = list(cc.records(cc.home() / "delegate-log.jsonl"))
        self.assertEqual([r["event"] for r in rows], ["run", "mark"])
        self.assertEqual(rows[-1]["cc_cost_after"]["value"], 7)
        self.assertEqual(rows[-1]["outcome"], "edited")

    def test_codex_loop_warnings_and_worktree(self):
        cc.git(self.project, "init", "-q")
        (self.project / "a").write_text("before\n")
        cc.git(self.project, "add", "a")
        cc.git(self.project, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
               "commit", "-qm", "fixture")
        self.worker("codex", """
assert sys.stdin.read().endswith("验收")
print(json.dumps({"type":"thread.started","thread_id":"thread1"}))
for i in range(3):
    for lifecycle in ("item.started","item.completed"):
        print(json.dumps({"type":lifecycle,"item":{"type":"command_execution","id":str(i),
                                                  "command":"ls" + " "*(i+1) + "-l"}}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":17}}))
Path("a").write_text("after\\n")
Path("new name").write_text("new")
Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps(""" + repr(ANSWER) + "))\n")
        d, data = self.run_task("--mode", "write")
        self.assertTrue(data["loop_warning"])
        self.assertEqual(sum(e.get("type") == "loop_warning" for e in cc.records(d / "events.jsonl")), 1)
        self.assertEqual(data["worker_session_id"], "thread1")
        self.assertEqual(data["worker_usage"]["input_tokens"], 17)
        self.assertEqual(data["verification"], [])
        self.assertEqual(data["changed_files"], ["a", "new name"])
        self.assertEqual(data["diff_stat"], [{"path":"a", "added":"1","deleted":"1"}])
        self.assertEqual((self.project / "a").read_text(), "before\n")
        self.assertEqual(cc.git(Path(data["worktree"]), "branch", "--show-current").strip(), "delegate/"+d.name)

    def test_failure_and_stall(self):
        self.worker("codex", "sys.stdin.read()\nsys.exit(3)\n")
        _, data = self.run_task(expected=2)
        self.assertEqual(data["exit_code"], 3)
        self.worker("codex", "sys.stdin.read()\ntime.sleep(60)\n")
        _, data = self.run_task("--stall-seconds", "0.3", expected=2)
        self.assertEqual(data["reason"], "stalled")
        self.assertEqual(data["exit_code"], -signal.SIGINT)  # asked to stop first, like Ctrl-C

    def test_filesystem_activity_without_stdout(self):
        self.worker("codex", """
sys.stdin.read()
last = Path(sys.argv[sys.argv.index("-o")+1])
for _ in range(8):
    time.sleep(.1)
    last.touch()
last.write_text(json.dumps(""" + repr(ANSWER) + "))\n")
        _, data = self.run_task("--stall-seconds", "0.4")
        self.assertEqual(data["status"], "ok")
        self.assertGreater(data["duration_s"], .8)

    def test_repo_locks(self):
        (cc.home() / "delegate-runs").mkdir(parents=True)
        with contextlib.ExitStack() as stack:
            # Writers run in private clones, so several share a repository; one per --onto branch.
            cc.slot(stack, self.project, self.project, "write", "feature")
            with self.assertRaisesRegex(cc.Blocked, "onto_branch_busy"):
                cc.slot(stack, self.project, self.project, "write", "feature")
            cc.slot(stack, self.project, self.project, "write", "other")
            cc.slot(stack, self.project, self.project, "write")
            cc.slot(stack, self.project, self.project, "write")
            with self.assertRaisesRegex(cc.Blocked, "writers_busy"):
                cc.slot(stack, self.project, self.project, "write")
            for _ in range(3):
                cc.slot(stack, self.project, self.project, "ro")
            with self.assertRaisesRegex(cc.Blocked, "readers_busy"):
                cc.slot(stack, self.project, self.project, "ro")
        with contextlib.ExitStack() as stack:
            cc.slot(stack, self.project, self.project, "write", "feature")
        with patch.dict(os.environ, {"CC_DELEGATE_MAX_READERS": "5", "CC_DELEGATE_MAX_WRITERS": "1"}), \
                contextlib.ExitStack() as stack:
            for _ in range(5):
                cc.slot(stack, self.project, self.project, "ro")
            with self.assertRaisesRegex(cc.Blocked, "readers_busy"):
                cc.slot(stack, self.project, self.project, "ro")
            cc.slot(stack, self.project, self.project, "write")
            with self.assertRaisesRegex(cc.Blocked, "writers_busy"):
                cc.slot(stack, self.project, self.project, "write")
        with patch.dict(os.environ, {"CC_DELEGATE_MAX_READERS": "zero"}), contextlib.ExitStack() as stack:
            for _ in range(3):  # unparsable limits fall back to the default
                cc.slot(stack, self.project, self.project, "ro")
            with self.assertRaisesRegex(cc.Blocked, "readers_busy"):
                cc.slot(stack, self.project, self.project, "ro")

    def test_breaches_ignore_only_sibling_landings(self):
        state = lambda head, refs, branch="refs/heads/feature": {
            "head": head, "branch": branch, "status": "s", "refs": refs}
        before = state("a", {"refs/heads/feature": "a", "refs/heads/other": "x"})
        chain = [{"ref": "refs/heads/feature", "old": "a", "new": "b"},
                 {"ref": "refs/heads/feature", "old": "b", "new": "c"}]
        # The checked-out branch fast-forwarded twice by siblings: HEAD moves with it.
        self.assertEqual(cc.breaches(before, state("c", {"refs/heads/feature": "c", "refs/heads/other": "x"}),
                                     chain), [])
        # Same end state with no landing on record: the worker did it.
        self.assertEqual(cc.breaches(before, state("c", {"refs/heads/feature": "c", "refs/heads/other": "x"}),
                                     []), ["head", "refs"])
        # A landing explains one ref; a new branch alongside it is still flagged.
        after = state("c", {"refs/heads/feature": "c", "refs/heads/other": "x", "refs/heads/escaped": "c"})
        self.assertEqual(cc.breaches(before, after, chain), ["refs"])
        # A landing that does not start from the snapshot value explains nothing.
        self.assertEqual(cc.breaches(before, state("a", {"refs/heads/feature": "a", "refs/heads/other": "y"}),
                                     [{"ref": "refs/heads/other", "old": "w", "new": "y"}]), ["refs"])
        # Switching branches is never a sibling's doing.
        self.assertEqual(cc.breaches(before, state("x", before["refs"], "refs/heads/other"), chain),
                         ["branch", "head"])

    def test_concurrent_jsonl_appends(self):
        cc.home().mkdir()
        code = ('import cc_delegate as c, sys\nfor i in range(8):\n'
                ' c.append_log(dict(task_id=sys.argv[1]+"-"+str(i),note="x"*12000), "run")')
        children = [subprocess.Popen([sys.executable, "-c", code, str(i)], cwd=ROOT,
                                     stdin=subprocess.DEVNULL) for i in range(6)]
        self.assertEqual([p.wait() for p in children], [0]*6)
        lines = (cc.home() / "delegate-log.jsonl").read_text().splitlines()
        self.assertEqual(len({json.loads(s)["task_id"] for s in lines}), 48)

    def test_stalled_group_with_exited_leader(self):
        # The leader exits successfully while its descendant keeps stdout open.
        marker = self.project / "survived"
        child = ("import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 f"time.sleep(1.6); pathlib.Path({str(marker)!r}).touch()")
        self.worker("codex", "import subprocess\nsys.stdin.read()\n"
                    f"subprocess.Popen([sys.executable, '-c', {child!r}])\n")
        with patch.object(cc, "TERM_GRACE", .2):
            _, data = self.run_task("--stall-seconds", ".3", expected=2)
        self.assertEqual(data["reason"], "stalled")
        time.sleep(1.4)
        self.assertFalse(marker.exists())

    def test_blocked_overflow_write_without_git_and_unknown_task(self):
        self.write_rows([{"type": "user", "message": {"content": "X"*3000}}])
        with patch.object(cc.CodexExec, "window", return_value=(500, True)):
            d, data = self.run_task("--context", "session", expected=1)
        self.assertEqual(data["reason"], "context_overflow")
        self.assertEqual(data["status"], "blocked")
        self.assertFalse((d / "command.txt").exists())
        _, data = self.run_task("--mode", "write", "--dry-run", expected=1)
        self.assertEqual(data["reason"], "write_requires_git_head")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cc.main(["status", "../example"]), 1)

    def test_nested_secret_inputs_and_image(self):
        blocks = [("assistant", [{"type": "tool_use", "id": "r", "name": "Read", "input": {
            "file_path": "/fixture/.env.test", "token": "opaque_data", "nested": {"type": "image",
                                                                                "data": "IMAGE_DATA"}}}]),
                  ("user", [{"type": "tool_result", "tool_use_id": "r", "content": "opaque_data"}])]
        p, n = cc.package("review", blocks, [], "full", "ro", 2000)
        self.assertNotIn("opaque_data", p)
        self.assertNotIn("IMAGE_DATA", p)
        self.assertIn("[image omitted]", p)
        self.assertEqual(n, 2)

    def test_receipt_limits_and_newlines(self):
        data = dict(summary=["x\ny"]*8, uncertainties=["a\x1bb"*1000], mode="write",
                    changed_files=["a\nb"], worktree="work tree", loop_warning=True, outcome="edited")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cc.receipt(data)
        self.assertEqual(len(buf.getvalue().splitlines()), 15)
        self.assertNotIn("\x1b", buf.getvalue())

    def test_settings_exact_paths_and_preview(self):
        (self.project / "nested").mkdir()
        (self.project / "nested" / "demo.pem").touch()
        (self.project / ".env.local").touch()
        cwd = self.root / "planned-tree"
        rules = cc.agy_settings(cwd, "ro", self.project)["permissions"]["deny"]
        for rule in (f"read_file({cwd / 'nested/demo.pem'})", f"read_file({cwd / '.env.local'})",
                     f"read_file({Path.home() / '.ssh'})", "command(*)"):
            self.assertIn(rule, rules)

    def test_write_dry_run_has_no_checkout_side_effects(self):
        cc.git(self.project, "init", "-q")
        cc.git(self.project, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
               "commit", "--allow-empty", "-qm", "fixture")
        d, data = self.run_task("--mode", "write", "--dry-run")
        self.assertFalse((self.root / ".wt-delegate").exists())
        self.assertFalse((self.project / ".git/cc-delegate.lock").exists())
        self.assertIn(".wt-delegate", (d / "command.txt").read_text())
        self.assertIsNone(data["worktree"])
    def commit_fixture(self):
        cc.git(self.project, "init", "-q", "-b", "main")
        (self.project / "a").write_text("before\n")
        cc.git(self.project, "add", "a")
        cc.git(self.project, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
               "commit", "-qm", "fixture")

    def test_write_checkout_is_isolated_and_breach_is_reported(self):
        self.commit_fixture()
        main_head = cc.git(self.project, "rev-parse", "HEAD").strip()
        # The worker commits in its checkout, then tries to move the user's branch the way
        # a shared-refs worktree allowed (git -C <main> merge/update-ref).
        self.worker("codex", f"""
sys.stdin.read()
import subprocess
Path("a").write_text("after\\n")
subprocess.run(["git","-c","user.name=W","-c","user.email=w@x.invalid","commit","-qam","w"], check=True)
subprocess.run(["git","rev-parse","--git-common-dir"], check=True)
assert subprocess.run(["git","remote"], capture_output=True, text=True).stdout == ""
Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({ANSWER!r}))
if os.environ.get("ESCAPE"):
    subprocess.run(["git","-C",{str(self.project)!r},"branch","escaped"], check=True)
""")
        d, data = self.run_task("--mode", "write")
        cmd = (d / "command.txt").read_text()
        self.assertIn("approval_policy=\"never\"", cmd)
        self.assertIn("writable_roots", cmd)
        self.assertEqual(cc.git(self.project, "rev-parse", "main").strip(), main_head)
        self.assertNotIn("isolation_breach", data)
        branch = cc.git(self.project, "rev-parse", "delegate/" + d.name).strip()
        self.assertNotEqual(branch, main_head)
        self.assertEqual(data["changed_files"], ["a"])
        self.assertFalse((Path(data["worktree"]) / ".git").is_dir())
        with patch.dict(os.environ, {"ESCAPE": "1"}):
            d2, data2 = self.run_task("--mode", "write")
        self.assertEqual(data2["status"], "partial")
        self.assertIn("refs", data2["isolation_breach"])
        self.assertTrue(any(u.startswith("isolation_breach") for u in data2["uncertainties"]))

    def test_write_resume_from_previous_task_branch(self):
        self.commit_fixture()
        self.worker("codex", """
sys.stdin.read()
import subprocess
Path("a").write_text(Path("a").read_text() + "x\\n")
subprocess.run(["git","-c","user.name=W","-c","user.email=w@x.invalid","commit","-qam","w"], check=True)
print(json.dumps({"type":"thread.started","thread_id":"t1"}))
Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps(""" + repr(ANSWER) + "))\n")
        first, one = self.run_task("--mode", "write")
        for base in (first.name, "delegate/" + first.name):
            d, data = self.run_task("--mode", "write", "--resume", "t1", "--base", base)
            self.assertEqual(data["base_sha"], cc.git(self.project, "rev-parse", "delegate/" + first.name).strip())
            self.assertEqual((Path(data["worktree"]) / "a").read_text(), "before\nx\nx\n")
            cmd = (d / "command.txt").read_text()
            self.assertIn("resume", cmd)
            self.assertIn('sandbox_mode="workspace-write"', cmd)
        d, data = self.run_task("--mode", "write", "--base", "no-such-ref", expected=1)
        self.assertEqual((data["status"], data["reason"]), ("blocked", "unknown_base_ref"))

    def test_reediting_dirty_file_reports_breach(self):
        self.commit_fixture()
        path = self.project / "a"
        for text, mtime_delta in (("longer", 0), ("edits", 1)):
            with self.subTest(text=text):
                path.write_text("dirty")
                stamp = path.stat().st_mtime_ns + mtime_delta
                self.worker("codex", f"""
sys.stdin.read()
path = Path({str(path)!r})
path.write_text({text!r})
os.utime(path, ns=({stamp}, {stamp}))
Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({ANSWER!r}))
""")
                _, data = self.run_task("--mode", "write")
                self.assertEqual(data["isolation_breach"], ["status"])

    def test_moving_existing_delegate_ref_reports_breach(self):
        self.commit_fixture()
        cc.git(self.project, "branch", "delegate/earlier")
        cc.git(self.project, "-c", "user.name=W", "-c", "user.email=w@example.invalid",
               "commit", "--allow-empty", "-qm", "later")
        self.worker("codex", f"""
sys.stdin.read()
import subprocess
subprocess.run(["git", "-C", {str(self.project)!r}, "branch", "-f", "delegate/earlier", "HEAD"], check=True)
Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({ANSWER!r}))
""")
        _, data = self.run_task("--mode", "write")
        self.assertEqual(data["isolation_breach"], ["refs"])

    def test_launch_failure_does_not_publish_empty_branch(self):
        self.commit_fixture()
        cc.git(self.project, "branch", "feature")
        with patch.object(cc, "supervise", side_effect=cc.WorkerLaunchError("fixture launch failure")):
            d, data = self.run_task("--mode", "write", "--onto", "feature", expected=1)
        self.assertEqual(data["status"], "failed")
        self.assertIn("onto_skipped_unsuccessful", data["uncertainties"])
        self.assertNotIn("branch", data)
        self.assertNotIn("branch", json.loads((d / "meta.json").read_text()))
        self.assertNotIn("branch=", self.receipt)
        self.assertFalse(cc.git(self.project, "show-ref", "--verify", "refs/heads/delegate/" + d.name, check=False))

    def test_uncommitted_work_keeps_diff_without_landing(self):
        self.commit_fixture()
        cc.git(self.project, "branch", "feature")
        self.worker("codex", f"""
sys.stdin.read()
Path("a").write_text("work in progress")
Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps({ANSWER!r}))
""")
        d, data = self.run_task("--mode", "write", "--onto", "feature")
        self.assertEqual(data["changed_files"], ["a"])
        self.assertIn("onto_no_commits", data["uncertainties"])
        self.assertNotIn("branch", data)
        self.assertFalse(cc.git(self.project, "show-ref", "--verify", "refs/heads/delegate/" + d.name, check=False))
        self.assertEqual(cc.git(self.project, "rev-parse", "feature").strip(), data["base_sha"])

    def test_readonly_rejects_write_refs(self):
        for option in ("base", "onto"):
            with self.subTest(option=option):
                d, data = self.run_task("--mode", "ro", "--" + option, "feature", expected=1)
                self.assertEqual((data["status"], data["reason"]), ("blocked", option + "_requires_write_mode"))
                self.assertFalse((d / "command.txt").exists())

    def test_onto_fast_forwards_only_the_named_branch(self):
        self.commit_fixture()
        cc.git(self.project, "branch", "feature")
        main = cc.git(self.project, "rev-parse", "main").strip()
        self.worker("codex", """
sys.stdin.read()
import subprocess
Path("a").write_text(Path("a").read_text() + "x\\n")
subprocess.run(["git","-c","user.name=W","-c","user.email=w@x.invalid","commit","-qam","w"], check=True)
Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps(""" + repr(ANSWER) + "))\n")
        # Branch not checked out: moved by update-ref.
        d, data = self.run_task("--mode", "write", "--onto", "feature")
        self.assertEqual(cc.git(self.project, "rev-parse", "feature").strip(), data["onto_after"])
        self.assertEqual(cc.git(self.project, "rev-parse", "main").strip(), main)
        self.assertIn("branch -f feature " + main, data["rollback"])
        # Checked out: fast-forwarded with the working tree; the next task continues from it.
        cc.git(self.project, "checkout", "-q", "feature")
        d, data = self.run_task("--mode", "write", "--onto", "feature", "--resume", "t1")
        self.assertEqual((self.project / "a").read_text(), "before\nx\nx\n")
        self.assertIn("reset --keep", data["rollback"])
        (self.project / "a").write_text("dirty\n")
        d, data = self.run_task("--mode", "write", "--onto", "feature", expected=1)
        self.assertEqual(data["reason"], "checkout_has_uncommitted_changes")
        cc.git(self.project, "checkout", "-q", "--", "a")
        for args, reason in ((["--onto", "main"], "onto_protected_branch"),
                             (["--onto", "nope"], "unknown_onto_branch"),
                             (["--onto", "feature", "--base", "main"], "onto_and_base_conflict")):
            d, data = self.run_task("--mode", "write", *args, expected=1)
            self.assertEqual(data["reason"], reason)

    def test_parallel_writers_land_without_false_breach(self):
        self.commit_fixture()
        for b in ("fast", "slow"):
            cc.git(self.project, "branch", b)
        cc.git(self.project, "checkout", "-q", "fast")  # the fast task fast-forwards the checkout
        (cc.home() / "delegate-runs").mkdir(parents=True)
        self.worker("codex", """
prompt = sys.stdin.read()
import subprocess
slow = "SLOW" in prompt
time.sleep(2.5 if slow else 0.3)
Path("a").write_text(Path("a").read_text() + ("slow\\n" if slow else "fast\\n"))
subprocess.run(["git","-c","user.name=W","-c","user.email=w@x.invalid","commit","-qam","w"], check=True)
Path(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps(""" + repr(ANSWER) + "))\n")
        script = Path(cc.__file__).resolve()
        procs = [subprocess.Popen([sys.executable, str(script), "run", "--to", "codex", "--model", "synthetic",
                                   "--cc-session", str(self.session), "--project-dir", str(self.project),
                                   "--mode", "write", "--onto", b, "--task", task],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for b, task in (("slow", "SLOW 验收"), ("fast", "验收"))]
        results = {}
        for p, b in zip(procs, ("slow", "fast")):
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, out + err)
            run_dir = Path(json.loads(out.splitlines()[-1].split("=", 1)[1]))
            results[b] = json.loads((run_dir / "result.json").read_text())
        fast, slow = results["fast"], results["slow"]
        self.assertLess(fast["finished_at"], slow["finished_at"])
        for b, data in results.items():
            self.assertEqual(data["status"], "ok", data)
            self.assertNotIn("isolation_breach", data)
            self.assertEqual(cc.git(self.project, "rev-parse", b).strip(), data["onto_after"])
        self.assertEqual((self.project / "a").read_text(), "before\nfast\n")
        self.assertEqual(cc.git(self.project, "show", "slow:a"), "before\nslow\n")
        landings = cc.landings_since(Path(cc.git(self.project, "rev-parse", "--path-format=absolute",
                                                 "--git-common-dir").strip()), 0)
        expected = []
        for b, data in results.items():
            expected.append((f"refs/heads/{b}", data["base_sha"], data["onto_after"]))
            expected.append((f"refs/heads/delegate/{data['task_id']}", None, data["onto_after"]))
        self.assertCountEqual([(e["ref"], e["old"], e["new"]) for e in landings], expected)

    def test_default_context_and_agy_print_alias(self):
        self.write_rows([{"type": "user", "message": {"content": "private fixture conversation"}}])
        d, meta = self.run_task("--backend", "print", "--dry-run", worker="agy")
        self.assertEqual(meta["context"], "none")
        self.assertNotIn("private fixture conversation", (d / "package.md").read_text())
        self.assertIn("--print-timeout", (d / "command.txt").read_text())

    def test_fallback_environment_and_default_receipt(self):
        self.worker("agy", "sys.stdin.read(); sys.exit(3)")
        self.worker("codex", 'sys.stdin.read()\nPath(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps('
                    + repr(ANSWER) + '))')
        argv = ["run", "--to", "agy", "--model", "m", "--project-dir", str(self.project),
                "--cc-session", str(self.session), "--task", "x"]
        for env, flags, worker in (("", [], "agy"), ("1", [], "codex"), ("1", ["--no-fallback"], "agy")):
            out = io.StringIO()
            with patch.dict(os.environ, CC_DELEGATE_AGY_FALLBACK=env), contextlib.redirect_stdout(out):
                self.assertEqual(cc.main(argv + flags), 0 if worker == "codex" else 2)
            d = Path(json.loads(out.getvalue().splitlines()[-1].split("=", 1)[1]))
            self.assertEqual(json.loads((d / "result.json").read_text())["worker"], worker)

    def test_missing_cli_fails_before_clone(self):
        self.commit_fixture()
        with patch.object(cc.shutil, "which", return_value=None), patch.object(cc, "seatbelt_available", return_value=True):
            for worker, backend in (("codex", "exec"), ("agy", "exec"), ("agy", "acp")):
                _, meta = self.run_task("--mode", "write", "--backend", backend, worker=worker, expected=1)
                self.assertEqual(meta["reason"], worker + " executable not found in PATH")
                self.assertFalse(cc.clone_root(self.project).exists())
            self.run_task("--backend", "daemon", "--dry-run")

    def test_onto_rechecks_worktrees_and_ancestry(self):
        self.commit_fixture()
        ancestor = cc.git(self.project, "rev-parse", "HEAD").strip()
        cc.git(self.project, "-c", "user.name=W", "-c", "user.email=w@example.invalid",
               "commit", "--allow-empty", "-qm", "later")
        old = cc.git(self.project, "rev-parse", "HEAD").strip()
        cc.git(self.project, "branch", "feature")
        meta = dict(onto="feature", onto_before=old, status="ok")
        reply = dict(uncertainties=[])
        cc.land(self.project, self.project / ".git", meta, reply, ancestor)
        self.assertEqual(reply["uncertainties"], ["onto_not_fast_forward"])
        cc.git(self.project, "worktree", "add", "-q", str(self.root / "other"), "feature")
        _, blocked = self.run_task("--mode", "write", "--onto", "feature", expected=1)
        self.assertEqual(blocked["reason"], "onto_checked_out_elsewhere")
        self.assertFalse(cc.clone_root(self.project).exists())
        cc.land(self.project, self.project / ".git", meta, reply, old)
        self.assertEqual(reply["uncertainties"][-1], "onto_checked_out_elsewhere")
        self.assertEqual(cc.git(self.project, "rev-parse", "feature").strip(), old)

    def test_agy_failure_falls_back_to_codex(self):
        self.worker("agy", "sys.stdin.read()\nsys.exit(3)\n")
        self.worker("codex", "sys.stdin.read()\nassert '--resume' not in sys.argv\n"
                    "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(" + repr(ANSWER) + "))\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cc.main(["run", "--to", "agy", "--model", "m", "--cc-session", str(self.session),
                            "--project-dir", str(self.project), "--task", "验收", "--context", "none", "--fallback"])
        out = buf.getvalue()
        self.assertEqual(code, 0, out)
        self.assertLessEqual(len(out.splitlines()), 15)
        summaries = [line for line in out.splitlines() if line.startswith("summary=")]
        self.assertIn("agy 失败（worker_failed），已自动改派 Codex", summaries[0])
        self.assertIn('fallback="agy 失败，已自动改派 Codex"', out)
        self.assertIn('fallback_reason="worker_failed"', out)
        data = json.loads((Path(json.loads(out.splitlines()[-1].split("=", 1)[1])) / "result.json").read_text())
        self.assertEqual((data["worker"], data["model"], data["status"]), ("codex", "gpt-6.2-astra", "ok"))
        self.assertIn("-agy-", data["fallback_from"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cc.main(["run", "--to", "agy", "--model", "m", "--cc-session", str(self.session),
                                      "--project-dir", str(self.project), "--task", "x", "--no-fallback"]), 2)

    def test_missing_agy_fallback_names_original_error_first(self):
        self.worker("codex", "sys.stdin.read()\n"
                    "Path(sys.argv[sys.argv.index('-o')+1]).write_text(json.dumps(" + repr(ANSWER) + "))\n")
        real_which = cc.shutil.which
        buf = io.StringIO()
        with patch.object(cc.shutil, "which", lambda name, *a, **k: None if name == "agy" else real_which(name, *a, **k)), \
                contextlib.redirect_stdout(buf):
            code = cc.main(["run", "--to", "agy", "--model", "gemini-3.8-flash-high", "--cc-session", str(self.session),
                            "--project-dir", str(self.project), "--task", "x", "--fallback"])
        out = buf.getvalue()
        self.assertEqual(code, 0, out)
        summaries = [line for line in out.splitlines() if line.startswith("summary=")]
        self.assertIn("agy executable not found in PATH", summaries[0])
        data = json.loads((Path(json.loads(out.splitlines()[-1].split("=", 1)[1])) / "result.json").read_text())
        # The agy model name must not leak into the Codex retry.
        self.assertEqual((data["worker"], data["model"]), ("codex", "gpt-6.2-astra"))

    def test_agy_stuck_only_after_silence(self):
        body = """
sys.stdin.read()
log = Path(sys.argv[sys.argv.index("--log-file")+1])
for _ in range(REQUESTS):  # a subagent's rapid requests: no step events in between
    with log.open("a") as f:
        f.write("I1122 23:34:20.896429 1 http_helpers.go] URL: https://x/v1internal:streamGenerateContent?alt=sse\\n")
    time.sleep(0.5)
if os.environ.get("HANG"):
    time.sleep(60)
print(json.dumps({"event":"result","result":{"structured_output":""" + repr(ANSWER) + """}}))
"""
        with patch.object(cc, "AGY_STALL_SECONDS", 3):
            # Many requests without a step, longer than the stall limit in total: not stuck.
            self.worker("agy", "REQUESTS = 10\n" + body)
            d, data = self.run_task(worker="agy")
            self.assertEqual(data["status"], "ok")
            # Silence past the limit: stuck.
            self.worker("agy", "REQUESTS = 1\n" + body)
            started = time.monotonic()
            with patch.dict(os.environ, {"HANG": "1"}):
                d, data = self.run_task(worker="agy", expected=2)
            self.assertLess(time.monotonic() - started, 30)
            self.assertEqual((data["reason"], data["stall_reason"]), ("stalled", "no_activity_for_stall_seconds"))

    def test_repeated_verification_is_not_a_loop(self):
        events = cc.WorkerEvents("codex")
        def cmd(i, c, out="ok"):
            return {"type": "item.completed", "item": {"id": str(i), "type": "command_execution",
                    "command": c, "exit_code": 0, "aggregated_output": out}}
        i = 0
        for n in range(6):
            for e in (cmd(i, "git diff --check"),
                      {"type": "item.completed", "item": {"id": f"f{n}", "type": "file_change"}},
                      cmd(i + 1, "python3 -m unittest", f"run {n}")):
                self.assertIsNone(events.observe(e))
            i += 2
        for n in range(4):
            self.assertIsNone(events.observe({"type": "item.completed", "item": {"id": f"e{n}", "type": "file_change"}}))
        self.assertFalse(events.loop_warning)
        # The same command with the same result three times in a row is a loop.
        for _ in range(3):
            warning = events.observe(cmd(i, "cat missing")); i += 1
        self.assertEqual(warning["type"], "loop_warning")
        agy = cc.WorkerEvents("agy")
        step = {"event": "step_update", "step_update": {"step_type": "tool", "state": "ERROR", "tool_name": "read_file",
                "tool_info": {"parameters": {"Path": "/x"}, "error": {"message": "file does not exist"}}}}
        self.assertEqual([agy.observe(step) for _ in range(3)][-1]["type"], "loop_warning")

    def test_effective_resume_context_in_run_and_mark_log(self):
        d, data = self.run_task("--context", "full", "--resume", "id", "--dry-run")
        self.assertEqual(data["context"], "none")
        # A pre-fix result already recorded the effective tier under its other name.
        cc.save_json(d / "result.json", dict(data, context="session"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cc.main(["mark", d.name, "--outcome", "edited"]), 0)
        self.assertEqual([row["context"] for row in cc.records(cc.home() / "delegate-log.jsonl")], ["none", "none"])

    def test_agy_rules_for_repo_worktree_and_nested_project(self):
        repo = self.project
        cc.git(repo, "init", "-q")
        cc.git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
               "commit", "--allow-empty", "-qm", "fixture")
        (repo / ".env").touch()
        (repo / "alias").symlink_to(repo / ".env")
        self.project = repo / "src"
        self.project.mkdir()
        self.worker("agy", 'sys.stdin.read()\nprint(json.dumps({"structured_output":' + repr(ANSWER) + '}))')
        for mode in ("ro", "write"):
            d, data = self.run_task("--mode", mode, "--backend", "exec", worker="agy")
            rules = json.loads((d / "settings.json").read_text())["permissions"]["deny"]
            for path in (repo / ".env", repo / ".git", repo / "alias"):
                self.assertIn(f"read_file({path})", rules)
            self.assertEqual(f"read_file({repo})" in rules, mode == "write")
            if mode == "write":
                self.assertIn(f"read_file({data['worktree']}/.git)", rules)
            patterns = [rule[len("command(regex:"):-1].split() for rule in rules if rule.startswith("command(regex:")]
            for cmd in ("curl x", "wget x", "/usr/bin/curl x", "/bin/nc x", "scp x", "ssh x",
                        "env wget x", "sh -c 'curl x'", "/bin/bash -c '/usr/bin/scp x'"):
                words = cc.shlex.split(cmd)
                self.assertTrue(any(len(p) <= len(words) and all(cc.re.fullmatch(r, w) for r, w in zip(p, words))
                                    for p in patterns), cmd)

    def test_redaction_false_positives(self):
        for sample in ("disk-full-read-access", "disk-usage", "--task-file", "ask-for-approval", "TASK-ID",
                       "sk-short", "akia1234567890123456", "prefixAKIA1234567890123456",
                       "AKIA12345678901234567", "def verify(token: str):", "password = None",
                       'secret = os.getenv("S")', "token: 1200", 'token = ""', "token=null",
                       "password=false", "DB_PASSWORD=\nnext line",
                       "-----BEGIN RSA PRIVATE KEY-----\nThe rest of this document is an example."):
            with self.subTest(sample=sample):
                self.assertEqual(cc.redact(sample), (sample, 0))
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    cc.receipt({"summary": [sample]})
                self.assertIn("summary=" + cc.dumps(sample), out.getvalue())

    def test_redaction_credential_formats_and_config_keys(self):
        jwt = "eyJmb28iOiJiYXIifQ.eyJpZCI6MX0.Signature"
        for sample in ["DB_PASSWORD=hunter2", "OPENAI_API_KEY=custom", "AWS_SECRET_ACCESS_KEY=opaque",
                       "GITHUB_TOKEN=fixture", "JWT_SECRET=fixture", "AUTH_TOKEN=fixture",
                       "Authorization: Bearer synthetic-value", "Bearer synthetic-value", "Authorization: Basic dGVzdA==", jwt,
                       "ASIA0123456789ABCDEF", "AIza" + "a"*35, "https://user:pass@host",
                       "postgres://user:pass@host", 'password="secret with spaces"',
                       "-----BEGIN PGP PRIVATE KEY BLOCK-----\nfixture\n-----END PGP PRIVATE KEY BLOCK-----",
                       "-----BEGIN RSA PRIVATE KEY-----\n" + "Z"*64 + "\n" + "A"*64 + "\nYWJjZA==",
                       *("gh" + prefix + "_" + "a"*20 for prefix in "pousr")]:
            with self.subTest(sample=sample):
                expected = "[REDACTED]host" if "://" in sample else "[REDACTED]"
                p, n = cc.package(sample, [], [], "none", "ro", 5000)
                self.assertTrue(p.endswith("## 当前任务\n" + expected), p)
                self.assertEqual(n, 1)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    cc.receipt({"summary": [sample]})
                self.assertIn("summary=" + cc.dumps(expected), out.getvalue())
        for header in ('{"Authorization": "Basic dGVzdA=="}', '{"Authorization": "Bearer short"}'):
            self.assertNotIn(header.split()[-1].rstrip('"}'), cc.redact(header)[0])
        self.assertEqual(cc.redact("DB_PASSWORD=sk-" + "a"*20), ("[REDACTED]", 1))
        for key in ("openai_api_key", "access_token", "db_password", "client_secret", "aws_secret_access_key"):
            blocks = [("assistant", [{"type": "tool_use", "input": {"nested": [{key: "opaque-value"}]}}])]
            p, n = cc.package("go", blocks, [], "full", "ro", 2000)
            self.assertNotIn("opaque-value", p)
            self.assertEqual(n, 1, key)

    def test_sensitive_tool_results_for_commands_and_path_arrays(self):
        for data in ({"command": "cat .env"}, {"command": "cat ~/.ssh/id_rsa"},
                     {"command": "cat 'a dir/key.pem'"}, {"paths": ["/x/a", "/x/.env"]},
                     {"nested": {"file_paths": ["a", "key.pem"]}}):
            for tier in ("full", "session"):
                blocks = [("assistant", [{"type": "tool_use", "id": "r", "name": "Bash", "input": data}]),
                          ("user", [{"type": "tool_result", "tool_use_id": "r", "content": "opaque result"}])]
                p, n = cc.package("go", blocks, [], tier, "ro", 2000)
                self.assertNotIn("opaque result", p)
                self.assertIn("[result] [REDACTED]", p)
                self.assertEqual(n, 1)

    def test_source_files_keyword_dirs_and_worker_output(self):
        project = self.project / "token-service"
        source = project / "tokens"
        source.mkdir(parents=True)
        for name in ("token.py", "password_hash.py", "secret_filter.py", "tokens.ts", "password.md",
                     "token.go", "secret.rs", "password.js", "credentials.java"):
            path = source / name
            path.write_text("source preview")
            self.assertEqual(cc.safe_file(path), path)
            self.assertIn("source preview", cc.package("go", [], [path], "session", "ro", 2000)[0])
        (source / "db_password.json").touch()
        rules = cc.agy_settings(project, "ro")["permissions"]["deny"]
        self.assertIn(f"read_file({source / 'db_password.json'})", rules)
        for path in (source, source / "password_hash.py", source / "token.py"):
            self.assertNotIn(f"read_file({path})", rules)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cc.main(["run", "--to", "agy", "--model", "fixture", "--project-dir", str(project),
                                      "--task-file", str(source / "token.py"), "--include", "tokens/password_hash.py",
                                      "--dry-run", "--cc-session", str(self.session)]), 0)
        self.worker("codex", 'sys.stdin.read()\nPath(sys.argv[sys.argv.index("-o")+1]).write_text(json.dumps('
                    + repr(ANSWER) + '))')
        with patch.dict(os.environ, {"CC_DELEGATE_HOME": str(self.root / "secret_runs")}):
            _, data = self.run_task()
            self.assertEqual(data["status"], "ok")
        self.worker("codex", 'sys.stdin.read()\nPath(sys.argv[sys.argv.index("-o")+1]).symlink_to('
                    + repr(str(source / "db_password.json")) + ')')
        _, data = self.run_task(expected=2)
        self.assertEqual(data["reason"], "unsafe_worker_output_link")

    def test_agy_terminal_events_and_errors(self):
        events = cc.WorkerEvents("agy")
        events.observe({"event": "init", "conversation_id": "id"})
        events.observe({"event": "result", "usage": {"total": 12},
                        "result": {"conversation_id": None, "structured_output": cc.dumps(ANSWER)}})
        events.observe({"event": "tool_result", "result": {"response": "wrong", "usage": {"total": 1}}})
        events.observe({"event": "result", "usage": None, "result": {}})
        self.assertEqual((events.session, events.usage, events.reply), ("id", {"total": 12}, cc.dumps(ANSWER)))
        for event in ({"event": "error", "error": "broken"},
                      {"event": "result", "status": "ERROR", "error": "broken", "result": {"response": ""}},
                      {"event": "result", "result": {"status": "ERROR", "error": "broken"}}):
            self.worker("agy", "sys.stdin.read()\nprint(json.dumps(" + repr(event) + "))")
            _, data = self.run_task(worker="agy", expected=2)
            self.assertEqual(data["reason"], "worker_failed")
            self.assertIn("worker_failed", data["uncertainties"])
            self.assertNotIn("invalid_worker_response", data["uncertainties"])

    def test_oversize_summary_preserves_status_and_report(self):
        for status, summary in (("blocked", ["长"*161]), ("failed", ["ok"]*9)):
            reply = dict(ANSWER, status=status, summary=summary, uncertainties=["original"])
            self.worker("agy", 'sys.stdin.read()\nprint(json.dumps({"response":json.dumps(' + repr(reply) + ')}))')
            d, data = self.run_task(worker="agy", expected=1)
            self.assertEqual(data["status"], status)
            self.assertEqual(data["summary"], [s[:160] for s in summary[:8]])
            self.assertEqual(data["uncertainties"], ["original", "summary_truncated"])
            self.assertEqual((d / "report.md").read_text(), ANSWER["report_markdown"])

    def test_malformed_transcript_still_packages_valid_fragments(self):
        self.write_rows([{"type": "user", "message": "wrong"},
                         {"type": "assistant", "message": {"id": {}, "content": [
                             {"type": []}, {"type": "text", "text": None}, {"type": "text", "text": "keep"},
                             {"type": "tool_use", "name": None, "id": [], "input": {"paths": [3]}}]}},
                         {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": {},
                             "content": [{"type": "text", "text": None}, {"type": "text", "text": "result"}]}]}}])
        for tier in ("scoped", "session", "full"):
            d, data = self.run_task("--context", tier, "--dry-run")
            self.assertEqual(data["status"], "dry_run")
            self.assertIn("keep", (d / "package.md").read_text())
            if tier != "scoped":
                self.assertIn("[result] result", (d / "package.md").read_text())

    def test_event_log_carriage_returns_and_corrupt_line(self):
        self.worker("agy", r'''sys.stdin.read()
sys.stdout.buffer.write(b'{"what":\r"ever"}\n')
''')
        d, data = self.run_task(worker="agy")
        self.assertEqual(data["status"], "partial")
        self.assertEqual(json.loads((d / "report.md").read_text()), {"what": "ever"})
        self.assertNotIn(b"\r", (d / "events.jsonl").read_bytes())
        (d / "events.jsonl").write_bytes(b'{"a":\r1}\ntruncated\n{"stream":"stdout","text":"ok"}\n')
        self.assertEqual(cc.raw_stdout(d), '{"a":\r1}\nok')

    def test_nonblocking_retries_and_slow_input_progress(self):
        self.worker("codex", "assert sys.stdin.read().endswith('x'*5000)\n"
                    "print(json.dumps({'type':'item.completed','item':{'type':'agent_message',"
                    "'text':json.dumps(" + repr(ANSWER) + ")}}))")
        write, read = os.write, os.read
        retried = set()

        def slow_write(fd, buf):
            if "write" not in retried:
                retried.add("write")
                raise BlockingIOError
            time.sleep(.04)
            return write(fd, buf[:512])

        def sometimes_read(fd, n):
            if n == 65536 and "read" not in retried:
                retried.add("read")
                raise BlockingIOError
            return read(fd, n)

        with patch.object(cc.os, "write", slow_write), patch.object(cc.os, "read", sometimes_read):
            _, data = self.run_task("--task", "x"*5000, "--stall-seconds", ".25")
        self.assertEqual(retried, {"read", "write"})
        self.assertEqual(data["status"], "ok")

    def test_group_cleanup_order_confirmation_and_permission_denied(self):
        proc = Mock(pid=123, returncode=None)
        calls = []

        def killpg(pid, sig):
            if proc.wait.called:
                self.assertEqual(sig, 0, "a reaped PID must never receive a destructive signal")
                calls.append("probe")
                if calls.count("probe") == 2:
                    raise ProcessLookupError
            else:
                calls.append(sig)

        with patch.object(cc, "TERM_GRACE", 0), patch.object(cc, "INT_GRACE", 0), patch.object(os, "killpg", killpg):
            self.assertTrue(cc.stop_group(proc))
        self.assertEqual(calls, [cc.signal.SIGINT, cc.signal.SIGTERM, cc.signal.SIGKILL, "probe", "probe"])
        with patch.object(os, "killpg", side_effect=PermissionError), patch.object(os, "kill"), \
                patch.object(cc, "peek_exit", return_value=0), patch.object(time, "sleep") as sleep:
            self.assertFalse(cc.stop_group(Mock(pid=123)))
        sleep.assert_not_called()

    def test_failed_leader_still_waitable_when_cleanup_starts(self):
        self.worker("codex", "sys.stdin.read()\nsys.exit(3)")
        stop = cc.stop_group
        observed = []

        def check_leader(proc):
            observed.append((proc.returncode, cc.peek_exit(proc.pid)))
            return stop(proc)

        with patch.object(cc, "stop_group", check_leader), patch.object(cc, "TERM_GRACE", .1):
            _, data = self.run_task(expected=2)
        self.assertEqual(observed, [(None, 3)])
        self.assertEqual((data["reason"], data["exit_code"]), ("worker_failed", 3))

    def test_stall_preserves_primary_reason_and_cleanup_warning(self):
        cc.git(self.project, "init", "-q")
        cc.git(self.project, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
               "commit", "--allow-empty", "-qm", "fixture")
        self.worker("codex", "sys.stdin.read()\ntime.sleep(20)")
        stop = cc.stop_group

        def unconfirmed(proc):
            stop(proc)
            return False

        with patch.object(cc, "stop_group", unconfirmed), patch.object(cc, "diff_info", side_effect=OSError("diff")):
            _, data = self.run_task("--mode", "write", "--stall-seconds", ".2", expected=2)
        self.assertEqual(data["reason"], "stalled")
        self.assertEqual(data["uncertainties"], ["stalled", "group_cleanup_unconfirmed", "diff_collection_failed", "diff"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cc.receipt(data)
        self.assertIn("group_cleanup_confirmed=false", out.getvalue())

    def test_double_interrupt_publishes_session_usage_and_partial_line(self):
        self.worker("codex", '''
import signal
def on_term(sig, frame):
    os.kill(os.getppid(), signal.SIGINT)
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"active"}), flush=True)
print(json.dumps({"type":"turn.completed","usage":{"total":7}}), end="", flush=True)
time.sleep(.2)
os.kill(os.getppid(), signal.SIGTERM)
time.sleep(20)
''')
        proc = subprocess.run([sys.executable, str(SCRIPT), "run", "--to", "codex", "--model", "fixture",
                               "--task", "interrupt", "--project-dir", str(self.project), "--cc-session",
                               str(self.session)], capture_output=True, text=True, timeout=18)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        d = Path(json.loads(proc.stdout.splitlines()[-1].split("=", 1)[1]))
        result = json.loads((d / "result.json").read_text())
        self.assertEqual(result, json.loads((d / "meta.json").read_text()))
        self.assertEqual(result["reason"], "interrupted")
        self.assertEqual(result["worker_session_id"], "active")
        self.assertEqual(result["worker_usage"], {"total": 7})
        self.assertIsInstance(result["group_cleanup_confirmed"], bool)
        self.assertEqual(list(cc.records(d / "events.jsonl"))[-1]["type"], "turn.completed")
        self.assertEqual(list(cc.records(cc.home() / "delegate-log.jsonl"))[-1]["status"], "outcome_unknown")


class PythonVersionCase(unittest.TestCase):
    def test_old_python_gets_a_clear_message(self):
        script = Path(cc.__file__)
        probe = ("import runpy, sys\nsys.version_info = (3, 9, 6)\n"
                 f"runpy.run_path({str(script)!r}, run_name='__main__')\n")
        proc = subprocess.run([sys.executable, "-c", probe, "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("需要 Python 3.10 或更高，当前是 3.9", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(proc.stdout, "")
