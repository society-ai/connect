"""MCP Events in webhook mode (draft SEP-3415, extension io.modelcontextprotocol/events), as ChatGPT uses it.

Unlike the other styles, the user pastes nothing: the agent's app subscribes over MCP (events/subscribe)
with its own callback URL and a whsec_ secret, Society Connect checks the URL with a signed verification
challenge, and a wake-up is then one signed event saying messages are waiting. The event carries no
message content; the agent reads its messages with check_messages.

Where the draft and ChatGPT's guide disagree, this follows ChatGPT (the client that uses it): the
webhook-id equals the event's eventId.
"""

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone

SECRET_PREFIX = "whsec_"
EVENT_NAME = "society.messages.waiting"
SUBSCRIPTION_HEADER = "X-MCP-Subscription-Id"


def secret_ok(secret: str) -> bool:
    """whsec_ and base64 of 24 to 64 random bytes (the draft's rule)."""
    if not secret.startswith(SECRET_PREFIX):
        return False
    try:
        raw = base64.b64decode(secret[len(SECRET_PREFIX):], validate=True)
    except (binascii.Error, ValueError):
        return False
    return 24 <= len(raw) <= 64


def _key(secret: str) -> bytes:
    return base64.b64decode(secret[len(SECRET_PREFIX):])


def headers(secret: str, msg_id: str, ts: int, body: bytes, subscription_id: str) -> dict:
    content = f"{msg_id}.{ts}.".encode() + body
    signature = base64.b64encode(hmac.new(_key(secret), content, hashlib.sha256).digest()).decode()
    out = {"content-type": "application/json", "webhook-id": msg_id, "webhook-timestamp": str(ts),
           "webhook-signature": f"v1,{signature}"}
    if subscription_id:
        out[SUBSCRIPTION_HEADER] = subscription_id
    return out


def payload(text: str, state: dict) -> dict:
    """The event body: one event, no message content beyond the fixed wake-up text."""
    return {
        "eventId": f"evt_{secrets.token_hex(12)}",
        "name": EVENT_NAME,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": {"text": text},
    }


def build(url: str, secret: str, body: bytes, state: dict) -> dict:
    event_id = json.loads(body).get("eventId") or f"msg_{secrets.token_hex(12)}"
    return {"headers": headers(secret, event_id, int(time.time()), body, state.get("subscription_id", ""))}


def on_response(status: int, state: dict) -> dict:
    return {"derivation": "mcp_events"}


def verification(secret: str, subscription_id: str) -> tuple[str, bytes, dict]:
    """The signed challenge sent to a new callback URL: (challenge, body, headers)."""
    challenge = secrets.token_urlsafe(24)
    body = json.dumps({"type": "verification", "challenge": challenge}).encode()
    msg_id = f"msg_verification_{secrets.token_hex(8)}"
    return challenge, body, headers(secret, msg_id, int(time.time()), body, subscription_id)


def verified(status: int, body: bytes, challenge: str) -> bool:
    """The endpoint answered 2xx with {"challenge": "<the same nonce>"}."""
    if not 200 <= status < 300:
        return False
    try:
        echoed = json.loads(body).get("challenge")
    except (ValueError, AttributeError):
        return False
    return isinstance(echoed, str) and hmac.compare_digest(echoed, challenge)
