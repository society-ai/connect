"""Wake styles: how Society Connect wakes a cloud agent that pulls its messages (docs/contribute/wake-styles.md).

The only code from this repo that runs on Society AI's servers. Society Connect loads it from a pinned
release, checks the trigger URL against the type's manifest and its own guards, and keeps everything
else (rate limits, coalescing, the hold queue) itself. A style gets no database, no tokens and no
message content: only the trigger URL, the key the user saved, the fixed wake-up body and its own
small state (what it learned from earlier attempts).

Each style module has:

    secret_ok(secret) -> bool                    does the key look like this style's key
    build(url, secret, body, state) -> request   the request to send: {"headers": {...}}
    on_response(status, state) -> state          what to remember; state["retry"] = True asks to try
                                                 again at once (at most MAX_ATTEMPTS per wake-up)
"""

from . import bearer, standard_webhooks

STYLES = {"bearer": bearer, "standard_webhooks": standard_webhooks}
MAX_ATTEMPTS = 4
