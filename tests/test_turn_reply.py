"""The reply is the whole turn: the bridge waits for the turn's final entry,
so a turn that used a tool is not cut off at its opening line."""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("SESSION_MODE", "false")

from core import bridge  # noqa: E402

EVENT = "task-1"


def entry(kind, **kw):
    if kind == "event":
        return {"type": "user", "message": {"role": "user", "content":
                f'<channel source="society-ai-channel" event_id="{EVENT}" kind="chat">hi</channel>'}}
    if kind == "text":
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": kw["text"]}],
                                                  "stop_reason": kw["stop"]}}
    if kind == "tool":
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}],
                                                  "stop_reason": "tool_use"}}


class TurnReply(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = self._tmp.name
        self.sid = "sid-1"
        from agents.claude_code.runner.transcript_shipper import transcript_path
        self.path = transcript_path(self.cwd, self.sid)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.path.parent, ignore_errors=True)
        self._tmp.cleanup()

    def write(self, *entries):
        self.path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    def test_partial_turn_is_not_final(self):
        self.write(entry("event"), entry("text", text="Let me look.", stop="tool_use"), entry("tool"))
        text, final = bridge.Bridge._turn_reply(self.cwd, self.sid, EVENT)
        self.assertEqual((text, final), ("Let me look.", False))

    def test_complete_turn(self):
        self.write(entry("event"), entry("text", text="Let me look.", stop="tool_use"), entry("tool"),
                   entry("text", text="It is red.", stop="end_turn"))
        text, final = bridge.Bridge._turn_reply(self.cwd, self.sid, EVENT)
        self.assertEqual((text, final), ("Let me look.\n\nIt is red.", True))

    def test_close_waits_for_the_late_final_block(self):
        self.write(entry("event"), entry("text", text="Let me look.", stop="tool_use"), entry("tool"))
        with tempfile.TemporaryDirectory() as d:
            ctx = bridge.AgentContext(name="a", token="t", work_dir=d, extra_dirs=[], company_id="",
                                      api_url="https://x", socket=f"{d}/b.sock", state_dir=d)
            b = bridge.Bridge(ctx)

            async def run():
                fut = asyncio.get_event_loop().create_future()
                b._pending_session_tasks = {EVENT: fut}   # session-mode state
                b._session_awaiting = {self.sid: {"task_id": EVENT, "cwd": self.cwd}}

                async def flush_late():
                    await asyncio.sleep(0.8)  # the CLI writes the final block after the hook
                    self.write(entry("event"), entry("text", text="Let me look.", stop="tool_use"),
                               entry("tool"), entry("text", text="It is red.", stop="end_turn"))
                asyncio.ensure_future(flush_late())
                await b._close_turn_from_transcript(self.sid)
                return fut.result()

            self.assertEqual(asyncio.run(run()), "Let me look.\n\nIt is red.")

    def test_never_final_still_answers_after_the_wait(self):
        self.write(entry("event"), entry("text", text="Partial.", stop="refusal"))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bridge, "TURN_FINAL_WAIT_S", 0.5):
            ctx = bridge.AgentContext(name="a", token="t", work_dir=d, extra_dirs=[], company_id="",
                                      api_url="https://x", socket=f"{d}/b.sock", state_dir=d)
            b = bridge.Bridge(ctx)

            async def run():
                fut = asyncio.get_event_loop().create_future()
                b._pending_session_tasks = {EVENT: fut}   # session-mode state
                b._session_awaiting = {self.sid: {"task_id": EVENT, "cwd": self.cwd}}
                await b._close_turn_from_transcript(self.sid)
                return fut.result()

            self.assertEqual(asyncio.run(run()), "Partial.")


def upload(tool_id, name="report.pdf"):
    return {"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": tool_id, "name": "mcp__society-ai__upload_file",
        "input": {"file_path": f"/tmp/{name}", "mime_type": "application/pdf", "name": name}}],
        "stop_reason": "tool_use"}}


def uploaded(tool_id, answer):
    return {"type": "user", "message": {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": tool_id,
        "content": [{"type": "text", "text": "[acting as lior-cc @ http://x]\n" + json.dumps(answer)}]}]}}


class TurnFiles(TurnReply):
    """Files the agent published with upload_file in a turn go back with its reply (agent to person)."""

    ART = "0a99eb9c-8284-45b9-8a4a-27fe159bd509"

    def test_a_turns_upload_becomes_a_file_part(self):
        self.write(entry("event"), upload("tu1"),
                   uploaded("tu1", {"artifact_id": self.ART, "size": 2048, "mime_type": "application/pdf"}),
                   entry("text", text="Here it is.", stop="end_turn"))
        files = bridge.Bridge._turn_files(self.cwd, self.sid, EVENT)
        self.assertEqual(files, [{
            "type": "file",
            "file": {"name": "report.pdf", "mimeType": "application/pdf", "uri": f"artifact:{self.ART}"},
            "metadata": {"artifact_id": self.ART, "size": 2048},
        }])

    def test_the_wrapped_result_shape_claude_code_records(self):
        # Seen live (Claude Code 2.1.319): the tool_result content is the string {"result": "<label>\\n<json>"}.
        inner = "[acting as lior-cc @ http://x]\n" + json.dumps({"artifact_id": self.ART, "size": 64})
        wrapped = {"type": "user", "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "tu1", "content": json.dumps({"result": inner})}]}}
        held = {"type": "user", "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "tu2", "content": json.dumps({"result": "[acting as x]\n"
                + json.dumps({"status": "held", "held_action_id": "h1"})})}]}}
        self.write(entry("event"), upload("tu1"), wrapped, upload("tu2", "other.pdf"), held)
        files = bridge.Bridge._turn_files(self.cwd, self.sid, EVENT)
        self.assertEqual([f["metadata"]["artifact_id"] for f in files], [self.ART])  # a held upload is not a file yet

    def test_failed_uploads_other_tools_and_other_turns_do_not_count(self):
        earlier = {"type": "user", "message": {"role": "user", "content":
                   '<channel source="society-ai-channel" event_id="task-0" kind="chat">before</channel>'}}
        later = {"type": "user", "message": {"role": "user", "content":
                 '<channel source="society-ai-channel" event_id="task-2" kind="chat">next</channel>'}}
        self.write(earlier, upload("tu0"), uploaded("tu0", {"artifact_id": "old-file"}),
                   entry("event"),
                   upload("tu1"), uploaded("tu1", {"error": "Storage refused the upload"}),
                   entry("tool"),
                   later, upload("tu2"), uploaded("tu2", {"artifact_id": "next-file"}))
        self.assertEqual(bridge.Bridge._turn_files(self.cwd, self.sid, EVENT), [])

    def test_task_complete_carries_the_files_after_the_text(self):
        b = bridge.Bridge.__new__(bridge.Bridge)
        sent = []

        async def send_rpc(method, params, msg_id=None):
            sent.append((method, params))

        b.send_rpc = send_rpc
        b._next_id = lambda: 1
        part = {"type": "file", "file": {"name": "a.png", "mimeType": "image/png", "uri": "artifact:x"},
                "metadata": {"artifact_id": "x"}}
        asyncio.run(b._send_task_complete("t1", "Done.", files=[part]))
        self.assertEqual(sent[0][1]["status"]["message"]["parts"], [{"type": "text", "text": "Done."}, part])


if __name__ == "__main__":
    unittest.main()
