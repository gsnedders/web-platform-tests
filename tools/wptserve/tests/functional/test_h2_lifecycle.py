"""Lifecycle tests for the HTTP/2 request handler.

Each section targets a specific concurrency / framing edge case in
`Http2WebTestRequestHandler`. Tests in this file go to the wire (raw TLS
+ h2.Connection) so what's exercised is the real protocol state machine,
not a mocked surface.
"""

import os
import threading
from unittest import mock

import pytest

import localpaths  # noqa: F401  # populates sys.path

import wptserve.server
import wptserve.utils
from h2.connection import H2Connection
from h2.errors import ErrorCodes
from h2.events import ConnectionTerminated
from h2.events import StreamReset

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
                isinstance(e, ConnectionTerminated)
                or (isinstance(e, StreamReset) and e.stream_id == 1)
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
