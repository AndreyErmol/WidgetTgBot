import logging
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path('/data/widget_stats.sqlite3')
VERSION = 2
COLUMNS = ('update_id', 'user_id', 'username', 'requested_at')
OLD_COLUMNS = ('bot_id', 'update_id', 'user_id', 'username', 'chat_id',
               'message_id', 'requested_at', 'recorded_at')


class WidgetStats:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        self.backup_path = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            mode = connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if mode.lower() != 'wal':
                raise RuntimeError('SQLite WAL mode unavailable')
            connection.execute('BEGIN IMMEDIATE')
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, VERSION):
                raise RuntimeError('Unsupported statistics schema version')
            info = connection.execute('PRAGMA table_info(widget_requests)').fetchall()
            columns = tuple(row[1] for row in info)
            if not columns and version == 0:
                self._create_table(connection, 'widget_requests')
            elif columns == OLD_COLUMNS and version == 0:
                self._migrate(connection)
            elif columns == COLUMNS:
                if [row[1] for row in info if row[5]] != ['update_id']:
                    raise RuntimeError('Unexpected statistics primary key')
            else:
                raise RuntimeError('Unrecognized statistics schema; refusing to change data')
            self._install_guards(connection)
            connection.execute('PRAGMA user_version=2')
        if self.backup_path is not None:
            logging.info('Statistics migrated to four columns; backup: %s', self.backup_path)

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

    @staticmethod
    def _create_table(connection, name):
        if name not in ('widget_requests', 'widget_requests_new'):
            raise ValueError('Invalid table name')
        connection.execute(f'CREATE TABLE {name} ('
                           'update_id INTEGER PRIMARY KEY, '
                           'user_id INTEGER NOT NULL, username TEXT, '
                           'requested_at TEXT NOT NULL)')

    def _backup(self):
        fd, name = tempfile.mkstemp(prefix=self.path.stem + '.before-v2-',
                                    suffix='.sqlite3', dir=self.path.parent)
        os.close(fd)
        # Another connection reads the committed snapshot while the caller holds
        # BEGIN IMMEDIATE, preventing other writers until migration is committed.
        with closing(self._connect()) as source, closing(sqlite3.connect(name)) as target:
            source.backup(target)
            if target.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise RuntimeError('Statistics backup integrity check failed')
        return Path(name)

    def _migrate(self, connection):
        bots = connection.execute('SELECT DISTINCT bot_id FROM widget_requests LIMIT 2').fetchall()
        if len(bots) > 1:
            raise RuntimeError('Statistics contain multiple bots; automatic migration refused')
        duplicate = connection.execute('SELECT update_id FROM widget_requests '
                                       'GROUP BY update_id HAVING COUNT(*)>1 LIMIT 1').fetchone()
        if duplicate:
            raise RuntimeError('Duplicate update_id; automatic migration refused')
        self.backup_path = self._backup()
        count = connection.execute('SELECT COUNT(*) FROM widget_requests').fetchone()[0]
        self._create_table(connection, 'widget_requests_new')
        fields = ','.join(COLUMNS)
        connection.execute(f'INSERT INTO widget_requests_new ({fields}) '
                           f'SELECT {fields} FROM widget_requests')
        copied = connection.execute('SELECT COUNT(*) FROM widget_requests_new').fetchone()[0]
        different = connection.execute(f'SELECT {fields} FROM widget_requests '
                                       f'EXCEPT SELECT {fields} FROM widget_requests_new LIMIT 1').fetchone()
        if copied != count or different is not None:
            raise RuntimeError('Statistics migration verification failed')
        connection.execute('DROP TABLE widget_requests')
        connection.execute('ALTER TABLE widget_requests_new RENAME TO widget_requests')

    @staticmethod
    def _install_guards(connection):
        connection.execute('CREATE INDEX IF NOT EXISTS widget_requests_user_time '
                           'ON widget_requests(user_id, requested_at)')
        for operation in ('update', 'delete'):
            name = 'widget_requests_no_' + operation
            connection.execute(f'DROP TRIGGER IF EXISTS {name}')
            connection.execute(f'CREATE TRIGGER {name} BEFORE {operation.upper()} '
                               "ON widget_requests BEGIN SELECT RAISE(ABORT, 'widget_requests is append only'); END")
        connection.execute('DROP TRIGGER IF EXISTS widget_requests_no_replace')
        connection.execute("""CREATE TRIGGER widget_requests_no_replace
            BEFORE INSERT ON widget_requests
            WHEN NEW.update_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM widget_requests WHERE update_id=NEW.update_id)
            BEGIN
                SELECT RAISE(ABORT, 'widget_requests cannot replace an existing event');
            END""")

    def append(self, update_id, message):
        user = message['from']
        requested = datetime.fromtimestamp(message['date'], timezone.utc).isoformat(timespec='seconds')
        with closing(self._connect()) as connection, connection:
            connection.execute('BEGIN IMMEDIATE')
            if connection.execute('SELECT 1 FROM widget_requests WHERE update_id=?', (update_id,)).fetchone():
                return False
            connection.execute('INSERT INTO widget_requests '
                               '(update_id,user_id,username,requested_at) VALUES (?,?,?,?)',
                               (update_id, user['id'], user.get('username'), requested))
        return True


def record_widget_request(stats, _bot_id, update):
    # Preserve the polling integration signature; this database is for one bot.
    message = update.get('message', {})
    words = message.get('text', '').split()
    command = words[0].split('@')[0] if words else ''
    if command != '/widget' or not message.get('from'):
        return False
    return stats.append(update['update_id'], message)
