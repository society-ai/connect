"""The owner's answer to something an agent asked (its inbox item, or an
action it was told to ask about first) continues the session that asked.

- The router names the chat the agent asked from as the dispatch's chat_id;
  only those answers use it (inbox_answer_chat).
- A known session for that chat takes the answer, even when the item is
  linked to a task (a task session has its chat aliased to it).
- A session the owner started in Claude Code gets it through the relay.
- The answered session gets a Remote Control entry: in place when it is
  running (/remote-control), at launch when it resumes.
- Such a session's MCP server names its chat, so what it asks carries it.

Run: ./venv/bin/python -m unittest discover tests
"""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import bridge, chat_context  # noqa: E402
from core.contacts import OWNER, Sender  # noqa: E402
from agents.claude_code.runner.session_manager import (  # noqa: E402
    PersonaPolicy,
    SessionManager,
    SessionRecord,
)

OWNER_ID = "11111111-1111-1111-1111-111111111111"
CHAT = "0a0de5a7-075d-4f7f-858d-6b815943fce0"
OTHER_CHAT = "35321dc2-9f8b-4db3-b422-675444e19fc6"
TASK = "81059c7a-16ea-45d6-b84a-4124a7a136d6"


def run(coro):
    return asyncio.run(coro)


def answer_meta(reason="inbox_responded", chat=CHAT, **extra):
    return {
        "user_id": OWNER_ID,
        "trigger_reason": reason,
        "chat_id": chat,
        "frame": {"cause": "inbox", "presence": "background", "from": {"kind": "owner"}},
        **extra,
    }


class InboxAnswerChat(unittest.TestCase):
    def frame(self, cause="inbox"):
        return {"cause": cause, "from": {"kind": "owner"}}

    def test_answers_name_the_chat_they_go_back_to(self):
        for reason in ("inbox_responded", "held_action"):
            meta = {"trigger_reason": reason, "chat_id": CHAT}
            self.assertEqual(bridge.inbox_answer_chat(OWNER, self.frame(), meta), CHAT)

    def test_nothing_else_does(self):
        meta = {"trigger_reason": "inbox_responded", "chat_id": CHAT}
        # A new item for the agent is not an answer to it.
        self.assertEqual(bridge.inbox_answer_chat(
            OWNER, self.frame(), {"trigger_reason": "inbox_received", "chat_id": CHAT}), "")
        self.assertEqual(bridge.inbox_answer_chat(OWNER, self.frame("message"), meta), "")
        self.assertEqual(bridge.inbox_answer_chat(OWNER, None, meta), "")
        contact = Sender(is_owner=False, key="agent:nova", label="nova", permission="chat")
        self.assertEqual(bridge.inbox_answer_chat(contact, self.frame(), meta), "")
        updates = Sender(is_owner=True, key="owner", label="owner", permission=None, session="updates")
        self.assertEqual(bridge.inbox_answer_chat(updates, self.frame(), meta), "")
        self.assertEqual(bridge.inbox_answer_chat(
            OWNER, self.frame(), {"trigger_reason": "inbox_responded", "chat_id": "not-a-chat"}), "")


class Routing(unittest.TestCase):
    """handle_task_execute picks the session an answer lands in."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        ctx = bridge.AgentContext(
            name="jenkins", token="sai_x", work_dir=d, extra_dirs=[], company_id="",
            api_url="https://api.x", socket=f"{d}/b.sock", state_dir=d,
        )
        self.b = bridge.Bridge(ctx)
        self.b._session_mode = True
        self.b._runner = None
        self.b._owner_user_id = OWNER_ID
        self.b._session_mgr = SessionManager(f"{d}/channels.sock")
        self.calls = []

        async def execute(task_id, key, title, content, **kw):
            self.calls.append((key, kw))

        self.b._execute_via_session = execute

    def tearDown(self):
        self._tmp.cleanup()

    def dispatch(self, metadata, session_id=None):
        params = {
            "id": "task-a2a-1",
            "sessionId": session_id or metadata.get("chat_id"),
            "message": {"parts": [{"type": "text", "text": "Your inbox item was approved."}]},
            "metadata": metadata,
        }
        run(self.b.handle_task_execute("m1", params))
        return self.calls[-1]

    def test_the_session_that_asked_takes_the_answer_even_for_a_task(self):
        mgr = self.b._session_mgr
        mgr._sessions[TASK] = SessionRecord(
            work_item_key=TASK, persona="jenkins", session_id="s-1", tmux_name="t", title="Task work",
        )
        mgr.alias(CHAT, TASK)
        key, kw = self.dispatch(answer_meta(agent_task_id=TASK))
        self.assertEqual(key, CHAT)
        self.assertEqual(mgr.resolve(key), TASK)
        self.assertEqual(kw["inbox_chat"], CHAT)

    def test_held_action_outcomes_go_back_the_same_way(self):
        mgr = self.b._session_mgr
        mgr._sessions[CHAT] = SessionRecord(
            work_item_key=CHAT, persona="jenkins", session_id="s-2", tmux_name="t", title="Chat",
        )
        key, kw = self.dispatch(answer_meta(reason="held_action", agent_task_id=TASK))
        self.assertEqual((key, kw["inbox_chat"]), (CHAT, CHAT))

    def test_an_unknown_chat_keeps_the_usual_order(self):
        key, kw = self.dispatch(answer_meta(agent_task_id=TASK))
        self.assertEqual(key, TASK)
        key, kw = self.dispatch(answer_meta())
        self.assertEqual(key, CHAT)

    def test_other_dispatches_carry_no_answer_chat(self):
        meta = answer_meta(reason="inbox_received")
        key, kw = self.dispatch(meta, session_id=OTHER_CHAT)
        self.assertEqual(kw["inbox_chat"], "")


class DirectSessions(unittest.TestCase):
    """An answer to a session the owner started in Claude Code goes through the relay."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        d = self._tmp.name
        ctx = bridge.AgentContext(
            name="jenkins", token="sai_x", work_dir=d, extra_dirs=[], company_id="",
            api_url="https://api.x", socket=f"{d}/b.sock", state_dir=d,
        )
        self.b = bridge.Bridge(ctx)
        self.b._session_mgr = mock.Mock()
        self.b._session_mgr.ensure_session = mock.AsyncMock(side_effect=AssertionError("no launch"))
        self.relayed = []

        async def reply(task_id, session, content, delivery_chat_id, timeout_s, had_files):
            self.relayed.append((session, content, delivery_chat_id))

        self.b._reply_into_direct_session = reply

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_answer_is_relayed_into_it(self):
        direct = object()
        with mock.patch("agents.claude_code.runner.direct_reply.direct_session_for_chat",
                        side_effect=lambda chat: direct if chat == CHAT else None) as find:
            run(self.b._execute_via_session(
                "t1", "fresh-key", "title", "Your inbox item was approved.", kind="trigger",
                sender=OWNER, inbox_chat=CHAT,
            ))
        find.assert_called_once_with(CHAT)
        self.assertEqual(self.relayed, [(direct, "Your inbox item was approved.", None)])

    def test_other_automation_never_reaches_it(self):
        with mock.patch("agents.claude_code.runner.direct_reply.direct_session_for_chat") as find:
            self.b._session_mgr.ensure_session = mock.AsyncMock(side_effect=RuntimeError("stop"))
            self.b._report_delivery = mock.AsyncMock()
            self.b._send_task_complete = mock.AsyncMock()
            run(self.b._execute_via_session("t1", CHAT, "title", "wake", kind="trigger", sender=OWNER))
        find.assert_not_called()
        self.assertEqual(self.relayed, [])


class RemoteControl(unittest.TestCase):
    """The answered session shows in Claude Code (Remote Control)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mgr = SessionManager(str(pathlib.Path(self._tmp.name) / "channels.sock"))
        self.mgr._enforce_concurrency = mock.AsyncMock()
        self.sent = []

        async def send(name, keys, *, enter=False, literal=False):
            self.sent.append((name, keys, enter, literal))

        self.mgr._tmux_send = send

    def tearDown(self):
        self._tmp.cleanup()

    def headless(self, alive):
        rec = SessionRecord(
            work_item_key=CHAT, persona="jenkins", session_id="s-1", tmux_name="sai-jenkins-x",
            title="Jenkins: Release\nmigration 0014", background=True, has_run_once=True,
        )
        rec.state = "ready"
        self.mgr._sessions[CHAT] = rec
        self.mgr._tmux_alive = mock.AsyncMock(return_value=alive)
        return rec

    def test_a_running_session_turns_it_on_in_place(self):
        rec = self.headless(alive=True)
        self.mgr._launch = mock.AsyncMock(side_effect=AssertionError("no relaunch"))
        run(self.mgr.ensure_session(CHAT, "jenkins", background=False))
        self.assertFalse(rec.background)
        self.assertEqual(self.sent, [
            ("sai-jenkins-x", "/remote-control Jenkins: Release migration 0014", False, True),
            ("sai-jenkins-x", "", True, False),
        ])
        # Once is enough.
        run(self.mgr.ensure_session(CHAT, "jenkins", background=False))
        self.assertEqual(len(self.sent), 2)

    def test_a_closed_session_gets_it_when_it_resumes(self):
        rec = self.headless(alive=False)
        flags = {}

        async def launch(r, pol, resume=False):
            flags["cmd"] = self.mgr._session_flags(r, pol, resume)
            r.state = "ready"

        self.mgr._launch = launch
        self.mgr._has_history = lambda r: True
        run(self.mgr.ensure_session(CHAT, "jenkins", background=False))
        self.assertFalse(rec.background)
        self.assertIn("--remote-control", flags["cmd"])
        self.assertEqual(self.sent, [])

    def test_other_automation_stays_headless(self):
        rec = self.headless(alive=True)
        run(self.mgr.ensure_session(CHAT, "jenkins", background=True))
        self.assertTrue(rec.background)
        self.assertEqual(self.sent, [])

    def test_a_persona_without_remote_control_is_left_alone(self):
        self.headless(alive=True)
        self.mgr.set_policy(PersonaPolicy(name="jenkins", work_dir=self._tmp.name, remote_control=False))
        run(self.mgr.ensure_session(CHAT, "jenkins", background=False))
        self.assertEqual(self.sent, [])


class OwnerSessionChat(unittest.TestCase):
    """A session the owner started in Claude Code names its chat on calls."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self._tmp.name)
        self.sessions = base / "sessions"
        self.sessions.mkdir()
        self.state = base / "state.json"
        self._p = [
            mock.patch.object(chat_context, "CLAUDE_SESSIONS_DIR", self.sessions),
            mock.patch.object(chat_context, "TRACKER_STATE", self.state),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for p in self._p:
            p.start()
        os.environ.pop("SOCIETY_AI_CHAT_ID", None)
        os.environ.pop("SOCIETY_AI_SESSION_KEY", None)
        chat_context._cache.clear()

    def tearDown(self):
        for p in reversed(self._p):
            p.stop()
        self._tmp.cleanup()

    def register(self, origin):
        (self.sessions / f"{os.getppid()}.json").write_text(json.dumps({"pid": os.getppid(), "sessionId": "s-9"}))
        self.state.write_text(json.dumps({"sessions": {"s-9": {"chat_id": CHAT, "origin": origin, "cwd": "/r"}}}))

    def test_its_recorded_chat(self):
        self.register("direct")
        self.assertEqual(run(chat_context.current_chat_id()), CHAT)

    def test_only_a_session_the_owner_started(self):
        self.register("society_ai")
        self.assertEqual(run(chat_context.current_chat_id()), "")

    def test_nothing_recorded_means_no_chat(self):
        self.assertEqual(run(chat_context.current_chat_id()), "")
        (self.sessions / f"{os.getppid()}.json").write_text("not json")
        self.assertEqual(run(chat_context.current_chat_id()), "")


if __name__ == "__main__":
    unittest.main()
