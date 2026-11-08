"""The runner interface and the generic runners (core/runner.py, runners/), against stand-in agent programs."""

import asyncio
import os
import pathlib
import stat
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SESSION_MODE", "false")

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

if HAVE_YAML:
    from core import runner as core_runner
    from core.runner import ConversationRef, RunnerConfigError, RunnerSpec, Turn
    from runners.acp import AcpRunner
    from runners.exec import ExecRunner

EXEC_AGENT = """#!{python}
import os, sys
if sys.argv[1:] == ["--version"]:
    print("fake-agent 1.4.2"); sys.exit(0)
prompt = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
if "FAIL" in prompt:
    print("it broke", file=sys.stderr); sys.exit(3)
leaked = "SOCIETY_AI_AUTH_TOKEN" in os.environ
print(f"cwd={{os.path.basename(os.getcwd())}} leaked={{leaked}} saw_history={{'[Earlier in this conversation]' in prompt}}")
print("last=" + prompt.strip().splitlines()[-1])
"""

ACP_AGENT = """#!{python}
import json, sys

def send(m):
    sys.stdout.write(json.dumps(m) + "\\n"); sys.stdout.flush()

def recv():
    line = sys.stdin.readline()
    return json.loads(line) if line else None

sessions = {{}}
while True:
    m = recv()
    if m is None:
        break
    method, mid, p = m.get("method"), m.get("id"), m.get("params") or {{}}
    if method == "initialize":
        send({{"jsonrpc": "2.0", "id": mid, "result": {{"protocolVersion": 1, "agentCapabilities": {{}}}}}})
    elif method == "session/new":
        sid = "s%d" % (len(sessions) + 1); sessions[sid] = []
        send({{"jsonrpc": "2.0", "id": mid, "result": {{"sessionId": sid}}}})
    elif method == "session/prompt":
        sid = p["sessionId"]; text = p["prompt"][0]["text"]; sessions[sid].append(text)
        send({{"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {{
            "sessionId": sid, "toolCall": {{"toolCallId": "t1"}},
            "options": [{{"optionId": "yes", "name": "Allow", "kind": "allow_once"}},
                        {{"optionId": "no", "name": "Reject", "kind": "reject_once"}}]}}}})
        answer = recv()
        chosen = (answer.get("result") or {{}}).get("outcome", {{}}).get("optionId")
        send({{"jsonrpc": "2.0", "method": "session/update", "params": {{"sessionId": sid, "update": {{
            "sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": "turn %d, " % len(sessions[sid])}}}}}}}})
        send({{"jsonrpc": "2.0", "method": "session/update", "params": {{"sessionId": sid, "update": {{
            "sessionUpdate": "agent_message_chunk", "content": {{"type": "text", "text": "permission=%s" % chosen}}}}}}}})
        send({{"jsonrpc": "2.0", "id": mid, "result": {{"stopReason": "end_turn"}}}})
"""


def run(coro):
    return asyncio.run(coro)


async def collect(runner, session, turn):
    return [e async for e in runner.run(session, turn)]


@unittest.skipUnless(HAVE_YAML, "pip install -r requirements.txt")
class Specs(unittest.TestCase):
    def test_a_type_s_runner_comes_from_its_manifest(self):
        spec = core_runner.runner_spec("codex")
        self.assertEqual(spec.use, "exec")
        self.assertEqual(spec.command, ("codex", "exec", "--skip-git-repo-check", "{prompt}"))
        self.assertEqual(spec.binaries, frozenset({"codex"}))
        self.assertEqual(core_runner.runner_spec("gemini_cli").use, "acp")

    def test_a_generic_type_runs_the_users_own_command(self):
        spec = core_runner.runner_spec("generic_cli", 'myagent run --quiet')
        self.assertEqual(spec.command, ("myagent", "run", "--quiet", "{prompt}"))
        self.assertEqual(spec.binaries, frozenset({"myagent"}))
        with self.assertRaises(RunnerConfigError):
            core_runner.runner_spec("generic_cli")

    def test_cloud_and_unknown_types_have_no_runner(self):
        for bad in ("grok_bot", "nope"):
            with self.assertRaises(RunnerConfigError):
                core_runner.runner_spec(bad)

    def test_only_declared_programs_start(self):
        spec = RunnerSpec("t", "exec", ("python3",), (), frozenset({"codex"}), ())
        with self.assertRaises(RunnerConfigError):
            core_runner.check_allowed(spec, ("python3", "-c", "1"))

    def test_the_agent_key_never_reaches_the_program(self):
        with mock.patch.dict(os.environ, {"SOCIETY_AI_AUTH_TOKEN": "sai_x", "AGENT_ROUTER_API_URL": "u",
                                          "AGENT_NAME": "n", "PATH": "/bin"}):
            env = core_runner.child_env()
        self.assertNotIn("SOCIETY_AI_AUTH_TOKEN", env)
        self.assertNotIn("AGENT_ROUTER_API_URL", env)
        self.assertNotIn("AGENT_NAME", env)
        self.assertEqual(env["PATH"], "/bin")


class StandIn(unittest.TestCase):
    """A temp folder on PATH holding one stand-in agent program."""

    program = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bindir = pathlib.Path(self.tmp.name) / "bin"
        bindir.mkdir()
        self.work = pathlib.Path(self.tmp.name) / "work"
        self.work.mkdir()
        path = bindir / "fakeagent"
        path.write_text(textwrap.dedent(self.program.format(python=sys.executable)))
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        self.env = mock.patch.dict(os.environ, {"PATH": f"{bindir}:{os.environ['PATH']}",
                                                "SOCIETY_AI_AUTH_TOKEN": "sai_secret"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def conversation(self, key="chat-1"):
        return ConversationRef(key, cwd=str(self.work))


@unittest.skipUnless(HAVE_YAML, "pip install -r requirements.txt")
class Exec(StandIn):
    program = EXEC_AGENT

    def spec(self, command=("fakeagent", "{prompt}")):
        return RunnerSpec("fake", "exec", command, ("fakeagent", "--version"), frozenset({"fakeagent"}), ())

    def test_each_turn_runs_once_with_the_conversation_so_far(self):
        runner = ExecRunner(self.spec())

        async def go():
            session = await runner.start(self.conversation())
            first = await collect(runner, session, Turn("t1", "hello", owner=True))
            second = await collect(runner, session, Turn("t2", "and again", owner=True))
            return first, second

        first, second = run(go())
        self.assertEqual(first[-1].kind, "final")
        self.assertIn("cwd=work leaked=False saw_history=False", first[-1].text)
        self.assertIn("saw_history=True", second[-1].text)
        self.assertIn("last=and again", second[-1].text)

    def test_the_prompt_goes_on_stdin_without_a_placeholder(self):
        runner = ExecRunner(self.spec(("fakeagent",)))
        events = run(self._one(runner, "from stdin"))
        self.assertIn("last=from stdin", events[-1].text)

    def test_a_failing_or_slow_program_is_an_error_not_a_reply(self):
        events = run(self._one(ExecRunner(self.spec()), "FAIL please"))
        self.assertEqual(events[-1].kind, "error")
        self.assertIn("exited with 3: it broke", events[-1].text)

    def test_the_version_comes_from_the_detect_command(self):
        self.assertEqual(run(ExecRunner(self.spec()).version()), "1.4.2")

    async def _one(self, runner, text):
        session = await runner.start(self.conversation())
        return await collect(runner, session, Turn("t", text, owner=True))


@unittest.skipUnless(HAVE_YAML, "pip install -r requirements.txt")
class Acp(StandIn):
    program = ACP_AGENT

    def runner(self):
        return AcpRunner(RunnerSpec("fake", "acp", ("fakeagent",), (), frozenset({"fakeagent"}), ()), timeout_s=10)

    def test_one_agent_process_keeps_a_session_per_conversation(self):
        runner = self.runner()

        async def go():
            a = await runner.start(self.conversation("chat-a"))
            first = await collect(runner, a, Turn("t1", "hi", owner=True))
            again = await collect(runner, a, Turn("t2", "more", owner=True))
            b = await runner.start(self.conversation("chat-b"))
            other = await collect(runner, b, Turn("t3", "hi", owner=True))
            await runner.close()
            return first, again, other

        first, again, other = run(go())
        self.assertEqual([e.kind for e in first], ["progress", "progress", "final"])
        self.assertEqual(first[-1].text, "turn 1, permission=yes")
        self.assertTrue(again[-1].text.startswith("turn 2"))  # the agent kept its own context
        self.assertTrue(other[-1].text.startswith("turn 1"))  # a new conversation, a new session

    def test_permission_requests_are_refused_for_anyone_but_the_owner(self):
        runner = self.runner()

        async def go():
            session = await runner.start(self.conversation())
            events = await collect(runner, session, Turn("t", "hi", owner=False, permission="read"))
            await runner.close()
            return events

        self.assertEqual(run(go())[-1].text, "turn 1, permission=no")


@unittest.skipUnless(HAVE_YAML, "pip install -r requirements.txt")
class BridgeRunsOtherTypesThroughTheirRunner(unittest.TestCase):
    def setUp(self):
        from core import bridge
        from kit.mock_hub import frames
        from kit.mock_hub.hub import MockHub

        self.frames = frames
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        turns = self.turns = []

        class FakeRunner:
            capabilities = core_runner.RunnerCapabilities(False, False, False, enforces_limits=False)

            async def start(self, conversation):
                return core_runner.SessionHandle(conversation)

            async def run(self, session, turn):
                turns.append((session.conversation.key, turn))
                yield core_runner.ReplyEvent("final", "the runner's answer")

            async def stop(self, session):
                pass

            async def version(self):
                return "9.9"

        with mock.patch.object(bridge, "AGENT_TYPE", "codex"), \
                mock.patch("core.runner.load_runner", return_value=FakeRunner()):
            ctx = bridge.AgentContext(name="t", token="sai_x", work_dir=d, extra_dirs=[], company_id="",
                                      api_url="http://127.0.0.1:9", socket=f"{d}/b.sock", state_dir=d)
            self.b = bridge.Bridge(ctx)
        self.b._owner_user_id = frames.OWNER_ID
        self.hub = MockHub()
        self.hub.attach(self.b)

    def tearDown(self):
        self.tmp.cleanup()

    def deliver(self, task):
        async def go():
            with mock.patch.object(self.b, "_report_delivery", mock.AsyncMock()), \
                    mock.patch.object(self.b, "_stream_delivery", mock.AsyncMock()):
                await self.hub.deliver(self.b, task)
                for _ in range(200):
                    if task["id"] in self.hub.replies:
                        break
                    await asyncio.sleep(0.01)
        run(go())
        return self.hub.replies.get(task["id"])

    def test_no_claude_code_sessions_are_set_up(self):
        self.assertFalse(self.b._session_mode)
        self.assertIsNone(self.b._session_mgr)

    def test_the_owners_message_runs_as_one_turn_in_its_chat(self):
        task = self.frames.owner("what changed today?", chat_id="chat-9")
        reply = self.deliver(task)
        self.assertIn("the runner's answer", str(reply))
        key, turn = self.turns[0]
        self.assertEqual(key, "chat-9")
        self.assertTrue(turn.owner)
        self.assertIn("what changed today?", turn.prompt)

    def test_a_contact_is_refused_when_the_runner_cannot_hold_it_to_its_level(self):
        reply = self.deliver(self.frames.contact("hi", permission="act"))
        self.assertIn("can't take requests from contacts", str(reply))
        self.assertEqual(self.turns, [])


if __name__ == "__main__":
    unittest.main()
