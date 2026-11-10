import json
import logging
import os
import time
import zlib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(Exception):
    pass


def api(token, method, **params):
    request = Request(
        f'https://api.telegram.org/bot{token}/{method}',
        data=json.dumps(params).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urlopen(request, timeout=40) as response:
            payload = json.load(response)
    except HTTPError as error:
        try:
            payload = json.loads(error.read())
        except (ValueError, UnicodeError):
            raise TelegramError(f'{method}: HTTP {error.code}') from None
    except (URLError, TimeoutError, OSError):
        # Do not log the request URL: it contains the bot token.
        raise TelegramError(f'{method}: network error') from None
    if not payload.get('ok'):
        description = str(payload.get('description', 'Unknown error')).replace(token, '<redacted>')
        raise TelegramError(f'{method}: {description}')
    return payload['result']


def handle_message(token, message, html, allowed_user):
    chat = message.get('chat', {})
    if chat.get('type') != 'private':
        return
    if allowed_user and message.get('from', {}).get('id') != allowed_user:
        return
    words = message.get('text', '').split()
    command = words[0].split('@')[0] if words else ''
    if command == '/widget':
        try:
            api(token, 'sendWidget', chat_id=chat['id'], html=html,
                caption='HTML-счётчик: нажимай +1. При повторном открытии счётчик начинается с нуля.',
                width=300, height=180)
        except TelegramError as error:
            logging.error('%s', error)
            api(token, 'sendMessage', chat_id=chat['id'],
                text='Не удалось отправить HTML-виджет. Подробности ошибки есть в логах бота.')
    elif command in ('/start', '/help'):
        api(token, 'sendMessage', chat_id=chat['id'],
            text='Отправь /widget, чтобы получить HTML-виджет с кнопкой +1. Работает только в личном чате.')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ.get('BOT_TOKEN', '').strip()
    if not token:
        raise SystemExit('Set BOT_TOKEN before starting the bot.')
    try:
        allowed_user = int(os.environ.get('ALLOWED_USER_ID', '0'))
    except ValueError:
        raise SystemExit('ALLOWED_USER_ID must be an integer.') from None
    html = Path(__file__).with_name('widget.html').read_text(encoding='utf-8')
    raw = html.encode('utf-8')
    if b'\x00' in raw or len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
        raise SystemExit('widget.html exceeds Telegram limits or contains NUL.')
    try:
        me = api(token, 'getMe')
        info = api(token, 'getWebhookInfo')
    except TelegramError as error:
        raise SystemExit(str(error)) from None
    if info.get('url'):
        raise SystemExit('A webhook is configured. Remove it before using long polling; the bot will not remove it automatically.')
    logging.info('Started @%s. Send /widget in a private chat.', me.get('username', 'bot'))
    offset = None
    while True:
        try:
            params = {'timeout': 30, 'allowed_updates': ['message']}
            if offset is not None:
                params['offset'] = offset
            updates = api(token, 'getUpdates', **params)
            for update in updates:
                try:
                    if 'message' in update:
                        handle_message(token, update['message'], html, allowed_user)
                except TelegramError as error:
                    logging.error('%s', error)
                offset = update['update_id'] + 1
        except TelegramError as error:
            logging.error('%s; retrying in 5 seconds', error)
            time.sleep(5)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
