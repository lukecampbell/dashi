from collections import namedtuple
import socket
import threading
import traceback
import uuid
import sys
import logging

from datetime import datetime, timedelta
from kombu.connection import Connection
from kombu.messaging import Consumer
from kombu.pools import connections, producers
from kombu.entity import Queue, Exchange
from kombu.common import maybe_declare

from exceptions import DashiError, BadRequestError, NotFoundError, UnknownOperationError, WriteConflictError

__version__ = '0.2.7'

log = logging.getLogger(__name__)

DEFAULT_HEARTBEAT = None  # Disabled for now

class DashiConnection(object):

    consumer_timeout = 1.0

    #TODO support connection info instead of uri

    def __init__(self, name, uri, exchange, durable=False, auto_delete=True,
                 serializer=None, transport_options=None, ssl=False, 
                 heartbeat=DEFAULT_HEARTBEAT, sysname=None):
        """Set up a Dashi connection

        @param name: name of destination service queue used by consumers
        @param uri: broker URI (e.g. 'amqp://guest:guest@localhost:5672//')
        @param exchange: name of exchange to create and use
        @param durable: if True, destination service queue and exchange will be
        created as durable
        @param auto_delete: if True, destination service queue and exchange
        will be deleted when all consumers are gone
        @param serializer: specify a serializer for message encoding
        @param transport_options: custom parameter dict for the transport backend
        @param heartbeat: amqp heartbeat interval
        @param sysname: a prefix for exchanges and queues for namespacing
        """

        self._heartbeat_interval = heartbeat
        self._conn = Connection(uri, transport_options=transport_options,
                ssl=ssl, heartbeat=self._heartbeat_interval)
        self._name = name
        self._sysname = sysname
        if self._sysname is not None:
            self._exchange_name = "%s.%s" % (self._sysname, exchange)
        else:
            self._exchange_name = exchange
        self._exchange = Exchange(name=self._exchange_name, type='direct',
                                  durable=durable, auto_delete=auto_delete)

        # visible attributes
        self.durable = durable
        self.auto_delete = auto_delete

        self._consumer_conn = None
        self._consumer = None

        self._linked_exceptions = {}

        self._serializer = serializer

    @property
    def sysname(self):
        return self._sysname

    @property
    def name(self):
        return self._name

    def add_sysname(self, name):
        if self.sysname is not None:
            return "%s.%s" % (self.sysname, name)
        else:
            return name

    def fire(self, name, operation, args=None, **kwargs):
        """Send a message without waiting for a reply

        @param name: name of destination service queue
        @param operation: name of service operation to invoke
        @param args: dictionary of keyword args to pass to operation.
                     Use this OR kwargs.
        @param kwargs: additional args to pass to operation
        """

        if args:
            if kwargs:
                raise TypeError("specify args dict or keyword arguments, not both")
        else:
            args = kwargs

        d = dict(op=operation, args=args)
        headers = {'sender': self.add_sysname(self.name)}

        with producers[self._conn].acquire(block=True) as producer:
            maybe_declare(self._exchange, producer.channel)
            producer.publish(d, routing_key=self.add_sysname(name), exchange=self._exchange_name,
                             headers=headers, serializer=self._serializer)

    def call(self, name, operation, timeout=5, args=None, **kwargs):
        """Send a message and wait for reply

        @param name: name of destination service queue
        @param operation: name of service operation to invoke
        @param timeout: RPC timeout to await a reply
        @param args: dictionary of keyword args to pass to operation.
                     Use this OR kwargs.
        @param kwargs: additional args to pass to operation
        """

        if args:
            if kwargs:
                raise TypeError("specify args dict or keyword arguments, not both")
        else:
            args = kwargs

        # create a direct exchange and queue for the reply. This may end up
        # being a bottleneck for performance: each rpc call gets a brand new
        # direct exchange and exclusive queue. However this approach is used
        # in nova.rpc and seems to have carried them pretty far. If/when this
        # becomes a bottleneck we can set up a long-lived backend queue and
        # use correlation_id to deal with concurrent RPC calls. See:
        #   http://www.rabbitmq.com/tutorials/tutorial-six-python.html
        msg_id = uuid.uuid4().hex
        exchange = Exchange(name=msg_id, type='direct',
                            durable=False, auto_delete=True)

        # check out a connection from the pool
        with connections[self._conn].acquire(block=True) as conn:
            queue = Queue(name=msg_id, exchange=exchange, routing_key=msg_id,
                          exclusive=True, durable=False, auto_delete=True)
            log.debug("declared call() reply queue %s", msg_id)

            messages = []

            def _callback(body, message):
                messages.append(body)
                message.ack()

            consumer = Consumer(conn, queues=(queue,), callbacks=(_callback,))
            consumer.declare()

            d = dict(op=operation, args=args)
            headers = {'reply-to': msg_id, 'sender': self.add_sysname(self.name)}

            with producers[self._conn].acquire(block=True) as producer:
                maybe_declare(self._exchange, producer.channel)
                log.debug("sending call to %s:%s", self.add_sysname(name), operation)
                producer.publish(d, routing_key=self.add_sysname(name), headers=headers,
                                 exchange=self._exchange, serializer=self._serializer)

            with consumer:
                log.debug("awaiting call reply on %s", msg_id)
                # only expecting one event
                conn.drain_events(timeout=timeout)

            msg_body = messages[0]
            if msg_body.get('error'):
                raise_error(msg_body['error'])
            else:
                return msg_body.get('result')

    def reply(self, msg_id, body):
        with producers[self._conn].acquire(block=True) as producer:
            try:
                producer.publish(body, routing_key=msg_id, exchange=msg_id, serializer=self._serializer)
            except self._conn.channel_errors:
                log.exception("Failed to reply to msg %s", msg_id)

    def handle(self, operation, operation_name=None, sender_kwarg=None):
        """Handle an operation using the specified function

        @param operation: function to call for this operation
        @param operation_name: operation name. if unspecifed operation.__name__ is used
        @param sender_kwarg: optional keyword arg on operation to feed in sender name
        """
        if not self._consumer:
            self._consumer_conn = connections[self._conn].acquire()
            self._consumer = DashiConsumer(self, self._consumer_conn,
                    self._name, self._exchange, sysname=self._sysname)
        self._consumer.add_op(operation_name or operation.__name__, operation,
                              sender_kwarg=sender_kwarg)

    def consume(self, count=None, timeout=None):
        """Consume operations from the queue

        @param count: number of messages to consume before returning
        @param timeout: time in seconds to wait without receiving a message
        """
        self._consumer.consume(count, timeout)

    def cancel(self, block=True):
        """Cancel a call to consume() happening in another thread

        This could take up to DashiConnection.consumer_timeout to complete.

        @param block: if True, waits until the consumer has returned
        """
        if self._consumer:
            self._consumer.cancel(block=block)

    def disconnect(self):
        """Disconnects a consumer binding if exists
        """
        if self._consumer:
            self._consumer.disconnect()

    def link_exceptions(self, custom_exception=None, dashi_exception=None):
        """Link a custom exception thrown on the receiver to a dashi exception
        """
        if custom_exception is None:
            raise ValueError("custom_exception must be set")
        if dashi_exception is None:
            raise ValueError("dashi_exception must be set")

        self._linked_exceptions[custom_exception] = dashi_exception


_OpSpec = namedtuple('_OpSpec', ['function', 'sender_kwarg'])


class DashiConsumer(object):
    def __init__(self, dashi, connection, name, exchange, sysname=None):
        self._dashi = dashi
        self._conn = connection
        self._name = name
        self._exchange = exchange
        self._sysname = sysname

        self._channel = None
        self._ops = {}
        self._cancelled = False
        self._consumer_lock = threading.Lock()
        self._last_heartbeat_check = datetime.min

        self.connect()

    def connect(self):

        self._channel = self._conn.channel()

        if self._sysname is not None:
            name = "%s.%s" % (self._sysname, self._name)
        else:
            name = self._name
        self._queue = Queue(channel=self._channel, name=name,
                exchange=self._exchange, routing_key=name,
                durable=self._dashi.durable,
                auto_delete=self._dashi.auto_delete)
        self._queue.declare()

        self._consumer = Consumer(self._channel, [self._queue],
                callbacks=[self._callback])
        self._consumer.consume()

    def disconnect(self):
        self._consumer.cancel()
        self._channel.close()
        self._conn.release()

    def consume(self, count=None, timeout=None):

        # hold a lock for the duration of the consuming. this prevents
        # multiple consumers and allows cancel to detect when consuming
        # has ended.
        if not self._consumer_lock.acquire(False):
            raise Exception("only one consumer thread may run concurrently")

        try:
            if count:
                i = 0
                while i < count and not self._cancelled:
                    self._consume_one(timeout)
                    i += 1
            else:
                while not self._cancelled:
                    self._consume_one(timeout)
        finally:
            self._consumer_lock.release()
            self._cancelled = False

    def _consume_one(self, timeout=None):

        # do consuming in a busy-ish loop, checking for cancel. There doesn't
        # seem to be an easy way to interrupt drain_events other than the
        # timeout. This could probably be added to kombu if needed. In
        # practice cancellation is likely infrequent (except in tests) so this
        # should hold for now. Can use a long timeout for production and a
        # short one for tests.

        inner_timeout = self._dashi.consumer_timeout
        elapsed = 0

        # keep trying until a single event is drained or timeout hit
        while not self._cancelled:

            self.heartbeat()

            try:
                self._conn.drain_events(timeout=inner_timeout)
                break

            except socket.timeout:
                if timeout:
                    elapsed += inner_timeout
                    if elapsed >= timeout:
                        raise

                    if elapsed + inner_timeout > timeout:
                        inner_timeout = timeout - elapsed

    def heartbeat(self):
        if self._dashi._heartbeat_interval is None:
            return

        time_between_tics = timedelta(seconds=self._dashi._heartbeat_interval / 2)

        if self._dashi.consumer_timeout > time_between_tics.seconds:
            msg = "dashi consumer timeout (%s) must be half or smaller than the heartbeat interval %s" % (
                    self._dashi.consumer_timeout, self._dashi._heartbeat_interval)

            raise DashiError(msg)

        if datetime.now() - self._last_heartbeat_check > time_between_tics:
            self._last_heartbeat_check = datetime.now()
            self._conn.heartbeat_check()


    def cancel(self, block=True):
        self._cancelled = True
        if block:
            # acquire the lock and release it immediately
            with self._consumer_lock:
                pass

    def _callback(self, body, message):
        reply_to = None
        ret = None
        err = None
        try:
            reply_to = message.headers.get('reply-to')

            try:
                op = str(body['op'])
                args = body.get('args')
            except Exception, e:
                log.warn("Failed to interpret message body: %s", body,
                         exc_info=True)
                raise BadRequestError("Invalid request: %s" % str(e))

            op_spec = self._ops.get(op)
            if not op_spec:
                raise UnknownOperationError("Unknown operation: " + op)
            op_fun = op_spec.function

            # stick the sender into kwargs if handler requested it
            if op_spec.sender_kwarg:
                sender = message.headers.get('sender')
                args[op_spec.sender_kwarg] = sender

            try:
                ret = op_fun(**args)
            except TypeError, e:
                log.exception("Type error with handler for %s:%s", self._name, op)
                raise BadRequestError("Type error: %s" % str(e))
            except Exception:
                log.exception("Error in handler for %s:%s", self._name, op)
                raise

        except Exception:
            err = sys.exc_info()
        finally:
            if reply_to:
                if err:
                    tb = "".join(traceback.format_exception(*err))

                    # some error types are specific to dashi (not underlying
                    # service code). These get raised with the same type on
                    # the client side. Identify them by prefixing the package
                    # name on the exc_type.

                    exc_type = err[0]

                    # Check if there is a dashi exception linked to this custom exception
                    linked_exception = self._dashi._linked_exceptions.get(exc_type)
                    if linked_exception:
                        exc_type = linked_exception

                    known_type = ERROR_TYPE_MAP.get(exc_type.__name__)
                    if known_type and exc_type is known_type:
                        exc_type_name = ERROR_PREFIX + exc_type.__name__
                    else:
                        exc_type_name = exc_type.__name__

                    err = dict(exc_type=exc_type_name, value=str(err[1]),
                               traceback=tb)

                reply = dict(result=ret, error=err)
                self._dashi.reply(reply_to, reply)

            message.ack()

    def add_op(self, name, fun, sender_kwarg=None):
        if not callable(fun):
            raise ValueError("operation function must be callable")

        self._ops[name] = _OpSpec(fun, sender_kwarg)


def raise_error(error):
    """Intakes a dict of remote error information and raises a DashiError
    """
    exc_type = error.get('exc_type')
    if exc_type and exc_type.startswith(ERROR_PREFIX):
        exc_type = exc_type[len(ERROR_PREFIX):]
        exc_cls = ERROR_TYPE_MAP.get(exc_type, DashiError)
    else:
        exc_cls = DashiError

    raise exc_cls(**error)

ERROR_PREFIX = "dashi.exceptions."
ERROR_TYPES = (BadRequestError, NotFoundError, UnknownOperationError, WriteConflictError)
ERROR_TYPE_MAP = dict((cls.__name__, cls) for cls in ERROR_TYPES)
