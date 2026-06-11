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
from wptserve.server import Http2WebTestRequestHandler


def _bare_handler():
    """Return an `Http2WebTestRequestHandler` instance whose
    `BaseHTTPRequestHandler.__init__` chain has NOT been run.

    Tests then assign whichever attributes the method under test reads.
    """
    handler = Http2WebTestRequestHandler.__new__(Http2WebTestRequestHandler)
    handler.logger = wptserve.logger.get_logger()
    return handler


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
