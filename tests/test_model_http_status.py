"""Generated HTTP failures: no private key, actual network or error-body read."""

import io
import json
import unittest
import urllib.error

from backend.contracts import ServiceError
from backend.gemini import GeminiClient, http_failure


class HttpStatusTests(unittest.TestCase):
    def test_generic_status_is_persistent_bounded_error_code(self):
        for status in (400, 408, 413, 415, 422, 500, 502, 503, 504, 599):
            with self.subTest(status=status):
                error = http_failure(status, "application/json")
                self.assertEqual(error.code, f"model_http_{status}")
                self.assertIn(f"HTTP {status}", error.message)
                self.assertTrue(error.retryable)

    def test_existing_auth_limit_endpoint_and_redirect_categories_preserved(self):
        for status, media, expected in ((302, "", "model_redirect"), (401, "application/json", "model_auth"),
                (403, "application/xhtml+xml", "model_gateway_denied"), (404, "", "model_endpoint"),
                (429, "", "model_rate_limit")):
            self.assertEqual(http_failure(status, media).code, expected)

    def test_untrusted_non_numeric_status_never_interpolated(self):
        for status in ("generated-private-reflection", True, None, 502.0, -1, 600, 100000000):
            error = http_failure(status, "")
            self.assertEqual(error.code, "model_http_error")
            self.assertNotIn("generated-private-reflection", json.dumps(error.payload()))

    def test_error_headers_and_body_not_read_or_returned(self):
        marker = "generated-private-reflection"
        class Body(io.BytesIO):
            def read(self, *args):
                raise AssertionError("must never read provider error body")
        body = Body(marker.encode())
        class Opener:
            count = 0
            def open(self, request, timeout):
                self.count += 1
                raise urllib.error.HTTPError(request.full_url, 502, marker,
                    {"Content-Type": "text/html", "X-Error": marker}, body)
        opener = Opener()
        with self.assertRaises(ServiceError) as caught:
            GeminiClient(opener=opener).generate({"base_url": "https://generated.invalid", "model": "generated"}, marker, "generated", json_response=False)
        self.assertEqual(caught.exception.code, "model_http_502")
        self.assertNotIn(marker, json.dumps(caught.exception.payload()))
        self.assertEqual(opener.count, 1)
        self.assertTrue(body.closed)

    def test_network_timeout_does_not_invent_http_status(self):
        class Opener:
            def open(self, request, timeout):
                raise TimeoutError()
        with self.assertRaises(ServiceError) as caught:
            GeminiClient(opener=Opener()).generate({"base_url": "https://generated.invalid", "model": "generated"}, "generated-key", "generated", json_response=False)
        self.assertEqual(caught.exception.code, "model_timeout_unknown")

    def test_status_does_not_claim_free_or_definite_provider_cause(self):
        error = http_failure(500, "")
        self.assertIn("不足以确定具体原因", error.message)
        self.assertIn("实际用量可能未知", error.message)
        self.assertNotIn("免费", error.message)


if __name__ == "__main__":
    unittest.main()
