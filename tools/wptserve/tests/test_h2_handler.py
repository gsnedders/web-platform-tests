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
import threading
from queue import Empty, Queue
from unittest import mock

import pytest

import localpaths  # noqa: F401  # populates sys.path

import wptserve.logger
import h2.settings
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


def test_recv_loop_skips_request_after_connection_terminated_in_same_batch():
    """A RequestReceived arriving in the same batch as a
    ConnectionTerminated must never spawn a worker. Real h2 cannot
    produce that batch: once a peer GOAWAY moves the connection to the
    CLOSED state, subsequent HEADERS raise ProtocolError out of
    receive_data rather than yielding a RequestReceived. The recv loop
    encodes that invariant as `assert not terminated` in the
    RequestReceived branch.

    This test fabricates the impossible batch by mocking receive_data to
    return [ConnectionTerminated, RequestReceived]. The assert fires, the
    recv loop's `except Exception` catches it, and crucially no worker is
    spawned for the stream the GOAWAY already invalidated. Mocking is the
    only way to reach this tripwire — by construction it cannot be
    produced on the wire (see the functional companion
    test_headers_after_goaway_closes_connection_without_handler).
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "_stream_ws_pump_frames consumes DataReceived frames and writes "
        "them to the pipe but never returns flow-control credit. Crediting "
        "timing is implementation-defined (RFC 9113 §5.2.1) and §5.2.2's "
        "read-promptly MUST is satisfied; §6.9.1 only describes credit "
        "following consumption. ws-over-h2 streams (RFC 8441) are long-lived "
        "and unbounded, so consuming DATA without ever crediting drains the "
        "connection-level inbound window to 0 and wedges every stream on the "
        "connection — an interop/quality gap, not a normative violation."
    ),
)
def test_stream_ws_pump_frames_returns_flow_control_credit():
    handler = _bare_handler()
    handler.uid = "test"

    wfile = mock.MagicMock()

    # Drive a server-side H2Connection through real events so the
    # inbound flow-control state matches what the pump would see in
    # production. Wrap it in H2ConnectionGuard exactly like
    # handle_one_request does at line ~478.
    capture = _H2EventCapture()
    capture.send_headers(stream_id=1)
    capture.send_data(stream_id=1, data=b"x" * 1000, end_stream=False)
    # A terminator so the pump returns; no end_stream on the data, just
    # a None sentinel, mirrors the connection-teardown shape.
    capture.events.append(None)

    handler.conn = server_module.H2ConnectionGuard(capture.server)

    # The capture's startup handshake has already drained outbound
    # data; reset so we can tell whether the pump produces fresh
    # WINDOW_UPDATE bytes.
    capture.server.data_to_send()

    queue = Queue()
    for event in capture.events:
        queue.put(event)

    handler._stream_ws_pump_frames(stream_id=1, queue=queue, wfile=wfile)

    # If the pump returns credit, h2 will have queued at least one
    # WINDOW_UPDATE frame for the bytes consumed.
    outbound = bytes(capture.server.data_to_send())
    assert _contains_window_update(outbound), (
        "Pump consumed DATA but emitted no WINDOW_UPDATE — the "
        "connection-level inbound window will eventually drain to 0 "
        "and wedge every stream on the connection."
    )


def _contains_window_update(data):
    """True if `data` contains an HTTP/2 WINDOW_UPDATE frame (type 0x08).

    HTTP/2 frame header: 24-bit length, 8-bit type, 8-bit flags,
    32-bit stream id. Walk the frames and return as soon as one with
    type 0x08 is found.
    """
    offset = 0
    while offset + 9 <= len(data):
        length = int.from_bytes(data[offset:offset + 3], "big")
        frame_type = data[offset + 3]
        if frame_type == 0x08:
            return True
        offset += 9 + length
    return False


def test_stream_ws_thread_joins_pump_when_transfer_data_raises():
    """When the websocket handler (dispatcher.transfer_data) raises an
    exception other than StreamClosedError/ProtocolError, _stream_ws_thread
    must still wake and join its frame-pump helper before propagating, so
    the pump isn't left orphaned on queue.get(). The helper is woken by the
    `queue.put(None)` in the finally and reaped by the following `t.join()`.

    The real pump is replaced with a recording stub: the test verifies the
    orchestration (spawn → signal → join), not the pump internals, which are
    covered by test_stream_ws_pump_frames_*. A missing `queue.put(None)`
    would wedge the join (the outer join below would time out); a missing
    finally would orphan the pump (no 'pump-exited' marker).
    """
    handler = _bare_handler()
    handler.uid = "test"
    handler.server = mock.MagicMock()  # provides self.server.ws_doc_root

    # _stream_ws_thread only needs a RequestReceived to dequeue first; the
    # handshake machinery that reads its headers is mocked out below.
    frame = mock.MagicMock(spec=server_module.RequestReceived)
    frame.stream_id = 1
    queue = Queue()
    queue.put(frame)

    markers = Queue()

    def fake_pump(self, stream_id, q, wfile):
        # Mirror the real pump's exit-on-sentinel contract so the worker's
        # t.join() can return. The timeout is only a safety net: if the
        # worker's finally fails to signal us (the regression this guards),
        # we still exit instead of leaking the thread — but we do NOT record
        # a clean exit, so the assertion below still fails.
        markers.put("pump-started")
        while True:
            try:
                item = q.get(timeout=5.0)
            except Empty:
                return
            if item is None:
                markers.put("pump-exited")
                return

    captured = {}

    def run_worker():
        try:
            handler._stream_ws_thread(stream_id=1, queue=queue)
        except BaseException as e:
            captured["exc"] = e

    with mock.patch.object(server_module, "H2HandlerCopy"), \
         mock.patch.object(server_module, "H2Request"), \
         mock.patch.object(server_module, "H2Response"), \
         mock.patch.object(server_module, "_WebSocketRequest"), \
         mock.patch.object(server_module, "WsH2Handshaker"), \
         mock.patch.object(server_module.dispatch, "Dispatcher") as mock_dispatcher, \
         mock.patch.object(
             server_module.Http2WebTestRequestHandler,
             "_stream_ws_pump_frames", fake_pump):
        # get_handler_suite must be truthy (else the worker 404s and returns
        # before spawning the pump); transfer_data raises the unexpected error.
        mock_dispatcher.return_value.get_handler_suite.return_value = object()
        mock_dispatcher.return_value.transfer_data.side_effect = RuntimeError("boom")

        worker = threading.Thread(target=run_worker, daemon=True)
        worker.start()
        worker.join(timeout=1.0)

    assert not worker.is_alive(), (
        "_stream_ws_thread did not return within 1s — its finally likely "
        "blocked on t.join() because the pump was never signalled."
    )

    seen = []
    while not markers.empty():
        seen.append(markers.get())
    assert "pump-started" in seen and "pump-exited" in seen, (
        f"pump was not spawned and cleanly joined; markers seen: {seen}"
    )
    assert isinstance(captured.get("exc"), RuntimeError), (
        f"expected the RuntimeError from transfer_data to propagate out of "
        f"_stream_ws_thread, got {captured.get('exc')!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "_stream_ws_thread's teardown does `queue.put(None); t.join()`, but "
        "if the frame pump is parked inside the unbuffered wfile.write (the "
        "pipe filled because the ws session stopped reading), the None "
        "sentinel can't reach it — the pump isn't at queue.get() — so "
        "t.join() blocks forever and the worker deadlocks. A fix would close "
        "the read end before joining so the blocked write raises and the "
        "pump exits."
    ),
)
def test_stream_ws_thread_does_not_deadlock_when_pump_blocked_on_write():
    handler = _bare_handler()
    handler.uid = "test"
    handler.server = mock.MagicMock()  # provides self.server.ws_doc_root

    # The worker's initial queue.get() consumes this RequestReceived; the
    # pump then consumes the rest.
    req = mock.MagicMock(spec=server_module.RequestReceived)
    req.stream_id = 1
    # A DATA frame far larger than the pipe buffer (~64 KiB). The pump's
    # single unbuffered wfile.write() fills the buffer and then blocks
    # forever because nothing reads the pipe (transfer_data is mocked out).
    big_frame = mock.MagicMock(spec=server_module.DataReceived)
    big_frame.data = b"\0" * (4 * 1024 * 1024)
    big_frame.stream_ended = False

    queue = Queue()
    queue.put(req)        # consumed by _stream_ws_thread's initial get
    queue.put(big_frame)  # pump blocks writing this to the real pipe
    queue.put(None)       # sentinel the blocked pump can never reach

    # Capture the real pipe file objects by mode so the cleanup below can
    # close the read end and unblock the pump (no leaked thread).
    real_fdopen = server_module.os.fdopen
    opened = {}

    def recording_fdopen(fd, mode="r", *args, **kwargs):
        f = real_fdopen(fd, mode, *args, **kwargs)
        opened[mode] = f
        return f

    def run_worker():
        handler._stream_ws_thread(stream_id=1, queue=queue)

    worker = threading.Thread(target=run_worker, daemon=True)
    try:
        with mock.patch.object(server_module.os, "fdopen", recording_fdopen), \
             mock.patch.object(server_module, "H2HandlerCopy"), \
             mock.patch.object(server_module, "H2Request"), \
             mock.patch.object(server_module, "H2Response"), \
             mock.patch.object(server_module, "_WebSocketRequest"), \
             mock.patch.object(server_module, "WsH2Handshaker"), \
             mock.patch.object(server_module.dispatch, "Dispatcher") as mock_dispatcher:
            # Truthy handler suite so the worker proceeds; transfer_data
            # returns immediately so the worker reaches its teardown finally
            # while the pump is still blocked on the big write.
            mock_dispatcher.return_value.get_handler_suite.return_value = object()

            worker.start()
            worker.join(timeout=2.0)

            assert not worker.is_alive(), (
                "_stream_ws_thread deadlocked: the frame pump is blocked in "
                "wfile.write (pipe full, no reader) and the teardown's "
                "queue.put(None); t.join() cannot interrupt it, so the worker "
                "never returns."
            )
    finally:
        # Unblock the pump without leaking it: drain the read end so the
        # blocked write completes, after which the pump reads the sentinel
        # and exits cleanly. (Closing the read end instead would make the
        # write raise BrokenPipeError in the pump thread, which pytest's
        # thread-exception plugin escalates to an error under
        # filterwarnings=error.) Guarded for the XPASS case, where the
        # worker already returned and closed the pipe.
        read_end = opened.get("rb")
        if read_end is not None and not read_end.closed:
            try:
                read_end.read()
            except (ValueError, OSError):
                pass
        worker.join(timeout=2.0)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "handle_one_request (server.py line ~499) eagerly calls "
        "connection.local_settings.acknowledge() during connection setup, "
        "before any peer SETTINGS_ACK has arrived. acknowledge() is "
        "h2-internal bookkeeping the library fires for itself when it "
        "receives an inbound SETTINGS_ACK frame (h2/connection.py "
        "_receive_settings_frame); application code driving it prematurely "
        "commits the local SETTINGS deque as acknowledged while those "
        "settings are still in flight. At setup time no peer has acked our "
        "settings, so this call should not happen at all."
    ),
)
def test_local_settings_not_acknowledged_during_setup():
    """The handler must not self-acknowledge its own local SETTINGS while
    they are still in flight.

    The setup block (server.py ~492-499) copies local_settings, enables the
    CONNECT protocol, then calls local_settings.acknowledge() before any
    bytes leave the socket. That call is library bookkeeping h2 owns and
    drives itself on receipt of an inbound SETTINGS_ACK; the application
    should never invoke it. We make the handler exit right after the setup
    block by failing the initial sendall — far enough to have called
    acknowledge() if the code does, but before any wire I/O that could
    legitimately receive a peer SETTINGS_ACK — and assert it was never
    called.

    acknowledge is patched at the class level with a call-through wrapper
    (a plain function, so `instance.acknowledge()` binds self and real
    behaviour is preserved for any code path that legitimately
    acknowledges) that records every invocation. The expected outcome is
    zero calls; today the premature call at line ~499 makes it one (the
    xfail).
    """
    handler = _bare_handler()
    handler.request = mock.Mock(spec_set=socket.socket)
    handler.request.sendall.side_effect = ConnectionResetError()

    original_acknowledge = h2.settings.Settings.acknowledge
    acknowledge = mock.Mock(name="acknowledge", side_effect=original_acknowledge)

    def recording_acknowledge(self):
        return acknowledge(self)

    with mock.patch.object(
        h2.settings.Settings, "acknowledge", recording_acknowledge
    ):
        handler.handle_one_request()

    assert acknowledge.call_count == 0, (
        "local_settings.acknowledge() was called during connection setup, "
        "before any peer SETTINGS_ACK arrived — the application is driving "
        "h2-internal bookkeeping the library owns."
    )


def test_handle_one_request_returns_on_clean_eof_after_preface():
    """A client that completes the H2 preface and then cleanly closes makes
    the server's recv return b''. The recv loop's `if data == b'': break`
    path must exit handle_one_request promptly.

    recv yields a real client preface (24-byte magic + SETTINGS) once and
    then b'' forever. Correct code reads twice — the preface, then the EOF
    that breaks the loop — and returns. If the b''-as-EOF break regressed,
    receive_data(b'') would return no events and the loop would spin on recv
    forever; the daemon worker below would never finish and the join would
    time out (the `is_alive` assertion catches that). The exact `recv` count
    pins the second read to the break, not an extra round trip.
    """
    handler = _bare_handler()
    handler.request = mock.Mock(spec_set=socket.socket)

    client = H2Connection(config=H2Configuration(client_side=True))
    client.initiate_connection()
    preface = client.data_to_send()

    recv_calls = []

    def fake_recv(bufsize):
        recv_calls.append(bufsize)
        return preface if len(recv_calls) == 1 else b""

    handler.request.recv.side_effect = fake_recv

    captured = {}

    def run():
        try:
            handler.handle_one_request()
        except BaseException as e:  # surfaced via captured, asserted below
            captured["exc"] = e

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=1.0)

    assert not worker.is_alive(), (
        "handle_one_request did not return after a clean EOF (b'') — the recv "
        "loop's `if data == b'': break` may have regressed, leaving it "
        "spinning on recv."
    )
    assert "exc" not in captured, (
        f"handle_one_request raised on a clean EOF: {captured.get('exc')!r}"
    )
    assert len(recv_calls) == 2, (
        "Expected exactly two recv calls (the preface, then the EOF that "
        f"breaks the loop); got {len(recv_calls)}."
    )
