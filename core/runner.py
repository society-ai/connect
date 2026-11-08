"""The runner interface: how the bridge makes a program on this computer do one turn of work.

core does everything before and after a turn (docs/contribute/runners.md): it signs in, decides who sent
the message and which conversation it belongs to, applies the owner's contact levels, builds the prompt the
agent sees (with the untrusted-content label and the limit line), and returns the reply. A runner only runs
turns, so a runner can never change who reaches the user's agent or what it may do.

A runner implements ``Runner``. The generic ones are in runners/ (exec: one command per message; acp: the
Agent Client Protocol over stdio); a type that needs more keeps its own in agents/<id>/runner/ (Claude
Code's tmux sessions, channel and hooks predate this interface and are driven by core/bridge.py directly).

Which runner an agent uses comes from its type's manifest (agents/<id>/agent.yaml, ``runner``).
"""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Optional, Protocol

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_CODE = "claude_code"
# Environment a runner's program never inherits: the agent's key and Society AI's own settings stay in core
# (docs/review.md: runners never see the agent key).
PRIVATE_ENV_PREFIXES = ("SOCIETY_AI_", "AGENT_ROUTER_", "CONNECT_")
PRIVATE_ENV = frozenset({"AGENT_NAME", "DISPLAY_NAME", "COMPANY_ID"})


@dataclass(frozen=True)
class RunnerCapabilities:
    sessions: bool  # keeps its own context between turns; without it core puts recent history in the prompt
    streaming: bool  # reports progress before the final reply
    cancel: bool  # a turn can be stopped
    enforces_limits: bool  # can hold a contact to its level (chat, read, act); without it contacts are refused


@dataclass(frozen=True)
class ConversationRef:
    key: str  # core's conversation key: one per owner chat, contact conversation, group or task
    cwd: str  # the folder the agent works in


@dataclass
class SessionHandle:
    conversation: ConversationRef
    state: dict = field(default_factory=dict)  # the runner's own (an ACP session id, a history)


@dataclass(frozen=True)
class Turn:
    task_id: str
    prompt: str  # built by core: standing instructions, the work, the sender's limits
    owner: bool  # from the owner (core decided); contacts reach a runner only if it enforces limits
    permission: Optional[str] = None  # a contact's level (chat | read | act)


@dataclass(frozen=True)
class ReplyEvent:
    kind: str  # "progress" | "final" | "error"
    text: str


class Runner(Protocol):
    capabilities: RunnerCapabilities

    async def start(self, conversation: ConversationRef) -> SessionHandle: ...

    def run(self, session: SessionHandle, turn: Turn) -> AsyncIterator[ReplyEvent]:
        """Send the turn and yield what comes back: progress, then one final reply (or an error)."""
        ...

    async def stop(self, session: SessionHandle) -> None: ...

    async def version(self) -> Optional[str]:
        """The agent program's version, for the agent's page (None when unknown)."""
        ...


class RunnerConfigError(Exception):
    """The agent type's runner cannot start on this computer (not installed, not allowed, misconfigured)."""


@dataclass(frozen=True)
class RunnerSpec:
    """A type's ``runner`` and ``permissions`` from its manifest, with the user's values filled in."""

    type_id: str
    use: str
    command: tuple[str, ...]
    detect: tuple[str, ...]
    binaries: frozenset[str]
    network: tuple[str, ...]


def load_manifest(type_id: str) -> dict:
    path = REPO_ROOT / "agents" / type_id / "agent.yaml"
    if not path.is_file():
        raise RunnerConfigError(f"unknown agent type {type_id!r}: there is no agents/{type_id}/agent.yaml")
    return yaml.safe_load(path.read_text()) or {}


def _fill(tokens: list, command: list[str]) -> tuple[str, ...]:
    """Replace a "{command}" token with the user's own command (generic types). When the user's command says
    where the prompt goes ({prompt}), the manifest's own {prompt} after it is dropped."""
    out: list[str] = []
    user_places_prompt = "{prompt}" in command
    for token in tokens:
        if token == "{command}":
            out.extend(command)
        elif token == "{prompt}" and user_places_prompt:
            continue
        else:
            out.append(str(token))
    return tuple(out)


def runner_spec(type_id: str, user_command: str = "") -> RunnerSpec:
    manifest = load_manifest(type_id)
    if manifest.get("kind") != "local":
        raise RunnerConfigError(f"{type_id} is not a local agent type")
    runner = manifest.get("runner") or {}
    permissions = manifest.get("permissions") or {}
    command = shlex.split(user_command) if user_command else []
    if "{command}" in (runner.get("command") or []) and not command:
        raise RunnerConfigError(f"{type_id} runs the command you name: set AGENT_COMMAND (./connect setup asks for it)")
    binaries = {command[0] if b == "{command}" and command else b for b in permissions.get("binaries") or []}
    return RunnerSpec(
        type_id=type_id,
        use=runner.get("use", ""),
        command=_fill(runner.get("command") or [], command),
        detect=_fill(runner.get("detect") or [], command),
        binaries=frozenset(binaries),
        network=tuple(permissions.get("network") or ()),
    )


def check_allowed(spec: RunnerSpec, argv: tuple[str, ...]) -> str:
    """The program a runner may start: one its manifest declares, found on this computer. Returns its path."""
    program = argv[0] if argv else ""
    if Path(program).name not in spec.binaries:
        raise RunnerConfigError(f"{spec.type_id} may start only {', '.join(sorted(spec.binaries))}, not {program!r}")
    found = shutil.which(program)
    if not found:
        raise RunnerConfigError(f"{program!r} is not installed (or not on PATH) on this computer")
    return found


def child_env() -> dict[str, str]:
    """The environment a runner's program gets: this computer's, without the agent's key and settings."""
    return {
        k: v for k, v in os.environ.items()
        if k not in PRIVATE_ENV and not k.startswith(PRIVATE_ENV_PREFIXES)
    }


def load_runner(type_id: str, user_command: str = "") -> Runner:
    """The runner for a local agent type other than Claude Code."""
    spec = runner_spec(type_id, user_command)
    if spec.use == "exec":
        from runners.exec import ExecRunner

        return ExecRunner(spec)
    if spec.use == "acp":
        from runners.acp import AcpRunner

        return AcpRunner(spec)
    raise RunnerConfigError(f"{type_id} uses a {spec.use!r} runner, which this bridge does not have")
