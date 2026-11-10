import hashlib
import json
import logging
import math
import os
import random
import threading
import time
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parent
TOKEN = ''
STOP = threading.Event()
LOCK = threading.RLock()
SEND_LOCK = threading.Lock()
STATE = {'coins': [], 'ct': 0, 'fx': None, 'ft': 0}
SAVED = {}


class APIError(Exception):
    def __init__(self, message, limited=False, retry=0):
        super().__init__(message)
        self.limited, self.retry = limited, retry


def retry_seconds(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def json_request(url, params=None):
    body = None if params is None else json.dumps(params).encode()
    request = Request(url, data=body, headers={'User-Agent': 'CryptunaPriceWidget/4',
                                              'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=40 if params is not None else 8) as response:
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
        self.lock = threading.Lock()
        self.values = {}
        self.until = 0
        self.failures = 0

    def get(self, key, loader, ttl=10):
        # One loader at a time; concurrent callers reuse the same response.
        with self.lock:
            now = time.monotonic()
            old = self.values.get(key)
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
            self.failures = 0
            self.until = 0
            entry = (data, int(time.time()), time.monotonic() + ttl)
            self.values[key] = entry
            return entry[:2]


CMC_CACHE, FX_CACHE = Cache(), Cache()
TG_LOCK = threading.Lock()
TG_UNTIL = 0


def api(method, **params):
    global TG_UNTIL
    with TG_LOCK:
        if time.monotonic() < TG_UNTIL:
            raise APIError('Telegram cooldown')
    # Long polling must not block publishing or sending widgets.
    try:
        payload = json_request(f'https://api.telegram.org/bot{TOKEN}/{method}', params)
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


def cmc(path, **params):
    key = path + '?' + urlencode(sorted(params.items()))
    def load():
        response = json_request('https://pro-api.coinmarketcap.com/public-api/' + key)
        status = response.get('status', {})
        code = int(status.get('error_code') or 0)
        if code:
            raise APIError(str(status.get('error_message', 'CMC error')),
                           code in (429, 1008, 1009, 1010), 60)
        return response['data']
    return CMC_CACHE.get(key, load)


def records(data):
    if isinstance(data, list):
        return data
    return [item for value in data.values() for item in (value if isinstance(value, list) else [value])]


def finite(value, positive=False):
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError('Invalid numeric quote')
    return number


def coin(item):
    quotes = item['quote']
    usd = next(q for q in quotes if q.get('symbol') == 'USD') if isinstance(quotes, list) else quotes['USD']
    return {'id': int(item['id']), 'name': str(item['name']), 'symbol': str(item['symbol']),
            'slug': item.get('slug', ''), 'rank': int(item['cmc_rank']),
            'p': finite(usd['price'], True), 'c': finite(usd['percent_change_24h'])}


def select_coins(items, sui):
    others = sorted({c['id']: c for c in items if c['id'] != sui['id'] and
                     c['symbol'].upper() not in ('USDT', 'USDC')}.values(), key=lambda c: c['rank'])[:9]
    if len(others) != 9:
        raise ValueError('Need nine eligible coins plus Sui')
    return others[:4] + [sui] + others[4:]


def fetch_coins():
    data, stamp = cmc('v3/cryptocurrency/listings/latest', start=1, limit=100,
                      convert='USD', sort='market_cap', sort_dir='desc')
    items = list(map(coin, records(data)))
    sui = next((c for c in items if c['slug'] == 'sui'), None)
    if sui is None:
        extra, extra_stamp = cmc('v3/cryptocurrency/quotes/latest', slug='sui', convert='USD')
        sui = coin(next(c for c in records(extra) if c.get('slug') == 'sui'))
        stamp = min(stamp, extra_stamp)
    return select_coins(items, sui), stamp


def format_price(value):
    number = finite(value, True)
    places = 6 if number < .01 else 2
    whole, fraction = f'{number:.{places}f}'.split('.')
    groups = [whole[max(0, i-3):i] for i in range(len(whole), 0, -3)]
    return ' '.join(reversed(groups)) + ',' + fraction


def format_percent(value):
    number = round(finite(value), 2)
    if number == 0:
        number = 0.0
    return f'{number:+.2f}'.replace('.', ',') + '%'


def pack():
    with LOCK:
        snapshot = dict(STATE)
        coins = [dict(c) for c in STATE['coins']]
    for name_limit in (20, 12, 6, 0):
        rows = [[c['id'], c['name'][:name_limit], c['symbol'][:12], c['rank'],
                 format_price(c['p']), format_percent(c['c']), int(c['c'] >= 0)] for c in coins]
        state = {'r': time.time_ns() // 1000, 't': snapshot['ct'], 'q': rows,
                 'f': format_price(snapshot['fx']) if snapshot['fx'] else None}
        text = json.dumps(state, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(text.encode()) <= 1024:
            return text
    raise ValueError('State exceeds 1024 bytes')


def persist():
    with LOCK:
        data = dict(SAVED, state=STATE, bot=TOKEN.split(':')[0], version=4)
        tmp = BASE / 'runtime.json.tmp'
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(BASE / 'runtime.json')


def source_loop():
    while not STOP.is_set():
        started = time.monotonic()
        try:
            coins, stamp = fetch_coins()
            with LOCK:
                STATE.update(coins=coins, ct=stamp)
        except Exception as error:
            logging.warning('CMC: %s; keeping cached prices', error)
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
            logging.warning('Coinbase: %s; keeping cached rate', error)
        try:
            with LOCK:
                widget_id = SAVED.get('widget_id')
            if widget_id:
                api('editSharedWidgetData', widget_id=widget_id, public_data=pack(), cache_time=10)
            persist()
        except Exception as error:
            logging.warning('Publish/save: %s', error)
        STOP.wait(max(0, 10 - (time.monotonic() - started)))


def send_widget(chat_id):
    # No market-data calls here: all viewers use one in-memory snapshot.
    with SEND_LOCK:
        markup = (BASE / 'widget.html').read_text(encoding='utf-8')
        raw = markup.encode()
        if len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096 or b'\x00' in raw:
            raise ValueError('HTML exceeds Telegram limits')
        digest = hashlib.sha256(raw).hexdigest()
        with LOCK:
            widget_id = SAVED.get('widget_id') if SAVED.get('template') == digest else None
        if widget_id:
            api('sendSharedWidget', chat_id=chat_id, widget_id=widget_id)
        else:
            result = api('sendWidget', chat_id=chat_id, html=markup, width=360, height=0,
                         is_shared=True, public_data=pack())
            with LOCK:
                SAVED.update(widget_id=result['widget']['id'], template=digest)
            persist()


def main():
    global TOKEN, SAVED
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    TOKEN = os.environ.get('BOT_TOKEN', '').strip()
    if not TOKEN:
        raise SystemExit('Set BOT_TOKEN.')
    allowed = int(os.environ.get('ALLOWED_USER_ID', '0'))
    path = BASE / 'runtime.json'
    if path.exists():
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('bot') == TOKEN.split(':')[0]:
            if saved.get('version') == 4:
                SAVED = saved
                STATE.update(saved.get('state', {}))
            elif saved.get('version') == 3:
                # Keep useful old prices, never reuse the old layout.
                coins = saved.get('coins', [])
                sui = next((c for c in coins if c['symbol'].upper() == 'SUI'), None)
                try:
                    STATE['coins'] = select_coins(coins, sui) if sui else []
                except (ValueError, TypeError):
                    STATE['coins'] = []
                STATE.update(fx=saved.get('fx'), ft=saved.get('ft', 0), ct=saved.get('ct', 0))
    me = api('getMe')
    if api('getWebhookInfo').get('url'):
        raise SystemExit('Remove existing webhook before long polling.')
    worker = threading.Thread(target=source_loop, daemon=True)
    worker.start()
    offset = None
    logging.info('Started @%s. Send /widget.', me.get('username'))
    try:
        while not STOP.is_set():
            try:
                params = {'timeout': 25, 'allowed_updates': ['message']}
                if offset is not None:
                    params['offset'] = offset
                for update in api('getUpdates', **params):
                    message = update.get('message', {})
                    chat = message.get('chat', {})
                    if chat.get('type') == 'private' and (not allowed or message.get('from', {}).get('id') == allowed):
                        words = message.get('text', '').split()
                        command = words[0].split('@')[0] if words else ''
                        try:
                            if command in ('/widget', '/prices'):
                                send_widget(chat['id'])
                            elif command in ('/start', '/help'):
                                api('sendMessage', chat_id=chat['id'], text='/widget — CryptunaPriceWidget')
                        except Exception as error:
                            logging.error('Command: %s', error)
                    offset = update['update_id'] + 1
            except Exception as error:
                logging.warning('Polling: %s', error)
                STOP.wait(5)
    finally:
        STOP.set()
        worker.join(timeout=12)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
