import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse
import bot


def coins():
    return [{'id': i+1, 'symbol': symbol, 'slug': slug, 'name': symbol,
             'rank': i+1, 'p': 1.25, 'c': -2.19} for i,(symbol,slug) in enumerate(bot.ASSETS)]


def response():
    return {'data': [dict(c, cmc_rank=c['rank'], quote={'USD': {'price': c['p'],
                     'percent_change_24h': c['c']}}) for c in coins()]}


class Tests(unittest.TestCase):
    def test_exact_order(self):
        result=bot.order_coins(list(reversed(coins())))
        self.assertEqual([c['symbol'] for c in result],['BTC','ETH','BNB','XRP','SUI','SOL','TRX','HYPE','ZEC','DOGE'])

    def test_missing_asset_rejected(self):
        with self.assertRaises(ValueError):
            bot.order_coins(coins()[:-1])

    def test_wrong_symbol_rejected(self):
        data=coins(); data[0]['symbol']='OTHER'
        with self.assertRaises(ValueError):
            bot.order_coins(data)

    def test_one_quotes_request_for_ten(self):
        with patch.object(bot,'CMC_LIMIT',bot.RateLimiter()), patch.object(bot,'CMC_CACHE',bot.Cache()), patch('bot.json_request',return_value=response()) as request:
            first=bot.fetch_coins(); second=bot.fetch_coins()
        self.assertEqual(request.call_count,1)
        self.assertEqual(first,second)
        url=request.call_args.args[0]
        self.assertIn('/v3/cryptocurrency/quotes/latest?',url)
        query=parse_qs(urlparse(url).query)
        self.assertEqual(query['slug'][0].split(','),[slug for _,slug in bot.ASSETS])
        self.assertNotIn('listings',url)

    def test_limit_boundary(self):
        now=[0.0]; limiter=bot.RateLimiter(clock=lambda:now[0])
        self.assertTrue(all(limiter.reserve() for _ in range(50)))
        self.assertFalse(limiter.reserve())
        now[0]=59.999; self.assertFalse(limiter.reserve())
        now[0]=60; self.assertTrue(limiter.reserve())

    def test_concurrent_limit(self):
        limiter=bot.RateLimiter(clock=lambda:0)
        with ThreadPoolExecutor(max_workers=20) as pool:
            results=list(pool.map(lambda _:limiter.reserve(),range(200)))
        self.assertEqual(sum(results),50)

    def test_failure_consumes_slot(self):
        limiter=bot.RateLimiter(limit=1)
        with patch.object(bot,'CMC_LIMIT',limiter), patch.object(bot,'CMC_CACHE',bot.Cache()), patch('bot.json_request',side_effect=bot.APIError('offline')):
            with self.assertRaises(bot.APIError): bot.fetch_coins()
        self.assertFalse(limiter.reserve())

    def test_cooldown_keeps_timestamp(self):
        cache=bot.Cache(); loader=Mock(return_value=coins())
        old=cache.get('q',loader,ttl=0)
        loader.side_effect=bot.APIError('429',True,120)
        with self.assertRaises(bot.APIError): cache.get('q',loader)
        self.assertEqual(cache.get('q',loader),old)
        self.assertEqual(loader.call_count,2)

    def test_recent_no_fetch(self):
        with patch.dict(bot.STATE,coins=coins(),ct=100,fx=83.17,ft=100), patch('bot.time.time',return_value=105), patch('bot.fetch_coins') as fetch, patch('bot.json_request') as request:
            bot.refresh_on_demand()
        fetch.assert_not_called(); request.assert_not_called()

    def test_stale_refresh(self):
        with patch.dict(bot.STATE,coins=coins(),ct=10,fx=83.17,ft=100), patch('bot.time.time',return_value=100), patch('bot.fetch_coins',return_value=(coins(),100)) as fetch:
            bot.refresh_on_demand(); self.assertEqual(bot.STATE['ct'],100)
        fetch.assert_called_once()

    def test_error_fallback(self):
        with patch.dict(bot.STATE,coins=coins(),ct=10,fx=83.17,ft=100), patch('bot.time.time',return_value=100), patch('bot.fetch_coins',side_effect=bot.APIError('429',True,120)):
            before=bot.STATE['coins']; bot.refresh_on_demand()
            self.assertEqual(bot.STATE['coins'],before); self.assertEqual(bot.STATE['ct'],10)

    def test_twenty_commands_use_cache(self):
        with patch.dict(bot.STATE,coins=coins(),ct=100,fx=83.17,ft=100), patch.dict(bot.SAVED,{},clear=True), patch('bot.time.time',return_value=105), patch('bot.fetch_coins') as fetch, patch('bot.json_request') as request, patch('bot.Path.read_text',return_value='<b>widget</b>'), patch('bot.persist'), patch('bot.api',return_value={'widget':{'id':'test'}}) as api:
            for i in range(20): bot.send_widget(i)
        fetch.assert_not_called(); request.assert_not_called()
        self.assertEqual(sum(c.args[0]=='sendWidget' for c in api.call_args_list),1)
        self.assertEqual(sum(c.args[0]=='sendSharedWidget' for c in api.call_args_list),19)

    def test_restore_reorders_fixed_watchlist(self):
        with patch.object(bot,'TOKEN','test:secret'), patch.dict(bot.STATE,{},clear=True), patch.dict(bot.SAVED,{},clear=True):
            bot.restore({'bot':'test','version':4,'state':{'coins':list(reversed(coins())),'ct':100}})
            self.assertEqual([c['symbol'] for c in bot.STATE['coins']],[s for s,_ in bot.ASSETS])

    def test_format_and_size(self):
        self.assertEqual(bot.format_percent(-2.19),'-2,19%')
        self.assertEqual(bot.format_price(73057.95),'73 057,95')
        with patch.dict(bot.STATE,coins=coins(),fx=83.17):
            state=bot.pack()
        self.assertLessEqual(len(state.encode()),1024)
        self.assertEqual(json.loads(state)['q'][0][5],'-2,19%')

    def test_no_background_worker(self):
        self.assertFalse(hasattr(bot,'source_loop'))


if __name__=='__main__':
    unittest.main()
