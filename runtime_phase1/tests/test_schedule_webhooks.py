"""Causal transport checks: real isolated HTTP plus deterministic network boundaries."""
from __future__ import annotations

import base64
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

import pytest
from standardwebhooks.webhooks import Webhook

from workers_projects_runtime.schedule_webhooks import (
    WebhookLimits, WebhookNetworkPolicy, WebhookTransport,
)

SECRET = "whsec_" + base64.b64encode(b"synthetic-webhook-signing-material!").decode()
BODY = '{"event":"scheduled","text":"héllo"}'.encode()


@pytest.fixture
def receiver():
    requests, behavior = [], {"status": 204, "body": b"", "delay": 0.0}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append({"path": self.path, "headers": dict(self.headers), "body": raw,
                             "client_port": self.client_address[1]})
            time.sleep(behavior["delay"])
            try:
                self.send_response(behavior["status"])
                self.send_header("Content-Length", str(len(behavior["body"])))
                self.send_header("Set-Cookie", "synthetic=never-forward")
                if behavior.get("location"):
                    self.send_header("Location", behavior["location"])
                self.end_headers()
                if behavior.get("dribble"):
                    for char in behavior["body"]:
                        self.wfile.write(bytes([char]))
                        self.wfile.flush()
                        time.sleep(behavior["dribble"])
                else:
                    self.wfile.write(behavior["body"])
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    policy = WebhookNetworkPolicy(allow_local_http=True, allowed_ports=(443, port))
    with WebhookTransport(policy=policy) as transport:
        yield transport, f"http://127.0.0.1:{port}/receive", requests, behavior
    server.shutdown()
    server.server_close()
    thread.join(2)


def send(transport, url, **kwargs):
    return transport.send(url=url, secret=SECRET, event_id="evt_synthetic-1", body=BODY, **kwargs)


def test_actual_http_exact_signed_payload_and_managed_pool(receiver, monkeypatch):
    transport, url, requests, _ = receiver
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    first = send(transport, url, headers={"Authorization": "Bearer synthetic-one"})
    second = send(transport, url)
    assert first.accepted and second.accepted
    assert first.http_status == 204
    assert requests[0]["body"] == BODY
    Webhook(SECRET).verify(requests[0]["body"], requests[0]["headers"])
    assert requests[0]["headers"]["Authorization"] == "Bearer synthetic-one"
    assert "Authorization" not in requests[1]["headers"]
    assert "Cookie" not in requests[1]["headers"]
    assert requests[0]["client_port"] == requests[1]["client_port"]
    assert "connect_ms" in first.timings_ms
    assert "connect_ms" not in second.timings_ms  # genuine connection reuse
    assert first.timings_ms["sign_ms"] >= 0
    assert first.timings_ms["total_ms"] > 0


def test_no_secret_destination_or_response_material_in_outcome(receiver):
    transport, url, _, behavior = receiver
    behavior.update(status=503, body=b"synthetic-private-response")
    outcome = send(transport, url + "/synthetic-secret?token=private", headers={"Authorization": "Bearer private"})
    serialized = json.dumps(asdict(outcome))
    for sensitive in (SECRET, "synthetic-secret", "token=private", "synthetic-private-response", "Bearer private", "127.0.0.1"):
        assert sensitive not in serialized
    assert outcome.error_code == "http_rejected" and outcome.retryable


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "https://127.0.0.1/", "https://[::1]/",
    "https://[::ffff:127.0.0.1]/", "https://169.254.169.254/",
    "https://10.0.0.1/", "https://[fe80::1]/", "https://0.0.0.0/",
    "https://224.0.0.1/", "https://[ff02::1]/", "https://192.168.1.1/",
])
def test_hosted_network_deny_default(url):
    with WebhookTransport() as transport:
        result = send(transport, url)
    assert result.state == "rejected"
    assert result.error_code in {"https_required", "invalid_destination", "network_denied"}


@pytest.mark.parametrize("url", [
    "https://user:synthetic@example.invalid/", "https://example.invalid/#fragment",
    "https://example.invalid:9443/", "ftp://example.invalid/", "file:///etc/passwd",
    "https://example.invalid/\r\nHeader: injected", "https://example.invalid\\@127.0.0.1/",
    "https://[fe80::1%25en0]/", "https://example.invalid/#",
])
def test_invalid_url_never_opens_network(url, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: pytest.fail("must reject before DNS"))
    with WebhookTransport() as transport:
        result = send(transport, url)
    assert result.error_code == "invalid_destination"


def test_dns_mixed_public_private_fails_closed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 443, 0, 0)),
    ])
    with WebhookTransport() as transport:
        assert send(transport, "https://synthetic.example/").error_code == "network_denied"


def test_dns_is_resolved_once_then_numeric_connection_and_reuse(receiver, monkeypatch):
    transport, url, requests, _ = receiver
    calls = []

    def resolve(host, port, *_):
        calls.append((host, port))
        # A vulnerable second lookup would get this hostile alternate response.
        address = "127.0.0.1" if len(calls) == 1 else "169.254.169.254"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    dns_url = url.replace("127.0.0.1", "synthetic.example")
    assert send(transport, dns_url).accepted
    assert send(transport, dns_url).accepted
    assert len(calls) == 1 and len(requests) == 2
    assert requests[0]["headers"]["Host"].startswith("synthetic.example:")


def test_dns_timeout_is_bounded_and_capacity_not_unbounded(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: (release.wait(1), [])[1])
    limits = replace(WebhookLimits(), dns_seconds=0.02, total_seconds=0.1, dns_workers=1)
    try:
        with WebhookTransport(limits=limits) as transport:
            start = time.monotonic()
            assert send(transport, "https://synthetic.example/").error_code == "dns_timeout"
            assert time.monotonic() - start < 0.3
            assert send(transport, "https://other.example/").error_code == "dns_capacity"
    finally:
        release.set()


@pytest.mark.parametrize("headers", [
    {"Webhook-Id": "forged"}, {"webhook-signature": "forged"}, {"Host": "forged"},
    {"Content-Length": "1"}, {"Transfer-Encoding": "chunked"}, {"Proxy-Authorization": "synthetic"},
    {"Cookie": "synthetic"}, {"Bad\r\nHeader": "synthetic"}, {"Authorization": "bad\nvalue"},
])
def test_header_overrides_rejected_before_delivery(receiver, headers):
    transport, url, requests, _ = receiver
    result = send(transport, url, headers=headers)
    assert result.error_code == "invalid_headers" and not requests


def test_redirect_is_not_followed(receiver):
    transport, url, requests, behavior = receiver
    behavior.update(status=307, location=url + "/redirect-canary")
    result = send(transport, url)
    assert result.http_status == 307 and not result.accepted and not result.retryable
    assert len(requests) == 1


@pytest.mark.parametrize("status,retryable", [(200, False), (202, False), (400, False), (401, False), (404, False), (408, True), (429, True), (500, True)])
def test_http_results_are_truthful(receiver, status, retryable):
    transport, url, _, behavior = receiver
    behavior["status"] = status
    result = send(transport, url)
    assert result.accepted is (200 <= status < 300)
    assert result.retryable is retryable


def test_expired_is_not_sent_and_active_attempt_uses_remaining_budget(receiver):
    transport, url, requests, behavior = receiver
    expired = send(transport, url, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert expired.state == "expired" and not requests
    behavior["delay"] = 0.2
    start = time.monotonic()
    result = send(transport, url, expires_at=datetime.now(timezone.utc) + timedelta(seconds=0.04))
    assert time.monotonic() - start < 0.18
    assert result.state == "unacknowledged" and result.error_code == "attempt_timeout"


def test_dribbling_response_cannot_extend_total_budget(receiver):
    original, url, _, behavior = receiver
    behavior.update(status=200, body=b"abcdefghij", dribble=0.02)
    with WebhookTransport(policy=original.policy, limits=replace(WebhookLimits(), total_seconds=0.07)) as transport:
        start = time.monotonic()
        result = send(transport, url)
    assert time.monotonic() - start < 0.18
    assert result.error_code == "attempt_timeout"
    assert result.accepted and not result.retryable  # known ACK survives body timeout


def test_request_response_and_header_bounds(receiver):
    original, url, requests, behavior = receiver
    with WebhookTransport(policy=original.policy, limits=replace(WebhookLimits(), request_bytes=10)) as transport:
        assert send(transport, url).error_code == "payload_too_large"
    assert not requests
    behavior.update(status=200, body=b"123456789")
    with WebhookTransport(policy=original.policy, limits=replace(WebhookLimits(), response_bytes=4)) as transport:
        result = send(transport, url)
        assert result.error_code == "response_too_large" and result.accepted and not result.retryable
    with WebhookTransport(policy=original.policy, limits=replace(WebhookLimits(), header_bytes=8)) as transport:
        assert send(transport, url, headers={"X-Synthetic": "too-large"}).error_code == "headers_too_large"


def test_key_validation_event_id_expiry_and_closed_transport(receiver):
    transport, url, _, _ = receiver
    for key in ("not-a-base64-key!", "", b"too-short"):
        result = transport.send(url=url, secret=key, event_id="evt_test", body=BODY)
        assert result.error_code == "invalid_signing_material"
    assert transport.send(url=url, secret=SECRET, event_id="bad\nevent", body=BODY).error_code == "invalid_event_id"
    assert send(transport, url, expires_at=datetime.now()).error_code == "invalid_expiry"
    transport.close()
    assert send(transport, url).error_code == "transport_closed"


def test_operator_network_grants_are_explicit():
    hosted = WebhookNetworkPolicy()
    scoped = WebhookNetworkPolicy(allowed_networks=("10.20.0.0/16",))
    assert not hosted.permits("10.20.1.1")
    assert scoped.permits("10.20.1.1") and not scoped.permits("10.21.1.1")
    assert not scoped.permits("10.20.1.1", local_http=True)
    assert not WebhookNetworkPolicy(allowed_networks=("0.0.0.0/0",)).permits("0.0.0.0")


def test_ipv6_transition_paths_cannot_hide_private_targets():
    policy = WebhookNetworkPolicy()
    for address in ("2002:7f00:1::", "2001:0:4136:e378:8000:63bf:3fff:fdd2", "64:ff9b::a9fe:a9fe", "64:ff9b:1::a00:1"):
        assert not policy.permits(address)
    assert policy.permits("2606:4700:4700::1111")


def test_real_ipv6_local_loopback_when_available():
    class IPv6Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    try:
        server = IPv6Server(("::1", 0), Handler)
    except OSError:
        pytest.skip("IPv6 loopback is unavailable on this test host")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        policy = WebhookNetworkPolicy(allow_local_http=True, allowed_ports=(server.server_port,))
        with WebhookTransport(policy=policy) as transport:
            assert send(transport, f"http://[::1]:{server.server_port}/receive").accepted
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_numeric_fallback_only_uses_validated_addresses(receiver, monkeypatch):
    transport, url, requests, _ = receiver
    port = int(url.split(":")[2].split("/")[0])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", port)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
    ])
    original_connect = socket.socket.connect
    connected = []

    def connect(sock, address):
        connected.append(address[0])
        if address[0] == "127.0.0.2":
            raise ConnectionRefusedError("synthetic first-address failure")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    assert send(transport, url.replace("127.0.0.1", "synthetic.example")).accepted
    assert connected == ["127.0.0.2", "127.0.0.1"]
    assert len(requests) == 1


def test_real_tls_rejects_untrusted_certificate_without_sending_payload(tmp_path):
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(key, hashes.SHA256()))
    certfile, keyfile = tmp_path / "cert.pem", tmp_path / "key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    payloads = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payloads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        policy = WebhookNetworkPolicy(allowed_networks=("127.0.0.1/32",), allowed_ports=(server.server_port,))
        with WebhookTransport(policy=policy) as transport:
            result = send(transport, f"https://127.0.0.1:{server.server_port}/receive?secret=canary")
        assert result.error_code == "connection_failed" and not payloads
        assert "secret" not in json.dumps(asdict(result))
        assert "tls_ms" in result.timings_ms
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_destination_setup_validation_has_no_network_or_secret_echo(monkeypatch):
    from workers_projects_runtime.schedule_webhooks import validate_destination_config, WebhookPolicyError
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_: pytest.fail("setup must not resolve DNS"))
    assert validate_destination_config("https://example.invalid/private?secret=synthetic", {"Authorization": "Bearer synthetic"}, SECRET) is None
    assert validate_destination_config("http://127.0.0.1:9432/", {}, None) is None
    for url, headers, secret in [("file:///private", {}, None), ("https://user:private@example.invalid/", {}, None),
                                  ("https://example.invalid/", {"Webhook-Id": "forged"}, None),
                                  ("https://example.invalid/", {}, "private-invalid")]:
        with pytest.raises(WebhookPolicyError) as error:
            validate_destination_config(url, headers, secret)
        assert "private" not in str(error.value)
