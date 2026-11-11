import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from admin_panel import AdminPanel, MSK

ADMIN=804664389
NOW=datetime(2026,11,11,14,0,tzinfo=MSK)


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'stats.sqlite3'
        self.sql('CREATE TABLE widget_requests(update_id INTEGER PRIMARY KEY,user_id INTEGER,username TEXT,requested_at TEXT)')
        self.app=SimpleNamespace(api=Mock(return_value={'message_id':42}))
        self.panel=AdminPanel(self.app,self.path)

    def sql(self, sql, args=()):
        with closing(sqlite3.connect(self.path)) as c,c:
            return c.execute(sql,args).fetchall()

    def admins(self, ids):
        self.sql("UPDATE system_parameters SET value=? WHERE key='admin_ids'",(json.dumps(ids),))

    def add(self, i, user, stamp, name='alice'):
        self.sql('INSERT INTO widget_requests VALUES (?,?,?,?)',(i,user,name,stamp))

    def message(self,uid=ADMIN,text='/statistic',kind='private'):
        return {'from':{'id':uid},'chat':{'id':uid,'type':kind},'text':text}

    def query(self,uid=ADMIN,mid=42,data='stats:1:0'):
        return {'id':'q','from':{'id':uid},'chat':{'id':uid,'type':'private'},'message_id':mid,'data':data}

    def register(self):
        self.sql('INSERT INTO statistic_widgets VALUES (?,?,?,?)',(ADMIN,42,ADMIN,'2026-11-11'))

    def test_seed_and_no_resurrection(self):
        self.assertTrue(self.panel.is_admin(ADMIN))
        self.admins([]); again=AdminPanel(self.app,self.path)
        self.assertFalse(again.is_admin(ADMIN))
        self.sql("DELETE FROM system_parameters WHERE key='admin_ids'")
        self.assertFalse(AdminPanel(self.app,self.path).is_admin(ADMIN))

    def test_unauthorized_commands_silent(self):
        for text in ['/statistic','/notification','/notification 09:00']:
            self.assertTrue(self.panel.handle_message(self.message(123,text)))
        self.assertTrue(self.panel.jobs.empty()); self.app.api.assert_not_called()

    def test_groups_silent(self):
        self.assertTrue(self.panel.handle_message(self.message(kind='group')))
        self.assertTrue(self.panel.jobs.empty()); self.app.api.assert_not_called()

    def test_authorized_command_queued(self):
        self.assertTrue(self.panel.handle_message(self.message()))
        self.assertEqual(self.panel.jobs.get_nowait(),(ADMIN,'/statistic',[]))
        self.assertFalse(self.panel.handle_message(self.message(text='/widget')))

    def test_msk_boundaries_admin_exclusion_and_counts(self):
        self.add(1,123,'2026-11-10T20:59:59+00:00')
        self.add(2,123,'2026-11-10T21:00:00+00:00')
        self.add(3,123,'2026-11-11T10:00:00+00:00')
        self.add(4,456,'2026-11-11T20:59:59+00:00',None)
        self.add(5,456,'2026-11-11T21:00:00+00:00')
        self.add(6,ADMIN,'2026-11-11T10:00:00+00:00','admin')
        data=self.panel.snapshot(ADMIN,'2026-11-11')
        self.assertEqual((data['calls'],data['users']),(3,2))
        self.assertEqual(data['rows'][-1][2],'00:00:00')
        self.assertEqual(self.panel.snapshot(ADMIN,'2026-11-11',1)['calls'],1)
        self.admins([ADMIN,456])
        self.assertEqual(self.panel.snapshot(ADMIN,'2026-11-11')['calls'],2)

    def test_payload_pagination_and_size(self):
        for i in range(17):self.add(i,123,'2026-11-11T10:00:00+00:00','я'*100)
        offset=0; seen=0
        while offset<17:
            data=self.panel.snapshot(ADMIN,'2026-11-11',0,offset)
            self.assertLessEqual(len(json.dumps(data,ensure_ascii=False).encode()),1024)
            self.assertTrue(data['rows']); seen+=len(data['rows']); offset+=len(data['rows'])
        self.assertEqual(seen,17)

    def test_widget_is_private_protected_and_bound(self):
        with patch('admin_panel.Path.read_text',return_value='<b>stats</b>'):
            self.assertTrue(self.panel.send_statistic(ADMIN,NOW))
        call=self.app.api.call_args
        self.assertEqual(call.args[0],'sendWidget')
        self.assertFalse(call.kwargs['is_shared']); self.assertTrue(call.kwargs['protect_content'])
        self.assertNotIn('public_data',call.kwargs)
        self.assertEqual(self.sql('SELECT owner_id,anchor FROM statistic_widgets'),[(ADMIN,'2026-11-11')])

    def test_revoked_before_send(self):
        with patch('admin_panel.Path.read_text',return_value='stats'), patch.object(self.panel,'is_admin',return_value=False):
            self.assertFalse(self.panel.send_statistic(ADMIN,NOW))
        self.app.api.assert_not_called()

    def test_queries_recheck_owner_role_message(self):
        self.register()
        self.panel.handle_query(self.query())
        self.assertIn('data',self.app.api.call_args.kwargs)
        for q in [self.query(123),self.query(mid=43),self.query(data='stats:9:0'),self.query(data='stats:0:-1')]:
            self.panel.handle_query(q)
            self.assertNotIn('data',self.app.api.call_args.kwargs)
        self.panel.handle_query(self.query(),shared=True)
        self.assertNotIn('data',self.app.api.call_args.kwargs)
        self.admins([]); self.panel.handle_query(self.query())
        self.assertNotIn('data',self.app.api.call_args.kwargs)
        self.assertFalse(self.panel.handle_query(self.query(data='refresh')))

    def test_notification_validation_and_individual_times(self):
        self.admins([ADMIN,123])
        self.panel.configure(ADMIN,['09:30'],NOW)
        self.panel.configure(123,['21:15'],NOW)
        rows=self.sql('SELECT user_id,minute,next_run FROM notification_settings ORDER BY user_id')
        self.assertEqual([r[1] for r in rows],[1275,570])
        self.assertEqual(datetime.fromtimestamp(rows[0][2],MSK).day,11)
        self.assertEqual(datetime.fromtimestamp(rows[1][2],MSK).day,12)
        for bad in ['24:00','09:60','9:30','abc']:
            self.panel.configure(ADMIN,[bad],NOW)
        self.assertEqual(self.sql('SELECT minute FROM notification_settings WHERE user_id=?',(ADMIN,)),[(570,)])
        self.panel.configure(ADMIN,['off'],NOW)
        self.assertFalse(self.sql('SELECT * FROM notification_settings WHERE user_id=?',(ADMIN,)))

    def test_scheduled_delivery_once_and_restart(self):
        self.panel.configure(ADMIN,['13:59'],NOW)
        self.sql('UPDATE notification_settings SET next_run=?',(int(NOW.timestamp())-60,))
        with patch('admin_panel.Path.read_text',return_value='stats'):
            self.panel.tick(NOW)
            AdminPanel(self.app,self.path).tick(NOW)
        sends=[c for c in self.app.api.call_args_list if c.args[0]=='sendWidget']
        self.assertEqual(len(sends),1)
        self.assertEqual(self.sql('SELECT last_day FROM notification_settings'),[('2026-11-11',)])

    def test_revoked_schedule_never_sends(self):
        self.panel.configure(ADMIN,['13:59'],NOW)
        self.sql('UPDATE notification_settings SET next_run=0'); self.admins([])
        self.app.api.reset_mock(); self.panel.tick(NOW)
        self.app.api.assert_not_called()
        self.assertFalse(self.sql('SELECT * FROM notification_settings'))

    def test_schedule_retries_with_backoff(self):
        self.panel.configure(ADMIN,['13:59'],NOW)
        self.sql('UPDATE notification_settings SET next_run=0')
        with patch.object(self.panel,'send_statistic',side_effect=RuntimeError('network')):
            self.panel.tick(NOW)
        due,failures=self.sql('SELECT next_run,failures FROM notification_settings')[0]
        self.assertEqual((due,failures),(int(NOW.timestamp())+60,1))

    def test_invalid_admin_config_fails_closed(self):
        self.sql("UPDATE system_parameters SET value='[true]' WHERE key='admin_ids'")
        with self.assertRaises(ValueError):self.panel.is_admin(ADMIN)


if __name__=='__main__':unittest.main()
