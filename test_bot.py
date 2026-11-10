import json
import unittest
from unittest.mock import Mock, patch

import bot


class MarketTests(unittest.TestCase):
    @patch('bot.request_json')
    def test_crypto_usd_and_change(self, request):
        request.return_value = {'last': '110', 'open': '100'}
        quote = bot.fetch_quote('BTC')
        self.assertEqual(quote['p'], 110)
        self.assertEqual(quote['c'], 10)
        self.assertFalse(quote['e'])
        self.assertIn('BTC-USD/stats', request.call_args.args[0])

    @patch('bot.request_json')
    def test_fx_direction_and_no_fake_change(self, request):
        request.return_value = {'data': {'currency': 'USD', 'rates': {'RUB': '97.45'}}}
        quote = bot.fetch_quote('USD')
        self.assertEqual(quote['p'], 97.45)
        self.assertIsNone(quote['c'])

    def test_bad_prices(self):
        for value in ('NaN', 'inf', '-1', '0'):
            with self.assertRaises(ValueError):
                bot.positive(value)

    @patch('bot.fetch_quote')
    def test_partial_failure_retains_old_value(self, fetch):
        market = bot.Market()
        market.state['q']['BTC'] = {'p': 100, 'c': 1, 't': 10, 'e': False}
        def result(symbol):
            if symbol == 'BTC':
                raise bot.APIError('offline')
            return {'p': 50, 'c': None, 't': 20, 'e': False}
        fetch.side_effect = result
        before = market.state['r']
        market.refresh()
        state = json.loads(market.snapshot())
        self.assertGreater(state['r'], before)
        self.assertEqual(state['q']['BTC']['p'], 100)
        self.assertEqual(state['q']['BTC']['t'], 10)
        self.assertTrue(state['q']['BTC']['e'])
        self.assertFalse(state['q']['ETH']['e'])
        self.assertLessEqual(len(market.snapshot().encode()), 1024)

    def test_oversized_state(self):
        with self.assertRaises(ValueError):
            bot.encode_state({'x': 'a' * 1025})

    def test_private_and_user_filter(self):
        market, widgets = Mock(), Mock()
        bot.handle_message('token', {'chat': {'type': 'group'}}, market, widgets, 0)
        bot.handle_message('token', {'chat': {'type': 'private'}, 'from': {'id': 2}}, market, widgets, 1)
        widgets.send.assert_not_called()

    def test_command_sends_current_state(self):
        market, widgets = Mock(), Mock()
        market.snapshot.return_value = '{"r":1,"q":{}}'
        bot.handle_message('token', {'chat': {'type': 'private', 'id': 7},
            'from': {'id': 7}, 'text': '/widget'}, market, widgets, 7)
        widgets.send.assert_called_once_with(7, market.snapshot.return_value)


if __name__ == '__main__':
    unittest.main()
