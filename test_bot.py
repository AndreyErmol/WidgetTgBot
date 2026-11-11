import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse
import bot
from market_data import ASSETS, SOURCE, MarketData, Slot


def response(url):
    if 'coinbase.com' in url:
        return {'data':{'currency':'USD','rates':{'RUB':'83.17'}}}
    pair=parse_qs(urlparse(url).query)['symbol'][0]
    return {'retCode':0,'result':{'category':'spot','list':[{'symbol':pair,'lastPrice':'1.25','price24hPcnt':'-0.0219'}]}}


class Tests(unittest.TestCase):
    def setUp(self):
        self.request=Mock(side_effect=response)
        self.market=MarketData(self.request)

    def test_exact_pairs_and_order(self):
        coins,stamp=self.market.coins()
        self.assertEqual([c['symbol'] for c in coins],['BTC','ETH','BNB','XRP','SUI','SOL','TRX','HYPE','GRAM','DOGE'])
        self.assertEqual(self.request.call_count,10)
        pairs=[]
        for call in self.request.call_args_list:
            url=call.args[0]; q=parse_qs(urlparse(url).query)
            self.assertEqual(urlparse(url).netloc,'api.bybit.com')
            self.assertEqual(q['category'],['spot']); pairs+=q['symbol']
        self.assertEqual(set(pairs),{s+'USDT' for s,_ in ASSETS})
        self.assertTrue(stamp); self.assertAlmostEqual(coins[0]['c'],-2.19)

    def test_cache_shared_by_callers(self):
        first=self.market.coins();second=self.market.coins()
        self.assertEqual(first,second);self.assertEqual(self.request.call_count,10)

    def test_concurrent_singleflight(self):
        with ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(lambda _:self.market.coins(),range(3)))
        self.assertEqual(self.request.call_count,10)

    def test_partial_failure_keeps_timestamp(self):
        first,_=self.market.coins()
        for slot in self.market.slots.values():slot.fresh=0
        def flaky(url):
            if 'symbol=BTCUSDT' in url:raise bot.APIError('offline')
            return response(url)
        self.request.side_effect=flaky
        second,_=self.market.coins()
        self.assertEqual(first[0],second[0])
        self.assertEqual(second[-1]['p'],1.25)

    def test_missing_pair_is_not_futures_or_zero(self):
        self.request.return_value=None
        self.request.side_effect=lambda url:{'retCode':0,'result':{'category':'spot','list':[]}}
        coins,stamp=self.market.coins()
        self.assertIsNone(coins[0]['p']);self.assertEqual(stamp,0)
        self.assertEqual(self.request.call_count,10)

    def test_wrong_pair_and_nonfinite_rejected(self):
        for field,value in [('symbol','BTCUSD'),('lastPrice','NaN'),('lastPrice','0'),('price24hPcnt','inf')]:
            def bad(url):
                result=response(url);result['result']['list'][0][field]=value;return result
            market=MarketData(bad)
            self.assertIsNone(market.quote(ASSETS[0])[0])

    def test_error_cooldown_silent(self):
        slot=Slot('test');loader=Mock(side_effect=bot.APIError('429',True,120))
        with patch('market_data.logging.warning') as warning:
            self.assertEqual(slot.get(loader,10),(None,0))
            for _ in range(10):slot.get(loader,10)
        self.assertEqual(loader.call_count,1);self.assertEqual(warning.call_count,1)

    def test_global_rate_limit_and_pause(self):
        self.assertTrue(all(self.market.permit() for _ in range(60)))
        self.assertFalse(self.market.permit())
        market=MarketData(self.request);market.failed(bot.APIError('429',True,120))
        self.assertFalse(market.permit())
        market=MarketData(self.request);market.failed(bot.APIError('HTTP 403'))
        self.assertFalse(market.permit())

    def test_bybit_business_limit(self):
        self.request.side_effect=lambda url:{'retCode':10006,'retMsg':'too many'}
        self.assertIsNone(self.market.quote(ASSETS[0])[0]);self.assertFalse(self.market.permit())

    def test_fx_independent_cache(self):
        self.assertEqual(self.market.rate()[0],83.17)
        self.market.rate();self.assertEqual(self.request.call_count,1)
        self.assertEqual(self.request.call_args.args[0],'https://api.coinbase.com/v2/exchange-rates?currency=USD')

    def test_recent_no_fetch(self):
        with patch.dict(bot.STATE,coins=[{}],ct=100,fx=83.17,ft=100),patch('bot.time.time',return_value=105),patch('bot.fetch_coins') as fetch,patch.object(bot.MARKET,'rate') as rate:
            bot.refresh_on_demand()
        fetch.assert_not_called();rate.assert_not_called()

    def test_restored_bybit_data_does_not_refetch_when_fresh(self):
        coins,stamp=self.market.coins()
        market=MarketData(self.request);market.seed(coins,83.17,stamp)
        self.request.reset_mock();market.coins();market.rate();self.request.assert_not_called()

    def test_legacy_dollar_prices_never_restored_as_usdt(self):
        with patch.object(bot,'MARKET',self.market),patch.object(bot,'TOKEN','1:secret'),patch.dict(bot.STATE,{},clear=True),patch.dict(bot.SAVED,{},clear=True):
            bot.restore({'bot':'1','version':6,'widget_id':'old','state':{'coins':[{'symbol':'BTC','p':100,'c':1,'t':int(time.time())}],'fx':83.17,'ft':int(time.time())}})
            self.assertTrue(all(c['p'] is None for c in bot.STATE['coins']))
            self.assertEqual(bot.STATE['fx'],83.17);self.assertNotIn('widget_id',bot.SAVED)
        self.request.assert_not_called()

    def test_new_runtime_restores_source(self):
        coins,stamp=self.market.coins()
        fresh=MarketData(self.request)
        with patch.object(bot,'MARKET',fresh),patch.object(bot,'TOKEN','1:secret'),patch.dict(bot.STATE,{},clear=True),patch.dict(bot.SAVED,{},clear=True):
            bot.restore({'bot':'1','version':7,'quote_source':SOURCE,'widget_id':'new','state':{'coins':coins,'fx':83.17,'ft':stamp}})
            self.assertEqual(bot.STATE['coins'][0]['p'],1.25);self.assertEqual(bot.SAVED['widget_id'],'new')

    def test_format_and_payload_size_with_missing_data(self):
        self.assertEqual(bot.format_percent(-2.19),'-2,19%')
        self.assertEqual(bot.format_price(73057.95),'73 057,95')
        coins,stamp=self.market.coins();coins[-1].update(p=None,c=None,t=0)
        with patch.dict(bot.STATE,coins=coins,ct=stamp,fx=83.17,ft=stamp):state=bot.pack()
        self.assertLessEqual(len(state.encode()),1024)
        self.assertEqual(json.loads(state)['q'][0][5],'-2,19%')
        self.assertIsNone(json.loads(state)['q'][-1][4]);self.assertEqual(json.loads(state)['ft'],stamp)

    def test_idle_and_restore_do_not_request(self):
        fresh=MarketData(self.request);fresh.seed([]);fresh.cached()
        self.request.assert_not_called()
        self.assertFalse(hasattr(bot,'source_loop'))


if __name__=='__main__':unittest.main()
