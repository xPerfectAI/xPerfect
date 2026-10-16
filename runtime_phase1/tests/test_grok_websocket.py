"""The native socket transport preserves existing ACP error and frame limits."""
from unittest.mock import Mock
import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close
from workers_projects_runtime.grok_acp import AcpError
from workers_projects_runtime.grok_websocket import _Output, WebSocketAcpClient


def test_listener_identity_is_proved_before_secret_is_sent(monkeypatch):
    connect = Mock()
    monkeypatch.setattr("websockets.sync.client.connect", connect)
    with pytest.raises(AcpError, match="listener"):
        WebSocketAcpClient("ws://127.0.0.1:12345/ws", "synthetic", verify_listener=lambda: False)
    connect.assert_not_called()


def test_websocket_native_size_rejection_keeps_typed_acp_error():
    connection = Mock()
    connection.recv.side_effect = ConnectionClosedError(None, Close(1009, "oversize"))
    with pytest.raises(AcpError, match="exceeds the configured limit"):
        _Output(connection).readline(128)


def test_binary_native_frames_are_rejected():
    connection = Mock()
    connection.recv.return_value = b"binary"
    with pytest.raises(AcpError, match="non-text"):
        _Output(connection).readline(128)


def test_utf8_native_frame_is_bounded_in_bytes_including_acp_newline():
    connection = Mock()
    connection.recv.return_value = "éé"
    assert _Output(connection).readline(5) == "éé\n".encode()
    with pytest.raises(AcpError, match="limit"):
        _Output(connection).readline(4)


def test_transport_uses_the_same_frame_limit_as_existing_acp(monkeypatch):
    connect = Mock(return_value=Mock())
    monkeypatch.setattr("websockets.sync.client.connect", connect)
    monkeypatch.setattr("workers_projects_runtime.grok_websocket.AcpClient.__init__", lambda *args, **kwargs: None)
    WebSocketAcpClient("ws://127.0.0.1:12345/ws", "synthetic", verify_listener=lambda: True, max_frame_bytes=512)
    assert connect.call_args.kwargs["max_size"] == 511
