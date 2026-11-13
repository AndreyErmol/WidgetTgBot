import logging
import threading
from collections import deque


class LiveController:
    def __init__(self, app):
        self.app = app
        self.condition = threading.Condition()
        self.commands = deque()
        self.refresh_requested = False
        self.stopped = False
        self.thread = threading.Thread(target=self._run, name='widget-demand-worker', daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        with self.condition:
            self.stopped = True
            self.commands.clear()
            self.refresh_requested = False
            self.condition.notify_all()
        self.thread.join(timeout=12)

    def enqueue_send(self, chat_id):
        with self.condition:
            if self.stopped or len(self.commands) >= 32:
                logging.warning('Widget command queue full or stopped')
                return False
            self.commands.append(chat_id)
            self.condition.notify()
            return True

    def request_refresh(self):
        with self.condition:
            if not self.stopped:
                self.refresh_requested = True
                self.condition.notify()

    def handle_query(self, query, allowed_user=0, shared=False):
        valid = (not shared and query.get('chat', {}).get('type') == 'private'
                 and query.get('data') == 'refresh'
                 and (not allowed_user or query.get('from', {}).get('id') == allowed_user))
        params = {'widget_query_id': query['id']}
        if valid:
            params['data'] = self.app.pack()
        try:
            # No source calls, queue waits or SEND_LOCK before answering.
            self.app.api('answerWidgetQuery', **params)
        except Exception as error:
            logging.warning('answerWidgetQuery failed: %s', error)
            return
        if valid:
            self.request_refresh()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopped or self.commands or self.refresh_requested)
                if self.stopped:
                    return
                chat_id = self.commands.popleft() if self.commands else None
                self.refresh_requested = False
            try:
                if chat_id is not None:
                    self.app.send_widget(chat_id)
                else:
                    self.app.refresh_on_demand()
                    self.app.persist()
            except Exception as error:
                logging.warning('Demand worker: %s', error)
