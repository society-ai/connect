"""Wake styles (wake/): what Society Connect sends to wake a cloud agent."""

import base64
import hashlib
import hmac
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from wake import STYLES, bearer, mcp_events, standard_webhooks  # noqa: E402

BODY = b'{"type":"society.messages.available"}'


class Bearer(unittest.TestCase):
    def test_sends_the_key_as_a_bearer_token(self):
        self.assertEqual(bearer.build("https://x", "crsr_k", BODY, {})["headers"]["authorization"], "Bearer crsr_k")
        self.assertTrue(bearer.secret_ok("crsr_k"))
        self.assertFalse(bearer.secret_ok("whsec_k"))
        self.assertEqual(bearer.on_response(401, {}), {"derivation": "bearer"})


class StandardWebhooks(unittest.TestCase):
    SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef").decode()

    def test_the_signature_follows_the_standard(self):
        h = standard_webhooks.headers(self.SECRET, "standard", BODY, "msg_1", 1700000000)
        expected = base64.b64encode(hmac.new(b"0123456789abcdef", b"msg_1.1700000000." + BODY,
                                             hashlib.sha256).digest()).decode()
        self.assertEqual(h["webhook-signature"], f"v1,{expected}")
        self.assertEqual((h["webhook-id"], h["webhook-timestamp"]), ("msg_1", "1700000000"))

    def test_after_a_signature_refusal_the_next_derivation_is_tried_and_the_winner_kept(self):
        state = {}
        tried = []
        for status in (401, 401, 200):
            tried.append(standard_webhooks._current(state))
            state = standard_webhooks.on_response(status, state)
        self.assertEqual(tried, ["standard", "raw_full", "raw_suffix"])
        self.assertEqual(state, {"derivation": "raw_suffix"})

    def test_a_learned_derivation_is_kept_and_never_retried(self):
        self.assertEqual(standard_webhooks.on_response(401, {"derivation": "raw_full"}), {"derivation": "raw_full"})

    def test_other_failures_and_running_out_stop_without_learning(self):
        self.assertEqual(standard_webhooks.on_response(500, {}), {})
        self.assertEqual(standard_webhooks.on_response(401, {"trying": "raw_suffix"}), {})

    def test_every_manifest_style_exists(self):
        self.assertEqual(set(STYLES), {"bearer", "standard_webhooks", "mcp_events"})


class McpEvents(unittest.TestCase):
    SECRET = "whsec_" + base64.b64encode(b"k" * 32).decode()

    def test_secrets_follow_the_draft(self):
        self.assertTrue(mcp_events.secret_ok(self.SECRET))
        self.assertFalse(mcp_events.secret_ok("whsec_" + base64.b64encode(b"short").decode()))
        self.assertFalse(mcp_events.secret_ok("crsr_abc"))
        self.assertFalse(mcp_events.secret_ok("whsec_not base64!"))

    def test_an_event_carries_no_message_content_and_is_signed_with_its_event_id(self):
        import json
        event = mcp_events.payload("You have new Society AI messages.", {})
        self.assertEqual(set(event), {"eventId", "name", "timestamp", "data"})
        self.assertEqual(event["name"], "society.messages.waiting")
        body = json.dumps(event).encode()
        h = mcp_events.build("https://chatgpt.com/x", self.SECRET, body, {"subscription_id": "sub_1"})["headers"]
        self.assertEqual(h["webhook-id"], event["eventId"])  # ChatGPT's guide: webhook-id = eventId
        self.assertEqual(h["X-MCP-Subscription-Id"], "sub_1")
        expected = base64.b64encode(hmac.new(b"k" * 32, f"{h['webhook-id']}.{h['webhook-timestamp']}.".encode() + body,
                                             hashlib.sha256).digest()).decode()
        self.assertEqual(h["webhook-signature"], f"v1,{expected}")

    def test_verification_needs_the_same_challenge_back(self):
        import json
        challenge, body, h = mcp_events.verification(self.SECRET, "sub_1")
        self.assertEqual(json.loads(body), {"type": "verification", "challenge": challenge})
        self.assertTrue(h["webhook-id"].startswith("msg_verification_"))
        self.assertTrue(mcp_events.verified(200, json.dumps({"challenge": challenge}).encode(), challenge))
        self.assertFalse(mcp_events.verified(200, b'{"challenge": "other"}', challenge))
        self.assertFalse(mcp_events.verified(500, json.dumps({"challenge": challenge}).encode(), challenge))
        self.assertFalse(mcp_events.verified(200, b"not json", challenge))


if __name__ == "__main__":
    unittest.main()
