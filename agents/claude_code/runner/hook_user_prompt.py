#!/usr/bin/env python3
"""UserPromptSubmit hook: register a session at its first message, and mirror
a locally typed message right away.

1. First message of a session the owner started in Claude Code (and the first
   after resuming it): ask the machine's tracking bridge to register it
   (session_start, Society AI design session-trackers D37). The bridge answers
   whether it is mirrored and tracked, with its chat link, or why not. The
   hook prints that as the terminal banner (`systemMessage`) and asks the
   agent to add the same line to the Society AI blockquote at the top of its
   reply (the desktop app renders no hook banner, D43). Once per session per
   Claude Code process; sessions the bridge started and the Tracker's own runs
   skip it.

2. Mirror notify: tells the bridges "a prompt just landed in this session"
   the moment it is submitted, and the bridge ships the transcript delta on a
   short delay (the prompt entry is flushed to the transcript as the turn
   starts, not synchronously with this hook). The transcript shipper
   otherwise runs only on Stop, at the END of a turn. Sessions a bridge does
   not ship are ignored bridge-side, so this is safe to run globally.

Must stay FAST: UserPromptSubmit blocks the turn from starting until every
hook returns. Short-timeout IPC only, no waiting on a ship.
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

from agents.claude_code.runner.hook_stop import _bridge_sockets, _notify_mirrors, _read_hook_input

START_TIMEOUT_S = 3.5
STARTED_DIR = _pathlib.Path(os.path.expanduser("~")) / ".cache" / "society-ai" / "tracker" / "started"
STARTED_KEEP_S = 7 * 24 * 3600


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _marker(session_id: str) -> _pathlib.Path:
    # One per Claude Code process: a resumed session asks again (it may have
    # been stopped or deleted meanwhile) and shows its line again.
    return STARTED_DIR / f"{session_id}-{os.getppid()}"


def _session_start(hook_input: dict[str, Any]) -> Optional[dict]:
    """The tracking bridge's answer, or None when none answered."""
    frame = (json.dumps({
        "id": f"hook-start-{os.getpid()}",
        "method": "session_start",
        "params": {
            "session_id": str(hook_input.get("session_id")),
            "cwd": hook_input.get("cwd"),
            "transcript_path": hook_input.get("transcript_path"),
            "prompt": str(hook_input.get("prompt") or "")[:500],
        },
    }) + "\n").encode("utf-8")
    deadline = time.monotonic() + START_TIMEOUT_S
    for sock_path in _bridge_sockets():
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(left)
                s.connect(sock_path)
                s.sendall(frame)
                data = b""
                while not data.endswith(b"\n"):
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            result = json.loads(data or b"{}").get("result")
        except (OSError, ValueError):
            continue
        if isinstance(result, dict):
            return result
    return None


def banner_line(result: dict, md: bool) -> Optional[str]:
    """The line for the banner (D43), or None (tracking off: nothing to say)."""
    if result.get("mirrored") and result.get("chat_url"):
        if md:
            return f"🪞 Mirrored and tracked on Society AI · [Open this session]({result['chat_url']})"
        return f"🪞 Mirrored and tracked on Society AI · {result['chat_url']}"
    reason = result.get("reason")
    if reason == "folder_skipped" and result.get("folder"):
        return f"🔒 Not mirrored: {result['folder']} is skipped in Settings"
    if reason == "stopped":
        return "🔒 Not mirrored: you stopped mirroring this session"
    return None


def hook_output(result: dict) -> Optional[dict]:
    line = banner_line(result, md=True)
    if not line:
        return None
    context = [
        "[Society AI session mirror]",
        "Add this line, verbatim, as the last line of the Society AI blockquote at the top of your reply. "
        "If you are not showing that blockquote in this reply, start your reply with just this line as a "
        "blockquote, then a blank line:",
        f"> {line}",
        "Only in this reply.",
    ]
    if result.get("mirrored"):
        context.append(
            "This session's conversation (the user's messages and your replies, never tool output or files) "
            "is saved to Society AI and tracked. If the user asks not to save, mirror or track this session, "
            "call the society-ai tool stop_session_mirror."
        )
    return {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(context)},
        "systemMessage": banner_line(result, md=False),
    }


def _forget_old_markers() -> None:
    try:
        cutoff = time.time() - STARTED_KEEP_S
        for p in STARTED_DIR.iterdir():
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
    except OSError:
        pass


def main() -> int:
    hook_input = _read_hook_input()
    session_id = hook_input.get("session_id")
    output = None
    if session_id and not _flag("SOCIETY_AI_TRACKER") and not _flag("SOCIETY_AI_DISPATCHED"):
        marker = _marker(str(session_id))
        if not marker.exists():
            result = _session_start(hook_input)
            if result is not None:
                try:
                    STARTED_DIR.mkdir(parents=True, exist_ok=True)
                    marker.touch()
                    if len(os.listdir(STARTED_DIR)) > 200:
                        _forget_old_markers()
                except OSError:
                    pass
                output = hook_output(result)
    _notify_mirrors(hook_input, event="prompt")
    if output:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
