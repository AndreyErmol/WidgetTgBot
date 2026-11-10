import base64
import hashlib
import html
import io
import json
import logging
import math
import os
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from PIL import Image

BASE = Path(__file__).resolve().parent
LOCK = threading.RLock()
STOP = threading.Event()
CURRENT = {'coins': [], 'fx': None, 'ct': 0, 'ft': 0}
SAVED = {}
TOKEN = ''


class APIError(Exception):
    pass


def http(url, data=None):
    body = None if data is None else json.dumps(data).encode()
    request = Request(url, data=body, headers={
        'User-Agent': 'CryptunaPriceWidget/3', 'Content-Type': 'application/json'})
    try:
        with urlopen(request, timeout=40 if data is not None else 8) as response:
            return response.read(2000000)
    except (HTTPError, URLError, OSError) as error:
        detail = str(getattr(error, 'reason', error))
        if TOKEN:
            detail = detail.replace(TOKEN, '<redacted>')
        raise APIError(detail) from None


def api(method, **params):
    response = json.loads(http(f'https://api.telegram.org/bot{TOKEN}/{method}', params))
    if not response.get('ok'):
        raise APIError(str(response.get('description', 'Telegram error')).replace(TOKEN, '<redacted>'))
    return response['result']


def cmc(path, **params):
    response = json.loads(http('https://pro-api.coinmarketcap.com/public-api/' + path + '?' + urlencode(params)))
    if str(response.get('status', {}).get('error_code', 0)) != '0':
        raise APIError(str(response.get('status', {}).get('error_message', 'CMC error')))
    return response['data']


def records(data):
    if isinstance(data, list):
        return data
    return [item for value in data.values() for item in (value if isinstance(value, list) else [value])]


def finite(value, positive=False):
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError('Invalid numeric quote')
    return result


def coin(item):
    quotes = item['quote']
    usd = next(q for q in quotes if q.get('symbol') == 'USD') if isinstance(quotes, list) else quotes['USD']
    return {'id': int(item['id']), 'name': str(item['name'])[:40],
            'symbol': str(item['symbol'])[:15], 'rank': int(item['cmc_rank']),
            'p': float(format(finite(usd['price'], True), '.9g')),
            'c': round(finite(usd['percent_change_24h']), 2)}


def select_coins(items, sui):
    others = sorted({c['id']: c for c in items if c['id'] != sui['id']}.values(), key=lambda c: c['rank'])[:9]
    if len(others) != 9:
        raise ValueError('Need nine ranked coins plus Sui')
    return others[:4] + [sui] + others[4:]


def fetch_coins():
    top = [coin(c) for c in cmc('v3/cryptocurrency/listings/latest', start=1, limit=10,
                              convert='USD', sort='market_cap', sort_dir='desc')]
    sui = next((c for c in top if c['symbol'].upper() == 'SUI'), None)
    if sui is None:
        candidates = records(cmc('v3/cryptocurrency/quotes/latest', slug='sui', convert='USD'))
        sui = coin(next(c for c in candidates if c.get('slug') == 'sui'))
    return select_coins(top, sui)


def pack(coins, fx, ct, ft):
    state = {'r': time.time_ns() // 1000, 't': ct, 'f': [fx, ft],
             'q': [[c['id'], c['rank'], c['p'], c['c']] for c in coins]}
    text = json.dumps(state, separators=(',', ':'), allow_nan=False)
    if len(text.encode()) > 1024:
        raise ValueError('Widget state exceeds 1024 bytes')
    return text


def persist():
    temporary = BASE / 'runtime.json.tmp'
    temporary.write_text(json.dumps(SAVED, ensure_ascii=True), encoding='utf-8')
    temporary.replace(BASE / 'runtime.json')


def source_loop():
    while not STOP.is_set():
        started = time.monotonic()
        try:
            fresh = fetch_coins()
            with LOCK:
                CURRENT['coins'] = fresh
                CURRENT['ct'] = int(time.time())
        except Exception as error:
            logging.warning('CMC: %s; keeping previous prices', error)
        try:
            fx = json.loads(http('https://api.coinbase.com/v2/exchange-rates?currency=USD'))['data']
            if fx['currency'] != 'USD':
                raise ValueError('Unexpected base currency')
            rate = round(finite(fx['rates']['RUB'], True), 4)
            with LOCK:
                CURRENT['fx'], CURRENT['ft'] = rate, int(time.time())
        except Exception as error:
            logging.warning('USD/RUB: %s; keeping previous rate', error)
        try:
            with LOCK:
                if SAVED.get('version') == 3 and SAVED.get('widget_id'):
                    previous = {c['id']: c for c in SAVED['coins']}
                    latest = {c['id']: c for c in CURRENT['coins']}
                    missing = [i for i in previous if i not in latest]
                    if missing:
                        try:
                            latest.update({c['id']: c for c in map(coin, records(cmc(
                                'v3/cryptocurrency/quotes/latest', id=','.join(map(str, missing)), convert='USD')))})
                        except Exception as error:
                            logging.warning('Retained roster quotes: %s', error)
                    roster = [latest.get(c['id'], c) for c in SAVED['coins']]
                    ct = CURRENT['ct'] if all(i in latest for i in previous) else SAVED.get('ct', 0)
                    state = pack(roster, CURRENT['fx'], ct, CURRENT['ft'])
                    api('editSharedWidgetData', widget_id=SAVED['widget_id'], public_data=state, cache_time=10)
                    SAVED.update(coins=roster, fx=CURRENT['fx'], ct=ct, ft=CURRENT['ft'])
                    persist()
        except Exception as error:
            logging.warning('Publish: %s', error)
        STOP.wait(max(0, 10 - (time.monotonic() - started)))


def logo_bytes(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != 's2.coinmarketcap.com':
        raise ValueError('Unexpected logo host')
    raw = http(url)
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        return image.convert('RGBA')


def build_widget(coins):
    metadata = cmc('v2/cryptocurrency/info', id=','.join(str(c['id']) for c in coins))
    urls = [metadata[str(c['id'])]['logo'] for c in coins]
    with ThreadPoolExecutor(max_workers=5) as pool:
        images = list(pool.map(logo_bytes, urls))
    template = (BASE / 'widget.html').read_text(encoding='utf-8')
    for size, colors in ((20, 8), (16, 4), (12, 4), (12, 2)):
        rows = []
        for n, (c, image) in enumerate(zip(coins, images)):
            tiny = image.resize((size, size), Image.Resampling.LANCZOS).quantize(colors=colors)
            output = io.BytesIO()
            tiny.save(output, format='PNG', optimize=True)
            logo = base64.b64encode(output.getvalue()).decode()
            sub = html.escape(c['symbol']) + (' · закреплена' if n == 4 else '')
            rows.append('<div class="row" id="row%d"><div class="left"><img width="24" height="24" src="data:image/png;base64,%s"><div><b>%s</b><small id="sub%d">%s</small></div></div><div class="right"><b id="p%d">—</b><small id="c%d">—</small></div></div>' % (n, logo, html.escape(c['name']), n, sub, n, n))
        config = [[c['id'], c['symbol'], n == 4] for n, c in enumerate(coins)]
        result = template.replace('__ROWS__', ''.join(rows)).replace('__COINS__', json.dumps(config, ensure_ascii=True).replace('<', '\u003c'))
        raw = result.encode()
        if len(raw) <= 32768 and len(zlib.compress(raw, 9)) <= 4096 and b'\x00' not in raw:
            return result
    raise ValueError('Logos and HTML exceed Telegram limits; no fake icons substituted')


def send_widget(chat_id):
    with LOCK:
        coins = [dict(c) for c in CURRENT['coins']]
        if len(coins) != 10:
            raise APIError('CMC has not returned ten coins yet. Retry /widget shortly; see logs.')
        ids = [c['id'] for c in coins]
        template_hash = hashlib.sha256((BASE / 'widget.html').read_bytes()).hexdigest()
        if (SAVED.get('version') == 3 and SAVED.get('widget_id') and
                SAVED.get('ids') == ids and SAVED.get('template') == template_hash):
            api('sendSharedWidget', chat_id=chat_id, widget_id=SAVED['widget_id'])
            return
    markup = build_widget(coins)
    with LOCK:
        latest = {c['id']: c for c in CURRENT['coins']}
        roster = [latest.get(c['id'], c) for c in coins]
        state = pack(roster, CURRENT['fx'], CURRENT['ct'], CURRENT['ft'])
        message = api('sendWidget', chat_id=chat_id, html=markup, width=360, height=0,
                      is_shared=True, public_data=state)
        SAVED.clear()
        SAVED.update(version=3, bot=TOKEN.split(':')[0], widget_id=message['widget']['id'],
                     ids=ids, template=template_hash, coins=roster,
                     fx=CURRENT['fx'], ct=CURRENT['ct'], ft=CURRENT['ft'])
        persist()
        api('editSharedWidgetData', widget_id=SAVED['widget_id'], public_data=state, cache_time=10)


def main():
    global TOKEN, SAVED
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    TOKEN = os.environ.get('BOT_TOKEN', '').strip()
    if not TOKEN:
        raise SystemExit('Set BOT_TOKEN.')
    allowed = int(os.environ.get('ALLOWED_USER_ID', '0'))
    if (BASE / 'runtime.json').exists():
        SAVED = json.loads((BASE / 'runtime.json').read_text())
        if SAVED.get('version') != 3 or SAVED.get('bot') != TOKEN.split(':')[0]:
            SAVED = {}
        else:
            CURRENT.update(coins=SAVED['coins'], fx=SAVED.get('fx'), ct=SAVED.get('ct', 0), ft=SAVED.get('ft', 0))
    me = api('getMe')
    if api('getWebhookInfo').get('url'):
        raise SystemExit('Remove existing webhook before long polling.')
    worker = threading.Thread(target=source_loop, daemon=True)
    worker.start()
    offset = None
    logging.info('Started @%s. Send /widget.', me.get('username'))
    try:
        while True:
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
                                api('sendMessage', chat_id=chat['id'], text='/widget — CryptunaPriceWidget, 10 монет и SUI на первой странице.')
                        except Exception as error:
                            logging.error('Send widget: %s', error)
                            api('sendMessage', chat_id=chat['id'], text='Не удалось создать виджет. Повтори /widget чуть позже; подробности в логах.')
                    offset = update['update_id'] + 1
            except (APIError, ValueError) as error:
                logging.warning('Polling: %s; retry in 5 s', error)
                STOP.wait(5)
    finally:
        STOP.set()
        worker.join(timeout=12)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
