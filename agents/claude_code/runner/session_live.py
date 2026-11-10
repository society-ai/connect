"""Live state of the sessions on this machine, for the Sessions live view
(Society AI design: docs/design/session-trackers.md §19, D44 to D52).

The hooks report whether a session works or waits (hook_live.py for tool
starts and permission prompts, the prompt and Stop notifies for turns); the
tracking runner keeps it here and sends what changed in batches
(session_tracker.py). Only running, the turn's start and the permission wait
are kept: nothing about a tool call, so nothing about one can be sent.
Pure state, no I/O: every change bumps a revision, and a session is sent
again only when its revision moved past the one last sent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

HEARTBEAT_S = 300.0     # a running session is sent again after this, so it never reads as lost contact
FORGET_S = 3600.0       # a finished session is forgotten after this
MAX_SESSIONS = 500


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Live:
    running: bool = False
    turn_started_at: Optional[str] = None
    waiting_permission: bool = False
    rev: int = 0
    sent_rev: int = 0
    sent_at: float = 0.0        # monotonic: last successful send (or heartbeat check)
    seen_at: float = 0.0        # monotonic: last event of any kind (a tool start too)


class LiveBoard:
    def __init__(self, clock: Callable[[], float] = time.monotonic, now: Callable[[], str] = _now):
        self._clock = clock
        self._now = now
        self.sessions: dict[str, Live] = {}

    def _get(self, sid: str) -> Live:
        live = self.sessions.get(sid)
        if live is None:
            live = self.sessions[sid] = Live()
        live.seen_at = self._clock()
        return live

    def _changed(self, live: Live) -> None:
        live.rev += 1

    # -- events --

    def prompt(self, sid: str) -> None:
        """A turn starts (UserPromptSubmit): running, the clock starts, no wait."""
        live = self._get(sid)
        live.running = True
        live.turn_started_at = self._now()
        live.waiting_permission = False
        self._changed(live)

    def tool(self, sid: str) -> None:
        """A tool started (PreToolUse): Claude Code is working. Nothing about
        the tool is known here. It clears a permission wait (the owner
        answered) and starts a turn the prompt hook did not see (a message
        from the bridge's channel). A changed session only when one of those
        changed, so a busy session is not sent on every tool call; the
        heartbeat keeps a long turn from reading as lost contact."""
        live = self._get(sid)
        changed = False
        if not live.running:
            live.running = True
            changed = True
        if live.turn_started_at is None:
            live.turn_started_at = self._now()
            changed = True
        if live.waiting_permission:
            live.waiting_permission = False
            changed = True
        if changed:
            self._changed(live)

    def permission(self, sid: str) -> None:
        """Claude Code waits for a permission (Notification)."""
        live = self._get(sid)
        if live.waiting_permission:
            return
        live.waiting_permission = True
        self._changed(live)

    def stop(self, sid: str) -> None:
        """The turn ended (Stop): idle, no clock, no wait."""
        live = self._get(sid)
        if not live.running and not live.waiting_permission and live.rev:
            return
        live.running = False
        live.turn_started_at = None
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
        """What is sent for a session: these four fields and nothing else."""
        live = self.sessions[sid]
        return live.rev, {
            "external_session_id": sid,
            "running": live.running,
            "turn_started_at": live.turn_started_at,
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
        # whose turn end never came after a day without any event.
        for sid in [s for s, live in self.sessions.items()
                    if now - live.seen_at > (FORGET_S * 24 if live.running else FORGET_S)]:
            del self.sessions[sid]
        if len(self.sessions) > MAX_SESSIONS:
            keep = sorted(self.sessions.items(), key=lambda kv: kv[1].seen_at)[-MAX_SESSIONS:]
            self.sessions = dict(keep)
