"""Synthetic claude -p worker. The caller supplies only fixture environment variables."""
import json
import os
from pathlib import Path
import signal
import sys
import time


def emit(kind, **fields):
    print(json.dumps(dict(type=kind, **fields)), flush=True)


trace = Path(os.environ["FIXTURE_TRACE"])
trace.write_text(json.dumps(dict(argv=sys.argv[1:], stdin=sys.stdin.read(), env=dict(os.environ))))
scenario = os.environ.get("FIXTURE_SCENARIO", "success")
session = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else "fixture-new"
emit("system", subtype="init", session_id=session)
emit("system", subtype="permission_denied", tool_name="Write")
for n in range(3 if scenario == "loop" else 1):
    emit("assistant", message={"content": [
        {"type": "text", "text": "checking"},
        {"type": "tool_use", "id": str(n), "name": "Bash", "input": {"command": "git status"}}]})
    emit("user", message={"content": [
        {"type": "tool_result", "tool_use_id": str(n), "is_error": True,
         "content": [{"type": "text", "text": "synthetic error"}]}]})
if scenario == "wait":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    trace.with_suffix(".ready").touch()
    time.sleep(30)
reply = dict(status="ok", summary=["finished"], uncertainties=[], report_markdown="fixture report")
emit("result", subtype="max_turns" if scenario == "max_turns" else "success",
     is_error=scenario == "error", session_id=session,
     result="401 OAuth token expired, api_key=fake-private-value" if scenario == "error" else json.dumps(reply),
     **({} if scenario == "text" else {"structured_output": dict(reply, report_markdown="structured")}),
     usage=dict(input_tokens=5, output_tokens=7, cache_creation_input_tokens=11, cache_read_input_tokens=13),
     modelUsage={"fixture-model": {}, "fixture-secondary": {}}, total_cost_usd=0.25,
     permission_denials=[{"tool_name": "Write"}])
