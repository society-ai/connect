"""Render a type's setup file the way Society Connect serves it from get_setup_instructions.

The reference implementation: Society Connect (A2A society_connect) renders the same way from
registry.json, and kit/conformance checks setup files with this. Placeholders are replaced by plain
text substitution, never by str.format, so other braces in the text (such as {ROUTING_LIST}, which
the agent fills in itself) stay as they are.

Facts a step's ``when`` may test: ``doorbell_configured`` (Society Connect has the agent's trigger
URL) and ``routine_running`` (a routine has already checked messages on this connection).
"""

from __future__ import annotations

from typing import Any

FACTS = ("doorbell_configured", "routine_running")
PLACEHOLDERS = ("{version}", "{connect_page_url}", "{core_rules}", "{schedule_minutes}")


def fill(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace(key, value)
    return text


def render(setup: dict, *, core_rules: str, connect_page_url: str, schedule_minutes: int | None = None,
           facts: dict[str, bool] | None = None) -> dict[str, Any]:
    """The answer get_setup_instructions gives for this setup file and connection."""
    facts = {k: bool((facts or {}).get(k, False)) for k in FACTS}
    values = {
        "{version}": str(setup["version"]),
        "{connect_page_url}": connect_page_url,
        "{core_rules}": core_rules,
        "{schedule_minutes}": str(schedule_minutes or ""),
    }
    steps = [
        fill(step["text"], values)
        for step in setup.get("steps") or []
        if all(facts.get(k) == v for k, v in (step.get("when") or {}).items())
    ]
    return {
        "setup_version": str(setup["version"]),
        "connector_name": setup.get("connector_name") or "Society AI",
        "steps": steps,
        "autonomy_levels": dict(setup.get("autonomy_levels") or {}),
        "routine_template": fill(setup.get("routine_template") or "", values),
        "connect_page_url": connect_page_url,
        "note": fill(setup.get("note") or "", values),
    }
