"""An install may show a channel brand's own mark from its own state; the repository ships none."""
import os

from fastapi.testclient import TestClient
import pytest

import glass_drive_ui.server as server_module
from test_server import FakeRuntimeClient, clear_glasshive_ui_env  # noqa: F401

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2 2"><rect width="2" height="2"/></svg>'


@pytest.fixture
def client():
    with TestClient(server_module.create_app(runtime_client=FakeRuntimeClient())) as test_client:
        yield test_client


def test_without_a_brand_assets_folder_nothing_is_offered(client, monkeypatch):
    monkeypatch.delenv("XPERFECT_BRAND_ASSETS_DIR", raising=False)
    assert client.get("/brand-assets.json").json() == {"channels": {}}
    assert client.get("/brand-assets/channel-teams.svg").status_code == 404


def test_an_owner_placed_mark_is_served_by_the_page_itself(client, monkeypatch, tmp_path):
    (tmp_path / "channel-teams.svg").write_text(SVG)
    (tmp_path / "channel-slack.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    monkeypatch.setenv("XPERFECT_BRAND_ASSETS_DIR", str(tmp_path))
    listed = client.get("/brand-assets.json")
    assert listed.json() == {"channels": {"teams": "/brand-assets/channel-teams.svg", "slack": "/brand-assets/channel-slack.png"}}
    assert listed.headers["cache-control"] == "no-store"
    served = client.get("/brand-assets/channel-teams.svg")
    assert served.status_code == 200 and served.text == SVG
    assert served.headers["content-type"].startswith("image/svg+xml")
    assert served.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in served.headers["content-security-policy"]


def test_only_the_known_names_are_served_and_never_a_link_or_an_oversized_file(client, monkeypatch, tmp_path):
    (tmp_path / "notes.txt").write_text("private")
    (tmp_path / "elsewhere.svg").write_text(SVG)
    os.symlink(tmp_path / "elsewhere.svg", tmp_path / "channel-teams.svg")
    (tmp_path / "channel-slack.svg").write_text("x" * (server_module.BRAND_ASSET_MAX_BYTES + 1))
    monkeypatch.setenv("XPERFECT_BRAND_ASSETS_DIR", str(tmp_path))
    assert client.get("/brand-assets.json").json() == {"channels": {}}
    for name in ("notes.txt", "elsewhere.svg", "channel-teams.svg", "channel-slack.svg", "..%2Fnotes.txt"):
        assert client.get(f"/brand-assets/{name}").status_code == 404, name
