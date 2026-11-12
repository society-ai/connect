"""An agent deleted on Society AI leaves this computer, keeping the owner's files.

- remove_persona: the key file and its backups, the LaunchAgent and its
  backups and a named persona's cache folder go; WORK_DIR stays; a tombstone
  says what happened; the service stops last (detached from the bridge).
- It removes nothing when the key file names another agent, and never the
  default agent's shared cache folder.
- The bridge: agent.removed for this agent (or a key refused with
  X-Society-Agent-Status: deleted) removes it and stops; agent.removed for
  another agent is ignored.
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
from core import removal  # noqa: E402
from core.bridge import AgentDeletedError, AuthError, Bridge  # noqa: E402


class _Machine(unittest.TestCase):
    """A fake repo and home folder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.repo = root / "repo"
        self.home = root / "home"
        self.repo.mkdir()
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        (self.home / ".cache" / "society-ai").mkdir(parents=True)
        self.patches = [
            mock.patch.object(removal, "REPO_DIR", self.repo),
            mock.patch.object(pathlib.Path, "home", return_value=self.home),
            mock.patch.object(removal, "stop_service", return_value=True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def persona(self, name, agent, work_dir="/Users/x/Society AI/searchpro"):
        env = self.repo / (f".env.{name}" if name else ".env")
        env.write_text(f"AGENT_NAME={agent}\nSOCIETY_AI_AUTH_TOKEN=sai_secret\nWORK_DIR=\"{work_dir}\"\n")
        label = f"{removal.BASE_LABEL}.{name}" if name else removal.BASE_LABEL
        plist = self.home / "Library" / "LaunchAgents" / f"{label}.plist"
        plist.write_text("<plist/>")
        return env, plist


class RemovePersona(_Machine):
    def test_removes_the_agent_and_keeps_its_folder(self):
        env, plist = self.persona("searchpro", "searchpro")
        env_bak = self.repo / ".env.searchpro.bak-1794173301"
        env_bak.write_text("SOCIETY_AI_AUTH_TOKEN=sai_old\n")
        plist_bak = plist.with_name(plist.name + ".bak-1794173301")
        plist_bak.write_text("<plist/>")
        cache = self.home / ".cache" / "society-ai" / "searchpro"
        cache.mkdir()
        (cache / "bridge.log").write_text("log")
        other_env, other_plist = self.persona("buzz", "buzz")

        result = removal.remove_persona("searchpro", reason=removal.DELETED_ON_SOCIETY_AI, from_bridge=True,
                                        expected_agent="searchpro")

        for path in (env, env_bak, plist, plist_bak, cache):
            self.assertFalse(path.exists(), path)
        self.assertTrue(other_env.exists() and other_plist.exists())
        self.assertEqual(result["kept"], ["/Users/x/Society AI/searchpro"])
        removal.stop_service.assert_called_once_with(f"{removal.BASE_LABEL}.searchpro", detach=True)
        [stone] = json.loads((self.home / ".cache" / "society-ai" / "removed.json").read_text())
        self.assertEqual((stone["id"], stone["agent_name"], stone["reason"]),
                         ("searchpro", "searchpro", "deleted_on_society_ai"))

    def test_another_agents_key_file_is_never_removed(self):
        env, plist = self.persona("", "jenkins")
        with self.assertRaises(removal.RemovalError):
            removal.remove_persona("", reason=removal.DELETED_ON_SOCIETY_AI, expected_agent="searchpro")
        self.assertTrue(env.exists() and plist.exists())
        removal.stop_service.assert_not_called()

    def test_the_default_agent_keeps_the_shared_cache_folder(self):
        env, plist = self.persona("", "jenkins")
        shared = self.home / ".cache" / "society-ai" / "status-token"
        shared.write_text("t")
        removal.remove_persona("", reason=removal.REMOVED_HERE)
        self.assertFalse(env.exists() or plist.exists())
        self.assertTrue(shared.exists())
        removal.stop_service.assert_called_once_with(removal.BASE_LABEL, detach=False)

    def test_unknown_or_invalid_persona(self):
        with self.assertRaises(removal.RemovalError):
            removal.remove_persona("nobody", reason=removal.REMOVED_HERE)
        with self.assertRaises(removal.RemovalError):
            removal.remove_persona("../etc", reason=removal.REMOVED_HERE)

    def test_dismiss(self):
        self.persona("searchpro", "searchpro")
        removal.remove_persona("searchpro", reason=removal.REMOVED_HERE)
        self.assertTrue(removal.dismiss_tombstone("searchpro"))
        self.assertEqual(removal.read_tombstones(), [])
        self.assertFalse(removal.dismiss_tombstone("searchpro"))

    def test_connect_remove_asks_first(self):
        env, _ = self.persona("searchpro", "searchpro")
        with mock.patch("builtins.input", return_value="n"), mock.patch("builtins.print"):
            self.assertEqual(removal.main(["searchpro"]), 1)
        self.assertTrue(env.exists())
        with mock.patch("builtins.print"):
            self.assertEqual(removal.main(["searchpro", "--yes"]), 0)
        self.assertFalse(env.exists())


def _bridge(name="searchpro"):
    b = Bridge.__new__(Bridge)
    b.ctx = types.SimpleNamespace(name=name)
    b.stop = mock.Mock()
    return b


class BridgeRemovesItself(unittest.TestCase):
    def test_agent_removed_for_this_agent(self):
        b = _bridge()
        with mock.patch.dict(os.environ, {"SOCIETY_AI_PERSONA": "searchpro"}), \
                mock.patch.object(removal, "remove_persona",
                                  return_value={"removed": [], "kept": []}) as remove:
            b.handle_agent_removed({"agent_name": "searchpro", "reason": "deleted"})
        remove.assert_called_once_with("searchpro", reason=removal.DELETED_ON_SOCIETY_AI, from_bridge=True,
                                       expected_agent="searchpro")
        b.stop.assert_called_once()

    def test_agent_removed_for_another_agent_is_ignored(self):
        b = _bridge()
        with mock.patch.object(removal, "remove_persona") as remove:
            b.handle_agent_removed({"agent_name": "jenkins"})
        remove.assert_not_called()
        b.stop.assert_not_called()

    def test_a_failed_removal_still_stops(self):
        b = _bridge()
        with mock.patch.object(removal, "remove_persona", side_effect=removal.RemovalError("x")):
            b.handle_agent_removed({"agent_name": "searchpro"})
        b.stop.assert_called_once()


class _Resp:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = "refused"


def _exchange(resp):
    client = mock.MagicMock()
    client.__aenter__ = mock.AsyncMock(return_value=client)
    client.__aexit__ = mock.AsyncMock(return_value=False)
    client.post = mock.AsyncMock(return_value=resp)
    with mock.patch.object(bridge_mod.httpx, "AsyncClient", return_value=client):
        return asyncio.run(bridge_mod.exchange_api_key_for_jwt("sai_x", "https://api.x"))


class RefusedKey(unittest.TestCase):
    def test_a_deleted_agents_key(self):
        with self.assertRaises(AgentDeletedError):
            _exchange(_Resp(401, {"X-Society-Agent-Status": "deleted"}))

    def test_any_other_refused_key_is_not_deleted(self):
        with self.assertRaises(AuthError) as e:
            _exchange(_Resp(401))
        self.assertNotIsInstance(e.exception, AgentDeletedError)


if __name__ == "__main__":
    unittest.main()
