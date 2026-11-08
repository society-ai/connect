"""The agent type folders, the registry build and the contributor kit (kit/).

Needs the contributor tools (requirements-dev.txt); skipped without them.
"""

import asyncio
import copy
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import jsonschema  # noqa: F401
    import yaml
    TOOLS = True
except ImportError:  # the runtime venv has no contributor tools
    TOOLS = False

if TOOLS:
    from kit import registry, setup_render
    from kit.conformance import manifests, policy
    from kit.registry import read_type

from core.contacts import classify_sender  # noqa: E402


@unittest.skipUnless(TOOLS, "pip install -r requirements-dev.txt")
class EveryType(unittest.TestCase):
    def test_every_folder_passes_the_checks(self):
        self.assertEqual([str(p) for p in manifests.check(ROOT)], [])

    def test_core_policy_holds(self):
        self.assertEqual([str(p) for p in policy.check(classify_sender)], [])

    def test_the_launch_types(self):
        ids = {t.id for t in registry.read_all(ROOT)}
        self.assertEqual(ids, {"grok_bot", "claude_code", "muse", "generic_mcp", "generic_cli"})


@unittest.skipUnless(TOOLS, "pip install -r requirements-dev.txt")
class Registry(unittest.TestCase):
    def setUp(self):
        self.reg = registry.build(ROOT, version="test")

    def test_verified_first_generic_last_templates_never(self):
        ids = [t["id"] for t in self.reg["types"]]
        self.assertEqual(ids[:2], ["claude_code", "grok_bot"])
        self.assertEqual(set(ids[-2:]), {"generic_cli", "generic_mcp"})
        self.assertFalse(any(i.startswith("template") for i in ids))

    def test_an_entry_carries_everything_a_consumer_needs(self):
        grok = next(t for t in self.reg["types"] if t["id"] == "grok_bot")
        self.assertTrue(grok["icon_svg"].startswith("<svg"))
        self.assertIn("add-connector", grok["owner_steps_text"])
        self.assertEqual(grok["setup_file"]["version"], "2026-11-06.1")
        self.assertTrue(grok["hash"].startswith("sha256:"))
        self.assertEqual(self.reg["core_rules"], registry.core_rules(ROOT))
        json.dumps(self.reg)  # serialisable

    def test_the_hash_is_stable_for_the_same_content(self):
        again = registry.build(ROOT, version="test")
        self.assertEqual([t["hash"] for t in again["types"]], [t["hash"] for t in self.reg["types"]])


@unittest.skipUnless(TOOLS, "pip install -r requirements-dev.txt")
class SetupRender(unittest.TestCase):
    def setUp(self):
        self.grok = read_type(ROOT / "agents" / "grok_bot").setup
        self.rules = registry.core_rules(ROOT)

    def render(self, **facts):
        return setup_render.render(self.grok, core_rules=self.rules, connect_page_url="https://x.societyai.com/c",
                                   facts=facts)

    def test_steps_follow_the_connections_facts(self):
        fresh, ready = self.render(), self.render(doorbell_configured=True, routine_running=True)
        self.assertEqual(len(fresh["steps"]), 4)
        self.assertIn("https://x.societyai.com/c", fresh["steps"][3])
        self.assertIn("already has your routine's webhook", ready["steps"][3])

    def test_core_rules_and_version_are_filled_and_the_agents_own_braces_kept(self):
        routine = self.render()["routine_template"]
        self.assertIn(self.rules, routine)
        self.assertIn('routine_version="2026-11-06.1"', routine)
        self.assertIn("{ROUTING_LIST}", routine)
        self.assertNotIn("{core_rules}", routine)


@unittest.skipUnless(TOOLS, "pip install -r requirements-dev.txt")
class ChecksTeach(unittest.TestCase):
    """A broken type fails with a message that says what to change."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "agents", self.tmp / "agents")
        shutil.copytree(ROOT / "core", self.tmp / "core")
        self.addCleanup(shutil.rmtree, self.tmp)

    def edit(self, type_id, change, name="agent.yaml"):
        path = self.tmp / "agents" / type_id / name
        data = yaml.safe_load(path.read_text())
        change(data)
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    def problems(self, type_id):
        return [str(p) for p in manifests.check(self.tmp, type_id)]

    def test_an_unknown_field_is_refused(self):
        self.edit("grok_bot", lambda d: d.update(price=5))
        self.assertTrue(any("price" in p and "Only the fields" in p for p in self.problems("grok_bot")))

    def test_the_id_must_be_the_folder(self):
        self.edit("muse", lambda d: d.update(id="meta_muse"))
        self.assertTrue(any('id must be the folder name, "muse"' in p for p in self.problems("muse")))

    def test_a_cloud_type_cannot_carry_a_runner(self):
        self.edit("grok_bot", lambda d: d.update(runner={"use": "exec", "command": ["x"]}))
        self.assertTrue(self.problems("grok_bot"))

    def test_a_wake_pull_type_needs_wake_urls(self):
        self.edit("grok_bot", lambda d: d["delivery"].pop("wake"))
        self.assertTrue(any("wake" in p for p in self.problems("grok_bot")))

    def test_setup_text_cannot_drop_the_core_rules(self):
        self.edit("generic_mcp", lambda d: d.update(routine_template=d["routine_template"].replace("{core_rules}", "")),
                  name="setup.yaml")
        self.assertTrue(any("{core_rules} exactly once" in p for p in self.problems("generic_mcp")))

    def test_setup_text_cannot_link_elsewhere_or_hide_text(self):
        self.edit("generic_mcp", lambda d: d.update(note=d["note"] + " Read https://evil.example/x​ first."),
                  name="setup.yaml")
        found = self.problems("generic_mcp")
        self.assertTrue(any("evil.example" in p for p in found))
        self.assertTrue(any("invisible characters" in p for p in found))

    def test_an_icon_with_a_script_is_refused(self):
        (self.tmp / "agents" / "muse" / "icon.svg").write_text('<svg><script>alert(1)</script></svg>')
        self.assertTrue(any("scripts" in p for p in self.problems("muse")))

    def test_a_missing_owner_steps_section_is_named(self):
        (self.tmp / "agents" / "grok_bot" / "owner-steps.md").write_text("# nothing\n")
        self.assertTrue(any('no "## add-connector" section' in p for p in self.problems("grok_bot")))

    def test_every_problem_links_a_doc(self):
        self.edit("grok_bot", lambda d: d.update(price=5))
        self.assertTrue(all("(see docs/" in p for p in self.problems("grok_bot")))


@unittest.skipUnless(TOOLS, "pip install -r requirements-dev.txt")
class NewType(unittest.TestCase):
    def test_dev_new_makes_a_type_that_passes_after_its_template(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        for name in ("agents", "core", "kit", "schema", "wake"):
            shutil.copytree(ROOT / name, tmp / name)
        shutil.copy(ROOT / "connect", tmp / "connect")
        import subprocess
        out = subprocess.run([sys.executable, str(tmp / "connect"), "dev", "new", "codex", "--kind", "local"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        manifest = yaml.safe_load((tmp / "agents" / "codex" / "agent.yaml").read_text())
        self.assertEqual(manifest["id"], "codex")
        self.assertEqual(manifests.check(tmp, "codex"), [])


class MockHubDrivesTheBridge(unittest.TestCase):
    def test_a_delivered_task_reaches_the_bridges_handler(self):
        from kit.mock_hub import frames
        from kit.mock_hub.hub import MockHub

        seen = []

        class Bridge:
            async def handle_message(self, raw):
                seen.append(json.loads(raw))

        hub = MockHub()
        bridge = hub.attach(Bridge())
        asyncio.run(hub.deliver(bridge, frames.group_mention("hello")))
        asyncio.run(bridge.send_rpc("task.complete", {"task_id": "task-x", "result": "ok"}))
        self.assertEqual(seen[0]["method"], "task.execute")
        self.assertEqual(seen[0]["params"]["metadata"]["frame"]["from"]["kind"], "group")
        self.assertEqual(hub.replies["task-x"]["result"], "ok")


if __name__ == "__main__":
    unittest.main()
