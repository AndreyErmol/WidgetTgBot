import json
import sqlite3
import tempfile
import unittest
import zlib
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from admin_panel import AdminPanel, MSK

ADMIN = 804664389
NOW = datetime(2026, 11, 11, 14, 0, tzinfo=MSK)
TABLE = ('CREATE TABLE price_notifications (user_id INTEGER PRIMARY KEY, '
         'minute INTEGER NOT NULL, slot INTEGER NOT NULL, next_run INTEGER NOT NULL, '
         'last_day TEXT, failures INTEGER NOT NULL DEFAULT 0)')


class NotificationStatistics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'stats.sqlite3'
        self.sql('CREATE TABLE widget_requests(update_id INTEGER PRIMARY KEY,user_id INTEGER,username TEXT,requested_at TEXT)')
        self.app = SimpleNamespace(api=Mock(return_value={'message_id': 42}))
        self.panel = AdminPanel(self.app, self.path)

    def sql(self, sql, args=()):
        with closing(sqlite3.connect(self.path)) as c, c:
            return c.execute(sql, args).fetchall()

    def enable(self, uid, hour, minute):
        self.sql('INSERT INTO price_notifications(user_id,minute,slot,next_run) VALUES (?,?,0,0)', (uid, hour * 60 + minute))

    def test_works_before_notification_table_exists(self):
        data = self.panel.snapshot(ADMIN, '2026-11-11')
        self.assertEqual(data['nt'], 0)
        data = self.panel.snapshot(ADMIN, '2026-11-11', 2)
        self.assertEqual((data['nt'], data['rows'], data['day']), (0, [], 2))

    def test_card_counts_all_enabled_including_admins_on_every_day(self):
        self.sql(TABLE)
        for uid in (ADMIN, 1, 2):
            self.enable(uid, 9, 0)
        for day in (0, 1, 2):
            self.assertEqual(self.panel.snapshot(ADMIN, '2026-11-11', day)['nt'], 3)

    def test_list_sorted_by_time_with_known_usernames(self):
        self.sql(TABLE)
        self.sql("INSERT INTO widget_requests VALUES (1,20,'old','2026-11-10T10:00:00+00:00')")
        self.sql("INSERT INTO widget_requests VALUES (2,20,'alice','2026-11-11T10:00:00+00:00')")
        self.enable(30, 21, 5)
        self.enable(20, 9, 30)
        self.enable(10, 9, 30)
        data = self.panel.snapshot(ADMIN, '2026-11-11', 2)
        self.assertEqual(data['rows'], [[10, None, '09:30'], [20, 'alice', '09:30'], [30, None, '21:05']])
        self.assertEqual((data['users'], data['calls'], data['nt'], data['offset']), (3, 3, 3, 0))

    def test_pagination_covers_every_user_once_within_size_limit(self):
        self.sql(TABLE)
        for uid in range(1, 18):
            self.enable(uid, uid % 24, uid % 60)
            self.sql('INSERT INTO widget_requests VALUES (?,?,?,?)', (uid, uid, 'n' * 40, '2026-11-11T10:00:00+00:00'))
        offset, seen = 0, []
        while offset < 17:
            data = self.panel.snapshot(ADMIN, '2026-11-11', 2, offset)
            self.assertLessEqual(len(json.dumps(data, ensure_ascii=False).encode()), 1024)
            self.assertTrue(data['rows'])
            seen += [r[0] for r in data['rows']]
            offset += len(data['rows'])
        self.assertEqual(sorted(seen), list(range(1, 18)))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(self.panel.snapshot(ADMIN, '2026-11-11', 2, 999)['offset'], 16)

    def test_non_admin_gets_nothing(self):
        self.sql(TABLE)
        self.enable(1, 9, 0)
        self.assertIsNone(self.panel.snapshot(123, '2026-11-11', 2))
        self.assertIsNone(self.panel.snapshot(123, '2026-11-11', 0))

    def test_query_returns_list_only_to_owner_admin(self):
        self.sql(TABLE)
        self.enable(1, 9, 0)
        self.sql('INSERT INTO statistic_widgets VALUES (?,?,?,?)', (ADMIN, 42, ADMIN, '2026-11-11'))
        query = {'id': 'q', 'from': {'id': ADMIN}, 'chat': {'id': ADMIN, 'type': 'private'}, 'message_id': 42, 'data': 'stats:2:0'}
        self.panel.handle_query(query)
        data = json.loads(self.app.api.call_args.kwargs['data'])
        self.assertEqual((data['day'], data['rows']), (2, [[1, None, '09:00']]))
        for bad in [dict(query, data='stats:3:0'), dict(query, message_id=43), dict(query, data='stats:2:-1'),
                    dict(query, **{'from': {'id': 5}, 'chat': {'id': 5, 'type': 'private'}})]:
            self.panel.handle_query(bad)
            self.assertNotIn('data', self.app.api.call_args.kwargs)
        self.panel.handle_query(query, shared=True)
        self.assertNotIn('data', self.app.api.call_args.kwargs)

    def test_sent_statistic_contains_card_value(self):
        self.sql(TABLE)
        self.enable(1, 9, 0)
        with patch('admin_panel.Path.read_text', return_value='<b>x</b>'):
            self.panel.send_statistic(ADMIN, NOW)
        self.assertEqual(json.loads(self.app.api.call_args.kwargs['chat_data'])['nt'], 1)

    def test_html_has_card_tab_and_follows_profile(self):
        raw = (Path(__file__).parent / 'statistic.html').read_bytes()
        html = raw.decode()
        self.assertLessEqual(len(raw), 32768)
        self.assertLessEqual(len(zlib.compress(raw, 9)), 4096)
        for needed in ('Уведомления включены у', "id=\"alerts\"", "ask(2,0,false)", 'пользователя', 'пользователей'):
            self.assertIn(needed, html)
        for forbidden in ('<select', '<input', 'fetch('):
            self.assertNotIn(forbidden, html)


if __name__ == '__main__':
    unittest.main()
