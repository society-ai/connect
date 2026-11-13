"""Session Tracker runner (session_tracker.py).

- Only the conversation is read: user prompts and agent text; tool calls,
  tool results, thinking, sub-agents and system reminders never are.
- The checklist is replayed from TaskCreate / TaskUpdate (and TodoWrite).
- Turns of one session coalesce into one job; the stop event wins.
- A job sends the machine's facts only (§16.5): registers a new session
  (bridge vs direct origin), keeps the cursor only on success, forgets a
  deleted session on 404. A session the platform does not track (setting,
  skipped folder, stopped) is not asked about again on every turn.
- session_start (D37): the first message of a direct session registers it and
  answers the banner facts; only the machine's runner answers.
- The server feed (§16.4): jobs are claimed once per account that runs its
  Trackers here, run with the job's own prompt and schema, and the output is
  sent back with the lease and cursor.
- Several accounts on one machine: each session is written as the Tracker
  agent of its own agent's owner, under that owner's setting; a refused
  session is tried again after a restart.
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

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.claude_code.runner import session_tracker as st  # noqa: E402


def rec(kind, content, ts="2026-10-27T10:00:00Z", **extra):
    return {"type": kind, "timestamp": ts, "message": {"content": content}, **extra}


TRANSCRIPT = [
    rec("user", "Design the tracker please"),
    rec("assistant", [{"type": "thinking", "thinking": "secret"}, {"type": "text", "text": "Sure, plan:"}]),
    rec("assistant", [{"type": "tool_use", "name": "TaskCreate", "input": {"subject": "Write schema"}}]),
    rec("user", [{"type": "tool_result", "content": "Task #1 created successfully"}]),
    rec("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "rm -rf /tmp/x"}}]),
    rec("assistant", [{"type": "tool_use", "name": "TaskCreate", "input": {"subject": "Write routes"}}]),
    rec("assistant", [{"type": "tool_use", "name": "TaskUpdate", "input": {"taskId": "1", "status": "completed"}}]),
    rec("user", "<system-reminder>ignore me</system-reminder>"),
    rec("assistant", [{"type": "text", "text": "Sub-agent text"}], isSidechain=True),
    rec("assistant", [{"type": "text", "text": "Schema done. Want me to commit?"}], ts="2026-10-27T10:05:00Z"),
]


def write_transcript(path: pathlib.Path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


class TestReading(unittest.TestCase):
    def test_conversation_only(self):
        seg = st.extract_segment(TRANSCRIPT)
        text = "\n".join(t for _, t in seg.turns)
        self.assertIn("Design the tracker please", text)
        self.assertIn("Want me to commit?", text)
        for leaked in ("secret", "rm -rf", "Task #1", "ignore me", "Sub-agent"):
            self.assertNotIn(leaked, text)
        self.assertEqual(seg.user_turns, 1)
        self.assertEqual(seg.last_agent_at, "2026-10-27T10:05:00Z")
        self.assertEqual(seg.first_prompt, "Design the tracker please")

    def test_agent_texts_of_one_turn_merge(self):
        seg = st.extract_segment(TRANSCRIPT)
        self.assertEqual([who for who, _ in seg.turns], ["USER", "AGENT"])

    def test_checklist_replay(self):
        tasks = st.apply_tasks(TRANSCRIPT, {})
        self.assertEqual(tasks, {
            "1": {"text": "Write schema", "state": "done"},
            "2": {"text": "Write routes", "state": "todo"},
        })
        more = [rec("assistant", [{"type": "tool_use", "name": "TaskCreate", "input": {"subject": "Tests"}}])]
        self.assertIn("3", st.apply_tasks(more, dict(tasks)))

    def test_read_records_stops_at_last_complete_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "t.jsonl"
            p.write_text(json.dumps(rec("user", "a")) + "\n" + '{"type": "user", "mess')
            records, end = st.read_records(p, 0)
            self.assertEqual(len(records), 1)
            self.assertEqual(end, len(json.dumps(rec("user", "a"))) + 1)


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.patches = [
            mock.patch.object(st, "BASE_DIR", base / "tracker"),
            mock.patch.object(st, "CLAUDE_SESSIONS_DIR", base / "claude-sessions"),
            mock.patch.object(st, "SOCIETY_CACHE", base / "cache"),
        ]
        for p in self.patches:
            p.start()
        (base / "tracker" / "runs").mkdir(parents=True)
        (base / "claude-sessions").mkdir()
        (base / "cache").mkdir()
        self.transcript = base / "s1.jsonl"
        write_transcript(self.transcript, TRANSCRIPT)
        self.runner = self.new_runner()
        self.calls = []
        self.responses = []

    def new_runner(self):
        runner = st.SessionTrackerRunner("jenkins", "sai_persona", "https://api.test", default_agent="jenkins")
        runner._lock_fd = 99
        config = {"tracking": True, "claude_code_sessions": True, "runs_trackers": True, "skip_folders": []}
        runner._configs = {"jenkins": (time.monotonic(), config), "searchpro": (time.monotonic(), dict(config))}

        async def fake_call(agent, method, path, body=None):
            self.calls.append((method, path, body, agent))
            return self.responses.pop(0)

        runner._call = fake_call
        runner._run_model = mock.AsyncMock(return_value={
            "set": {"status": "waiting_on_you"},
            "add_items": [{"kind": "approval", "text": "Commit the schema"}],
        })
        return runner

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def job(self, event="stop"):
        return {"event": event, "cwd": "", "transcript_path": str(self.transcript), "due": 0}

    def run_job(self, event="stop"):
        asyncio.run(self.runner._process("s1", self.job(event)))

    def test_events_coalesce_and_stop_wins(self):
        self.runner.notify({"session_id": "s1", "event": "stop", "cwd": "/r"})
        self.runner.notify({"session_id": "s1", "event": "prompt", "cwd": "/r"})
        self.assertEqual(self.runner._pending["s1"]["event"], "stop")
        self.runner.notify({"session_id": "s2", "cwd": str(st.BASE_DIR / "runs")})
        self.assertNotIn("s2", self.runner._pending)

    def test_new_direct_session_registers_then_sends_facts(self):
        tracker = {"id": "t1", "version": 0, "origin": "direct", "chatId": "c1"}
        self.responses = [
            (201, {"tracker": tracker, "chat_url": "https://app/chat/c1"}),  # register
            (200, {"tracker": tracker}),                                      # facts
        ]
        self.run_job()
        method, path, body, agent = self.calls[0]
        self.assertEqual(path, "/api/v1/session-trackers/register")
        self.assertEqual(body["origin"], "direct")
        self.assertEqual(body["agent_name"], "jenkins")
        self.assertEqual(body["title"], "Design the tracker please")
        self.assertEqual(body["home"], os.path.expanduser("~"))
        self.assertEqual(agent, "jenkins")
        _, ppath, patch, _ = self.calls[1]
        self.assertEqual(ppath, "/api/v1/session-trackers/t1")
        # Facts only: the summary comes from the server feed.
        self.assertEqual(set(patch), {"facts", "no_change"})
        self.assertEqual(patch["facts"]["steps"][0], {"text": "Write schema", "state": "done"})
        self.assertFalse(patch["facts"]["running"])
        self.runner._run_model.assert_not_awaited()
        state = self.runner._state["sessions"]["s1"]
        self.assertEqual(state["tracker_id"], "t1")
        self.assertTrue(state["mirrored"])
        self.assertEqual(state["chat_url"], "https://app/chat/c1")
        self.assertEqual(state["cursor"], self.transcript.stat().st_size)

    def bind(self, agent):
        """The running session (this process stands in for Claude Code) bound to `agent`."""
        pid = os.getpid()
        (st.CLAUDE_SESSIONS_DIR / f"{pid}.json").write_text(json.dumps({"sessionId": "s1", "pid": pid}))
        (st.SOCIETY_CACHE / "session-binding").mkdir(exist_ok=True)
        (st.SOCIETY_CACHE / "session-binding" / f"{pid}.json").write_text(json.dumps({"agent": agent}))

    def test_a_direct_session_follows_the_agent_it_switches_to(self):
        # The first message ("act as searchpro") registered it as the agent bound then.
        self.bind("jenkins")
        tracker = {"id": "t1", "version": 0, "origin": "direct", "chatId": "c1", "agentName": "jenkins"}
        self.responses = [(201, {"tracker": tracker, "chat_url": "https://app/chat/c1"}), (200, {"tracker": tracker})]
        self.run_job()
        self.assertEqual(self.runner._state["sessions"]["s1"]["agent"], "jenkins")

        # The switch: the next turn registers it again as the new agent, which moves it.
        self.bind("searchpro")
        self.calls.clear()
        moved = {**tracker, "agentName": "searchpro"}
        self.responses = [(200, {"tracker": moved}), (200, {"tracker": moved})]
        self.run_job()
        method, path, body, agent = self.calls[0]
        self.assertEqual((method, path, agent), ("POST", "/api/v1/session-trackers/register", "searchpro"))
        self.assertEqual((body["origin"], body["agent_name"], body["external_session_id"]), ("direct", "searchpro", "s1"))
        self.assertNotIn("title", body)
        self.assertEqual(self.calls[1][3], "searchpro")  # the facts go as the new agent
        self.assertEqual(self.runner._state["sessions"]["s1"]["agent"], "searchpro")

        # Unchanged binding: no new registration.
        self.calls.clear()
        self.responses = [(200, {"tracker": moved})]
        self.run_job()
        self.assertEqual([c[1] for c in self.calls], ["/api/v1/session-trackers/t1"])

    def test_a_refused_move_keeps_the_session_where_it_was(self):
        self.bind("jenkins")
        tracker = {"id": "t1", "version": 0, "origin": "direct", "chatId": "c1", "agentName": "jenkins"}
        self.responses = [(201, {"tracker": tracker}), (200, {"tracker": tracker})]
        self.run_job()
        self.bind("searchpro")
        self.calls.clear()
        self.responses = [(403, {"detail": "no"}), (200, {"tracker": tracker})]
        self.run_job()
        self.assertEqual(self.runner._state["sessions"]["s1"]["agent"], "jenkins")
        self.assertNotIn("s1", self.runner._refused)
        self.assertEqual(self.calls[1][3], "jenkins")

    def test_a_session_the_platform_does_not_track_is_asked_again_later(self):
        self.responses = [(200, {"tracked": False, "reason": "folder_skipped", "folder": "~/client"})]
        self.run_job()
        state = self.runner._state["sessions"]["s1"]
        self.assertEqual(state["reason"], "folder_skipped")
        self.assertFalse(state["mirrored"])
        self.calls.clear()
        self.run_job()
        self.assertEqual(self.calls, [])
        state["checked_at"] = time.time() - st.RECHECK_S - 1
        self.responses = [(200, {"tracked": False, "reason": "off"})]
        self.run_job()
        self.assertEqual(self.calls[0][1], "/api/v1/session-trackers/register")

    def test_a_stopped_session_sends_nothing(self):
        self.runner._state["sessions"]["s1"] = {"cursor": 0, "tasks": {}, "agent": "jenkins", "reason": "stopped",
                                                "checked_at": 0}
        self.run_job()
        self.assertEqual(self.calls, [])

    def searchpro_session(self):
        (st.SOCIETY_CACHE / "searchpro").mkdir()
        (st.SOCIETY_CACHE / "searchpro" / "sessions.json").write_text(
            json.dumps({"sessions": [{"session_id": "s1", "persona": "searchpro"}]})
        )

    def test_bridge_session_is_written_as_its_personas_owner(self):
        self.searchpro_session()
        self.responses = [(201, {"tracker": {"id": "t1", "version": 0, "origin": "society_ai"}}), (200, {})]
        self.run_job()
        self.assertEqual(self.calls[0][2]["origin"], "society_ai")
        self.assertEqual(self.calls[0][2]["agent_name"], "searchpro")
        # Every call of this session goes as searchpro's owner's Tracker agent.
        self.assertEqual({c[3] for c in self.calls}, {"searchpro"})
        self.assertEqual(self.runner._state["sessions"]["s1"]["agent"], "searchpro")
        # Mirrored by the bridge that started it, not adopted as a direct session.
        self.assertFalse(self.runner._state["sessions"]["s1"]["mirrored"])

    def test_each_accounts_own_setting_decides(self):
        self.searchpro_session()
        self.runner._configs["searchpro"] = (time.monotonic(), {"tracking": False})
        self.run_job()
        self.assertEqual(self.calls, [])
        self.assertNotIn("tracker_id", self.runner._state["sessions"]["s1"])

    def test_prompt_event_sends_running(self):
        self.runner._state["sessions"]["s1"] = {"tracker_id": "t1", "cursor": 0, "tasks": {}, "agent": "jenkins"}
        self.responses = [(200, {})]
        self.run_job("prompt")
        patch = self.calls[0][2]
        self.assertTrue(patch["no_change"])
        self.assertTrue(patch["facts"]["running"])

    def test_a_failed_facts_patch_keeps_the_cursor(self):
        self.runner._state["sessions"]["s1"] = {"tracker_id": "t1", "cursor": 0, "tasks": {}, "agent": "jenkins"}
        self.responses = [(500, {})]
        self.run_job()
        self.assertEqual(self.runner._state["sessions"]["s1"]["cursor"], 0)

    def test_deleted_session_is_forgotten(self):
        self.runner._state["sessions"]["s1"] = {"tracker_id": "t1", "cursor": 0, "tasks": {}, "agent": "jenkins"}
        self.responses = [(404, {})]
        self.run_job()
        self.assertNotIn("s1", self.runner._state["sessions"])

    def test_a_refused_session_is_tried_again_after_a_restart(self):
        self.responses = [(403, {})]
        self.run_job()
        self.calls.clear()
        self.run_job()
        self.assertEqual(self.calls, [])
        self.runner._save_state()
        self.runner = self.new_runner()
        self.runner._load_state()
        self.responses = [(201, {"tracker": {"id": "t1", "version": 0, "origin": "direct"}}), (200, {})]
        self.run_job()
        self.assertEqual(self.calls[0][1], "/api/v1/session-trackers/register")
        self.assertEqual(self.runner._state["sessions"]["s1"]["tracker_id"], "t1")

    # -- session_start (D37) --

    def start(self, **params):
        return asyncio.run(self.runner.session_start({
            "session_id": "s1", "cwd": "/Users/me/app", "prompt": "Fix the login bug\nplease", **params,
        }))

    def test_first_message_registers_and_answers_the_banner(self):
        tracker = {"id": "t1", "origin": "direct", "chatId": "c1"}
        self.responses = [(201, {"tracker": tracker, "chat_url": "https://app/chat/c1"})]
        with mock.patch.object(self.runner, "_agent_token", return_value="sai_persona"):
            answer = self.start()
        self.assertEqual(answer, {"mirrored": True, "chat_url": "https://app/chat/c1", "reason": None, "folder": None})
        body = self.calls[0][2]
        self.assertEqual(body["title"], "Fix the login bug")
        self.assertEqual(body["cwd"], "/Users/me/app")
        saved = json.loads((st.BASE_DIR / "state.json").read_text())["sessions"]["s1"]
        # The agent's own bridge reads this to mirror the session.
        self.assertTrue(saved["mirrored"])
        self.assertEqual(saved["agent"], "jenkins")
        self.assertEqual(saved["chat_id"], "c1")

    def test_first_message_says_why_not(self):
        self.responses = [(200, {"tracked": False, "reason": "stopped"})]
        with mock.patch.object(self.runner, "_agent_token", return_value="sai_persona"):
            answer = self.start()
        self.assertEqual(answer["mirrored"], False)
        self.assertEqual(answer["reason"], "stopped")

    def test_only_the_runner_answers_and_bridge_sessions_are_left_alone(self):
        self.runner._lock_fd = None
        self.assertIsNone(self.start())
        self.runner._lock_fd = 99
        self.searchpro_session()
        self.assertEqual(self.start(), {"mirrored": False})
        self.assertEqual(self.calls, [])

    # -- the server feed (§16.4) --

    def test_feed_claims_once_per_account_and_sends_outputs_back(self):
        job = {"tracker_id": "t9", "lease": "l" * 32, "cursor": {"seq": 7}, "base_version": 2,
               "short_ids": {"q1": "item-1"}, "input": "CURRENT TRACKER ...", "model": "haiku",
               "prompt": "p", "output_schema": {}}
        self.runner._identities = {"jenkins": {"agent_name": "session-tracker-a"},
                                   "searchpro": {"agent_name": "session-tracker-a"}}
        self.responses = [(200, {"jobs": [job]}), (200, {"status": "applied"})]
        with mock.patch.object(self.runner, "_machine_agents", return_value=["jenkins", "searchpro"]), \
                mock.patch.object(self.runner, "_tracker_token", mock.AsyncMock(return_value="trk")):
            done = asyncio.run(self.runner._feed_once())
        self.assertEqual(done, 1)
        claims = [c for c in self.calls if c[1].endswith("/jobs/claim")]
        self.assertEqual(len(claims), 1)  # one account, asked once
        self.assertEqual(claims[0][2], {"runtime": "claude_code", "host": self.runner._host, "max": st.FEED_MAX_JOBS})
        self.runner._run_model.assert_awaited_once_with(job, "CURRENT TRACKER ...")
        _, rpath, result, _ = self.calls[1]
        self.assertEqual(rpath, "/api/v1/session-trackers/jobs/t9/result")
        self.assertEqual(result["lease"], "l" * 32)
        self.assertEqual(result["cursor"], {"seq": 7})
        self.assertEqual(result["base_version"], 2)
        self.assertEqual(result["short_ids"], {"q1": "item-1"})
        self.assertEqual(result["output"]["set"], {"status": "waiting_on_you"})

    def test_feed_skips_accounts_that_run_trackers_elsewhere_and_failed_runs(self):
        self.runner._configs["searchpro"] = (time.monotonic(), {"tracking": True, "runs_trackers": False})
        self.runner._identities = {"jenkins": {"agent_name": "session-tracker-a"}}
        self.runner._run_model = mock.AsyncMock(return_value=None)
        self.responses = [(200, {"jobs": [{"tracker_id": "t9", "lease": "x", "cursor": {"seq": 1}, "input": "i"}]})]
        with mock.patch.object(self.runner, "_machine_agents", return_value=["jenkins", "searchpro"]), \
                mock.patch.object(self.runner, "_tracker_token", mock.AsyncMock(return_value="trk")):
            done = asyncio.run(self.runner._feed_once())
        self.assertEqual(done, 0)
        # Claimed for jenkins only; the failed run sends nothing (its lease runs out).
        self.assertEqual([c[1] for c in self.calls], ["/api/v1/session-trackers/jobs/claim"])

    def test_a_turn_end_asks_for_jobs_soon(self):
        self.runner._feed_due = time.monotonic() + 60
        self.runner.notify({"session_id": "s1", "event": "stop", "cwd": "/r"})
        self.assertLess(self.runner._feed_due, time.monotonic() + st.FEED_AFTER_TURN_S + 1)


class TestTrackerAgents(unittest.TestCase):
    """One Tracker agent token per agent, asked for with that agent's own token."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(st, "BASE_DIR", pathlib.Path(self.tmp.name))
        self.patch.start()
        self.requests = []
        self.revoked = set()

        def handler(request):
            auth = request.headers["Authorization"].removeprefix("Bearer ")
            self.requests.append((request.method, request.url.path, auth))
            if request.url.path == "/api/v1/session-trackers/agent":
                owner = {"sai_jenkins": "a", "sai_searchpro": "b"}[auth]
                return httpx.Response(200, json={"agent_name": f"session-tracker-{owner}",
                                                 "token": f"trk_{owner}_{len(self.requests)}"})
            if auth in self.revoked:
                return httpx.Response(401, json={})
            return httpx.Response(200, json={"ok": auth})

        self.runner = st.SessionTrackerRunner("jenkins", "sai_jenkins", "https://api.test")
        self.runner._client = httpx.AsyncClient(base_url="https://api.test",
                                                transport=httpx.MockTransport(handler))
        personas = [{"name": "jenkins", "token": "sai_jenkins", "api_url": "https://api.test"},
                    {"name": "searchpro", "token": "sai_searchpro", "api_url": "https://api.test/"}]
        self.personas = mock.patch("core.hook_common.discover_personas", return_value=personas)
        self.personas.start()

    def tearDown(self):
        self.personas.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def call(self, agent):
        return asyncio.run(self.runner._call(agent, "GET", "/api/v1/session-trackers/t1"))

    def test_each_agent_gets_its_owners_tracker_agent(self):
        self.assertEqual(self.call("jenkins")[1], {"ok": "trk_a_1"})
        self.assertEqual(self.call("searchpro")[1], {"ok": "trk_b_3"})
        self.call("searchpro")
        minted = [r[2] for r in self.requests if r[1].endswith("/agent")]
        self.assertEqual(minted, ["sai_jenkins", "sai_searchpro"])
        saved = json.loads((st.BASE_DIR / "identities.json").read_text())
        self.assertEqual(saved["searchpro"]["agent_name"], "session-tracker-b")
        self.assertEqual(os.stat(st.BASE_DIR / "identities.json").st_mode & 0o777, 0o600)

    def test_a_revoked_token_is_replaced_for_that_agent_only(self):
        self.call("jenkins")
        self.call("searchpro")
        self.revoked.add("trk_b_3")
        self.assertEqual(self.call("searchpro")[0], 401)
        self.assertNotIn("searchpro", self.runner._identities)
        self.assertIn("jenkins", self.runner._identities)
        self.assertEqual(self.call("searchpro")[0], 200)

    def test_an_agent_not_on_this_machine_is_not_tracked(self):
        self.assertEqual(self.call("someone-else"), (0, {}))
        self.assertEqual(asyncio.run(self.runner._config_for("someone-else")), None)
        self.assertEqual(self.requests, [])


class TestHookGuard(unittest.TestCase):
    def test_tracker_runs_never_notify(self):
        from agents.claude_code.runner import hook_stop
        with mock.patch.dict(os.environ, {"SOCIETY_AI_TRACKER": "1"}), \
                mock.patch.object(hook_stop, "_bridge_sockets") as socks:
            hook_stop._notify_mirrors({"session_id": "s1"})
        socks.assert_not_called()


class BridgeSockets(unittest.TestCase):
    def test_the_sessions_own_bridge_is_reached_wherever_it_runs(self):
        # A bridge run from another folder (a dev or test checkout) launches its sessions with its own
        # SOCIETY_AI_BRIDGE_SOCKET: the Stop hook must reach it, not only the installed checkout's.
        from agents.claude_code.runner import hook_stop
        with tempfile.TemporaryDirectory() as d:
            sock = os.path.join(d, "bridge.sock")
            pathlib.Path(sock).touch()
            with mock.patch.dict(os.environ, {"SOCIETY_AI_BRIDGE_SOCKET": sock}), \
                    mock.patch.object(hook_stop, "REPO_DIR", pathlib.Path(d) / "no-checkout"):
                self.assertEqual(hook_stop._bridge_sockets()[0], sock)  # first; the machine's own may follow


if __name__ == "__main__":
    unittest.main()
