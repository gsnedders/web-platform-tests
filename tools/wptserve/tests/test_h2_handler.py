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
    reason=(
        "The initial sendall (handle_one_request line ~506) only "
        "catches ConnectionResetError. BrokenPipeError and "
        "ConnectionAbortedError are sibling OSError subclasses, both "
        "easy to provoke (peer closed before we wrote, peer aborted "
        "mid-handshake) — they escape handle_one_request as a "
        "traceback instead of returning cleanly before the recv loop. "
        "The recv-loop catchall at line ~613 doesn't cover "
        "this path because the sendall happens before the loop opens."
    ),
)
@pytest.mark.parametrize(
    "exc",
    [BrokenPipeError(), ConnectionAbortedError()],
    ids=["BrokenPipeError", "ConnectionAbortedError"],
)
def test_handle_one_request_marks_connection_closed_on_initial_sendall_oserror(exc):
    handler = _bare_handler()
    handler.request = mock.Mock(spec_set=socket.socket)
    handler.request.sendall.side_effect = exc

    handler.handle_one_request()

    # The OSError branch should return before entering the recv loop.
    # If the fix were applied, recv would not be called — the method returns
    # early at the sendall site, exactly like the ConnectionResetError case.
    # Today these sibling OSErrors escape instead (the xfail), so this
    # assertion would only be reached if the fix were also applied.
    handler.request.recv.assert_not_called()


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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "_stream_ws_sub_thread catches only "
        "(StreamClosedError, ProtocolError) around "
        "dispatcher.transfer_data. Any other exception (a buggy ws "
        "handler raising ValueError, an OSError from a torn socket, "
        "etc.) escapes the sub-thread without putting None on the "
        "queue. The parent _stream_ws_thread is then blocked on "
        "queue.get() with no path to wake until the recv loop or the "
        "connection-level finally signals — neither of which fires "
        "promptly when the H2 stream itself is still half-open."
    ),
)
def test_stream_ws_sub_thread_signals_queue_on_uncaught_exception():
    handler = _bare_handler()

    request = mock.MagicMock()
    request._dispatcher.transfer_data.side_effect = RuntimeError("boom")

    stream_handler = mock.MagicMock()
    queue = Queue()

    with pytest.raises(RuntimeError, match="boom"):
        handler._stream_ws_sub_thread(request, stream_handler, queue)

    assert not queue.empty(), (
        "_stream_ws_sub_thread didn't enqueue a sentinel before dying; "
        "the parent _stream_ws_thread will block forever on queue.get()."
    )
    assert queue.get_nowait() is None


def test_recv_loop_skips_request_after_connection_terminated_in_same_batch():
    """When ConnectionTerminated and a new RequestReceived arrive in
    the same frame batch, the recv loop must not spawn a worker for
    the late RequestReceived. ConnectionTerminated sets
    close_connection True; the RequestReceived branch's
    `if self.close_connection: continue` guard then bails. Previously
    the guard was missing and the loop fell through, spawning a worker
    thread that immediately had to be drained and joined in finally.
    """
    conn_term = mock.MagicMock(spec=server_module.ConnectionTerminated)
    req_frame = mock.MagicMock(spec=server_module.RequestReceived)
    req_frame.stream_id = 3

    handler = _bare_handler()
    handler.request = mock.Mock(spec_set=socket.socket)
    handler.request.recv.return_value = b"some-frame-bytes"
    handler.start_stream_thread = mock.MagicMock(name="start_stream_thread")

    with mock.patch.object(
        H2Connection,
        "receive_data",
        autospec=True,
        return_value=[conn_term, req_frame],
    ):
        handler.handle_one_request()

    handler.start_stream_thread.assert_not_called()


def test_handle_one_request_marks_connection_closed_on_recv_oserror():
    """An OSError from `recv` (peer reset, broken pipe, TLS layer surfacing a
    socket failure) must be caught by the recv loop so the method returns
    cleanly — neither re-reading a now-broken connection nor letting the
    error escape — and must leave close_connection True so the stdlib
    `handle()` loop does not re-enter handle_one_request.

    recv.assert_called_once() proves the loop entered, caught the error, and
    broke out exactly once rather than hanging or propagating. The
    close_connection assertion guards the flag on this path: at this commit
    the OSError branch sets it inline, and it must keep holding after the
    branch's later refactor moves the flag into `finally`.
    """
    handler = _bare_handler()
    handler.request = mock.Mock(spec_set=socket.socket)
    handler.request.recv.side_effect = ConnectionResetError(
        "peer reset connection"
    )

    handler.handle_one_request()

    # The recv loop must have been entered (recv was called at least once)
    # and then exited cleanly — neither hanging nor escaping with an
    # exception. If the fix were reverted and the loop didn't catch the
    # error and break, recv would be called repeatedly (hanging), which
    # would manifest as call_count != 1.
    handler.request.recv.assert_called_once()
    # Keep the close_connection check too: at this commit the OSError branch
    # sets the flag inline, so it holds today and guards against a regression
    # that drops the flag on this path.
    assert handler.close_connection is True


def test_handle_one_request_marks_connection_closed_on_unexpected_exception():
    """Anything escaping the recv loop body that isn't OSError or
    ProtocolError — an AssertionError from one of our defensive asserts in
    the for-loop, a RuntimeError from threading.Thread.start under OS limit
    pressure, or a non-ProtocolError surfacing from h2 internals — must be
    caught by the loop's `except Exception` so the method returns cleanly,
    and must leave close_connection True.

    recv.assert_called_once() proves the loop entered recv, hit the error,
    and exited exactly once rather than looping or propagating. The
    close_connection assertion is the red→green discriminator: at this commit
    the catchall `except Exception` only logs and does not set the flag, so
    the assertion fails (AssertionError → the xfail holds); once the fix
    moves the flag into `finally` it passes and the xfail marker is removed.
    """
    handler = _bare_handler()
    handler.request = mock.Mock(spec_set=socket.socket)
    handler.request.recv.return_value = b"some-frame-bytes"

    # autospec catches signature drift if h2 ever renames receive_data.
    with mock.patch.object(
        H2Connection,
        "receive_data",
        autospec=True,
        side_effect=AssertionError(
            "simulated h2 internal invariant violation"
        ),
    ):
        handler.handle_one_request()

    # The recv loop must have been entered (recv was called at least once)
    # and then exited cleanly. If the fix were reverted and the exception
    # escaped without the loop catching it, recv would not have been called
    # a second time — but we'd get an uncaught exception instead of a clean
    # return, which pytest.raises would surface. call_count==1 proves the
    # loop entered recv, hit the error, and exited cleanly exactly once.
    handler.request.recv.assert_called_once()
    # Keep the close_connection check too: it is the red→green discriminator
    # for the xfail. At this commit the catchall `except Exception` only logs
    # and the flag isn't yet defaulted True, so this fails (AssertionError →
    # the xfail(raises=AssertionError) holds); the later fix that moves the
    # flag into `finally` flips it to PASS and the xfail marker is removed.
    assert handler.close_connection is True


@pytest.mark.xfail(
    strict=True,
    raises=NotImplementedError,
    reason=(
        "_stream_ws_pump_frames raises NotImplementedError on a "
        "DataReceived with stream_ended=True. The fix is to close wfile "
        "(so the ws handler's read on rfile sees EOF and transfer_data "
        "exits cleanly) and break out of the loop."
    ),
)
def test_stream_ws_pump_frames_closes_wfile_on_end_stream():
    handler = _bare_handler()
    handler.uid = "test"

    wfile = mock.MagicMock()

    # Real h2 emits the full event sequence for HEADERS + DATA(end_
    # stream=True). The pump only acts on DataReceived (and the
    # terminator types), silently skipping anything else.
    capture = _H2EventCapture()
    capture.send_headers(stream_id=1)
    capture.send_data(stream_id=1, data=b"hello", end_stream=True)

    queue = Queue()
    for event in capture.events:
        queue.put(event)

    handler._stream_ws_pump_frames(stream_id=1, queue=queue, wfile=wfile)

    wfile.write.assert_called_once_with(b"hello")
    wfile.close.assert_called_once()

