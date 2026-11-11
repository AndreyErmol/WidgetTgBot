import json
import logging
import queue
import re
import sqlite3
import threading
import time
import zlib
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

MSK = timezone(timedelta(hours=3), 'MSK')


class AdminPanel:
    def __init__(self, app, path):
        self.app, self.path = app, Path(path)
        self.jobs, self.stop = queue.Queue(32), threading.Event()
        with self.db() as c, c:
            c.execute('BEGIN IMMEDIATE')
            fresh = not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_parameters'").fetchone()
            c.execute('CREATE TABLE IF NOT EXISTS system_parameters (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            if fresh:
                c.execute('INSERT INTO system_parameters VALUES (?,?)', ('admin_ids', '[804664389]'))
            c.execute('CREATE TABLE IF NOT EXISTS notification_settings ('
                      'user_id INTEGER PRIMARY KEY, minute INTEGER NOT NULL CHECK(minute BETWEEN 0 AND 1439), '
                      'next_run INTEGER NOT NULL, last_day TEXT, failures INTEGER NOT NULL DEFAULT 0)')
            c.execute('CREATE TABLE IF NOT EXISTS statistic_widgets ('
                      'chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, '
                      'anchor TEXT NOT NULL, PRIMARY KEY(chat_id,message_id))')
            c.execute('CREATE INDEX IF NOT EXISTS widget_requests_time ON widget_requests(requested_at)')
        self.thread = threading.Thread(target=self.loop, name='admin-panel', daemon=True)

    def db(self):
        return closing(sqlite3.connect(self.path, timeout=5))

    @staticmethod
    def admins(c):
        row = c.execute("SELECT value FROM system_parameters WHERE key='admin_ids'").fetchone()
        if not row:
            return set()
        ids = json.loads(row[0])
        if not isinstance(ids, list) or len(ids) > 100 or any(type(i) is not int or i <= 0 for i in ids):
            raise ValueError('Invalid admin_ids configuration')
        return set(ids)

    def is_admin(self, uid):
        with self.db() as c:
            return uid in self.admins(c)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=12)

    def handle_message(self, message):
        words = message.get('text', '').split()
        command = words[0].split('@')[0] if words else ''
        if command not in ('/statistic', '/notification'):
            return False
        uid = message.get('from', {}).get('id')
        chat = message.get('chat', {})
        if chat.get('type') == 'private' and chat.get('id') == uid and self.is_admin(uid):
            try:
                self.jobs.put_nowait((uid, command, words[1:]))
            except queue.Full:
                logging.warning('Admin queue full user=%s', uid)
        return True

    @staticmethod
    def next_run(minute, now, tomorrow=False):
        local = now.astimezone(MSK)
        target = local.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
        if tomorrow or target <= local:
            target += timedelta(days=1)
        return int(target.timestamp())

    def reply(self, uid, text):
        if self.is_admin(uid):
            self.app.api('sendMessage', chat_id=uid, text=text, protect_content=True)

    def configure(self, uid, args, now=None):
        now = now or datetime.now(MSK)
        if not self.is_admin(uid):
            return
        if not args:
            with self.db() as c:
                row = c.execute('SELECT minute FROM notification_settings WHERE user_id=?', (uid,)).fetchone()
            status = f'{row[0]//60:02d}:{row[0]%60:02d} МСК' if row else 'выключены'
            self.reply(uid, 'Уведомления: '+status+'\n/notification HH:MM — ежедневно\n/notification off — выключить')
            return
        if args == ['off']:
            with self.db() as c, c:
                if uid not in self.admins(c):
                    return
                c.execute('DELETE FROM notification_settings WHERE user_id=?', (uid,))
            self.reply(uid, 'Уведомления выключены.')
            logging.info('Notification disabled user=%s', uid)
            return
        if len(args) != 1 or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', args[0]):
            self.reply(uid, 'Формат: /notification HH:MM по МСК или /notification off')
            return
        h, m = map(int, args[0].split(':'))
        minute = h * 60 + m
        with self.db() as c, c:
            c.execute('BEGIN IMMEDIATE')
            if uid not in self.admins(c):
                return
            old = c.execute('SELECT last_day FROM notification_settings WHERE user_id=?', (uid,)).fetchone()
            due = self.next_run(minute, now, bool(old and old[0] == now.astimezone(MSK).date().isoformat()))
            c.execute('INSERT INTO notification_settings(user_id,minute,next_run) VALUES (?,?,?) '
                      'ON CONFLICT(user_id) DO UPDATE SET minute=excluded.minute,next_run=excluded.next_run,failures=0',
                      (uid, minute, due))
        self.reply(uid, 'Статистика будет приходить ежедневно в '+args[0]+' МСК.')
        logging.info('Notification configured user=%s time=%s MSK', uid, args[0])

    @staticmethod
    def notification_count(c):
        try:
            return c.execute('SELECT COUNT(*) FROM price_notifications').fetchone()[0]
        except sqlite3.OperationalError as error:
            if 'no such table' not in str(error):
                raise
            return 0

    def notification_snapshot(self, uid, offset=0):
        with self.db() as c:
            c.execute('BEGIN')
            if uid not in self.admins(c):
                return None
            total = self.notification_count(c)
            offset = min(offset, max(0, total-1))
            records = []
            if total:
                records = c.execute(
                    'SELECT n.user_id,'
                    '(SELECT w.username FROM widget_requests w WHERE w.user_id=n.user_id '
                    'ORDER BY w.update_id DESC LIMIT 1),n.minute '
                    'FROM price_notifications n ORDER BY n.minute,n.user_id LIMIT 5 OFFSET ?', (offset,)).fetchall()
        rows = [[i, n[:32] if n else None, f'{m//60:02d}:{m%60:02d}'] for i, n, m in records]
        data = {'day':2, 'date':'Сортировка по времени отправки', 'users':total, 'calls':total,
                'nt':total, 'offset':offset, 'rows':rows}
        while len(json.dumps(data, ensure_ascii=False).encode()) > 1024 and rows:
            rows.pop()
        if total and not rows:
            raise ValueError('Notification state exceeds Telegram limit')
        return data

    def snapshot(self, uid, anchor, day=0, offset=0):
        if day == 2:
            return self.notification_snapshot(uid, offset)
        start = datetime.strptime(anchor, '%Y-%m-%d').replace(tzinfo=MSK) - timedelta(days=day)
        bounds = [d.astimezone(timezone.utc).isoformat(timespec='seconds') for d in (start, start+timedelta(days=1))]
        with self.db() as c:
            c.execute('BEGIN')
            admins = self.admins(c)
            if uid not in admins:
                return None
            marks = ','.join('?' for _ in admins)
            where = f'requested_at>=? AND requested_at<? AND user_id NOT IN ({marks})'
            params = (*bounds, *sorted(admins))
            calls, users = c.execute('SELECT COUNT(*),COUNT(DISTINCT user_id) FROM widget_requests WHERE '+where, params).fetchone()
            enabled = self.notification_count(c)
            offset = min(offset, max(0, calls-1))
            records = c.execute('SELECT user_id,username,requested_at FROM widget_requests WHERE '+where+
                                ' ORDER BY requested_at DESC,update_id DESC LIMIT 5 OFFSET ?', (*params, offset)).fetchall()
        rows = [[i, n[:32] if n else None, datetime.fromisoformat(t).astimezone(MSK).strftime('%H:%M:%S')] for i,n,t in records]
        data = {'day':day, 'date':start.strftime('%d.%m.%Y'), 'users':users, 'calls':calls, 'nt':enabled, 'offset':offset, 'rows':rows}
        while len(json.dumps(data, ensure_ascii=False).encode()) > 1024 and rows:
            rows.pop()
        if calls and not rows:
            raise ValueError('Statistics state exceeds Telegram limit')
        return data

    def send_statistic(self, uid, now=None):
        now = now or datetime.now(MSK)
        anchor = now.astimezone(MSK).date().isoformat()
        data = self.snapshot(uid, anchor)
        if data is None:
            return False
        html = (Path(__file__).parent/'statistic.html').read_text(encoding='utf-8')
        raw = html.encode()
        if len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
            raise ValueError('Statistics HTML exceeds Telegram limit')
        if not self.is_admin(uid):
            return False
        result = self.app.api('sendWidget', chat_id=uid, html=html, width=360, height=0,
                              is_shared=False, allow_queries=True, protect_content=True,
                              chat_data=json.dumps(data, ensure_ascii=False),
                              caption='Статистика /widget · снимок '+now.astimezone(MSK).strftime('%d.%m.%Y %H:%M')+' МСК')
        with self.db() as c, c:
            c.execute('INSERT INTO statistic_widgets VALUES (?,?,?,?)', (uid, result['message_id'], uid, anchor))
        logging.info('Statistic sent user=%s message=%s', uid, result['message_id'])
        return True

    def handle_query(self, query, shared=False):
        text = query.get('data', '')
        if not text.startswith('stats:'):
            return False
        params = {'widget_query_id':query['id']}
        uid = query.get('from', {}).get('id')
        chat = query.get('chat', {})
        match = re.fullmatch(r'stats:([012]):([0-9]{1,10})', text)
        try:
            if not shared and match and chat.get('type') == 'private' and chat.get('id') == uid:
                with self.db() as c:
                    session = c.execute('SELECT owner_id,anchor FROM statistic_widgets WHERE chat_id=? AND message_id=?',
                                        (uid, query.get('message_id'))).fetchone()
                if session and session[0] == uid:
                    data = self.snapshot(uid, session[1], int(match[1]), int(match[2]))
                    if data is not None and self.is_admin(uid):
                        params['data'] = json.dumps(data, ensure_ascii=False)
        except Exception as error:
            logging.warning('Statistic query failed user=%s: %s', uid, error)
        self.app.api('answerWidgetQuery', **params)
        return True

    def tick(self, now=None):
        now = now or datetime.now(MSK)
        stamp, today = int(now.timestamp()), now.astimezone(MSK).date().isoformat()
        with self.db() as c:
            due = c.execute('SELECT user_id,minute,last_day,failures FROM notification_settings WHERE next_run<=?', (stamp,)).fetchall()
        for uid, minute, last_day, failures in due:
            if self.stop.is_set():
                return
            if not self.is_admin(uid):
                with self.db() as c, c:
                    c.execute('DELETE FROM notification_settings WHERE user_id=?', (uid,))
                continue
            try:
                if last_day != today and not self.send_statistic(uid, now):
                    continue
                with self.db() as c, c:
                    c.execute('UPDATE notification_settings SET last_day=?,next_run=?,failures=0 WHERE user_id=?',
                              (today, self.next_run(minute, now, True), uid))
            except Exception as error:
                logging.warning('Scheduled statistic failed user=%s: %s', uid, error)
                with self.db() as c, c:
                    c.execute('UPDATE notification_settings SET next_run=?,failures=failures+1 WHERE user_id=?',
                              (stamp+min(3600,60*2**min(failures,6)),uid))

    def loop(self):
        check = 0
        while not self.stop.is_set():
            try:
                try:
                    uid, command, args = self.jobs.get(timeout=1)
                except queue.Empty:
                    command = None
                if command and self.is_admin(uid):
                    if command == '/statistic':
                        self.send_statistic(uid)
                    else:
                        self.configure(uid, args)
                if time.monotonic() >= check:
                    check = time.monotonic()+30
                    self.tick()
            except Exception as error:
                logging.warning('Admin worker: %s', error)
