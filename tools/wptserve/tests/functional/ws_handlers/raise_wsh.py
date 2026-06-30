"""A websocket-over-HTTP/2 handler whose data phase raises.

Used by test_ws_handler_exception_resets_stream to exercise the path where a
ws handler raises an exception other than StreamClosedError / ProtocolError.
web_socket_transfer_data records (via the WPT_WS_ORPHAN_MARKER env var) that
it was actually reached — so the test can tell a genuine handler exception
apart from a handshake failure — and then raises.
"""

import os


def web_socket_do_extra_handshake(request):
    pass


def web_socket_transfer_data(request):
    marker = os.environ.get("WPT_WS_ORPHAN_MARKER")
    if marker:
        with open(marker, "w") as fh:
            fh.write("reached")
    raise RuntimeError("ws handler bug")
