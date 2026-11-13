"""TranscriptShipper — mirror Claude Code session transcripts to Society AI.

Phase 2 of the recorded-workspace model: every session the bridge launches
(SESSION_MODE) gets its local JSONL transcript shipped to the platform in
per-turn delta batches, POST /api/v1/session-mirror (sai_ token auth — the
bridge never holds a service key). The platform upserts the session row and
appends entries idempotently on (session, seq), so re-shipping after a crash
or retry is a no-op.

Scope: only sessions the bridge *registered* are shipped. Stop-hook
notifications for unknown session ids are ignored. A session the owner
started in Claude Code is registered (work_item_kind 'direct') only when the
platform said it is mirrored and tracked (session-trackers D36, D37): the
owner tracks sessions started in Claude Code and its folder is not skipped.
It then ships from the start of its transcript. A batch refused because the
owner stopped mirroring it (409, D41) makes the shipper forget the session.
MIRROR=false disables shipping entirely.

Turns the owner sent from the app into such a session (the Haiku relay, or one
resumed turn) are already in the chat; their user entry ships marked as a chat
echo so the chat does not show them twice.

WHAT ships is a security boundary: the conversation only, meaning
user/dispatch text, assistant text and summaries, plus the instructions of a
skill the agent loads (its SKILL.md text, which Claude Code adds to the
conversation for the model): those ship as a `skill` entry, shown in the chat
as the agent's, never as the owner's message. A message from another Claude
session on the machine ships as a `peer` entry with that session's name.
Society AI's own relay and channel events carry the owner's app messages and
ship as before; Claude Code's other notes to the model stay local. The Society AI banner the
agent puts at the top of a reply is for the Claude Code app and is cut from
the text that ships. Claude Code's own tool
activity (Bash, Read, Edit, Grep, WebFetch, ...: names, targets, inputs,
results), thinking, attachments and internal records NEVER leave the
machine; that's where secrets live (env output, file contents, tokens in
command lines). Society AI tool calls are recorded by the platform itself
when the society-ai MCP server makes them.

Offsets (byte position + next seq per session) persist across bridge
restarts in mirror_state.json next to the bridge socket, keeping seq
numbers stable so the server-side dedup works. The record→entry transform
is deterministic, so re-reading the same byte range regenerates identical
(seq, entry) pairs and re-ships stay idempotent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import socket
import time
from itertools import islice
from typing import Any, Optional

import httpx

from core.chat_context import chat_headers

logger = logging.getLogger("transcript_shipper")

# Server-side caps (mirror agent_router's SessionMirrorBatch limits).
MAX_ENTRIES_PER_BATCH = 500

# Trimming: transcripts can embed file dumps / base64 blobs. We keep the
# record's shape but cap every string and collection so one entry stays
# well under the server's per-entry limit.
MAX_STR_CHARS = 4000
MAX_COLLECTION_ITEMS = 100
MAX_READ_BYTES = 8 * 1024 * 1024   # per ship() call; the rest ships next turn
MAX_TRACKED_SESSIONS = 200

# A firewall in front of the platform (the WAF on its load balancer) can
# refuse a batch for what is IN it: transcript text full of shell commands,
# local URLs and ../ paths reads as an attack to its managed rules. It answers
# with its own HTML page, never the platform's JSON, and it refuses the same
# bytes every time. Re-sending them held the session's cursor forever, so no
# later turn shipped either (one session re-sent one batch ~17,000 times).
# Such a batch is split until the refused entries are found; each ships as
# this note instead, and the rest of the conversation moves on.
REFUSED_ENTRY_NOTE = (
    "[Not mirrored: a firewall in front of Society AI refused this message. "
    "It is still in the session's transcript on its machine.]"
)

# Any other failed ship is retried by the retry loop, at most this often.
RETRY_BACKOFF_FIRST = 120.0
RETRY_BACKOFF_MAX = 3600.0

_PROJECTS_DIR = pathlib.Path.home() / ".claude" / "projects"


def transcript_path(cwd: str, session_id: str) -> pathlib.Path:
    """Path of a session's local transcript. Claude Code munges the cwd by
    replacing every non-alphanumeric character with '-'
    (/Users/x/.claude → -Users-x--claude)."""
    # Claude Code files transcripts under the PHYSICAL cwd (getcwd resolves
    # symlinks: /tmp -> /private/tmp), so resolve before munging.
    munged = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))
    return _PROJECTS_DIR / munged / f"{session_id}.jsonl"


def _trim(value: Any, depth: int = 0) -> Any:
    """Recursively cap strings and collections, preserving record shape."""
    if depth > 12:
        return "…[depth-trimmed]"
    if isinstance(value, str):
        if len(value) > MAX_STR_CHARS:
            return value[:MAX_STR_CHARS] + f"…[+{len(value) - MAX_STR_CHARS} chars]"
        return value
    if isinstance(value, list):
        out = [_trim(v, depth + 1) for v in value[:MAX_COLLECTION_ITEMS]]
        if len(value) > MAX_COLLECTION_ITEMS:
            out.append(f"…[+{len(value) - MAX_COLLECTION_ITEMS} items]")
        return out
    if isinstance(value, dict):
        out = {k: _trim(v, depth + 1) for k, v in islice(value.items(), MAX_COLLECTION_ITEMS)}
        if len(value) > MAX_COLLECTION_ITEMS:
            out["…"] = f"+{len(value) - MAX_COLLECTION_ITEMS} keys trimmed"
        return out
    return value


def _clip(s: Any, n: int = 200) -> str:
    s = s if isinstance(s, str) else ""
    s = " ".join(s.split())
    return s[:n]


_RELAY_RE = re.compile(
    r'<cross-session-message[^>]*from-name="Society AI"[^>]*>\n?(.*?)\n?</cross-session-message>', re.S,
)


# The first line of the Society AI blockquote the agent shows at the top of a reply
# (the SessionStart identity banner, the first-message mirror line).
_BANNER_FIRST_LINES = (
    "> ⚡ **Society AI**",
    "> 🪞 Mirrored and tracked on Society AI",
    "> 🔒 Not mirrored",
)


def strip_banner(text: str) -> str:
    """The reply without the Society AI blockquote at its top: the banner is for
    the Claude Code app, the chat on Society AI does not need it."""
    lines = text.lstrip("\n").split("\n")
    if not lines or not lines[0].startswith(_BANNER_FIRST_LINES):
        return text
    i = 0
    while i < len(lines) and lines[i].startswith(">"):
        i += 1
    return "\n".join(lines[i:]).strip()


def chat_echo(text: str) -> str:
    """A user entry the app already holds: the projection into the chat skips it
    and the agent's reply to it (the reply is saved by the chat already)."""
    return f'<channel source="society-ai" kind="chat" via="app">\n{text}\n</channel>'


def _shippable_entries(record: dict, skills: Optional[dict] = None) -> list[dict]:
    """Project one raw transcript record into zero or more shippable
    entries. Deterministic — same record always
    yields the same entries in the same order. `skills` maps a Skill tool
    call's id to the skill's name (from its result, the line before)."""
    rtype = record.get("type")

    if rtype == "summary":
        s = _clip(record.get("summary"), MAX_STR_CHARS)
        return [{"entry_type": "summary", "content": {"summary": s}}] if s else []

    if rtype == "user":
        content = (record.get("message") or {}).get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for b in content:
                # tool_result blocks are command/file output — never ship.
                if isinstance(b, dict) and b.get("type") == "text":
                    texts.append(b.get("text") or "")
        text = "\n".join(t for t in texts if t.strip()).strip()
        if not text:
            return []
        if record.get("isMeta"):
            # Text Claude Code put in front of the model, never typed by the owner.
            origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
            kind = origin.get("kind")
            name = (skills or {}).get(record.get("sourceToolUseID"))
            if name:
                # A skill the agent loaded: its instructions, the agent's.
                return [{"entry_type": "skill", "content": {"name": _clip(name, 200), "text": _trim(text)}}]
            if kind == "peer" and origin.get("name") != "Society AI":
                # A message from another Claude session on this machine, labelled with it.
                body = origin.get("body") if isinstance(origin.get("body"), str) else ""
                if not body:
                    m = re.search(r"<cross-session-message[^>]*>\n?(.*?)\n?</cross-session-message>", text, re.S)
                    body = m.group(1) if m else ""
                if not body.strip():
                    return []
                return [{"entry_type": "peer", "content": {
                    "name": _clip(origin.get("name") or "another session", 200), "text": _trim(body.strip()),
                }}]
            if kind not in ("peer", "channel"):
                # Command caveats, reminders and the like stay on the machine. Society AI's
                # relay (peer "Society AI") and channel events go on below: they carry the
                # owner's messages from the app, which ship as chat echoes.
                return []
        relayed = _RELAY_RE.search(text)
        if relayed:
            # The owner's message from the app, carried in by the Haiku relay.
            text = chat_echo(relayed.group(1).strip())
        return [{"entry_type": "user", "content": {"text": _trim(text)}}]

    if rtype == "assistant":
        content = (record.get("message") or {}).get("content")
        out: list[dict] = []
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                text = strip_banner(b.get("text") or "") if b.get("type") == "text" else ""
                if text.strip():
                    out.append({
                        "entry_type": "assistant",
                        "content": {"text": _trim(text)},
                    })
                # tool_use blocks (Claude Code's own tools) and thinking
                # blocks never ship.
        elif isinstance(content, str) and strip_banner(content).strip():
            out.append({"entry_type": "assistant", "content": {"text": _trim(strip_banner(content))}})
        return out

    # Everything else (queue-operation, attachment, mode, last-prompt,
    # bridge-session, progress, file-history-snapshot, ...) is Claude Code
    # internal state — attachments in particular can embed file contents.
    return []


def _stopped(response: httpx.Response) -> bool:
    try:
        return bool((response.json() or {}).get("stopped"))
    except ValueError:
        return False


def _refused_for_content(response: httpx.Response) -> bool:
    """The batch itself was refused, so sending it again cannot help: too
    large (413), entries the platform cannot take (422), or a 403 that is not
    the platform's JSON (a firewall's page). The platform's own 403s are JSON
    and about the token, so those are retried."""
    if response.status_code in (413, 422):
        return True
    if response.status_code != 403:
        return False
    try:
        response.json()
    except ValueError:
        return True
    return False


def _refused_note(entry: dict) -> dict:
    """The entry with its text replaced by REFUSED_ENTRY_NOTE. Same seq, so
    the platform's dedup still holds."""
    content = entry.get("content") if isinstance(entry.get("content"), dict) else {}
    key = "summary" if entry.get("entry_type") == "summary" else "text"
    note = {k: v for k, v in entry.items() if k != "content"}
    note["content"] = {**{k: v for k, v in content.items() if k not in ("text", "summary")},
                       key: REFUSED_ENTRY_NOTE}
    return note


def _body_excerpt(text: str, n: int = 120) -> str:
    """A refusal body for the log: tags and whitespace collapsed (a firewall
    page is HTML), clipped."""
    return " ".join(re.sub(r"<[^>]+>", " ", text or "").split())[:n]


class TranscriptShipper:
    """Per-bridge shipper. One instance per persona/bridge process."""

    def __init__(self, api_url: str, token: str, state_dir: str):
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._machine = socket.gethostname()
        self._state_path = pathlib.Path(state_dir) / "mirror_state.json"
        # Fired (awaited) when the platform reports which chat a session
        # projects into: async (session_id, chat_id) -> None. The bridge
        # uses it to alias chat-composer sends back into the same session.
        self.on_chat_link = None
        # session_id -> {"pos": int, "seq": int, "cwd": str, "title": str,
        #                "work_item_kind": str|None, "work_item_id": str|None}
        self._state: dict[str, dict] = self._load_state()
        self._locks: dict[str, asyncio.Lock] = {}
        self._client: Optional[httpx.AsyncClient] = None
        # session_id -> texts the owner sent from the app as a resumed turn,
        # not yet seen in the transcript (marked as chat echoes when they are).
        self._app_turns: dict[str, list[str]] = {}
        # Skill tool call id -> skill name, from the call's result, so the skill's
        # instructions (the next line) ship as a named `skill` entry.
        self._skill_names: dict[str, str] = {}

    # -- registry --------------------------------------------------------------

    def register(
        self,
        session_id: str,
        *,
        cwd: str,
        title: str = "",
        work_item_kind: Optional[str] = None,
        work_item_id: Optional[str] = None,
        contact: Optional[dict] = None,
        chat_id: str = "",
    ) -> None:
        """Mark a session as bridge-owned and shippable. Idempotent; metadata
        refreshes on every call (a resume can update the title).

        `chat_id` is the app Chat the bridge knows the session works in, sent
        with each batch until the platform reports the session's own chat."""
        rec = self._state.setdefault(session_id, {"pos": 0, "seq": 0})
        rec["cwd"] = cwd
        if chat_id:
            rec["dispatch_chat_id"] = chat_id
        # First title wins: later dispatches into the same session (trigger
        # echoes, follow-up turns) must not overwrite the human title.
        if title and not rec.get("title"):
            rec["title"] = title[:500]
        if work_item_kind:
            rec["work_item_kind"] = work_item_kind
        if work_item_id:
            rec["work_item_id"] = str(work_item_id)[:255]
        if contact:
            # Who a contact session works for and at which level, so the
            # Sessions list can label it.
            rec["contact_sender"] = str(contact.get("sender") or "")[:255]
            rec["contact_permission"] = str(contact.get("permission") or "")[:16]
        self._prune()
        self._save_state()

    def forget_contact_sessions(self) -> int:
        """Stop recording every contact session (work items "contact:...")
        registered so far. Used when recording contact conversations is off,
        so a session registered by an earlier version stops shipping too."""
        gone = [sid for sid, rec in self._state.items()
                if str(rec.get("work_item_id") or "").startswith("contact:")]
        for sid in gone:
            del self._state[sid]
        if gone:
            self._save_state()
        return len(gone)

    def is_registered(self, session_id: str) -> bool:
        return session_id in self._state

    def work_item_kind(self, session_id: str) -> Optional[str]:
        """The kind a registered session was registered with ('direct' for one
        the owner started in Claude Code), or None."""
        return (self._state.get(session_id) or {}).get("work_item_kind")

    def expect_app_turn(self, session_id: str, text: str) -> None:
        """The owner's message from the app is about to run as a turn in this
        session (a closed session resumed for one reply): its user entry ships
        as a chat echo, since the chat already shows it and its reply."""
        if text.strip():
            self._app_turns.setdefault(session_id, []).append(text.strip())

    def forget(self, session_id: str) -> None:
        """Stop shipping a session (its mirroring was stopped, D41)."""
        if self._state.pop(session_id, None) is not None:
            self._save_state()
        self._app_turns.pop(session_id, None)

    # -- shipping ---------------------------------------------------------------

    async def ship(self, session_id: str, *, status: Optional[str] = None) -> bool:
        """Ship new transcript lines (and/or a status flip) for a registered
        session. Serialized per session; safe to call from multiple places
        (turn end, stop hook, reap). Returns True when the platform accepted
        the batch (or there was nothing to send)."""
        meta = self._state.get(session_id)
        if meta is None:
            logger.debug("ship: unknown session %s (ignored)", session_id[:8])
            return False

        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            # A status flip that failed to ship earlier (network blip at
            # reap time) is retried on the next ship of any kind.
            if status is None and meta.get("pending_status"):
                status = meta["pending_status"]

            entries, new_pos, new_seq = self._read_new_entries(session_id, meta)
            if not entries and status is None:
                return True  # nothing new, nothing to flip

            # Chunk to the server's batch cap; status rides the last chunk so
            # 'ended' lands only after all content is in. On a failure the
            # cursor stays put and the whole delta re-ships next time — the
            # server dedups on (mirror, seq), so retries are free. A batch
            # refused for its content never fails here: _ship_chunk ships its
            # refused entries as notes.
            chunks = [entries[i:i + MAX_ENTRIES_PER_BATCH]
                      for i in range(0, len(entries), MAX_ENTRIES_PER_BATCH)] or [[]]
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                outcome = await self._ship_chunk(
                    session_id, meta, chunk, status=status if is_last else None
                )
                if outcome != "ok":
                    if outcome == "failed":
                        # Status flips must not be lost — the session_ended
                        # wake (and the Scribe) depend on them. Park for retry.
                        if status:
                            meta["pending_status"] = status
                        fails = int(meta.get("fail_count", 0)) + 1
                        meta["fail_count"] = fails
                        meta["retry_at"] = time.time() + min(
                            RETRY_BACKOFF_FIRST * 2 ** (fails - 1), RETRY_BACKOFF_MAX)
                        self._save_state()
                    return False

            meta["pos"] = new_pos
            meta["seq"] = new_seq
            for k in ("pending_status", "fail_count", "retry_at"):
                meta.pop(k, None)
            self._save_state()
            return True

    async def _ship_chunk(
        self, session_id: str, meta: dict, chunk: list[dict], *, status: Optional[str]
    ) -> str:
        """Post one chunk. When it is refused for its content, split it in
        halves until the refused entries are found and ship each as a note
        (REFUSED_ENTRY_NOTE). Returns 'ok', 'stopped' or 'failed'."""
        outcome = await self._post_batch(session_id, meta, chunk, status=status)
        if outcome != "refused":
            return outcome
        if len(chunk) > 1:
            mid = len(chunk) // 2
            first = await self._ship_chunk(session_id, meta, chunk[:mid], status=None)
            if first != "ok":
                return first
            return await self._ship_chunk(session_id, meta, chunk[mid:], status=status)
        if len(chunk) == 1:
            logger.warning("mirror entry %s of %s was refused for its content; shipped as a note",
                           chunk[0].get("seq"), session_id[:8])
            outcome = await self._post_batch(
                session_id, meta, [_refused_note(chunk[0])], status=status)
            if outcome != "refused":
                return outcome
        # Even the note, or a status flip with no entries, was refused: the
        # conversation is not the cause, so try again later.
        logger.warning("mirror ship for %s refused with no entry to blame; retrying later",
                       session_id[:8])
        return "failed"

    def park_status(self, session_id: str, status: str) -> None:
        """Durably record a status flip without shipping (shutdown path —
        no network). The next bridge start's retry pass ships it."""
        meta = self._state.get(session_id)
        if meta is not None and not meta.get("pending_status"):
            meta["pending_status"] = status
            self._save_state()

    async def retry_pending(self) -> int:
        """Re-ship sessions whose status flip (or delta) failed earlier.
        Called at bridge start and periodically; a session whose ships keep
        failing waits longer each time (up to RETRY_BACKOFF_MAX). Returns
        retries attempted."""
        attempted = 0
        now = time.time()
        for sid in [s for s, m in self._state.items()
                    if m.get("pending_status") and float(m.get("retry_at", 0)) <= now]:
            attempted += 1
            await self.ship(sid)
        return attempted

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # -- internals ----------------------------------------------------------------

    def _read_new_entries(
        self, session_id: str, meta: dict
    ) -> tuple[list[dict], int, int]:
        """Read complete new JSONL lines from the transcript past the byte
        cursor. Returns (entries, new_byte_pos, next_seq). Partial trailing
        lines (mid-write) stay unread until the next call."""
        path = transcript_path(meta.get("cwd", ""), session_id)
        pos = int(meta.get("pos", 0))
        seq = int(meta.get("seq", 0))
        try:
            size = path.stat().st_size
        except OSError:
            return [], pos, seq
        if size <= pos:
            return [], pos, seq

        try:
            with path.open("rb") as f:
                f.seek(pos)
                blob = f.read(MAX_READ_BYTES)
        except OSError as e:
            logger.warning("transcript read failed for %s: %s", session_id[:8], e)
            return [], pos, seq

        # Only complete lines; leave a trailing partial for the next pass.
        last_nl = blob.rfind(b"\n")
        if last_nl < 0:
            return [], pos, seq
        complete = blob[: last_nl + 1]

        entries: list[dict] = []
        expected = self._app_turns.get(session_id) or []
        for raw in complete.split(b"\n"):
            if not raw.strip():
                continue
            for partial in self._line_entries(raw):
                if expected and partial["entry_type"] == "user":
                    text = str((partial.get("content") or {}).get("text") or "").strip()
                    if text in expected:
                        expected.remove(text)
                        partial["content"] = {"text": _trim(chat_echo(text))}
                partial["seq"] = seq
                entries.append(partial)
                seq += 1
        return entries, pos + len(complete), seq

    def _line_entries(self, raw: bytes) -> list[dict]:
        """Project one transcript line into shippable entries.
        Deterministic, so re-reads regenerate identical seq assignment."""
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []  # unparseable internals are never worth shipping
        if not isinstance(record, dict):
            return []

        ts = record.get("timestamp")
        ts = ts[:64] if isinstance(ts, str) and ts else None

        self._remember_skill(record)
        out = _shippable_entries(record, self._skill_names)
        if ts:
            for e in out:
                e["ts"] = ts
        return out

    def _remember_skill(self, record: dict) -> None:
        """A Skill tool result names the skill it launched (toolUseResult.commandName)."""
        result = record.get("toolUseResult")
        if record.get("type") != "user" or not isinstance(result, dict) or not result.get("commandName"):
            return
        content = (record.get("message") or {}).get("content")
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                self._skill_names[str(b["tool_use_id"])] = str(result["commandName"])
        while len(self._skill_names) > MAX_TRACKED_SESSIONS:
            self._skill_names.pop(next(iter(self._skill_names)))

    async def _post_batch(
        self, session_id: str, meta: dict, entries: list[dict], *, status: Optional[str]
    ) -> str:
        """POST one batch. Returns 'ok', 'stopped' (the owner stopped
        mirroring: the session is forgotten), 'refused' (refused for its
        content, see _refused_for_content) or 'failed' (try again later)."""
        body: dict[str, Any] = {
            "claude_session_id": session_id,
            "machine": self._machine,
            "cwd": meta.get("cwd"),
            "entries": entries,
        }
        for k in ("title", "work_item_kind", "work_item_id", "contact_sender", "contact_permission"):
            if meta.get(k):
                body[k] = meta[k]
        if status:
            body["status"] = status

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        try:
            r = await self._client.post(
                f"{self._api_url}/api/v1/session-mirror", json=body,
                headers=chat_headers(meta.get("chat_id") or meta.get("dispatch_chat_id")),
            )
            if r.status_code == 409 and _stopped(r):
                # The owner stopped mirroring this session (D41): later turns
                # stay on this machine.
                logger.info("mirroring stopped for %s; no longer shipped", session_id[:8])
                self.forget(session_id)
                return "stopped"
            if _refused_for_content(r):
                logger.info("mirror batch of %d entries for %s refused for its content: %s %s",
                            len(entries), session_id[:8], r.status_code, _body_excerpt(r.text))
                return "refused"
            if r.status_code >= 400:
                logger.warning(
                    "mirror ship rejected for %s: %s %s",
                    session_id[:8], r.status_code, _body_excerpt(r.text, 200),
                )
                return "failed"
            # The platform reports which chat this session projects into;
            # surface it once so the bridge can alias chat sends → session.
            try:
                chat_id = (r.json() or {}).get("chatId")
            except ValueError:
                chat_id = None
            if chat_id and meta.get("chat_id") != chat_id:
                meta["chat_id"] = chat_id
                self._save_state()
                if self.on_chat_link is not None:
                    try:
                        await self.on_chat_link(session_id, chat_id)
                    except Exception:
                        logger.exception("on_chat_link callback failed")
            logger.debug("shipped %d entries for %s", len(entries), session_id[:8])
            return "ok"
        except httpx.HTTPError as e:
            logger.warning("mirror ship failed for %s: %s", session_id[:8], e)
            return "failed"

    # -- state persistence ---------------------------------------------------------

    def _load_state(self) -> dict[str, dict]:
        try:
            data = json.loads(self._state_path.read_text())
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state))
            os.replace(tmp, self._state_path)
        except OSError as e:
            logger.warning("could not persist mirror state: %s", e)

    def _prune(self) -> None:
        if len(self._state) <= MAX_TRACKED_SESSIONS:
            return
        # Insertion order ≈ registration order; drop the oldest beyond cap.
        for k in list(self._state.keys())[: len(self._state) - MAX_TRACKED_SESSIONS]:
            self._state.pop(k, None)
            self._locks.pop(k, None)
