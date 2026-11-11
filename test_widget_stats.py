import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from widget_stats import DB_PATH, WidgetStats, record_widget_request


def message(username='alice', text='/widget'):
    return {'from':{'id':123,'username':username},'chat':{'id':123,'type':'private'},
            'message_id':9,'date':1700000000,'text':text}


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'data'/'widget_stats.sqlite3'
        self.stats = WidgetStats(self.path)

    def rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute('SELECT * FROM widget_requests ORDER BY bot_id, update_id').fetchall()

    def test_path_and_insert(self):
        self.assertEqual(str(DB_PATH),'/data/widget_stats.sqlite3')
        self.assertTrue(self.stats.append(1,2,message()))
        row=self.rows()[0]
        self.assertEqual(row[:6],(1,2,123,'alice',123,9))
        self.assertEqual(row[6],'2023-11-14T22:13:20+00:00')
        self.assertTrue(row[7].endswith('+00:00'))

    def test_no_username_and_parameter_binding(self):
        self.stats.append(1,1,message(None))
        self.stats.append(1,2,message("'; DROP TABLE widget_requests; --"))
        self.assertIsNone(self.rows()[0][3])
        self.assertEqual(len(self.rows()),2)

    def test_replay_does_not_overwrite_username(self):
        self.stats.append(1,1,message())
        self.assertFalse(self.stats.append(1,1,message('changed')))
        self.assertEqual(self.rows()[0][3],'alice')

    def test_new_events_and_bot_ids(self):
        for bot,update in [(1,1),(1,2),(2,1)]:
            self.stats.append(bot,update,message())
        self.assertEqual(len(self.rows()),3)

    def test_update_delete_replace_blocked_with_default_connection(self):
        self.stats.append(1,1,message())
        before=self.rows()
        with closing(sqlite3.connect(self.path)) as connection:
            statements=[('UPDATE widget_requests SET username=?',('changed',)),
                        ('DELETE FROM widget_requests',()),
                        ('INSERT OR REPLACE INTO widget_requests VALUES (?,?,?,?,?,?,?,?)',before[0])]
            for sql,values in statements:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql,values)
                connection.rollback()
        self.assertEqual(self.rows(),before)

    def test_restart_keeps_rows_and_guards(self):
        self.stats.append(1,1,message())
        again=WidgetStats(self.path)
        self.assertFalse(again.append(1,1,message()))
        self.assertTrue(again.append(1,2,message()))
        self.assertEqual(len(self.rows()),2)

    def test_concurrent_replay_is_one_row(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.stats.append(1,1,message()),range(20)))
        self.assertEqual(sum(results),1)
        self.assertEqual(len(self.rows()),1)

    def test_only_widget_messages(self):
        for text in ['/prices','/start','/widgetx','hello']:
            self.assertFalse(record_widget_request(self.stats,1,{'update_id':1,'message':message(text=text)}))
        self.assertFalse(record_widget_request(self.stats,1,{'update_id':1,'widget_query':{'data':'refresh'}}))
        for update,text in [(1,'/widget'),(2,'/widget@SomeBot')]:
            self.assertTrue(record_widget_request(self.stats,1,{'update_id':update,'message':message(text=text)}))
        self.assertEqual(len(self.rows()),2)

    def test_failure_not_silenced(self):
        self.path.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            self.stats.append(1,1,message())


if __name__=='__main__':
    unittest.main()
