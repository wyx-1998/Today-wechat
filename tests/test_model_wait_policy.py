"""Generated adapter wait and safe timeout classification; no network or keys."""

import io
import json
import unittest
import urllib.error

from backend.contracts import ServiceError, unknown_model_result
from backend.gemini import GeminiClient


CONFIG = {"base_url": "https://generated.invalid", "model": "generated"}
PROMPT = '输入：{"new_messages":[],"context_messages":[],"candidate_tasks":[]}'


class Opener:
    def __init__(self):
        self.waits = []

    def open(self, request, timeout):
        self.waits.append(timeout)
        return io.BytesIO(json.dumps({"candidates": [{"finishReason": "STOP",
            "content": {"parts": [{"text": '{"tasks":[],"candidate_decisions":[]}'}]}}]}).encode())


class ModelWaitPolicyTests(unittest.TestCase):
    def test_discovery_and_nonstructured_requests_keep_short_wait(self):
        opener = Opener()
        client = GeminiClient(opener=opener)
        client.generate(CONFIG, "generated-key", PROMPT, schema={"properties": {"tasks": {}}})
        client.generate(CONFIG, "generated-key", "固定问候", json_response=False)
        self.assertEqual(opener.waits, [40, 40])

    def test_full_candidate_ledger_uses_longer_wait_without_extra_request(self):
        opener = Opener()
        client = GeminiClient(opener=opener)
        client.generate(CONFIG, "generated-key", PROMPT, schema={"properties": {"tasks": {}, "candidate_decisions": {}}})
        self.assertEqual(opener.waits, [120])

    def test_explicit_wait_values_are_separate_and_validated(self):
        opener = Opener()
        client = GeminiClient(timeout=5, reconciliation_timeout=75, opener=opener)
        client.generate(CONFIG, "generated-key", "固定问候", json_response=False)
        client.generate(CONFIG, "generated-key", PROMPT, schema={"properties": {"tasks": {}, "candidate_decisions": {}}})
        self.assertEqual(opener.waits, [5, 75])
        for invalid in (True, None, 0, -1, float("inf"), float("nan"), 601, "120"):
            for field in ("timeout", "reconciliation_timeout"):
                with self.assertRaises(ValueError):
                    GeminiClient(**{field: invalid})

    def test_wrapped_timeout_is_unknown_without_error_reason_echo_or_retry(self):
        class Failed:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                raise urllib.error.URLError(TimeoutError("generated-private-error"))
        opener = Failed()
        with self.assertRaises(ServiceError) as caught:
            GeminiClient(opener=opener).generate(CONFIG, "generated-key", "固定问候", json_response=False)
        self.assertEqual(caught.exception.code, "model_timeout_unknown")
        self.assertTrue(unknown_model_result(caught.exception.code))
        self.assertNotIn("generated-private-error", json.dumps(caught.exception.payload()))
        self.assertEqual(opener.calls, 1)

    def test_response_read_timeout_closes_response_and_retains_unknown_semantics(self):
        class Reply(io.BytesIO):
            def read(self, *args):
                raise TimeoutError("generated-private-error")
        response = Reply()
        class Reading:
            def open(self, request, timeout):
                return response
        with self.assertRaises(ServiceError) as caught:
            GeminiClient(opener=Reading()).generate(CONFIG, "generated-key", "固定问候", json_response=False)
        self.assertEqual(caught.exception.code, "model_timeout_unknown")
        self.assertTrue(response.closed)
        self.assertNotIn("generated-private-error", caught.exception.message)

    def test_non_timeout_network_error_stays_generic_and_http_is_not_unknown(self):
        class Failed:
            def open(self, request, timeout):
                raise urllib.error.URLError("generated-private-error")
        with self.assertRaises(ServiceError) as caught:
            GeminiClient(opener=Failed()).generate(CONFIG, "generated-key", "固定问候", json_response=False)
        self.assertEqual(caught.exception.code, "model_result_unknown")
        self.assertTrue(unknown_model_result("model_result_unknown"))
        self.assertFalse(unknown_model_result("model_http_504"))
        self.assertNotIn("generated-private-error", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
