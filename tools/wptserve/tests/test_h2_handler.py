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
from queue import Queue
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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "_stream_thread wraps finish_handling in `except "
        "(StreamClosedError, ProtocolError)` only. Any other exception "
        "(OSError(EPIPE) from a sendall on a half-dead peer, a handler "
        "bug surfacing as RuntimeError, etc.) escapes the worker past "
        "the trailing cleanup() call, leaking the rfile/wfile pipe fds."
    ),
)
def test_stream_thread_closes_pipe_fds_when_finish_handling_raises():
    file_mocks = [mock.MagicMock(), mock.MagicMock()]

    def fake_handler_copy(handler, req_frame, rfile):
        h = mock.MagicMock()
        # Plain object => hasattr(req_handler, 'frame_handler') is False
        # and the frame_handler / handle_headers branches are skipped.
        h.server.router.get_handler.return_value = object()
        return h

    handler = _bare_handler()
    handler.uid = "test"
    handler.close_connection = False
    handler.finish_handling = mock.Mock(side_effect=RuntimeError("boom"))

    # Use real h2 to produce the event sequence the worker would see
    # in production for a single complete request: HEADERS(end_stream=
    # True) on the wire yields [RequestReceived, StreamEnded] from
    # receive_data(). The worker opens the pipes on RequestReceived
    # and invokes finish_handling on StreamEnded.
    capture = _H2EventCapture()
    capture.send_headers(stream_id=1, end_stream=True)

    queue = Queue()
    for event in capture.events:
        queue.put(event)

    # autospec on os.fdopen catches signature drift in the patched call.
    # The H2 wrapper classes don't expose the attributes we need to
    # drive (`stream_handler.server.router.get_handler`) at the class
    # level, so autospec there would leave us with a bare MagicMock
    # anyway.
    #
    # os.pipe is also patched: otherwise _stream_thread opens a real pipe
    # (server.py ~810) whose fds would never be closed — cleanup() closes the
    # mocked fdopen file objects, not the underlying raw fds — leaking two fds
    # per run. With os.pipe stubbed, no real fds are created.
    with mock.patch.object(
        server_module.os, "pipe", return_value=(-1, -1)
    ), mock.patch.object(
        server_module.os, "fdopen", autospec=True, side_effect=file_mocks
    ), mock.patch.object(
        server_module, "H2HandlerCopy", side_effect=fake_handler_copy
    ), mock.patch.object(
        server_module, "H2Request"
    ), mock.patch.object(
        server_module, "H2Response"
    ):
        with pytest.raises(RuntimeError, match="boom"):
            handler._stream_thread(stream_id=1, queue=queue)

    for f in file_mocks:
        f.close.assert_called_once()
