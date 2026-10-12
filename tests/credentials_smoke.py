"""Generated credential sessions; no OS keychain, private files, or network."""

import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from backend.contracts import ServiceError
from backend.credentials import CredentialSession, system_credentials
from backend.service import LocalService
from tests.model_smoke import FakeClient, KEY, install_legacy_config


class Provider:
    def __init__(self):
        self.calls = []
        self.value = bytes(range(32))
        self.failure = None
        self.callback = None

    def master(self, account, *, create=False):
        self.calls.append((account, create))
        if self.callback:
            self.callback()
        if self.failure:
            raise self.failure
        return self.value


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.provider = Provider()
        self.session = CredentialSession(self.provider)
        self.addCleanup(self.session.close)

    def test_reuses_success_for_read_and_create_without_requery(self):
        self.assertEqual(self.session.master("vault", create=True), self.provider.value)
        for _ in range(5):
            self.assertEqual(self.session.master("vault"), self.provider.value)
        self.assertEqual(self.provider.calls, [("vault", True)])

    def test_distinct_vaults_do_not_share_unlock(self):
        self.session.master("one"); self.session.master("two")
        self.assertEqual(len(self.provider.calls), 2)

    def test_failure_suppressed_until_explicit_retry(self):
        self.provider.failure = ServiceError("keychain_denied", "generated private provider error")
        for _ in range(3):
            with self.assertRaises(ServiceError) as error:
                self.session.master("vault")
            self.assertEqual(error.exception.code, "keychain_denied")
            self.assertNotIn("private provider", str(error.exception))
        self.assertEqual(len(self.provider.calls), 1)
        self.provider.failure = None
        self.session.retry_access()
        self.assertEqual(self.session.master("vault"), self.provider.value)
        self.assertEqual(len(self.provider.calls), 2)

    def test_missing_failure_never_creates_a_replacement_implicitly(self):
        self.provider.failure = ServiceError("keychain_missing", "generated missing")
        with self.assertRaises(ServiceError):
            self.session.master("existing")
        with self.assertRaises(ServiceError):
            self.session.master("existing", create=True)
        self.assertEqual(self.provider.calls, [("existing", False)])

    def test_malformed_provider_value_is_not_cached(self):
        for value in (None, b"short", "a" * 32):
            with self.subTest(value_type=type(value).__name__):
                self.session.invalidate(); self.provider.value = value
                with self.assertRaises(ServiceError) as error:
                    self.session.master("vault")
                self.assertEqual(error.exception.code, "keychain_invalid")
                self.assertEqual(self.session.values, {})

    def test_unknown_provider_exception_is_sanitized(self):
        self.provider.failure = RuntimeError("generated-secret-bearing-exception")
        with self.assertRaises(ServiceError) as error:
            self.session.master("vault")
        self.assertEqual(error.exception.code, "keychain_unavailable")
        self.assertNotIn("secret-bearing", str(error.exception))
        self.assertEqual(self.session.failures, {"vault": "keychain_unavailable"})

    def test_invalid_reference_never_reaches_provider(self):
        for account, create in (("", False), (None, False), ("vault", 1)):
            with self.assertRaises(ServiceError):
                self.session.master(account, create=create)
        self.assertEqual(self.provider.calls, [])

    def test_invalidate_and_new_session_require_new_unlock(self):
        self.session.master("vault"); self.session.invalidate()
        self.assertEqual(self.session.values, {})
        self.session.master("vault"); self.session.close()
        other = CredentialSession(self.provider)
        self.addCleanup(other.close)
        other.master("vault")
        self.assertEqual(len(self.provider.calls), 3)

    def test_close_clears_values_and_refuses_future_unlock(self):
        self.session.master("vault"); self.session.close()
        self.assertEqual(self.session.values, {})
        with self.assertRaises(ServiceError) as error:
            self.session.master("vault")
        self.assertEqual(error.exception.code, "service_closed")
        self.assertEqual(len(self.provider.calls), 1)

    def pending_threads(self, count):
        entered, release = threading.Event(), threading.Event()
        def wait():
            entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("generated unlock timeout")
        self.provider.callback = wait
        results = []
        def run():
            try:
                results.append(self.session.master("vault"))
            except ServiceError as error:
                results.append(error.code)
        threads = [threading.Thread(target=run) for _ in range(count)]
        for thread in threads:
            thread.start()
        self.assertTrue(entered.wait(timeout=1))
        return threads, release, results

    def finish_threads(self, threads, release):
        release.set()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

    def test_concurrent_unlocks_coalesce(self):
        threads, release, results = self.pending_threads(3)
        try:
            self.assertEqual(len(self.provider.calls), 1)
        finally:
            self.finish_threads(threads, release)
        self.assertEqual(results, [self.provider.value] * 3)
        self.assertEqual(len(self.provider.calls), 1)

    def test_close_during_os_prompt_does_not_wait_or_cache_late_result(self):
        threads, release, results = self.pending_threads(2)
        try:
            self.session.close()
            self.assertEqual(self.session.values, {})
        finally:
            self.finish_threads(threads, release)
        self.assertEqual(results, ["service_closed", "service_closed"])
        self.assertEqual(self.session.values, {})

    def test_invalidate_during_prompt_rejects_late_result(self):
        threads, release, results = self.pending_threads(1)
        try:
            self.session.invalidate()
        finally:
            self.finish_threads(threads, release)
        self.assertEqual(results, ["credential_session_changed"])
        self.assertEqual(self.session.values, {})

    def test_unsupported_platform_has_no_plaintext_fallback(self):
        with patch("backend.credentials.sys.platform", "win32"):
            session = system_credentials()
        self.addCleanup(session.close)
        with self.assertRaises(ServiceError) as error:
            session.master("vault", create=True)
        self.assertEqual(error.exception.code, "keychain_unavailable")

    def service(self):
        temp = tempfile.TemporaryDirectory(prefix="generated-credentials-")
        self.addCleanup(temp.cleanup)
        client = FakeClient()
        service = LocalService(temp.name, keychain=self.session, model_client=client)
        self.addCleanup(service.close)
        service.models.save(base_url="https://synthetic.invalid", model="generated-flash", api_key=KEY)
        return service, client

    def test_multiple_local_model_requests_never_unlock_and_only_database_has_key(self):
        service, client = self.service()
        service.models.test(authorize=True); service.models.test(authorize=True)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(self.provider.calls), 0)
        self.assertNotIn(KEY, json.dumps(service.models.public()))
        self.assertIn(KEY.encode(), service.store.path.read_bytes())
        service.close()
        self.assertEqual(self.session.values, {})

    def test_cache_never_substitutes_for_authorization_or_pause_guard(self):
        service, client = self.service()
        with self.assertRaises(ServiceError):
            service.models.test(authorize=False)
        def stopped():
            raise ServiceError("analysis_paused", "generated paused gate")
        with self.assertRaises(ServiceError):
            service.models.request("generated", purpose="text_analysis", authorize=True, guard=stopped)
        self.assertEqual(client.calls, [])
        self.assertEqual(service.models.usage()["today"]["requests"], 0)

    def test_model_disable_and_reenable_do_not_unlock_local_credentials(self):
        service, client = self.service()
        service.models.enable(enabled=False)
        self.assertEqual(self.session.values, {})
        with self.assertRaises(ServiceError):
            service.models.test(authorize=True)
        self.assertEqual(len(self.provider.calls), 0)
        service.models.enable(enabled=True)
        service.models.test(authorize=True)
        self.assertEqual(len(self.provider.calls), 0)
        self.assertEqual(len(client.calls), 1)

    def test_public_state_does_not_unlock_credentials(self):
        service, client = self.service()
        self.session.invalidate()
        self.provider.failure = ServiceError("keychain_denied", "generated denial")
        for _ in range(3):
            service.state(); service.models.public(); service.models.usage()
        self.assertEqual(len(self.provider.calls), 0)
        self.assertEqual(client.calls, [])

    def test_service_close_during_unlock_prevents_network_and_late_cache(self):
        service, client = self.service()
        install_legacy_config(service)
        self.session.invalidate()
        entered, release = threading.Event(), threading.Event()
        def wait():
            entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("generated wait timeout")
        self.provider.callback = wait
        outcome = []
        def request():
            try:
                service.models.migrate(revision=service.models.public()["revision"], confirm=True)
            except ServiceError as error:
                outcome.append(error.code)
        thread = threading.Thread(target=request)
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=1))
            service.close()
        finally:
            release.set(); thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome, ["service_closed"])
        self.assertEqual(client.calls, [])
        self.assertEqual(self.session.values, {})

    def test_local_batch_retry_ignores_keychain_and_keeps_frozen_input(self):
        from tests.automatic_smoke import AutomaticTests
        scenario = AutomaticTests("runTest")
        scenario.setUp()
        self.addCleanup(scenario.doCleanups)
        scenario.service.models.keychain = self.session
        self.provider.failure = ServiceError("keychain_denied", "generated denied")
        def fail():
            raise ServiceError("model_result_unknown", "generated transport failure")
        scenario.client.callback = fail
        scenario.ready()
        self.assertEqual(scenario.service.automatic.tick()["status"], "unknown")
        batch = scenario.batches()[0]
        self.assertEqual(scenario.service.automatic.tick()["status"], "waiting")
        self.assertEqual(len(self.provider.calls), 0)
        scenario.client.callback = None
        with self.assertRaises(ServiceError):
            scenario.service.automatic.retry(batch_id=batch["batch_id"], authorize=False)
        scenario.service.automatic.retry(batch_id=batch["batch_id"], authorize=True)
        self.assertEqual(scenario.service.automatic.tick()["status"], "success")
        self.assertEqual(len(self.provider.calls), 0)
        self.assertEqual([m["source_message_id"] for m in scenario.client.payloads[0]], ["initial"])

    def test_disable_during_unlock_does_not_wait_or_start_network(self):
        service, client = self.service()
        install_legacy_config(service)
        self.session.invalidate()
        entered, release, disabled = threading.Event(), threading.Event(), threading.Event()
        def wait():
            entered.set()
            if not release.wait(timeout=3):
                raise RuntimeError("generated wait timeout")
        self.provider.callback = wait
        outcome = []
        def request():
            try:
                service.models.migrate(revision=service.models.public()["revision"], confirm=True)
            except ServiceError as error:
                outcome.append(error.code)
        requester = threading.Thread(target=request)
        def disable():
            service.models.enable(enabled=False); disabled.set()
        requester.start()
        disabler = None
        try:
            self.assertTrue(entered.wait(timeout=1))
            disabler = threading.Thread(target=disable)
            disabler.start()
            self.assertTrue(disabled.wait(timeout=1))
        finally:
            release.set(); requester.join(timeout=3)
            if disabler:
                disabler.join(timeout=3)
        self.assertFalse(requester.is_alive())
        self.assertEqual(outcome, ["credential_session_changed"])
        self.assertEqual(client.calls, [])
        self.assertEqual(self.session.values, {})


if __name__ == "__main__":
    unittest.main()
