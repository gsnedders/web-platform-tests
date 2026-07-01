"""Unit tests for `Http2WebTestRequestHandler`.

These exercise individual methods on the handler directly, bypassing the
stdlib `BaseHTTPRequestHandler.__init__` call chain (which would
auto-invoke setup → handle → finish on a real socket). Tests construct
the handler via `__new__` and set the attributes each method actually
reads.

Functional / integration coverage of the same handler lives in
`tests/functional/test_h2_lifecycle.py`.
"""

import socket
from unittest import mock

import pytest

import localpaths  # noqa: F401  # populates sys.path

import wptserve.logger
from h2.config import H2Configuration
from h2.connection import H2Connection
from wptserve import server as server_module
from wptserve.server import Http2WebTestRequestHandler


def _bare_handler():
    """Return an `Http2WebTestRequestHandler` instance whose
    `BaseHTTPRequestHandler.__init__` chain has NOT been run.

    Tests then assign whichever attributes the method under test reads.
    """
    handler = Http2WebTestRequestHandler.__new__(Http2WebTestRequestHandler)
    handler.logger = wptserve.logger.get_logger()
    return handler


class _H2EventCapture:
    """Drive a client/server H2Connection pair and harvest the events
    the server emits.

    Using real h2 state machines on both sides means whatever events
    h2 actually emits today (and only those) are what the test sees.
    If h2's event vocabulary changes in a future vendor bump, tests
    that rely on the harvested events will fail loudly rather than
    silently passing on stale assumptions.

    Each `send_*` method drives the client, ships its outbound bytes
    to the server, and accumulates the server's resulting events into
    `self.events`.
    """

    def __init__(self):
        self.client = H2Connection(config=H2Configuration(client_side=True))
        self.server = H2Connection(config=H2Configuration(client_side=False))
        self.events = []

        self.client.initiate_connection()
        self.server.initiate_connection()
        self._pump_client_to_server()                       # preface + SETTINGS
        self.client.receive_data(self.server.data_to_send())  # server SETTINGS + ack
        self._pump_client_to_server()                       # client's SETTINGS ack
        # Drop the SETTINGS handshake events — tests care about the
        # request-level events that follow.
        self.events = []

    def _pump_client_to_server(self):
        data = self.client.data_to_send()
        if data:
            self.events += self.server.receive_data(data)

    def send_headers(self, stream_id, end_stream=False, extra_headers=()):
        self.client.send_headers(
            stream_id=stream_id,
            headers=[
                (":method", "GET"),
                (":scheme", "https"),
                (":authority", "localhost"),
                (":path", "/"),
                *extra_headers,
            ],
            end_stream=end_stream,
        )
        self._pump_client_to_server()

    def send_data(self, stream_id, data, end_stream=False):
        self.client.send_data(stream_id=stream_id, data=data, end_stream=end_stream)
        self._pump_client_to_server()

    def reset_stream(self, stream_id):
        self.client.reset_stream(stream_id=stream_id)
        self._pump_client_to_server()

    def close_connection(self, last_stream_id=0):
        self.client.close_connection(last_stream_id=last_stream_id)
        self._pump_client_to_server()


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "ConnectionResetError raised by the initial sendall returns "
        "from handle_one_request before reaching the try/finally that "
        "sets close_connection=True. The stdlib handle() loop's "
        "`while not self.close_connection: self.handle_one_request()` "
        "then re-enters handle_one_request on the now-broken socket."
    ),
)
def test_handle_one_request_marks_connection_closed_on_initial_sendall_reset():
    handler = _bare_handler()
    handler.request = mock.Mock(spec_set=socket.socket)
    handler.request.sendall.side_effect = ConnectionResetError()

    handler.handle_one_request()

    # The ConnectionResetError branch returns before entering the recv loop.
    # If the fix were reverted and the method fell through to the loop, recv
    # would be called — so asserting it was never called ties this test to
    # the actual fix's code path (the early return at server.py:506-509).
    handler.request.recv.assert_not_called()
    # close_connection must still end up True even though the early return
    # skips the recv loop. This is the assertion that flips red→green when
    # the fix (defaulting close_connection to True) lands, and it guards
    # against that default being reverted; recv.assert_not_called() above
    # keeps the check tied to the reset scenario rather than a bare flag.
    assert handler.close_connection is True
