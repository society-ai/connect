"""The ACP runner: drive an agent that speaks the Agent Client Protocol (agentclientprotocol.com) over stdio.

The bridge starts the agent once and talks JSON-RPC to it, one message per line: initialize, then one ACP
session per conversation (session/new), a prompt per turn (session/prompt), streamed text as session/update
notifications, and the turn's end as the prompt's result. The agent keeps its own context, so core does not
repeat the history. Protocol version 1.

The bridge offers the agent no file system or terminal of its own (it uses its own tools), and answers its
permission requests: allowed once for the owner, refused for anyone else.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import AsyncIterator, Optional

from core.runner import ConversationRef, ReplyEvent, RunnerCapabilities, RunnerSpec, SessionHandle, Turn, check_allowed, child_env
from runners import detect_version

logger = logging.getLogger("runners.acp")

PROTOCOL_VERSION = 1
TURN_TIMEOUT_S = 600.0
REQUEST_TIMEOUT_S = 60.0
MAX_LINE_BYTES = 8 * 1024 * 1024


class AcpError(Exception):
    pass


class AcpRunner:
    capabilities = RunnerCapabilities(sessions=True, streaming=True, cancel=True, enforces_limits=False)

    def __init__(self, spec: RunnerSpec, timeout_s: float = TURN_TIMEOUT_S):
        self.spec = spec
        self.timeout_s = timeout_s
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.Task] = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._updates: dict[str, asyncio.Queue] = {}  # session id -> session/update notifications
        self._owner_turn: dict[str, bool] = {}  # session id -> the current turn is the owner's
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- the agent process

    async def _ensure(self, cwd: str) -> None:
        async with self._lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            program = check_allowed(self.spec, self.spec.command)
            self._proc = await asyncio.create_subprocess_exec(
                program, *self.spec.command[1:], cwd=cwd, env=child_env(), limit=MAX_LINE_BYTES,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            self._reader = asyncio.create_task(self._read())
            result = await self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                "clientInfo": {"name": "society-ai-connect", "version": "1"},
            })
            if int(result.get("protocolVersion", 0)) != PROTOCOL_VERSION:
                raise AcpError(f"{self.spec.type_id} speaks ACP version {result.get('protocolVersion')}, not {PROTOCOL_VERSION}")

    async def _send(self, message: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        await self._proc.stdin.drain()

    async def _request(self, method: str, params: dict, timeout: float = REQUEST_TIMEOUT_S) -> dict:
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # not protocol output
                await self._handle(message)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(AcpError(f"{self.spec.type_id} stopped"))
            for queue in self._updates.values():
                queue.put_nowait(None)

    async def _handle(self, message: dict) -> None:
        if "id" in message and ("result" in message or "error" in message) and "method" not in message:
            future = self._pending.get(message["id"])
            if future is not None and not future.done():
                if message.get("error"):
                    future.set_exception(AcpError(str(message["error"].get("message") or message["error"])))
                else:
                    future.set_result(message.get("result") or {})
            return
        method, params = message.get("method"), message.get("params") or {}
        if method == "session/update":
            queue = self._updates.get(params.get("sessionId", ""))
            if queue is not None:
                queue.put_nowait(params.get("update") or {})
            return
        if "id" not in message:
            return  # a notification we do not use
        if method == "session/request_permission":
            await self._send({"jsonrpc": "2.0", "id": message["id"], "result": self._permission(params)})
        else:
            # The bridge offers no file system or terminal: the agent uses its own tools.
            await self._send({"jsonrpc": "2.0", "id": message["id"],
                              "error": {"code": -32601, "message": f"{method} is not offered by this client"}})

    def _permission(self, params: dict) -> dict:
        """Allowed once for the owner's turn, refused for anyone else's."""
        options = params.get("options") or []
        owner = self._owner_turn.get(params.get("sessionId", ""), False)
        wanted = ("allow_once", "allow_always") if owner else ("reject_once", "reject_always")
        for kind in wanted:
            for option in options:
                if option.get("kind") == kind:
                    return {"outcome": {"outcome": "selected", "optionId": option.get("optionId")}}
        return {"outcome": {"outcome": "cancelled"}}

    # ---------------------------------------------------------------- Runner

    async def start(self, conversation: ConversationRef) -> SessionHandle:
        await self._ensure(conversation.cwd)
        result = await self._request("session/new", {"cwd": conversation.cwd, "mcpServers": []})
        session_id = result.get("sessionId")
        if not session_id:
            raise AcpError(f"{self.spec.type_id} did not open a session")
        self._updates[session_id] = asyncio.Queue()
        return SessionHandle(conversation, {"session_id": session_id})

    async def run(self, session: SessionHandle, turn: Turn) -> AsyncIterator[ReplyEvent]:
        session_id = session.state["session_id"]
        queue = self._updates[session_id]
        self._owner_turn[session_id] = turn.owner
        while not queue.empty():
            queue.get_nowait()  # anything left from a turn that already ended
        prompt = asyncio.ensure_future(self._request(
            "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": turn.prompt}]},
            timeout=self.timeout_s,
        ))
        chunks: list[str] = []
        try:
            while True:
                getter = asyncio.ensure_future(queue.get())
                done, _ = await asyncio.wait({prompt, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    update = getter.result()
                    if update is None:
                        break
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        content = update.get("content") or {}
                        if content.get("type") == "text" and content.get("text"):
                            chunks.append(content["text"])
                            yield ReplyEvent("progress", content["text"])
                else:
                    getter.cancel()
                if prompt.done():
                    while not queue.empty():  # updates sent just before the result
                        update = queue.get_nowait()
                        if update and update.get("sessionUpdate") == "agent_message_chunk":
                            text = (update.get("content") or {}).get("text")
                            if text:
                                chunks.append(text)
                                yield ReplyEvent("progress", text)
                    break
            result = await prompt
        except (AcpError, asyncio.TimeoutError) as e:
            await self.cancel(session)
            yield ReplyEvent("error", f"{self.spec.type_id}: {e or 'no answer in time'}")
            return
        stop = result.get("stopReason", "end_turn")
        text = "".join(chunks).strip()
        if stop == "refusal" and not text:
            text = "(The agent declined to answer.)"
        yield ReplyEvent("final", text)

    async def cancel(self, session: SessionHandle) -> None:
        if self._proc is not None and self._proc.returncode is None:
            await self._send({"jsonrpc": "2.0", "method": "session/cancel",
                              "params": {"sessionId": session.state.get("session_id")}})

    async def stop(self, session: SessionHandle) -> None:
        self._updates.pop(session.state.get("session_id", ""), None)

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            await self._proc.wait()
        if self._reader is not None:
            self._reader.cancel()

    async def version(self) -> Optional[str]:
        return await detect_version(self.spec)
