import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from widget_stats import COLUMNS, DB_PATH, WidgetStats, record_widget_request

OLD_SCHEMA = 'CREATE TABLE widget_requests (\nbot_id INTEGER NOT NULL, update_id INTEGER NOT NULL, user_id INTEGER NOT NULL,\nusername TEXT, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,\nrequested_at TEXT NOT NULL, recorded_at TEXT NOT NULL,\nPRIMARY KEY(bot_id,update_id)) WITHOUT ROWID;\nCREATE INDEX widget_requests_user_time ON widget_requests(user_id,requested_at);\nCREATE TRIGGER widget_requests_no_update BEFORE UPDATE ON widget_requests\nBEGIN SELECT RAISE(ABORT,\'append only\'); END;\nCREATE TRIGGER widget_requests_no_delete BEFORE DELETE ON widget_requests\nBEGIN SELECT RAISE(ABORT,\'append only\'); END;\nCREATE TRIGGER widget_requests_no_replace BEFORE INSERT ON widget_requests\nWHEN EXISTS(SELECT 1 FROM widget_requests WHERE bot_id=NEW.bot_id AND update_id=NEW.update_id)\nBEGIN SELECT RAISE(ABORT,\'append only\'); END;'


def message(username='alice', text='/widget'):
    return {'from':{'id':123,'username':username},'chat':{'id':123,'type':'private'},
            'message_id':9,'date':1700000000,'text':text}


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'widget_stats.sqlite3'

    def read(self, sql='SELECT * FROM widget_requests ORDER BY update_id', path=None):
        with closing(sqlite3.connect(path or self.path)) as c:
            return c.execute(sql).fetchall()

    def legacy(self, rows=None):
        if rows is None:
            rows=[(1,7,123,'oldname',123,9,'2023-11-14T22:13:20+00:00','old-recorded'),
                  (1,8,123,None,123,10,'2023-11-14T22:14:20+00:00','old-recorded')]
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute('PRAGMA journal_mode=WAL')
            c.executescript(OLD_SCHEMA)
            c.executemany('INSERT INTO widget_requests VALUES (?,?,?,?,?,?,?,?)',rows)
        return rows

    def test_fresh_database_and_utc(self):
        stats=WidgetStats(self.path)
        self.assertEqual(str(DB_PATH),'/data/widget_stats.sqlite3')
        self.assertTrue(stats.append(2,message()))
        self.assertEqual(self.read(),[(2,123,'alice','2023-11-14T22:13:20+00:00')])
        self.assertEqual(self.read('PRAGMA user_version'),[(2,)])
        self.assertIsNone(stats.backup_path)

    def test_null_and_sql_parameters(self):
        stats=WidgetStats(self.path)
        stats.append(1,message(None)); stats.append(2,message("'; DROP TABLE widget_requests; --"))
        self.assertIsNone(self.read()[0][2]); self.assertEqual(len(self.read()),2)

    def test_replay_does_not_overwrite(self):
        stats=WidgetStats(self.path)
        stats.append(1,message())
        self.assertFalse(stats.append(1,message('changed')))
        self.assertTrue(stats.append(2,message('changed')))
        self.assertEqual([r[2] for r in self.read()],['alice','changed'])

    def assert_guards(self):
        before=self.read()
        with closing(sqlite3.connect(self.path)) as c:
            for sql,values in [('UPDATE widget_requests SET username=?',('changed',)),
                               ('DELETE FROM widget_requests',()),
                               ('INSERT OR REPLACE INTO widget_requests VALUES (?,?,?,?)',before[0]),
                               ('INSERT INTO widget_requests VALUES (?,?,?,?) ON CONFLICT(update_id) DO UPDATE SET username=excluded.username',before[0])]:
                with self.assertRaises(sqlite3.IntegrityError):c.execute(sql,values)
                c.rollback()
        self.assertEqual(self.read(),before)

    def test_append_only(self):
        WidgetStats(self.path).append(7,message())
        self.assert_guards()

    def test_restart(self):
        WidgetStats(self.path).append(1,message())
        again=WidgetStats(self.path)
        self.assertFalse(again.append(1,message()))
        self.assertTrue(again.append(2,message()))
        self.assertEqual(len(self.read()),2)
        self.assertIsNone(again.backup_path)

    def test_concurrent_replay(self):
        stats=WidgetStats(self.path)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:stats.append(1,message()),range(20)))
        self.assertEqual(sum(results),1); self.assertEqual(len(self.read()),1)

    def test_only_widget_messages(self):
        stats=WidgetStats(self.path)
        for text in ['/prices','/start','/widgetx','hello']:
            self.assertFalse(record_widget_request(stats,1,{'update_id':1,'message':message(text=text)}))
        self.assertFalse(record_widget_request(stats,1,{'update_id':1,'widget_query':{'data':'refresh'}}))
        for update,text in [(1,'/widget'),(2,'/widget@SomeBot')]:
            self.assertTrue(record_widget_request(stats,1,{'update_id':update,'message':message(text=text)}))
        self.assertEqual(len(self.read()),2)

    def test_db_failure_propagates(self):
        stats=WidgetStats(self.path)
        self.path.unlink()
        with self.assertRaises(sqlite3.OperationalError):stats.append(1,message())

    def test_migration_preserves_history_and_backup(self):
        old=self.legacy()
        stats=WidgetStats(self.path)
        self.assertEqual(self.read(),[(r[1],r[2],r[3],r[6]) for r in old])
        self.assertEqual(tuple(r[1] for r in self.read('PRAGMA table_info(widget_requests)')),COLUMNS)
        self.assertEqual(stats.backup_path.parent,self.path.parent)
        self.assertEqual(self.read('SELECT * FROM widget_requests ORDER BY update_id',stats.backup_path),old)
        self.assertEqual(self.read('PRAGMA quick_check',stats.backup_path),[('ok',)])
        self.assert_guards()
        self.assertFalse(stats.append(7,message()))
        self.assertTrue(stats.append(9,message()))
        before=list(self.path.parent.glob('*.before-v2-*.sqlite3'))
        self.assertIsNone(WidgetStats(self.path).backup_path)
        self.assertEqual(list(self.path.parent.glob('*.before-v2-*.sqlite3')),before)

    def test_empty_legacy_migration(self):
        self.legacy([])
        self.assertIsNotNone(WidgetStats(self.path).backup_path)
        self.assertEqual(self.read(),[])

    def test_multiple_bots_refused(self):
        self.legacy([(1,7,123,None,123,9,'t','r'),(2,8,456,None,456,10,'t','r')])
        before=self.read()
        with self.assertRaisesRegex(RuntimeError,'multiple bots'):WidgetStats(self.path)
        self.assertEqual(self.read(),before)
        self.assertEqual(len(self.read('PRAGMA table_info(widget_requests)')),8)

    def test_conflicting_ids_refused(self):
        self.legacy([(1,7,123,None,123,9,'t','r'),(2,7,456,None,456,10,'t','r')])
        with self.assertRaises(RuntimeError):WidgetStats(self.path)
        self.assertEqual(len(self.read()),2)

    def test_backup_failure_rolls_back(self):
        self.legacy(); before=self.read()
        with patch.object(WidgetStats,'_backup',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):WidgetStats(self.path)
        self.assertEqual(self.read(),before)
        self.assertEqual(self.read('PRAGMA user_version'),[(0,)])

    def test_ddl_failure_rolls_back_and_backup_survives(self):
        self.legacy(); before=self.read()
        with patch.object(WidgetStats,'_install_guards',side_effect=RuntimeError('injected DDL failure')):
            with self.assertRaises(RuntimeError):WidgetStats(self.path)
        self.assertEqual(self.read(),before)
        self.assertEqual(len(self.read('PRAGMA table_info(widget_requests)')),8)
        self.assertEqual(self.read('PRAGMA user_version'),[(0,)])
        self.assertEqual(len(list(self.path.parent.glob('*.before-v2-*.sqlite3'))),1)
        self.assertIsNotNone(WidgetStats(self.path).backup_path)

    def test_unknown_schema_refused(self):
        with closing(sqlite3.connect(self.path)) as c,c:
            c.execute('CREATE TABLE widget_requests(secret TEXT)')
            c.execute("INSERT INTO widget_requests VALUES ('keep')")
        with self.assertRaises(RuntimeError):WidgetStats(self.path)
        self.assertEqual(self.read('SELECT * FROM widget_requests'),[('keep',)])

    def test_future_version_refused(self):
        WidgetStats(self.path).append(1,message())
        with closing(sqlite3.connect(self.path)) as c,c:c.execute('PRAGMA user_version=99')
        with self.assertRaises(RuntimeError):WidgetStats(self.path)
        self.assertEqual(len(self.read()),1)

    def test_backup_contains_uncheckpointed_wal(self):
        with closing(sqlite3.connect(self.path)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.executescript(OLD_SCHEMA)
            writer.execute('INSERT INTO widget_requests VALUES (1,77,123,NULL,123,9,?,?)',('time','recorded'))
            writer.commit()
            self.assertTrue(Path(str(self.path)+'-wal').exists())
            stats=WidgetStats(self.path)
            self.assertEqual(self.read(),[(77,123,None,'time')])
            self.assertEqual(self.read('SELECT update_id FROM widget_requests',stats.backup_path),[(77,)])


if __name__=='__main__':
    unittest.main()
