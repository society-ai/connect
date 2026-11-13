"""Sessions started in Claude Code, mirrored while tracked (Society AI design
session-trackers D36 to D43).

- The first message's hook asks the tracking bridge to register the session
  and prints the banner line (terminal) and the agent's instruction (desktop);
  once per session per Claude Code process; nothing for bridge sessions or
  when tracking is off.
- The agent's own bridge picks the session up from the runner's state and
  ships it as 'direct'; another agent's bridge does not.
- A batch refused because mirroring was stopped makes the shipper forget the
  session.
- Turns the owner sent from the app ship marked as chat echoes, so the chat
  does not show them twice.
- stop_session_mirror finds its own session and stops it.
"""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("SESSION_MODE", "false")

from agents.claude_code.runner import hook_user_prompt as hook  # noqa: E402
from agents.claude_code.runner import session_tracker as st  # noqa: E402
from agents.claude_code.runner import transcript_shipper as ts  # noqa: E402


class Banner(unittest.TestCase):
    def test_lines(self):
        url = "https://app/chat/c1"
        self.assertEqual(
            hook.banner_line({"mirrored": True, "chat_url": url}, md=True),
            f"🪞 Mirrored and tracked on Society AI · [Open this session]({url})",
        )
        self.assertEqual(hook.banner_line({"mirrored": True, "chat_url": url}, md=False),
                         f"🪞 Mirrored and tracked on Society AI · {url}")
        self.assertEqual(hook.banner_line({"mirrored": False, "reason": "folder_skipped", "folder": "~/x"}, md=True),
                         "🔒 Not mirrored: ~/x is skipped in Settings")
        self.assertEqual(hook.banner_line({"mirrored": False, "reason": "stopped"}, md=True),
                         "🔒 Not mirrored: you stopped mirroring this session")
        for quiet in ({"mirrored": False, "reason": "off"}, {"mirrored": False}, {"mirrored": True}):
            self.assertIsNone(hook.banner_line(quiet, md=True))

    def test_output_asks_the_agent_for_the_line_and_names_the_stop_tool(self):
        out = hook.hook_output({"mirrored": True, "chat_url": "https://app/chat/c1"})
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("> 🪞 Mirrored and tracked on Society AI · [Open this session](https://app/chat/c1)", context)
        self.assertIn("stop_session_mirror", context)
        self.assertEqual(out["systemMessage"], "🪞 Mirrored and tracked on Society AI · https://app/chat/c1")
        skipped = hook.hook_output({"mirrored": False, "reason": "folder_skipped", "folder": "~/x"})
        self.assertNotIn("stop_session_mirror", skipped["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(hook.hook_output({"mirrored": False, "reason": "off"}))


class HookMain(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(hook, "STARTED_DIR", pathlib.Path(self.tmp.name) / "started"),
            mock.patch.object(hook, "_notify_mirrors"),
            mock.patch.object(hook, "_read_hook_input",
                              return_value={"session_id": "s1", "cwd": "/w", "prompt": "hi"}),
        ]
        for p in self.patches:
            p.start()
        env = {k: v for k, v in os.environ.items() if k not in ("SOCIETY_AI_TRACKER", "SOCIETY_AI_DISPATCHED")}
        self.env = mock.patch.dict(os.environ, env, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_hook(self):
        with mock.patch("builtins.print") as printed:
            hook.main()
        return [json.loads(c.args[0]) for c in printed.call_args_list]

    def test_once_per_session_per_process(self):
        answer = {"mirrored": True, "chat_url": "https://app/chat/c1"}
        with mock.patch.object(hook, "_session_start", return_value=answer) as start:
            first = self.run_hook()
            second = self.run_hook()
        start.assert_called_once()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        # The prompt is mirrored either way.
        self.assertEqual(hook._notify_mirrors.call_count, 2)

    def test_asks_again_when_no_bridge_answered(self):
        with mock.patch.object(hook, "_session_start", return_value=None) as start:
            self.assertEqual(self.run_hook(), [])
            self.run_hook()
        self.assertEqual(start.call_count, 2)

    def test_bridge_sessions_and_tracker_runs_skip_it(self):
        for flag in ("SOCIETY_AI_DISPATCHED", "SOCIETY_AI_TRACKER"):
            with mock.patch.dict(os.environ, {flag: "1"}), \
                    mock.patch.object(hook, "_session_start") as start:
                self.assertEqual(self.run_hook(), [])
            start.assert_not_called()

    def test_session_start_takes_the_runners_answer(self):
        replies = [None, {"mirrored": False, "reason": "off"}]

        class FakeSock:
            def __init__(self, *a):
                self.reply = replies.pop(0)
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def settimeout(self, t):
                pass
            def connect(self, path):
                pass
            def sendall(self, data):
                self.sent = json.loads(data)
            def recv(self, n):
                if self.reply is False:
                    return b""
                out = (json.dumps({"id": "x", "result": self.reply}) + "\n").encode()
                self.reply = False
                return out

        with mock.patch.object(hook, "_bridge_sockets", return_value=["/a.sock", "/b.sock"]), \
                mock.patch.object(hook.socket, "socket", FakeSock):
            answer = hook._session_start({"session_id": "s1", "cwd": "/w", "prompt": "hi"})
        self.assertEqual(answer, {"mirrored": False, "reason": "off"})


class Adopting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.patch = mock.patch.object(st, "BASE_DIR", self.base)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def adopt(self, persona, state):
        from core.bridge import Bridge
        (self.base / "state.json").write_text(json.dumps({"sessions": {"s1": state}}))
        shipper = ts.TranscriptShipper("https://api.x", "sai_x", str(self.base / persona))
        fake = types.SimpleNamespace(_shipper=shipper, ctx=types.SimpleNamespace(name=persona))
        Bridge._adopt_direct_session(fake, "s1", {"cwd": "/w"})
        return shipper

    def test_the_sessions_own_agent_ships_it_as_direct(self):
        shipper = self.adopt("jenkins", {"mirrored": True, "agent": "jenkins", "chat_id": "c1"})
        self.assertTrue(shipper.is_registered("s1"))
        self.assertEqual(shipper._state["s1"]["work_item_kind"], "direct")
        self.assertEqual(shipper._state["s1"]["dispatch_chat_id"], "c1")

    def test_a_session_moved_to_another_agent_changes_bridges(self):
        jenkins = self.adopt("jenkins", {"mirrored": True, "agent": "jenkins", "chat_id": "c1"})
        self.assertTrue(jenkins.is_registered("s1"))
        # The person switched to buzz: jenkins's bridge lets it go, buzz's takes it.
        self.assertFalse(self.adopt("jenkins", {"mirrored": True, "agent": "buzz", "chat_id": "c1"}).is_registered("s1"))
        self.assertTrue(self.adopt("buzz", {"mirrored": True, "agent": "buzz", "chat_id": "c1"}).is_registered("s1"))

    def test_a_bridge_session_is_never_handed_over(self):
        from core.bridge import Bridge
        shipper = ts.TranscriptShipper("https://api.x", "sai_x", str(self.base / "jenkins"))
        shipper.register("s1", cwd="/w", work_item_kind="chat", work_item_id="c1")
        (self.base / "state.json").write_text(json.dumps({"sessions": {"s1": {"mirrored": True, "agent": "buzz"}}}))
        fake = types.SimpleNamespace(_shipper=shipper, ctx=types.SimpleNamespace(name="jenkins"))
        Bridge._adopt_direct_session(fake, "s1", {"cwd": "/w"})
        self.assertTrue(shipper.is_registered("s1"))

    def test_not_for_another_agent_or_a_session_not_mirrored(self):
        self.assertFalse(self.adopt("searchpro", {"mirrored": True, "agent": "jenkins"}).is_registered("s1"))
        self.assertFalse(self.adopt("jenkins", {"mirrored": False, "agent": "jenkins"}).is_registered("s1"))


def _record(text, **extra):
    return {"type": "user", "timestamp": "2026-11-08T10:00:00Z", "message": {"content": text}, **extra}


class Shipping(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.transcript = self.dir / "s1.jsonl"
        self.shipper = ts.TranscriptShipper("https://api.x", "sai_x", str(self.dir))
        self.shipper.register("s1", cwd=str(self.dir), work_item_kind="direct")
        self.path = mock.patch.object(ts, "transcript_path", return_value=self.transcript)
        self.path.start()

    def tearDown(self):
        self.path.stop()
        self.tmp.cleanup()

    def entries(self, records):
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
        entries, _, _ = self.shipper._read_new_entries("s1", self.shipper._state["s1"])
        return [e["content"]["text"] for e in entries]

    def test_relayed_messages_ship_as_chat_echoes(self):
        relayed = ('Another Claude session sent a message:\n<cross-session-message from="uds:/x.sock" '
                   'from-name="Society AI" from-mode="prompting">\nDeploy it\n</cross-session-message>\n\nThis came')
        texts = self.entries([_record(relayed), _record("typed here")])
        self.assertEqual(texts[0], ts.chat_echo("Deploy it"))
        self.assertTrue(texts[0].startswith('<channel ') and 'kind="chat"' in texts[0])
        self.assertEqual(texts[1], "typed here")

    def test_turns_the_session_machinery_wrote_stay_on_the_machine(self):
        notice = ('<task-notification>\n<task-id>bpozm8ozh</task-id>\n<summary>Monitor event: "replay"</summary>\n'
                  '<event>progress 200/2277</event>\n</task-notification>')
        texts = self.entries([
            _record(notice, origin={"kind": "task-notification", "producer": "session-task"}),
            _record(notice),  # older Claude Code: no origin, the tag says it
            _record("<command-name>/clear</command-name>\n<command-message>clear</command-message>"),
            _record("<local-command-stdout>Cleared</local-command-stdout>"),
            _record("<bash-input>git status</bash-input>"),
            _record('<ci-monitor-event pr="7">checks passed</ci-monitor-event>'),
            _record('<scheduled-task name="morning">Run the brief</scheduled-task>'),
            _record("<system-reminder>Context only</system-reminder>"),
            _record("<artifact-view-context artifact=\"a1\">\n{}\n</artifact-view-context>\n\nship it"),
            _record("typed here", origin={"kind": "human"}),
        ])
        self.assertEqual(texts, ["ship it", "typed here"])

    def test_a_resumed_turn_from_the_app_ships_as_a_chat_echo_once(self):
        self.shipper.expect_app_turn("s1", "What is left?")
        texts = self.entries([_record("What is left?"), _record("What is left?")])
        self.assertEqual(texts, [ts.chat_echo("What is left?"), "What is left?"])

    def test_a_stopped_session_is_forgotten(self):
        self.transcript.write_text(json.dumps(_record("hello")) + "\n")
        handler = lambda request: httpx.Response(409, json={"stopped": True})  # noqa: E731
        self.shipper._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.assertFalse(asyncio.run(self.shipper.ship("s1")))
        self.assertFalse(self.shipper.is_registered("s1"))
        saved = json.loads((self.dir / "mirror_state.json").read_text())
        self.assertNotIn("s1", saved)

    def test_other_refusals_keep_the_session(self):
        self.transcript.write_text(json.dumps(_record("hello")) + "\n")
        handler = lambda request: httpx.Response(409, json={"error": "other"})  # noqa: E731
        self.shipper._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.assertFalse(asyncio.run(self.shipper.ship("s1")))
        self.assertTrue(self.shipper.is_registered("s1"))

    FIREWALL_PAGE = "<html>\n<head><title>403 Forbidden</title></head>\n<body>\n<center><h1>403 Forbidden</h1></center>\n</body>\n</html>\n"

    def firewall(self, accepted, blocked="../"):
        """A firewall that refuses any batch whose text contains `blocked`,
        in front of a platform that keeps what it accepts."""
        def handler(request):
            body = json.loads(request.content)
            if blocked in json.dumps(body["entries"]):
                return httpx.Response(403, text=self.FIREWALL_PAGE, headers={"content-type": "text/html"})
            accepted.extend(body["entries"])
            return httpx.Response(200, json={})
        return handler

    def test_a_firewall_refusal_ships_the_refused_entry_as_a_note_and_moves_on(self):
        texts = ["first", "see [bridge.py](../core/bridge.py)", "third", "fourth"]
        self.transcript.write_text("".join(json.dumps(_record(t)) + "\n" for t in texts))
        accepted: list = []
        self.shipper._client = httpx.AsyncClient(transport=httpx.MockTransport(self.firewall(accepted)))
        self.assertTrue(asyncio.run(self.shipper.ship("s1", status="active")))
        by_seq = {e["seq"]: e["content"]["text"] for e in accepted}
        self.assertEqual(by_seq, {0: "first", 1: ts.REFUSED_ENTRY_NOTE, 2: "third", 3: "fourth"})
        meta = self.shipper._state["s1"]
        self.assertEqual((meta["pos"], meta["seq"]), (self.transcript.stat().st_size, 4))
        self.assertNotIn("pending_status", meta)
        # Nothing is re-sent: the next ship has nothing new.
        accepted.clear()
        self.assertTrue(asyncio.run(self.shipper.ship("s1")))
        self.assertEqual(accepted, [])

    def test_the_platforms_own_403_is_not_split(self):
        self.transcript.write_text("".join(json.dumps(_record(t)) + "\n" for t in ("a", "b")))
        calls: list = []

        def handler(request):
            calls.append(request)
            return httpx.Response(403, json={"detail": "token revoked"})
        self.shipper._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.assertFalse(asyncio.run(self.shipper.ship("s1", status="active")))
        self.assertEqual(len(calls), 1)
        meta = self.shipper._state["s1"]
        self.assertEqual((meta["pos"], meta["pending_status"], meta["fail_count"]), (0, "active", 1))

    def test_a_failing_session_waits_longer_before_each_retry(self):
        calls: list = []

        def handler(request):
            calls.append(request)
            return httpx.Response(503, text="unavailable")
        self.shipper._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with mock.patch.object(ts.time, "time", return_value=1000.0):
            self.assertFalse(asyncio.run(self.shipper.ship("s1", status="ended")))
            self.assertEqual(self.shipper._state["s1"]["retry_at"], 1000.0 + ts.RETRY_BACKOFF_FIRST)
            self.assertEqual(asyncio.run(self.shipper.retry_pending()), 0)  # still waiting
        with mock.patch.object(ts.time, "time", return_value=1000.0 + ts.RETRY_BACKOFF_FIRST):
            self.assertEqual(asyncio.run(self.shipper.retry_pending()), 1)
            self.assertEqual(self.shipper._state["s1"]["retry_at"],
                             1000.0 + 3 * ts.RETRY_BACKOFF_FIRST)  # doubled
        self.assertEqual(len(calls), 2)
        self.shipper._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        with mock.patch.object(ts.time, "time", return_value=10_000.0):
            self.assertEqual(asyncio.run(self.shipper.retry_pending()), 1)
        meta = self.shipper._state["s1"]
        self.assertFalse({"pending_status", "fail_count", "retry_at"} & set(meta))

    def test_a_refused_status_flip_with_no_entries_retries_later(self):
        handler = lambda request: httpx.Response(403, text=self.FIREWALL_PAGE)  # noqa: E731
        self.shipper._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.assertFalse(asyncio.run(self.shipper.ship("s1", status="ended")))
        self.assertEqual(self.shipper._state["s1"]["pending_status"], "ended")


class StopTool(unittest.TestCase):
    def test_finds_its_own_session_and_stops_it(self):
        from core import mcp_server
        with tempfile.TemporaryDirectory() as home:
            sessions = pathlib.Path(home) / ".claude" / "sessions"
            sessions.mkdir(parents=True)
            (sessions / "1.json").write_text(json.dumps({"pid": 1, "sessionId": "other"}))
            (sessions / "2.json").write_text(json.dumps({"pid": os.getppid(), "sessionId": "mine"}))
            with mock.patch.dict(os.environ, {"HOME": home}), \
                    mock.patch.object(mcp_server.api, "post", mock.AsyncMock(return_value={"stopped": True})) as post, \
                    mock.patch.object(mcp_server, "_label", return_value="[as jenkins]"):
                out = asyncio.run(mcp_server.stop_session_mirror())
        post.assert_awaited_once_with("/api/v1/session-trackers/stop", {"external_session_id": "mine"})
        self.assertIn('"stopped": true', out)

    def test_says_so_when_it_cannot_tell_the_session(self):
        from core import mcp_server
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}), \
                mock.patch.object(mcp_server, "_label", return_value="[as jenkins]"):
            out = asyncio.run(mcp_server.stop_session_mirror())
        self.assertIn("Could not tell which Claude Code session this is", out)


if __name__ == "__main__":
    unittest.main()


class WhatTheChatGets(unittest.TestCase):
    """The banner is for the Claude Code app; a skill's instructions are the agent's."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.shipper = ts.TranscriptShipper("https://api.x", "sai_x", self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def ship(self, *records):
        out = []
        for r in records:
            out.extend(self.shipper._line_entries(json.dumps(r).encode()))
        return [(e["entry_type"], e["content"]) for e in out]

    def test_the_banner_is_cut_from_the_reply(self):
        banner = ("> ⚡ **Society AI** · 2 agents on this machine, none bound to this session\n"
                  "> 🤖 **jenkins** · Society AI Cloud · **46 open tasks**\n"
                  "> 🪞 Mirrored and tracked on Society AI · [Open this session](https://x/chat/1)\n\n")
        reply = {"type": "assistant", "message": {"content": [{"type": "text", "text": banner + "Here is the status."}]}}
        alone = {"type": "assistant", "message": {"content": [{"type": "text", "text": banner}]}}
        own_quote = {"type": "assistant", "message": {"content": [{"type": "text", "text": "> a quote\nmine"}]}}
        self.assertEqual(self.ship(reply, alone, own_quote), [
            ("assistant", {"text": "Here is the status."}),
            ("assistant", {"text": "> a quote\nmine"}),
        ])

    def test_a_loaded_skill_ships_as_the_agents_skill_entry(self):
        # The records Claude Code writes when the agent calls the Skill tool.
        result = {"type": "user", "toolUseResult": {"success": True, "commandName": "live-results"},
                  "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1",
                                           "content": "Launching skill: live-results"}]}}
        meta = {"type": "user", "isMeta": True, "sourceToolUseID": "toolu_1",
                "message": {"content": [{"type": "text", "text": "# Live results\n\nEvery number from Polymarket."}]}}
        self.assertEqual(self.ship(result, meta), [
            ("skill", {"name": "live-results", "text": "# Live results\n\nEvery number from Polymarket."}),
        ])

    def test_a_message_from_another_session_ships_with_its_name(self):
        text = ('Another Claude session sent a message:\n<cross-session-message from="uds:/tmp/cc-socks/1.sock" '
                'from-name="Generic agent connector design">\nShip it as v0.32.0\n</cross-session-message>\n\nThis came from another Claude session')
        peer = {"type": "user", "isMeta": True,
                "origin": {"kind": "peer", "name": "Generic agent connector design", "body": "Ship it as v0.32.0"},
                "message": {"content": [{"type": "text", "text": text}]}}
        no_body = dict(peer, origin={"kind": "peer", "name": "Other"})
        self.assertEqual(self.ship(peer, no_body), [
            ("peer", {"name": "Generic agent connector design", "text": "Ship it as v0.32.0"}),
            ("peer", {"name": "Other", "text": "Ship it as v0.32.0"}),
        ])

    def test_the_owners_app_messages_still_ship_as_chat_echoes(self):
        relay = {"type": "user", "isMeta": True, "origin": {"kind": "peer", "name": "Society AI"},
                 "message": {"content": [{"type": "text", "text": (
                     'Another Claude session sent a message:\n<cross-session-message from="uds:/x.sock" '
                     'from-name="Society AI">\nDeploy it\n</cross-session-message>')}]}}
        channel_text = '<channel source="society-ai-channel" kind="chat">\nhello\n</channel>'
        channel = {"type": "user", "isMeta": True, "origin": {"kind": "channel"},
                   "message": {"content": [{"type": "text", "text": channel_text}]}}
        self.assertEqual(self.ship(relay, channel), [
            ("user", {"text": ts.chat_echo("Deploy it")}),
            ("user", {"text": channel_text}),
        ])

    def test_other_notes_for_the_model_stay_local(self):
        caveat = {"type": "user", "isMeta": True,
                  "message": {"content": "Caveat: The messages below were generated by the user while running local commands."}}
        unknown = {"type": "user", "isMeta": True, "sourceToolUseID": "toolu_unknown",
                   "message": {"content": [{"type": "text", "text": "internal"}]}}
        # The record that reached the owner's chat as their message (Claude Code 2.1.319).
        reminder = {"type": "user", "isMeta": True, "turnCompanion": True,
                    "message": {"content": [{"type": "text", "text": (
                        "<system-reminder><tools-used-before-summary>mcp__society-ai-channel__channel"
                        "</tools-used-before-summary>&lt;/system-reminder&gt;")}]}}
        self.assertEqual(self.ship(caveat, unknown, reminder), [])
