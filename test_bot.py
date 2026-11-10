import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
import bot


def item(i, symbol='X', rank=None):
    return {'id': i, 'rank': rank or i, 'name': 'Coin', 'symbol': symbol, 'p': 1.25, 'c': -2.19}


class Tests(unittest.TestCase):
    def test_filter_and_pin(self):
        items = [item(i) for i in range(1, 16)]
        items[1]['symbol'] = 'USDT'
        items[3]['symbol'] = 'USDC'
        result = bot.select_coins(items, item(99, 'SUI', 25))
        self.assertEqual([c['id'] for c in result], [1,3,5,6,99,7,8,9,10,11])
        self.assertEqual(result[4]['rank'], 25)
        self.assertEqual(len({c['id'] for c in result}), 10)

    def test_existing_sui_not_duplicated(self):
        items = [item(i) for i in range(1, 12)]
        self.assertEqual(len({c['id'] for c in bot.select_coins(items, items[2])}), 10)

    def test_percent_format(self):
        self.assertEqual(bot.format_percent(-2.19), '-2,19%')
        self.assertEqual(bot.format_percent(2.34), '+2,34%')
        self.assertEqual(bot.format_percent(-0.001), '+0,00%')
        self.assertEqual(bot.format_percent(1234.567), '+1234,57%')

    def test_price_format(self):
        self.assertEqual(bot.format_price(73057.95), '73 057,95')
        self.assertEqual(bot.format_price(.001234), '0,001234')

    def test_singleflight(self):
        cache = bot.Cache()
        def load():
            time.sleep(.02)
            return {'price': 1}
        loader = Mock(side_effect=load)
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(lambda _: cache.get('quotes', loader), range(40)))
        self.assertEqual(loader.call_count, 1)
        self.assertTrue(all(r == results[0] for r in results))

    def test_429_cooldown_stale_timestamp(self):
        cache = bot.Cache()
        loader = Mock(return_value={'price': 2})
        old = cache.get('q', loader, ttl=0)
        loader.side_effect = bot.APIError('429', True, 120)
        with self.assertRaises(bot.APIError):
            cache.get('q', loader)
        self.assertGreaterEqual(cache.until - time.monotonic(), 119)
        self.assertEqual(cache.get('q', loader), old)
        self.assertEqual(loader.call_count, 2)
        other = Mock()
        with self.assertRaises(bot.APIError):
            cache.get('another-endpoint', other)
        other.assert_not_called()

    def test_state_size_and_percent(self):
        with patch.dict(bot.STATE, coins=[item(i) for i in range(10)], fx=83.17, ct=1800000000):
            state = bot.pack()
        self.assertLessEqual(len(state.encode()), 1024)
        self.assertEqual(json.loads(state)['q'][0][5], '-2,19%')

    def test_coin_percentage_not_scaled(self):
        raw = {'id': 1, 'name': 'Bitcoin', 'symbol': 'BTC', 'cmc_rank': 1,
               'quote': {'USD': {'price': 100, 'percent_change_24h': -2.19}}}
        self.assertEqual(bot.coin(raw)['c'], -2.19)

    @patch('bot.persist')
    @patch('bot.pack', return_value='{}')
    @patch('bot.Path.read_text', return_value='<b>widget</b>')
    @patch('bot.cmc')
    @patch('bot.api', return_value={'widget': {'id': 'test'}})
    def test_many_viewers_no_cmc_calls(self, api, cmc, read, pack, persist):
        with patch.dict(bot.SAVED, {}, clear=True):
            for i in range(20):
                bot.send_widget(i)
        cmc.assert_not_called()
        self.assertEqual(sum(c.args[0] == 'sendWidget' for c in api.call_args_list), 1)
        self.assertEqual(sum(c.args[0] == 'sendSharedWidget' for c in api.call_args_list), 19)


if __name__ == '__main__':
    unittest.main()
