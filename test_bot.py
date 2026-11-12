import io
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
import bot
from market_data import ASSETS, CRYPTO, METALS, SOURCE, MarketData, Slot

PAIRS = [s + 'USDT' for s in CRYPTO]


def item(pair, price='1.25', percent='-2.19'):
    return {'symbol': pair, 'lastPrice': price, 'priceChangePercent': percent}


def response(url):
    if 'coinbase.com' in url:
        return {'data': {'currency': 'USD', 'rates': {'RUB': '83.17'}}}
    query = parse_qs(urlparse(url).query)
    if 'symbols' in query:
        return [item(pair) for pair in json.loads(query['symbols'][0])]
    return item(query['symbol'][0])


class Tests(unittest.TestCase):
    def setUp(self):
        self.request = Mock(side_effect=response)
        self.market = MarketData(self.request)

    def urls(self):
        return [call.args[0] for call in self.request.call_args_list]

    def test_order_and_endpoints(self):
        coins, stamp = self.market.coins()
        self.assertEqual([c['symbol'] for c in coins],
                         ['BTC', 'ETH', 'BNB', 'XRP', 'SUI', 'SOL', 'TRX', 'HYPE', 'GRAM', 'DOGE', 'XAU', 'XPD', 'XAG'])
        self.assertEqual([c['id'] for c in coins], list(range(1, 14)))
        urls = self.urls()
        self.assertEqual(len(urls), 4)
        spot = urlparse(urls[0])
        self.assertEqual((spot.netloc, spot.path), ('api.binance.com', '/api/v3/ticker/24hr'))
        self.assertEqual(json.loads(parse_qs(spot.query)['symbols'][0]), PAIRS)
        futures = [urlparse(u) for u in urls[1:]]
        self.assertTrue(all((u.netloc, u.path) == ('fapi.binance.com', '/fapi/v1/ticker/24hr') for u in futures))
        self.assertEqual(sorted(parse_qs(u.query)['symbol'][0] for u in futures), ['XAGUSDT', 'XAUUSDT', 'XPDUSDT'])
        self.assertTrue(stamp)
        self.assertAlmostEqual(coins[0]['c'], -2.19)

    def test_cache_and_singleflight(self):
        first = self.market.coins()
        self.assertEqual(first, self.market.coins())
        self.assertEqual(self.request.call_count, 4)
        fresh = MarketData(self.request)
        self.request.reset_mock()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: fresh.coins(), range(4)))
        self.assertEqual(self.request.call_count, 4)

    def test_metal_failure_does_not_touch_crypto_and_keeps_old_price(self):
        first, _ = self.market.coins()
        for slot in self.market.slots.values():
            slot.fresh = 0

        def flaky(url):
            if 'symbol=XAUUSDT' in url:
                raise bot.APIError('offline')
            return response(url)
        self.request.side_effect = flaky
        second, _ = self.market.coins()
        self.assertEqual(first[10], second[10])
        self.assertEqual(second[0]['p'], 1.25)

    def test_missing_pair_in_bulk_only_hits_that_coin(self):
        self.request.side_effect = lambda url: ([item(p) for p in PAIRS if p != 'HYPEUSDT']
                                                if 'symbols' in url else response(url))
        coins, _ = self.market.coins()
        self.assertIsNone(coins[7]['p'])
        self.assertEqual(sum(c['p'] is not None for c in coins), 12)

    def test_bad_request_isolates_bad_symbol(self):
        def reject(url):
            query = parse_qs(urlparse(url).query)
            if 'symbols' in query:
                raise bot.APIError('HTTP 400: Invalid symbol.', code=400)
            if query.get('symbol') == ['GRAMUSDT']:
                raise bot.APIError('HTTP 400: Invalid symbol.', code=400)
            return response(url)
        self.request.side_effect = reject
        coins, _ = self.market.coins()
        self.assertIsNone(coins[8]['p'])
        self.assertEqual(sum(c['p'] is not None for c in coins), 12)
        self.assertEqual(self.request.call_count, 1 + 10 + 3)

    def test_total_failure_backs_off_without_hammering(self):
        self.request.side_effect = bot.APIError('HTTP 500', code=500)
        with patch('market_data.logging.warning') as warning:
            for _ in range(5):
                self.market.coins()
        self.assertEqual(self.request.call_count, 4)
        self.assertEqual(warning.call_count, 4)

    def test_invalid_values_rejected(self):
        for field, value in [('symbol', 'BTCUSD'), ('lastPrice', 'NaN'), ('lastPrice', '0'), ('priceChangePercent', 'inf')]:
            def bad(url, field=field, value=value):
                result = response(url)
                target = result[0] if isinstance(result, list) else result
                target[field] = value
                return result
            market = MarketData(bad)
            coins, _ = market.coins()
            self.assertTrue(all(c['p'] is None for c in coins[:1] + coins[10:11]), (field, value))

    def test_cooldown_logging_slot(self):
        slot = Slot('test')
        loader = Mock(side_effect=bot.APIError('429', True, 120))
        with patch('market_data.logging.warning') as warning:
            self.assertEqual(slot.get(loader, 10), (None, 0))
            for _ in range(10):
                slot.get(loader, 10)
        self.assertEqual((loader.call_count, warning.call_count), (1, 1))

    def test_limits_and_provider_pauses(self):
        self.assertTrue(all(self.market.permit() for _ in range(60)))
        self.assertFalse(self.market.permit())
        for error in (bot.APIError('429', True, 120), bot.APIError('HTTP 418', True, 300),
                      bot.APIError('HTTP 403'), bot.APIError('HTTP 451: restricted location')):
            market = MarketData(self.request)
            market.failed(error)
            self.assertFalse(market.permit(), str(error))

    def test_pause_stops_requests(self):
        self.request.side_effect = bot.APIError('HTTP 429', True, 120, 429)
        self.market.coins()
        count = self.request.call_count
        for slot in self.market.slots.values():
            slot.retry = 0
        self.market.coins()
        self.assertEqual(self.request.call_count, count)

    def test_fx_independent_cache(self):
        self.assertEqual(self.market.rate()[0], 83.17)
        self.market.rate()
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.request.call_args.args[0], 'https://api.coinbase.com/v2/exchange-rates?currency=USD')

    def test_recent_no_fetch(self):
        with patch.dict(bot.STATE, coins=[{}], ct=100, fx=83.17, ft=100), patch('bot.time.time', return_value=105), \
                patch('bot.fetch_coins') as fetch, patch.object(bot.MARKET, 'rate') as rate:
            bot.refresh_on_demand()
        fetch.assert_not_called()
        rate.assert_not_called()

    def test_restored_data_does_not_refetch_when_fresh(self):
        coins, stamp = self.market.coins()
        market = MarketData(self.request)
        market.seed(coins, 83.17, stamp)
        self.request.reset_mock()
        market.coins()
        market.rate()
        self.request.assert_not_called()

    def test_previous_providers_never_restored(self):
        for version, source in [(6, None), (7, 'bybit_spot_usdt')]:
            fresh = MarketData(self.request)
            saved = {'bot': '1', 'version': version, 'quote_source': source, 'widget_id': 'old',
                     'state': {'coins': [{'symbol': 'BTC', 'p': 100, 'c': 1, 't': int(time.time())}],
                               'fx': 83.17, 'ft': int(time.time())}}
            with patch.object(bot, 'MARKET', fresh), patch.object(bot, 'TOKEN', '1:secret'), \
                    patch.dict(bot.STATE, {}, clear=True), patch.dict(bot.SAVED, {}, clear=True):
                bot.restore(saved)
                self.assertTrue(all(c['p'] is None for c in bot.STATE['coins']))
                self.assertEqual(bot.STATE['fx'], 83.17)
                self.assertNotIn('widget_id', bot.SAVED)
        self.request.assert_not_called()

    def test_new_runtime_restores(self):
        coins, stamp = self.market.coins()
        fresh = MarketData(self.request)
        with patch.object(bot, 'MARKET', fresh), patch.object(bot, 'TOKEN', '1:secret'), \
                patch.dict(bot.STATE, {}, clear=True), patch.dict(bot.SAVED, {}, clear=True):
            bot.restore({'bot': '1', 'version': 8, 'quote_source': SOURCE, 'widget_id': 'new',
                         'state': {'coins': coins, 'fx': 83.17, 'ft': stamp}})
            self.assertEqual(bot.STATE['coins'][12]['p'], 1.25)
            self.assertEqual(bot.SAVED['widget_id'], 'new')

    def test_format_and_payload_for_thirteen_rows(self):
        self.assertEqual(bot.format_percent(-2.19), '-2,19%')
        self.assertEqual(bot.format_price(73057.95), '73 057,95')
        coins, stamp = self.market.coins()
        coins[0].update(p=123456.789, c=-12.345)
        coins[-1].update(p=None, c=None, t=0)
        with patch.dict(bot.STATE, coins=coins, ct=stamp, fx=83.17, ft=stamp):
            state = bot.pack()
        data = json.loads(state)
        self.assertLessEqual(len(state.encode()), 1024)
        self.assertEqual(len(data['q']), 13)
        self.assertEqual(data['q'][0][:5], [1, 'BTC', '123 456,79', '-12,35%', 0])
        self.assertEqual(data['q'][-1][1:5], ['XAG', None, None, None])
        self.assertNotIn('ft', data)

    def test_idle_makes_no_requests(self):
        MarketData(self.request).seed([])
        self.request.assert_not_called()
        self.assertFalse(hasattr(bot, 'source_loop'))
        self.assertEqual(ASSETS, CRYPTO + METALS)


class HttpTests(unittest.TestCase):
    def setUp(self):
        bot.TOKEN, bot.TG_UNTIL = '1:secret', 0

    @staticmethod
    def error(code, body, headers=None):
        message = Message()
        for key, value in (headers or {}).items():
            message[key] = value
        return HTTPError('u', code, 'x', message, io.BytesIO(json.dumps(body).encode()))

    def test_binance_ban_and_message(self):
        with patch('bot.urlopen', side_effect=self.error(418, {'code': -1003, 'msg': 'Way too many requests'}, {'Retry-After': '120'})):
            with self.assertRaises(bot.APIError) as context:
                bot.json_request('https://api.binance.com/x')
        e = context.exception
        self.assertEqual((e.code, e.limited, e.retry), (418, True, 120))
        self.assertIn('Way too many requests', str(e))

    def test_bad_symbol_error_is_400_without_limit(self):
        with patch('bot.urlopen', side_effect=self.error(400, {'code': -1121, 'msg': 'Invalid symbol.'})):
            with self.assertRaises(bot.APIError) as context:
                bot.json_request('https://api.binance.com/x')
        self.assertEqual((context.exception.code, context.exception.limited), (400, False))

    def test_telegram_description_and_token_redaction(self):
        with patch('bot.urlopen', side_effect=self.error(403, {'ok': False, 'description': 'Forbidden: bot was blocked 1:secret'})):
            with self.assertRaises(bot.APIError) as context:
                bot.api('sendWidget')
        self.assertEqual(context.exception.code, 403)
        self.assertNotIn('secret', str(context.exception))
        self.assertIn('blocked', str(context.exception))


if __name__ == '__main__':
    unittest.main()
