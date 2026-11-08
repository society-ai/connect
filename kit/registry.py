"""Read the agent types in agents/ and build registry.json (docs/contribute/registry.md).

registry.json is how Society Connect (mcp.societyai.com) and the Society AI app learn about agent
types. Each of them loads a pinned copy; they never read this repo's main branch. Folders starting
with "_" (the templates) are not agent types.

    ./connect registry build --version v0.29.0 --out dist/registry.json
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

ROOT = Path(__file__).resolve().parents[1]
AGENTS = "agents"
REGISTRY_FORMAT = 1
CORE_RULES = Path("core") / "setup_rules.md"
SECTION_RE = re.compile(r"^## ([a-z0-9-]+)\s*$")


@dataclass
class AgentType:
    """One folder in agents/, as read from disk (nothing checked yet)."""

    folder: Path
    manifest: dict
    setup: Optional[dict] = None  # the parsed setup file, for types that pull
    owner_steps: dict[str, str] = field(default_factory=dict)  # owner-steps.md sections by anchor
    icon_svg: Optional[str] = None

    @property
    def id(self) -> str:
        return str(self.manifest.get("id") or self.folder.name)

    @property
    def is_template(self) -> bool:
        return self.folder.name.startswith("_")


def read_sections(text: str) -> dict[str, str]:
    """The `## anchor` sections of an owner-steps.md, each as one paragraph of text."""
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        match = SECTION_RE.match(line)
        if match:
            current = match.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def read_type(folder: Path) -> AgentType:
    """Read one agent type folder. Raises ValueError for a file that is not valid YAML."""
    try:
        manifest = yaml.safe_load((folder / "agent.yaml").read_text()) or {}
    except FileNotFoundError:
        manifest = {}
    except yaml.YAMLError as e:
        raise ValueError(f"{folder.name}/agent.yaml is not valid YAML: {e}") from e
    entry = AgentType(folder=folder, manifest=manifest if isinstance(manifest, dict) else {})
    setup_name = (entry.manifest.get("setup") or {}).get("agent_instructions")
    if isinstance(setup_name, str) and (folder / setup_name).is_file():
        try:
            entry.setup = yaml.safe_load((folder / setup_name).read_text())
        except yaml.YAMLError as e:
            raise ValueError(f"{folder.name}/{setup_name} is not valid YAML: {e}") from e
    if (folder / "owner-steps.md").is_file():
        entry.owner_steps = read_sections((folder / "owner-steps.md").read_text())
    icon_name = entry.manifest.get("icon")
    if isinstance(icon_name, str) and (folder / icon_name).is_file():
        entry.icon_svg = (folder / icon_name).read_text().strip()
    return entry


def read_all(root: Path = ROOT, *, templates: bool = False) -> list[AgentType]:
    """Every type folder under agents/ ("_" folders are templates; Python's __pycache__ and dot folders are not types)."""
    folders = sorted(
        p for p in (root / AGENTS).iterdir() if p.is_dir() and not p.name.startswith(("__", "."))
    )
    return [read_type(p) for p in folders if templates or not p.name.startswith("_")]


def core_rules(root: Path = ROOT) -> str:
    return (root / CORE_RULES).read_text().strip()


def _entry(t: AgentType) -> dict[str, Any]:
    entry = {
        **t.manifest,
        "verified": bool(t.manifest.get("verified", False)),
        "generic": bool(t.manifest.get("generic", False)),
        "contacts_only": bool(t.manifest.get("contacts_only", True)),
        "icon_svg": t.icon_svg,
        "owner_steps_text": {k: v for k, v in t.owner_steps.items()},
        "setup_file": t.setup,
    }
    entry.pop("icon", None)
    canonical = json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    entry["hash"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    return entry


def _git_commit(root: Path) -> Optional[str]:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def build(root: Path = ROOT, *, version: str) -> dict[str, Any]:
    """registry.json for the types in ``root``: Verified first, then generic types last, then by name.
    Run the conformance checks first (./connect dev check): build does not check."""
    types = read_all(root)
    order = sorted(types, key=lambda t: (not t.manifest.get("verified", False),
                                         bool(t.manifest.get("generic", False)), t.manifest.get("name", t.id)))
    return {
        "registry": REGISTRY_FORMAT,
        "version": version,
        "commit": _git_commit(root),
        "core_rules": core_rules(root),
        "types": [_entry(t) for t in order],
    }


def write(registry: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
