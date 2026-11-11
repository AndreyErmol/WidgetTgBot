import json
import sqlite3
import tempfile
import unittest
import zlib
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from user_notifications import GRACE, MSK, PriceNotifications

NOW = datetime(2026, 11, 11, 14, 0, tzinfo=MSK)
BLOCKED = 'Telegram/sendSharedWidget: HTTP 403: Forbidden: bot was blocked by the user'


class Failure(Exception):
    def __init__(self, message, code=0):
        super().__init__(message)
        self.code = code


def at(hour, minute, second=0, day=11):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=MSK)


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'stats.sqlite3'
        self.app = SimpleNamespace(api=Mock(return_value={'message_id': 1}), send_widget=Mock())
        self.service = PriceNotifications(self.app, self.path)

    def rows(self, sql='SELECT user_id,minute,slot,next_run,last_day,failures FROM price_notifications ORDER BY user_id'):
        with closing(sqlite3.connect(self.path)) as c:
            return c.execute(sql).fetchall()

    def query(self, data, uid=1, chat_id=None, kind='private'):
        return {'id': 'q', 'from': {'id': uid}, 'chat': {'id': uid if chat_id is None else chat_id, 'type': kind},
                'message_id': 5, 'data': data}

    def answer(self):
        call = self.app.api.call_args
        self.assertEqual(call.args[0], 'answerWidgetQuery')
        self.assertEqual(call.kwargs['widget_query_id'], 'q')
        return call.kwargs

    def test_default_state_is_off(self):
        self.assertEqual(self.service.state(1, NOW), {'on': False, 'h': 9, 'm': 0})

    def test_time_is_today_or_tomorrow_in_moscow(self):
        self.assertTrue(self.service.set_time(1, '15:30', NOW))
        self.assertTrue(self.service.set_time(2, '09:00', NOW))
        self.assertEqual(self.service.state(1)['next'], '11.11.2026 15:30')
        self.assertEqual(self.service.state(2)['next'], '12.11.2026 09:00')
        self.assertEqual(self.service.state(2)['h'], 9)

    def test_invalid_times_rejected(self):
        for bad in ['24:00', '09:60', '9:30', 'abc', '09:30 ', '', None, 930]:
            self.assertFalse(self.service.set_time(1, bad, NOW))
        self.assertFalse(self.rows())

    def test_disable_removes_row_and_is_idempotent(self):
        self.service.set_time(1, '10:00', NOW)
        self.assertTrue(self.service.disable(1))
        self.assertFalse(self.service.disable(1))
        self.assertFalse(self.rows())
        self.assertFalse(self.service.state(1)['on'])

    def test_change_time_after_today_sent_waits_for_tomorrow(self):
        self.service.set_time(1, '14:30', NOW)
        self.service.tick(at(14, 30, 10))
        self.service.set_time(1, '18:00', at(14, 40))
        self.assertEqual(self.service.state(1)['next'], '12.11.2026 18:00')

    def test_query_set_get_off(self):
        self.assertTrue(self.service.handle_query(self.query('notify:set:10:05'), NOW))
        data = json.loads(self.answer()['data'])
        self.assertEqual((data['on'], data['h'], data['m']), (True, 10, 5))
        self.service.handle_query(self.query('notify:get'), NOW)
        self.assertEqual(json.loads(self.answer()['data'])['m'], 5)
        self.service.handle_query(self.query('notify:off'), NOW)
        self.assertEqual(json.loads(self.answer()['data'])['on'], False)
        self.assertFalse(self.rows())

    def test_query_isolated_per_user(self):
        self.service.handle_query(self.query('notify:set:10:00', uid=1), NOW)
        self.service.handle_query(self.query('notify:set:21:15', uid=2), NOW)
        self.service.handle_query(self.query('notify:off', uid=1), NOW)
        self.assertEqual([(r[0], r[1]) for r in self.rows()], [(2, 1275)])

    def test_query_rejections_answer_without_data(self):
        cases = [self.query('notify:set:25:00'), self.query('notify:set:10:00', kind='group'),
                 self.query('notify:set:10:00', uid=1, chat_id=2), self.query('notify:other'),
                 self.query('notify:set:10:00', uid=-5)]
        for case in cases:
            self.app.api.reset_mock()
            self.assertTrue(self.service.handle_query(case, NOW))
            self.assertNotIn('data', self.answer())
        self.assertFalse(self.rows())

    def test_other_queries_are_not_taken(self):
        for data in ['refresh', 'stats:0:0', None, 5]:
            self.assertFalse(self.service.handle_query(self.query(data), NOW))
        self.app.api.assert_not_called()

    def test_allowed_user_restriction(self):
        service = PriceNotifications(self.app, self.path, allowed=5)
        self.assertFalse(service.set_time(6, '10:00', NOW))
        self.assertFalse(service.enqueue_settings(6))
        self.assertTrue(service.enqueue_settings(5))
        service.handle_query(self.query('notify:set:10:00', uid=6), NOW)
        self.assertNotIn('data', self.answer())
        self.service.set_time(6, '14:30', NOW)
        service.tick(at(14, 30, 5))
        self.assertFalse(self.rows())
        self.app.send_widget.assert_not_called()

    def test_not_due_does_not_send(self):
        self.service.set_time(1, '14:30', NOW)
        self.service.tick(at(14, 29, 59))
        self.app.send_widget.assert_not_called()

    def test_send_once_reschedule_and_restart_safe(self):
        self.service.set_time(1, '14:30', NOW)
        self.service.tick(at(14, 30, 10))
        self.app.send_widget.assert_called_once_with(1)
        self.service.tick(at(14, 30, 40))
        PriceNotifications(self.app, self.path).tick(at(14, 31))
        self.assertEqual(self.app.send_widget.call_count, 1)
        row = self.rows()[0]
        self.assertEqual(row[4], '2026-11-11')
        self.assertEqual(datetime.fromtimestamp(row[2], MSK), at(14, 30, day=12))
        self.service.tick(at(14, 30, 1, day=12))
        self.assertEqual(self.app.send_widget.call_count, 2)

    def test_late_occurrence_is_skipped_not_sent(self):
        self.service.set_time(1, '14:30', NOW)
        self.service.tick(at(14, 45, 1))
        self.app.send_widget.assert_not_called()
        row = self.rows()[0]
        self.assertEqual(datetime.fromtimestamp(row[2], MSK), at(14, 30, day=12))
        self.assertIsNone(row[4])

    def test_blocked_user_removed_and_others_still_served(self):
        self.service.set_time(1, '14:30', NOW)
        self.service.set_time(2, '14:30', NOW)
        self.app.send_widget.side_effect = lambda uid: (_ for _ in ()).throw(Failure(BLOCKED, 403)) if uid == 1 else None
        self.service.tick(at(14, 30, 5))
        self.assertEqual(self.app.send_widget.call_count, 2)
        self.assertEqual([r[0] for r in self.rows()], [2])
        self.service.tick(at(14, 30, 50))
        self.assertEqual(self.app.send_widget.call_count, 2)

    def test_chat_not_found_removed_but_rate_limit_and_network_kept(self):
        for uid in (1, 2, 3):
            self.service.set_time(uid, '14:30', NOW)
        errors = {1: Failure('Telegram/x: HTTP 400: Bad Request: chat not found', 400),
                  2: Failure('HTTP 429', 429), 3: Failure('timed out')}
        self.app.send_widget.side_effect = lambda uid: (_ for _ in ()).throw(errors[uid])
        self.service.tick(at(14, 30, 5))
        self.assertEqual([r[0] for r in self.rows()], [2, 3])
        self.assertTrue(all(r[5] == 1 for r in self.rows()))

    def test_transient_failure_retries_with_backoff_then_succeeds(self):
        self.service.set_time(1, '14:30', NOW)
        self.app.send_widget.side_effect = Failure('timed out')
        self.service.tick(at(14, 30, 5))
        row = self.rows()[0]
        self.assertEqual(row[5], 1)
        self.assertEqual(row[3], int(at(14, 30, 5).timestamp()) + 30)
        self.service.tick(at(14, 30, 20))
        self.assertEqual(self.app.send_widget.call_count, 1)
        self.app.send_widget.side_effect = None
        self.service.tick(at(14, 30, 40))
        self.assertEqual(self.app.send_widget.call_count, 2)
        row = self.rows()[0]
        self.assertEqual((row[4], row[5]), ('2026-11-11', 0))

    def test_persistent_failure_gives_up_after_grace_and_keeps_schedule(self):
        self.service.set_time(1, '14:30', NOW)
        self.app.send_widget.side_effect = Failure('timed out')
        for seconds in range(5, GRACE + 400, 30):
            self.service.tick(at(14, 30 + seconds // 60, seconds % 60))
        row = self.rows()[0]
        self.assertEqual(datetime.fromtimestamp(row[2], MSK), at(14, 30, day=12))

    def test_settings_change_during_send_is_not_overwritten(self):
        self.service.set_time(1, '14:30', NOW)
        old = self.rows()[0][2]
        self.service.set_time(1, '16:00', NOW)
        self.assertFalse(self.service.still(1, old))
        self.service.advance(1, old, 870, at(14, 31), True)
        row = self.rows()[0]
        self.assertEqual(row[1], 960)
        self.assertIsNone(row[4])

    def test_disabled_before_send_is_not_sent(self):
        self.service.set_time(1, '14:30', NOW)
        original = self.service.still
        self.service.still = lambda uid, slot: (self.service.disable(uid) and False) or original(uid, slot)
        self.service.tick(at(14, 30, 5))
        self.app.send_widget.assert_not_called()

    def test_block_events(self):
        self.service.set_time(1, '14:30', NOW)
        self.service.set_time(2, '14:30', NOW)
        self.assertFalse(self.service.handle_member({'chat': {'id': 1, 'type': 'private'}, 'new_chat_member': {'status': 'member'}}))
        self.assertFalse(self.service.handle_member({'chat': {'id': 2, 'type': 'group'}, 'new_chat_member': {'status': 'kicked'}}))
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(self.service.handle_member({'chat': {'id': 1, 'type': 'private'}, 'new_chat_member': {'status': 'kicked'}}))
        self.assertTrue(self.service.handle_member({'chat': {'id': 2, 'type': 'private'}, 'new_chat_member': {'status': 'left'}}))
        self.assertFalse(self.rows())

    def test_settings_widget_is_private_protected_with_state(self):
        self.service.set_time(1, '15:30', NOW)
        self.service.send_settings(1, NOW)
        call = self.app.api.call_args
        self.assertEqual(call.args[0], 'sendWidget')
        self.assertEqual(call.kwargs['chat_id'], 1)
        self.assertFalse(call.kwargs['is_shared'])
        self.assertTrue(call.kwargs['allow_queries'] and call.kwargs['protect_content'])
        self.assertNotIn('public_data', call.kwargs)
        self.assertEqual(json.loads(call.kwargs['chat_data'])['h'], 15)
        self.assertLessEqual(len(call.kwargs['chat_data'].encode()), 1024)
        self.assertTrue(call.kwargs['caption'])

    def test_settings_delivery_blocked_user_cleaned_up(self):
        self.service.set_time(1, '15:30', NOW)
        self.app.api.side_effect = Failure(BLOCKED, 403)
        self.service.deliver_settings(1)
        self.assertFalse(self.rows())

    def test_queue_limit(self):
        self.assertTrue(all(self.service.enqueue_settings(1) for _ in range(32)))
        self.assertFalse(self.service.enqueue_settings(1))

    def test_html_follows_widget_profile(self):
        raw = (Path(__file__).parent / 'notification_settings.html').read_bytes()
        html = raw.decode()
        self.assertLessEqual(len(raw), 32768)
        self.assertLessEqual(len(zlib.compress(raw, 9)), 4096)
        for forbidden in ('<select', '<input', '<textarea', 'fetch(', 'XMLHttpRequest'):
            self.assertNotIn(forbidden, html)
        for needed in ('notify:get', 'notify:off', 'notify:set:'):
            self.assertIn(needed, html)
        self.assertLessEqual(len(b'notify:set:23:59'), 64)

    def test_widget_stats_table_is_untouched(self):
        with closing(sqlite3.connect(self.path)) as c:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {'price_notifications'})


if __name__ == '__main__':
    unittest.main()
