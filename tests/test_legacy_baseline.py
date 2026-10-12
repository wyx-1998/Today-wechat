"""Check that diagnostics stay offline and descriptive, not product passing tests."""

import hashlib
import unittest
from unittest.mock import patch

from tests.legacy_baseline import COLLECTOR_SOURCE, observe_legacy


class LegacyBaselineTests(unittest.TestCase):
    def test_diagnostic_uses_synthetic_data_and_does_not_edit_collector(self):
        before = hashlib.sha256(COLLECTOR_SOURCE.read_bytes()).hexdigest()
        with patch("socket.socket", side_effect=AssertionError("No network")), patch("socket.create_connection", side_effect=AssertionError("No network")):
            report = observe_legacy()
        self.assertTrue(report["synthetic_only"])
        self.assertFalse(report["product_acceptance"])
        self.assertEqual(hashlib.sha256(COLLECTOR_SOURCE.read_bytes()).hexdigest(), before)
        checks = {item["check"] for item in report["observations"]}
        self.assertEqual(checks, {"full_original", "exact_message_window", "unknown_sender", "empty_vs_missing_table", "commit_failure_count", "message_insert_dedup"})
        for item in report["observations"]:
            self.assertIsInstance(item["gap"], bool)
            self.assertTrue(item["task"])
        # Deliberately do not assert that any observed bug must remain unfixed.


if __name__ == "__main__":
    unittest.main()
