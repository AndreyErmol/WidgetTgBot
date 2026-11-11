import logging
import math
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

SOURCE = 'bybit_spot_usdt'
ASSETS = (('BTC','Bitcoin'),('ETH','Ethereum'),('BNB','BNB'),('XRP','XRP'),
          ('SUI','Sui'),('SOL','Solana'),('TRX','TRON'),('HYPE','Hyperliquid'),
          ('ZEC','Zcash'),('DOGE','Dogecoin'))


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
        if stamp <= 0 or stamp > time.time()+60:
            return
        with self.lock:
            self.data, self.stamp = data, stamp
            self.fresh = time.monotonic()+max(0,ttl-max(0,time.time()-stamp))

    def get(self, loader, ttl, permit=lambda:True, on_error=lambda e:None):
        with self.lock:
            now = time.monotonic()
            if now < self.fresh or now < self.retry or not permit():
                return self.data, self.stamp
            try:
                value = loader()
            except Exception as error:
                self.failures += 1
                delay = min(900,5*2**min(self.failures-1,8))
                if getattr(error,'limited',False):
                    delay = max(60,delay,getattr(error,'retry',0))
                self.retry = time.monotonic()+delay
                on_error(error)
                logging.warning('%s: %s; keeping previous data',self.name,error)
                return self.data,self.stamp
            if self.failures:
                logging.info('%s recovered',self.name)
            self.data,self.stamp = value,int(time.time())
            self.fresh,self.retry,self.failures = time.monotonic()+ttl,0,0
            return self.data,self.stamp


class MarketData:
    def __init__(self, request):
        self.request = request
        self.slots = {s:Slot('Bybit/'+s+'USDT') for s,_ in ASSETS}
        self.fx = Slot('Coinbase/USD-RUB')
        self.lock,self.attempts,self.pause = threading.Lock(),deque(),0

    def permit(self):
        with self.lock:
            now = time.monotonic()
            while self.attempts and self.attempts[0] <= now-60:
                self.attempts.popleft()
            if now < self.pause or len(self.attempts) >= 60:
                return False
            self.attempts.append(now)
            return True

    def failed(self, error):
        delay = 0
        if getattr(error,'limited',False):
            delay = max(60,getattr(error,'retry',0))
        if str(error).startswith('HTTP 403'):
            delay = max(delay,600)
        if delay:
            with self.lock:
                self.pause = max(self.pause,time.monotonic()+delay)

    def quote(self, asset):
        symbol,name = asset
        pair = symbol+'USDT'
        def load():
            params = urlencode({'category':'spot','symbol':pair})
            result = self.request('https://api.bybit.com/v5/market/tickers?'+params)
            code = int(result.get('retCode',-1))
            if code:
                error = ValueError('Bybit retCode='+str(code)+': '+str(result.get('retMsg',''))[:160])
                error.limited = code in (10006,10429,10009)
                error.retry = 600 if code == 10009 else 60
                raise error
            data = result['result']
            items = data['list']
            if data.get('category') != 'spot' or len(items) != 1 or items[0].get('symbol') != pair:
                raise ValueError('Missing or mismatched spot pair '+pair)
            item = items[0]
            return {'p':number(item['lastPrice'],True),'c':number(number(item['price24hPcnt'])*100)}
        return self.slots[symbol].get(load,10,self.permit,self.failed)

    def cached(self):
        coins = []
        for index,(symbol,name) in enumerate(ASSETS,1):
            slot = self.slots[symbol]
            with slot.lock:
                data,stamp = slot.data,slot.stamp
                coins.append({'id':index,'rank':index,'name':name,'symbol':symbol,
                              'p':data['p'] if data else None,'c':data['c'] if data else None,'t':stamp})
        return coins,min((c['t'] for c in coins if c['p'] is not None),default=0)

    def coins(self):
        with ThreadPoolExecutor(max_workers=3,thread_name_prefix='bybit') as pool:
            list(pool.map(self.quote,ASSETS))
        return self.cached()

    def rate(self):
        def load():
            data = self.request('https://api.coinbase.com/v2/exchange-rates?currency=USD')['data']
            if data['currency'] != 'USD':
                raise ValueError('Wrong FX currency')
            return number(data['rates']['RUB'],True)
        return self.fx.get(load,60)

    def seed(self, coins, fx=None, ft=0):
        for coin in coins:
            symbol = coin.get('symbol')
            if symbol not in self.slots or coin.get('p') is None:
                continue
            try:
                data = {'p':number(coin['p'],True),'c':number(coin['c'])}
                self.slots[symbol].seed(data,coin['t'],10)
            except (ValueError,TypeError,KeyError,OverflowError):
                continue
        if fx is not None:
            try:
                self.fx.seed(number(fx,True),ft,60)
            except (ValueError,TypeError,OverflowError):
                pass
