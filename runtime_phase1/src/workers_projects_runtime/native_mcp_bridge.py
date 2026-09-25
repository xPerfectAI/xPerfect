"""Stdio MCP bridge for native workers in packaged deployments.

The native harness starts this program as an ordinary stdio MCP server. Each
newline-delimited JSON-RPC message is forwarded to one fixed runtime MCP
endpoint through the Unix socket the runtime serves in this workspace box; no
other container can reach that socket. The run-scoped bearer arrives through
the harness environment and the bridge never writes it anywhere. Standard
library only: it runs from source inside the native worker image.

Usage: python3 -c <this source> <http+unix-endpoint> <token-environment-name>
"""

import http.client
import json
import os
import socket
import sys
import urllib.parse

# The runtime's MCP servers admit this host name for in-package callers.
HOST, PORT = "runtime", 8766


def _error(ident, message):
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32000, "message": message}}


def _messages(body, content_type):
    text = body.decode("utf-8").strip()
    if not text:
        return []
    if "text/event-stream" in content_type:
        payloads = [
            line[5:].strip() for line in text.splitlines() if line.startswith("data:")
        ]
        return [json.loads(item) for item in payloads if item]
    value = json.loads(text)
    return value if isinstance(value, list) else [value]


def _endpoint(value):
    parsed = urllib.parse.urlsplit(value)
    socket_path = urllib.parse.unquote(parsed.netloc)
    if (parsed.scheme != "http+unix" or not socket_path.startswith("/")
            or not parsed.path.startswith("/") or parsed.query or parsed.fragment):
        return None
    return socket_path, parsed.path


class _SocketConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout):
        super().__init__(HOST, PORT, timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(self.socket_path)
        except OSError:
            connection.close()
            raise
        self.sock = connection


def _post(socket_path, path, body, headers, timeout=120):
    connection = _SocketConnection(socket_path, timeout)
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type", ""), response.read()
    finally:
        connection.close()


def main(argv, stdin, stdout, environ, post=_post):
    endpoint = _endpoint(argv[1]) if len(argv) == 3 else None
    if endpoint is None:
        sys.stderr.write("usage: bridge <http+unix-endpoint> <token-environment-name>\n")
        return 2
    socket_path, path = endpoint
    token = environ.get(argv[2], "")
    protocol = ""
    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        ident = message.get("id") if isinstance(message, dict) else None
        if not token:
            if ident is not None:
                stdout.write(json.dumps(_error(ident, "xPerfect native access is unavailable")) + "\n")
                stdout.flush()
            continue
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if protocol:
            headers["MCP-Protocol-Version"] = protocol
        try:
            status, content_type, body = post(socket_path, path, raw.encode("utf-8"), headers)
            if status >= 400:
                replies = [_error(ident, f"xPerfect native endpoint returned HTTP {status}")] if ident is not None else []
            else:
                replies = _messages(body, content_type)
        except (OSError, ValueError, http.client.HTTPException):
            replies = [_error(ident, "xPerfect native endpoint is unavailable")] if ident is not None else []
        for reply in replies:
            result = reply.get("result") if isinstance(reply, dict) else None
            if isinstance(result, dict) and isinstance(result.get("protocolVersion"), str):
                protocol = result["protocolVersion"]
            stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
            stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv, sys.stdin, sys.stdout, os.environ))
