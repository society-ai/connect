"""Live state of the sessions on this machine, for the Sessions live view
(Society AI design: docs/design/session-trackers.md §19, D44 to D52).

The hooks report what a session does (hook_live.py for tool calls and
permission prompts, the prompt and Stop notifies for turns); the tracking
runner keeps it here and sends what changed in batches (session_tracker.py).
Pure state, no I/O: every change bumps a revision, and a session is sent
again only when its revision moved past the one last sent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

MAX_TEXT = 200          # characters per action line
RECENT = 5              # actions kept, newest first
HEARTBEAT_S = 300.0     # a running session is sent again after this, so it never reads as lost contact
FORGET_S = 3600.0       # a finished session is forgotten after this
MAX_SESSIONS = 500


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _clip(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1].rstrip() + "…"


@dataclass
class Live:
    running: bool = False
    turn_started_at: Optional[str] = None
    action: Optional[str] = None
    recent: list[dict] = field(default_factory=list)
    waiting_permission: bool = False
    rev: int = 0
    sent_rev: int = 0
    sent_at: float = 0.0        # monotonic: last successful send (or heartbeat check)
    changed_at: float = 0.0     # monotonic


class LiveBoard:
    def __init__(self, clock: Callable[[], float] = time.monotonic, now: Callable[[], str] = _now):
        self._clock = clock
        self._now = now
        self.sessions: dict[str, Live] = {}

    def _get(self, sid: str) -> Live:
        live = self.sessions.get(sid)
        if live is None:
            live = self.sessions[sid] = Live()
        return live

    def _changed(self, live: Live) -> None:
        live.rev += 1
        live.changed_at = self._clock()

    # -- events --

    def prompt(self, sid: str) -> None:
        """A turn starts (UserPromptSubmit): running, the clock starts, a clean slate."""
        live = self._get(sid)
        live.running = True
        live.turn_started_at = self._now()
        live.action = None
        live.recent = []
        live.waiting_permission = False
        self._changed(live)

    def tool(self, sid: str, text: str) -> None:
        """A tool call (PreToolUse): the current action, first in the recent ones."""
        live = self._get(sid)
        text = _clip(text) or "Using a tool"
        if not live.running:
            # A turn the prompt hook did not see (a message from the bridge's channel).
            live.running = True
            live.turn_started_at = live.turn_started_at or self._now()
        live.action = text
        live.recent = ([{"at": self._now(), "text": text}] + live.recent)[:RECENT]
        live.waiting_permission = False
        self._changed(live)

    def permission(self, sid: str) -> None:
        """Claude Code waits for a permission (Notification)."""
        live = self._get(sid)
        if live.waiting_permission:
            return
        live.waiting_permission = True
        self._changed(live)

    def stop(self, sid: str) -> None:
        """The turn ended (Stop): not running, no current action. The recent
        actions stay, so an idle session still shows what it did last."""
        live = self._get(sid)
        if not live.running and live.action is None and not live.waiting_permission and live.rev:
            return
        live.running = False
        live.turn_started_at = None
        live.action = None
        live.waiting_permission = False
        self._changed(live)

    # -- sending --

    def dirty(self) -> list[str]:
        return [sid for sid, live in self.sessions.items() if live.rev != live.sent_rev]

    def heartbeat_due(self) -> list[str]:
        """Running sessions whose state has not been sent for HEARTBEAT_S."""
        now = self._clock()
        return [sid for sid, live in self.sessions.items()
                if live.running and live.rev == live.sent_rev and now - live.sent_at >= HEARTBEAT_S]

    def beat(self, sid: str, alive: bool) -> None:
        """After a heartbeat check: send a still running session again; otherwise
        leave it (the app shows lost contact) and check again later."""
        live = self.sessions.get(sid)
        if live is None:
            return
        if alive:
            live.rev += 1
        else:
            live.sent_at = self._clock()

    def payload(self, sid: str) -> tuple[int, dict]:
        live = self.sessions[sid]
        return live.rev, {
            "external_session_id": sid,
            "running": live.running,
            "turn_started_at": live.turn_started_at,
            "action": live.action,
            "recent": [dict(r) for r in live.recent],
            "waiting_permission": live.waiting_permission,
        }

    def sent(self, sid: str, rev: int) -> None:
        """`rev` reached the platform; a later change stays to be sent."""
        live = self.sessions.get(sid)
        if live is None:
            return
        live.sent_rev = max(live.sent_rev, rev)
        live.sent_at = self._clock()

    def prune(self) -> None:
        now = self._clock()
        # A finished session after an hour (sent, or never tracked), and one
        # whose turn end never came after a day.
        for sid in [s for s, live in self.sessions.items()
                    if now - live.changed_at > (FORGET_S * 24 if live.running else FORGET_S)]:
            del self.sessions[sid]
        if len(self.sessions) > MAX_SESSIONS:
            keep = sorted(self.sessions.items(), key=lambda kv: kv[1].changed_at)[-MAX_SESSIONS:]
            self.sessions = dict(keep)
