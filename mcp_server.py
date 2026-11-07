"""Society AI MCP server for Claude Code: the shared tool catalog, plus the tools that must run here.

Every agent type uses one tool catalog, served by Society Connect (A2A
society_connect/catalog.py). This server lists and calls it as the bound
agent (catalog_client.py), so a Claude Code session sees the same tools as
any other agent of its owner's, cut to what the agent may do, and a new tool
needs no bridge release (docs/design/connect-open-repo.md, step C3).

The tools defined here are the ones that need this machine:

  switch_agent   bind the session to another agent on this machine
  upload_file    publish a file from this machine's disk
  download_file  save a Society AI file onto this machine's disk

Catalog calls get the session's chat (chat_id) and the agent's company
(company_id, from COMPANY_ID) filled in when the tool takes them and the call
does not say. Every result starts with the agent it ran as, so a wrong
binding is visible on the first read.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import httpx
import mcp.types as types
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

import api
import attachments
import catalog_client
import chat_context
import identity

_ident = identity.current

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_VALID_PIN_ENTITY_TYPES = {"company", "space", "project", "task"}


def _error(message: str, status: int | None = None, body: str | None = None) -> dict[str, Any]:
    return api.error(message, status=status, body=body)

def _label() -> str:
    ident = _ident()
    return f"[acting as {ident.name} @ {ident.api_url}]" if ident.bound else "[no agent bound]"


def _result(data: Any) -> str:
    """Serialize a success or error dict for MCP transport, after the acting identity."""
    return _label() + "\n" + json.dumps(data, indent=2, default=str)


def _scope_company_id(company_id: Optional[str]) -> str:
    """The company a company-optional tool works in: the one passed, else
    COMPANY_ID, else none (personal scope). company_id="personal" means
    personal scope even when COMPANY_ID is set. Validate the result."""
    if (company_id or "").strip().lower() == "personal":
        return ""
    return (company_id or _ident().company_id or "").strip()


def _validate_uuid(value: str, name: str) -> str:
    if not isinstance(value, str) or not _UUID_RE.match(value):
        raise ValueError(f"{name} must be a valid UUID, got: {value!r}")
    return value

# The platform permission each local tool needs (the catalog's tools carry their own).
LOCAL_PERMISSIONS: dict[str, str] = {
    "upload_file": "files.use", "download_file": "files.use",
}

# The bound agent's permissions on the platform, per agent name (None: not
# known, so every local tool is shown and the platform decides). Read from
# /api/v1/agents/me; cleared when switch_agent changes the binding.
_permissions: dict[str, frozenset] = {}


async def _agent_permissions() -> Optional[frozenset]:
    ident = _ident()
    if not ident.bound or not ident.name:
        return None
    if ident.name in _permissions:
        return _permissions[ident.name]
    me = await api.get("/api/v1/agents/me")
    if not isinstance(me, dict) or me.get("error") or not isinstance(me.get("permissions"), list):
        return None  # not cached: a passing network error must not stick
    _permissions[ident.name] = frozenset(me["permissions"])
    return _permissions[ident.name]


def _allowed(tool: str, permissions: Optional[frozenset]) -> bool:
    needed = LOCAL_PERMISSIONS.get(tool)
    return permissions is None or not needed or needed in permissions


def _fill(tool: dict, arguments: dict, chat_id: str) -> dict:
    """The arguments with the session's chat and the agent's company added where the tool takes them and the
    call left them out."""
    args = dict(arguments or {})
    props = (tool.get("inputSchema") or {}).get("properties") or {}
    if "chat_id" in props and not args.get("chat_id") and chat_id:
        args["chat_id"] = chat_id
    company = _ident().company_id
    if "company_id" in props and not args.get("company_id") and company and _UUID_RE.match(company):
        args["company_id"] = company
    return args


class SocietyMCP(FastMCP):
    """The local tools, then the bound agent's catalog tools."""

    async def list_tools(self):
        permissions = await _agent_permissions()
        local = [t for t in await super().list_tools() if _allowed(t.name, permissions)]
        names = {t.name for t in local}
        if not _ident().bound:
            return local
        try:
            remote = await catalog_client.list_tools()
        except catalog_client.CatalogError:
            return local  # the call itself explains when a catalog tool cannot be reached
        return local + [
            types.Tool(
                name=t["name"], description=t.get("description"), inputSchema=t.get("inputSchema") or {"type": "object"},
                annotations=types.ToolAnnotations(**t["annotations"]) if t.get("annotations") else None,
            )
            for t in remote if t.get("name") and t["name"] not in names
        ]

    async def call_tool(self, name, arguments):
        if self._tool_manager.get_tool(name) is not None:
            if not _allowed(name, await _agent_permissions()):
                raise ToolError(
                    f"Your owner has not allowed you {name} yet. They decide what you may do in "
                    "Society AI, on your page (What it can do), or by making you their Lead agent."
                )
            token = chat_context.current_tool.set(name)
            try:
                return await super().call_tool(name, arguments)
            finally:
                chat_context.current_tool.reset(token)
        try:
            tools = {t["name"]: t for t in await catalog_client.list_tools()}
            args = _fill(tools.get(name) or {}, arguments, await chat_context.current_chat_id())
            result = await catalog_client.call_tool(name, args)
        except catalog_client.CatalogError as e:
            return types.CallToolResult(content=[types.TextContent(type="text", text=f"{_label()}\n{e}")], isError=True)
        content = [types.TextContent(type="text", text=_label())] + [
            types.TextContent(type="text", text=c.get("text", "")) if c.get("type") == "text" else c
            for c in result.get("content") or []
        ]
        return types.CallToolResult(content=content, isError=bool(result.get("isError")))


mcp = SocietyMCP("society-ai", instructions="Society AI tools for Claude Code")


@mcp.tool()
async def switch_agent(agent_name: str = "", ctx: Optional[Context] = None) -> str:
    """Switch which Society AI agent this session acts as, or list the
    identities available on this machine.

    Every session starts bound to one agent (shown in the session-start
    banner and prefixed on every tool result). When the user asks to act as
    a different agent, e.g. "act as agent-8u6qy3ba", call this with that
    name. All later Society AI tools run as the new agent, with its tools.

    Args:
        agent_name: Persona to switch to: canonical id (agent-8u6qy3ba) OR
            the user-given display name (e.g. "kilo"), case-insensitive.
            Empty: list available identities and the current binding
            without switching.
    """
    ident = _ident()
    available = [
        {"name": p["name"], "display_name": p.get("display_name", ""), "api_url": p["api_url"]}
        for p in identity.personas()
    ]
    if not agent_name.strip():
        return _result({"acting_as": ident.name, "display_name": ident.display_name, "api_url": ident.api_url,
                         "available": available})
    wanted = agent_name.strip()
    if wanted == ident.name or (ident.display_name and wanted.lower() == ident.display_name.lower()):
        return _result({"acting_as": ident.name, "display_name": ident.display_name, "api_url": ident.api_url,
                        "unchanged": True})
    try:
        new = identity.bind(wanted)
    except ValueError as e:
        return _result(_error(str(e)))

    # /api/v1/agents/me proves the key works and returns the platform's current display name and permissions.
    check = await api.get("/api/v1/agents/me")
    verified = isinstance(check, dict) and not check.get("error")
    if verified:
        new = identity.refresh_display_name(check.get("display_name", ""))
    _permissions.pop(new.name, None)
    if verified and isinstance(check.get("permissions"), list):
        _permissions[new.name] = frozenset(check["permissions"])
    catalog_client.forget()
    if ctx is not None:
        try:
            await ctx.session.send_tool_list_changed()
        except Exception:
            pass
    out: dict[str, Any] = {"switched_to": new.name, "display_name": new.display_name, "api_url": new.api_url,
                           "verified": verified}
    if not verified:
        out["warning"] = "switched, but a test API call failed: the key may be stale or the platform unreachable"
        out["check"] = check
    return _result(out)


@mcp.tool()
async def upload_file(
    file_path: str,
    mime_type: str,
    name: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    company_id: Optional[str] = None,
    space_id: Optional[str] = None,
    project_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    cache_key: Optional[str] = None,
    pin_to_entity_type: Optional[str] = None,
    pin_to_entity_id: Optional[str] = None,
    pin_to_label: Optional[str] = None,
) -> str:
    """Publish a local file as a Society AI artifact (S3-backed, presigned URL).

    Files can be up to 10 MB, videos (mp4, webm, mov) up to 100 MB. The file
    goes straight from disk to storage: agent_router's
    `/api/v1/artifacts/upload-url` (with the agent's normal sai_ token)
    returns a link signed for its type and size, the file is PUT there, and
    `/api/v1/artifacts/complete` records it. No service key is involved.

    Args:
        file_path: Absolute path to a local file.
        mime_type: MIME type of the file (e.g. "application/json").
        name: Filename (1-255 chars).
        title: Optional display title.
        description: Optional description.
        company_id: Optional company UUID for scoping.
        space_id, project_id, chat_id: Optional scope UUIDs.
        cache_key: If set with company_id, creates/replaces a stable
            (company_id, cache_key) artifact (the Peirot pattern).
            Subsequent uploads with the same key replace the file
            in place.
        pin_to_entity_type: When set with pin_to_entity_id, also pins the
            created artifact to that entity in the same call (saves a
            follow-up pin_file call). One of: 'company', 'space',
            'project', 'task'.
        pin_to_entity_id: UUID of the entity to pin to.
        pin_to_label: Optional display label for the pin.
    """
    if not file_path or not os.path.isfile(file_path):
        return _result(_error(f"file_path not found or not a file: {file_path}"))
    if not mime_type or not mime_type.strip():
        return _result(_error("mime_type is required"))
    if not name or not name.strip() or len(name) > 255:
        return _result(_error("name is required and must be 1-255 chars"))
    if cache_key:
        if not _scope_company_id(company_id):
            return _result(_error("cache_key requires company_id (also reads COMPANY_ID env)"))
        if not (1 <= len(cache_key) <= 255):
            return _result(_error("cache_key must be 1-255 chars"))
    for u_name, u_val in (
        ("company_id", _scope_company_id(company_id)),
        ("space_id", space_id),
        ("project_id", project_id),
        ("chat_id", chat_id),
    ):
        if u_val:
            try:
                _validate_uuid(u_val, u_name)
            except ValueError as e:
                return _result(_error(str(e)))
    if pin_to_entity_type or pin_to_entity_id:
        if not (pin_to_entity_type and pin_to_entity_id):
            return _result(_error(
                "pin_to_entity_type and pin_to_entity_id must be set together"
            ))
        if pin_to_entity_type not in _VALID_PIN_ENTITY_TYPES:
            return _result(_error(
                f"pin_to_entity_type must be one of {sorted(_VALID_PIN_ENTITY_TYPES)}"
            ))
        try:
            _validate_uuid(pin_to_entity_id, "pin_to_entity_id")
        except ValueError as e:
            return _result(_error(str(e)))

    try:
        size = os.path.getsize(file_path)
    except OSError as e:
        return _result(_error(f"Could not read {file_path}: {e}"))
    if size == 0:
        return _result(_error(f"{file_path} is empty"))

    body: dict[str, Any] = {
        "name": name.strip(),
        "mime_type": mime_type.strip(),
        "source_type": "agent",
        "source_agent": _ident().name,
    }
    if title is not None:
        body["title"] = title
    if description is not None:
        body["description"] = description
    cid = _scope_company_id(company_id)
    if cid:
        body["company_id"] = cid
    if space_id:
        body["space_id"] = space_id
    if project_id:
        body["project_id"] = project_id
    if chat_id:
        body["chat_id"] = chat_id
    if cache_key:
        body["cache_key"] = cache_key
    if pin_to_entity_type and pin_to_entity_id:
        pin: dict[str, Any] = {
            "entity_type": pin_to_entity_type,
            "entity_id": pin_to_entity_id,
        }
        if pin_to_label:
            pin["label"] = pin_to_label
        body["pin_to"] = pin

    # 1. A link signed for exactly this type and size (the router checks the
    #    type, the size, the scopes and the owner's storage first).
    link = await api.post("/api/v1/artifacts/upload-url", {**body, "size": size})
    if not isinstance(link, dict) or link.get("error") or not link.get("upload_url"):
        return _result(link)
    # 2. The file, from disk to storage.
    stored = await _put_file(link, file_path, size)
    if stored is not None:
        return _result(stored)
    # 3. Recorded (measured in storage, then saved like any other artifact).
    return _result(await api.post("/api/v1/artifacts/complete", {
        **body, "artifact_id": link["artifact_id"], "file_key": link["file_key"],
    }))


# A 100 MB video on a slow uplink.
_UPLOAD_TIMEOUT = 600.0


async def _put_file(link: dict, file_path: str, size: int) -> Optional[dict]:
    """Stream a file to an upload link with the headers it was signed for.
    None when stored, else an error dict."""
    async def chunks():
        with open(file_path, "rb") as f:
            while block := f.read(1024 * 1024):
                yield block

    headers = {**(link.get("headers") or {}), "Content-Length": str(size)}
    try:
        resp = await api.client().put(link["upload_url"], content=chunks(), headers=headers, timeout=_UPLOAD_TIMEOUT)
    except httpx.HTTPError as e:
        return _error(f"Uploading {os.path.basename(file_path)} failed: {e}")
    if resp.status_code >= 300:
        return _error(f"Storage refused the upload of {os.path.basename(file_path)} (HTTP {resp.status_code}). Try again.")
    return None


def _files_dir() -> str:
    """Where download_file saves: the agent's work folder (injected by the bridge
    into the sessions it launches), else the session's own folder."""
    return os.path.join(os.environ.get("SOCIETY_AI_WORK_DIR") or os.getcwd(), attachments.FILES_DIRNAME)


@mcp.tool()
async def download_file(artifact_id: str) -> str:
    """Download a Society AI file (an artifact) into this agent's folder.

    Works for files this agent produced, files sent to it in a message, and
    files in workspaces (companies, spaces, projects) the agent belongs to.
    Files that arrive with a message are already downloaded and listed under
    [Attachments], so use this for other files: ones named in a task result,
    an older message, or found with list_files.

    Saves into the agent's work folder under files/ and returns the local
    path. Open images with the Read tool to see them. Files over 50 MB are
    not downloaded: the result carries a 10-minute download link instead.

    Args:
        artifact_id: The file's artifact id.
    """
    try:
        _validate_uuid(artifact_id, "artifact_id")
    except ValueError as e:
        return _result(_error(str(e)))
    ident = _ident()
    try:
        info = await attachments.file_info(
            api.client(), ident.api_url, ident.token, artifact_id,
            await chat_context.request_headers(),
        )
    except PermissionError:
        return _result(_error("No such file, or this agent cannot open it.", status=404))
    except Exception as e:
        return _result(_error(f"Could not look up the file: {e}"))
    out = attachments.describe(info)
    size = int(info.get("size") or 0)
    if size > attachments.MAX_FILE_BYTES:
        out.update(url=info.get("url"), url_expires_at=info.get("url_expires_at"),
                   note="Over 50 MB, not downloaded. The link expires in about 10 minutes.")
        return _result(out)
    dest = os.path.join(_files_dir(), attachments.safe_filename(artifact_id, info.get("name") or "file"))
    try:
        size = await attachments.download(api.client(), info["url"], attachments.pathlib.Path(dest),
                                          attachments.MAX_FILE_BYTES)
    except Exception as e:
        return _result(_error(f"Could not download the file: {e}"))
    out.update(path=dest, size=size)
    if str(info.get("mime_type") or "").startswith("image/"):
        out["note"] = "An image: open the path with the Read tool to see it."
    return _result(out)


if __name__ == "__main__":
    # Mirror the spawn-time binding for the status-line hook (best-effort;
    # our PPID here is the `claude` process that spawned us, the same PPID
    # the status-line hook sees).
    identity.write_binding()
    mcp.run(transport="stdio")
