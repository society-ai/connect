#!/usr/bin/env python3
"""PreToolUse and Notification hook: what a session does right now, for the
Sessions live view (Society AI design: docs/design/session-trackers.md §19).

- PreToolUse: turns the tool call into one action line ("Running pytest",
  "Editing lib/x.ts", ...) and hands it to the machine's tracking bridge.
- Notification: when Claude Code says it needs a permission, tells the bridge
  the session waits for its owner. The next tool call or the turn's end
  clears it.

The bridge batches these and sends what changed at most every 5 seconds. The
hook itself only talks to the local bridges over their IPC sockets and returns
at once: PreToolUse blocks the tool until it returns. It never prints, so it
never changes what the tool may do.
"""

from __future__ import annotations

import pathlib as _pathlib
import sys as _sys

# Started by path (Claude Code runs this file): make the repo root importable.
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[3]))

import json
import os
import re
import socket
import sys
import time
from typing import Any, Optional

from agents.claude_code.runner.hook_stop import _bridge_sockets, _read_hook_input

MAX_CHARS = 200
IPC_TIMEOUT_S = 0.5
IPC_DEADLINE_S = 1.0

EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
CHECKLIST_TOOLS = {"TodoWrite", "TaskCreate", "TaskUpdate"}

# Credentials that show up in commands and URLs are masked before the line
# leaves the session: the line is shown on the owner's Sessions page. Paths
# and patterns are shown as they are.
_SECRETS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    (re.compile(r"\b(sai|sk|pk|rk|ghp|gho|ghs|ghu|github_pat|xox[abprs]|glpat)([-_])[A-Za-z0-9_-]{16,}"), r"\1\2***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|APIKEY|ACCESS_KEY)[A-Z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)"),
     r"\1=***"),
    (re.compile(r"(?i)([?&](?:token|key|secret|password|access_token|api_key|sig)=)[^&\s]+"), r"\1***"),
    (re.compile(r"(?i)(://[^/\s:@]+:)[^@\s/]+@"), r"\1***@"),
]


def _mask(text: str) -> str:
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


def clip(text: str, limit: int = MAX_CHARS) -> str:
    """One line, at most `limit` characters."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _rel(path: Any, cwd: Optional[str]) -> str:
    """`path` relative to the session's folder when it is inside it."""
    p = str(path or "").strip()
    if not p:
        return ""
    if cwd and os.path.isabs(p):
        base = cwd.rstrip("/") or "/"
        if p == base:
            return "."
        if p.startswith(base + "/"):
            return p[len(base) + 1:]
    return p


def action_line(tool_name: Any, tool_input: Any, cwd: Optional[str] = None) -> str:
    """The action line for one tool call (§19.3)."""
    name = str(tool_name or "").strip() or "a tool"
    inp = tool_input if isinstance(tool_input, dict) else {}

    def arg(*keys: str) -> str:
        for k in keys:
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    if name == "Bash":
        cmd = arg("command")
        line = f"Running {_mask(cmd)}" if cmd else "Running a command"
    elif name in EDIT_TOOLS:
        path = _rel(arg("file_path", "notebook_path", "path"), cwd)
        line = f"Editing {path}" if path else "Editing a file"
    elif name == "Read":
        path = _rel(arg("file_path", "path"), cwd)
        line = f"Reading {path}" if path else "Reading a file"
    elif name in ("Grep", "Glob"):
        pattern = arg("pattern")
        line = f"Searching {pattern}" if pattern else "Searching"
    elif name == "WebFetch":
        url = arg("url")
        line = f"Fetching {_mask(url)}" if url else "Fetching a page"
    elif name == "WebSearch":
        query = arg("query")
        line = f"Searching the web: {query}" if query else "Searching the web"
    elif name in ("Task", "Agent"):
        desc = arg("description", "prompt")
        line = f"Asking a subagent: {desc}" if desc else "Asking a subagent"
    elif name in CHECKLIST_TOOLS:
        line = "Updating the checklist"
    else:
        line = f"Using {name}"
    return clip(line)


def is_permission_prompt(hook_input: dict[str, Any]) -> bool:
    """A Notification that says Claude Code waits for a permission."""
    kind = str(hook_input.get("notification_type") or "").lower()
    if kind:
        return "permission" in kind
    return "permission" in str(hook_input.get("message") or "").lower()


def live_params(hook_input: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The IPC params for this hook event, or None when there is nothing to say."""
    session_id = hook_input.get("session_id")
    if not session_id:
        return None
    event = hook_input.get("hook_event_name")
    base = {"session_id": str(session_id), "cwd": hook_input.get("cwd")}
    if event == "PreToolUse":
        return {**base, "event": "tool",
                "action": action_line(hook_input.get("tool_name"), hook_input.get("tool_input"),
                                      hook_input.get("cwd"))}
    if event == "Notification" and is_permission_prompt(hook_input):
        return {**base, "event": "permission"}
    return None


def send(params: dict[str, Any]) -> None:
    """Hand the event to the bridges; stop at the tracking bridge's answer."""
    frame = (json.dumps({"id": f"hook-live-{os.getpid()}", "method": "session_live",
                         "params": params}) + "\n").encode("utf-8")
    deadline = time.monotonic() + IPC_DEADLINE_S
    for sock_path in _bridge_sockets():
        left = min(IPC_TIMEOUT_S, deadline - time.monotonic())
        if left <= 0:
            return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(left)
                s.connect(sock_path)
                s.sendall(frame)
                data = s.recv(4096)
            if json.loads(data or b"{}").get("result"):
                return  # the machine's tracking bridge took it
        except (OSError, ValueError):
            continue


def main() -> int:
    # The Tracker agent's own model runs are never tracked (no loop).
    if os.environ.get("SOCIETY_AI_TRACKER") == "1":
        return 0
    params = live_params(_read_hook_input())
    if params:
        send(params)
    return 0


if __name__ == "__main__":
    sys.exit(main())
