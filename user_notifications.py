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
GRACE = 900
CHECK_SECONDS = 15
TIME_RE = re.compile(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]')


def unreachable(error):
    code = getattr(error, 'code', 0)
    return code == 403 or (code == 400 and 'chat not found' in str(error).lower())


class PriceNotifications:
    def __init__(self, app, path, allowed=0):
        self.app, self.path, self.allowed = app, Path(path), allowed
        self.jobs, self.stop = queue.Queue(32), threading.Event()
        with self.db() as c, c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('CREATE TABLE IF NOT EXISTS price_notifications ('
                      'user_id INTEGER PRIMARY KEY, '
                      'minute INTEGER NOT NULL CHECK(minute BETWEEN 0 AND 1439), '
                      'slot INTEGER NOT NULL, next_run INTEGER NOT NULL, '
                      'last_day TEXT, failures INTEGER NOT NULL DEFAULT 0)')
        self.thread = threading.Thread(target=self.loop, name='price-notifications', daemon=True)

    def db(self):
        return closing(sqlite3.connect(self.path, timeout=5))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=12)

    def permitted(self, uid):
        return type(uid) is int and uid > 0 and (not self.allowed or uid == self.allowed)

    @staticmethod
    def next_slot(minute, now, skip_today=False):
        local = now.astimezone(MSK)
        target = local.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
        if skip_today or target <= local:
            target += timedelta(days=1)
        return int(target.timestamp())

    def state(self, uid, now=None):
        with self.db() as c:
            row = c.execute('SELECT minute,slot FROM price_notifications WHERE user_id=?', (uid,)).fetchone()
        if not row:
            return {'on': False, 'h': 9, 'm': 0}
        return {'on': True, 'h': row[0] // 60, 'm': row[0] % 60,
                'next': datetime.fromtimestamp(row[1], MSK).strftime('%d.%m.%Y %H:%M')}

    def state_json(self, uid, now=None):
        return json.dumps(self.state(uid, now), ensure_ascii=False, separators=(',', ':'))

    def set_time(self, uid, text, now=None):
        if not self.permitted(uid) or not isinstance(text, str) or not TIME_RE.fullmatch(text):
            return False
        now = now or datetime.now(MSK)
        minute = int(text[:2]) * 60 + int(text[3:])
        today = now.astimezone(MSK).date().isoformat()
        with self.db() as c, c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT last_day FROM price_notifications WHERE user_id=?', (uid,)).fetchone()
            slot = self.next_slot(minute, now, bool(old and old[0] == today))
            c.execute('INSERT INTO price_notifications(user_id,minute,slot,next_run) VALUES (?,?,?,?) '
                      'ON CONFLICT(user_id) DO UPDATE SET minute=excluded.minute,slot=excluded.slot,'
                      'next_run=excluded.slot,failures=0', (uid, minute, slot, slot))
        logging.info('Price notification set user=%s time=%s MSK', uid, text)
        return True

    def disable(self, uid, reason='user'):
        with self.db() as c, c:
            removed = c.execute('DELETE FROM price_notifications WHERE user_id=?', (uid,)).rowcount
        if removed:
            logging.info('Price notification removed user=%s reason=%s', uid, reason)
        return bool(removed)

    def handle_member(self, event):
        chat = event.get('chat', {})
        status = event.get('new_chat_member', {}).get('status')
        uid = chat.get('id')
        if chat.get('type') == 'private' and type(uid) is int and status in ('kicked', 'left'):
            self.disable(uid, 'bot blocked')
            return True
        return False

    def handle_query(self, query, now=None):
        text = query.get('data', '')
        if not isinstance(text, str) or not text.startswith('notify:'):
            return False
        params = {'widget_query_id': query['id']}
        uid = query.get('from', {}).get('id')
        chat = query.get('chat', {})
        try:
            if chat.get('type') == 'private' and chat.get('id') == uid and self.permitted(uid):
                if text == 'notify:get':
                    done = True
                elif text == 'notify:off':
                    self.disable(uid)
                    done = True
                elif text.startswith('notify:set:'):
                    done = self.set_time(uid, text[11:], now)
                else:
                    done = False
                if done:
                    params['data'] = self.state_json(uid, now)
        except Exception as error:
            logging.warning('Notification query failed user=%s: %s', uid, error)
        self.app.api('answerWidgetQuery', **params)
        return True

    def enqueue_settings(self, uid):
        if not self.permitted(uid):
            return False
        try:
            self.jobs.put_nowait(uid)
            return True
        except queue.Full:
            logging.warning('Notification settings queue full user=%s', uid)
            return False

    def send_settings(self, uid, now=None):
        html = (Path(__file__).parent / 'notification_settings.html').read_text(encoding='utf-8')
        raw = html.encode()
        if len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096 or b'\x00' in raw:
            raise ValueError('Notification HTML exceeds Telegram limits')
        return self.app.api('sendWidget', chat_id=uid, html=html, width=360, height=0,
                            is_shared=False, allow_queries=True, protect_content=True,
                            chat_data=self.state_json(uid, now),
                            caption='Настройка ежедневных уведомлений · время МСК')

    def deliver_settings(self, uid):
        try:
            self.send_settings(uid)
        except Exception as error:
            if unreachable(error):
                self.disable(uid, 'bot blocked')
            logging.warning('Notification settings not sent user=%s: %s', uid, error)

    def still(self, uid, slot):
        with self.db() as c:
            return c.execute('SELECT 1 FROM price_notifications WHERE user_id=? AND slot=?',
                             (uid, slot)).fetchone() is not None

    def advance(self, uid, slot, minute, now, sent):
        new = self.next_slot(minute, now)
        day = datetime.fromtimestamp(slot, MSK).date().isoformat()
        with self.db() as c, c:
            if sent:
                c.execute('UPDATE price_notifications SET slot=?,next_run=?,last_day=?,failures=0 '
                          'WHERE user_id=? AND slot=?', (new, new, day, uid, slot))
            else:
                c.execute('UPDATE price_notifications SET slot=?,next_run=?,failures=0 '
                          'WHERE user_id=? AND slot=?', (new, new, uid, slot))

    def tick(self, now=None):
        now = now or datetime.now(MSK)
        stamp = int(now.timestamp())
        with self.db() as c:
            due = c.execute('SELECT user_id,minute,slot,last_day,failures FROM price_notifications '
                            'WHERE next_run<=? ORDER BY next_run LIMIT 100', (stamp,)).fetchall()
        for uid, minute, slot, last_day, failures in due:
            if self.stop.is_set():
                return
            if not self.permitted(uid):
                self.disable(uid, 'not allowed')
                continue
            slot_day = datetime.fromtimestamp(slot, MSK).date().isoformat()
            if stamp - slot > GRACE or last_day == slot_day:
                logging.info('Price notification skipped user=%s slot=%s', uid, slot_day)
                self.advance(uid, slot, minute, now, False)
                continue
            if not self.still(uid, slot):
                continue
            try:
                self.app.send_widget(uid)
            except Exception as error:
                if unreachable(error):
                    self.disable(uid, 'bot blocked')
                    continue
                delay = min(300, 30 * 2 ** min(failures, 4))
                with self.db() as c, c:
                    c.execute('UPDATE price_notifications SET next_run=?,failures=failures+1 '
                              'WHERE user_id=? AND slot=?', (stamp + delay, uid, slot))
                logging.warning('Price notification failed user=%s retry=%ss: %s', uid, delay, error)
                continue
            self.advance(uid, slot, minute, now, True)

    def loop(self):
        check = 0
        while not self.stop.is_set():
            try:
                try:
                    uid = self.jobs.get(timeout=1)
                except queue.Empty:
                    uid = None
                if uid is not None:
                    self.deliver_settings(uid)
                if time.monotonic() >= check:
                    check = time.monotonic() + CHECK_SECONDS
                    self.tick()
            except Exception as error:
                logging.warning('Notification worker: %s', error)
