"""Sessions live view (Society AI design: session-trackers §19.3).

- hook_live.py: PreToolUse only says a tool started (no tool name, input,
  command, path, URL or pattern); a permission Notification says the session
  waits.
- LiveBoard: a prompt starts the turn's clock, a tool start means "working"
  (clears a permission wait, starts a turn the prompt hook missed), Stop goes
  idle, a permission waits until the next tool start or Stop; only changed
  sessions are sent again, and repeated tool starts change nothing.
- Nothing about a tool call is ever sent: a payload has exactly four fields.
- The runner's live sender: one batch per account, only tracked sessions, at
  most every 5 seconds, the newest state coalesced; a 429 waits (Retry-After,
  else a growing wait) and keeps the state; a 404 turns live updates off
  until a restart without touching the facts path.
- Hook registration: setup and the reply path's settings add PreToolUse and
  Notification idempotently; the tracking bridge adds them on machines set
  up before, only where the Society AI hooks are installed.
"""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.claude_code.runner import configure_hooks as ch  # noqa: E402
from agents.claude_code.runner import hook_live  # noqa: E402
from agents.claude_code.runner import session_live as sl  # noqa: E402
from agents.claude_code.runner import session_tracker as st  # noqa: E402


TOOL_CALLS = [
    ("Bash", {"command": "curl -H 'Authorization: Bearer abcdefghijklmnop1234' https://u:hunter2@api.x/y?token=zzz"}),
    ("Edit", {"file_path": "/Users/me/Coding/app/lib/secret_plan.ts", "old_string": "a", "new_string": "b"}),
    ("Read", {"file_path": "/Users/me/Coding/app/README.md"}),
    ("Grep", {"pattern": "def private_pattern", "path": "src"}),
    ("WebFetch", {"url": "https://example.com/docs?key=k123"}),
    ("WebSearch", {"query": "my private query"}),
    ("Agent", {"description": "Review the diff", "prompt": "long"}),
    ("mcp__society-ai__upload_file", {"path": "x"}),
]
# Text that must never appear in anything the hook or the board passes on.
TOOL_TEXT = ["Bash", "curl", "Bearer", "abcdefghijklmnop1234", "hunter2", "zzz", "api.x", "Edit",
             "secret_plan", "lib/", "README", "Read", "Grep", "private_pattern", "src", "WebFetch",
             "example.com", "k123", "WebSearch", "private query", "Agent", "Review the diff",
             "upload_file", "mcp__", "Running", "Editing", "Reading", "Searching", "Fetching", "Using"]


class Hook(unittest.TestCase):
    CWD = "/Users/me/Coding/app"

    def test_no_action_text_helpers_remain(self):
        for name in ("action_line", "clip", "_rel", "_mask", "_SECRETS", "MAX_CHARS"):
            self.assertFalse(hasattr(hook_live, name), name)

    def test_a_tool_start_carries_nothing_about_the_tool(self):
        for tool, inp in TOOL_CALLS:
            with self.subTest(tool=tool):
                params = hook_live.live_params({
                    "hook_event_name": "PreToolUse", "session_id": "s1", "cwd": self.CWD,
                    "tool_name": tool, "tool_input": inp, "tool_use_id": "toolu_1",
                })
                self.assertEqual(params, {"session_id": "s1", "cwd": self.CWD, "event": "tool"})
                for text in TOOL_TEXT:
                    self.assertNotIn(text, json.dumps(params))

    def test_permission_notifications(self):
        self.assertTrue(hook_live.is_permission_prompt({"notification_type": "permission_prompt"}))
        self.assertTrue(hook_live.is_permission_prompt({"message": "Claude needs your permission to use Bash"}))
        self.assertFalse(hook_live.is_permission_prompt({"notification_type": "idle_prompt",
                                                         "message": "Claude is waiting for your input"}))
        self.assertFalse(hook_live.is_permission_prompt({"message": "Claude is waiting for your input"}))

    def test_live_params(self):
        self.assertEqual(hook_live.live_params({
            "hook_event_name": "Notification", "session_id": "s1", "message": "needs your permission",
        })["event"], "permission")
        self.assertIsNone(hook_live.live_params({"hook_event_name": "Notification", "session_id": "s1",
                                                 "message": "waiting for your input"}))
        self.assertIsNone(hook_live.live_params({"hook_event_name": "PreToolUse", "tool_name": "Bash"}))

    def test_hook_stops_at_the_runners_answer_and_prints_nothing(self):
        replies = [False, True, True]
        used = []

        class FakeSock:
            def __init__(self, *a):
                self.reply = replies.pop(0)
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def settimeout(self, t):
                assert t <= hook_live.IPC_TIMEOUT_S
            def connect(self, path):
                used.append(path)
            def sendall(self, data):
                frame = json.loads(data)
                assert frame["method"] == "session_live"
                assert frame["params"] == {"session_id": "s1", "cwd": None, "event": "tool"}, frame
                for text in ("Bash", "ls -la", "command"):
                    assert text not in data.decode(), text
            def recv(self, n):
                return (json.dumps({"id": "x", "result": self.reply}) + "\n").encode()

        hook_input = {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                      "tool_input": {"command": "ls -la"}}
        with mock.patch.object(hook_live, "_bridge_sockets", return_value=["/a", "/b", "/c"]), \
                mock.patch.object(hook_live.socket, "socket", FakeSock), \
                mock.patch.object(hook_live, "_read_hook_input", return_value=hook_input), \
                mock.patch("builtins.print") as printed, \
                mock.patch.dict(os.environ, {"SOCIETY_AI_TRACKER": ""}):
            self.assertEqual(hook_live.main(), 0)
        self.assertEqual(used, ["/a", "/b"])
        printed.assert_not_called()

    def test_tracker_runs_never_report(self):
        with mock.patch.dict(os.environ, {"SOCIETY_AI_TRACKER": "1"}), \
                mock.patch.object(hook_live, "send") as send, \
                mock.patch.object(hook_live, "_read_hook_input",
                                  return_value={"hook_event_name": "PreToolUse", "session_id": "s1"}):
            hook_live.main()
        send.assert_not_called()


class Board(unittest.TestCase):
    def setUp(self):
        self.t = 1000.0
        self.n = 0

        def now():
            self.n += 1
            return f"2026-11-10T10:00:{self.n:02d}Z"

        self.board = sl.LiveBoard(clock=lambda: self.t, now=now)

    def test_a_turn(self):
        b = self.board
        b.prompt("s1")
        rev, body = b.payload("s1")
        self.assertEqual(body, {"external_session_id": "s1", "running": True,
                                "turn_started_at": "2026-11-10T10:00:01Z", "waiting_permission": False})
        for _ in range(7):
            b.tool("s1")
        self.assertEqual(b.payload("s1"), (rev, body))       # working: nothing new to say
        b.permission("s1")
        self.assertTrue(b.payload("s1")[1]["waiting_permission"])
        b.tool("s1")                                         # the owner answered
        _, body = b.payload("s1")
        self.assertFalse(body["waiting_permission"])
        self.assertEqual(body["turn_started_at"], "2026-11-10T10:00:01Z")  # the same turn's clock
        b.permission("s1")
        b.stop("s1")
        _, body = b.payload("s1")
        self.assertEqual(body, {"external_session_id": "s1", "running": False,
                                "turn_started_at": None, "waiting_permission": False})

    def test_a_tool_start_without_a_prompt_starts_a_turn(self):
        self.board.tool("s1")
        _, body = self.board.payload("s1")
        self.assertTrue(body["running"])
        self.assertIsNotNone(body["turn_started_at"])
        self.assertEqual(self.board.dirty(), ["s1"])

    def test_tool_takes_no_text(self):
        with self.assertRaises(TypeError):
            self.board.tool("s1", "Running pytest")

    def test_payloads_never_carry_tool_activity(self):
        b = self.board
        for step in (lambda: b.prompt("s1"), lambda: b.tool("s1"), lambda: b.permission("s1"),
                     lambda: b.tool("s1"), lambda: b.stop("s1"), lambda: b.tool("s2")):
            step()
            for sid in b.sessions:
                _, body = b.payload(sid)
                self.assertEqual(set(body), {"external_session_id", "running", "turn_started_at",
                                             "waiting_permission"})
                self.assertNotIn("action", body)
                self.assertNotIn("recent", body)
        for field in ("action", "recent"):
            self.assertFalse(hasattr(b.sessions["s1"], field), field)

    def test_repeated_tool_starts_are_not_a_change(self):
        b = self.board
        b.prompt("s1")
        b.sent("s1", b.payload("s1")[0])
        for _ in range(50):
            b.tool("s1")
            self.assertEqual(b.dirty(), [])
        b.permission("s1")
        b.sent("s1", b.payload("s1")[0])
        b.tool("s1")                   # clears the wait: a change
        self.assertEqual(b.dirty(), ["s1"])
        b.sent("s1", b.payload("s1")[0])
        b.tool("s1")
        self.assertEqual(b.dirty(), [])
        b.stop("s1")
        b.sent("s1", b.payload("s1")[0])
        b.tool("s1")                   # a turn the prompt hook missed starts: a change
        self.assertEqual(b.dirty(), ["s1"])
        b.sent("s1", b.payload("s1")[0])
        b.tool("s1")
        self.assertEqual(b.dirty(), [])

    def test_change_detection(self):
        b = self.board
        b.prompt("s1")
        rev, _ = b.payload("s1")
        self.assertEqual(b.dirty(), ["s1"])
        b.permission("s1")         # changed while the batch was on its way
        b.sent("s1", rev)
        self.assertEqual(b.dirty(), ["s1"])
        b.sent("s1", b.payload("s1")[0])
        self.assertEqual(b.dirty(), [])
        b.permission("s1")         # already waiting: no change
        b.stop("s1")
        b.sent("s1", b.payload("s1")[0])
        b.stop("s1")               # already stopped: no change
        self.assertEqual(b.dirty(), [])

    def test_tool_starts_keep_a_long_turn_from_being_forgotten(self):
        b = self.board
        b.prompt("s1")
        b.sent("s1", b.payload("s1")[0])
        self.t += sl.FORGET_S * 24 - 10
        b.tool("s1")
        self.t += 20
        b.prune()
        self.assertIn("s1", b.sessions)

    def test_heartbeat_and_forgetting(self):
        b = self.board
        b.prompt("s1")
        b.sent("s1", b.payload("s1")[0])
        self.assertEqual(b.heartbeat_due(), [])
        self.t += sl.HEARTBEAT_S
        self.assertEqual(b.heartbeat_due(), ["s1"])
        b.beat("s1", alive=False)
        self.assertEqual((b.heartbeat_due(), b.dirty()), ([], []))
        self.t += sl.HEARTBEAT_S
        b.beat("s1", alive=True)
        self.assertEqual(b.dirty(), ["s1"])
        b.prompt("s2")
        b.stop("s2")
        self.t += sl.FORGET_S + 1
        b.prune()
        self.assertNotIn("s2", b.sessions)
        self.assertIn("s1", b.sessions)  # still running


class Sender(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(st, "BASE_DIR", pathlib.Path(self.tmp.name))
        self.patch.start()
        self.cache = mock.patch.object(st, "SOCIETY_CACHE", pathlib.Path(self.tmp.name) / "cache")
        self.cache.start()
        self.alive = mock.patch.object(st, "running_session", return_value={"pid": 1})
        self.alive.start()
        self.t = 1000.0
        r = st.SessionTrackerRunner("jenkins", "sai_jenkins", "https://api.test")
        r._lock_fd = 99
        r._clock = lambda: self.t
        r._live = sl.LiveBoard(clock=lambda: self.t, now=lambda: "2026-11-10T10:00:00Z")
        r._host = "macbook"
        self.config = {"tracking": True, "claude_code_sessions": True, "skip_folders": [], "runs_trackers": True}
        r._configs = {a: (time.monotonic(), dict(self.config)) for a in ("jenkins", "searchpro", "kilo")}
        r._identities = {
            "jenkins": {"api_url": "https://api.test", "agent_name": "session-tracker-a", "token": "trk_a"},
            "searchpro": {"api_url": "https://api.test", "agent_name": "session-tracker-a", "token": "trk_a"},
            "kilo": {"api_url": "https://api.test", "agent_name": "session-tracker-b", "token": "trk_b"},
        }
        r._state["sessions"] = {
            "s1": {"tracker_id": "t1", "agent": "jenkins", "origin": "direct", "mirrored": True, "cwd": "/w/app"},
            "s2": {"tracker_id": "t2", "agent": "searchpro", "origin": "society_ai"},
            "s3": {"tracker_id": "t3", "agent": "kilo", "origin": "direct", "cwd": "/w/other"},
        }
        self.posts = []
        self.responses = []

        async def fake_post(agent, body):
            self.posts.append((agent, body))
            return self.responses.pop(0) if self.responses else (200, None)

        r._post_live = fake_post
        self.r = r

    def tearDown(self):
        self.alive.stop()
        self.cache.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def flush(self):
        return asyncio.run(self.r._live_flush())

    def sids(self, i=-1):
        return [s["external_session_id"] for s in self.posts[i][1]["sessions"]]

    def test_one_batch_per_account_tracked_sessions_only(self):
        self.r.notify({"session_id": "s1", "event": "prompt"})
        # Anything else in the params (an older hook's action line) is ignored.
        self.r.live_event({"session_id": "s1", "event": "tool", "action": "Running pytest",
                           "tool_name": "Bash", "tool_input": {"command": "pytest"}})
        self.r.live_event({"session_id": "s2", "event": "tool"})
        self.r.live_event({"session_id": "s3", "event": "permission"})
        self.r.live_event({"session_id": "unknown", "event": "tool"})
        self.assertEqual(self.flush(), 3)
        by_agent = {agent: body for agent, body in self.posts}
        self.assertEqual(len(self.posts), 2)                      # accounts a and b
        self.assertEqual(sorted(s["external_session_id"] for s in by_agent["jenkins"]["sessions"]), ["s1", "s2"])
        self.assertEqual(by_agent["kilo"]["sessions"][0]["waiting_permission"], True)
        body = by_agent["jenkins"]
        self.assertEqual(body["host"], "macbook")
        s1 = next(s for s in body["sessions"] if s["external_session_id"] == "s1")
        self.assertEqual(s1, {"external_session_id": "s1", "running": True,
                              "turn_started_at": "2026-11-10T10:00:00Z", "waiting_permission": False})
        sent = json.dumps([b for _, b in self.posts])
        for text in ("action", "recent", "Running", "pytest", "Bash", "tool_name", "command"):
            self.assertNotIn(text, sent)

    def test_from_hook_to_post_nothing_about_the_tools(self):
        self.r.notify({"session_id": "s1", "event": "prompt"})
        for i, (tool, inp) in enumerate(TOOL_CALLS):
            for hook_input in ({"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": "/w/app",
                                "tool_name": tool, "tool_input": inp},
                               {"hook_event_name": "Notification", "session_id": "s1", "cwd": "/w/app",
                                "notification_type": "permission_prompt",
                                "message": f"Claude needs your permission to use {tool}"}):
                self.assertTrue(self.r.live_event(hook_live.live_params(hook_input)))
                self.t += st.LIVE_INTERVAL_S
                self.flush()
        self.assertGreater(len(self.posts), len(TOOL_CALLS))
        for _, body in self.posts:
            for session in body["sessions"]:
                self.assertEqual(set(session), {"external_session_id", "running", "turn_started_at",
                                                "waiting_permission"})
        sent = json.dumps([body for _, body in self.posts])
        for text in TOOL_TEXT:
            self.assertNotIn(text, sent)

    def test_only_changes_at_most_every_five_seconds_newest_state(self):
        self.r.live_event({"session_id": "s1", "event": "tool"})
        self.flush()
        self.assertEqual(self.flush(), 0)                         # nothing changed
        self.t += 1
        self.r.live_event({"session_id": "s1", "event": "permission"})
        self.r.notify({"session_id": "s1", "event": "stop"})
        self.assertEqual(self.flush(), 0)                         # inside the 5 s window
        self.t += st.LIVE_INTERVAL_S
        self.assertEqual(self.flush(), 1)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.posts[-1][1]["sessions"][0],
                         {"external_session_id": "s1", "running": False, "turn_started_at": None,
                          "waiting_permission": False})

    def test_a_busy_session_is_not_sent_on_every_tool_call(self):
        self.r.notify({"session_id": "s1", "event": "prompt"})
        self.assertEqual(self.flush(), 1)
        for _ in range(int(sl.HEARTBEAT_S // st.LIVE_INTERVAL_S) - 1):
            self.t += st.LIVE_INTERVAL_S
            for _ in range(5):
                self.r.live_event({"session_id": "s1", "event": "tool"})
            self.assertEqual(self.flush(), 0)
        self.assertEqual(len(self.posts), 1)
        self.t += st.LIVE_INTERVAL_S                              # the heartbeat
        self.assertEqual(self.flush(), 1)
        self.assertTrue(self.posts[-1][1]["sessions"][0]["running"])

    def test_429_waits_and_keeps_the_newest_state(self):
        self.r.live_event({"session_id": "s1", "event": "tool"})
        self.responses = [(429, 30.0)]
        self.flush()
        self.r.live_event({"session_id": "s1", "event": "permission"})
        self.t += 29
        self.assertEqual(self.flush(), 0)
        self.assertEqual(len(self.posts), 1)
        self.t += 1
        self.assertEqual(self.flush(), 1)
        self.assertTrue(self.posts[-1][1]["sessions"][0]["waiting_permission"])

    def test_429_without_retry_after_waits_longer_each_time(self):
        self.r.live_event({"session_id": "s1", "event": "tool"})
        self.responses = [(429, None), (429, None), (200, None)]
        self.flush()
        waits = []
        for _ in range(2):
            start = self.t
            while len(self.posts) < len(waits) + 2:
                self.t += 1
                self.flush()
            waits.append(self.t - start)
        self.assertEqual(waits, [st.LIVE_BACKOFF_S, st.LIVE_BACKOFF_S * 2])
        self.assertEqual(self.r._live.dirty(), [])

    def test_a_failed_send_is_tried_again(self):
        self.r.live_event({"session_id": "s1", "event": "tool"})
        self.responses = [(0, None)]
        self.flush()
        self.assertEqual(self.r._live.dirty(), ["s1"])
        self.t += st.LIVE_INTERVAL_S
        self.assertEqual(self.flush(), 1)

    def test_a_router_without_the_route_turns_live_off_not_facts(self):
        self.r.live_event({"session_id": "s1", "event": "tool"})
        self.responses = [(404, None)]
        self.flush()
        self.assertTrue(self.r._live_off)
        self.t += 60
        self.r.live_event({"session_id": "s1", "event": "permission"})
        self.assertEqual(self.flush(), 0)
        self.assertEqual(len(self.posts), 1)
        # The facts path is unchanged.
        self.r.notify({"session_id": "s1", "event": "stop", "cwd": "/w/app"})
        self.assertIn("s1", self.r._pending)

    def test_at_most_50_per_call(self):
        for i in range(120):
            sid = f"x{i}"
            self.r._state["sessions"][sid] = {"tracker_id": f"t{i}", "agent": "jenkins", "origin": "society_ai"}
            self.r.live_event({"session_id": sid, "event": "tool"})
        self.assertEqual(self.flush(), 120)
        self.assertEqual([len(b["sessions"]) for _, b in self.posts], [50, 50, 20])

    def test_sessions_the_owner_does_not_track_are_not_sent(self):
        st_ = self.r._state["sessions"]
        st_["s1"]["cwd"] = os.path.expanduser("~/Coding/client-x/sub")
        self.r._configs["jenkins"][1]["skip_folders"] = ["~/Coding/client-x"]
        st_["s2"]["reason"] = "stopped"
        self.r._configs["kilo"][1]["tracking"] = False
        for sid in ("s1", "s2", "s3"):
            self.r.live_event({"session_id": sid, "event": "tool"})
        self.assertEqual(self.flush(), 0)
        self.assertEqual(self.posts, [])
        self.r._configs["kilo"][1].update(tracking=True, claude_code_sessions=False)
        self.assertEqual(self.flush(), 0)

    def test_a_session_registered_later_is_sent_then(self):
        self.r.live_event({"session_id": "s9", "event": "tool"})
        self.assertEqual(self.flush(), 0)
        self.r._state["sessions"]["s9"] = {"tracker_id": "t9", "agent": "jenkins", "origin": "society_ai"}
        self.assertEqual(self.flush(), 1)

    def test_a_long_running_turn_is_sent_again(self):
        self.r.notify({"session_id": "s2", "event": "prompt"})
        self.flush()
        self.t += sl.HEARTBEAT_S
        self.assertEqual(self.flush(), 1)
        self.assertTrue(self.posts[-1][1]["sessions"][0]["running"])

    def test_only_the_runner_takes_events(self):
        self.r._lock_fd = None
        self.assertFalse(self.r.live_event({"session_id": "s1", "event": "tool"}))
        self.assertEqual(self.r._live.sessions, {})


class PostLive(unittest.TestCase):
    def test_posts_as_the_tracker_agent_and_reads_retry_after(self):
        import httpx
        seen = []

        def handler(request):
            seen.append((request.method, request.url.path, request.headers["Authorization"],
                         json.loads(request.content)))
            if len(seen) == 1:
                return httpx.Response(200, json={"updated": 1})
            return httpx.Response(429, headers={"Retry-After": "17"}, json={"detail": "slow down"})

        with tempfile.TemporaryDirectory() as d, mock.patch.object(st, "BASE_DIR", pathlib.Path(d)):
            r = st.SessionTrackerRunner("jenkins", "sai_jenkins", "https://api.test")
            r._identities = {"jenkins": {"api_url": "https://api.test", "agent_name": "t", "token": "trk_a"}}
            r._client = httpx.AsyncClient(base_url="https://api.test", transport=httpx.MockTransport(handler))
            body = {"host": "m", "sessions": []}
            self.assertEqual(asyncio.run(r._post_live("jenkins", body)), (200, None))
            self.assertEqual(asyncio.run(r._post_live("jenkins", body)), (429, 17.0))
        self.assertEqual(seen[0][:3], ("POST", "/api/v1/session-trackers/live", "Bearer trk_a"))
        self.assertEqual(seen[0][3], body)


class Registration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = pathlib.Path(self.tmp.name)
        self.settings = d / "settings.json"
        self.runner_dir = pathlib.Path(ch.__file__).resolve().parent
        venv = d / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("")
        self.py = str(venv / "python")
        self.patches = [
            mock.patch.object(ch, "SETTINGS_PATH", str(self.settings)),
            mock.patch.object(ch, "_paths", return_value=(self.py, str(self.runner_dir))),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def read(self):
        return json.loads(self.settings.read_text())

    def ours(self, hooks, event, script):
        return [e for e in hooks.get(event, []) if ch._entry_owns_script(e, script)]

    def test_setup_registers_live_hooks_idempotently(self):
        foreign = {"hooks": [{"type": "command", "command": "/usr/bin/true"}]}
        self.settings.write_text(json.dumps({"hooks": {"PreToolUse": [foreign]}, "model": "opus"}))
        with mock.patch("builtins.print"):
            ch.main()
            ch.main()
        data = self.read()
        hooks = data["hooks"]
        for event in ("SessionStart", "Stop", "UserPromptSubmit", "PreToolUse", "Notification"):
            self.assertEqual(len([e for e in hooks[event]
                                  if "hook_" in e["hooks"][0]["command"]]), 1, event)
        for event in ("PreToolUse", "Notification"):
            cmd = self.ours(hooks, event, "hook_live.py")[0]["hooks"][0]["command"]
            self.assertEqual(cmd, f"{self.py} {self.runner_dir / 'hook_live.py'}")
        self.assertIn(foreign, hooks["PreToolUse"])
        self.assertEqual(data["model"], "opus")

    def test_bridge_adds_live_hooks_where_ours_are_installed(self):
        stop = {"hooks": [{"type": "command", "command": f"{self.py} /old/hook_stop.py", "timeout": 5}]}
        self.settings.write_text(json.dumps({"hooks": {"Stop": [stop]}}))
        self.assertTrue(ch.ensure_live_hooks())
        hooks = self.read()["hooks"]
        self.assertEqual(len(self.ours(hooks, "PreToolUse", "hook_live.py")), 1)
        self.assertEqual(len(self.ours(hooks, "Notification", "hook_live.py")), 1)
        self.assertEqual(hooks["Stop"], [stop])
        self.assertFalse(ch.ensure_live_hooks())  # already there

    def test_bridge_leaves_settings_without_our_hooks_alone(self):
        self.assertFalse(ch.ensure_live_hooks())
        self.assertFalse(self.settings.exists())
        self.settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "mine.sh"}]}]}}))
        self.assertFalse(ch.ensure_live_hooks())
        self.assertNotIn("PreToolUse", self.read()["hooks"])

    def test_reply_path_settings_carry_live_hooks(self):
        from agents.claude_code.runner.session_manager import SessionManager
        hooks = SessionManager._reply_hook_settings()["hooks"]
        for event in ("PreToolUse", "Notification"):
            self.assertIn("hook_live.py", hooks[event][0]["hooks"][0]["command"])
        self.assertIn("--mirror-only", hooks["Stop"][0]["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
