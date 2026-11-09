"""The update outcome reaches the feed for the update that just ran (Society AI
design agent-updates.md).

- The restarted bridge waits for update.sh's outcome while its pending file is
  there, then posts it; without a pending file it posts only an outcome
  already written, and gives up after UPDATE_RESULT_WAIT_S.
- The updater learns the version the asking bridge runs, so an already updated
  folder still restarts a bridge running older code.
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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("SESSION_MODE", "false")

from core import bridge as bridge_mod  # noqa: E402
from core.bridge import Bridge  # noqa: E402


class _Resp:
    status_code = 200


def _fake(state_dir):
    return types.SimpleNamespace(
        ctx=types.SimpleNamespace(state_dir=state_dir, token="sai_x", api_url="https://api.x", name="jenkins"),
    )


class ReportOutcome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.client = mock.Mock()
        self.client.post = mock.AsyncMock(return_value=_Resp())
        self.patches = [
            mock.patch.object(bridge_mod, "_get_http_client", return_value=self.client),
            mock.patch.object(bridge_mod, "UPDATE_RESULT_POLL_S", 0.01),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write(self, name, data):
        with open(os.path.join(self.dir, name), "w") as f:
            json.dump(data, f)

    def posted(self):
        return [c.kwargs["json"]["message"] for c in self.client.post.await_args_list]

    def test_waits_for_the_outcome_while_the_update_is_pending(self):
        self.write("update-pending.json", {"at": "now"})

        async def run():
            task = asyncio.ensure_future(Bridge._report_update_outcome(_fake(self.dir)))
            await asyncio.sleep(0.05)
            self.assertEqual(self.posted(), [])
            os.remove(os.path.join(self.dir, "update-pending.json"))
            self.write("update-result.json", {"ok": True, "from": "0.30.1", "to": "0.31.0"})
            await task

        asyncio.run(run())
        self.assertEqual(self.posted(), ["Updated from 0.30.1 to 0.31.0."])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "update-result.json")))

    def test_without_a_pending_update_only_an_existing_outcome_is_posted(self):
        asyncio.run(Bridge._report_update_outcome(_fake(self.dir)))
        self.assertEqual(self.posted(), [])
        self.write("update-result.json", {"ok": False, "from": "0.30.1", "to": "0.31.0",
                                          "error": "boom", "rolled_back": True})
        asyncio.run(Bridge._report_update_outcome(_fake(self.dir)))
        self.assertEqual(self.posted(), ["Update to 0.31.0 failed (boom), rolled back to 0.30.1."])

    def test_gives_up_on_a_pending_update_that_never_finishes(self):
        self.write("update-pending.json", {"at": "then"})
        with mock.patch.object(bridge_mod, "UPDATE_RESULT_WAIT_S", 0.05):
            asyncio.run(Bridge._report_update_outcome(_fake(self.dir)))
        self.assertEqual(self.posted(), [])


class SpawnUpdater(unittest.TestCase):
    def test_the_updater_learns_the_running_version(self):
        with tempfile.TemporaryDirectory() as state_dir:
            fake = _fake(state_dir)
            fake._pending_update = {"latest_version": "0.31.0", "reason": "user_approved"}
            fake._derive_persona = lambda: "searchpro"
            with mock.patch.object(bridge_mod.subprocess, "Popen") as popen:
                Bridge._start_pending_update(fake)
        args, kwargs = popen.call_args
        self.assertTrue(args[0][0].endswith("core/installer/update.sh"))
        self.assertEqual(args[0][1:], ["searchpro"])
        self.assertEqual(kwargs["env"]["SOCIETY_AI_RUNNING_VERSION"], bridge_mod.__version__)
        self.assertTrue(kwargs["start_new_session"])
        self.assertIsNone(fake._pending_update)


if __name__ == "__main__":
    unittest.main()
