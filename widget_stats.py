import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path('/data/widget_stats.sqlite3')

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS widget_requests (
    bot_id INTEGER NOT NULL,
    update_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    username TEXT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (bot_id, update_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS widget_requests_user_time
ON widget_requests(user_id, requested_at);
CREATE TRIGGER IF NOT EXISTS widget_requests_no_update
BEFORE UPDATE ON widget_requests BEGIN
    SELECT RAISE(ABORT, 'widget_requests is append only');
END;
CREATE TRIGGER IF NOT EXISTS widget_requests_no_delete
BEFORE DELETE ON widget_requests BEGIN
    SELECT RAISE(ABORT, 'widget_requests is append only');
END;
CREATE TRIGGER IF NOT EXISTS widget_requests_no_replace
BEFORE INSERT ON widget_requests
WHEN EXISTS (SELECT 1 FROM widget_requests
             WHERE bot_id=NEW.bot_id AND update_id=NEW.update_id)
BEGIN
    SELECT RAISE(ABORT, 'widget_requests cannot replace an existing event');
END;
COMMIT;
"""


class WidgetStats:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            mode = connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if mode.lower() != 'wal':
                raise RuntimeError('SQLite WAL mode unavailable')
            connection.executescript(SCHEMA)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA temp_store=MEMORY')
            connection.execute('PRAGMA recursive_triggers=ON')
            return connection
        except Exception:
            connection.close()
            raise

    def append(self, bot_id, update_id, message):
        user = message['from']
        requested = datetime.fromtimestamp(message['date'], timezone.utc).isoformat(timespec='seconds')
        recorded = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        key = (bot_id, update_id)
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM widget_requests WHERE bot_id=? AND update_id=?', key).fetchone():
                return False
            connection.execute(
                'INSERT INTO widget_requests '
                '(bot_id,update_id,user_id,username,chat_id,message_id,requested_at,recorded_at) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (*key, user['id'], user.get('username'), message['chat']['id'],
                 message['message_id'], requested, recorded))
        return True


def record_widget_request(stats, bot_id, update):
    message = update.get('message', {})
    words = message.get('text', '').split()
    command = words[0].split('@')[0] if words else ''
    if command != '/widget' or not message.get('from'):
        return False
    return stats.append(bot_id, update['update_id'], message)
