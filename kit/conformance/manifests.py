"""The checks every agent type folder must pass (docs/review.md, "Automated checks").

Each problem says what to change and links the doc section, so an agent can fix it without asking.
CI runs exactly these through ./connect dev check.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator

from kit import setup_render
from kit.registry import ROOT, AgentType, read_all, read_type

SCHEMA_PATH = ROOT / "schema" / "agent.schema.json"
DOCS = "docs/contribute"
PULL_LEVELS = ("tools_only", "scheduled_pull", "wake_pull")
# Hosts a setup file may link to. Setup text is instructions agents follow, so any other link is a
# prompt-injection risk (docs/review.md#setup-text).
SETUP_HOSTS = ("societyai.com", "github.com/society-ai")
URL_RE = re.compile(r"https?://[^\s\"')]+")
HIDDEN_RE = re.compile("[​-‏‪-‮⁠-⁤﻿]")
BLOB_RE = re.compile(r"[A-Za-z0-9+/=_-]{80,}")
PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([a-z_]+)\}(?!\})")
AGENT_FILLED = ("ROUTING_LIST", "AUTONOMY_LEVEL")  # filled in by the agent itself, not by Society AI
SVG_FORBIDDEN = re.compile(r"<script|\bon[a-z]+\s*=|javascript:|href\s*=\s*[\"']?(?!#)|<foreignObject|@import|url\(",
                           re.IGNORECASE)


@dataclass(frozen=True)
class Problem:
    where: str  # "grok_bot/agent.yaml: delivery.wake"
    message: str  # what to change
    doc: str  # where it is explained

    def __str__(self) -> str:
        return f"{self.where}: {self.message} (see {self.doc})"


def _validator() -> Draft202012Validator:
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text()))


def _schema_problems(t: AgentType) -> list[Problem]:
    out = []
    for error in sorted(_validator().iter_errors(t.manifest), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in error.absolute_path) or "(top level)"
        message = error.message
        if error.validator == "additionalProperties":
            message += ". Only the fields in schema/agent.schema.json are allowed"
        out.append(Problem(f"{t.folder.name}/agent.yaml: {path}", message, f"{DOCS}/manifest.md"))
    return out


def _icon_problems(t: AgentType) -> list[Problem]:
    where = f"{t.folder.name}/{t.manifest.get('icon', 'icon.svg')}"
    if t.icon_svg is None:
        return [Problem(where, "the icon file is missing", f"{DOCS}/manifest.md#icon")]
    out = []
    if not t.icon_svg.lstrip().startswith("<svg"):
        out.append(Problem(where, "the icon must be an SVG document starting with <svg", f"{DOCS}/manifest.md#icon"))
    if SVG_FORBIDDEN.search(t.icon_svg):
        out.append(Problem(where, "the icon may not contain scripts, event handlers, links or external references",
                           f"{DOCS}/manifest.md#icon"))
    if len(t.icon_svg) > 20_000:
        out.append(Problem(where, "the icon is larger than 20 KB; simplify it", f"{DOCS}/manifest.md#icon"))
    return out


def _steps_problems(t: AgentType) -> list[Problem]:
    out = []
    for i, step in enumerate((t.manifest.get("setup") or {}).get("owner_steps") or []):
        ref = step.get("text") if isinstance(step, dict) else None
        if isinstance(ref, str) and "#" in ref:
            anchor = ref.split("#", 1)[1]
            if anchor not in t.owner_steps:
                out.append(Problem(f"{t.folder.name}/agent.yaml: setup.owner_steps.{i}.text",
                                   f'owner-steps.md has no "## {anchor}" section', f"{DOCS}/manifest.md#owner-steps"))
    return out


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _allowed_link(url: str) -> bool:
    if not url.startswith("https://"):
        return False
    host, _, path = url[len("https://"):].partition("/")
    if host == "societyai.com" or host.endswith(".societyai.com"):
        return True
    return host == "github.com" and path.startswith("society-ai/")


def setup_text_problems(where: str, setup: dict) -> list[Problem]:
    """The setup text check: setup text is instructions agents follow (docs/review.md#setup-text)."""
    doc = "docs/review.md#setup-text"
    out = []
    for key in ("version", "steps", "routine_template", "note"):
        if not setup.get(key):
            out.append(Problem(where, f"the setup file needs {key}", f"{DOCS}/setup-file.md"))
    if not isinstance(setup.get("version"), str):
        out.append(Problem(where, 'version must be a quoted string, such as "2026-11-07.1"', f"{DOCS}/setup-file.md"))
    for step in setup.get("steps") or []:
        if not isinstance(step, dict) or not isinstance(step.get("text"), str):
            out.append(Problem(where, "each step is {text: ...}, with an optional when", f"{DOCS}/setup-file.md"))
            continue
        unknown = set(step.get("when") or {}) - set(setup_render.FACTS)
        if unknown:
            out.append(Problem(where, f"a step's when may test only {', '.join(setup_render.FACTS)}; "
                                      f"not {', '.join(sorted(unknown))}", f"{DOCS}/setup-file.md#when"))
    routine = setup.get("routine_template") or ""
    if routine.count("{core_rules}") != 1:
        out.append(Problem(where, "routine_template must contain {core_rules} exactly once: Society AI's fixed "
                                  "rules for every agent (core/setup_rules.md) go there", doc))
    if "send_group_message" not in routine:
        out.append(Problem(where, "routine_template must say how to answer group chat messages (from "
                                  "@society-groups, with send_group_message), with the group conduct rules", doc))
    for text in _strings(setup):
        for name in PLACEHOLDER_RE.findall(text):
            if f"{{{name}}}" not in setup_render.PLACEHOLDERS and name.upper() not in AGENT_FILLED:
                out.append(Problem(where, f"unknown placeholder {{{name}}}; Society AI fills only "
                                          f"{', '.join(setup_render.PLACEHOLDERS)}", f"{DOCS}/setup-file.md"))
        for url in URL_RE.findall(text):
            if not _allowed_link(url):
                out.append(Problem(where, f"setup text may link only to {', '.join(SETUP_HOSTS)}; found {url}", doc))
        if HIDDEN_RE.search(text):
            out.append(Problem(where, "setup text contains invisible characters", doc))
        if "<!--" in text or "<script" in text.lower():
            out.append(Problem(where, "setup text may not contain HTML comments or markup", doc))
        if BLOB_RE.search(text):
            out.append(Problem(where, "setup text contains a long encoded-looking string", doc))
    return out


def check_type(t: AgentType) -> list[Problem]:
    name = t.folder.name
    problems = _schema_problems(t)
    if not t.is_template and t.manifest.get("id") != name:
        problems.append(Problem(f"{name}/agent.yaml: id", f'id must be the folder name, "{name}"', f"{DOCS}/manifest.md#id"))
    problems += _icon_problems(t) + _steps_problems(t)
    level = (t.manifest.get("delivery") or {}).get("level")
    setup_name = (t.manifest.get("setup") or {}).get("agent_instructions")
    if level in PULL_LEVELS:
        if t.setup is None:
            problems.append(Problem(f"{name}/{setup_name or 'setup.yaml'}", "a type that pulls its messages needs a "
                                    "setup file: it is what the agent reads to set itself up", f"{DOCS}/setup-file.md"))
        else:
            problems += setup_text_problems(f"{name}/{setup_name}", t.setup)
    return problems


def check(root: Path = ROOT, only: str | None = None) -> list[Problem]:
    """Every problem in agents/ (or in one type folder)."""
    if only:
        folder = root / "agents" / only
        if not folder.is_dir():
            return [Problem(f"agents/{only}", "no such agent type folder", f"{DOCS}/adding-an-agent-type.md")]
        types = [read_type(folder)]
    else:
        types = read_all(root, templates=True)
    problems = [p for t in types for p in check_type(t)]
    ids = [t.manifest.get("id") for t in types if not t.is_template]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(Problem(f"agents/{dup}", "two folders declare this id", f"{DOCS}/manifest.md#id"))
    return problems
