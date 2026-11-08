"""The society-ai MCP server: the shared tool catalog as the bound agent, plus the local tools.

- The catalog (Society Connect) lists and runs every tool the agent may use, with its own key.
- Local tools (switch_agent, upload_file, download_file: the ones that need this machine) follow the agent's
  permissions from /api/v1/agents/me.
- Catalog calls get the session's chat and the agent's company filled in when the tool takes them.
"""

import asyncio
import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import catalog_client  # noqa: E402
from core import mcp_server  # noqa: E402

CHAT = "5d86bae8-0000-4000-8000-00000000000c"
COMPANY = "5d86bae8-0000-4000-8000-00000000000d"
REGULAR = ["network.use", "inbox.use", "tasks.own", "files.use", "groups.use"]
CATALOG_TOOLS = [
    {"name": "list_tasks", "description": "List tasks.", "inputSchema": {"type": "object", "properties": {
        "board": {"type": "string"}, "chat_id": {"type": "string"}}}, "annotations": {"readOnlyHint": True}},
    {"name": "list_company_agents", "description": "Company agents.", "inputSchema": {"type": "object", "properties": {
        "company_id": {"type": "string"}, "chat_id": {"type": "string"}}, "required": ["company_id"]}},
    {"name": "send_group_message", "description": "Post in a group.", "inputSchema": {"type": "object", "properties": {
        "group_id": {"type": "string"}, "text": {"type": "string"}, "chat_id": {"type": "string"}}}},
]


def ident(name="jenkins", bound=True, company_id=""):
    return SimpleNamespace(api_url="https://api.societyai.com", token="sai_jenkins" if bound else "",
                           company_id=company_id, bound=bound, name=name, display_name="")


def parsed(result: str) -> dict:
    return json.loads(result.split("\n", 1)[1])


class Catalog:
    """A fake Society Connect: answers tools/list and tools/call, records what it was sent."""

    def __init__(self, status=200):
        self.status = status
        self.sent = []

    async def post(self, url, json=None, headers=None, timeout=None):
        self.sent.append({"url": url, "body": json, "headers": headers})
        if self.status != 200:
            return httpx.Response(self.status, text="nope")
        if json["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": CATALOG_TOOLS}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": '{"items": [], "count": 0}'}], "isError": False}})


class ProxyTest(unittest.TestCase):
    def setUp(self):
        mcp_server._permissions.clear()
        catalog_client.forget()
        self.catalog = Catalog()
        self.who = ident()
        me = {"agent_name": "jenkins", "permissions": REGULAR}
        self.patches = [
            mock.patch.object(mcp_server, "_ident", side_effect=lambda: self.who),
            mock.patch.object(catalog_client.identity, "current", side_effect=lambda: self.who),
            mock.patch.object(catalog_client.api, "client", return_value=self.catalog),
            mock.patch.object(mcp_server.api, "get", new=mock.AsyncMock(return_value=me)),
            mock.patch.object(mcp_server.chat_context, "current_chat_id", new=mock.AsyncMock(return_value=CHAT)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        catalog_client.forget()

    def names(self):
        return [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]

    def call(self, name, args):
        return asyncio.run(mcp_server.mcp.call_tool(name, args))


class ListsTheCatalog(ProxyTest):
    def test_local_tools_then_the_agents_catalog(self):
        names = self.names()
        self.assertEqual(names[:3], ["switch_agent", "upload_file", "download_file"])
        self.assertEqual(names[3:], ["list_tasks", "list_company_agents", "send_group_message"])
        sent = self.catalog.sent[0]
        self.assertEqual(sent["url"], "https://mcp.societyai.com/mcp")
        self.assertEqual(sent["headers"]["Authorization"], "Bearer sai_jenkins")

    def test_the_list_is_reused_until_the_agent_switches(self):
        self.names()
        self.names()
        self.assertEqual(len(self.catalog.sent), 1)
        catalog_client.forget()
        self.names()
        self.assertEqual(len(self.catalog.sent), 2)

    def test_an_unbound_session_shows_only_the_local_tools(self):
        self.who = ident(bound=False)
        self.assertNotIn("list_tasks", self.names())
        self.assertEqual(self.catalog.sent, [])

    def test_local_tools_follow_the_permissions(self):
        with mock.patch.object(mcp_server.api, "get", new=mock.AsyncMock(return_value={"permissions": ["tasks.own"]})):
            names = self.names()
        self.assertNotIn("upload_file", names)
        self.assertIn("switch_agent", names)

    def test_an_unreachable_catalog_still_lists_the_local_tools(self):
        self.catalog.status = 502
        self.assertEqual(self.names(), ["switch_agent", "upload_file", "download_file"])


class CallsTheCatalog(ProxyTest):
    def test_a_call_runs_as_the_agent_with_the_sessions_chat(self):
        result = self.call("list_tasks", {"board": "mine"})
        call = self.catalog.sent[-1]["body"]
        self.assertEqual(call["method"], "tools/call")
        self.assertEqual(call["params"], {"name": "list_tasks", "arguments": {"board": "mine", "chat_id": CHAT}})
        self.assertFalse(result.isError)
        self.assertEqual(result.content[0].text, "[acting as jenkins @ https://api.societyai.com]")

    def test_the_agents_company_is_filled_in(self):
        self.who = ident(company_id=COMPANY)
        self.call("list_company_agents", {})
        self.assertEqual(self.catalog.sent[-1]["body"]["params"]["arguments"]["company_id"], COMPANY)
        self.call("list_company_agents", {"company_id": "5d86bae8-0000-4000-8000-00000000000e"})
        self.assertEqual(self.catalog.sent[-1]["body"]["params"]["arguments"]["company_id"],
                         "5d86bae8-0000-4000-8000-00000000000e")

    def test_a_tool_that_takes_no_chat_gets_none(self):
        self.catalog.sent.clear()
        with mock.patch.object(catalog_client, "list_tools", new=mock.AsyncMock(return_value=[
            {"name": "check", "inputSchema": {"properties": {}}}])):
            self.call("check", {})
        self.assertEqual(self.catalog.sent[-1]["body"]["params"]["arguments"], {})

    def test_a_refused_key_is_an_error_result_not_a_crash(self):
        self.names()
        self.catalog.status = 401
        result = self.call("list_tasks", {})
        self.assertTrue(result.isError)
        self.assertIn("revoked", result.content[0].text)


if __name__ == "__main__":
    unittest.main()
