"""A small RFC 6455 WebSocket client, written on ``socket`` (task C8).

Why this exists rather than an import of ``websockets``: that package is not a
dependency of NEXUS. It arrives only because ``uvicorn[standard]`` happens to
pull it in, which means it is absent the moment the core is run on a plain
``uvicorn`` (or any other ASGI server) -- and a browser-automation tool that
stops working because of an unlisted transitive dependency is a failure with no
readable cause. ``requirements.txt`` is the list of things NEXUS actually needs;
this module keeps it that way.

The second reason is scope. NEXUS needs one narrow thing: a synchronous client
that speaks the Chrome DevTools Protocol to a port on loopback. That protocol is
request/response with occasional server-pushed notifications, so a blocking
client with a read timeout is a better fit than an async client that would have
to be driven from the worker thread the tool runtime already uses.

What is implemented, precisely:

* the opening handshake, including the ``Sec-WebSocket-Accept`` check -- a
  server that does not echo the right digest is refused rather than trusted,
  because the accept header is the only thing distinguishing a WebSocket upgrade
  from any other HTTP response;
* text frames in both directions, with client-side masking as the RFC requires;
* fragmented messages, reassembled across continuation frames;
* control frames: ``ping`` is answered with ``pong`` transparently, ``pong`` is
  absorbed, ``close`` returns a clean end-of-stream;
* ``wss://`` via the standard TLS context.

What is *not* implemented, and is refused rather than half-supported: extensions
(``permessage-deflate`` and friends). The handshake never offers any, so a server
that negotiates one is violating the protocol, and ``_read_frame`` rejects any
frame with an RSV bit set instead of mis-parsing compressed bytes as text.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import os
import socket
import ssl
import struct
import urllib.parse
from typing import Any

#: Server-to-client frames are never masked, but a frame large enough to matter
#: is a bug or an attack either way. 32 MiB is far above any CDP response -- a
#: screenshot arrives base64-encoded and is split across events well below this.
MAX_FRAME_BYTES = 32 * 1024 * 1024

# Opcodes (RFC 6455 §5.2).
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

CONTROL_OPCODES = frozenset({OP_CLOSE, OP_PING, OP_PONG})

#: The magic string from RFC 6455 §1.3, concatenated with the client key to prove
#: the server actually understood the upgrade rather than echoing a request it
#: happened to receive.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketError(RuntimeError):
    """The connection failed, or the peer sent something malformed."""


class WebSocketClosed(WebSocketError):
    """The peer closed the connection, optionally with a code and reason."""

    def __init__(self, code: int | None = None, reason: str = "") -> None:
        self.code = code
        self.reason = reason
        detail = f"closed with code {code}" if code is not None else "closed"
        super().__init__(f"The connection was {detail}{f': {reason}' if reason else '.'}")


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    """Read exactly ``count`` bytes, or raise if the peer stops early."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        try:
            chunk = sock.recv(remaining)
        except socket.timeout:
            raise
        except OSError as exc:
            raise WebSocketError(f"The connection broke while reading: {exc}") from exc
        if not chunk:
            raise WebSocketError(
                f"The connection closed after {count - remaining} of {count} bytes."
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class WebSocket:
    """A connected WebSocket. Blocking, with a per-read timeout.

    Not thread-safe on its own and does not pretend to be: :class:`_Lock`-free by
    design, because the only caller (`nexus.browser.driver`) serialises every
    command through its own lock. A second concurrent reader would interleave
    frames and both would parse garbage.
    """

    def __init__(self, sock: socket.socket, *, url: str = "") -> None:
        self._socket = sock
        self.url = url
        self._send_lock = False

    # ------------------------------------------------------------- connecting

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        timeout: float = 10.0,
        headers: dict[str, str] | None = None,
        origin: str | None = None,
    ) -> "WebSocket":
        """Open a WebSocket to ``ws://`` or ``wss://`` and complete the handshake."""
        parsed = urllib.parse.urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in ("ws", "wss"):
            raise WebSocketError(f"'{url}' is not a ws:// or wss:// address.")
        if not parsed.hostname:
            raise WebSocketError(f"'{url}' has no host.")

        host = parsed.hostname
        port = parsed.port or (443 if scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise WebSocketError(f"Could not reach {host}:{port}: {exc}") from exc

        if scheme == "wss":
            try:
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=host)
            except (ssl.SSLError, OSError) as exc:
                sock.close()
                raise WebSocketError(f"TLS with {host} failed: {exc}") from exc

        try:
            cls._handshake(sock, host, port, path, headers or {}, origin)
        except Exception:
            sock.close()
            raise

        sock.settimeout(timeout)
        return cls(sock, url=url)

    @staticmethod
    def _handshake(
        sock: socket.socket,
        host: str,
        port: int,
        path: str,
        headers: dict[str, str],
        origin: str | None,
    ) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        default_port = 443 if port == 443 else 80
        host_header = host if port == default_port else f"{host}:{port}"

        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        if origin:
            lines.append(f"Origin: {origin}")
        for name, value in headers.items():
            lines.append(f"{name}: {value}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

        try:
            sock.sendall(request)
        except OSError as exc:
            raise WebSocketError(f"The handshake could not be sent: {exc}") from exc

        # Read only as far as the end of the headers. A devtools endpoint answers
        # with headers alone, but reading past them here would swallow the first
        # frame on a server that pipelines one.
        raw = b""
        while b"\r\n\r\n" not in raw:
            try:
                chunk = sock.recv(4096)
            except (socket.timeout, OSError) as exc:
                raise WebSocketError(f"No handshake response arrived: {exc}") from exc
            if not chunk:
                raise WebSocketError("The server closed during the WebSocket handshake.")
            raw += chunk
            if len(raw) > 65536:
                raise WebSocketError("The handshake response headers were implausibly large.")

        head, _, remainder = raw.partition(b"\r\n\r\n")
        if remainder:
            # Legal but unexpected here; refusing beats silently dropping bytes
            # that are almost certainly the start of a frame we would then
            # mis-frame for the rest of the connection.
            raise WebSocketError(
                "The server sent data before the handshake finished, which this client "
                "does not support."
            )

        try:
            text = head.decode("latin-1")
        except UnicodeDecodeError as exc:  # pragma: no cover - latin-1 never raises
            raise WebSocketError(f"The handshake response was not readable: {exc}") from exc

        lines_out = text.split("\r\n")
        status = lines_out[0] if lines_out else ""
        if " 101" not in status:
            raise WebSocketError(
                f"The server refused the WebSocket upgrade: {status or 'no status line'}"
            )

        response_headers: dict[str, str] = {}
        for line in lines_out[1:]:
            name, _, value = line.partition(":")
            if name:
                response_headers[name.strip().lower()] = value.strip()

        expected = base64.b64encode(
            hashlib.sha1((key + _GUID).encode("ascii")).digest()
        ).decode("ascii")
        got = response_headers.get("sec-websocket-accept", "")
        if got != expected:
            raise WebSocketError(
                "The server's Sec-WebSocket-Accept did not match, so this is not a "
                "WebSocket connection."
            )
        if "permessage-deflate" in response_headers.get("sec-websocket-extensions", ""):
            raise WebSocketError(
                "The server negotiated a WebSocket extension this client did not offer."
            )

    # ---------------------------------------------------------------- sending

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._send_lock:
            raise WebSocketError(
                "A send is already in progress on this socket. WebSocket is not "
                "thread-safe; serialize calls instead."
            )
        self._send_lock = True
        try:
            _write_frame(self._socket, opcode, payload)
        except OSError as exc:
            raise WebSocketError(f"The connection broke while sending: {exc}") from exc
        finally:
            self._send_lock = False

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def send_ping(self, payload: bytes = b"") -> None:
        self._send_frame(OP_PING, payload)

    # --------------------------------------------------------------- receiving

    def recv(self, timeout: float | None = None) -> str:
        """Return the next complete text message.

        Fragments are reassembled and control frames handled, so a ``ping`` or a
        ``pong`` never surfaces here as a message. Raises :class:`WebSocketClosed`
        on a close frame, and ``socket.timeout`` if nothing arrives in time.
        """
        if timeout is not None:
            self._socket.settimeout(timeout)

        buffer = bytearray()
        message_opcode: int | None = None

        while True:
            fin, opcode, payload = self._read_frame()

            if opcode in CONTROL_OPCODES:
                if not fin:
                    raise WebSocketError("A control frame arrived fragmented, which is illegal.")
                if opcode == OP_PING:
                    # Answer immediately and keep waiting for a data message.
                    self._send_frame(OP_PONG, payload)
                    continue
                if opcode == OP_PONG:
                    continue
                code, reason = _parse_close(payload)
                # Echo the close, as the RFC directs, then report it.
                try:
                    self._send_frame(OP_CLOSE, payload[:125])
                except WebSocketError:
                    pass
                raise WebSocketClosed(code, reason)

            if opcode == OP_CONTINUATION:
                if message_opcode is None:
                    raise WebSocketError("A continuation frame arrived with nothing to continue.")
            else:
                if message_opcode is not None:
                    raise WebSocketError(
                        "A new data frame arrived while a fragmented message was still open."
                    )
                message_opcode = opcode

            buffer.extend(payload)
            if len(buffer) > MAX_FRAME_BYTES:
                raise WebSocketError("A message exceeded the size this client will assemble.")

            if fin:
                if message_opcode == OP_BINARY:
                    # CDP is JSON text only. Returning bytes where every caller
                    # expects str would fail far from the cause.
                    raise WebSocketError("The server sent a binary message; this client reads text.")
                try:
                    return buffer.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise WebSocketError(f"A text message was not valid UTF-8: {exc}") from exc

    def _read_frame(self) -> tuple[bool, int, bytes]:
        header = _recv_exactly(self._socket, 2)
        first, second = header[0], header[1]

        fin = bool(first & 0x80)
        if first & 0x70:
            raise WebSocketError(
                "A frame arrived with an extension bit set, but no extension was negotiated."
            )
        opcode = first & 0x0F

        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", _recv_exactly(self._socket, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", _recv_exactly(self._socket, 8))[0]

        if length > MAX_FRAME_BYTES:
            raise WebSocketError(
                f"A frame claimed to be {length:,} bytes, above the {MAX_FRAME_BYTES:,} "
                "this client will read."
            )
        if opcode in CONTROL_OPCODES and length > 125:
            raise WebSocketError("A control frame was longer than the RFC allows.")

        mask = _recv_exactly(self._socket, 4) if masked else b""
        payload = _recv_exactly(self._socket, length) if length else b""
        if masked:
            # Servers must not mask, but unmasking is well-defined either way and
            # refusing here would break against a non-conforming peer for no gain
            # in safety.
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))

        return fin, opcode, payload

    # ----------------------------------------------------------------- closing

    def close(self, code: int = 1000, reason: str = "") -> None:
        """Send a close frame and shut the socket. Safe to call twice."""
        try:
            if self._socket.fileno() != -1:
                payload = struct.pack("!H", code) + reason.encode("utf-8")[:123]
                _write_frame(self._socket, OP_CLOSE, payload)
        except (OSError, WebSocketError):
            # The peer may already be gone; the close below is what matters.
            pass
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError as exc:
            if exc.errno not in (errno.ENOTCONN, errno.EBADF, errno.EINVAL):
                raise
        finally:
            self._socket.close()

    @property
    def closed(self) -> bool:
        return self._socket.fileno() == -1

    def __enter__(self) -> "WebSocket":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def _write_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    """Write one complete, masked, unfragmented frame.

    Client-to-server frames must be masked (RFC 6455 §5.3); a server is entitled
    to drop the connection when they are not, so this is not optional.
    """
    length = len(payload)
    header = bytearray()
    header.append(0x80 | opcode)  # FIN set: this client never fragments on send.

    mask_bit = 0x80
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))

    mask = os.urandom(4)
    header.extend(mask)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _parse_close(payload: bytes) -> tuple[int | None, str]:
    if len(payload) < 2:
        return None, ""
    code = struct.unpack("!H", payload[:2])[0]
    try:
        reason = payload[2:].decode("utf-8")
    except UnicodeDecodeError:
        reason = ""
    return code, reason
