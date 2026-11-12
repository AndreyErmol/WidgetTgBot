import json
import logging
import math
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

SOURCE = 'binance_spot_futures_usdt'
CRYPTO = ('BTC', 'ETH', 'BNB', 'XRP', 'SUI', 'SOL', 'TRX', 'HYPE', 'GRAM', 'DOGE')
METALS = ('XAU', 'XPD', 'XAG')
ASSETS = CRYPTO + METALS
SPOT_URL = 'https://api.binance.com/api/v3/ticker/24hr'
FUTURES_URL = 'https://fapi.binance.com/fapi/v1/ticker/24hr'
TTL = 10


def number(value, positive=False):
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError('Invalid market value')
    return value


class Slot:
    def __init__(self, name):
        self.name, self.lock = name, threading.Lock()
        self.data, self.stamp, self.fresh, self.retry, self.failures = None, 0, 0, 0, 0

    def seed(self, data, stamp, ttl):
        stamp = int(number(stamp))
        if stamp <= 0 or stamp > time.time() + 60:
            return
        with self.lock:
            self.data, self.stamp = data, stamp
            self.fresh = time.monotonic() + max(0, ttl - max(0, time.time() - stamp))

    def due(self):
        with self.lock:
            now = time.monotonic()
            return now >= self.fresh and now >= self.retry

    def snapshot(self):
        with self.lock:
            return self.data, self.stamp

    def store(self, data, ttl):
        with self.lock:
            if self.failures:
                logging.info('%s recovered', self.name)
            self.data, self.stamp = data, int(time.time())
            self.fresh, self.retry, self.failures = time.monotonic() + ttl, 0, 0

    def fail(self, error, log=True):
        with self.lock:
            self.failures += 1
            delay = min(900, 5 * 2 ** min(self.failures - 1, 8))
            if getattr(error, 'limited', False):
                delay = max(60, delay, getattr(error, 'retry', 0))
            self.retry = time.monotonic() + delay
        if log:
            logging.warning('%s: %s; keeping previous data', self.name, error)

    def get(self, loader, ttl, permit=lambda: True, on_error=lambda e: None):
        with self.lock:
            now = time.monotonic()
            if now < self.fresh or now < self.retry or not permit():
                return self.data, self.stamp
            try:
                value = loader()
            except Exception as error:
                self.failures += 1
                delay = min(900, 5 * 2 ** min(self.failures - 1, 8))
                if getattr(error, 'limited', False):
                    delay = max(60, delay, getattr(error, 'retry', 0))
                self.retry = time.monotonic() + delay
                on_error(error)
                logging.warning('%s: %s; keeping previous data', self.name, error)
                return self.data, self.stamp
            if self.failures:
                logging.info('%s recovered', self.name)
            self.data, self.stamp = value, int(time.time())
            self.fresh, self.retry, self.failures = time.monotonic() + ttl, 0, 0
            return self.data, self.stamp


def parse(item, pair):
    if not isinstance(item, dict) or item.get('symbol') != pair:
        raise ValueError('Missing or mismatched pair ' + pair)
    return {'p': number(item['lastPrice'], True), 'c': number(item['priceChangePercent'])}


class MarketData:
    def __init__(self, request):
        self.request = request
        self.slots = {s: Slot(('Binance Spot/' if s in CRYPTO else 'Binance Futures/') + s + 'USDT') for s in ASSETS}
        self.fx = Slot('Coinbase/USD-RUB')
        self.lock, self.attempts, self.pause = threading.Lock(), deque(), 0
        self.flight = threading.Lock()

    def permit(self):
        with self.lock:
            now = time.monotonic()
            while self.attempts and self.attempts[0] <= now - 60:
                self.attempts.popleft()
            if now < self.pause or len(self.attempts) >= 60:
                return False
            self.attempts.append(now)
            return True

    def failed(self, error):
        delay = 0
        if getattr(error, 'limited', False):
            delay = max(60, getattr(error, 'retry', 0))
        if str(error).startswith(('HTTP 403', 'HTTP 451')):
            delay = max(delay, 600)
        if delay:
            with self.lock:
                self.pause = max(self.pause, time.monotonic() + delay)

    def apply(self, results):
        for symbol, value in results.items():
            if isinstance(value, Exception):
                self.slots[symbol].fail(value)
            else:
                self.slots[symbol].store(value, TTL)

    def spot_bulk(self, symbols):
        pairs = [s + 'USDT' for s in symbols]
        url = SPOT_URL + '?' + urlencode({'symbols': json.dumps(pairs, separators=(',', ':'))})
        items = self.request(url)
        if not isinstance(items, list):
            raise ValueError('Unexpected Binance spot response')
        by_pair = {}
        for item in items:
            if isinstance(item, dict) and item.get('symbol') in pairs:
                by_pair.setdefault(item['symbol'], item)
        results = {}
        for symbol in symbols:
            try:
                results[symbol] = parse(by_pair.get(symbol + 'USDT'), symbol + 'USDT')
            except (ValueError, KeyError, TypeError, OverflowError) as error:
                results[symbol] = error
        return results

    def single(self, base, symbol):
        pair = symbol + 'USDT'
        return parse(self.request(base + '?' + urlencode({'symbol': pair})), pair)

    def refresh_crypto(self):
        due = [s for s in CRYPTO if self.slots[s].due()]
        if not due or not self.permit():
            return
        try:
            results = self.spot_bulk(due)
        except Exception as error:
            self.failed(error)
            if getattr(error, 'code', 0) == 400 and len(due) > 1:
                return self.isolate(due)
            logging.warning('Binance Spot bulk request: %s; keeping previous data', error)
            for symbol in due:
                self.slots[symbol].fail(error, log=False)
            return
        self.apply(results)

    def isolate(self, due):
        results = {}
        for symbol in due:
            if not self.permit():
                results[symbol] = RuntimeError('Local request limit or provider pause')
                continue
            try:
                results[symbol] = self.single(SPOT_URL, symbol)
            except Exception as error:
                self.failed(error)
                results[symbol] = error
        self.apply(results)

    def refresh_metals(self):
        due = [s for s in METALS if self.slots[s].due()]
        if not due:
            return

        def one(symbol):
            if not self.permit():
                return symbol, None
            try:
                return symbol, self.single(FUTURES_URL, symbol)
            except Exception as error:
                self.failed(error)
                return symbol, error

        with ThreadPoolExecutor(max_workers=3, thread_name_prefix='metals') as pool:
            results = dict(pool.map(one, due))
        self.apply({s: v for s, v in results.items() if v is not None})

    def cached(self):
        coins = []
        for index, symbol in enumerate(ASSETS, 1):
            data, stamp = self.slots[symbol].snapshot()
            coins.append({'id': index, 'symbol': symbol, 'p': data['p'] if data else None,
                          'c': data['c'] if data else None, 't': stamp})
        return coins, min((c['t'] for c in coins if c['p'] is not None), default=0)

    def coins(self):
        with self.flight:
            self.refresh_crypto()
            self.refresh_metals()
        return self.cached()

    def rate(self):
        def load():
            data = self.request('https://api.coinbase.com/v2/exchange-rates?currency=USD')['data']
            if data['currency'] != 'USD':
                raise ValueError('Wrong FX currency')
            return number(data['rates']['RUB'], True)
        return self.fx.get(load, 60)

    def seed(self, coins, fx=None, ft=0):
        for coin in coins:
            symbol = coin.get('symbol')
            if symbol not in self.slots or coin.get('p') is None:
                continue
            try:
                data = {'p': number(coin['p'], True), 'c': number(coin['c'])}
                self.slots[symbol].seed(data, coin['t'], TTL)
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
        if fx is not None:
            try:
                self.fx.seed(number(fx, True), ft, 60)
            except (ValueError, TypeError, OverflowError):
                pass
