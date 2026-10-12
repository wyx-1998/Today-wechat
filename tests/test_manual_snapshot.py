"""Frozen manual requests with generated data and mocked model calls only."""
import unittest
from backend.contracts import ServiceError
from tests import automatic_smoke


class ManualSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.h = automatic_smoke.AutomaticTests('runTest')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.s = self.h.service
        self.s.resume()

    def preview(self):
        return self.s.dispatch('analysis.real.preview', {'chat_id': 'first'})

    def run_preview(self, preview):
        return self.s.dispatch('analysis.real', {'chat_id': 'first', 'authorize': True, 'preview_token': preview['preview_token']})

    def test_sync_does_not_need_to_stop_and_no_continuous_grant_is_created(self):
        preview = self.preview()
        self.assertEqual(self.h.client.calls, [])
        self.assertIn('time_end', preview)
        self.assertEqual(self.run_preview(preview)['status'], 'success')
        self.assertFalse(self.s.store.settings()['paused'])
        self.assertIsNone(self.s.automatic.public()['grant'])

    def test_new_and_late_messages_cannot_enlarge_preview(self):
        preview = self.preview()
        self.h.add('late', sent=self.h.now-20)
        self.h.now += 5
        self.h.add('later')
        self.run_preview(preview)
        self.assertEqual([r['source_message_id'] for r in self.h.client.payloads[0]], ['initial'])
        self.assertEqual({r['message_id'] for r in self.s.analysis.real_rows(self.s.store.settings(), 'first')}, {'late', 'later'})

    def test_selected_original_change_stops_before_send(self):
        preview = self.preview()
        with self.s.store.database() as db:
            db.execute("UPDATE messages SET content='different' WHERE message_id='initial'")
        with self.assertRaises(ServiceError): self.run_preview(preview)
        self.assertEqual(self.h.client.calls, [])

    def test_pause_then_resume_still_invalidates_preview(self):
        preview = self.preview()
        self.s.pause(); self.s.resume()
        with self.assertRaises(ServiceError): self.run_preview(preview)
        self.assertEqual(self.h.client.calls, [])

    def test_automatic_claim_after_preview_prevents_duplicate_send(self):
        preview = self.preview()
        self.h.grant(); self.h.now += 600
        self.s.automatic.tick()
        self.assertEqual(len(self.h.client.calls), 1)
        with self.assertRaises(ServiceError): self.run_preview(preview)
        self.assertEqual(len(self.h.client.calls), 1)

    def test_manual_claim_is_not_sent_again_by_automatic_queue(self):
        self.h.grant()
        self.run_preview(self.preview())
        self.h.now += 600
        self.s.automatic.tick()
        self.assertEqual(len(self.h.client.calls), 1)
        self.assertEqual(self.s.automatic.public()['pending_messages'], 0)

    def test_queued_automatic_batch_blocks_review_without_pausing_sync(self):
        self.h.grant(); self.h.now += 600
        with self.s.store.database() as db:
            grant = self.s.automatic.active(db)
            self.s.automatic.capture(db, grant); self.s.automatic.freeze(db, grant)
        with self.assertRaises(ServiceError) as error:
            self.s.reanalysis.preview(chat_id='first')
        self.assertEqual(error.exception.code, 'manual_batch_busy')
        self.assertEqual(self.h.client.calls, [])
        self.assertFalse(self.s.store.settings()['paused'])

    def test_preview_single_use_and_new_message_after_send_waits_for_later(self):
        preview = self.preview()
        self.h.client.callback = lambda: self.h.add('arriving', sent=self.h.now-2)
        self.run_preview(preview)
        with self.assertRaises(ServiceError): self.run_preview(preview)
        self.assertEqual([r['source_message_id'] for r in self.h.client.payloads[0]], ['initial'])
        self.assertEqual(len(self.h.client.calls), 1)

    def test_current_counters_do_not_promote_old_failed_batches(self):
        self.h.grant(); self.h.now += 600
        self.s.automatic.tick()
        self.s.store.set_range('2023-11-14T22:13:19+00:00')
        self.h.grant()
        state = self.s.automatic.public()
        self.assertEqual(state['running_batches'], 0)
        self.assertEqual(state['queued_batches'], 0)
        self.assertTrue(all(not b['current_scope'] for b in state['batches']))
