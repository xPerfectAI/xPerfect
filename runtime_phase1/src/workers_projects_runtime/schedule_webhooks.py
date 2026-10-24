"""Bounded Standard Webhooks delivery; persistence and retries stay with the outbox.

The operator supplies network policy, while the service resolves protected owner
references before calling this module. Neither URLs, credentials nor response
bodies are returned in an outcome. The HTTP pool has no proxy, cookie or auth
machinery. A numeric socket connects to the exact address already checked here;
TLS still verifies the original hostname. This prevents a second DNS lookup.
"""
from __future__ import annotations

import base64
import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timezone
import ipaddress
import math
import select
import socket
import ssl
import threading
import time
from typing import Mapping
from urllib.parse import quote, urlsplit

import httpcore
from standardwebhooks.webhooks import Webhook


class WebhookPolicyError(ValueError):
    """A safe machine-readable rejection, containing no destination material."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WebhookNetworkPolicy:
    # Never populate these grants from a worker request or an untrusted payload.
    allow_local_http: bool = False
    allowed_ports: tuple[int, ...] = (443,)
    allowed_networks: tuple[str, ...] = ()

    def permits(self, value: str, *, local_http: bool = False) -> bool:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address):
            if address.ipv4_mapped:
                address = address.ipv4_mapped
            elif (address.sixtofour or address.teredo
                  or address in ipaddress.ip_network("64:ff9b::/96")
                  or address in ipaddress.ip_network("64:ff9b:1::/48")):
                # Transition gateways may translate a seemingly public IPv6
                # address into a private IPv4 target. Require an explicit grant.
                return (not local_http and any(address in ipaddress.ip_network(network)
                                               for network in self.allowed_networks))
        if address.is_unspecified or address.is_multicast:
            return False
        if local_http:
            return self.allow_local_http and address.is_loopback
        return address.is_global or any(address in ipaddress.ip_network(network) for network in self.allowed_networks)


@dataclass(frozen=True)
class WebhookLimits:
    total_seconds: float = 8.0
    dns_seconds: float = 2.0
    connect_seconds: float = 3.0
    tls_seconds: float = 3.0
    read_seconds: float = 3.0
    write_seconds: float = 3.0
    pool_seconds: float = 0.25
    request_bytes: int = 256 * 1024
    response_bytes: int = 64 * 1024
    header_bytes: int = 16 * 1024
    max_connections: int = 8
    dns_workers: int = 4
    keepalive_seconds: float = 30.0


@dataclass(frozen=True)
class WebhookOutcome:
    state: str
    error_code: str | None = None
    http_status: int | None = None
    retryable: bool = False
    response_bytes: int = 0
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.state == "accepted"


@dataclass
class _Attempt:
    deadline: float
    limits: WebhookLimits
    timings: dict[str, float] = field(default_factory=dict)

    def budget(self, seconds: float | None) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise httpcore.ReadTimeout()
        return min(remaining, seconds if seconds is not None else remaining)

    def add(self, name: str, start: float) -> None:
        self.timings[name] = self.timings.get(name, 0.0) + (time.monotonic() - start) * 1000


class _SocketStream(httpcore.NetworkStream):
    def __init__(self, sock: socket.socket, backend: "_PinnedBackend"):
        self.sock, self.backend = sock, backend

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        attempt, start = self.backend.attempt(), time.monotonic()
        try:
            self.sock.settimeout(attempt.budget(timeout))
            return self.sock.recv(max_bytes)
        except socket.timeout:
            raise httpcore.ReadTimeout() from None
        except OSError:
            raise httpcore.ReadError() from None
        finally:
            attempt.add("read_ms", start)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        attempt, start = self.backend.attempt(), time.monotonic()
        try:
            while buffer:
                self.sock.settimeout(attempt.budget(timeout))
                sent = self.sock.send(buffer)
                if not sent:
                    raise httpcore.WriteError()
                buffer = buffer[sent:]
        except socket.timeout:
            raise httpcore.WriteTimeout() from None
        except OSError:
            raise httpcore.WriteError() from None
        finally:
            attempt.add("write_ms", start)

    def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None,
                  timeout: float | None = None) -> "_SocketStream":
        attempt, start = self.backend.attempt(), time.monotonic()
        try:
            self.sock.settimeout(attempt.budget(attempt.limits.tls_seconds))
            wrapped = ssl_context.wrap_socket(self.sock, server_hostname=server_hostname)
            return _SocketStream(wrapped, self.backend)
        except socket.timeout:
            self.close()
            raise httpcore.ConnectTimeout() from None
        except OSError:
            self.close()
            raise httpcore.ConnectError() from None
        finally:
            attempt.add("tls_ms", start)

    def close(self) -> None:
        self.sock.close()

    def get_extra_info(self, info: str):
        if info == "ssl_object" and isinstance(self.sock, ssl.SSLSocket):
            return self.sock._sslobj
        if info == "server_addr":
            return self.sock.getpeername()
        if info == "client_addr":
            return self.sock.getsockname()
        if info == "socket":
            return self.sock
        if info == "is_readable":
            try:
                return bool(select.select([self.sock], [], [], 0)[0])
            except (ValueError, OSError):
                return True
        return None


class _PinnedBackend(httpcore.NetworkBackend):
    def __init__(self, policy: WebhookNetworkPolicy, limits: WebhookLimits):
        self.policy, self.limits = policy, limits
        self.local = threading.local()
        self.resolvers = concurrent.futures.ThreadPoolExecutor(max_workers=limits.dns_workers, thread_name_prefix="webhook-dns")
        self.dns_slots = threading.BoundedSemaphore(limits.dns_workers)

    def attempt(self) -> _Attempt:
        return self.local.attempt

    def addresses(self, host: str, port: int, *, local_http: bool) -> list[tuple]:
        attempt, start = self.attempt(), time.monotonic()
        try:
            # Literal addresses require no DNS and retain a numeric-only connect.
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if not self.dns_slots.acquire(blocking=False):
                    raise WebhookPolicyError("dns_capacity")
                try:
                    future = self.resolvers.submit(socket.getaddrinfo, host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
                except RuntimeError:
                    self.dns_slots.release()
                    raise WebhookPolicyError("transport_closed") from None
                future.add_done_callback(lambda _: self.dns_slots.release())
                try:
                    entries = future.result(timeout=attempt.budget(self.limits.dns_seconds))
                except concurrent.futures.TimeoutError:
                    future.cancel()
                    raise WebhookPolicyError("dns_timeout") from None
                except OSError:
                    raise WebhookPolicyError("dns_failed") from None
            else:
                entries = [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", (str(address), port, 0, 0))] if address.version == 6 else [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (str(address), port))]
            if not entries:
                raise WebhookPolicyError("dns_failed")
            checked = []
            for family, kind, proto, _name, sockaddr in entries:
                if family not in (socket.AF_INET, socket.AF_INET6) or not self.policy.permits(sockaddr[0], local_http=local_http):
                    raise WebhookPolicyError("network_denied")
                entry = (family, kind, proto, sockaddr)
                if entry not in checked:
                    checked.append(entry)
            return checked
        finally:
            attempt.add("resolve_ms", start)

    def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                    local_address: str | None = None, socket_options=None) -> _SocketStream:
        # No proxy implementation exists here; HTTP(S)_PROXY/ALL_PROXY are ignored.
        if local_address:
            raise WebhookPolicyError("network_denied")
        attempt = self.attempt()
        entries = self.addresses(host, port, local_http=self.local.local_http)
        start = time.monotonic()
        try:
            connect_deadline = min(attempt.deadline, start + (timeout or self.limits.connect_seconds))
            for family, kind, proto, sockaddr in entries:
                sock = socket.socket(family, kind, proto)
                try:
                    remaining = connect_deadline - time.monotonic()
                    if remaining <= 0:
                        raise httpcore.ConnectTimeout()
                    sock.settimeout(remaining)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    for option in socket_options or ():
                        sock.setsockopt(*option)
                    sock.connect(sockaddr)
                    # Check actual peer too, including mapped IPv6 addresses.
                    if not self.policy.permits(sock.getpeername()[0], local_http=self.local.local_http):
                        raise WebhookPolicyError("network_denied")
                    return _SocketStream(sock, self)
                except (OSError, httpcore.ConnectTimeout):
                    sock.close()
                except BaseException:
                    sock.close()
                    raise
            if time.monotonic() >= connect_deadline:
                raise httpcore.ConnectTimeout()
            raise httpcore.ConnectError()
        finally:
            attempt.add("connect_ms", start)

    def connect_unix_socket(self, *args, **kwargs):
        raise WebhookPolicyError("network_denied")


_RESERVED_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "te", "trailer",
    "upgrade", "expect", "content-encoding", "cookie", "set-cookie",
})


def _url(url: str, policy: WebhookNetworkPolicy) -> tuple[str, bool]:
    try:
        if not isinstance(url, str) or len(url) > 8192 or any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
            raise ValueError()
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment or "#" in url:
            raise ValueError()
        if parsed.scheme not in {"http", "https"} or port not in policy.allowed_ports or "%" in parsed.hostname:
            raise ValueError()
        host = parsed.hostname.encode("idna").decode("ascii")
        if parsed.scheme == "http" and not policy.allow_local_http:
            raise WebhookPolicyError("https_required")
        # httpcore constructs Host and handles IDNA authority; fragments are forbidden.
        hostpart = f"[{host}]" if ":" in host else host
        authority = hostpart + (f":{port}" if parsed.port else "")
        return f"{parsed.scheme}://{authority}{quote(parsed.path or '/', safe="/%:@!$&'()*+,;=-._~")}" + (f"?{quote(parsed.query, safe="%/:?@!$&'()*+,;=-._~")}" if parsed.query else ""), parsed.scheme == "http"
    except WebhookPolicyError:
        raise
    except (ValueError, UnicodeError, AttributeError):
        raise WebhookPolicyError("invalid_destination") from None


def _headers(headers: Mapping[str, str] | None, limits: WebhookLimits) -> list[tuple[bytes, bytes]]:
    output = []
    total = 0
    if headers is not None and not isinstance(headers, Mapping):
        raise WebhookPolicyError("invalid_headers")
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise WebhookPolicyError("invalid_headers")
        lower = name.lower()
        if not name or lower in _RESERVED_HEADERS or lower.startswith(("webhook-", "proxy-")) or any(not (char.isascii() and (char.isalnum() or char in "!#$%&'*+-.^_`|~")) for char in name):
            raise WebhookPolicyError("invalid_headers")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise WebhookPolicyError("invalid_headers")
        try:
            pair = (name.encode("ascii"), value.encode("ascii"))
        except UnicodeError:
            raise WebhookPolicyError("invalid_headers") from None
        total += len(pair[0]) + len(pair[1]) + 4
        if total > limits.header_bytes:
            raise WebhookPolicyError("headers_too_large")
        output.append(pair)
    return output


def _signing_key(secret: str | bytes) -> bytes:
    try:
        if isinstance(secret, str):
            if len(secret) > 4096:
                raise ValueError()
            encoded = secret.removeprefix("whsec_")
            key = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
        else:
            key = secret
        if not isinstance(key, bytes) or not 32 <= len(key) <= 3072:
            raise ValueError()
        return key
    except (ValueError, TypeError):
        raise WebhookPolicyError("invalid_signing_material") from None


def validate_destination_config(url: str, headers: Mapping[str, str] | None = None,
                                secret: str | bytes | None = None) -> None:
    """Validate private setup syntax without DNS or granting network authority.

    Actual address/port policy is always checked by the operator-owned sender.
    This function returns no credentials or destination material.
    """
    try:
        port = urlsplit(url).port or (443 if urlsplit(url).scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError()
        _url(url, WebhookNetworkPolicy(allow_local_http=True, allowed_ports=(port,)))
    except WebhookPolicyError:
        raise
    except (ValueError, TypeError, AttributeError):
        raise WebhookPolicyError("invalid_destination") from None
    _headers(headers, WebhookLimits())
    if secret is not None:
        _signing_key(secret)


class WebhookTransport:
    """Managed thread-safe HTTP/1.1 pool with a fixed operator network policy.

    Callers own bounded submissions and persisted retry times. A 2xx response is
    accepted delivery only. It says nothing about application work completion.
    All raw material is confined to this call; never persist the object's inputs
    in normal status or event records.
    """

    def __init__(self, *, policy: WebhookNetworkPolicy | None = None, limits: WebhookLimits | None = None):
        self.policy, self.limits = policy or WebhookNetworkPolicy(), limits or WebhookLimits()
        for value in vars(self.limits).values():
            if not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError("invalid_webhook_limits")
        if not self.policy.allowed_ports or any(not 1 <= port <= 65535 for port in self.policy.allowed_ports):
            raise ValueError("invalid_webhook_network_policy")
        for network in self.policy.allowed_networks:
            ipaddress.ip_network(network)
        self.backend = _PinnedBackend(self.policy, self.limits)
        self.pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(), network_backend=self.backend,
            max_connections=self.limits.max_connections,
            max_keepalive_connections=self.limits.max_connections,
            keepalive_expiry=self.limits.keepalive_seconds, http1=True, http2=False, retries=0,
        )
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.pool.close()
        self.backend.resolvers.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "WebhookTransport":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def send(self, *, url: str, secret: str | bytes, event_id: str, body: bytes,
             expires_at: datetime | None = None, headers: Mapping[str, str] | None = None) -> WebhookOutcome:
        start = time.monotonic()
        remaining = self.limits.total_seconds
        if expires_at is not None:
            if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
                return WebhookOutcome("rejected", "invalid_expiry")
            remaining = min(remaining, (expires_at - datetime.now(timezone.utc)).total_seconds())
        if remaining <= 0:
            return WebhookOutcome("expired", "expired")
        attempt = _Attempt(start + remaining, self.limits)
        self.backend.local.attempt = attempt
        size, status = 0, None
        state, code, retryable = "rejected", None, False
        try:
            if self.closed:
                raise WebhookPolicyError("transport_closed")
            if not isinstance(body, bytes) or len(body) > self.limits.request_bytes:
                raise WebhookPolicyError("payload_too_large")
            if not isinstance(event_id, str) or not event_id or len(event_id) > 200 or any(ord(char) <= 32 or ord(char) >= 127 for char in event_id):
                raise WebhookPolicyError("invalid_event_id")
            destination, local_http = _url(url, self.policy)
            self.backend.local.local_http = local_http
            request_headers = _headers(headers, self.limits)
            try:
                text = body.decode("utf-8")
                key = _signing_key(secret)
                stamp = datetime.now(timezone.utc)
                sign_start = time.monotonic()
                signature = Webhook(key).sign(event_id, stamp, text)
                attempt.add("sign_ms", sign_start)
            except (ValueError, UnicodeError, TypeError):
                raise WebhookPolicyError("invalid_signing_material") from None
            request_headers.extend([
                (b"webhook-id", event_id.encode("ascii")),
                (b"webhook-timestamp", str(math.floor(stamp.timestamp())).encode("ascii")),
                (b"webhook-signature", signature.encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
            ])
            if not any(name.lower() == b"content-type" for name, _ in request_headers):
                request_headers.append((b"content-type", b"application/json"))
            attempt.budget(None)
            dispatch = time.monotonic()
            with self.pool.stream("POST", destination, headers=request_headers, content=body,
                                  extensions={"timeout": {"connect": self.limits.connect_seconds,
                                                         "read": self.limits.read_seconds,
                                                         "write": self.limits.write_seconds,
                                                         "pool": attempt.budget(self.limits.pool_seconds)}}) as response:
                attempt.timings["to_response_headers_ms"] = (time.monotonic() - dispatch) * 1000
                status = response.status
                if sum(len(name) + len(value) + 4 for name, value in response.headers) > self.limits.header_bytes:
                    raise WebhookPolicyError("response_headers_too_large")
                for chunk in response.iter_stream():
                    attempt.budget(None)
                    size += len(chunk)
                    if size > self.limits.response_bytes:
                        raise WebhookPolicyError("response_too_large")
                state = "accepted" if 200 <= status < 300 else "rejected"
                code = None if state == "accepted" else "http_rejected"
                retryable = status in (408, 425, 429) or status >= 500
        except WebhookPolicyError as exc:
            code = exc.code
            state = "unacknowledged" if status is not None else "rejected"
            retryable = code in {"dns_timeout", "dns_failed", "dns_capacity"}
        except httpcore.TimeoutException:
            state, code, retryable = "unacknowledged", "attempt_timeout", True
        except httpcore.NetworkError:
            state, code, retryable = "unacknowledged", "connection_failed", True
        except (httpcore.ProtocolError, httpcore.UnsupportedProtocol):
            state, code, retryable = "unacknowledged", "protocol_error", False
        finally:
            # A valid 2xx header is an ACK already received. A broken or large
            # optional response body cannot undo it or authorize another send.
            if status is not None and 200 <= status < 300:
                state, retryable = "accepted", False
            attempt.timings["total_ms"] = (time.monotonic() - start) * 1000
            self.backend.local.attempt = None
        return WebhookOutcome(state, code, status, retryable, min(size, self.limits.response_bytes + 1),
                              {key: round(value, 3) for key, value in attempt.timings.items()})
