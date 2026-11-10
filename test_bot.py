import json
import unittest
from unittest.mock import patch
import bot


def item(i, rank=None):
    return {'id': i, 'rank': rank or i, 'name': 'Coin', 'symbol': 'X', 'p': 1.25, 'c': -2.0}


class Tests(unittest.TestCase):
    def test_sui_outside_top(self):
        result = bot.select_coins([item(i) for i in range(1, 11)], item(99, 25))
        self.assertEqual([c['id'] for c in result], [1, 2, 3, 4, 99, 5, 6, 7, 8, 9])
        self.assertEqual(result[4]['rank'], 25)

    def test_sui_inside_top(self):
        result = bot.select_coins([item(i) for i in range(1, 11)], item(3))
        self.assertEqual(len({c['id'] for c in result}), 10)
        self.assertEqual(result[4]['id'], 3)
        self.assertEqual([c['id'] for c in result if c['id'] != 3], [1,2,4,5,6,7,8,9,10])

    def test_state_size(self):
        state = bot.pack([item(i) for i in range(10)], 83.17, 1800000000, 1800000000)
        self.assertLessEqual(len(state.encode()), 1024)
        self.assertEqual(len(json.loads(state)['q']), 10)

    def test_v3_quote_list(self):
        raw = {'id': 1, 'name': 'Bitcoin', 'symbol': 'BTC', 'cmc_rank': 1,
               'quote': [{'symbol': 'USD', 'price': 100, 'percent_change_24h': -2}]}
        self.assertEqual(bot.coin(raw)['p'], 100)
        raw['quote'] = {'USD': raw['quote'][0]}
        self.assertEqual(bot.coin(raw)['c'], -2)

    def test_bad_prices(self):
        for value in ('nan', 'inf', 0, -2):
            with self.assertRaises(ValueError):
                bot.finite(value, True)

    @patch('bot.http')
    def test_cmc_error(self, http):
        http.return_value = b'{"status":{"error_code":429,"error_message":"Rate limited"}}'
        with self.assertRaises(bot.APIError):
            bot.cmc('v3/cryptocurrency/listings/latest')


if __name__ == '__main__':
    unittest.main()
