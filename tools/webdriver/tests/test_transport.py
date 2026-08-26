# mypy: allow-untyped-defs

import socket
import time

import pytest

from webdriver.transport import DEFAULT_TIMEOUT, HTTPWireProtocol


@pytest.fixture
def transport():
    return HTTPWireProtocol("127.0.0.1", 4444)


@pytest.fixture
def restore_default_timeout():
    previous = socket.getdefaulttimeout()
    yield
    socket.setdefaulttimeout(previous)


class FakeSocket:
    def __init__(self):
        self.timeout = None

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeout = value


class FakeConnection:
    """Stub for http.client.HTTPConnection, recording call order."""

    def __init__(self):
        self.timeout = None
        self.sock = FakeSocket()
        self.calls = []

    def request(self, method, url, payload, headers):
        self.calls.append(("request", self.sock.gettimeout()))

    def getresponse(self):
        self.calls.append(("getresponse", self.sock.gettimeout()))
        return "canned-response"


def test_deadline_from_timeout_none(transport):
    assert transport._deadline_from_timeout(None) is None


def test_deadline_from_timeout_round_trip(transport):
    before = time.monotonic_ns()
    deadline = transport._deadline_from_timeout(5)
    after = time.monotonic_ns()

    assert before + int(5 * 1e9) <= deadline <= after + int(5 * 1e9)


def test_timeout_from_deadline_none(transport):
    assert transport._timeout_from_deadline() is None


def test_timeout_from_deadline_remaining(transport):
    transport._deadline = time.monotonic_ns() + int(10 * 1e9)
    remaining = transport._timeout_from_deadline()

    assert 0 < remaining <= 10


def test_timeout_from_deadline_floors_at_epsilon_when_expired(transport):
    # A deadline far in the past.
    transport._deadline = time.monotonic_ns() - int(60 * 1e9)
    remaining = transport._timeout_from_deadline()

    assert remaining > 0
    assert remaining < 1


def test_deadline_sets_and_restores(transport):
    assert transport._deadline is None

    with transport.deadline(5):
        assert transport._deadline is not None

    assert transport._deadline is None


def test_deadline_restores_on_exception(transport):
    assert transport._deadline is None

    with pytest.raises(ValueError):
        with transport.deadline(5):
            assert transport._deadline is not None
            raise ValueError("boom")

    assert transport._deadline is None


def test_nested_deadline_clamps_to_tighter_inner(transport):
    with transport.deadline(10):
        outer_deadline = transport._deadline
        with transport.deadline(1):
            inner_deadline = transport._deadline
            assert inner_deadline < outer_deadline
        # Restores the middle (outer) deadline, not None.
        assert transport._deadline == outer_deadline
    assert transport._deadline is None


def test_nested_deadline_does_not_lengthen_outer(transport):
    with transport.deadline(1):
        outer_deadline = transport._deadline
        with transport.deadline(100):
            # A longer nested deadline must not lengthen the enclosing one.
            assert transport._deadline == outer_deadline
        assert transport._deadline == outer_deadline
    assert transport._deadline is None


def test_nested_deadline_none_child_keeps_real_parent(transport):
    with transport.deadline(5):
        outer_deadline = transport._deadline
        with transport.deadline(None):
            # An infinite child under a real parent must not lengthen it.
            assert transport._deadline == outer_deadline
    assert transport._deadline is None


def test_nested_deadline_real_child_under_none_parent_takes_child(transport):
    with transport.deadline(None):
        assert transport._deadline is None
        with transport.deadline(5):
            assert transport._deadline is not None
        assert transport._deadline is None
    assert transport._deadline is None


def test_effective_timeout_sentinel_with_active_deadline(transport):
    with transport.deadline(10):
        effective = transport._effective_timeout(DEFAULT_TIMEOUT)
        assert 0 < effective <= 10


def test_effective_timeout_sentinel_without_deadline_uses_socket_default(
    transport, restore_default_timeout
):
    socket.setdefaulttimeout(42)
    assert transport._effective_timeout(DEFAULT_TIMEOUT) == 42


def test_effective_timeout_none_overrides_deadline(transport):
    with transport.deadline(10):
        assert transport._effective_timeout(None) is None


def test_effective_timeout_none_without_deadline(transport):
    assert transport._effective_timeout(None) is None


def test_effective_timeout_float_returned_unchanged(transport):
    assert transport._effective_timeout(2.5) == 2.5


def test_effective_timeout_float_overrides_deadline(transport):
    with transport.deadline(10):
        assert transport._effective_timeout(0.5) == 0.5


def test_request_sets_connection_timeout_before_request_call(transport, monkeypatch):
    fake_conn = FakeConnection()
    monkeypatch.setattr(
        type(transport), "connection", property(lambda self: fake_conn)
    )

    with transport.deadline(10):
        transport._request("GET", "status", None)

    assert fake_conn.calls[0][0] == "request"
    # The socket timeout recorded at the time of the request() call must be
    # the resolved effective value, not None/unset (covering the send path).
    assert fake_conn.calls[0][1] is not None
    assert 0 < fake_conn.calls[0][1] <= 10


def test_request_expired_deadline_yields_epsilon_floor_not_zero(transport, monkeypatch):
    fake_conn = FakeConnection()
    monkeypatch.setattr(
        type(transport), "connection", property(lambda self: fake_conn)
    )

    transport._deadline = time.monotonic_ns() - int(60 * 1e9)
    transport._request("GET", "status", None)

    assert fake_conn.timeout > 0
    assert fake_conn.timeout < 1


def test_request_restore_guards_against_concurrent_close(transport, monkeypatch):
    """Test that concurrent close() prevents socket timeout restore onto replacement socket."""
    fake_conn = FakeConnection()
    original_sock = fake_conn.sock

    def patched_getresponse():
        # Simulate concurrent close() replacing the socket mid-request
        fake_conn.sock = None
        return "canned-response"

    fake_conn.getresponse = patched_getresponse
    monkeypatch.setattr(
        type(transport), "connection", property(lambda self: fake_conn)
    )

    with transport.deadline(10):
        # This should not raise even though sock was replaced during getresponse()
        transport._request("GET", "status", None)

    # Since sock was set to None during getresponse(), the finally block should
    # have skipped the restore (because conn.sock is None != original_sock)
    # The original socket's timeout remains at the deadline value
    assert original_sock.timeout > 0
    assert original_sock.timeout <= 10
