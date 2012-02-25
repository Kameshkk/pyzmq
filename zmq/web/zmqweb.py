"""Classes allows Tornado handlers to be run in separate processes.

This module uses ZeroMQ/PyZMQ sockets (DEALER/ROUTER) to enable individual
Tornado handlers to be run in a separate backend process. Through the
usage of DEALER/ROUTER sockets, multiple backend processes for a given 
handler can be started and requests will be load balanced among the backend
processes.
 
Authors:

* Brian Granger
"""

#-----------------------------------------------------------------------------
#
#    Copyright (c) 2012 Min Ragan-Kelley, Brian Granger
#
#    This file is part of pyzmq.
#
#    pyzmq is free software; you can redistribute it and/or modify it under
#    the terms of the Lesser GNU General Public License as published by
#    the Free Software Foundation; either version 3 of the License, or
#    (at your option) any later version.
#
#    pyzmq is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    Lesser GNU General Public License for more details.
#
#    You should have received a copy of the Lesser GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#-----------------------------------------------------------------------------

#-----------------------------------------------------------------------------
# Imports
#-----------------------------------------------------------------------------

import logging
import time
import uuid
import urlparse

from tornado import httpserver
from tornado import httputil
from tornado import web
from tornado import stack_context
from tornado.escape import native_str
from tornado.util import b

import zmq
from zmq.eventloop.zmqstream import ZMQStream
from zmq.eventloop.ioloop import IOLoop, DelayedCallback
from zmq.utils import jsonapi

#-----------------------------------------------------------------------------
# Service client
#-----------------------------------------------------------------------------


class ZMQApplicationProxy(object):
    """A proxy for a ZeroMQ based ZMQApplication that is using ZMQHTTPRequest.

    This class is a proxy for a backend that is running a
    ZMQApplication and MUST be used with the ZMQHTTPRequest class. This
    version sends the reply parts (each generated by RequestHandler.flush) as
    a single multipart zmq message for low latency replies. See
    ZMQStreamingApplicationProxy, for a version that has higher latency, but
    which sends each reply part as a separate zmq message.
    """

    def __init__(self, loop=None, context=None):
        self.loop = loop if loop is not None else IOLoop.instance()
        self.context = context if context is not None else zmq.Context.instance()
        self._callbacks = {}
        self.socket = self.context.socket(zmq.DEALER)
        self.stream = ZMQStream(self.socket, self.loop)
        self.stream.on_recv(self._handle_reply)
        self.urls = []

    def connect(self, url):
        """Connect the service client to the proto://ip:port given in the url."""
        self.urls.append(url)
        self.socket.connect(url)

    def bind(self, url):
        """Bind the service client to the proto://ip:port given in the url."""
        self.urls.append(url)
        self.socket.bind(url)

    def send_request(self, request, args, kwargs, handler, timeout):
        """Send a request to the service."""
        req = {}
        req['method'] = request.method
        req['uri'] = request.uri
        req['version'] = request.version
        req['headers'] = dict(request.headers)
        body = request.body
        req['remote_ip'] = request.remote_ip
        req['protocol'] = request.protocol
        req['host'] = request.host
        req['files'] = request.files
        req['arguments'] = request.arguments
        req['args'] = args
        req['kwargs'] = kwargs

        msg_id = bytes(uuid.uuid4())
        msg_list = [b'|', msg_id, jsonapi.dumps(req)]
        if body:
            msg_list.append(body)
        logging.debug('Sending request: %r' % msg_list)
        self.stream.send_multipart(msg_list)

        if timeout > 0:
            def _handle_timeout():
                handler.send_error(504) # Gateway timeout
                try:
                    self._callbacks.pop(msg_id)
                except KeyError:
                    logging.error('Unexpected error removing callbacks')
            dc = DelayedCallback(_handle_timeout, timeout, self.loop)
            dc.start()
        else:
            dc = None
        self._callbacks[msg_id] = (handler, dc)
        return msg_id

    def _handle_reply(self, msg_list):
        logging.debug('Handling reply: %r' % msg_list)
        len_msg_list = len(msg_list)
        if len_msg_list < 2:
            logging.error('Unexpected reply from proxy in ZMQApplicationProxy._handle_reply')
            return
        msg_id = msg_list[0]
        replies = msg_list[1:]
        cb = self._callbacks.pop(msg_id, None)
        if cb is not None:
            handler, dc = cb
            if dc is not None:
                dc.stop()
            try:
                for reply in replies:
                    handler.write(reply)
                # The backend has already processed the headers and they are
                # included in the above write calls, so we manually tell the
                # handler that the headers are already written.
                handler._headers_written = True
                # We set transforms to an empty list because the backend
                # has already applied all of the transforms.
                handler._transforms = []
                handler.finish()
            except:
                logging.error('Unexpected error in ZMQApplicationProxy._handle_reply', exc_info=True)


class ZMQStreamingApplicationProxy(ZMQApplicationProxy):
    """A proxy for a ZeroMQ based ZMQApplication that is using ZMQStreamingHTTPRequest.

    This class is a proxy for a backend that is running a
    ZMQApplication and MUST be used with the ZMQStreamingHTTPRequest class.
    This version sends the reply parts (each generated by RequestHandler.flush)
    as separate zmq messages to enable streaming replies. See
    ZMQApplicationProxy, for a version that has lower latency, but which sends
    all reply parts as a single zmq message.
    """

    def _handle_reply(self, msg_list):
        logging.debug('Handling reply: %r' % msg_list)
        len_msg_list = len(msg_list)
        if len_msg_list < 2:
            logging.error('Unexpected reply from proxy in ZMQStreamingApplicationProxy._handle_reply')
            return
        msg_id = msg_list[0]
        reply = msg_list[1]
        cb = self._callbacks.get(msg_id)
        if cb is not None:
            handler, dc = cb
            if reply == b'DATA' and len_msg_list == 3:
                if dc is not None:
                    # Stop the timeout DelayedCallback and set it to None.
                    dc.stop()
                    self._callbacks[msg_id] = (handler, None)
                try:
                    handler.write(msg_list[2])
                    # The backend has already processed the headers and they are
                    # included in the above write calls, so we manually tell the
                    # handler that the headers are already written.
                    handler._headers_written = True
                    # We set transforms to an empty list because the backend
                    # has already applied all of the transforms.
                    handler._transforms = []
                    handler.flush()
                except socket.error:
                    # socket.error is raised if the client disconnects while
                    # we are sending.
                    pass
                except:
                    logging.error('Unexpected write error', exc_info=True)
            elif reply == b'FINISH':
                # We are done so we can get rid of the callbacks for this msg_id.
                self._callbacks.pop(msg_id)
                try:
                    handler.finish()
                except socket.error:
                    # socket.error is raised if the client disconnects while
                    # we are sending.
                    pass
                except:
                    logging.error('Unexpected finish error', exc_info=True)


class ZMQRequestHandlerProxy(web.RequestHandler):
    """A handler for use with a ZeroMQ backend service client."""

    SUPPORTED_METHODS = ("GET", "HEAD", "POST", "DELETE", "PUT", "OPTIONS")

    def initialize(self, proxy, timeout=0):
        """Initialize with a proxy and timeout.

        Parameters
        ----------
        proxy : ZMQApplicationProxy. ZMQStreamingApplicationProxy
            A proxy instance that will be used to send requests to a backend
            process.
        timeout : int
            The timeout, in milliseconds. If this timeout is reached
            before the backend's first reply, then the server is sent a
            status code of 504 to the browser to indicate a gateway/proxy
            timeout. Set to 0 or a negative number to disable (infinite 
            timeout).
        """
        # zmqweb Note: This method is empty in the base class.
        self.proxy = proxy
        self.timeout = timeout

    def _execute(self, transforms, *args, **kwargs):
        """Executes this request with the given output transforms."""
        # ZMQWEB NOTE: Transforms should be applied in the backend service so
        # we null any transforms passed in here. This may be a little too
        # silent, but there may be other handlers that do need the transforms.
        self._transforms = []
        # ZMQWEB NOTE: This following try/except block is taken from the base
        # class, but is modified to send the request to the proxy.
        try:
            if self.request.method not in self.SUPPORTED_METHODS:
                raise web.HTTPError(405)
            # ZMQWEB NOTE: We have removed the XSRF cookie handling from here
            # as it will be handled in the backend.
            self.prepare()
            if not self._finished:
                # ZMQWEB NOTE: Here is where we send the request to the proxy.
                # We don't decode args or kwargs as that will be done in the
                # backen.
                self.proxy.send_request(
                    self.request, args, kwargs, self, self.timeout
                )
        except Exception:
            # ZMQWEB NOTE: We don't call the usual error handling logic
            # as that will be called by the backend process.
            logging.error('Unexpected error in _execute', exc_info=True)


#-----------------------------------------------------------------------------
# Service implementation
#-----------------------------------------------------------------------------


class ZMQHTTPRequest(httpserver.HTTPRequest):
    """A single HTTP request that receives requests and replies to a zmq proxy.

    This version MUST be used with the `ZMQApplicationProxy` class and sends
    the reply parts as a single zmq message. This is the default HTTP request
    class, but you can set it explicitly by passing the `http_request_class`
    argument::

        ZMQApplication(handlers, http_request_class=ZMQHTTPRequest)
    """

    def __init__(self, method, uri, version="HTTP/1.0", headers=None,
                 body=None, remote_ip=None, protocol=None, host=None,
                 files=None, connection=None, arguments=None,
                 idents=None, msg_id=None, stream=None):
        # ZMQWEB NOTE: This method is copied from the base class to make a
        # number of changes. We have added the arguments, ident, msg_id and
        # stream kwargs.
        self.method = method
        self.uri = uri
        self.version = version
        self.headers = headers or httputil.HTTPHeaders()
        self.body = body or ""
        # ZMQWEB NOTE: We simply copy the remote_ip, protocol and host as they
        # have been parsed by the other side.
        self.remote_ip = remote_ip
        self.protocol = protocol
        self.host = host
        self.files = files or {}
        # ZMQWEB NOTE: The connection attribute MUST not be saved in the
        # instance. This is because its precense triggers logic in the base
        # class that doesn't apply because ZeroMQ sockets are connectionless.
        self._start_time = time.time()
        self._finish_time = None

        # ZMQWEB NOTE: Attributes we have added to ZMQHTTPRequest.
        self.idents = idents
        self.msg_id = msg_id
        self.stream = stream
        self._chunks = []
        self._write_callback = None

        scheme, netloc, path, query, fragment = urlparse.urlsplit(native_str(uri))
        self.path = path
        self.query = query
        # ZMQWEB NOTE: We let the other side parse the arguments and simply
        # pass them into this class.
        self.arguments = arguments

    def _create_msg_list(self):
        """Create a new msg_list with idents and msg_id."""
        # Always create a copy as we use this multiple times.
        msg_list = []
        msg_list.extend(self.idents)
        msg_list.append(self.msg_id)
        return msg_list

    def write(self, chunk, callback=None):
        # ZMQWEB NOTE: This method is overriden from the base class.
        logging.debug('Buffering chunk: %r' % chunk)
        if callback is not None:
            self._write_callback = stack_context.wrap(callback)
        self._chunks.append(chunk)

    def finish(self):
        # ZMQWEB NOTE: This method is overriden from the base class to remove
        # a call to self.connection.finish() and send the reply message.
        msg_list = self._create_msg_list()
        msg_list.extend(self._chunks)
        self._chunks = []
        logging.debug('Sending reply: %r' % msg_list)
        self._finish_time = time.time()
        self.stream.send_multipart(msg_list)
        if self._write_callback is not None:
            try:
                self._write_callback()
            except:
                logging.error('Unexpected exception in write callback', exc_info=True)
            self._write_callback = None

    def get_ssl_certificate(self):
        # ZMQWEB NOTE: This method is overriden from the base class.
        raise NotImplementedError('get_ssl_certificate is not implemented subclass')


class ZMQStreamingHTTPRequest(ZMQHTTPRequest):
    """A single HTTP request that receives requests and replies to a zmq proxy.

    This version MUST be used with the `ZMQStreamingApplicationProxy` class
    and sends the reply parts as separate zmq messages. To use this version,
    pass the `http_request_class` argument::

        ZMQApplication(handlers, http_request_class=ZMQStreamingHTTPRequest)
    """

    def write(self, chunk, callback=None):
        # ZMQWEB NOTE: This method is overriden from the base class.
        msg_list = self._create_msg_list()
        msg_list.extend([b'DATA', chunk])
        logging.debug('Sending write: %r' % msg_list)
        self.stream.send_multipart(msg_list)
        # ZMQWEB NOTE: We don't want to permanently register an on_send callback
        # with the stream, so we just call the callback immediately.
        if callback is not None:
            try:
                stack_context.wrap(callback)()
            except:
                logging.error('Unexpected exception in write callback', exc_info=True)

    def finish(self):
        # ZMQWEB NOTE: This method is overriden from the base class to remove
        # a call to self.connection.finish() and send the FINISH message.
        self._finish_time = time.time()
        msg_list = self._create_msg_list()
        msg_list.append(b'FINISH')
        logging.debug('Sending finish: %r' % msg_list)
        self.stream.send_multipart(msg_list)


class ZMQApplication(web.Application):
    """A ZeroMQ based application that server requests for a proxy.

    This class is run in a backend process and handles requests for a
    `ZMQApplicationProxy` or `ZMQStreamingApplicationProxy` class running
    in the frontend. Which of these classes is used in the frontend will
    depend on which HTTP request class is used in your backend `ZMQApplication`.
    Here is the correlation:
    
    * `ZMQApplicationProxy` with `ZMQHTTPRequest`.
    * `ZMQStreamingApplicationProxy` with `ZMQStreamingHTTPRequest`.

    To set the HTTP request class, pass the `http_request_class` setting to
    this class::

        ZMQApplication(handlers, http_request_class=ZMQStreamingHTTPRequest)
    """

    def __init__(self, handlers=None, default_host="", transforms=None,
                 wsgi=False, **settings):
        # ZMQWEB NOTE: This method is overriden from the base class.
        # ZMQWEB NOTE: We have added new context and loop settings.
        self.context = settings.pop('context', zmq.Context.instance())
        self.loop = settings.pop('loop', IOLoop.instance())
        super(ZMQApplication,self).__init__(
            handlers=handlers, default_host=default_host,
            transforms=transforms, wsgi=wsgi, **settings
        )
        # ZMQWEB NOTE: Here we create the zmq socket and stream and setup a
        # list of urls that are bound/connected to.
        self.socket = self.context.socket(zmq.ROUTER)
        self.stream = ZMQStream(self.socket, self.loop)
        self.stream.on_recv(self._handle_request)
        self.urls = []

    def connect(self, url):
        """Connect the service to the proto://ip:port given in the url."""
        # ZMQWEB NOTE: This is a new method in this subclass.
        self.urls.append(url)
        self.socket.connect(url)

    def bind(self, url):
        """Bind the service to the proto://ip:port given in the url."""
        # ZMQWEB NOTE: This is a new method in this subclass.
        self.urls.append(url)
        self.socket.bind(url)

    def _handle_request(self, msg_list):
        # ZMQWEB NOTE: This is a new method in this subclass. This method
        # is used as the on_recv callback for self.stream.
        logging.debug('Handling request: %r' % msg_list)
        try:
            request, args, kwargs = self._parse_request(msg_list)
        except IndexError:
            logging.error('Unexpected request message format in ZMQApplication._handle_request.')
        else:
            self.__call__(request, args, kwargs)

    def _parse_request(self, msg_list):
        # ZMQWEB NOTE: This is a new method in this subclass.
        len_msg_list = len(msg_list)
        if len_msg_list < 4:
            raise IndexError('msg_list must have length 3 or more')
        # Use | to as a delimeter between identities and the rest.
        i = msg_list.index(b'|')
        idents = msg_list[0:i]
        msg_id = msg_list[i+1]
        req = jsonapi.loads(msg_list[i+2])
        body = msg_list[i+3] if len_msg_list==i+4 else ""

        http_request_class = self.settings.get('http_request_class',
            ZMQHTTPRequest)
        request = http_request_class(method=req['method'], uri=req['uri'],
            version=req['version'], headers=req['headers'],
            body=body, remote_ip=req['remote_ip'], protocol=req['protocol'],
            host=req['host'], files=req['files'], arguments=req['arguments'],
            idents=idents, msg_id=msg_id, stream=self.stream
        )
        args = req['args']
        kwargs = req['kwargs']
        return request, args, kwargs

    def __call__(self, request, args, kwargs):
        """Called by HTTPServer to execute the request."""
        # ZMQWEB NOTE: This method overrides the logic in the base class.
        # This is just like web.Application.__call__ but it lacks the
        # parsing logic for args/kwargs, which are already parsed on the
        # other side and are passed as arguments.
        transforms = [t(request) for t in self.transforms]
        handler = None
        args = args
        kwargs = kwargs
        handlers = self._get_host_handlers(request)
        # ZMQWEB NOTE: ZMQRedirectHandler is used here.
        redirect_handler_class = self.settings.get("redirect_handler_class",
                                            web.RedirectHandler)
        if not handlers:
            handler = redirect_handler_class(
                self, request, url="http://" + self.default_host + "/")
        else:
            for spec in handlers:
                match = spec.regex.match(request.path)
                if match:
                    handler = spec.handler_class(self, request, **spec.kwargs)
                    # ZMQWEB NOTE: web.Application.__call__ has logic here to
                    # parse args and kwargs. This These are already parsed for us and passed
                    # into __call__ so we just use them.
                    break
            if not handler:
                handler = web.ErrorHandler(self, request, status_code=404)

        # ZMQWEB NOTE: ZMQRequestHandler and ZMQStaticFileHandler are used here.
        if self.settings.get("debug"):
            with web.RequestHandler._template_loader_lock:
                for loader in web.RequestHandler._template_loaders.values():
                    loader.reset()
            web.StaticFileHandler.reset()

        handler._execute(transforms, *args, **kwargs)
        return handler

    #---------------------------------------------------------------------------
    # Methods not used from tornado.web.Application
    #---------------------------------------------------------------------------

    def listen(self, port, address="", **kwargs):
        # ZMQWEB NOTE: This method is overriden from the base class.
        raise NotImplementedError('listen is not implmemented')