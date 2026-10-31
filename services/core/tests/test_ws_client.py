"""The RFC 6455 client (C8), tested against a real socket rather than a mock.

A WebSocket client cannot be honestly tested with a fake socket. Every property
that matters here -- the accept-digest check, masked client frames, reassembling
fragments, answering a ping without surfacing it -- is a property of *bytes on
the wire*. A mock that returns `{"ok": true}` would pass while the shipped client
sent unmasked frames that every real server drops.

So the peer in these tests is a real listening socket on loopback, driven by a
thread, speaking frames the test builds by hand. Several of the peers are
deliberately *wrong* -- a bad accept digest, an RSV bit, an oversized control
frame -- because the interesting half of a protocol client is what it refuses.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
import time
from typing import Callable

import pytest

from nexus.core.ws_client import (
    OP_BINARY,
    OP_CLOSE,
    OP_CONTINUATION,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    WebSocket,
    WebSocketClosed,
    WebSocketError,
)

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --------------------------------------------------------------- wire helpers


def _frame(opcode: int, payload: bytes, *, fin: bool = True, mask: bool = False, rsv: int = 0) -> bytes:
    """Build one frame. Server-side frames are unmasked unless a test asks otherwise."""
    first = (0x80 if fin else 0x00) | (rsv << 4) | opcode
    length = len(payload)
    header = bytearray([first])
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    if mask:
        key = os.urandom(4)
        header.extend(key)
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + payload


def _read_exactly(sock: socket.socket, count: int) -> bytes:
    chunks = []
    while count > 0:
        chunk = sock.recv(count)
        if not chunk:
            raise AssertionError("the peer closed early")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def _read_client_frame(sock: socket.socket) -> tuple[int, bytes, bool]:
    """Read one frame the *client* sent. Returns (opcode, payload, was_masked)."""
    first, second = _read_exactly(sock, 2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exactly(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read_exactly(sock, 8))[0]
    key = _read_exactly(sock, 4) if masked else b""
    payload = _read_exactly(sock, length) if length else b""
    if masked:
        payload = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
    return opcode, payload, masked


class Peer:
    """A one-connection loopback server whose behaviour the test supplies.

    ``behaviour`` receives the accepted socket after the handshake and may do
    anything with it -- echo, fragment, ping, lie about the accept digest.
    """

    def __init__(
        self,
        behaviour: Callable[[socket.socket], None],
        *,
        accept: str | None = None,
        status: str = "101 Switching Protocols",
    ) -> None:
        self._behaviour = behaviour
        self._accept_override = accept
        self._status = status
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port = self._server.getsockname()[1]
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._done = threading.Event()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/"

    def _serve(self) -> None:
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                request += chunk
            key = ""
            for line in request.decode("latin-1").split("\r\n"):
                name, _, value = line.partition(":")
                if name.strip().lower() == "sec-websocket-key":
                    key = value.strip()
            accept = self._accept_override or base64.b64encode(
                hashlib.sha1((key + _GUID).encode("ascii")).digest()
            ).decode("ascii")
            conn.sendall(
                f"HTTP/1.1 {self._status}\r\n".encode("ascii")
                + b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Accept: " + accept.encode("ascii") + b"\r\n\r\n"
            )
            self._behaviour(conn)
        except BaseException as exc:  # noqa: BLE001 - reported to the test thread
            self.error = exc
        finally:
            self._done.set()
            try:
                conn.close()
            except OSError:
                pass
            self._server.close()

    def wait(self, timeout: float = 5.0) -> None:
        assert self._done.wait(timeout), "the peer thread did not finish"
        if self.error is not None:
            raise self.error

    def __enter__(self) -> "Peer":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._done.wait(5.0)
        try:
            self._server.close()
        except OSError:
            pass


# ------------------------------------------------------------------ the tests


def test_a_text_message_round_trips_and_client_frames_are_masked() -> None:
    """The mask bit is asserted because a server is entitled to drop unmasked frames."""
    seen: dict[str, object] = {}

    def behaviour(conn: socket.socket) -> None:
        opcode, payload, masked = _read_client_frame(conn)
        seen["opcode"] = opcode
        seen["payload"] = payload
        seen["masked"] = masked
        conn.sendall(_frame(OP_TEXT, b"pong:" + payload))

    with Peer(behaviour) as peer:
        with WebSocket.connect(peer.url, timeout=5.0) as sock:
            sock.send_text("hello")
            assert sock.recv(timeout=5.0) == "pong:hello"
        peer.wait()

    assert seen["opcode"] == OP_TEXT
    assert seen["payload"] == b"hello"
    assert seen["masked"] is True


@pytest.mark.parametrize("size", [125, 126, 700, 70_000])
def test_payload_lengths_across_all_three_header_encodings(size: int) -> None:
    """125/126/700/70000 straddle the 7-bit, 16-bit and 64-bit length forms."""
    body = ("x" * size).encode("utf-8")

    def behaviour(conn: socket.socket) -> None:
        _read_client_frame(conn)
        conn.sendall(_frame(OP_TEXT, body))

    with Peer(behaviour) as peer:
        with WebSocket.connect(peer.url, timeout=10.0) as sock:
            sock.send_text("go")
            assert sock.recv(timeout=10.0) == "x" * size
        peer.wait()


def test_a_fragmented_message_is_reassembled() -> None:
    def behaviour(conn: socket.socket) -> None:
        _read_client_frame(conn)
        conn.sendall(_frame(OP_TEXT, b"frag", fin=False))
        conn.sendall(_frame(OP_CONTINUATION, b"men", fin=False))
        conn.sendall(_frame(OP_CONTINUATION, b"ted", fin=True))

    with Peer(behaviour) as peer:
        with WebSocket.connect(peer.url, timeout=5.0) as sock:
            sock.send_text("go")
            assert sock.recv(timeout=5.0) == "fragmented"
        peer.wait()


def test_a_ping_is_answered_and_never_surfaces_as_a_message() -> None:
    """The client must reply pong *and* keep waiting for the real message.

    Both halves matter: a client that ignored the ping fails the echo assertion
    below, and one that returned the ping payload as a message fails the recv.
    """
    observed: dict[str, object] = {}

    def behaviour(conn: socket.socket) -> None:
        _read_client_frame(conn)
        conn.sendall(_frame(OP_PING, b"beat"))
        opcode, payload, _ = _read_client_frame(conn)
        observed["opcode"] = opcode
        observed["payload"] = payload
        conn.sendall(_frame(OP_TEXT, b"the real message"))

    with Peer(behaviour) as peer:
        with WebSocket.connect(peer.url, timeout=5.0) as sock:
            sock.send_text("go")
            assert sock.recv(timeout=5.0) == "the real message"
        peer.wait()

    assert observed["opcode"] == OP_PONG
    assert observed["payload"] == b"beat"


def test_a_pong_is_absorbed_rather_than_returned() -> None:
    def behaviour(conn: socket.socket) -> None:
        _read_client_frame(conn)
        conn.sendall(_frame(OP_PONG, b"unsolicited"))
        conn.sendall(_frame(OP_TEXT, b"after"))

    with Peer(behaviour) as peer:
        with WebSocket.connect(peer.url, timeout=5.0) as sock:
            sock.send_text("go")
            assert sock.recv(timeout=5.0) == "after"
        peer.wait()


def test_a_close_frame_raises_with_the_code_and_reason() -> None:
    def behaviour(conn: socket.socket) -> None:
        _read_client_frame(conn)
        conn.sendall(_frame(OP_CLOSE, struct.pack("!H", 1001) + b"going away"))

    with Peer(behaviour) as peer:
        with WebSocket.connect(peer.url, timeout=5.0) as sock:
            sock.send_text("go")
            with pytest.raises(WebSocketClosed) as caught:
                sock.recv(timeout=5.0)
        peer.wait()

    assert caught.value.code == 1001
    assert caught.value.reason == "going away"


def test_multibyte_text_survives_the_round_trip() -> None:
    """UTF-8 is decoded once, at the message boundary -- not per frame."""
    text = "héllo — 世界 🌍"

    def behaviour(conn: socket.socket) -> None:
        _read_client_frame(conn)
        raw = text.encode("utf-8")
        # Split mid-character on purpose: a per-fragment decode would corrupt this.
        conn.sendall(_frame(OP_TEXT, raw[:4], fin=False))
        conn.sendall(_frame(OP_CONTINUATION, raw[4:], fin=True))

    with Peer(behaviour) as peer:
        with WebSocket.connect(peer.url, timeout=5.0) as sock:
            sock.send_text("go")
            assert sock.recv(timeout=5.0) == text
        peer.wait()


# --------------------------------------------------------- refusals, not guesses


def test_a_wrong_accept_digest_is_refused() -> None:
    """The digest is the only thing that distinguishes an upgrade from any 200."""
    with Peer(lambda conn: time.sleep(0.2), accept="not-the-right-digest") as peer:
        with pytest.raises(WebSocketError, match="Sec-WebSocket-Accept"):
            WebSocket.connect(peer.url, timeout=5.0)


def test_a_non_101_response_is_refused_even_with_the_right_headers() -> None:
    """A `200 OK` carrying every upgrade header is still not a WebSocket.

    This is the near-miss a key-digest check alone would wave through: the digest
    is correct, so only the status line distinguishes a real upgrade from a proxy
    answering the request normally.
    """

    def behaviour(conn: socket.socket) -> None:  # pragma: no cover - never reached
        pass

    with Peer(behaviour, status="200 OK") as peer:
        with pytest.raises(WebSocketError, match="refused the WebSocket upgrade"):
            WebSocket.connect(peer.url, timeout=5.0)


def test_an_rsv_bit_is_refused_rather_than_misparsed() -> None:
    """No extension is negotiated, so a set RSV bit is a frame we cannot read."""

    def behaviour(conn: socket.socket) -> None:
        _read_client_frame(conn)
        conn.sendall(_frame(OP_TEXT, b"compressed?", rsv=4))

    with Peer(behaviour) as peer:
        with WebSocket.connect(peer.url, timeout=5.0) as sock:
            sock.send_text("go")
            with pytest.raises(WebSocketError, match="extension bit"):
                sock.recv(timeout=5.0)


def test_a_fragmented_control_frame_is_refused() -> None:
    def behaviour(conn: socket.socket) -> None:
        _read_client_frame(conn)
        conn.sendall(_frame(OP_PING, b"beat", fin=False))

    with Peer(behaviour) as peer:
        with WebSocket.connect(peer.url, timeout=5.0) as sock:
            sock.send_text("go")
            with pytest.raises(WebSocketError, match="control frame"):
                sock.recv(timeout=5.0)


def test_a_continuation_with_nothing_to_continue_is_refused() -> None:
    def behaviour(conn: socket.socket) -> None:
        _read_client_frame(conn)
        conn.sendall(_frame(OP_CONTINUATION, b"orphan", fin=True))

    with Peer(behaviour) as peer:
        with WebSocket.connect(peer.url, timeout=5.0) as sock:
            sock.send_text("go")
            with pytest.raises(WebSocketError, match="nothing to continue"):
                sock.recv(timeout=5.0)


def test_a_binary_message_is_refused_rather_than_returned_as_text() -> None:
    """CDP is JSON text. Returning bytes where callers expect str fails far away."""

    def behaviour(conn: socket.socket) -> None:
        _read_client_frame(conn)
        conn.sendall(_frame(OP_BINARY, b"\x00\x01\x02"))

    with Peer(behaviour) as peer:
        with WebSocket.connect(peer.url, timeout=5.0) as sock:
            sock.send_text("go")
            with pytest.raises(WebSocketError, match="binary"):
                sock.recv(timeout=5.0)


def test_a_non_websocket_scheme_is_refused_before_any_connection() -> None:
    with pytest.raises(WebSocketError, match="ws://"):
        WebSocket.connect("http://127.0.0.1:1/")


def test_a_read_timeout_is_raised_as_a_timeout_not_as_a_close() -> None:
    """Silence and a close are different, and the driver treats them differently."""

    def behaviour(conn: socket.socket) -> None:
        _read_client_frame(conn)
        time.sleep(1.5)

    with Peer(behaviour) as peer:
        with WebSocket.connect(peer.url, timeout=5.0) as sock:
            sock.send_text("go")
            with pytest.raises(socket.timeout):
                sock.recv(timeout=0.3)
