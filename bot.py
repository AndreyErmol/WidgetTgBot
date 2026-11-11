import hashlib
import json
import logging
import math
import os
import random
import sys
import threading
import time
import zlib
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parent
DATA_DIR = Path('/data')
ASSETS = (('BTC', 'bitcoin'), ('ETH', 'ethereum'), ('BNB', 'bnb'), ('XRP', 'xrp'),
          ('SUI', 'sui'), ('SOL', 'solana'), ('TRX', 'tron'), ('HYPE', 'hyperliquid'),
          ('ZEC', 'zcash'), ('DOGE', 'dogecoin'))
TOKEN = ''
LOCK = threading.RLock()
SEND_LOCK = threading.Lock()
STATE = {'coins': [], 'ct': 0, 'fx': None, 'ft': 0}
SAVED = {}


class APIError(Exception):
    def __init__(self, message, limited=False, retry=0):
        super().__init__(message)
        self.limited, self.retry = limited, retry


class RateLimiter:
    def __init__(self, limit=50, period=60, clock=time.monotonic):
        self.limit, self.period, self.clock = limit, period, clock
        self.lock, self.attempts = threading.Lock(), deque()

    def reserve(self):
        with self.lock:
            now = self.clock()
            while self.attempts and self.attempts[0] <= now - self.period:
                self.attempts.popleft()
            if len(self.attempts) >= self.limit:
                return False
            self.attempts.append(now)
            return True


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
    request = Request(url, data=body, headers={'User-Agent': 'CryptunaPriceWidget/6',
                                              'Content-Type': 'application/json'})
    try:
        seconds = timeout if timeout is not None else (40 if params is not None else 8)
        with urlopen(request, timeout=seconds) as response:
            return json.load(response)
    except HTTPError as error:
        retry = retry_seconds(error.headers.get('Retry-After'))
        try:
            payload = json.loads(error.read(8192))
            retry = max(retry, retry_seconds(payload.get('parameters', {}).get('retry_after')))
        except (ValueError, AttributeError):
            pass
        raise APIError(f'HTTP {error.code}', error.code == 429, retry) from None
    except (URLError, OSError, ValueError) as error:
        detail = str(getattr(error, 'reason', error))
        if TOKEN:
            detail = detail.replace(TOKEN, '<redacted>')
        raise APIError(detail) from None


class Cache:
    def __init__(self):
        self.lock, self.values = threading.Lock(), {}
        self.until, self.failures = 0, 0

    def get(self, key, loader, ttl=10):
        with self.lock:
            now, old = time.monotonic(), self.values.get(key)
            if old and (now < old[2] or now < self.until):
                return old[:2]
            if now < self.until:
                raise APIError('Provider cooldown')
            try:
                data = loader()
            except Exception as error:
                self.failures += 1
                delay = min(900, 5 * 2 ** min(self.failures - 1, 8))
                if isinstance(error, APIError) and error.limited:
                    delay = max(60, delay, error.retry)
                self.until = time.monotonic() + delay + random.uniform(0, 1)
                raise
            self.failures, self.until = 0, 0
            entry = (data, int(time.time()), time.monotonic() + ttl)
            self.values[key] = entry
            return entry[:2]


CMC_CACHE, FX_CACHE = Cache(), Cache()
CMC_LIMIT = RateLimiter()
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
                           retry_seconds(payload.get('parameters', {}).get('retry_after')))
        return payload['result']
    except APIError as error:
        if error.limited:
            with TG_LOCK:
                TG_UNTIL = max(TG_UNTIL, time.monotonic() + max(1, error.retry or 60))
        raise APIError(f'Telegram/{method}: {error}', error.limited, error.retry) from None


def finite(value, positive=False):
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError('Invalid numeric quote')
    return number


def records(data):
    if isinstance(data, list):
        return data
    return [item for value in data.values() for item in (value if isinstance(value, list) else [value])]


def coin(item):
    quotes = item['quote']
    usd = next(q for q in quotes if q.get('symbol') == 'USD') if isinstance(quotes, list) else quotes['USD']
    return {'id': int(item['id']), 'name': str(item['name']), 'symbol': str(item['symbol']),
            'slug': item.get('slug', ''), 'rank': int(item['cmc_rank']),
            'p': finite(usd['price'], True), 'c': finite(usd['percent_change_24h'])}


def order_coins(items):
    by_slug = {c.get('slug'): c for c in items}
    ordered = []
    for symbol, slug in ASSETS:
        c = by_slug.get(slug)
        if c is None or c['symbol'].upper() != symbol:
            raise ValueError(f'Missing or mismatched quote: {symbol}/{slug}')
        ordered.append(c)
    return ordered


def fetch_coins():
    params = {'slug': ','.join(slug for _, slug in ASSETS), 'convert': 'USD'}
    key = 'v3/cryptocurrency/quotes/latest?' + urlencode(params)
    def load():
        if not CMC_LIMIT.reserve():
            raise APIError('Local CMC limit: 50 attempts per rolling 60 seconds')
        response = json_request('https://pro-api.coinmarketcap.com/public-api/' + key)
        status = response.get('status', {})
        code = int(status.get('error_code') or 0)
        if code:
            raise APIError(str(status.get('error_message', 'CMC error')),
                           code in (429, 1008, 1009, 1010), 60)
        return order_coins(list(map(coin, records(response['data']))))
    return CMC_CACHE.get(key, load, ttl=10)


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
    for size in (20, 12, 6, 0):
        rows = [[c['id'], c['name'][:size], c['symbol'], c['rank'],
                 format_price(c['p']), format_percent(c['c']), int(c['c'] >= 0)] for c in coins]
        state = {'r': time.time_ns() // 1000, 't': snapshot['ct'], 'q': rows,
                 'f': format_price(snapshot['fx']) if snapshot['fx'] else None}
        text = json.dumps(state, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(text.encode()) <= 1024:
            return text
    raise ValueError('State exceeds 1024 bytes')


def persist():
    with LOCK:
        data = dict(SAVED, state=STATE, bot=TOKEN.split(':')[0], version=6)
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
            logging.warning('CMC: %s; serving local prices', error)
    if STATE['fx'] is None or time.time() - STATE['ft'] >= 60:
        try:
            def load_fx():
                data = json_request('https://api.coinbase.com/v2/exchange-rates?currency=USD')['data']
                if data['currency'] != 'USD':
                    raise ValueError('Wrong FX currency')
                return finite(data['rates']['RUB'], True)
            rate, stamp = FX_CACHE.get('USD/RUB', load_fx, ttl=60)
            with LOCK:
                STATE.update(fx=rate, ft=stamp)
        except Exception as error:
            logging.warning('Coinbase: %s; serving local rate', error)


def send_widget(chat_id):
    with SEND_LOCK:
        refresh_on_demand()
        persist()
        markup = (BASE / 'widget.html').read_text(encoding='utf-8')
        raw = markup.encode()
        if len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096 or b'\x00' in raw:
            raise ValueError('HTML exceeds Telegram limits')
        digest, state = hashlib.sha256(raw).hexdigest(), pack()
        content = json.loads(state)
        content.pop('r', None)
        content_hash = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        widget_id = SAVED.get('widget_id') if SAVED.get('template') == digest else None
        if widget_id:
            if SAVED.get('content_hash') != content_hash:
                try:
                    api('editSharedWidgetData', widget_id=widget_id, public_data=state, cache_time=10)
                    SAVED['content_hash'] = content_hash
                except APIError as error:
                    logging.warning('Shared data update failed: %s', error)
            api('sendSharedWidget', chat_id=chat_id, widget_id=widget_id)
        else:
            result = api('sendWidget', chat_id=chat_id, html=markup, width=360, height=0,
                         is_shared=True, allow_queries=True, public_data=state)
            SAVED.update(widget_id=result['widget']['id'], template=digest, content_hash=content_hash)
        persist()


def restore(saved):
    if saved.get('bot') != TOKEN.split(':')[0]:
        return
    old = saved.get('state', {}) if saved.get('version') in (4, 5, 6) else saved
    try:
        coins = order_coins(old.get('coins', []))
    except (ValueError, KeyError, TypeError):
        coins = []
    STATE.update(coins=coins, ct=old.get('ct', 0) if coins else 0,
                 fx=old.get('fx'), ft=old.get('ft', 0))
    if saved.get('version') == 6:
        SAVED.update(saved)


def main():
    from live_updates import LiveController
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
    logging.info('Started @%s. Visible-widget refresh enabled; SQLite stats in /data.', me.get('username'))
    with LiveController(sys.modules[__name__]) as live:
        while True:
            try:
                params = {'timeout': 25, 'allowed_updates': ['message', 'widget_query', 'shared_widget_query']}
                if offset is not None:
                    params['offset'] = offset
                updates = api('getUpdates', **params)
                # Answer queries before scheduling commands from the same batch.
                for update in updates:
                    try:
                        if 'widget_query' in update:
                            live.handle_query(update['widget_query'], allowed)
                        elif 'shared_widget_query' in update:
                            live.handle_query(update['shared_widget_query'], allowed, shared=True)
                    except Exception as error:
                        logging.warning('Query handling: %s', error)
                for update in updates:
                    # Commit the audit event before processing or acknowledging its update.
                    # DB failures reach the polling retry without advancing this update's offset.
                    record_widget_request(stats, me['id'], update)
                    message = update.get('message', {})
                    chat = message.get('chat', {})
                    if chat.get('type') == 'private' and (not allowed or message.get('from', {}).get('id') == allowed):
                        words = message.get('text', '').split()
                        command = words[0].split('@')[0] if words else ''
                        try:
                            if command in ('/widget', '/prices'):
                                live.enqueue_send(chat['id'])
                            elif command in ('/start', '/help'):
                                api('sendMessage', chat_id=chat['id'], text='/widget — CryptunaPriceWidget. Цены обновляются, пока виджет виден.')
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
