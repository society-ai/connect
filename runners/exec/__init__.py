"""The exec runner: start the agent's command once per message with the prompt, and send what it prints.

For any command-line agent that can do one task and exit (`codex exec "<prompt>"`, `gemini -p "<prompt>"`).
Nothing stays running between messages, so core keeps the conversation: each prompt starts with the recent
exchanges. Simple and works almost everywhere; no live progress and a cold start on every message.

The manifest's command has a "{prompt}" token where the prompt goes; without one the prompt is sent on
standard input.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Optional

from core.runner import ConversationRef, ReplyEvent, RunnerCapabilities, RunnerSpec, SessionHandle, Turn, check_allowed, child_env
from runners import detect_version

TURN_TIMEOUT_S = 600.0
MAX_OUTPUT_BYTES = 1_000_000
HISTORY_TURNS = 6  # exchanges kept in the prompt
HISTORY_CHARS = 2_000  # per message in the history


def with_history(history: list[tuple[str, str]], prompt: str) -> str:
    if not history:
        return prompt
    lines = ["[Earlier in this conversation]"]
    for asked, answered in history[-HISTORY_TURNS:]:
        lines.append(f"Message: {asked[:HISTORY_CHARS]}")
        lines.append(f"You answered: {answered[:HISTORY_CHARS]}")
    return "\n".join(lines) + "\n\n[The new message]\n" + prompt


class ExecRunner:
    capabilities = RunnerCapabilities(sessions=False, streaming=False, cancel=True, enforces_limits=False)

    def __init__(self, spec: RunnerSpec, timeout_s: float = TURN_TIMEOUT_S):
        self.spec = spec
        self.timeout_s = timeout_s

    async def start(self, conversation: ConversationRef) -> SessionHandle:
        return SessionHandle(conversation, {"history": []})

    async def run(self, session: SessionHandle, turn: Turn) -> AsyncIterator[ReplyEvent]:
        history: list[tuple[str, str]] = session.state.setdefault("history", [])
        prompt = with_history(history, turn.prompt)
        on_stdin = "{prompt}" not in self.spec.command
        argv = tuple(prompt if token == "{prompt}" else token for token in self.spec.command)
        try:
            program = check_allowed(self.spec, argv)
        except Exception as e:  # noqa: BLE001
            yield ReplyEvent("error", str(e))
            return
        proc = await asyncio.create_subprocess_exec(
            program, *argv[1:], cwd=session.conversation.cwd, env=child_env(),
            stdin=asyncio.subprocess.PIPE if on_stdin else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode() if on_stdin else None), self.timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            yield ReplyEvent("error", f"{self.spec.type_id} did not finish within {int(self.timeout_s)} seconds")
            return
        text = out[:MAX_OUTPUT_BYTES].decode(errors="replace").strip()
        if proc.returncode != 0:
            tail = err.decode(errors="replace").strip()[-500:]
            yield ReplyEvent("error", f"{self.spec.type_id} exited with {proc.returncode}: {tail or text[-500:]}")
            return
        history.append((turn.prompt, text))
        yield ReplyEvent("final", text)

    async def stop(self, session: SessionHandle) -> None:
        session.state.clear()

    async def version(self) -> Optional[str]:
        return await detect_version(self.spec)
