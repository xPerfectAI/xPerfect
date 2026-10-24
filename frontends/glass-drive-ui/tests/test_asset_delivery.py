"""Mutable frontend assets revalidate without changing routes or file semantics."""
from hashlib import sha256
from html.parser import HTMLParser
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
import pytest

import glass_drive_ui.server as server_module
from test_server import FakeRuntimeClient, clear_glasshive_ui_env  # noqa: F401


@pytest.fixture
def assets(tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    for name, content in {
        "index.html": "<title>Asset fixture</title>",
        "conversation.html": (
            '<link href="/static/conversation.css?v=old">'
            '<script src="/static/conversation.js?v=old"></script>'
        ),
        "conversation.css": "body { color: black; }",
        "conversation.js": 'import "./modules/child.js?v=old";',
        "modules/child.js": 'export const value = "first";',
        "vendor/library.js": 'export const vendor = "unchanged";',
        "symbol.svg": '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
    }.items():
        path = static / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(server_module, "STATIC_DIR", static)
    with TestClient(server_module.create_app(runtime_client=FakeRuntimeClient())) as client:
        yield static, client


@pytest.mark.parametrize("asset,media", [
    ("conversation.css", "text/css"),
    ("conversation.js", "javascript"),
    ("modules/child.js", "javascript"),
    ("vendor/library.js", "javascript"),
    ("symbol.svg", "image/svg+xml"),
])
def test_static_get_head_and_conditional_requests_keep_native_semantics(assets, asset, media):
    static, client = assets
    url = f"/static/{asset}?v=unchanged-url"
    first = client.get(url)
    assert first.status_code == 200
    assert first.content == (static / asset).read_bytes()
    assert media in first.headers["content-type"]
    assert first.headers["cache-control"] == "no-cache"
    assert first.headers["last-modified"]
    etag = first.headers["etag"]

    for headers in (
        {"If-None-Match": etag},
        {"If-None-Match": f"W/{etag}"},
        {"If-Modified-Since": first.headers["last-modified"]},
    ):
        unchanged = client.get(url, headers=headers)
        assert unchanged.status_code == 304
        assert unchanged.content == b""
        assert unchanged.headers["etag"] == etag
        assert unchanged.headers["cache-control"] == "no-cache"

    head = client.head(url)
    assert head.status_code == 200
    assert head.content == b""
    for header in ("content-type", "content-length", "etag", "last-modified", "cache-control"):
        assert head.headers[header] == first.headers[header]


def test_changed_transitive_module_replaces_old_cached_bytes(assets):
    static, client = assets
    url = "/static/modules/child.js?v=same-generation"
    first = client.get(url)
    path = static / "modules/child.js"
    previous_mtime = path.stat().st_mtime
    path.write_text('export const value = "second";', encoding="utf-8")
    os.utime(path, (previous_mtime + 2, previous_mtime + 2))

    changed = client.get(url, headers={
        "If-None-Match": first.headers["etag"],
        "If-Modified-Since": first.headers["last-modified"],
    })
    assert changed.status_code == 200
    assert changed.content == path.read_bytes()
    assert changed.headers["etag"] != first.headers["etag"]
    assert changed.headers["last-modified"] != first.headers["last-modified"]
    assert changed.headers["cache-control"] == "no-cache"

    # An obsolete ETag must not be overridden by a matching date validator.
    assert client.get(url, headers={
        "If-None-Match": first.headers["etag"],
        "If-Modified-Since": changed.headers["last-modified"],
    }).status_code == 200
    assert client.get(url, headers={"If-None-Match": changed.headers["etag"]}).status_code == 304


def test_static_ranges_errors_and_path_boundary_are_preserved(assets, tmp_path):
    static, client = assets
    partial = client.get("/static/conversation.js", headers={"Range": "bytes=0-5"})
    assert partial.status_code == 206
    assert partial.content == (static / "conversation.js").read_bytes()[:6]
    assert partial.headers["content-range"].startswith("bytes 0-5/")
    assert partial.headers["cache-control"] == "no-cache"
    assert client.get("/static/missing.js").status_code == 404
    assert client.post("/static/conversation.js").status_code == 405

    outside = tmp_path / "outside.js"
    outside.write_text("private fixture", encoding="utf-8")
    (static / "outside.js").symlink_to(outside)
    assert client.get("/static/outside.js").status_code == 404
    assert client.get("/static/%2e%2e/outside.js").status_code == 404


class AssetReferences(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.urls = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.urls.extend(value for name, value in attrs if name in {"src", "href"})


@pytest.mark.parametrize("suffix", [
    "",
    "?v=old",
    "?mode=fixture&amp;v=old&amp;blank=#section",
])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_chat_fingerprints_unversioned_and_versioned_attributes(assets, suffix, quote):
    static, client = assets
    markup = (
        f"<link href={quote}/static/conversation.css{suffix}{quote}>"
        f"<script src={quote}/static/conversation.js{suffix}{quote}></script>"
        '<script src="https://example.invalid/static/conversation.js?v=external"></script>'
        '<script src="/static/conversation.js.map?v=other"></script>'
    )
    (static / "conversation.html").write_text(markup, encoding="utf-8")
    response = client.get("/conversation")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    references = AssetReferences(response.text).urls
    for asset, url in zip(("conversation.css", "conversation.js"), references[:2]):
        parsed = urlparse(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        assert parsed.path == f"/static/{asset}"
        assert query["v"] == [sha256((static / asset).read_bytes()).hexdigest()[:16]]
        if "mode" in suffix:
            assert query["mode"] == ["fixture"]
            assert query["blank"] == [""]
            assert parsed.fragment == "section"
        else:
            assert set(query) == {"v"}
    assert references[2:] == [
        "https://example.invalid/static/conversation.js?v=external",
        "/static/conversation.js.map?v=other",
    ]


def test_real_chat_markup_uses_current_fingerprint_and_changes_with_bytes(assets):
    static, client = assets
    source_static = Path(server_module.__file__).parent / "static"
    (static / "conversation.html").write_bytes((source_static / "conversation.html").read_bytes())
    first = client.get("/conversation")
    first_urls = AssetReferences(first.text).urls
    for name in ("conversation.css", "conversation.js"):
        expected = sha256((static / name).read_bytes()).hexdigest()[:16]
        assert f"/static/{name}?v={expected}" in first_urls

    (static / "conversation.js").write_text('export const revision = "new";', encoding="utf-8")
    second = client.get("/conversation")
    second_urls = AssetReferences(second.text).urls
    expected = sha256((static / "conversation.js").read_bytes()).hexdigest()[:16]
    assert f"/static/conversation.js?v={expected}" in second_urls
    assert first_urls != second_urls
    assert first.headers["cache-control"] == second.headers["cache-control"] == "no-store"
    assert client.get("/").headers["cache-control"] == "no-store"
