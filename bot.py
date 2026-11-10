import hashlib
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
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parent
INTERVAL = 10
SYMBOLS = ('BTC', 'ETH', 'SOL', 'USD')


class APIError(Exception):
    pass


def request_json(url, data=None, timeout=8, secret=''):
    headers = {'User-Agent': 'WidgetTgBot/2.0', 'Accept': 'application/json'}
    body = None
    if data is not None:
        body = json.dumps(data).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        raise APIError(f'HTTP {error.code}') from None
    except (URLError, OSError, ValueError) as error:
        detail = str(getattr(error, 'reason', error))
        if secret:
            detail = detail.replace(secret, '<redacted>')
        raise APIError(f'{type(error).__name__}: {detail}') from None


def telegram(token, method, **params):
    result = request_json(f'https://api.telegram.org/bot{token}/{method}', params, 40, token)
    if not result.get('ok'):
        description = str(result.get('description', 'Unknown error')).replace(token, '<redacted>')
        raise APIError(f'{method}: {description}')
    return result['result']


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError('Price must be positive and finite')
    return number


def fetch_quote(symbol):
    if symbol == 'USD':
        data = request_json('https://api.coinbase.com/v2/exchange-rates?currency=USD')['data']
        if data['currency'] != 'USD':
            raise ValueError('Unexpected FX base currency')
        price = positive(data['rates']['RUB'])
        change = None
    else:
        data = request_json(f'https://api.exchange.coinbase.com/products/{symbol}-USD/stats')
        price = positive(data['last'])
        opening = positive(data['open'])
        change = round((price / opening - 1) * 100, 2)
    # t is receipt time, not the exchange's quote timestamp.
    return {'p': round(price, 4), 'c': change, 't': int(time.time()), 'e': False}


def encode_state(state):
    text = json.dumps(state, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    if len(text.encode('utf-8')) > 1024:
        raise ValueError('Widget state exceeds 1024 bytes')
    return text


class Market:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {'r': time.time_ns() // 1000, 'q': {}}

    def refresh(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {symbol: pool.submit(fetch_quote, symbol) for symbol in SYMBOLS}
            quotes = {}
            for symbol, job in jobs.items():
                try:
                    quotes[symbol] = job.result()
                except Exception as error:
                    logging.warning('%s source unavailable: %s', symbol, error)
                    with self.lock:
                        previous = self.state['q'].get(symbol, {})
                        quotes[symbol] = dict(previous, e=True)
        with self.lock:
            self.state = {'r': max(self.state['r'] + 1, time.time_ns() // 1000), 'q': quotes}

    def snapshot(self):
        with self.lock:
            return encode_state(self.state)


class Widgets:
    def __init__(self, token, html):
        self.token = token
        self.html = html
        self.digest = hashlib.sha256(html.encode('utf-8')).hexdigest()
        self.lock = threading.Lock()
        self.widget_id = None
        try:
            saved = json.loads((BASE / 'runtime.json').read_text())
            if saved.get('layout') == self.digest and saved.get('bot') == token.split(':')[0]:
                self.widget_id = saved.get('widget_id')
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            raise SystemExit(f'Cannot read runtime.json: {error}') from None

    def send(self, chat_id, state):
        with self.lock:
            caption = 'BTC, ETH, SOL: Coinbase Exchange (USD). USD/RUB: справочный Coinbase. Опрос каждые 10 с; возможны задержки.'
            if self.widget_id:
                return telegram(self.token, 'sendSharedWidget', chat_id=chat_id,
                                widget_id=self.widget_id, caption=caption)
            result = telegram(self.token, 'sendWidget', chat_id=chat_id, html=self.html,
                              caption=caption, width=340, height=0,
                              is_shared=True, public_data=state)
            self.widget_id = result['widget']['id']
            saved = {'layout': self.digest, 'bot': self.token.split(':')[0], 'widget_id': self.widget_id}
            temporary = BASE / 'runtime.json.tmp'
            temporary.write_text(json.dumps(saved), encoding='utf-8')
            temporary.replace(BASE / 'runtime.json')
            telegram(self.token, 'editSharedWidgetData', widget_id=self.widget_id,
                     public_data=state, cache_time=INTERVAL)
            return result

    def publish(self, state):
        with self.lock:
            if self.widget_id:
                telegram(self.token, 'editSharedWidgetData', widget_id=self.widget_id,
                         public_data=state, cache_time=INTERVAL)


def updater(market, widgets, stop):
    while not stop.is_set():
        started = time.monotonic()
        market.refresh()
        try:
            widgets.publish(market.snapshot())
        except APIError as error:
            logging.warning('Widget update failed: %s', error)
        stop.wait(max(0, INTERVAL - (time.monotonic() - started)))


def handle_message(token, message, market, widgets, allowed_user):
    chat = message.get('chat', {})
    if chat.get('type') != 'private':
        return
    if allowed_user and message.get('from', {}).get('id') != allowed_user:
        return
    words = message.get('text', '').split()
    command = words[0].split('@')[0] if words else ''
    if command in ('/widget', '/prices'):
        widgets.send(chat['id'], market.snapshot())
    elif command in ('/start', '/help'):
        telegram(token, 'sendMessage', chat_id=chat['id'], text=(
            '/widget — BTC, ETH, SOL и USD/RUB. Опрос источников каждые 10 секунд. '
            'Криптовалюты: USD, Coinbase Exchange; доллар: справочный курс Coinbase, не ЦБ. '
            'Для обновления бот должен оставаться запущенным.'))


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.environ.get('BOT_TOKEN', '').strip()
    if not token:
        raise SystemExit('Set BOT_TOKEN before starting the bot.')
    try:
        allowed_user = int(os.environ.get('ALLOWED_USER_ID', '0'))
    except ValueError:
        raise SystemExit('ALLOWED_USER_ID must be an integer.') from None
    html = (BASE / 'widget.html').read_text(encoding='utf-8')
    raw = html.encode('utf-8')
    if b'\x00' in raw or len(raw) > 32768 or len(zlib.compress(raw, 9)) > 4096:
        raise SystemExit('widget.html exceeds Telegram limits or contains NUL.')
    try:
        me = telegram(token, 'getMe')
        if telegram(token, 'getWebhookInfo').get('url'):
            raise SystemExit('Remove the existing webhook before using long polling.')
    except APIError as error:
        raise SystemExit(str(error)) from None
    market = Market()
    widgets = Widgets(token, html)
    stop = threading.Event()
    worker = threading.Thread(target=updater, args=(market, widgets, stop), daemon=True)
    worker.start()
    offset = None
    logging.info('Started @%s. Send /widget.', me.get('username', 'bot'))
    try:
        while True:
            try:
                params = {'timeout': 25, 'allowed_updates': ['message']}
                if offset is not None:
                    params['offset'] = offset
                for update in telegram(token, 'getUpdates', **params):
                    try:
                        if 'message' in update:
                            handle_message(token, update['message'], market, widgets, allowed_user)
                    except (APIError, OSError, KeyError) as error:
                        logging.error('Command failed: %s', error)
                    offset = update['update_id'] + 1
            except APIError as error:
                logging.error('getUpdates: %s; retrying in 5 seconds', error)
                time.sleep(5)
    finally:
        stop.set()
        worker.join(timeout=12)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
