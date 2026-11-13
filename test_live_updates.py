import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import bot
from live_updates import LiveController


def app():
    return SimpleNamespace(LOCK=threading.RLock(), SAVED={'widget_id':'w'},
        pack=Mock(return_value='{"r":1,"q":[]}'), api=Mock(), persist=Mock(),
        refresh_on_demand=Mock(), send_widget=Mock())


def query(**extra):
    result={'id':'q','widget_id':'w','data':'refresh','from':{'id':7},'chat':{'type':'private'}}
    result.update(extra)
    return result


class LiveTests(unittest.TestCase):
    def test_answer_precedes_scheduling(self):
        a=app(); live=LiveController(a); calls=[]
        a.api.side_effect=lambda *args,**kwargs:calls.append('answer')
        live.request_refresh=Mock(side_effect=lambda:calls.append('schedule'))
        live.handle_query(query())
        self.assertEqual(calls,['answer','schedule'])
        a.refresh_on_demand.assert_not_called()
        a.api.assert_called_once_with('answerWidgetQuery',widget_query_id='q',data=a.pack.return_value)

    def test_invalid_requests_answered_without_refresh(self):
        for q,allowed,shared in [(query(data='bad'),0,False),(query(widget_id='other'),0,False),
             (query(chat={'type':'group'}),0,False),(query(),8,False),(query(),0,True)]:
            a=app(); live=LiveController(a); live.handle_query(q,allowed,shared)
            a.api.assert_called_once_with('answerWidgetQuery',widget_query_id='q')
            self.assertFalse(live.refresh_requested)

    def test_query_not_blocked_by_slow_refresh(self):
        a=app(); entered=threading.Event(); release=threading.Event()
        def slow():
            entered.set(); release.wait(2)
        a.refresh_on_demand.side_effect=slow
        try:
            with LiveController(a) as live:
                live.request_refresh(); self.assertTrue(entered.wait(1))
                live.handle_query(query())
                self.assertTrue(any(c.args[0]=='answerWidgetQuery' for c in a.api.call_args_list))
                release.set()
        finally:
            release.set()

    def test_idle_worker_does_not_fetch(self):
        a=app()
        with LiveController(a):
            pass
        a.refresh_on_demand.assert_not_called(); a.send_widget.assert_not_called()

    def test_refresh_signals_coalesce(self):
        a=app(); live=LiveController(a)
        for _ in range(100):live.request_refresh()
        self.assertTrue(live.refresh_requested)
        self.assertEqual(len(live.commands),0)

    def test_unchanged_state_not_republished(self):
        a=app(); live=LiveController(a)
        live.publish(); live.publish()
        self.assertEqual(sum(c.args[0]=='editSharedWidgetData' for c in a.api.call_args_list),1)

    def test_command_queue_is_bounded(self):
        a=app(); live=LiveController(a)
        self.assertTrue(all(live.enqueue_send(i) for i in range(32)))
        self.assertFalse(live.enqueue_send(33))

    def test_answer_failure_does_not_schedule(self):
        a=app(); a.api.side_effect=RuntimeError('network'); live=LiveController(a)
        live.handle_query(query())
        self.assertFalse(live.refresh_requested)

    def test_send_enables_queries(self):
        with patch.dict(bot.SAVED,{},clear=True), patch('bot.refresh_on_demand'), patch('bot.persist'), patch('bot.pack',return_value='{"r":1,"q":[]}'), patch('bot.Path.read_text',return_value='<b>widget</b>'), patch('bot.api',return_value={'widget':{'id':'w'}}) as api:
            bot.send_widget(7)
        call=next(c for c in api.call_args_list if c.args[0]=='sendWidget')
        self.assertTrue(call.kwargs['allow_queries'])
        self.assertTrue(call.kwargs['is_shared'])

    def test_answer_uses_short_timeout(self):
        with patch.object(bot,'TG_UNTIL',0), patch('bot.json_request',return_value={'ok':True,'result':True}) as request:
            bot.api('answerWidgetQuery',widget_query_id='q')
        self.assertEqual(request.call_args.kwargs['timeout'],5)

    def test_previous_widget_id_not_reused(self):
        with patch.object(bot,'TOKEN','test:secret'), patch.dict(bot.SAVED,{},clear=True), patch.dict(bot.STATE,{},clear=True):
            bot.restore({'bot':'test','version':5,'widget_id':'old','state':{'coins':[]}})
            self.assertNotIn('widget_id',bot.SAVED)


if __name__=='__main__':
    unittest.main()
