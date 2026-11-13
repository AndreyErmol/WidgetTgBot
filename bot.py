import json
import logging
import math
import os
import sys
import threading
import time
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from market_data import ASSETS, SOURCE, MarketData

BASE = Path(__file__).resolve().parent
DATA_DIR = Path('/data')
TOKEN = ''
LOCK = threading.RLock()
SEND_LOCK = threading.Lock()
STATE = {'coins': [], 'ct': 0, 'fx': None, 'ft': 0}
SAVED = {}


class APIError(Exception):
    def __init__(self, message, limited=False, retry=0, code=0):
        super().__init__(message)
        self.limited, self.retry, self.code = limited, retry, code


def retry_seconds(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def json_request(url, params=None, timeout=None):
    body = None if params is None else json.dumps(params).encode()
    request = Request(url, data=body, headers={'User-Agent': 'CryptunaPriceWidget/8',
                                              'Content-Type': 'application/json'})
    try:
        seconds = timeout if timeout is not None else (40 if params is not None else 8)
        with urlopen(request, timeout=seconds) as response:
            return json.load(response)
    except HTTPError as error:
        retry, detail = retry_seconds(error.headers.get('Retry-After')), ''
        try:
            payload = json.loads(error.read(8192))
            retry = max(retry, retry_seconds(payload.get('parameters', {}).get('retry_after')))
            detail = str(payload.get('description') or payload.get('msg') or '')[:200]
        except (ValueError, AttributeError):
            pass
        message = f'HTTP {error.code}' + (f': {detail}' if detail else '')
        if TOKEN:
            message = message.replace(TOKEN, '<redacted>')
        raise APIError(message, error.code in (418, 429), retry, error.code) from None
    except (URLError, OSError, ValueError) as error:
        detail = str(getattr(error, 'reason', error))
        if TOKEN:
            detail = detail.replace(TOKEN, '<redacted>')
        raise APIError(detail) from None


MARKET = MarketData(lambda url: json_request(url))
TG_UNTIL = 0
TG_LOCK = threading.Lock()


def api(method, **params):
    global TG_UNTIL
    with TG_LOCK:
        if time.monotonic() < TG_UNTIL:
            raise APIError('Telegram cooldown')
    try:
        url = f'https://api.telegram.org/bot{TOKEN}/{method}'
        payload = json_request(url, params, timeout=5 if method == 'answerWidgetQuery' else None)
        if not payload.get('ok'):
            raise APIError(str(payload.get('description', 'Telegram error')).replace(TOKEN, '<redacted>'),
                           payload.get('error_code') == 429,
                           retry_seconds(payload.get('parameters', {}).get('retry_after')),
                           payload.get('error_code') or 0)
        return payload['result']
    except APIError as error:
        if error.limited:
            with TG_LOCK:
                TG_UNTIL = max(TG_UNTIL, time.monotonic() + max(1, error.retry or 60))
        raise APIError(f'Telegram/{method}: {error}', error.limited, error.retry, error.code) from None


def finite(value, positive=False):
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError('Invalid numeric quote')
    return number


def fetch_coins():
    return MARKET.coins()


def format_price(value):
    number = finite(value, True)
    places = 6 if number < .01 else 2
    whole, fraction = f'{number:.{places}f}'.split('.')
    groups = [whole[max(0, i-3):i] for i in range(len(whole), 0, -3)]
    return ' '.join(reversed(groups)) + ',' + fraction


def format_percent(value):
    number = round(finite(value), 2)
    return f'{number if number else 0.0:+.2f}'.replace('.', ',') + '%'


def pack():
    with LOCK:
        snapshot, coins = dict(STATE), [dict(c) for c in STATE['coins']]
    rows = [[c['id'], c['symbol'],
             format_price(c['p']) if c['p'] is not None else None,
             format_percent(c['c']) if c['c'] is not None else None,
             int(c['c'] >= 0) if c['c'] is not None else None, c.get('t', 0)] for c in coins]
    state = {'r': time.time_ns() // 1000, 't': snapshot['ct'], 'q': rows,
             'f': format_price(snapshot['fx']) if snapshot['fx'] else None}
    text = json.dumps(state, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if len(text.encode()) > 1024:
        raise ValueError('State exceeds 1024 bytes')
    return text


def persist():
    with LOCK:
        data = dict(SAVED, state=STATE, bot=TOKEN.split(':')[0], version=8, quote_source=SOURCE)
        tmp = DATA_DIR / 'runtime.json.tmp'
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(DATA_DIR / 'runtime.json')


def refresh_on_demand():
    if not STATE['coins'] or time.time() - STATE['ct'] >= 10:
        try:
            coins, stamp = fetch_coins()
            with LOCK:
                STATE.update(coins=coins, ct=stamp)
        except Exception as error:
            logging.warning('Market snapshot: %s; keeping previous prices', error)
    if STATE['fx'] is None or time.time() - STATE['ft'] >= 60:
        rate, stamp = MARKET.rate()
        with LOCK:
            STATE.update(fx=rate, ft=stamp)


def send_widget(chat_id):
    with SEND_LOCK:
        refresh_on_demand()
        persist()
        markup = (BASE / 'widget.html').read_text(encoding='utf-8')
        raw = markup.encode()
        if len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096 or b'\x00' in raw:
            raise ValueError('HTML exceeds Telegram limits')
        api('sendWidget', chat_id=chat_id, html=markup, width=360, height=0,
            allow_queries=True, public_data=pack())


def restore(saved):
    if saved.get('bot') != TOKEN.split(':')[0]:
        return
    old = saved.get('state', {}) if saved.get('version') in (4, 5, 6, 7, 8) else saved
    compatible = saved.get('version') == 8 and saved.get('quote_source') == SOURCE
    MARKET.seed(old.get('coins', []) if compatible else [], old.get('fx'), old.get('ft', 0))
    coins, stamp = MARKET.cached()
    STATE.update(coins=coins, ct=stamp, fx=MARKET.fx.data, ft=MARKET.fx.stamp)
    if compatible:
        SAVED.update(saved)


def main():
    from admin_panel import AdminPanel
    from live_updates import LiveController
    from user_notifications import PriceNotifications
    from widget_stats import WidgetStats, record_widget_request
    global TOKEN
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    TOKEN = os.environ.get('BOT_TOKEN', '').strip()
    if not TOKEN:
        raise SystemExit('Set BOT_TOKEN.')
    allowed = int(os.environ.get('ALLOWED_USER_ID', '0'))
    stats = WidgetStats()
    path = DATA_DIR / 'runtime.json'
    if not path.exists():
        path = BASE / 'runtime.json'
    if path.exists():
        restore(json.loads(path.read_text(encoding='utf-8')))
    persist()
    me = api('getMe')
    if api('getWebhookInfo').get('url'):
        raise SystemExit('Remove existing webhook before long polling.')
    offset = None
    logging.info('Started @%s. Binance Spot and Futures USDT, Coinbase USD/RUB, statistics and price notifications.', me.get('username'))
    notify = PriceNotifications(sys.modules[__name__], stats.path, allowed)
    with LiveController(sys.modules[__name__]) as live, AdminPanel(sys.modules[__name__], stats.path) as admin, notify:
        while True:
            try:
                params = {'timeout': 25, 'allowed_updates': ['message', 'widget_query', 'shared_widget_query', 'my_chat_member']}
                if offset is not None:
                    params['offset'] = offset
                updates = api('getUpdates', **params)
                for update in updates:
                    try:
                        if 'widget_query' in update:
                            query = update['widget_query']
                            if not admin.handle_query(query) and not notify.handle_query(query):
                                live.handle_query(query, allowed)
                        elif 'shared_widget_query' in update:
                            query = update['shared_widget_query']
                            if not admin.handle_query(query, shared=True):
                                live.handle_query(query, allowed, shared=True)
                        elif 'my_chat_member' in update:
                            notify.handle_member(update['my_chat_member'])
                    except Exception as error:
                        logging.warning('Update handling: %s', error)
                for update in updates:
                    record_widget_request(stats, me['id'], update)
                    message = update.get('message', {})
                    chat = message.get('chat', {})
                    words = message.get('text', '').split()
                    command = words[0].split('@')[0] if words else ''
                    user_id = message.get('from', {}).get('id')
                    if command in ('/widget', '/prices', '/start', '/help', '/statistic', '/notification', '/notificationsettings'):
                        logging.info('Command update=%s user=%s command=%s', update['update_id'], user_id, command)
                    if admin.handle_message(message):
                        pass
                    elif chat.get('type') == 'private' and (not allowed or user_id == allowed):
                        try:
                            if command in ('/widget', '/prices'):
                                live.enqueue_send(chat['id'])
                            elif command == '/notificationsettings':
                                notify.enqueue_settings(chat['id'])
                            elif command in ('/start', '/help'):
                                api('sendMessage', chat_id=chat['id'], text='/widget — цены Binance в USDT и фьючерсы на металлы, пока виджет виден.\n/notificationsettings — ежедневный виджет с ценами в выбранное время по МСК.')
                        except Exception as error:
                            logging.error('Command: %s', error)
                    offset = update['update_id'] + 1
            except Exception as error:
                logging.warning('Polling: %s', error)
                time.sleep(5)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
