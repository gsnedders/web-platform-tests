"""Lifecycle tests for the HTTP/2 request handler.

Each section targets a specific concurrency / framing edge case in
`Http2WebTestRequestHandler`. Tests in this file go to the wire (raw TLS
+ h2.Connection) so what's exercised is the real protocol state machine,
not a mocked surface.
"""

import os
import logging
import socket
import threading
import time
from unittest import mock

import hpack
import pytest

import localpaths  # noqa: F401  # populates sys.path

import wptserve.server
import wptserve.utils
from h2.connection import H2Connection
from h2.errors import ErrorCodes
from h2.events import ConnectionTerminated
from h2.events import PingAckReceived
from h2.events import RemoteSettingsChanged
from h2.events import SettingsAcknowledged
from h2.events import StreamReset
from h2.events import DataReceived, StreamEnded
from h2.settings import SettingCodes
from hyperframe.frame import GoAwayFrame, HeadersFrame

from .base import doc_root
from .h2_raw_client import H2RawClient


pytestmark = pytest.mark.skipif(
    not wptserve.utils.http2_compatible(),
    reason="h2 server requires OpenSSL 1.0.2+",
)


@pytest.fixture
def h2_server():
    server = wptserve.server.WebTestHttpd(
        host="localhost",
        port=0,
        use_ssl=True,
        doc_root=doc_root,
        key_file=os.path.join(
            localpaths.repo_root, "tools", "certs", "web-platform.test.key"
        ),
        certificate=os.path.join(
            localpaths.repo_root, "tools", "certs", "web-platform.test.pem"
        ),
        handler_cls=wptserve.server.Http2WebTestRequestHandler,
        http2=True,
    )
    server.start()
    try:
        yield server
    finally:
        server.stop()


# -- Invalid extended CONNECT :protocol --------------------------------------
#
# RFC 8441 reserves the :protocol pseudo-header for the protocol being
# tunnelled inside an HTTP/2 extended-CONNECT stream. wptserve only
# implements 'websocket' and rejects anything else. The recv loop's
# ProtocolError branch handles this today by logging and exiting —
# closing the TCP connection without emitting any HTTP/2 frame.
#
# This is a robustness expectation, not a spec mandate. RFC 8441 defines
# only :protocol = "websocket" and says nothing about how a server reacts to
# an unknown-but-syntactically-valid value, and such a request is NOT
# "malformed" under RFC 9113 §8.1.1 — whose list is missing/prohibited/
# duplicated pseudo-header fields and invalid field syntax, not an
# unsupported pseudo-header value. So no clause is violated. Even so,
# silently closing the whole TCP connection with no HTTP/2 frame is a poor
# choice: rejecting just the offending stream with RST_STREAM (or answering
# with a 4xx/501) would be more robust and leave the connection usable.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "wptserve only implements extended CONNECT with :protocol=websocket. "
        "For an unknown-but-valid :protocol value the recv loop's "
        "ProtocolError branch closes the TCP connection without emitting any "
        "HTTP/2 frame. RFC 8441 does not define a reaction to an unsupported "
        ":protocol and RFC 9113 §8.1.1 does not class it as malformed, so no "
        "clause is violated — but dropping the whole connection silently is "
        "poor robustness; RST_STREAM on the offending stream (or a 4xx/501) "
        "would be better."
    ),
)
def test_invalid_extended_connect_emits_protocol_error_rst_stream(h2_server):
    with H2RawClient(h2_server.host, h2_server.port) as client:
        client.conn.send_headers(
            stream_id=1,
            headers=[
                (":method", "CONNECT"),
                (":scheme", "https"),
                (":authority", f"{client.host}:{client.port}"),
                (":path", "/"),
                (":protocol", "not-websocket"),
            ],
            end_stream=False,
        )
        client.send_outbound()
        events = client.drain_events(
            until_predicate=lambda e: (
                isinstance(e, ConnectionTerminated) or
                (isinstance(e, StreamReset) and e.stream_id == 1)
            ),
            total_timeout=5.0,
        )

    resets = [
        e for e in events
        if isinstance(e, StreamReset) and e.stream_id == 1
    ]
    assert resets, (
        "Server emitted no RST_STREAM on the unsupported extended CONNECT — "
        "it dropped the whole connection with no HTTP/2 frame. No RFC clause "
        "mandates a specific reaction, but RST_STREAM(PROTOCOL_ERROR) on "
        f"stream 1 would be the robust one; got events: "
        f"{[type(e).__name__ for e in events]}"
    )
    assert resets[0].error_code == ErrorCodes.PROTOCOL_ERROR, (
        f"Expected RST_STREAM(PROTOCOL_ERROR), got "
        f"{resets[0].error_code}"
    )
    terminations = [e for e in events if isinstance(e, ConnectionTerminated)]
    # GOAWAY is preferred but server may also just close. If GOAWAY is
    # sent, it must carry PROTOCOL_ERROR.
    if terminations:
        assert terminations[0].error_code == ErrorCodes.PROTOCOL_ERROR, (
            f"Expected PROTOCOL_ERROR GOAWAY, got "
            f"{terminations[0].error_code}"
        )


# -- DATA + RST_STREAM in one frame batch ------------------------------------
#
# When the client sends HEADERS + DATA + RST_STREAM in a single TCP write,
# h2 emits [RequestReceived, DataReceived, StreamReset] from one
# receive_data() call. The recv loop must deliver all three events to the
# stream worker in order. The previous implementation drove cleanup off
# h2's private `streams` dict and ran a `_drain_queue` on any stream that
# h2 considered closed at the end of the batch — which is exactly what
# RST_STREAM produces. The drain wiped the worker's queue (RequestReceived
# included) before the worker had read anything, then a None sentinel was
# put on top, and the worker exited without ever invoking `handle_data`.


def test_handler_observes_data_in_same_batch_as_rst_stream(h2_server):
    received_chunks = []
    handle_data_seen = threading.Event()

    class RecordingHandler:
        # `handle_data` is called by _stream_thread for each DataReceived
        # the worker dequeues. `__call__` is the regular handler entry
        # point, invoked from finish_handling — it never runs here because
        # RST_STREAM aborts the worker before stream_ended fires.

        def handle_data(self, frame, request, response):
            received_chunks.append(frame.data)
            handle_data_seen.set()

        def __call__(self, request, response):
            pass

    h2_server.router.register("POST", "/record-body", RecordingHandler())

    # Capture every receive_data call's return value to assert that all
    # three events arrived in a single batch. If TCP chunking split the
    # frames across recv calls, the regression (the closed-id sweep
    # removed in commit e378c813db) wouldn't trigger and this test would
    # silently pass for the wrong reason.
    batch_events = []

    # Capture the unbound method before patching. Inside the wrapper,
    # `H2Connection.receive_data` would resolve to the mock installed
    # below, so calling it there recurses until the stack blows. Binding
    # the original now gives a stable handle to the real implementation.
    original_receive_data = H2Connection.receive_data

    def recording_receive_data(self_conn, data):
        events = original_receive_data(self_conn, data)
        batch_events.append(events)
        return events

    with mock.patch.object(
        H2Connection,
        "receive_data",
        autospec=True,
        side_effect=recording_receive_data,
    ):
        with H2RawClient(h2_server.host, h2_server.port) as client:
            client.conn.send_headers(
                stream_id=1,
                headers=[
                    (":method", "POST"),
                    (":scheme", "https"),
                    (":authority", f"{client.host}:{client.port}"),
                    (":path", "/record-body"),
                ],
                end_stream=False,
            )
            client.conn.send_data(stream_id=1, data=b"body-bytes", end_stream=False)
            client.conn.reset_stream(stream_id=1)
            client.send_outbound()

            # No response will come back — RST aborts the stream — so we
            # block on the side-channel signal instead. If the bug regresses,
            # `handle_data` is never called and the wait times out.
            assert handle_data_seen.wait(timeout=5.0), (
                "handle_data was never invoked; the recv loop may be draining "
                "the worker's queue before it reads (regression of the closed-"
                "id sweep that this branch removed)."
            )

    assert received_chunks == [b"body-bytes"], (
        f"Expected [b'body-bytes'], got {received_chunks}"
    )

    # Assert that at least one receive_data call on the *server* side returned
    # a batch with 3+ events including a StreamReset. This confirms HEADERS,
    # DATA, and RST_STREAM arrived in one TCP read — the condition the
    # regression depends on. If they were split across reads, no single batch
    # would have 3 events and the test would miss the intended code path.
    rich_batches = [
        evts for evts in batch_events
        if len(evts) >= 3 and any(isinstance(e, StreamReset) for e in evts)
    ]
    assert rich_batches, (
        "No single receive_data call returned >=3 events with a StreamReset; "
        "frames may have been split across TCP reads so the batch-coalescing "
        "code path was never exercised. "
        f"Batches seen: {[[type(e).__name__ for e in b] for b in batch_events]}"
    )


# -- Inbound flow control credit return --------------------------------------
#
# h2 decrements the inbound stream / connection windows as DATA frames
# arrive. WINDOW_UPDATE frames are only sent when the receiver calls
# acknowledge_received_data() or increment_flow_control_window() on the
# h2 connection. wptserve does neither: it consumes the bytes (the
# handler runs against `request.body`) but never credits them back.
#
# RFC 9113 §6.9.1 describes credit return as the normal consequence of
# consuming data ("sends a WINDOW_UPDATE frame as it consumes data and
# frees up space in flow-control windows"). §5.2.1 leaves the exact
# algorithm to the receiver, but §5.2.2 requires endpoints to "read
# and process HTTP/2 frames from the TCP receive buffer as soon as
# data is available." A receiver that consumes the data — and thus
# satisfies §5.2.2 — without ever crediting it stalls any sender with
# more than 65,535 bytes to deliver on a single stream, even though
# the request is well-formed and the handler has completed.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "wptserve consumes inbound DATA (the handler reads the body) "
        "but never returns flow-control credit on H2 streams. A POST "
        "body larger than the 65,535-byte initial window stalls "
        "mid-send because no WINDOW_UPDATE arrives — even though the "
        "spec model (RFC 9113 §6.9.1) assumes credit follows "
        "consumption."
    ),
)
def test_large_post_body_returns_flow_control_credit(h2_server):
    @wptserve.handlers.handler
    def echo_size(request, response):
        response.content = str(len(request.body)).encode("ascii")

    h2_server.router.register("POST", "/echo-size", echo_size)

    body = b"x" * 100_000  # well past the 65,535-byte initial window
    with H2RawClient(h2_server.host, h2_server.port) as client:
        client.conn.send_headers(
            stream_id=1,
            headers=[
                (":method", "POST"),
                (":scheme", "https"),
                (":authority", f"{client.host}:{client.port}"),
                (":path", "/echo-size"),
                ("content-length", str(len(body))),
            ],
            end_stream=False,
        )
        # Send the body in chunks, respecting send-side flow control.
        # local_flow_control_window tracks how many bytes h2 will let us
        # send before needing a WINDOW_UPDATE from the peer. The deadline
        # is tight: the stall is deterministic — once the window is at 0
        # with no WINDOW_UPDATE in flight, none is coming.
        sent = 0
        deadline = time.monotonic() + 2.0
        while sent < len(body):
            if time.monotonic() > deadline:
                pytest.fail(
                    f"Sending body timed out after {sent}/{len(body)} "
                    "bytes — server didn't return flow-control credit"
                )
            client.send_outbound()
            client.drain_events(total_timeout=0.2)  # absorb WINDOW_UPDATE
            available = client.conn.local_flow_control_window(stream_id=1)
            if available == 0:
                continue
            chunk = body[sent:sent + min(available, 16384)]
            client.conn.send_data(stream_id=1, data=chunk)
            sent += len(chunk)
        client.conn.send_data(stream_id=1, data=b"", end_stream=True)
        client.send_outbound()

        events = client.drain_events(
            until_predicate=lambda e: isinstance(e, StreamEnded) and e.stream_id == 1,
            total_timeout=2.0,
        )

    body_chunks = [
        e.data for e in events
        if isinstance(e, DataReceived) and e.stream_id == 1
    ]
    assert b"".join(body_chunks) == str(len(body)).encode("ascii"), (
        f"Server didn't echo the expected body size. Got: "
        f"{b''.join(body_chunks)!r}"
    )


# -- GOAWAY graceful completion of in-flight requests ------------------------
#
# RFC 9113 §6.8 *permits* — but does not require — a GOAWAY receiver
# to keep processing streams numbered ≤ last_stream_id to completion:
# "Activity on streams numbered lower than or equal to the last stream
# identifier might still complete successfully." The "might" is
# deliberate; the spec only forbids opening *new* streams after a peer
# GOAWAY and mandates minimal frame processing for compression /
# flow-control accounting. Aborting in-flight streams is permitted.
#
# Even so, today's recv loop forecloses the graceful path entirely:
# on ConnectionTerminated it sets `terminated = True` and floods every
# in-flight stream's queue with the terminator. The workers break on
# it before the stream's remaining DATA/StreamEnded arrive, so no
# response goes out for an in-flight request whose client has signalled
# last_stream_id ≥ that stream — even though the client explicitly
# invited completion.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "When the server receives GOAWAY mid-stream, the recv loop "
        "floods every in-flight stream's queue with the "
        "ConnectionTerminated event. Workers break on the terminator "
        "before their stream's remaining DATA/StreamEnded can be "
        "processed, so in-flight responses are dropped. RFC 9113 §6.8 "
        "permits — but doesn't require — completing streams ≤ "
        "last_stream_id; this code path forecloses that option even "
        "when the client has explicitly signalled it would like the "
        "in-flight stream to finish."
    ),
)
def test_inflight_request_completes_after_client_goaway(h2_server):
    @wptserve.handlers.handler
    def echo_body(request, response):
        response.content = b"got: " + (request.body or b"")

    h2_server.router.register("POST", "/echo", echo_body)

    with H2RawClient(h2_server.host, h2_server.port) as client:
        client.conn.send_headers(
            stream_id=1,
            headers=[
                (":method", "POST"),
                (":scheme", "https"),
                (":authority", f"{client.host}:{client.port}"),
                (":path", "/echo"),
            ],
            end_stream=False,
        )
        client.send_outbound()
        # Client decides to close, but still finishes its in-flight
        # stream. Per RFC 9113 §6.8, stream 1 (≤ last_stream_id) "might
        # still complete successfully" — the server should send the
        # response before tearing down.
        #
        # Send the GOAWAY at the byte level rather than through h2's
        # client state machine: calling `client.conn.close_connection`
        # would transition the local connection to CLOSED and make h2
        # refuse the subsequent `send_data` we still need to issue.
        goaway = GoAwayFrame(
            stream_id=0,
            last_stream_id=1,
            error_code=ErrorCodes.NO_ERROR,
        )
        client.send_raw(goaway.serialize())
        client.conn.send_data(stream_id=1, data=b"hello", end_stream=True)
        client.send_outbound()

        events = client.drain_events(
            until_predicate=lambda e: isinstance(e, StreamEnded) and e.stream_id == 1,
            total_timeout=3.0,
        )

    body = b"".join(
        e.data for e in events
        if isinstance(e, DataReceived) and e.stream_id == 1
    )
    assert b"got: hello" in body, (
        f"Stream 1 was in-flight when GOAWAY arrived; per RFC 9113 "
        f"§6.8 it should complete. Got events: "
        f"{[type(e).__name__ for e in events]}"
    )


# -- Small POST body lost in buffered pipe -----------------------------------
#
# _stream_thread opens its DATA-forwarding pipe with the binary mode
# default buffer (`os.fdopen(pipe_wfile, 'wb')`, no buffering arg). The
# DataReceived branch does `wfile.write(frame.data)` but never flushes;
# `finish_handling` then runs while the bytes are still parked in the
# `BufferedWriter`. When the handler reads `request.body`, it gets
# whatever has reached the pipe — for any body smaller than the writer
# buffer (io.DEFAULT_BUFFER_SIZE, 8 KiB) that's zero bytes. The wfile
# is only closed (which flushes) by `cleanup()` in the finally, which
# doesn't run until after finish_handling has returned.
#
# The websocket sibling (`_stream_ws_thread`) opens its wfile with
# `os.fdopen(wfile, 'wb', 0)` — unbuffered — so this bug is specific to
# the regular request path.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "_stream_thread's wfile is buffered (no explicit buffering=0 "
        "arg on os.fdopen). For a POST body smaller than the buffer, "
        "the bytes never reach the rfile pipe before finish_handling "
        "runs, so the handler sees request.body as empty."
    ),
)
def test_small_post_body_reaches_handler(h2_server):
    @wptserve.handlers.handler
    def echo_size(request, response):
        response.content = str(len(request.body)).encode("ascii")

    h2_server.router.register("POST", "/echo-size-small", echo_size)

    # Well under both io.DEFAULT_BUFFER_SIZE (8 KiB) and the 65,535-byte
    # initial flow-control window: this body fits in one DATA frame and
    # in the buffered writer.
    body = b"hello-small-body"
    with H2RawClient(h2_server.host, h2_server.port) as client:
        client.conn.send_headers(
            stream_id=1,
            headers=[
                (":method", "POST"),
                (":scheme", "https"),
                (":authority", f"{client.host}:{client.port}"),
                (":path", "/echo-size-small"),
                ("content-length", str(len(body))),
            ],
            end_stream=False,
        )
        client.conn.send_data(stream_id=1, data=body, end_stream=True)
        client.send_outbound()

        events = client.drain_events(
            until_predicate=lambda e: isinstance(e, StreamEnded) and e.stream_id == 1,
            total_timeout=3.0,
        )

    body_chunks = [
        e.data for e in events
        if isinstance(e, DataReceived) and e.stream_id == 1
    ]
    assert b"".join(body_chunks) == str(len(body)).encode("ascii"), (
        f"Server didn't echo the expected body size. Got: "
        f"{b''.join(body_chunks)!r} (events: "
        f"{[type(e).__name__ for e in events]})"
    )


# -- recv(0) collapses connection on legal INITIAL_WINDOW_SIZE=0 -------------
#
# handle_one_request uses `connection.remote_settings.initial_window_size`
# as the `socket.recv(...)` buffer size — which conflates the per-stream
# send flow-control window (an h2-level concept) with the TCP read
# buffer size. RFC 9113 §6.5.2 allows INITIAL_WINDOW_SIZE in [0, 2^31-1];
# zero is a legitimate value (a receiver may want to gate all DATA on
# explicit WINDOW_UPDATEs). When the client advertises zero, the next
# recv call becomes `recv(0)`, which returns b'' immediately on both
# plain and TLS sockets. The loop reads that as EOF and tears down a
# perfectly healthy connection, even though the client never closed
# anything.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "handle_one_request passes remote_settings.initial_window_size "
        "to socket.recv() as the buffer size. A client that legally "
        "advertises INITIAL_WINDOW_SIZE=0 (RFC 9113 §6.5.2) causes the "
        "next recv(0) to return b'', which the loop misreads as EOF "
        "and closes the connection."
    ),
)
def test_zero_initial_window_size_does_not_collapse_connection(h2_server):
    @wptserve.handlers.handler
    def hello(request, response):
        response.content = b"hello"

    h2_server.router.register("GET", "/hello-zero-window", hello)

    with H2RawClient(h2_server.host, h2_server.port) as client:
        # Send a SETTINGS frame that lowers INITIAL_WINDOW_SIZE to 0.
        # h2 prepares the frame; send_outbound() flushes it on the wire.
        client.conn.update_settings({
            SettingCodes.INITIAL_WINDOW_SIZE: 0,
        })
        client.send_outbound()

        # h2 doesn't apply the new window to peer's send-side until the
        # server acks, but the server's recv() buffer size is read off
        # remote_settings as soon as the frame is processed — that's
        # the path under test.
        client.conn.send_headers(
            stream_id=1,
            headers=[
                (":method", "GET"),
                (":scheme", "https"),
                (":authority", f"{client.host}:{client.port}"),
                (":path", "/hello-zero-window"),
            ],
            end_stream=True,
        )
        client.send_outbound()

        events = client.drain_events(
            until_predicate=lambda e: isinstance(e, StreamEnded) and e.stream_id == 1,
            total_timeout=3.0,
        )

    body = b"".join(
        e.data for e in events
        if isinstance(e, DataReceived) and e.stream_id == 1
    )
    assert body == b"hello", (
        f"Server tore down the connection on a legal SETTINGS frame "
        f"(INITIAL_WINDOW_SIZE=0). Events: "
        f"{[type(e).__name__ for e in events]}"
    )


# -- HEADERS after a client GOAWAY -------------------------------------------
#
# A client that sends GOAWAY and then opens a new stream is misbehaving.
# h2's connection state machine moves to CLOSED on receipt of the peer
# GOAWAY, so the trailing HEADERS raise ProtocolError out of receive_data
# rather than yielding a RequestReceived. The recv loop's `except
# ProtocolError` branch then tears the connection down.
#
# This is the real-wire counterpart to the unit test
# test_recv_loop_skips_request_after_connection_terminated_in_same_batch:
# that test mocks receive_data to fabricate a [ConnectionTerminated,
# RequestReceived] batch (which the recv loop guards with an assert),
# while this test confirms that real h2 never produces such a batch — it
# raises instead. Together they show the assert is a tripwire for a
# scenario the wire cannot produce. The value here is the misbehaving-
# client guarantee: HEADERS-after-GOAWAY must tear the connection down
# promptly without running the handler and without hanging.


def test_headers_after_goaway_closes_connection_without_handler(h2_server):
    handler_invoked = threading.Event()

    class SideChannelHandler:
        # handle_headers fires in _stream_thread the instant a worker
        # dequeues a RequestReceived; __call__ fires from finish_handling
        # on StreamEnded. Either firing means the server processed the
        # post-GOAWAY HEADERS as a request, which it must not.
        def handle_headers(self, frame, request, response):
            handler_invoked.set()

        def __call__(self, request, response):
            handler_invoked.set()

    h2_server.router.register("GET", "/goaway-then-headers", SideChannelHandler())

    with H2RawClient(h2_server.host, h2_server.port) as client:
        # Build a raw HEADERS frame for stream 1. END_HEADERS marks the
        # block complete (no CONTINUATION); END_STREAM makes it a complete
        # request, so absent the GOAWAY the server would run the handler.
        encoder = hpack.Encoder()
        header_block = encoder.encode([
            (":method", "GET"),
            (":scheme", "https"),
            (":authority", f"{client.host}:{client.port}"),
            (":path", "/goaway-then-headers"),
        ])
        headers_frame = HeadersFrame(stream_id=1, data=header_block)
        headers_frame.flags.add("END_HEADERS")
        headers_frame.flags.add("END_STREAM")

        # GOAWAY(last_stream_id=0) then HEADERS(stream=1) in one TCP write.
        # The server's h2 connection is CLOSED by the GOAWAY, so the
        # HEADERS raise ProtocolError out of receive_data.
        goaway = GoAwayFrame(
            stream_id=0,
            last_stream_id=0,
            error_code=ErrorCodes.NO_ERROR,
        )
        client.send_raw(goaway.serialize() + headers_frame.serialize())

        # The server must close the connection promptly. drain_until_close
        # returns on clean EOF; if the server hung, it raises socket.timeout
        # and the test fails loudly rather than blocking forever.
        client.drain_until_close(timeout=5.0)

    assert not handler_invoked.is_set(), (
        "Handler ran for a stream opened after the client's GOAWAY. The "
        "server should reject the post-GOAWAY HEADERS at the h2 layer "
        "(ProtocolError) and tear the connection down without dispatching "
        "to a handler."
    )


# -- SETTINGS acknowledgement ------------------------------------------------
#
# RFC 9113 §6.5.3: "Once all values have been processed, the recipient MUST
# immediately emit a SETTINGS frame with the ACK flag set." h2 queues that
# ACK internally when receive_data() processes an inbound SETTINGS frame,
# but the recv loop never flushes connection-level outbound bytes — it
# routes stream events to workers and only the workers call data_to_send().
# A SETTINGS frame that arrives on a connection with no in-flight request is
# therefore processed but never acknowledged on the wire.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The recv loop never flushes connection-level outbound bytes after "
        "receive_data(), so the SETTINGS ACK that h2 queues for an inbound "
        "SETTINGS frame is never sent. RFC 9113 §6.5.3 requires the "
        "recipient to immediately emit a SETTINGS frame with the ACK flag "
        "set."
    ),
)
def test_settings_frame_is_acknowledged(h2_server):
    with H2RawClient(h2_server.host, h2_server.port) as client:
        # The preface SETTINGS was already sent by connect(); send a fresh
        # SETTINGS frame and wait for the server to acknowledge it. On the
        # client side, the server's ACK surfaces as a SettingsAcknowledged
        # event.
        client.conn.update_settings({SettingCodes.MAX_FRAME_SIZE: 32768})
        client.send_outbound()

        events = client.drain_events(
            until_predicate=lambda e: isinstance(e, SettingsAcknowledged),
            total_timeout=2.0,
        )

    acks = [e for e in events if isinstance(e, SettingsAcknowledged)]
    assert acks, (
        "Server never acknowledged the client's SETTINGS frame. Per RFC "
        "9113 §6.5.3 it must immediately emit a SETTINGS frame with the ACK "
        f"flag set. Events seen: {[type(e).__name__ for e in events]}"
    )


# -- PING acknowledgement ----------------------------------------------------
#
# RFC 9113 §6.7: "Receivers of a PING frame that does not include an ACK
# flag MUST send a PING frame with the ACK flag set in response, with an
# identical payload." As with SETTINGS, h2 queues the PING ACK when
# receive_data() processes the inbound PING, but the recv loop never flushes
# connection-level outbound bytes, so the ACK never reaches the wire on a
# connection with no in-flight request driving a worker's data_to_send().


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The recv loop never flushes connection-level outbound bytes after "
        "receive_data(), so the PING ACK h2 queues for an inbound PING is "
        "never sent. RFC 9113 §6.7 requires a PING ACK with an identical "
        "payload."
    ),
)
def test_ping_is_acknowledged(h2_server):
    payload = b"pingpong"  # PING payload is exactly 8 octets
    with H2RawClient(h2_server.host, h2_server.port) as client:
        client.conn.ping(payload)
        client.send_outbound()

        events = client.drain_events(
            until_predicate=lambda e: isinstance(e, PingAckReceived),
            total_timeout=2.0,
        )

    acks = [e for e in events if isinstance(e, PingAckReceived)]
    assert acks, (
        "Server never acknowledged the client's PING frame. Per RFC 9113 "
        f"§6.7 it must respond with a PING ACK. Events seen: "
        f"{[type(e).__name__ for e in events]}"
    )
    assert acks[0].ping_data == payload, (
        f"PING ACK echoed the wrong payload: expected {payload!r}, got "
        f"{acks[0].ping_data!r}"
    )


# -- GOAWAY on a connection error --------------------------------------------
#
# RFC 9113 §5.4.1: "An endpoint that encounters a connection error SHOULD
# first send a GOAWAY frame (Section 6.8) with the stream identifier of the
# last stream that it successfully received from its peer." When the
# server's h2 detects a connection error it queues exactly that GOAWAY and
# raises ProtocolError out of receive_data. The recv loop's `except
# ProtocolError` branch only logs and exits — it never flushes the queued
# GOAWAY — so the peer sees a bare TCP EOF with no error code.
#
# A connection-specific header field (RFC 9113 §8.2.2 forbids these) is a
# clean trigger: the client must send it raw, because its own h2 would
# refuse to serialise it.
#
# This is a SHOULD, not a MUST: §6.8 explicitly permits closing without a
# GOAWAY for a misbehaving peer (and a connection-specific header field is
# exactly such a peer), so the xfail documents the preferred behaviour, not
# a spec violation.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "On a connection error the server's h2 queues a GOAWAY and raises "
        "ProtocolError out of receive_data, but the recv loop's `except "
        "ProtocolError` branch logs and exits without flushing it, so the "
        "peer sees a bare TCP EOF with no error code. RFC 9113 §5.4.1 says an "
        "endpoint SHOULD (not MUST) send a GOAWAY carrying the error code, "
        "and §6.8 permits closing without one for a misbehaving peer — so "
        "this is the preferred-behaviour gap, not a violation."
    ),
)
def test_connection_error_emits_goaway(h2_server):
    with H2RawClient(h2_server.host, h2_server.port) as client:
        # HEADERS carrying a connection-specific header field. The server's
        # h2 treats this as a connection error (PROTOCOL_ERROR): it queues a
        # GOAWAY and raises ProtocolError out of receive_data. Sent raw
        # because the client's own h2 would refuse to serialise it.
        encoder = hpack.Encoder()
        header_block = encoder.encode([
            (":method", "GET"),
            (":scheme", "https"),
            (":authority", f"{client.host}:{client.port}"),
            (":path", "/"),
            ("connection", "close"),
        ])
        headers_frame = HeadersFrame(stream_id=1, data=header_block)
        headers_frame.flags.add("END_HEADERS")
        headers_frame.flags.add("END_STREAM")
        client.send_raw(headers_frame.serialize())

        events = client.drain_events(
            until_predicate=lambda e: isinstance(e, ConnectionTerminated),
            total_timeout=2.0,
        )

    terminations = [e for e in events if isinstance(e, ConnectionTerminated)]
    assert terminations, (
        "Server closed the connection on a protocol error without sending a "
        "GOAWAY — the client saw a bare TCP EOF. RFC 9113 §5.4.1 says a "
        "connection error should be reported with a GOAWAY carrying the "
        f"error code. Events seen: {[type(e).__name__ for e in events]}"
    )
    assert terminations[0].error_code == ErrorCodes.PROTOCOL_ERROR, (
        f"Expected GOAWAY(PROTOCOL_ERROR), got {terminations[0].error_code}"
    )


# -- Connection-level flow-control credit across streams ---------------------
#
# RFC 9113 §6.9.1: a receiver "sends a WINDOW_UPDATE frame as it consumes
# data and frees up space in flow-control windows." The connection-level
# inbound window starts at 65,535 octets (§6.9.2) and is shared by every
# stream; only a WINDOW_UPDATE on stream 0 replenishes it. wptserve consumes
# inbound DATA (each handler reads request.body) but never returns credit on
# any window, so the connection window monotonically drains across streams.
#
# The existing stream-level test (test_large_post_body_returns_flow_control
# _credit) can't isolate this: a single 100 KB body hits the stream and
# connection windows at the same 65,535 boundary. Here each stream's body is
# well under the per-stream window, so only the *connection* window can stall
# the third stream once the cumulative bytes pass 65,535.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "wptserve never returns connection-level flow-control credit "
        "(no WINDOW_UPDATE on stream 0). The 65,535-byte connection window "
        "(RFC 9113 §6.9.2) is shared across streams and drains monotonically "
        "as DATA is consumed, so a third small POST stalls once the "
        "cumulative body exceeds the window even though each stream is well "
        "under the per-stream window. Crediting timing is implementation-"
        "defined (§5.2.1) and §5.2.2's read-promptly MUST is satisfied; "
        "§6.9.1 only describes credit following consumption — so this is an "
        "interop/quality gap, not a normative violation."
    ),
)
def test_connection_window_credited_across_streams(h2_server):
    @wptserve.handlers.handler
    def echo_size(request, response):
        response.content = str(len(request.body)).encode("ascii")

    h2_server.router.register("POST", "/echo-size-conn", echo_size)

    body = b"y" * 30_000  # under the 65,535 per-stream window
    # Three streams → 90,000 cumulative bytes, past the 65,535-byte
    # connection window. Streams 1 and 3 fit; stream 5 stalls once the
    # connection window is exhausted.
    stream_ids = [1, 3, 5]

    with H2RawClient(h2_server.host, h2_server.port) as client:
        for stream_id in stream_ids:
            client.conn.send_headers(
                stream_id=stream_id,
                headers=[
                    (":method", "POST"),
                    (":scheme", "https"),
                    (":authority", f"{client.host}:{client.port}"),
                    (":path", "/echo-size-conn"),
                    ("content-length", str(len(body))),
                ],
                end_stream=False,
            )

            # Send the body respecting send-side flow control.
            # local_flow_control_window is min(stream, connection) window;
            # once the connection window is drained it returns 0 and stays
            # there because no WINDOW_UPDATE arrives.
            sent = 0
            deadline = time.monotonic() + 2.0
            while sent < len(body):
                if time.monotonic() > deadline:
                    pytest.fail(
                        f"Stream {stream_id} stalled after {sent}/{len(body)} "
                        "bytes — the connection-level window drained (cumulative "
                        "bytes across streams exceeded 65,535) and no "
                        "WINDOW_UPDATE on stream 0 ever replenished it"
                    )
                client.send_outbound()
                client.drain_events(total_timeout=0.2)  # absorb any WINDOW_UPDATE
                available = client.conn.local_flow_control_window(stream_id)
                if available == 0:
                    continue
                chunk = body[sent:sent + min(available, 16384)]
                client.conn.send_data(stream_id=stream_id, data=chunk)
                sent += len(chunk)
            client.conn.send_data(stream_id=stream_id, data=b"", end_stream=True)
            client.send_outbound()

            events = client.drain_events(
                until_predicate=lambda e: isinstance(e, StreamEnded) and e.stream_id == stream_id,
                total_timeout=2.0,
            )
            # Assert the stream *completed*, not its body content: a small
            # POST body may arrive empty at the handler because of the
            # separate buffered-pipe bug (test_small_post_body_reaches_
            # handler). What this test isolates is whether the third stream
            # can send its body at all once the connection window is drained.
            assert any(
                isinstance(e, StreamEnded) and e.stream_id == stream_id
                for e in events
            ), (
                f"Stream {stream_id} did not complete (no StreamEnded). "
                f"Events: {[type(e).__name__ for e in events]}"
            )


# -- Extended CONNECT missing :scheme ----------------------------------------
#
# RFC 8441 §4: "On requests that contain the :protocol pseudo-header field,
# the :scheme and :path pseudo-header fields of the target URI [...] MUST
# also be included." An extended-CONNECT request carrying :protocol but no
# :scheme is therefore malformed, and RFC 9113 §8.1.1 requires malformed
# requests to be treated as a stream error of type PROTOCOL_ERROR, i.e.
# RST_STREAM on the affected stream.
#
# Sibling of test_invalid_extended_connect_emits_protocol_error_rst_stream
# (bad :protocol value); this one omits :scheme entirely. Sent raw because
# the client's own h2 would refuse to serialise an extended CONNECT without
# :scheme.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "wptserve does not reject an extended-CONNECT request that omits "
        ":scheme. RFC 8441 §4 requires :scheme (and :path) on requests with "
        ":protocol, and RFC 9113 §8.1.1 makes a malformed request a stream "
        "error of type PROTOCOL_ERROR — the server should RST_STREAM, not "
        "silently close or process the request."
    ),
)
def test_extended_connect_missing_scheme_emits_protocol_error(h2_server):
    with H2RawClient(h2_server.host, h2_server.port) as client:
        encoder = hpack.Encoder()
        header_block = encoder.encode([
            (":method", "CONNECT"),
            (":protocol", "websocket"),
            (":authority", f"{client.host}:{client.port}"),
            (":path", "/"),
            # :scheme deliberately omitted — the RFC 8441 §4 violation.
        ])
        headers_frame = HeadersFrame(stream_id=1, data=header_block)
        headers_frame.flags.add("END_HEADERS")
        client.send_raw(headers_frame.serialize())

        events = client.drain_events(
            until_predicate=lambda e: (
                isinstance(e, ConnectionTerminated) or
                (isinstance(e, StreamReset) and e.stream_id == 1)
            ),
            total_timeout=5.0,
        )

    resets = [
        e for e in events
        if isinstance(e, StreamReset) and e.stream_id == 1
    ]
    assert resets, (
        "Server emitted no RST_STREAM for an extended CONNECT missing "
        ":scheme. Per RFC 8441 §4 + RFC 9113 §8.1.1 the right reaction is "
        f"RST_STREAM(PROTOCOL_ERROR) on stream 1; got events: "
        f"{[type(e).__name__ for e in events]}"
    )
    assert resets[0].error_code == ErrorCodes.PROTOCOL_ERROR, (
        f"Expected RST_STREAM(PROTOCOL_ERROR), got {resets[0].error_code}"
    )


# -- handle_data hook exception orphans the stream ---------------------------
#
# The regular handler call is wrapped by finish_handling (try/except Exception
# -> response.set_error(500, e), which also logs the traceback), and the H2
# frame_handler entry point is wrapped by Http2WebTestRequestHandler.
# frame_handler. But the per-frame hooks handle_headers and handle_data are
# invoked directly in _stream_thread with no try/except. An exception in one
# of them propagates out of the worker, runs cleanup() in the finally, and
# kills the worker thread (traceback to stderr via threading.excepthook) —
# without sending any frame for the stream.
#
# The connection itself survives (the recv loop runs in another thread and
# reaps the dead worker), so this is correctly stream-scoped, not a
# connection error. But nothing is signalled on the stream: no response, no
# RST_STREAM. RFC 9113 §5.4.2 says a stream error should be conveyed with
# RST_STREAM; here the stream is simply orphaned and the client hangs.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "handle_data / handle_headers hooks are called in _stream_thread "
        "without a try/except (unlike finish_handling and the frame_handler "
        "wrapper). An exception there kills the worker thread silently — no "
        "response and no RST_STREAM on the stream — so the client's stream "
        "hangs. RFC 9113 §5.4.2 expects a stream error to be signalled with "
        "RST_STREAM."
    ),
)
def test_handle_data_exception_does_not_orphan_stream(h2_server):
    class RaisingDataHandler:
        # handle_data runs in _stream_thread for each DataReceived, before
        # finish_handling. Raising here kills the worker before any response
        # is produced.
        def handle_data(self, frame, request, response):
            raise RuntimeError("handle_data bug")

        def __call__(self, request, response):
            # Never reached — the worker dies on the DATA frame above.
            pass

    h2_server.router.register("POST", "/raise-in-handle-data", RaisingDataHandler())

    with H2RawClient(h2_server.host, h2_server.port) as client:
        client.conn.send_headers(
            stream_id=1,
            headers=[
                (":method", "POST"),
                (":scheme", "https"),
                (":authority", f"{client.host}:{client.port}"),
                (":path", "/raise-in-handle-data"),
            ],
            end_stream=False,
        )
        client.conn.send_data(stream_id=1, data=b"trigger", end_stream=True)
        client.send_outbound()

        events = client.drain_events(
            until_predicate=lambda e: (
                (isinstance(e, StreamEnded) and e.stream_id == 1) or
                (isinstance(e, StreamReset) and e.stream_id == 1)
            ),
            total_timeout=2.0,
        )

    # A handler error on the stream should produce a stream-scoped signal —
    # a response (StreamEnded) or an RST_STREAM — not leave the client
    # waiting forever.
    resolved = [
        e for e in events
        if isinstance(e, (StreamEnded, StreamReset)) and e.stream_id == 1
    ]
    assert resolved, (
        "handle_data raised, but the server sent neither a response nor an "
        "RST_STREAM on stream 1 — the stream is orphaned and the client "
        f"would hang. Events: {[type(e).__name__ for e in events]}"
    )


# -- Idle connection is never closed by the server ---------------------------
#
# This documents a MAY, not a MUST: RFC 9113 §6.5.3 lets a sender of SETTINGS
# treat a missing ACK within SETTINGS_TIMEOUT as a connection error, and a
# server is generally free to reclaim idle connections. wptserve sets no idle
# timeout on self.request, so a client that completes the preface, never ACKs
# the server's SETTINGS, and issues no request pins the connection's handler
# thread on recv() indefinitely.
#
# This is not a harmful hang — the thread unblocks and cleans up as soon as
# the client disconnects — and HTTP/2 connections are persistent by design,
# so keeping the connection open is a legitimate choice. The test records the
# absence of any idle reclamation as a known gap (resource exhaustion under
# many silent clients), and stays xfail unless an idle timeout is added.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "wptserve sets no idle timeout on the H2 connection socket, so a "
        "client that completes the preface and then stays silent (no request, "
        "no SETTINGS ACK) keeps the handler thread blocked on recv() until it "
        "disconnects. RFC 9113 §6.5.3 only permits (MAY) tearing such a "
        "connection down via SETTINGS_TIMEOUT, so this documents a missing "
        "optional reclamation, not a spec violation."
    ),
)
def test_idle_connection_is_eventually_closed(h2_server):
    with H2RawClient(h2_server.host, h2_server.port) as client:
        # Absorb the server's initial SETTINGS so the read below reflects
        # idle behaviour rather than the handshake bytes.
        client.drain_events(
            until_predicate=lambda e: isinstance(e, RemoteSettingsChanged),
            total_timeout=2.0,
        )

        # The client now goes silent. A server that reclaims idle connections
        # would close, surfacing as EOF (recv -> b''). With no idle timeout
        # the read blocks until it times out client-side.
        client.tls_sock.settimeout(2.0)
        try:
            data = client.tls_sock.recv(65535)
        except (socket.timeout, TimeoutError):
            pytest.fail(
                "Server kept the idle connection open: a silent client with "
                "no request and no SETTINGS ACK was not reclaimed within the "
                "timeout (no SETTINGS_TIMEOUT / idle close)."
            )

    assert data == b"", (
        f"Expected EOF from an idle-connection close, got {data!r}"
    )


# -- Clean EOF after the preface releases the connection ---------------------
#
# A client that completes the H2 preface and then half-closes (TCP FIN, no
# GOAWAY) makes the server's recv return b''. handle_one_request's recv loop
# treats that as EOF (`if data == b'': break`) and returns, releasing the
# connection. The server's listening SSLSocket uses suppress_ragged_eofs (the
# wrap_socket default), so the missing TLS close_notify surfaces as a clean
# b'' rather than an SSLError — the loop's clean-EOF branch, not the OSError
# branch. This is the common, well-behaved disconnect: it must not log an
# error, must close the server's side, and must leave the server able to
# serve the next connection. The likely regression here is a recv loop that
# treats a short read or b'' as something other than a clean EOF.


def test_clean_eof_after_preface_releases_connection(h2_server, caplog):
    @wptserve.handlers.handler
    def hello(request, response):
        response.content = b"hello-after-eof"

    h2_server.router.register("GET", "/hello-after-eof", hello)

    with caplog.at_level(logging.ERROR):
        # Client 1: finish the preface, then half-close without any frames.
        with H2RawClient(h2_server.host, h2_server.port) as client1:
            # Wait until the server has sent its SETTINGS (RemoteSettingsChanged
            # on the client) so we exercise a clean EOF on an established
            # connection rather than racing the handshake.
            client1.drain_events(
                until_predicate=lambda e: isinstance(e, RemoteSettingsChanged),
                total_timeout=2.0,
            )
            # Half-close: TCP FIN on the write side. The server's blocked recv
            # returns b'' and the recv loop breaks.
            client1.tls_sock.shutdown(socket.SHUT_WR)
            # The server should reciprocate by closing its side. drain_until_
            # close returns on that EOF and raises socket.timeout if the server
            # hung on the half-closed connection instead of tearing it down.
            client1.drain_until_close(timeout=3.0)

        # Client 2: a fresh connection must be served normally, proving the
        # clean EOF released the handler without wedging the server.
        with H2RawClient(h2_server.host, h2_server.port) as client2:
            client2.conn.send_headers(
                stream_id=1,
                headers=[
                    (":method", "GET"),
                    (":scheme", "https"),
                    (":authority", f"{client2.host}:{client2.port}"),
                    (":path", "/hello-after-eof"),
                ],
                end_stream=True,
            )
            client2.send_outbound()
            events = client2.drain_events(
                until_predicate=lambda e: isinstance(e, StreamEnded) and e.stream_id == 1,
                total_timeout=3.0,
            )

    body = b"".join(
        e.data for e in events
        if isinstance(e, DataReceived) and e.stream_id == 1
    )
    assert body == b"hello-after-eof", (
        "Server did not serve a fresh connection after a clean client EOF. "
        f"Events: {[type(e).__name__ for e in events]}"
    )

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, (
        "Clean client EOF (half-close after the preface) produced ERROR-level "
        f"log records: {[r.getMessage() for r in errors]}"
    )
