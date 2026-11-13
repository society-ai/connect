"""Which Connect install a bridge runs from, for Society AI's update offers.

Agents on one machine share one Connect folder, so one update covers them all:
the first approved agent updates the folder and the others restart onto it.
Each bridge reports its install when it registers, and Society AI makes one
offer per install ("Update Connect to 0.37.0 on MacBook-Pro-9") instead of one
per agent (Society AI docs/design/agent-updates.md U9).

- `install_id`: a hash of a random id kept on this machine
  (~/.cache/society-ai/machine-id) and this folder's path. Two folders on one
  machine are two installs; the path itself never leaves the machine.
- `machine`: the computer's short name, for the offer's title.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import secrets
import socket

CACHE_DIR = pathlib.Path(os.path.expanduser("~")) / ".cache" / "society-ai"
REPO_DIR = pathlib.Path(__file__).resolve().parent.parent


def _machine_id() -> str:
    """This machine's random id, made once (owner-only file)."""
    path = CACHE_DIR / "machine-id"
    try:
        value = path.read_text().strip()
        if len(value) == 32 and all(c in "0123456789abcdef" for c in value):
            return value
    except OSError:
        pass
    value = secrets.token_hex(16)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value + "\n")
    except OSError:
        pass  # unwritable cache: the id still holds for this process
    return value


def machine_name() -> str:
    """The computer's short name ("MacBook-Pro-9"), or "" when unknown."""
    name = socket.gethostname().split(".")[0].strip()
    return name[:100]


def install_identity() -> dict[str, str]:
    """The fields a bridge adds to its registration; empty if the folder is unknown."""
    try:
        folder = str(REPO_DIR)
        install_id = hashlib.sha256(f"{_machine_id()}:{folder}".encode()).hexdigest()[:32]
    except Exception:
        return {}
    out = {"install_id": install_id}
    machine = machine_name()
    if machine:
        out["machine"] = machine
    return out
