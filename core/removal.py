"""Remove one agent from this computer, keeping the owner's files.

Three ways in, one removal:
- the bridge, when Society AI says its agent was deleted (the hub's
  ``agent.removed``, or a key refused with ``X-Society-Agent-Status: deleted``);
- ``./connect remove <persona>``;
- the Remove button in the local panel (``./connect status``).

What goes is what makes it an agent on this computer: its key file
(``.env`` / ``.env.<persona>`` and setup's backups of it, which hold the key
too), its background service (the LaunchAgent and its backups), and, for a
named persona, its cache folder (logs, sockets). What stays: its folder
(WORK_DIR), its Claude Code sessions and everything else of the owner's.

Each removal is noted in ``~/.cache/society-ai/removed.json``, so the panel
can say what happened and where the files are.

Standard library only: ``./connect`` runs on the system python.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time

REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
BASE_LABEL = "io.societyai.claude-code-bridge"
PERSONA_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
APP_URL = "https://societyai.com"

DELETED_ON_SOCIETY_AI = "deleted_on_society_ai"
REMOVED_HERE = "removed_here"


class RemovalError(Exception):
    """The persona does not exist on this computer, or its name is not valid."""


def cache_root() -> pathlib.Path:
    return pathlib.Path.home() / ".cache" / "society-ai"


def launch_agents_dir() -> pathlib.Path:
    return pathlib.Path.home() / "Library" / "LaunchAgents"


def tombstone_path() -> pathlib.Path:
    return cache_root() / "removed.json"


def paths_for(persona: str) -> dict:
    """Where one persona lives. ``persona`` is "" for the default agent (.env)."""
    if persona and not PERSONA_RE.fullmatch(persona):
        raise RemovalError(f"not a valid agent name: {persona!r}")
    label = f"{BASE_LABEL}.{persona}" if persona else BASE_LABEL
    return {
        "env_file": REPO_DIR / (f".env.{persona}" if persona else ".env"),
        "label": label,
        "plist": launch_agents_dir() / f"{label}.plist",
        # The default agent shares the top cache folder with the machine-wide
        # state (status token, statusline, trackers); only a named persona has
        # a folder of its own to delete.
        "cache_dir": (cache_root() / persona) if persona else None,
    }


def _read_env(path: pathlib.Path) -> dict:
    env = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            env[key] = value
    except OSError:
        pass
    return env


def _backups(path: pathlib.Path) -> list[pathlib.Path]:
    """setup.sh's and service.sh's timestamped copies of ``path`` (<name>.bak-<epoch>)."""
    pattern = re.compile(re.escape(path.name) + r"\.bak-\d+$")
    try:
        return sorted(p for p in path.parent.iterdir() if pattern.fullmatch(p.name))
    except OSError:
        return []


def _unlink(path: pathlib.Path, removed: list[str]) -> None:
    try:
        path.unlink()
        removed.append(str(path))
    except FileNotFoundError:
        pass


def read_tombstones() -> list[dict]:
    try:
        data = json.loads(tombstone_path().read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _write_tombstones(entries: list[dict]) -> None:
    path = tombstone_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(entries, indent=2) + "\n")
    os.replace(tmp, path)


def dismiss_tombstone(persona_id: str) -> bool:
    entries = read_tombstones()
    kept = [e for e in entries if e.get("id") != persona_id]
    if len(kept) == len(entries):
        return False
    _write_tombstones(kept)
    return True


def agent_page_url(agent_name: str) -> str:
    return f"{APP_URL}/agents/{agent_name}?tab=settings"


def stop_service(label: str, *, detach: bool) -> bool:
    """Stop the persona's LaunchAgent (macOS). ``detach`` when the caller IS
    that service (the bridge): bootout signals the caller, so it must not wait."""
    if sys.platform != "darwin":
        return False
    cmd = ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"]
    try:
        if detach:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        else:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def remove_persona(
    persona: str, *, reason: str, from_bridge: bool = False, expected_agent: str | None = None,
) -> dict:
    """Remove one agent from this computer. Returns what was removed and kept.

    ``expected_agent``: remove only if the persona's key file names this agent
    (the bridge passes its own name). The service is stopped last: when the
    bridge removes itself, stopping it ends this process, so every file must
    already be gone.
    """
    p = paths_for(persona)
    env_file: pathlib.Path = p["env_file"]
    if not env_file.is_file():
        raise RemovalError(f"no agent {persona or 'default'} on this computer ({env_file.name} not found)")
    env = _read_env(env_file)
    agent_name = env.get("AGENT_NAME") or persona or "default"
    if expected_agent is not None and env.get("AGENT_NAME") != expected_agent:
        raise RemovalError(f"{env_file.name} is for {env.get('AGENT_NAME') or 'no agent'}, not {expected_agent}")
    work_dir = env.get("WORK_DIR", "")
    kept = [os.path.expanduser(work_dir)] if work_dir else []

    entries = [e for e in read_tombstones() if e.get("id") != (persona or "default")]
    entries.append({
        "id": persona or "default",
        "agent_name": agent_name,
        "reason": reason,
        "at": int(time.time()),
        "kept": kept,
    })
    _write_tombstones(entries)

    removed: list[str] = []
    for path in [env_file, *_backups(env_file)]:
        _unlink(path, removed)
    plist: pathlib.Path = p["plist"]
    had_service = plist.is_file()
    for path in [plist, *_backups(plist)]:
        _unlink(path, removed)
    cache_dir = p["cache_dir"]
    if cache_dir is not None and cache_dir.is_dir():
        shutil.rmtree(cache_dir, ignore_errors=True)
        removed.append(str(cache_dir))

    if had_service:
        stop_service(p["label"], detach=from_bridge)

    return {"id": persona or "default", "agent_name": agent_name, "removed": removed, "kept": kept}


def main(argv: list[str]) -> int:
    """``./connect remove <persona>``: remove an agent from this computer."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="connect remove",
        description="Remove an agent from this computer: its background service, its key and its logs. "
                    "Its folder and sessions stay. To delete it on Society AI too, use its page there.",
    )
    parser.add_argument("persona", help="the agent to remove (its persona name; 'default' for the first agent)")
    parser.add_argument("--yes", action="store_true", help="do not ask first")
    args = parser.parse_args(argv)
    persona = "" if args.persona == "default" else args.persona

    try:
        p = paths_for(persona)
    except RemovalError as e:
        print(e, file=sys.stderr)
        return 2
    if not p["env_file"].is_file():
        print(f"There is no agent {args.persona} on this computer.", file=sys.stderr)
        return 1
    agent_name = _read_env(p["env_file"]).get("AGENT_NAME") or args.persona
    if not args.yes:
        answer = input(f"Remove {agent_name} from this computer? Its folder and sessions stay. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing removed.")
            return 1
    result = remove_persona(persona, reason=REMOVED_HERE)
    print(f"Removed {agent_name} from this computer.")
    for path in result["kept"]:
        print(f"  Kept its folder: {path}")
    print(f"It still exists on Society AI. To delete it there: {agent_page_url(agent_name)}")
    return 0
