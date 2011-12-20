import unittest
import threading
from functools import partial
import itertools
import uuid
import logging

from kombu.pools import connections

import dashi
import dashi.util
from dashi.tests.util import who_is_calling

log = logging.getLogger(__name__)

_NO_REPLY = object()

class TestReceiver(object):

    consume_timeout = 5

    def __init__(self, **kwargs):

        if 'name' in kwargs:
            self.name = kwargs['name']
        else:
            self.name = who_is_calling() + "." + uuid.uuid4().hex
        kwargs['name'] = self.name

        self.conn = dashi.DashiConnection(**kwargs)
        self.conn.consumer_timeout = 0.01
        self.received = []
        self.reply_with = {}

        self.consumer_thread = None
        self.condition = threading.Condition()

    def handle(self, opname, reply_with=_NO_REPLY):
        if reply_with is not _NO_REPLY:
            self.reply_with[opname] = reply_with
        self.conn.handle(partial(self._handler, opname), opname)

    def _handler(self, opname, **kwargs):
        with self.condition:
            self.received.append((opname, kwargs))
            self.condition.notifyAll()

        if opname in self.reply_with:
            reply_with = self.reply_with[opname]
            if callable(reply_with):
                return reply_with()
            return reply_with

    def wait(self, timeout=5):
        with self.condition:
            while not self.received:
                self.condition.wait(timeout)
                if not self.received:
                    raise Exception("timed out waiting for message")

    def consume(self, count):
        self.conn.consume(count=count, timeout=self.consume_timeout)

    def consume_in_thread(self, count=None):
        assert self.consumer_thread is None
        t = threading.Thread(target=self.consume, args=(count,))
        t.daemon = True
        self.consumer_thread = t
        t.start()

    def join_consumer_thread(self, cancel=False):
        if self.consumer_thread:
            if cancel:
                self.conn.cancel()
            self.consumer_thread.join()
            self.consumer_thread = None

    def clear(self):
        self.received[:] = []

    def cancel(self):
        self.conn.cancel()


class DashiConnectionTests(unittest.TestCase):

    uri = 'memory://hello'

    def test_fire(self):
        receiver = TestReceiver(uri=self.uri, exchange="x1")
        receiver.handle("test")
        receiver.handle("test2")

        conn = dashi.DashiConnection("s1", self.uri, "x1")
        args1 = dict(a=1, b="sandwich")
        conn.fire(receiver.name, "test", **args1)

        receiver.consume(1)

        self.assertEqual(len(receiver.received), 1)
        opname, gotargs = receiver.received[0]
        self.assertEqual(opname, "test")
        self.assertEqual(gotargs, args1)

        args2 = dict(a=2, b="burrito")
        args3 = dict(a=3)

        conn.fire(receiver.name, "test", **args2)
        conn.fire(receiver.name, "test2", **args3)

        receiver.clear()
        receiver.consume(2)

        self.assertEqual(len(receiver.received), 2)
        opname, gotargs = receiver.received[0]
        self.assertEqual(opname, "test")
        self.assertEqual(gotargs, args2)
        opname, gotargs = receiver.received[1]
        self.assertEqual(opname, "test2")
        self.assertEqual(gotargs, args3)

    def test_call(self):
        receiver = TestReceiver(uri=self.uri, exchange="x1")
        replies = [5,4,3,2,1]
        receiver.handle("test", replies.pop)
        receiver.consume_in_thread(1)

        conn = dashi.DashiConnection("s1", self.uri, "x1")
        args1 = dict(a=1, b="sandwich")

        ret = conn.call(receiver.name, "test", **args1)
        self.assertEqual(ret, 1)
        receiver.join_consumer_thread()

        receiver.consume_in_thread(4)

        for i in list(reversed(replies)):
            ret = conn.call(receiver.name, "test", **args1)
            self.assertEqual(ret, i)
            
        receiver.join_consumer_thread()

    def test_call_unknown_op(self):
        receiver = TestReceiver(uri=self.uri, exchange="x1")
        receiver.handle("test", True)
        receiver.consume_in_thread(1)

        conn = dashi.DashiConnection("s1", self.uri, "x1")

        try:
            conn.call(receiver.name, "notarealop")
        except dashi.UnknownOperationError:
            pass
        else:
            self.fail("Expected UnknownOperationError")
        finally:
            receiver.join_consumer_thread()

    def test_call_handler_error(self):
        def raise_hell():
            raise Exception("hell")

        receiver = TestReceiver(uri=self.uri, exchange="x1")
        receiver.handle("raiser", raise_hell)
        receiver.consume_in_thread(1)

        conn = dashi.DashiConnection("s1", self.uri, "x1")

        try:
            conn.call(receiver.name, "raiser")

        except dashi.DashiError:
            pass
        else:
            self.fail("Expected DashiError")
        finally:
            receiver.join_consumer_thread()

    def test_fire_many_receivers(self):
        extras = {}
        receivers = []
        receiver_name = None

        for i in range(3):
            receiver = TestReceiver(uri=self.uri, exchange="x1", **extras)
            if not receiver_name:
                receiver_name = receiver.name
                extras['name'] = receiver.name
            receiver.handle("test")
            receivers.append(receiver)

        conn = dashi.DashiConnection("s1", self.uri, "x1")
        for i in range(10):
            conn.fire(receiver_name, "test", n=i)

        # walk the receivers and have each one consume a single message
        receiver_cycle = itertools.cycle(receivers)
        for i in range(10):
            receiver = next(receiver_cycle)
            receiver.consume(1)
            opname, args = receiver.received[-1]
            self.assertEqual(opname, "test")
            self.assertEqual(args['n'], i)

    def test_cancel(self):

        receiver = TestReceiver(uri=self.uri, exchange="x1")
        receiver.handle("nothing", 1)
        receiver.consume_in_thread(1)

        receiver.cancel()

        # this should hang forever if cancel doesn't work
        receiver.join_consumer_thread()

    def test_cancel_resume_cancel(self):
        receiver = TestReceiver(uri=self.uri, exchange="x1")
        receiver.handle("test", 1)
        receiver.consume_in_thread()

        conn = dashi.DashiConnection("s1", self.uri, "x1")
        self.assertEqual(1, conn.call(receiver.name, "test"))

        receiver.cancel()
        receiver.join_consumer_thread()
        receiver.clear()

        # send message while receiver is cancelled
        conn.fire(receiver.name, "test", hats=4)

        # start up consumer again. message should arrive.
        receiver.consume_in_thread()

        receiver.wait()
        self.assertEqual(receiver.received[-1], ("test", dict(hats=4)))

        receiver.cancel()
        receiver.join_consumer_thread()


class RabbitDashiConnectionTests(DashiConnectionTests):
    """The base dashi tests run on rabbit, plus some extras which are
    rabbit specific
    """
    uri = "amqp://guest:guest@127.0.0.1//"

    def test_call_channel_free(self):

        # hackily ensure that call() releases its channel

        receiver = TestReceiver(uri=self.uri, exchange="x1")
        receiver.handle("test", "myreply")
        receiver.consume_in_thread(1)

        conn = dashi.DashiConnection("s1", self.uri, "x1")

        # peek into connection to grab a channel and note its id
        with connections[conn._conn].acquire(block=True) as kombuconn:
            with kombuconn.channel() as channel:
                channel_id = channel.channel_id
                log.debug("got channel ID %s", channel.channel_id)

        ret = conn.call(receiver.name, "test")
        self.assertEqual(ret, "myreply")
        receiver.join_consumer_thread()

        # peek into connection to grab a channel and note its id
        with connections[conn._conn].acquire(block=True) as kombuconn:
            with kombuconn.channel() as channel:
                log.debug("got channel ID %s", channel.channel_id)
                self.assertEqual(channel_id, channel.channel_id)

