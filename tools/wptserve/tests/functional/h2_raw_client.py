"""Helpers for HTTP/2 tests that need frame-level control.

The standard tests in this directory use httpx for HTTP/2, which is enough
for high-level behaviour but doesn't expose individual frames. The lifecycle
tests in `test_h2_lifecycle.py` need to send specific frame sequences
(GOAWAY mid-batch, PRIORITY for never-opened streams, CONNECT with custom
:protocol, etc.) and observe what the server emits in response.

`H2RawClient` wraps a TLS socket plus an `h2.Connection` with the
boilerplate for ALPN, the connection preface, and SETTINGS exchange.
"""

import socket
import ssl
import time

import localpaths  # noqa: F401  # adds vendored h2 to sys.path

from h2.config import H2Configuration
from h2.connection import H2Connection


def _client_ssl_context():
    # The wptserve cert is issued for web-platform.test, but the H2 server
    # binds to localhost in tests. Skip cert/hostname verification rather
    # than spoofing SNI — this matches the existing httpx-based tests
    # (httpx.Client(..., verify=False) in base.py).
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(["h2"])
    return context


class H2RawClient:
    """Minimal HTTP/2 client driving a real TLS socket.

    Exposes the underlying `h2.Connection` and socket so individual tests
    can hand-craft frame sequences that httpx wouldn't let them produce.
    """

    def __init__(self, host, port, timeout=10):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock = None
        self.tls_sock = None
        self.conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def connect(self, send_preface=True):
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(self.timeout)
        raw.connect((self.host, self.port))
        self.sock = raw

        context = _client_ssl_context()
        self.tls_sock = context.wrap_socket(raw, server_hostname=self.host)
        assert self.tls_sock.selected_alpn_protocol() == "h2"

        if send_preface:
            self.conn = H2Connection(config=H2Configuration(client_side=True))
            self.conn.initiate_connection()
            self.send_outbound()

    def send_outbound(self):
        """Flush any pending h2 outbound bytes to the wire."""
        data = self.conn.data_to_send()
        if data:
            self.tls_sock.sendall(data)

    def send_raw(self, data):
        """Send raw bytes without going through h2 (for malformed-frame tests)."""
        self.tls_sock.sendall(data)

    def recv_events(self, max_bytes=65535):
        """Read once and feed h2; return the events it produced.

        Returns [] on clean EOF. Raises socket.timeout if the peer is silent.
        """
        data = self.tls_sock.recv(max_bytes)
        if not data:
            return []
        return self.conn.receive_data(data)

    def drain_events(self, until_predicate=None, total_timeout=5.0):
        """Read events until `until_predicate(event)` is true for any event,
        or until the connection closes, or until `total_timeout` elapses.

        Returns the list of all events seen.
        """
        deadline = time.monotonic() + total_timeout
        events = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.tls_sock.settimeout(remaining)
            try:
                new_events = self.recv_events()
            except socket.timeout:
                break
            if not new_events:
                # Clean EOF.
                break
            events.extend(new_events)
            if until_predicate is not None and any(until_predicate(e) for e in new_events):
                break
        return events

    def drain_until_close(self, timeout=5.0):
        """Read events until the server closes the connection (clean EOF).

        Raises socket.timeout if the server doesn't close within `timeout`.
        Use this to assert that the server initiates connection teardown.
        """
        deadline = time.monotonic() + timeout
        events = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout(
                    f"Server did not close the connection within {timeout}s"
                )
            self.tls_sock.settimeout(remaining)
            new_events = self.recv_events()
            if not new_events:
                # recv returned b'' — clean EOF from server.
                return events
            events.extend(new_events)

    def close(self):
        for sock in (self.tls_sock, self.sock):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass
        self.tls_sock = None
        self.sock = None
        self.conn = None
