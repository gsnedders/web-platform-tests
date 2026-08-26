# mypy: allow-untyped-defs

import contextlib
import json
import select
import socket
import threading
import time

from http.client import HTTPConnection
from typing import Dict, List, Mapping, Sequence, Tuple
from urllib import parse as urlparse

from . import error

"""Implements HTTP transport for the WebDriver wire protocol."""


missing = object()

# Sentinel for the default value of every per-call `timeout` parameter,
# mirroring http.client's _GLOBAL_DEFAULT_TIMEOUT convention. It means "no
# explicit value here -- defer to the active deadline() scope, or, failing
# that, socket.getdefaulttimeout()". This is distinct from an explicit
# `None`, which means "no timeout, block forever" and overrides any active
# deadline. Named distinctly from `timeout` (rather than reusing that name
# for a module global) because every method below takes a parameter named
# `timeout`, which would shadow a same-named module global inside the
# method body.
DEFAULT_TIMEOUT: object = object()

# Floor applied to the remaining time under an active deadline() so that an
# expired deadline still resolves to a small positive timeout rather than a
# literal 0. socket.settimeout(0) means non-blocking mode, not "block with a
# very short timeout" -- using it would change the exception callers see
# (BlockingIOError instead of socket.timeout). This keeps expired deadlines
# in ordinary blocking-with-timeout mode.
_MIN_TIMEOUT = 1e-3


class ResponseHeaders(Mapping[str, str]):
    """Read-only dictionary-like API for accessing response headers.

    This class:
      * Normalizes the header keys it is built with to lowercase (such that
        iterating the items will return lowercase header keys).
      * Has case-insensitive header lookup.
      * Always returns all header values that have the same name, separated by
        commas.
    """
    def __init__(self, items: Sequence[Tuple[str, str]]):
        self.headers_dict: Dict[str, List[str]] = {}
        for key, value in items:
            key = key.lower()
            if key not in self.headers_dict:
                self.headers_dict[key] = []
            self.headers_dict[key].append(value)

    def __getitem__(self, key):
        """Get all headers of a certain (case-insensitive) name. If there is
        more than one, the values are returned comma separated"""
        values = self.headers_dict[key.lower()]
        if len(values) == 1:
            return values[0]
        else:
            return ", ".join(values)

    def get_list(self, key, default=missing):
        """Get all the header values for a particular field name as a list"""
        try:
            return self.headers_dict[key.lower()]
        except KeyError:
            if default is not missing:
                return default
            else:
                raise

    def __iter__(self):
        yield from self.headers_dict

    def __len__(self):
        return len(self.headers_dict)


class Response:
    """
    Describes an HTTP response received from a remote end whose
    body has been read and parsed as appropriate.
    """

    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = headers

    def __repr__(self):
        cls_name = self.__class__.__name__
        if self.error:
            return f"<{cls_name} status={self.status} error={repr(self.error)}>"
        return f"<{cls_name}: status={self.status} body={json.dumps(self.body)}>"

    def __str__(self):
        return json.dumps(self.body, indent=2)

    @property
    def error(self):
        if self.status != 200:
            return error.from_response(self)
        return None

    @classmethod
    def from_http(cls, http_response, decoder=json.JSONDecoder, **kwargs):
        try:
            body = json.load(http_response, cls=decoder, **kwargs)
            headers = ResponseHeaders(http_response.getheaders())
        except ValueError:
            raise ValueError("Failed to decode response body as JSON:\n" +
                             repr(http_response.read()))

        return cls(http_response.status, body, headers)


class HTTPWireProtocol:
    """
    Transports messages (commands and responses) over the WebDriver
    wire protocol.

    Complex objects, such as ``webdriver.ShadowRoot``, ``webdriver.WebElement``,
    ``webdriver.WebFrame``, and ``webdriver.WebWindow`` are by default not
    marshaled to enable use of `session.transport.send` in WPT tests::

        session = webdriver.Session("127.0.0.1", 4444)
        response = transport.send("GET", "element/active", None)
        print response.body["value"]
        # => {u'element-6066-11e4-a52e-4f735466cecf': u'<uuid>'}

    Automatic marshaling is provided by ``webdriver.protocol.Encoder``
    and ``webdriver.protocol.Decoder``, which can be passed in to
    ``HTTPWireProtocol.send`` along with a reference to the current
    ``webdriver.Session``::

        session = webdriver.Session("127.0.0.1", 4444)
        response = transport.send("GET", "element/active", None,
            encoder=protocol.Encoder, decoder=protocol.Decoder,
            session=session)
        print response.body["value"]
        # => webdriver.Element
    """

    def __init__(self, host, port, url_prefix="/"):
        """
        Construct interface for communicating with the remote server.

        :param host: Hostname of remote WebDriver server.
        :param port: Port of remote WebDriver server.
        :param url_prefix: Prefix for request URLs.
        """
        self.host = host
        self.port = port
        self.url_prefix = url_prefix
        # Absolute deadline in nanoseconds (time.monotonic_ns()), or None
        # when no deadline() scope is active. Never exposed directly; only
        # deadline() mutates it, and only _effective_timeout() reads it.
        self._deadline = None
        self._conn = None
        self._last_request_is_blocked = False
        self._request_lock = threading.Lock()

    def __del__(self):
        self.close()

    def close(self):
        """Closes the current HTTP connection, if there is one."""
        if self._conn:
            try:
                self._conn.close()
            except OSError:
                # The remote closed the connection
                pass
        self._conn = None

    @staticmethod
    def _deadline_from_timeout(timeout):
        """Convert a relative timeout in seconds to an absolute deadline in
        nanoseconds on the monotonic clock. Returns None for a None timeout."""
        if timeout is None:
            return None
        return time.monotonic_ns() + int(timeout * 1e9)

    def _timeout_from_deadline(self):
        """Convert the stored absolute deadline to a relative number of
        seconds remaining, or None when no deadline is set. Floors at
        _MIN_TIMEOUT rather than 0, so an expired deadline still yields a
        small positive timeout instead of a non-blocking socket."""
        if self._deadline is None:
            return None
        remaining_ns = self._deadline - time.monotonic_ns()
        remaining = remaining_ns / 1e9
        return max(remaining, _MIN_TIMEOUT)

    def _effective_timeout(self, timeout):
        """Resolve the three-way per-call `timeout` parameter to a plain
        float | None, ready to hand to a socket API.

        - DEFAULT_TIMEOUT (the sentinel): defer to the active deadline()'s
          remaining time if one is set, else socket.getdefaulttimeout().
        - None: explicitly no timeout -- block forever, overriding any
          active deadline.
        - a float: that many seconds, also overriding any active deadline.
        """
        if timeout is DEFAULT_TIMEOUT:
            remaining = self._timeout_from_deadline()
            if remaining is not None:
                return remaining
            return socket.getdefaulttimeout()
        return timeout

    @contextlib.contextmanager
    def deadline(self, timeout):
        """Scope a relative timeout, in seconds, as the active deadline for
        the duration of the `with` block.

        Ordinary per-call `timeout=DEFAULT_TIMEOUT` requests made within the
        block resolve against this deadline. A nested deadline() is clamped
        so it can only tighten, never lengthen, any enclosing deadline. The
        previous deadline is always restored on exit, including when the
        block raises.

        :param timeout: Relative number of seconds from now, or None for no
            deadline (blocks forever, unless a tighter deadline is already
            active).
        """
        previous_deadline = self._deadline
        new_deadline = self._deadline_from_timeout(timeout)

        if previous_deadline is None:
            clamped_deadline = new_deadline
        elif new_deadline is None:
            # An infinite child under a real parent must not lengthen it.
            clamped_deadline = previous_deadline
        else:
            clamped_deadline = min(previous_deadline, new_deadline)

        self._deadline = clamped_deadline
        try:
            yield
        finally:
            self._deadline = previous_deadline

    @property
    def connection(self):
        """Gets the current HTTP connection, or lazily creates one."""
        if not self._conn:
            self._conn = HTTPConnection(self.host, self.port)
            # Reconnecting implicitly from send() would let a socket appear
            # out from under a caller who already checked conn.sock; connect
            # explicitly in _request() instead.
            self._conn.auto_open = 0

        return self._conn

    def url(self, suffix):
        """
        From the relative path to a command end-point,
        craft a full URL suitable to be used in a request to the HTTPD.
        """
        return urlparse.urljoin(self.url_prefix, suffix)

    def send(self,
             method,
             uri,
             body=None,
             headers=None,
             encoder=json.JSONEncoder,
             decoder=json.JSONDecoder,
             timeout=DEFAULT_TIMEOUT,
             **codec_kwargs):
        """
        Send a command to the remote.

        The request `body` must be JSON serializable unless a
        custom `encoder` has been provided.  This means complex
        objects such as ``webdriver.ShadowRoot``, ``webdriver.WebElement``,
        ``webdriver.WebFrame``, and `webdriver.Window`` are not automatically
        made into JSON.  This behavior is, however, provided by
        ``webdriver.protocol.Encoder``, should you want it.

        Similarly, the response body is returned au natural
        as plain JSON unless a `decoder` that converts web
        element references to ``webdriver.Element`` is provided.
        Use ``webdriver.protocol.Decoder`` to achieve this behavior.

        The client will attempt to use persistent HTTP connections.

        :param method: `GET`, `POST`, or `DELETE`.
        :param uri: Relative endpoint of the requests URL path.
        :param body: Body of the request.  Defaults to an empty
            dictionary if ``method`` is `POST`.
        :param headers: Additional dictionary of headers to include
            in the request.
        :param encoder: JSON encoder class, which defaults to
            ``json.JSONEncoder`` unless specified.
        :param decoder: JSON decoder class, which defaults to
            ``json.JSONDecoder`` unless specified.
        :param timeout: Optional timeout for the underlying socket, in seconds.
            Defaults to ``DEFAULT_TIMEOUT``, which defers to the remaining time
            under an active ``deadline()`` scope, or, absent one, to
            ``socket.getdefaulttimeout()``. Pass `None` for no timeout at all
            (blocks forever), which overrides any active deadline. Pass a
            `float` for an explicit number of seconds, which also overrides
            any active deadline.
        :param codec_kwargs: Surplus arguments passed on to `encoder`
            and `decoder` on construction.

        :return: Instance of ``webdriver.transport.Response``
            describing the HTTP response received from the remote end.

        :raises ValueError: If `body` or the response body are not
            JSON serializable.
        """
        if body is None and method == "POST":
            body = {}

        payload = None
        if body is not None:
            try:
                payload = json.dumps(body, cls=encoder, **codec_kwargs)
            except ValueError:
                raise ValueError("Failed to encode request body as JSON:\n"
                                 "%s" % json.dumps(body, indent=2))

        response = self._request(method, uri, payload, headers, timeout=timeout)
        return Response.from_http(response, decoder=decoder, **codec_kwargs)

    def _request(self, method, uri, payload, headers=None, timeout=DEFAULT_TIMEOUT):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")

        if headers is None:
            headers = {}
        headers.update({"Connection": "keep-alive"})

        url = self.url(uri)

        with self._request_lock:
            if self._last_request_is_blocked or self._has_unread_data():
                self.close()
            self._last_request_is_blocked = True

        try:
            effective = self._effective_timeout(timeout)

            conn = self.connection
            conn.timeout = effective
            if conn.sock is None:
                conn.connect()

            sock = conn.sock
            previous_timeout = sock.gettimeout()
            sock.settimeout(effective)

            try:
                conn.request(method, url, payload, headers)
                response = conn.getresponse()
            finally:
                # Only restore if this is still the connection's socket: a concurrent
                # close() may have replaced it, and restoring then would write to
                # another request's socket.
                if conn.sock is sock:
                    sock.settimeout(previous_timeout)
        finally:
            self._last_request_is_blocked = False
        return response

    def _has_unread_data(self):
        return self._conn and self._conn.sock and select.select([self._conn.sock], [], [], 0)[0]
