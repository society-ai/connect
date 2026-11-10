"""Session Tracker runner: the machine side of Society AI's session Trackers
(Society AI design: docs/design/session-trackers.md §8, §16).

What it does on this machine:
- The user-level hooks tell every running bridge about every session's turns
  (mirror_notify) and, at a session's first message, ask to register it
  (session_start). One bridge per machine holds
  ~/.cache/society-ai/tracker/runner.lock and does the tracking; the others
  ignore it.
- Registers sessions: a session started in Claude Code is registered at its
  first message when its owner tracks sessions started in Claude Code and its
  folder is not skipped; the platform creates its chat and the agent's bridge
  then mirrors its conversation (D36, D37). The answer, with the chat link,
  goes back to the hook for the banner line.
- Sends facts after each prompt and turn end, without a model: running,
  activity, turn count, the checklist from Claude Code's task tools, git state
  and the Jump in link (§16.5).
- Sends live state for the Sessions live view (§19): whether a turn runs, since
  when, the current action with its path or command, the last five actions and
  whether Claude Code waits for a permission. Batched per account, only what
  changed, at most every 5 seconds (session_live.py keeps the state).
- Runs Trackers when this machine's runtime (Claude Code) is the one the owner
  chose: it asks the server for jobs, runs each with a small model (claude -p,
  Haiku, no tools, no MCP, no settings, no saved session) and sends the output
  back (§16.4). The server builds the input from the stored conversation, so
  the model never reads a local transcript, and owns the prompt and schema.
- Each session belongs to one agent on this machine: the persona whose bridge
  started it, else the agent the session bound itself to, else the machine's
  default agent (D19, D20). It is written as that agent's owner's hidden
  Tracker agent, whose token the runner asks for once, with that persona's own
  agent token. So agents of different Society AI accounts on one machine each
  get Trackers in their own account (D33).

Off when TRACKING=false or MIRROR=false locally. Each account's "Track my
sessions" setting in the app decides for that account's sessions.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import pathlib
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import certifi
import httpx

from agents.claude_code.runner.session_live import LiveBoard

logger = logging.getLogger("session_tracker")

BASE_DIR = pathlib.Path(os.path.expanduser("~")) / ".cache" / "society-ai" / "tracker"
CLAUDE_SESSIONS_DIR = pathlib.Path(os.path.expanduser("~")) / ".claude" / "sessions"
SOCIETY_CACHE = pathlib.Path(os.path.expanduser("~")) / ".cache" / "society-ai"
DESKTOP_LINK = "claude://claude.ai/epitaxy/{host_session_id}"

# Env marker on the model run; the hook scripts exit at once when they see it.
TRACKER_ENV = "SOCIETY_AI_TRACKER"

STOP_DELAY_S = 6.0          # the final answer lands in the transcript after Stop
PROMPT_DELAY_S = 1.5
CONFIG_REFRESH_S = 600.0
LOCK_RETRY_S = 60.0
MODEL_TIMEOUT_S = 120.0
MESSAGE_CAP = 4_000         # chars per message
MAX_SESSIONS = 500          # remembered sessions (oldest dropped)
RECHECK_S = 600.0           # a session not tracked is asked about again after this
FEED_POLL_S = 20.0          # how often to ask for Tracker jobs
FEED_AFTER_TURN_S = 8.0     # ask soon after a turn ends on this machine
FEED_MAX_JOBS = 3
RUNTIME = "claude_code"
LIVE_INTERVAL_S = 5.0       # per account, at most one live batch this often
LIVE_TICK_S = 1.0
LIVE_BATCH = 50             # sessions per live batch (the route's limit)
LIVE_BACKOFF_S = 10.0       # first wait after a 429 without Retry-After
LIVE_BACKOFF_MAX_S = 120.0

_NOISE_PREFIXES = (
    "<system-reminder>", "<task-notification>", "<command-", "<local-command",
    "<user-prompt-submit-hook>", "Caveat:",
)
_TASK_STATES = {"pending": "todo", "in_progress": "in_progress", "completed": "done"}


# ── Transcript reading (pure, tested) ─────────────────────────────────────

@dataclass
class Segment:
    """What a slice of the transcript holds, for one job."""
    turns: list[tuple[str, str]] = field(default_factory=list)  # (USER|AGENT, text)
    user_turns: int = 0
    last_user_at: Optional[str] = None
    last_agent_at: Optional[str] = None
    first_prompt: Optional[str] = None


def _clean(text: str) -> str:
    text = text.strip()
    if not text or text.startswith(_NOISE_PREFIXES):
        return ""
    return text[:MESSAGE_CAP]


def read_records(path: pathlib.Path, start: int) -> tuple[list[dict], int]:
    """Complete JSONL records from byte offset `start`; returns (records, new offset)."""
    try:
        with path.open("rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return [], start
    end = data.rfind(b"\n")
    if end < 0:
        return [], start
    records = []
    for line in data[: end + 1].splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records, start + end + 1


def apply_tasks(records: list[dict], tasks: dict[str, dict]) -> dict[str, dict]:
    """Replay Claude Code's checklist tools (TaskCreate / TaskUpdate, older
    TodoWrite) onto `tasks` ({id: {text, state}}), in order."""
    counter = max((int(k) for k in tasks if k.isdigit()), default=0)
    for r in records:
        if r.get("isSidechain"):
            continue
        content = (r.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name, inp = b.get("name"), b.get("input") or {}
            if name == "TaskCreate":
                counter += 1
                text = str(inp.get("subject") or inp.get("description") or "").strip()
                if text:
                    tasks[str(counter)] = {"text": text[:500], "state": "todo"}
            elif name == "TaskUpdate":
                tid = str(inp.get("taskId") or "")
                status = inp.get("status")
                if tid in tasks:
                    if status == "deleted":
                        tasks.pop(tid, None)
                    elif status in _TASK_STATES:
                        tasks[tid]["state"] = _TASK_STATES[status]
                    if inp.get("subject"):
                        tasks[tid]["text"] = str(inp["subject"])[:500]
            elif name == "TodoWrite" and isinstance(inp.get("todos"), list):
                tasks.clear()
                for i, t in enumerate(inp["todos"], start=1):
                    text = str(t.get("content") or "").strip()
                    if text:
                        tasks[str(i)] = {"text": text[:500], "state": _TASK_STATES.get(t.get("status"), "todo")}
                counter = len(tasks)
    return tasks


def extract_segment(records: list[dict]) -> Segment:
    """The conversation in `records`: user prompts and agent text only."""
    seg = Segment()
    for r in records:
        if r.get("isSidechain"):
            continue
        kind = r.get("type")
        content = (r.get("message") or {}).get("content")
        ts = r.get("timestamp")
        if kind == "user":
            origin = r.get("origin") or {}
            if origin.get("kind") == "peer":
                # A message from another local Claude session. One from the
                # Society AI relay is the owner writing from the app.
                text = _clean(str(origin.get("body") or ""))
                if text:
                    who = "USER" if origin.get("name") == "Society AI" else f"PEER {origin.get('name') or 'session'}"
                    seg.turns.append((who, text))
                    if who == "USER":
                        seg.user_turns += 1
                        seg.last_user_at = ts or seg.last_user_at
                continue
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    continue
                texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            else:
                continue
            text = _clean("\n".join(texts))
            if text:
                seg.turns.append(("USER", text))
                seg.user_turns += 1
                seg.last_user_at = ts or seg.last_user_at
                seg.first_prompt = seg.first_prompt or text
        elif kind == "assistant" and isinstance(content, list):
            text = _clean("\n".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            ))
            if text:
                # Consecutive agent texts of one turn read as one message.
                if seg.turns and seg.turns[-1][0] == "AGENT":
                    seg.turns[-1] = ("AGENT", (seg.turns[-1][1] + "\n" + text)[:MESSAGE_CAP])
                else:
                    seg.turns.append(("AGENT", text))
                seg.last_agent_at = ts or seg.last_agent_at
    return seg


# ── Machine facts (no model) ─────────────────────────────────────────────

async def _git(cwd: str, *args: str) -> Optional[str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", cwd, *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return out.decode().strip() if proc.returncode == 0 else None
    except (OSError, asyncio.TimeoutError):
        return None


async def git_state(cwd: str) -> Optional[dict]:
    """Branch, dirty files and unpushed commits of the repo at `cwd`."""
    if not cwd or not os.path.isdir(cwd):
        return None
    top = await _git(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return None
    branch = await _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    porcelain = await _git(cwd, "status", "--porcelain")
    unpushed = await _git(cwd, "rev-list", "--count", "@{u}..HEAD")
    common = await _git(cwd, "rev-parse", "--git-common-dir")
    worktree = top if common and not common.endswith(".git") and os.path.basename(common) != ".git" else None
    repo = {
        "path": top,
        "branch": branch,
        "dirty_files": len([ln for ln in (porcelain or "").splitlines() if ln.strip()]),
        "unpushed": int(unpushed) if unpushed and unpushed.isdigit() else 0,
    }
    if worktree and ".claude/worktrees" in worktree:
        repo["worktree"] = worktree
    return {"captured_at": _now(), "repos": [repo]}


def running_session(session_id: str) -> Optional[dict]:
    """Claude Code's own registry entry for a running session, if any."""
    try:
        for p in CLAUDE_SESSIONS_DIR.glob("*.json"):
            try:
                data = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if data.get("sessionId") != session_id:
                continue
            pid = data.get("pid")
            try:
                os.kill(int(pid), 0)
            except (OSError, TypeError, ValueError):
                return None
            return data
    except OSError:
        pass
    return None


def bridge_session(session_id: str) -> Optional[dict]:
    """The bridge registry row of a session some bridge on this machine started."""
    for path in SOCIETY_CACHE.glob("**/sessions.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for row in data.get("sessions") or []:
            if row.get("session_id") == session_id:
                return row
    return None


def _is_updates_session(session_id: str) -> bool:
    from core.contacts import UPDATES_SESSION

    row = bridge_session(session_id)
    return bool(row) and row.get("work_item_key") == UPDATES_SESSION


def bound_agent(pid: Any) -> Optional[str]:
    """The agent a session bound itself to ("act as X"), via its MCP binding file."""
    try:
        data = json.loads((SOCIETY_CACHE / "session-binding" / f"{int(pid)}.json").read_text())
        return data.get("agent") or None
    except (OSError, ValueError, TypeError):
        return None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def folder_skipped(cwd: Optional[str], skip_folders: list) -> bool:
    """Whether `cwd` is a skipped folder or inside one (~/ is this machine's home)."""
    if not cwd:
        return False
    path = cwd.rstrip("/") or "/"
    home = os.path.expanduser("~").rstrip("/")
    for folder in skip_folders or []:
        target = str(folder).rstrip("/")
        if target.startswith("~/"):
            target = home + target[1:]
        if target and (path == target or path.startswith(target + "/")):
            return True
    return False


# ── The runner ────────────────────────────────────────────────────────────

class SessionTrackerRunner:
    def __init__(self, persona_name: str, persona_token: str, api_url: str,
                 default_agent: Optional[str] = None):
        self._persona = persona_name
        self._persona_token = persona_token
        self._api_url = api_url.rstrip("/")
        self._default_agent = default_agent or persona_name
        self._host = socket.gethostname().split(".")[0]
        self._lock_fd: Optional[int] = None
        # agent -> (fetched at, runner-config of that agent's owner)
        self._configs: dict[str, tuple[float, Optional[dict]]] = {}
        # agent -> {api_url, agent_name, token} of its owner's Tracker agent
        self._identities: dict[str, dict] = {}
        # Sessions refused for this run (403); tried again after a restart.
        self._refused: set[str] = set()
        self._pending: dict[str, dict] = {}
        self._feed_due = 0.0
        self._feed_wake = asyncio.Event()
        self._wake = asyncio.Event()
        self._state: dict[str, Any] = {"sessions": {}}
        self._client: Optional[httpx.AsyncClient] = None
        # Live view (§19): state per session, and per account when it may send next.
        self._live = LiveBoard()
        self._live_wake = asyncio.Event()
        self._live_off = False      # the router has no live route: off until a restart
        self._live_accounts: dict[str, dict] = {}
        self._clock = time.monotonic
        self.running = True

    # -- hook events (any bridge; only the lock holder acts) --

    def notify(self, params: dict) -> None:
        if self._lock_fd is None:
            return
        sid = str(params.get("session_id") or "")
        cwd = str(params.get("cwd") or "")
        if not sid or cwd.startswith(str(BASE_DIR)):
            return
        if _is_updates_session(sid):
            # Society AI updates for the Lead stay on the agent's side (no Tracker, no chat).
            return
        event = "stop" if str(params.get("event") or "stop") == "stop" else "prompt"
        if event == "stop":
            self._live.stop(sid)
        else:
            self._live.prompt(sid)
        self._live_wake.set()
        prev = self._pending.get(sid, {})
        merged_event = "stop" if "stop" in (event, prev.get("event")) else "prompt"
        delay = STOP_DELAY_S if event == "stop" else PROMPT_DELAY_S
        self._pending[sid] = {
            "event": merged_event,
            "cwd": cwd or prev.get("cwd", ""),
            "transcript_path": params.get("transcript_path") or prev.get("transcript_path"),
            "due": time.monotonic() + delay,
        }
        self._wake.set()
        if event == "stop":
            # The turn's conversation reaches the platform with the Stop ship;
            # its Tracker job is ready a few seconds later.
            self._feed_due = min(self._feed_due, time.monotonic() + FEED_AFTER_TURN_S)
            self._feed_wake.set()

    def live_event(self, params: dict) -> bool:
        """A tool call or a permission prompt in a session (hook_live.py).
        Kept in memory and sent with the next live batch; never blocks the
        hook. False when this bridge is not the machine's runner."""
        if self._lock_fd is None:
            return False
        sid = str(params.get("session_id") or "")
        cwd = str(params.get("cwd") or "")
        if not sid or cwd.startswith(str(BASE_DIR)):
            return True
        event = params.get("event")
        if event == "tool":
            self._live.tool(sid, str(params.get("action") or ""))
        elif event == "permission":
            self._live.permission(sid)
        else:
            return True
        self._live_wake.set()
        return True

    async def session_start(self, params: dict) -> Optional[dict]:
        """The first message of a session started in Claude Code (the hook's
        session_start, D37): register it now and say whether it is mirrored
        and tracked, with its chat link, or why not. None when this bridge is
        not the machine's runner (another bridge answers)."""
        if self._lock_fd is None:
            return None
        sid = str(params.get("session_id") or "")
        cwd = str(params.get("cwd") or "")
        if not sid or cwd.startswith(str(BASE_DIR)) or _is_updates_session(sid):
            return {"mirrored": False}
        origin, agent = self._origin(sid)
        if origin != "direct":
            return {"mirrored": False}
        st = self._state["sessions"].setdefault(sid, {"cursor": 0, "tasks": {}})
        st["seen"] = time.time()
        st["agent"] = agent
        # The platform decides (setting, skipped folders, stopped), not a cached
        # config: a setting turned on a minute ago counts for this session.
        if self._agent_token(agent) is None:
            return {"mirrored": False}
        job = {"cwd": cwd, "transcript_path": params.get("transcript_path")}
        title = str(params.get("prompt") or "").strip()
        if await self._register(sid, job, st, _clean(title) or None):
            self._configs.pop(agent, None)  # tracking is on: refresh what this runner does
        self._save_state()
        return {
            "mirrored": bool(st.get("mirrored")),
            "chat_url": st.get("chat_url"),
            "reason": st.get("reason"),
            "folder": st.get("folder"),
        }

    # -- lifecycle --

    async def run(self) -> None:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        (BASE_DIR / "runs").mkdir(exist_ok=True)
        while self.running and not self._try_lock():
            await asyncio.sleep(LOCK_RETRY_S)
        if not self.running:
            return
        logger.info("Session tracking: this bridge (%s) tracks sessions on this machine", self._persona)
        self._load_state()
        try:
            from agents.claude_code.runner.configure_hooks import ensure_live_hooks
            if ensure_live_hooks():
                logger.info("Added the live view hooks (PreToolUse, Notification) to ~/.claude/settings.json")
        except Exception:
            logger.exception("Could not add the live view hooks")
        feed = asyncio.ensure_future(self._feed_loop())
        live = asyncio.ensure_future(self._live_loop())
        try:
            await self._facts_loop()
        finally:
            feed.cancel()
            live.cancel()

    async def _facts_loop(self) -> None:
        while self.running:
            try:
                await self._drain()
            except Exception:
                logger.exception("Session tracker loop error")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    def stop(self) -> None:
        self.running = False
        self._wake.set()
        self._feed_wake.set()
        self._live_wake.set()

    def _try_lock(self) -> bool:
        fd = os.open(BASE_DIR / "runner.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    # -- persistence --

    def _load_state(self) -> None:
        try:
            self._state = json.loads((BASE_DIR / "state.json").read_text())
        except (OSError, ValueError):
            self._state = {"sessions": {}}
        self._state.setdefault("sessions", {})
        try:
            self._identities = json.loads(self._identity_path().read_text())
        except (OSError, ValueError):
            self._identities = {}
        # The single-account identity file of v0.24.0: its token is replaced
        # by the per-agent ones above, so it is not left on disk.
        (BASE_DIR / "identity.json").unlink(missing_ok=True)

    def _save_state(self) -> None:
        sessions = self._state["sessions"]
        if len(sessions) > MAX_SESSIONS:
            keep = sorted(sessions.items(), key=lambda kv: kv[1].get("seen", 0))[-MAX_SESSIONS:]
            self._state["sessions"] = dict(keep)
        tmp = BASE_DIR / "state.json.tmp"
        tmp.write_text(json.dumps(self._state))
        os.replace(tmp, BASE_DIR / "state.json")

    # -- platform --

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._api_url, timeout=30.0,
                verify=ssl.create_default_context(cafile=certifi.where()),
            )
        return self._client

    def _agent_token(self, agent: str) -> Optional[str]:
        """The agent token of a persona on this machine, for the same platform."""
        if agent == self._persona:
            return self._persona_token
        from core.hook_common import discover_personas
        for p in discover_personas():
            if p["name"] == agent and p["api_url"].rstrip("/") == self._api_url:
                return p["token"]
        return None

    async def _config_for(self, agent: str) -> Optional[dict]:
        """runner-config of the agent's owner: what this runner does for their sessions."""
        cached = self._configs.get(agent)
        if cached and time.monotonic() - cached[0] < CONFIG_REFRESH_S:
            return cached[1]
        config = cached[1] if cached else None
        token = self._agent_token(agent)
        if token is None:
            if cached is None:
                logger.warning("No agent named %s on this machine: its sessions are not tracked", agent)
            self._configs[agent] = (time.monotonic(), None)
            return None
        try:
            r = await self._http().get(
                "/api/v1/session-trackers/runner-config",
                params={"runtime": RUNTIME},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code == 200:
                config = r.json()
                was = cached and cached[1] and cached[1].get("tracking")
                if cached is None or bool(was) != bool(config.get("tracking")):
                    logger.info("Session tracking is %s for %s's account%s",
                                "on" if config.get("tracking") else "off", agent,
                                ", Trackers run here" if config.get("runs_trackers") else "")
            else:
                logger.warning("runner-config for %s returned %s", agent, r.status_code)
        except httpx.HTTPError as e:
            logger.warning("runner-config for %s failed: %s", agent, e)
        self._configs[agent] = (time.monotonic(), config)
        return config

    def _identity_path(self) -> pathlib.Path:
        return BASE_DIR / "identities.json"

    def _save_identities(self) -> None:
        path = self._identity_path()
        fd = os.open(path.with_suffix(".tmp"), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._identities, f)
        os.replace(path.with_suffix(".tmp"), path)

    async def _tracker_token(self, agent: str) -> Optional[str]:
        """Token of the Tracker agent of the agent's owner, asked for once per agent."""
        identity = self._identities.get(agent)
        if identity and identity.get("api_url") == self._api_url:
            return identity.get("token")
        agent_token = self._agent_token(agent)
        if agent_token is None:
            return None
        r = await self._http().post(
            "/api/v1/session-trackers/agent",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"host": self._host},
        )
        if r.status_code != 200:
            logger.warning("Could not get the Tracker agent's token for %s (%s)", agent, r.status_code)
            return None
        body = r.json()
        self._identities[agent] = {
            "api_url": self._api_url, "agent_name": body["agent_name"], "token": body["token"],
        }
        self._save_identities()
        return body["token"]

    async def _call(self, agent: str, method: str, path: str,
                    body: Optional[dict] = None) -> tuple[int, dict]:
        token = await self._tracker_token(agent)
        if not token:
            return 0, {}
        r = await self._http().request(
            method, path, headers={"Authorization": f"Bearer {token}"}, json=body,
        )
        if r.status_code == 401:
            # Revoked by the owner: forget it and ask again next time.
            self._identities.pop(agent, None)
            self._save_identities()
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}

    # -- jobs --

    async def _drain(self) -> None:
        now = time.monotonic()
        due = [sid for sid, p in self._pending.items() if p["due"] <= now]
        for sid in due:
            job = self._pending.pop(sid)
            try:
                await self._process(sid, job)
            except Exception:
                logger.exception("Tracker job failed for %s", sid[:8])

    def _transcript(self, sid: str, job: dict) -> Optional[pathlib.Path]:
        p = job.get("transcript_path")
        if p and os.path.isfile(p):
            return pathlib.Path(p)
        if job.get("cwd"):
            from agents.claude_code.runner.transcript_shipper import transcript_path
            path = transcript_path(job["cwd"], sid)
            if path.is_file():
                return path
        return None

    def _origin(self, sid: str) -> tuple[str, str]:
        """(origin, agent) of a session: who started it and which agent it is."""
        live = running_session(sid)
        bridge_row = bridge_session(sid)
        if bridge_row or (live and str(live.get("tmux") or "").startswith("sai-")):
            return "society_ai", (bridge_row or {}).get("persona") or self._default_agent
        return "direct", bound_agent((live or {}).get("pid")) or self._default_agent

    async def _register(self, sid: str, job: dict, st: dict, first_prompt: Optional[str]) -> bool:
        live = running_session(sid)
        origin, agent = self._origin(sid)
        body: dict[str, Any] = {
            "runtime": "claude_code",
            "external_session_id": sid,
            "origin": origin,
            "agent_name": agent,
            "host": self._host,
            "cwd": job.get("cwd") or None,
            "resume_command": f"claude --resume {sid}",
        }
        if live and live.get("hostSessionId"):
            body["open_url"] = DESKTOP_LINK.format(host_session_id=live["hostSessionId"])
        if first_prompt:
            body["title"] = first_prompt.splitlines()[0][:60]
        body = {k: v for k, v in body.items() if v is not None}
        # For the owner's skip folders written as ~/...; not stored.
        body["home"] = os.path.expanduser("~")
        status, data = await self._call(agent, "POST", "/api/v1/session-trackers/register", body)
        st["checked_at"] = time.time()
        if status not in (200, 201):
            if status == 403:
                self._refused.add(sid)
                logger.warning("Tracker register for %s as %s was refused; tried again after a restart",
                               sid[:8], agent)
            else:
                logger.warning("Tracker register for %s returned %s", sid[:8], status)
            return False
        if data.get("tracked") is False or not data.get("tracker"):
            # Tracking off, sessions started in Claude Code off, a skipped
            # folder, or mirroring stopped for this session (D41).
            st["mirrored"] = False
            st["reason"] = data.get("reason") or "off"
            st["folder"] = data.get("folder")
            return False
        tracker = data["tracker"]
        st["tracker_id"] = tracker["id"]
        st["agent"] = agent
        st.pop("reason", None)
        st.pop("folder", None)
        # What the bridge needs to reply into this session from its chat page
        # (direct_reply.py) and to mirror it (the agent's own bridge): any
        # bridge on this machine reads it from here.
        st["chat_id"] = tracker.get("chatId")
        st["chat_url"] = data.get("chat_url")
        st["origin"] = tracker.get("origin")
        st["mirrored"] = tracker.get("origin") == "direct"
        st["cwd"] = job.get("cwd") or tracker.get("cwd")
        return True

    async def _process(self, sid: str, job: dict) -> None:
        """The machine's facts for one session after a prompt or turn end (§16.5).
        The summary itself comes from the server feed (_feed_loop)."""
        sessions = self._state["sessions"]
        if sid in self._refused:
            return
        st = sessions.setdefault(sid, {"cursor": 0, "tasks": {}})
        st["seen"] = time.time()
        if "tracker_id" not in st or "agent" not in st:
            st["agent"] = self._origin(sid)[1]
        agent = st["agent"]
        config = await self._config_for(agent)
        if not config or not config.get("tracking"):
            return
        if st.get("reason") == "stopped":
            return
        path = self._transcript(sid, job)
        if path is None:
            return

        first_build = "tracker_id" not in st
        if first_build:
            if st.get("reason") and time.time() - st.get("checked_at", 0) < RECHECK_S:
                return  # not tracked (setting, skipped folder): asked again later
            # The checklist from the whole file.
            records, end = read_records(path, 0)
            st["tasks"] = apply_tasks(records, {})
        else:
            records, end = read_records(path, st.get("cursor", 0))
            st["tasks"] = apply_tasks(records, st.get("tasks") or {})
        seg = extract_segment(records)

        if first_build and not await self._register(sid, job, st, seg.first_prompt):
            self._save_state()
            return

        facts: dict[str, Any] = {
            "running": job["event"] == "prompt",
            "last_activity_at": _now(),
            "steps": [{"text": t["text"], "state": t["state"]} for t in st["tasks"].values()],
            "host": self._host,
        }
        if seg.last_user_at:
            facts["last_user_at"] = seg.last_user_at
        if seg.last_agent_at:
            facts["last_agent_at"] = seg.last_agent_at
        st["turns"] = st.get("turns", 0) + seg.user_turns
        facts["turns"] = st["turns"]
        live = running_session(sid)
        if live and live.get("hostSessionId"):
            facts["open_url"] = DESKTOP_LINK.format(host_session_id=live["hostSessionId"])
        if job.get("cwd"):
            facts["cwd"] = job["cwd"]
            ws = await git_state(job["cwd"])
            if ws:
                facts["work_state"] = ws

        status, _ = await self._call(
            agent, "PATCH", f"/api/v1/session-trackers/{st['tracker_id']}", {"facts": facts, "no_change": True},
        )
        if status == 404:
            # Deleted by the owner: next activity registers it as new (D23).
            sessions.pop(sid, None)
            self._save_state()
            return
        if status == 200:
            st["cursor"] = end
        else:
            logger.warning("Tracker facts for %s returned %s", sid[:8], status)
        self._save_state()

    # -- live view (§19.3) --

    async def _live_loop(self) -> None:
        while self.running and not self._live_off:
            try:
                await self._live_flush()
            except Exception:
                logger.exception("Session live loop error")
            try:
                await asyncio.wait_for(self._live_wake.wait(), timeout=LIVE_TICK_S)
            except asyncio.TimeoutError:
                pass
            self._live_wake.clear()

    def _live_target(self, sid: str) -> Optional[str]:
        """The agent whose owner's Tracker gets this session's live state, or
        None when the session is not one this runner tracks."""
        st = self._state["sessions"].get(sid)
        if not st or sid in self._refused or "tracker_id" not in st or not st.get("agent"):
            return None
        if st.get("reason") == "stopped":
            return None
        return st["agent"]

    async def _live_flush(self) -> int:
        """Send every account's changed sessions, at most one batch per account
        every LIVE_INTERVAL_S. Returns the sessions sent."""
        if self._live_off:
            return 0
        board = self._live
        for sid in board.heartbeat_due():
            board.beat(sid, running_session(sid) is not None)
        board.prune()
        # owner (Tracker agent) -> (an agent of that account, its sessions)
        groups: dict[str, tuple[str, list[str]]] = {}
        for sid in board.dirty():
            agent = self._live_target(sid)
            if agent is None:
                continue
            config = await self._config_for(agent)
            if not config or not config.get("tracking"):
                continue
            st = self._state["sessions"][sid]
            if st.get("origin") == "direct" or st.get("mirrored"):
                # Sessions started in Claude Code: the same switch and skipped folders as registering.
                if config.get("claude_code_sessions") is False:
                    continue
                if folder_skipped(st.get("cwd"), config.get("skip_folders") or []):
                    continue
            if not await self._tracker_token(agent):
                continue
            owner = (self._identities.get(agent) or {}).get("agent_name") or agent
            groups.setdefault(owner, (agent, []))[1].append(sid)

        sent = 0
        now = self._clock()
        for owner, (agent, sids) in groups.items():
            account = self._live_accounts.setdefault(owner, {"next_at": 0.0, "backoff": 0.0})
            if now < account["next_at"]:
                continue  # coalesced: the newest state goes with the next batch
            account["next_at"] = now + LIVE_INTERVAL_S
            for i in range(0, len(sids), LIVE_BATCH):
                batch = [board.payload(sid) for sid in sids[i:i + LIVE_BATCH]]
                status, retry_after = await self._post_live(
                    agent, {"host": self._host, "sessions": [body for _, body in batch]},
                )
                if status == 200:
                    for (rev, body) in batch:
                        board.sent(body["external_session_id"], rev)
                    sent += len(batch)
                    account["backoff"] = 0.0
                    continue
                if status == 404:
                    self._live_off = True
                    logger.info("Society AI has no live view route yet: live updates are off until a restart")
                    return sent
                if status == 429:
                    wait = retry_after or min(max(account["backoff"] * 2, LIVE_BACKOFF_S), LIVE_BACKOFF_MAX_S)
                    account["backoff"] = wait
                    account["next_at"] = self._clock() + wait
                    logger.info("Live updates for %s's account are rate limited; next try in %.0fs", agent, wait)
                    break
                if status in (400, 422):
                    # Refused as it is: dropped, the session's next change is sent anew.
                    logger.warning("Live update for %s's account was refused (%s)", agent, status)
                    for (rev, body) in batch:
                        board.sent(body["external_session_id"], rev)
                    continue
                logger.warning("Live update for %s's account returned %s; tried again", agent, status)
                break
        return sent

    async def _post_live(self, agent: str, body: dict) -> tuple[int, Optional[float]]:
        """POST one live batch as the agent's owner's Tracker agent: (status,
        Retry-After seconds). Status 0 when it could not be sent."""
        token = await self._tracker_token(agent)
        if not token:
            return 0, None
        try:
            r = await self._http().post(
                "/api/v1/session-trackers/live", json=body,
                headers={"Authorization": f"Bearer {token}"}, timeout=10.0,
            )
        except httpx.HTTPError as e:
            logger.warning("Live update for %s's account failed: %s", agent, e)
            return 0, None
        if r.status_code == 401:
            # Revoked by the owner: forget it and ask again next time.
            self._identities.pop(agent, None)
            self._save_identities()
        retry_after = None
        try:
            retry_after = max(1.0, min(float(r.headers.get("retry-after") or 0), LIVE_BACKOFF_MAX_S)) \
                if r.headers.get("retry-after") else None
        except ValueError:
            retry_after = None
        return r.status_code, retry_after

    # -- the server feed (§16.4) --

    def _machine_agents(self) -> list[str]:
        """The agents on this machine for this platform, this bridge's first."""
        from core.hook_common import discover_personas
        names = [self._persona]
        for p in discover_personas():
            if p["api_url"].rstrip("/") == self._api_url and p["name"] not in names:
                names.append(p["name"])
        return names

    async def _feed_loop(self) -> None:
        while self.running:
            wait = self._feed_due - time.monotonic()
            if wait > 0:
                try:
                    await asyncio.wait_for(self._feed_wake.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
                self._feed_wake.clear()
                continue
            self._feed_due = time.monotonic() + FEED_POLL_S
            try:
                await self._feed_once()
            except Exception:
                logger.exception("Tracker feed error")

    async def _feed_once(self) -> int:
        """Ask for jobs for every account on this machine that runs its Trackers
        here, run them and send the outputs back. Returns the jobs run."""
        done = 0
        owners: set[str] = set()
        for agent in self._machine_agents():
            config = await self._config_for(agent)
            if not config or not config.get("runs_trackers"):
                continue
            if not await self._tracker_token(agent):
                continue
            owner = (self._identities.get(agent) or {}).get("agent_name") or agent
            if owner in owners:
                continue  # another agent of the same account already asked
            owners.add(owner)
            status, data = await self._call(agent, "POST", "/api/v1/session-trackers/jobs/claim", {
                "runtime": RUNTIME, "host": self._host, "max": FEED_MAX_JOBS,
            })
            if status != 200:
                logger.warning("Tracker job claim for %s's account returned %s", agent, status)
                continue
            for job in data.get("jobs") or []:
                output = await self._run_model(job, str(job.get("input") or ""))
                if output is None:
                    continue  # its lease runs out and the job comes back
                status, _ = await self._call(
                    agent, "POST", f"/api/v1/session-trackers/jobs/{job['tracker_id']}/result", {
                        "lease": job["lease"],
                        "cursor": job["cursor"],
                        "base_version": job.get("base_version", 0),
                        "short_ids": job.get("short_ids") or {},
                        "output": output,
                    },
                )
                if status not in (200, 409):
                    logger.warning("Tracker job result for %s returned %s", job["tracker_id"][:8], status)
                done += 1
        return done

    async def _run_model(self, cfg: dict, prompt_input: str) -> Optional[dict]:
        cmd = [
            "claude", "-p",
            "--model", str(cfg.get("model") or "haiku"),
            "--output-format", "json",
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--no-session-persistence",
            "--system-prompt", str(cfg.get("prompt") or ""),
            "--json-schema", json.dumps(cfg.get("output_schema") or {}),
        ]
        env = {**os.environ, TRACKER_ENV: "1"}
        (BASE_DIR / "runs").mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(BASE_DIR / "runs"), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(prompt_input.encode()), timeout=MODEL_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError) as e:
            logger.warning("Tracker model run failed: %s", e)
            return None
        if proc.returncode != 0:
            logger.warning("Tracker model run exited %s: %s", proc.returncode, err.decode()[:300])
            return None
        try:
            result = json.loads(out)
        except ValueError:
            return None
        structured = result.get("structured_output")
        if isinstance(structured, dict):
            return structured
        text = result.get("result")
        if isinstance(text, str):
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except ValueError:
                    return None
        return None
