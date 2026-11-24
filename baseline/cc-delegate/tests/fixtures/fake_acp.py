"""Offline stdio agent installed in the test's private bin directory."""
import json
import os
from pathlib import Path
import sys


def send(**message):
    print(json.dumps(dict(jsonrpc="2.0", **message)), flush=True)


def update(update_type, **fields):
    send(method="session/update", params=dict(sessionId=session, update=dict(sessionUpdate=update_type, **fields)))


def receive():
    msg = json.loads(input())
    assert msg["jsonrpc"] == "2.0"
    with Path(os.environ["ACP_TRACE"]).open("a") as log:
        print(json.dumps(msg), file=log)
    return msg


def choices(key, values):
    return dict(id=key, type="select", options=[dict(value=v, name=v) for v in values])


assert sys.argv[1:] == ["acp"]
options = [choices("model", ["synthetic", "synthetic-high"]), choices("thought_level", ["low", "high"]),
           choices("mode", ["plan", "accept-edits"])]
session = "fixture-new"
while True:
    req = receive()
    method, params = req["method"], req["params"]
    if method == "initialize":
        assert params["protocolVersion"] == 1
        assert params["clientCapabilities"] == dict(fs=dict(readTextFile=False, writeTextFile=False), terminal=False)
        result = dict(protocolVersion=1)
    elif method in ("session/new", "session/resume", "session/load"):
        assert params["cwd"] == os.getcwd() and params["mcpServers"] == []
        if os.getenv("ACP_REQUIRE_MARKER") and not (Path.home() / ".gemini" / "fixture-marker").is_file():
            send(id=req["id"], error=dict(code=-32000, message="Authentication required"))
            continue
        if method != "session/new":
            session = params["sessionId"]
        if method == "session/resume" and os.getenv("ACP_LOAD"):
            send(id=req["id"], error=dict(code=-32601, message="unsupported"))
            continue
        if method == "session/load":
            update("agent_message_chunk", content=dict(type="text", text="OLD"))
            for i in range(4):
                update("tool_call", toolCallId=str(i), title="old command", kind="execute", status="completed")
        result = dict(configOptions=options)
        if method == "session/new":
            result["sessionId"] = session
    elif method == "session/set_config_option":
        assert params["sessionId"] == session
        result = dict(configOptions=options)
    elif method == "session/prompt":
        assert params["sessionId"] == session
        update("tool_call", toolCallId="cmd", title="Shell", kind="execute", status="pending",
               rawInput={"CommandLine": "git status"})
        send(id=1, method="session/request_permission", params=dict(
            sessionId=session, toolCall=dict(toolCallId="cmd"),
            options=[dict(kind="allow_once", optionId="yes"), dict(kind="reject_once", optionId="no")]))
        response = receive()
        assert response["id"] == 1
        decision = response["result"]["outcome"]
        assert decision["outcome"] == "selected"
        allowed = decision["optionId"] == "yes"
        update("tool_call_update", toolCallId="cmd", status="completed" if allowed else "failed",
               rawOutput=dict(exitCode=0 if allowed else 1, output="fixture"))
        answer = json.dumps(dict(status="ok", summary=["done"], uncertainties=[], report_markdown="fixture reply"))
        answer = os.getenv("ACP_TEXT", answer)
        for chunk in (answer[:12], answer[12:]):
            update("agent_message_chunk", content=dict(type="text", text=chunk))
        reason = os.getenv("ACP_STOP", "end_turn")
        if os.getenv("ACP_WAIT"):
            Path(os.environ["ACP_READY"]).touch()
            cancel = receive()
            assert cancel == dict(jsonrpc="2.0", method="session/cancel", params=dict(sessionId=session))
            if os.environ["ACP_WAIT"] == "ignore":
                import time
                time.sleep(60)
            reason = "cancelled"
        result = dict(stopReason=reason, usage=dict(inputTokens=10, outputTokens=6, cachedReadTokens=2,
                                                    thoughtTokens=3, totalTokens=int(os.getenv("ACP_TOTAL_TOKENS", "16"))))
    else:
        raise AssertionError(method)
    send(id=req["id"], result=result)
    if method == "session/prompt":
        break
