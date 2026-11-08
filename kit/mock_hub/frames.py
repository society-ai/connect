"""Dispatches as the router sends them to a local agent (task.execute params), one builder per kind of
sender. They mirror the router's composed dispatch (A2A agent_router/dispatch/composer.py): frame.from
says who sent it, metadata.user_id is set by the router only for the owner's own messages and for the
wakes it addresses to the agent for its owner (group mentions, late replies, the Lead's updates).

core turns these into a session and a sender (core policy); a runner never sees them raw.
"""

from __future__ import annotations

import itertools
import uuid
from typing import Optional

OWNER_ID = "11111111-1111-4111-8111-111111111111"
OTHER_USER_ID = "22222222-2222-4222-8222-222222222222"
_ids = itertools.count(1)


def _task(text: str, frame: dict, **metadata) -> dict:
    n = next(_ids)
    return {
        "id": f"task-{n}",
        "sessionId": metadata.pop("session_id", None) or f"session-{n}",
        "message": {"role": "user", "parts": [{"type": "text", "text": text}]},
        "metadata": {"frame": frame, **metadata},
    }


def owner(text: str, chat_id: Optional[str] = None) -> dict:
    """The owner, writing in one of their Society AI chats."""
    chat_id = chat_id or str(uuid.uuid4())
    return _task(text, {"cause": "message", "from": {"kind": "owner"}, "chat_id": chat_id},
                 user_id=OWNER_ID, chat_id=chat_id, session_id=chat_id)


def contact(text: str, agent: str = "dana-claw", permission: str = "chat", conversation: str = "conv-1") -> dict:
    """Another agent that is one of the agent's contacts, at the level its owner gave it."""
    return _task(text, {"cause": "message", "conversation_id": conversation,
                        "from": {"kind": "agent", "name": agent, "permission": permission}},
                 user_id=OTHER_USER_ID)


def public(text: str, agent: str = "weather-pro") -> dict:
    """An agent that is not a contact, reaching a public agent (billed, chat only)."""
    return _task(text, {"cause": "message", "from": {"kind": "agent", "name": agent, "permission": "chat"}},
                 user_id=OTHER_USER_ID)


def platform_update(text: str) -> dict:
    """Society AI's update for the owner's Lead agent."""
    return _task(text, {"cause": "wake", "from": {"kind": "platform"}, "session": "updates"},
                 user_id=OWNER_ID, session_id="updates:agent")


def group_mention(text: str, group_id: str = "g-1", name: str = "Project chat: Builders",
                  permission: str = "chat") -> dict:
    """A mention, reply or @all in a group chat the agent is in. group_id is the chat's id; from.place says
    which group, workspace, project or company the chat belongs to (A2A docs/design/group-chat.md §12)."""
    place = {"type": "project", "id": "p-1", "name": "Builders", "kind": "project"}
    return _task(text, {"cause": "wake", "from": {"kind": "group", "id": group_id, "name": name, "sender": "dana",
                                                  "place": place},
                        "session": f"group:{group_id}"},
                 user_id=OWNER_ID, session_id=f"group:{group_id}",
                 sender_access={"kind": "agent", "permission": permission},
                 group={"id": group_id, "name": name})


def late_reply(text: str, chat_id: str, agent: str = "dana-claw") -> dict:
    """Another agent's answer to a message this agent sent from its owner's chat, after the send returned."""
    return _task(f"@{agent} answered your message:\n\n{text}",
                 {"cause": "message", "from": {"kind": "agent", "name": agent, "permission": "chat",
                                               "in_reply_to": "t-1"}, "chat_id": chat_id},
                 user_id=OWNER_ID, chat_id=chat_id, session_id=chat_id, reply={"task_id": "t-1", "from_agent": agent})
