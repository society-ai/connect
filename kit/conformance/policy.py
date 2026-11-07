"""core's sender rules (docs/design: the InboundMessage kinds), checked against the router's dispatches.

Which session each kind of sender lands in, and whether it is framed as the owner, is decided in core
for every agent type, so no runner can change who reaches the user's agent or what it may do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from kit.conformance.manifests import Problem
from kit.mock_hub import frames


@dataclass(frozen=True)
class Case:
    name: str
    task: Callable[[], dict]
    owner: bool  # framed as the owner (the owner's tools, the owner's session)
    session: str  # the named session it lands in ("" when keyed by its conversation)
    permission: str | None = None  # a contact's level


CASES = (
    Case("an owner message is the owner", lambda: frames.owner("hi"), owner=True, session=""),
    Case("a contact is never the owner", lambda: frames.contact("hi", permission="read"), owner=False, session="",
         permission="read"),
    Case("a public agent is chat only", lambda: frames.public("hi"), owner=False, session="", permission="chat"),
    Case("the Lead's updates land in the updates session", lambda: frames.platform_update("x"), owner=True,
         session="updates"),
    Case("a group mention lands in that group's session, never as the owner", lambda: frames.group_mention("x"),
         owner=False, session="group:g-1", permission="chat"),
    Case("a late reply returns to the owner's chat it was sent from", lambda: frames.late_reply("x", "chat-1"),
         owner=True, session=""),
)


def check(classify: Callable[[dict, dict, str], object]) -> list[Problem]:
    """``classify(frame, metadata, owner_id)`` is core's sender rule (contacts.classify_sender)."""
    problems = []
    for case in CASES:
        task = case.task()
        sender = classify(task["metadata"]["frame"], task["metadata"], frames.OWNER_ID)
        got = (sender.is_owner, sender.session, None if case.permission is None else sender.permission)
        want = (case.owner, case.session, case.permission)
        if got != want:
            problems.append(Problem(f"core policy: {case.name}", f"got owner={got[0]} session={got[1]!r} "
                                    f"permission={got[2]}, want owner={want[0]} session={want[1]!r} "
                                    f"permission={want[2]}", "docs/contribute/runners.md#who-sent-it"))
    return problems
