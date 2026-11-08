"""Wake styles (wake/): what Society Connect sends to wake a cloud agent."""

import base64
import hashlib
import hmac
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from wake import STYLES, bearer, standard_webhooks  # noqa: E402

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
        self.assertEqual(set(STYLES), {"bearer", "standard_webhooks"})


if __name__ == "__main__":
    unittest.main()
