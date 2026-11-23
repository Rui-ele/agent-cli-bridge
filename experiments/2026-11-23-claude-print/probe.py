"""Probe `claude -p` as a delegation worker: stream-json events, --json-schema,
permission modes, and running under a Seatbelt write sandbox.

Usage: python3 probe.py [model]   (default model: haiku, to keep the cost low)
Prints event shapes and outcomes; writes nothing outside a temp dir.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

MODEL = sys.argv[1] if len(sys.argv) > 1 else "haiku"
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}, "wrote": {"type": "boolean"}},
          "required": ["summary", "wrote"], "additionalProperties": False}


def q(p):
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


# Claude Code's own state dirs under ~/.claude. The rest of ~/.claude (settings, skills, hooks,
# tools, other runs) stays read-only so a worker cannot rewrite the host's configuration.
CLAUDE_STATE = ["projects", "sessions", "session-env", "shell-snapshots", "state", "cache", "file-history",
                "todos", "plans", "debug", "statsig", "telemetry", "backups", "paste-cache", "ide"]


def profile(writable):
    home = Path(os.path.realpath(Path.home()))
    allow = [*(home / ".claude" / d for d in CLAUDE_STATE), f"/private/tmp/claude-{os.getuid()}", "/dev",
             "/private/var/folders", os.path.realpath(tempfile.gettempdir()), *writable]
    rules = " ".join(f"(subpath {q(os.path.realpath(p))})" for p in allow)
    # ~/.claude.json (+ lock/backup siblings) and the Bash tool's /tmp/claude-<hex>-cwd marker files.
    rules += f' (regex #"^{home}/\\.claude\\.json") (regex #"^/private/tmp/claude-[0-9a-f]+-cwd$")'
    return f"(version 1)(allow default)(deny file-write*)(allow file-write* {rules})"


def run(label, cwd, mode, task, extra=(), sandbox_writable=None, resume=None):
    cmd = ["claude", "-p", "--model", MODEL, "--output-format", "stream-json", "--verbose",
           "--json-schema", json.dumps(SCHEMA), "--permission-mode", mode, "--permission-prompts", "none",
           "--settings", json.dumps({"sandbox": {"enabled": False}}), "--debug-file", str(Path(cwd).parent / f"{label}.debug"), *extra]
    if resume:
        cmd += ["--resume", resume]
    if sandbox_writable is not None:
        prof = Path(cwd).parent / f"{label}.sb"
        prof.write_text(profile(sandbox_writable))
        cmd = ["/usr/bin/sandbox-exec", "-f", str(prof), *cmd]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE_") and k != "CLAUDECODE"}
    p = subprocess.run(cmd, input=task, cwd=cwd, capture_output=True, text=True, env=env, timeout=300)
    events = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
    kinds = {}
    for e in events:
        k = e.get("type") + ("/" + e["subtype"] if e.get("subtype") else "")
        kinds[k] = kinds.get(k, 0) + 1
    tools = [(c.get("name"), json.dumps(c.get("input"))[:80]) for e in events if e.get("type") == "assistant"
             for c in e["message"].get("content", []) if c.get("type") == "tool_use"]
    denials = [c.get("content") if isinstance(c.get("content"), str) else json.dumps(c.get("content"))[:120]
               for e in events if e.get("type") == "user" for c in e["message"].get("content", [])
               if isinstance(c, dict) and c.get("is_error")]
    res = next((e for e in events if e.get("type") == "result"), {})
    print(f"== {label}: rc={p.returncode} kinds={kinds}")
    print("   tools:", tools)
    print("   errors:", [d[:120] for d in denials])
    print("   result keys:", sorted(res))
    print("   structured_output:", res.get("structured_output"), "| subtype:", res.get("subtype"),
          "| permission_denials:", len(res.get("permission_denials") or []), "| models:", sorted(res.get("modelUsage") or {}),
          "| duration_ms:", res.get("duration_ms"))
    print("   usage:", {k: v for k, v in (res.get("usage") or {}).items() if isinstance(v, int)},
          "| cost:", res.get("total_cost_usd"), "| session:", bool(res.get("session_id")))
    dbg = Path(cwd).parent / f"{label}.debug"
    eperm = sorted({l.split("EPERM")[1][:140] for l in dbg.read_text(errors="replace").splitlines() if "EPERM" in l}) if dbg.exists() else []
    print("   EPERM in debug log:", eperm[:6])
    if p.returncode and not events:
        print("   stderr:", p.stderr[-400:])
    return res


# The test tree must sit outside every writable path above, or the escape check proves nothing.
with tempfile.TemporaryDirectory(dir=Path.home(), prefix=".cc-delegate-probe-") as t:
    root = Path(os.path.realpath(t))
    repo = root / "repo"
    repo.mkdir()
    (repo / "a.txt").write_text("alpha line\n")
    subprocess.run(["git", "init", "-q", str(repo)])
    run("ro-plain", repo, "dontAsk", "Read a.txt and put its first line in summary. wrote=false.")
    run("ro-write-attempt", repo, "dontAsk",
        "Create ro.txt containing x (try both the Write tool and the shell command `touch ro2.txt`); "
        "report whether it worked.", extra=["--allowedTools", "Read", "Grep", "Glob", "Bash"],
        sandbox_writable=[])
    print("   ro.txt:", (repo / "ro.txt").exists(), "ro2.txt:", (repo / "ro2.txt").exists())
    w = run("write-sandboxed", repo, "bypassPermissions",
            f"1) create w.txt containing hi; 2) run `git add w.txt` then "
            f"`git -c user.name=w -c user.email=w@x.invalid commit -qm add`; 3) run `touch {root / 'escape.txt'}`; 4) run `touch ~/.claude/probe-escape.txt`. "
            "Report each result.", sandbox_writable=[repo])
    print("   w.txt:", (repo / "w.txt").exists(), "commit:",
          subprocess.run(["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True).stdout.strip(),
          "escape:", (root / "escape.txt").exists(),
          "~/.claude escape:", (Path.home() / ".claude/probe-escape.txt").exists())
    if w.get("session_id"):
        r = run("resume", repo, "dontAsk", "Without running tools: what file did you create last turn? summary only.",
                sandbox_writable=[], resume=w["session_id"])
        print("   same session:", r.get("session_id") == w["session_id"])
