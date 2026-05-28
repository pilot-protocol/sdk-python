"""Tests for high-level service helpers on Driver.

Covers ``send_message`` (data-exchange port 1001), ``send_file`` (TypeFile
framing on port 1001), and ``publish_event`` / ``subscribe_event`` (event
stream port 1002).

The wire formats are documented in the docstrings on Driver:
- data-exchange frame: ``[4-byte type][4-byte length][payload]``
- file payload:        ``[2-byte name len][name][file data]``
- event frame:         ``[2-byte topic len][topic][4-byte payload len][payload]``

We mock ``Driver.dial`` to return a fake Conn that records writes and
serves canned reads, so no libpilot or daemon is required.
"""

from __future__ import annotations

import json
import struct
import types
from collections import deque
from unittest import mock

import pytest

import pilotprotocol.client as client_mod
from pilotprotocol.client import PilotError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeConn:
    """Minimal Conn replacement: capture writes, serve canned reads."""

    def __init__(self, reads: list[bytes] | None = None) -> None:
        self.writes: list[bytes] = []
        self._reads: deque[bytes] = deque(reads or [])
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def read(self, size: int = 4096) -> bytes:
        if not self._reads:
            return b""
        chunk = self._reads.popleft()
        # If the caller wants a smaller chunk, slice + push back the remainder.
        if len(chunk) > size:
            self._reads.appendleft(chunk[size:])
            return chunk[:size]
        return chunk

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _ack_frame(payload: str = "ACK TEXT 5 bytes") -> list[bytes]:
    """Build the two reads send_message expects: header(8) then payload."""
    body = payload.encode()
    header = struct.pack(">II", 1, len(body))
    return [header, body]


def _make_driver_with_dial(monkeypatch, conn: FakeConn) -> client_mod.Driver:
    """Construct a Driver whose .dial returns the provided FakeConn.

    We don't go through ``__init__`` (it calls into libpilot) — we build the
    instance directly. The high-level methods never read ``self._h`` directly,
    they go through ``self.dial`` which we replace.
    """
    d = object.__new__(client_mod.Driver)
    d._h = 1
    d._closed = False
    monkeypatch.setattr(d, "dial", lambda addr, **kw: conn)
    return d


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    def test_protocol_address_skips_resolve(self, monkeypatch):
        conn = FakeConn(reads=_ack_frame("ACK TEXT 5 bytes"))
        d = _make_driver_with_dial(monkeypatch, conn)
        # If resolve_hostname were called this would explode (no real lib).
        d.resolve_hostname = mock.Mock(side_effect=AssertionError("should skip"))

        result = d.send_message("0:0001.0000.0002", b"hello")

        assert result["sent"] == 5
        assert result["type"] == "text"
        assert result["target"] == "0:0001.0000.0002"
        assert result["ack"] == "ACK TEXT 5 bytes"
        # Frame on the wire: [type=1][length=5]hello
        assert conn.writes[0] == struct.pack(">II", 1, 5) + b"hello"
        assert conn.closed is True

    def test_hostname_path_calls_resolve(self, monkeypatch):
        conn = FakeConn(reads=_ack_frame())
        d = _make_driver_with_dial(monkeypatch, conn)
        d.resolve_hostname = mock.Mock(return_value={"address": "0:0001.0000.0042"})

        result = d.send_message("agent-hostname", b"hi")

        d.resolve_hostname.assert_called_once_with("agent-hostname")
        assert result["target"] == "0:0001.0000.0042"

    def test_resolve_returns_empty_address_raises(self, monkeypatch):
        conn = FakeConn()
        d = _make_driver_with_dial(monkeypatch, conn)
        d.resolve_hostname = mock.Mock(return_value={"address": ""})

        with pytest.raises(PilotError, match="Could not resolve hostname"):
            d.send_message("nonexistent", b"x")

    def test_message_type_maps_correctly(self, monkeypatch):
        for label, code in (("text", 1), ("binary", 2), ("json", 3), ("file", 4)):
            conn = FakeConn(reads=_ack_frame())
            d = _make_driver_with_dial(monkeypatch, conn)
            d.send_message("0:0001.0000.0002", b"abc", msg_type=label)
            ftype, flen = struct.unpack(">II", conn.writes[0][:8])
            assert ftype == code
            assert flen == 3

    def test_unknown_msg_type_defaults_to_text(self, monkeypatch):
        conn = FakeConn(reads=_ack_frame())
        d = _make_driver_with_dial(monkeypatch, conn)
        d.send_message("0:0001.0000.0002", b"x", msg_type="weird-type")
        ftype, _ = struct.unpack(">II", conn.writes[0][:8])
        assert ftype == 1  # text fallback

    def test_ack_read_failure_still_returns_sent(self, monkeypatch):
        # No ACK frame readable → result lacks 'ack' but call succeeds
        conn = FakeConn(reads=[])
        d = _make_driver_with_dial(monkeypatch, conn)
        result = d.send_message("0:0001.0000.0002", b"hello")
        assert result == {"sent": 5, "type": "text", "target": "0:0001.0000.0002"}
        assert "ack" not in result

    def test_short_ack_header_falls_through(self, monkeypatch):
        # Header is < 8 bytes → ACK branch skipped
        conn = FakeConn(reads=[b"\x00\x00\x00"])  # 3 bytes, not 8
        d = _make_driver_with_dial(monkeypatch, conn)
        result = d.send_message("0:0001.0000.0002", b"x")
        assert "ack" not in result

    def test_ack_read_raises_caught(self, monkeypatch):
        # Conn.read raises after the write — caught silently
        class BoomConn(FakeConn):
            def read(self, size: int = 4096) -> bytes:
                raise PilotError("read broke")

        conn = BoomConn()
        d = _make_driver_with_dial(monkeypatch, conn)
        result = d.send_message("0:0001.0000.0002", b"x")
        assert result["sent"] == 1
        assert "ack" not in result

    def test_dial_target_uses_port_1001(self, monkeypatch):
        conn = FakeConn(reads=_ack_frame())
        d = object.__new__(client_mod.Driver)
        d._h = 1
        d._closed = False
        captured = {}

        def fake_dial(addr, **kw):
            captured["addr"] = addr
            return conn

        d.dial = fake_dial
        d.send_message("0:0001.0000.0002", b"x")
        assert captured["addr"] == "0:0001.0000.0002:1001"


# ---------------------------------------------------------------------------
# send_file
# ---------------------------------------------------------------------------


class TestSendFile:
    def test_missing_file_raises(self, monkeypatch, tmp_path):
        d = _make_driver_with_dial(monkeypatch, FakeConn())
        with pytest.raises(PilotError, match="File not found"):
            d.send_file("0:0001.0000.0002", str(tmp_path / "nope.bin"))

    def test_file_frame_layout(self, monkeypatch, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_bytes(b"contents")

        conn = FakeConn(reads=_ack_frame("ACK FILE 8 bytes"))
        d = _make_driver_with_dial(monkeypatch, conn)
        result = d.send_file("0:0001.0000.0002", str(f))

        assert result["sent"] == 8
        assert result["filename"] == "hello.txt"
        assert result["ack"] == "ACK FILE 8 bytes"

        # Frame: [type=4][total_len][2-byte name len][name][file data]
        ftype, total_len = struct.unpack(">II", conn.writes[0][:8])
        assert ftype == 4
        payload = conn.writes[0][8:]
        assert len(payload) == total_len
        name_len = struct.unpack(">H", payload[:2])[0]
        assert payload[2 : 2 + name_len] == b"hello.txt"
        assert payload[2 + name_len :] == b"contents"

    def test_hostname_resolution(self, monkeypatch, tmp_path):
        f = tmp_path / "x.bin"
        f.write_bytes(b"\x00")
        conn = FakeConn(reads=_ack_frame())
        d = _make_driver_with_dial(monkeypatch, conn)
        d.resolve_hostname = mock.Mock(return_value={"address": "0:0001.0000.0009"})

        result = d.send_file("hostname", str(f))
        d.resolve_hostname.assert_called_once_with("hostname")
        assert result["target"] == "0:0001.0000.0009"

    def test_resolve_empty_address_raises(self, monkeypatch, tmp_path):
        f = tmp_path / "x.bin"
        f.write_bytes(b"\x00")
        d = _make_driver_with_dial(monkeypatch, FakeConn())
        d.resolve_hostname = mock.Mock(return_value={"address": ""})
        with pytest.raises(PilotError, match="Could not resolve hostname"):
            d.send_file("nope", str(f))

    def test_ack_failure_does_not_raise(self, monkeypatch, tmp_path):
        f = tmp_path / "x.bin"
        f.write_bytes(b"data")

        class BoomConn(FakeConn):
            def read(self, size: int = 4096) -> bytes:
                raise PilotError("network died after write")

        conn = BoomConn()
        d = _make_driver_with_dial(monkeypatch, conn)
        result = d.send_file("0:0001.0000.0002", str(f))
        assert result["sent"] == 4
        assert "ack" not in result

    def test_short_ack_header_falls_through(self, monkeypatch, tmp_path):
        f = tmp_path / "x.bin"
        f.write_bytes(b"data")
        conn = FakeConn(reads=[b""])  # empty read = falsy → branch skipped
        d = _make_driver_with_dial(monkeypatch, conn)
        result = d.send_file("0:0001.0000.0002", str(f))
        assert result["sent"] == 4
        assert "ack" not in result


# ---------------------------------------------------------------------------
# publish_event
# ---------------------------------------------------------------------------


class TestPublishEvent:
    def test_subscribe_then_publish_frames(self, monkeypatch):
        conn = FakeConn()
        d = _make_driver_with_dial(monkeypatch, conn)
        r = d.publish_event("0:0001.0000.0002", "temp", b"42C")

        assert r == {"status": "published", "topic": "temp", "bytes": 3}
        # First write: subscribe (empty payload)
        # Wire: [2-byte topic len][topic][4-byte payload len][payload]
        topic_len = struct.unpack(">H", conn.writes[0][:2])[0]
        assert conn.writes[0][2 : 2 + topic_len] == b"temp"
        payload_len = struct.unpack(">I", conn.writes[0][2 + topic_len : 6 + topic_len])[0]
        assert payload_len == 0
        # Second write: actual publish
        topic_len2 = struct.unpack(">H", conn.writes[1][:2])[0]
        assert conn.writes[1][2 : 2 + topic_len2] == b"temp"
        plen2 = struct.unpack(">I", conn.writes[1][2 + topic_len2 : 6 + topic_len2])[0]
        assert plen2 == 3
        assert conn.writes[1][6 + topic_len2 :] == b"42C"
        assert conn.closed is True

    def test_resolves_hostname(self, monkeypatch):
        conn = FakeConn()
        d = _make_driver_with_dial(monkeypatch, conn)
        d.resolve_hostname = mock.Mock(return_value={"address": "0:0001.0000.0042"})
        d.publish_event("agent-host", "topic-A", b"x")
        d.resolve_hostname.assert_called_once_with("agent-host")

    def test_resolve_empty_raises(self, monkeypatch):
        d = _make_driver_with_dial(monkeypatch, FakeConn())
        d.resolve_hostname = mock.Mock(return_value={"address": ""})
        with pytest.raises(PilotError, match="Could not resolve hostname"):
            d.publish_event("nope", "t", b"x")

    def test_dial_uses_port_1002(self, monkeypatch):
        captured = {}

        def fake_dial(addr, **kw):
            captured["addr"] = addr
            return FakeConn()

        d = object.__new__(client_mod.Driver)
        d._h = 1
        d._closed = False
        d.dial = fake_dial
        d.publish_event("0:0001.0000.0002", "t", b"x")
        assert captured["addr"] == "0:0001.0000.0002:1002"


# ---------------------------------------------------------------------------
# subscribe_event
# ---------------------------------------------------------------------------


def _event_bytes(topic: str, payload: bytes) -> list[bytes]:
    """Encode an event frame as the four reads subscribe_event performs.

    Reads (in order): 2-byte topic len, topic, 4-byte payload len, payload.
    """
    tb = topic.encode()
    return [
        struct.pack(">H", len(tb)),
        tb,
        struct.pack(">I", len(payload)),
        payload,
    ]


class TestSubscribeEvent:
    def test_yields_events(self, monkeypatch):
        conn = FakeConn(
            reads=_event_bytes("foo", b"hello") + _event_bytes("bar", b"world")
        )
        d = _make_driver_with_dial(monkeypatch, conn)
        gen = d.subscribe_event("0:0001.0000.0002", "foo", timeout=5)
        events = list(gen)
        assert events == [("foo", b"hello"), ("bar", b"world")]
        # First write is the subscription frame with empty payload
        topic_len = struct.unpack(">H", conn.writes[0][:2])[0]
        assert conn.writes[0][2 : 2 + topic_len] == b"foo"

    def test_callback_invoked_instead_of_yield(self, monkeypatch):
        conn = FakeConn(reads=_event_bytes("t", b"p"))
        d = _make_driver_with_dial(monkeypatch, conn)
        received = []
        gen = d.subscribe_event(
            "0:0001.0000.0002", "t", callback=lambda topic, data: received.append((topic, data)), timeout=2,
        )
        # When callback is supplied, the generator yields nothing.
        assert list(gen) == []
        assert received == [("t", b"p")]

    def test_short_topic_len_returns_none(self, monkeypatch):
        # 1 byte instead of 2 — read_event returns None → loop breaks
        conn = FakeConn(reads=[b"\x00"])
        d = _make_driver_with_dial(monkeypatch, conn)
        events = list(d.subscribe_event("0:0001.0000.0002", "t", timeout=2))
        assert events == []
        assert conn.closed is True

    def test_short_topic_body_returns_none(self, monkeypatch):
        # Topic len says 5 but only 2 bytes follow
        reads = [struct.pack(">H", 5), b"ab"]
        conn = FakeConn(reads=reads)
        d = _make_driver_with_dial(monkeypatch, conn)
        events = list(d.subscribe_event("0:0001.0000.0002", "t", timeout=2))
        assert events == []

    def test_short_payload_len_returns_none(self, monkeypatch):
        reads = [struct.pack(">H", 3), b"foo", b"\x00\x01"]  # 2 bytes, need 4
        conn = FakeConn(reads=reads)
        d = _make_driver_with_dial(monkeypatch, conn)
        events = list(d.subscribe_event("0:0001.0000.0002", "t", timeout=2))
        assert events == []

    def test_short_payload_body_returns_none(self, monkeypatch):
        reads = [
            struct.pack(">H", 3),
            b"foo",
            struct.pack(">I", 10),
            b"abc",  # only 3 bytes, need 10
        ]
        conn = FakeConn(reads=reads)
        d = _make_driver_with_dial(monkeypatch, conn)
        events = list(d.subscribe_event("0:0001.0000.0002", "t", timeout=2))
        assert events == []

    def test_connection_closed_error_breaks_cleanly(self, monkeypatch):
        class BoomConn(FakeConn):
            def __init__(self):
                super().__init__()
                self._sent_subscribe = False

            def read(self, size: int = 4096) -> bytes:
                raise PilotError("connection closed by peer")

        conn = BoomConn()
        d = _make_driver_with_dial(monkeypatch, conn)
        events = list(d.subscribe_event("0:0001.0000.0002", "t", timeout=2))
        assert events == []
        assert conn.closed is True

    def test_eof_error_propagates(self, monkeypatch):
        # RuntimeError containing "EOF" is NOT a clean disconnect —
        # only PilotError("connection closed") should be silenced.
        class EofConn(FakeConn):
            def read(self, size: int = 4096) -> bytes:
                raise RuntimeError("unexpected EOF on stream")

        conn = EofConn()
        d = _make_driver_with_dial(monkeypatch, conn)
        with pytest.raises(RuntimeError, match="unexpected EOF on stream"):
            list(d.subscribe_event("0:0001.0000.0002", "t", timeout=2))
        assert conn.closed is True

    def test_other_exception_propagates(self, monkeypatch):
        # Error string contains neither "connection closed" nor "EOF" —
        # should propagate.
        class BadConn(FakeConn):
            def read(self, size: int = 4096) -> bytes:
                raise PilotError("permission denied")

        conn = BadConn()
        d = _make_driver_with_dial(monkeypatch, conn)
        with pytest.raises(PilotError, match="permission denied"):
            list(d.subscribe_event("0:0001.0000.0002", "t", timeout=2))
        # Connection should still be closed by the finally block
        assert conn.closed is True

    def test_timeout_terminates_loop(self, monkeypatch):
        # With timeout=0 the while loop never enters → no reads, immediate end
        conn = FakeConn(reads=_event_bytes("x", b"y"))
        d = _make_driver_with_dial(monkeypatch, conn)
        events = list(d.subscribe_event("0:0001.0000.0002", "x", timeout=0))
        assert events == []
        # But we should have written the subscription frame before entering the loop
        assert len(conn.writes) == 1

    def test_resolves_hostname(self, monkeypatch):
        conn = FakeConn(reads=[])
        d = _make_driver_with_dial(monkeypatch, conn)
        d.resolve_hostname = mock.Mock(return_value={"address": "0:0001.0000.0042"})
        list(d.subscribe_event("hostname", "t", timeout=1))
        d.resolve_hostname.assert_called_once_with("hostname")

    def test_resolve_empty_raises(self, monkeypatch):
        d = _make_driver_with_dial(monkeypatch, FakeConn())
        d.resolve_hostname = mock.Mock(return_value={"address": ""})
        with pytest.raises(PilotError, match="Could not resolve hostname"):
            list(d.subscribe_event("nope", "t", timeout=1))
