"""Native Grok WebSocket frames through the existing bounded ACP client.

This closes the transport only. The admitted run's existing lifecycle owner
must separately prove the leased native server and all its tools stopped.
"""
from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import urlsplit

try:
    from .grok_acp import AcpClient, AcpError
except ImportError:  # Private materialized runner copy.
    from grok_acp import AcpClient, AcpError


class _Input:
    def __init__(self, connection):
        self.connection, self.closed = connection, False

    def write(self, value):
        if self.closed:
            raise OSError("Native WebSocket transport is closed")
        from websockets.exceptions import ConnectionClosed
        try:
            self.connection.send(value.decode("utf-8").removesuffix("\n"))
        except ConnectionClosed as exc:
            raise OSError("Native WebSocket transport is closed") from exc
        return len(value)

    def flush(self):
        pass


class _Output:
    def __init__(self, connection):
        self.connection = connection

    def readline(self, limit):
        from websockets.exceptions import ConnectionClosed
        try:
            value = self.connection.recv()
        except ConnectionClosed as exc:
            if any(frame is not None and frame.code == 1009 for frame in (exc.rcvd, exc.sent)):
                raise AcpError("Grok ACP frame exceeds the configured limit") from exc
            return b""
        if not isinstance(value, str):
            raise AcpError("Native WebSocket returned a non-text frame")
        encoded = value.encode("utf-8") + b"\n"
        if len(encoded) > limit:
            raise AcpError("Native WebSocket frame exceeds the configured limit")
        return encoded


class WebSocketAcpClient(AcpClient):
    def __init__(self, url, secret, *, verify_listener, **kwargs):
        from websockets.sync.client import connect
        target = urlsplit(url)
        if (target.scheme != "ws" or target.hostname != "127.0.0.1"
                or target.path != "/ws" or target.username or target.password or target.query or target.fragment):
            raise AcpError("Native WebSocket transport requires its private loopback listener")
        if not verify_listener():
            raise AcpError("The selected native listener cannot be confirmed")
        frame_limit = kwargs.get("max_frame_bytes", 4 * 1024 * 1024)
        if type(frame_limit) is not int or frame_limit < 2:
            raise AcpError("Native WebSocket frame limit must be an integer of at least two bytes")
        from websockets.exceptions import WebSocketException
        try:
            self.connection = connect(url, additional_headers={"Authorization": "Bearer " + secret},
                open_timeout=2, close_timeout=0, max_size=frame_limit - 1, proxy=None)
        except (WebSocketException, TimeoutError) as exc:
            raise AcpError("Native WebSocket transport could not attach") from exc
        # Reuse ACP's one reader, bounded frames, reverse calls and request fences.
        # This object is an I/O channel, never a native process identity receipt.
        super().__init__(SimpleNamespace(stdin=_Input(self.connection), stdout=_Output(self.connection)), **kwargs)

    def close(self):
        # A native session has its own close/terminal acknowledgement. Do not add
        # a WebSocket closing-handshake timer: this is not process-stop proof.
        self.process.stdin.closed = True
        self.connection.close()
        self._fail(AcpError("Native WebSocket transport closed"))
        self._reader.join(timeout=2)
