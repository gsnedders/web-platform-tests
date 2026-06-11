"""Lifecycle tests for the HTTP/2 request handler.

Each section targets a specific concurrency / framing edge case in
`Http2WebTestRequestHandler`. Tests in this file go to the wire (raw TLS
+ h2.Connection) so what's exercised is the real protocol state machine,
not a mocked surface.
"""

import os

import pytest

import localpaths  # noqa: F401  # populates sys.path

import wptserve.server
import wptserve.utils
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
