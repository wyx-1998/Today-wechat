"""Bounded transport classification without real hosts, credentials or bodies."""

import http.client
import io
import json
import socket
import ssl
import unittest
import urllib.error

from backend.contracts import ServiceError, unknown_model_result
from backend.gemini import GeminiClient, transport_failure


class ModelTransportTests(unittest.TestCase):
    def failure(self, exception, *, reading=False):
        class Reply(io.BytesIO):
            def read(self, *args):
                raise exception
        reply = Reply()
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                if reading:
                    return reply
                raise exception
        opener = Opener()
        with self.assertRaises(ServiceError) as caught:
            GeminiClient(opener=opener).generate({"base_url":"https://generated.invalid", "model":"generated"},
                "PRIVATE_GENERATED_KEY", "GENERATED_CHAT_BODY", json_response=False)
        self.assertEqual(opener.calls, 1)
        self.assertTrue(unknown_model_result(caught.exception.code))
        for private in ("PRIVATE_GENERATED_KEY", "PRIVATE_EXCEPTION_TEXT", "PRIVATE_PARTIAL_RESPONSE", "GENERATED_CHAT_BODY"):
            self.assertNotIn(private, json.dumps(caught.exception.payload()))
        if reading:
            self.assertTrue(reply.closed)
        return caught.exception.code

    def test_direct_or_wrapped_remote_close_has_fixed_classification(self):
        for exception in (http.client.RemoteDisconnected("PRIVATE_EXCEPTION_TEXT"),
                          urllib.error.URLError(http.client.RemoteDisconnected("PRIVATE_EXCEPTION_TEXT")),
                          ssl.SSLEOFError("PRIVATE_EXCEPTION_TEXT"), EOFError("PRIVATE_EXCEPTION_TEXT")):
            with self.subTest(kind=type(exception).__name__):
                self.assertEqual(self.failure(exception), "model_connection_closed_unknown")

    def test_reset_and_aborted_connection_do_not_become_timeouts(self):
        for kind in (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            with self.subTest(kind=kind.__name__):
                self.assertEqual(self.failure(kind("PRIVATE_EXCEPTION_TEXT")), "model_connection_reset_unknown")

    def test_partial_http_body_is_not_used_or_exposed(self):
        self.assertEqual(self.failure(http.client.IncompleteRead(b"PRIVATE_PARTIAL_RESPONSE", 999), reading=True),
                         "model_response_incomplete_unknown")

    def test_dns_failure_has_fixed_classification(self):
        self.assertEqual(self.failure(urllib.error.URLError(socket.gaierror(8, "PRIVATE_EXCEPTION_TEXT"))), "model_dns_unknown")

    def test_tls_certificate_failure_never_relaxes_validation_or_logs_certificate_text(self):
        self.assertEqual(self.failure(ssl.SSLCertVerificationError(1, "PRIVATE_EXCEPTION_TEXT")), "model_tls_unknown")

    def test_bad_http_status_line_is_not_exposed(self):
        self.assertEqual(self.failure(http.client.BadStatusLine("PRIVATE_EXCEPTION_TEXT")), "model_http_protocol_unknown")

    def test_timeout_stays_unknown_and_response_is_closed(self):
        self.assertEqual(self.failure(TimeoutError("PRIVATE_EXCEPTION_TEXT"), reading=True), "model_timeout_unknown")

    def test_unclassified_exception_message_is_never_interpreted_as_timeout(self):
        for exception in (OSError("timeout PRIVATE_EXCEPTION_TEXT"), urllib.error.URLError("reset PRIVATE_EXCEPTION_TEXT")):
            self.assertEqual(self.failure(exception), "model_result_unknown")

    def test_new_unknown_codes_are_an_explicit_allowlist(self):
        self.assertFalse(unknown_model_result("model_http_504"))
        self.assertFalse(unknown_model_result("PRIVATE_UNKNOWN_VALUE"))
        self.assertEqual(transport_failure("socket timed out PRIVATE_EXCEPTION_TEXT").code, "model_result_unknown")


if __name__ == "__main__":
    unittest.main()
