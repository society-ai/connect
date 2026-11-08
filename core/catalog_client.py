"""The Society AI tool catalog, called as the bound agent.

Every agent type shares one tool catalog, served by Society Connect
(mcp.societyai.com, A2A society_connect/catalog.py). A cloud agent such as
Grok Bot reaches it through OAuth; this bridge reaches it with the bound
agent's own agent key, so a session sees exactly the tools its agent may use
and a new tool needs no bridge release (docs/design/connect-open-repo.md,
step C3).

The catalog is a stateless MCP server answering JSON, so each call is one
HTTP POST of a JSON-RPC request.
"""

from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core import api
from core import identity

PROTOCOL_VERSION = "2025-11-25"
# How long a tool list is reused before asking again. switch_agent clears it;
# a change the owner makes on the platform shows within this.
LIST_TTL_S = 60.0
CALL_TIMEOUT_S = 120.0

_lists: dict[str, tuple[float, list[dict]]] = {}


class CatalogError(Exception):
    """The catalog could not be reached or refused the request."""


def catalog_url(api_url: str) -> str:
    """Where the catalog is for an agent on this router: SOCIETY_AI_CONNECT_URL
    if set, else the router's host with api. replaced by mcp. (production:
    https://api.societyai.com -> https://mcp.societyai.com/mcp), and port 8090
    for a router on port 8000 (local development)."""
    explicit = os.environ.get("SOCIETY_AI_CONNECT_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    parts = urlsplit(api_url.rstrip("/"))
    host = parts.hostname or ""
    if host.startswith("api."):
        host = "mcp." + host[len("api."):]
    netloc = host
    if parts.port:
        netloc += f":{8090 if parts.port == 8000 else parts.port}"
    return urlunsplit((parts.scheme, netloc, "/mcp", "", ""))


async def _rpc(method: str, params: dict | None = None, *, timeout: float = 30.0) -> dict:
    ident = identity.current()
    if not ident.bound or not ident.token:
        raise CatalogError("no Society AI agent is bound to this session; call switch_agent first")
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    headers = {
        "Authorization": f"Bearer {ident.token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    try:
        resp = await api.client().post(catalog_url(ident.api_url), json=body, headers=headers, timeout=timeout)
    except Exception as e:  # noqa: BLE001 (network errors of every kind)
        raise CatalogError(f"Society AI tools are unreachable: {e}") from e
    if resp.status_code == 401:
        raise CatalogError("Society AI refused this agent's key: it may have been revoked. Reconnect the agent.")
    if resp.status_code != 200:
        raise CatalogError(f"Society AI tools answered {resp.status_code}: {resp.text[:300]}")
    try:
        data = resp.json()
    except ValueError as e:
        raise CatalogError("Society AI tools sent an answer that is not JSON") from e
    if data.get("error"):
        raise CatalogError(str(data["error"].get("message") or data["error"]))
    return data.get("result") or {}


async def list_tools() -> list[dict]:
    """The bound agent's tools, as the catalog lists them for it."""
    name = identity.current().name
    hit = _lists.get(name)
    if hit is not None and time.monotonic() - hit[0] < LIST_TTL_S:
        return hit[1]
    tools = (await _rpc("tools/list")).get("tools") or []
    _lists[name] = (time.monotonic(), tools)
    return tools


def forget() -> None:
    """Drop the cached lists (the session switched agents)."""
    _lists.clear()


async def call_tool(name: str, arguments: dict) -> dict:
    """Run a catalog tool as the bound agent. Returns the MCP CallToolResult."""
    return await _rpc("tools/call", {"name": name, "arguments": arguments}, timeout=CALL_TIMEOUT_S)
