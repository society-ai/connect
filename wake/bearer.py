"""The key as a bearer token (Grok Bot routines on api2.cursor.sh, with a crsr_ key)."""

SECRET_PREFIXES = ("crsr_",)


def secret_ok(secret: str) -> bool:
    return secret.startswith(SECRET_PREFIXES) and len(secret) < 500


def build(url: str, secret: str, body: bytes, state: dict) -> dict:
    return {"headers": {"content-type": "application/json", "authorization": f"Bearer {secret}"}}


def on_response(status: int, state: dict) -> dict:
    return {"derivation": "bearer"}
