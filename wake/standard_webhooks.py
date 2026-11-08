"""Standard Webhooks signatures (grok.com automations, with a whsec_ secret).

webhook-id, webhook-timestamp and webhook-signature "v1,<base64 HMAC-SHA256 of id.timestamp.body>".
How a receiver turns the whsec_ secret into the HMAC key is not always the standard one (a third party
saw Grok reject it), so after a signature refusal the next derivation is tried, and the one that works
is remembered in the state.
"""

import base64
import hashlib
import hmac
import secrets
import time

SECRET_PREFIXES = ("whsec_",)
DERIVATIONS = ("standard", "raw_full", "raw_suffix")
SIGNATURE_REFUSALS = (400, 401, 403)


def secret_ok(secret: str) -> bool:
    return secret.startswith(SECRET_PREFIXES) and len(secret) < 500


def hmac_key(secret: str, derivation: str) -> bytes:
    suffix = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    if derivation == "standard":
        return base64.b64decode(suffix + "=" * (-len(suffix) % 4))
    if derivation == "raw_full":
        return secret.encode()
    return suffix.encode()


def headers(secret: str, derivation: str, body: bytes, msg_id: str, ts: int) -> dict:
    content = f"{msg_id}.{ts}.".encode() + body
    signature = base64.b64encode(hmac.new(hmac_key(secret, derivation), content, hashlib.sha256).digest()).decode()
    return {"content-type": "application/json", "webhook-id": msg_id, "webhook-timestamp": str(ts),
            "webhook-signature": f"v1,{signature}"}


def _current(state: dict) -> str:
    return state.get("trying") or state.get("derivation") or DERIVATIONS[0]


def build(url: str, secret: str, body: bytes, state: dict) -> dict:
    return {"headers": headers(secret, _current(state), body, f"msg_{secrets.token_hex(12)}", int(time.time()))}


def on_response(status: int, state: dict) -> dict:
    tried = _current(state)
    if 200 <= status < 300:
        return {"derivation": tried}
    learned = state.get("derivation") if state.get("derivation") in DERIVATIONS else None
    if status in SIGNATURE_REFUSALS and not learned:
        later = DERIVATIONS[DERIVATIONS.index(tried) + 1:] if tried in DERIVATIONS else ()
        if later:
            return {"trying": later[0], "retry": True}
    return {"derivation": learned} if learned else {}
