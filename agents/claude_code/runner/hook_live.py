#!/usr/bin/env python3
"""PreToolUse and Notification hook: whether a session is working or waits
for its owner, for the Sessions live view (Society AI design:
docs/design/session-trackers.md §19).

- PreToolUse: tells the machine's tracking bridge that a tool started in this
  session, and nothing else: no tool name, no input, no command, path, URL or
  pattern. Claude Code's tool activity never leaves the computer.
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
import socket
import sys
import time
from typing import Any, Optional

from agents.claude_code.runner.hook_stop import _bridge_sockets, _read_hook_input

IPC_TIMEOUT_S = 0.5
IPC_DEADLINE_S = 1.0


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
        # Only that a tool started: nothing about the tool is passed on.
        return {**base, "event": "tool"}
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
