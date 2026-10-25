"""Legacy takeover pages must not freeze a desktop as an opening side effect."""
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from test_api import DesktopStubRuntime


class _Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.hrefs = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(value for key, value in attrs if key == "href")


class _ObservedDesktop(DesktopStubRuntime):
    def __init__(self, root, managed):
        super().__init__(root)
        self.managed = managed
        self.controls = []

    def describe_worker(self, worker):
        details = super().describe_worker(worker)
        if self.managed:
            details.update(desktop_supported=True, desktop_actions=["browser", "focus_browser"])
        return details

    def pause_worker(self, worker):
        self.controls.append("pause")
        return super().pause_worker(worker)

    def interrupt_worker(self, worker, run_id=None):
        self.controls.append("interrupt")
        return super().interrupt_worker(worker, run_id)

    def desktop_action(self, *args, **kwargs):
        self.controls.append("desktop")
        return super().desktop_action(*args, **kwargs)


@pytest.mark.parametrize("managed", [False, True], ids=["legacy-container", "managed-display"])
@pytest.mark.parametrize("paused", [False, True], ids=["ready", "paused"])
def test_legacy_pages_open_watch_without_automatic_pause(tmp_path, monkeypatch, managed, paused):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://operator.example.test")
    runtime = _ObservedDesktop(tmp_path / "desktop", managed)
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub", runtime=runtime)
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={
            "owner_id": "example-owner", "title": "Desktop controls", "goal": "Use the existing operator surface"}).json()
        created = client.post(f"/v1/projects/{project['project_id']}/workers", json={
            "owner_id": "example-owner", "name": "Desktop worker", "role": "operator", "profile": "codex-cli"})
        assert created.status_code == 201, created.text
        worker = created.json()
        wid = worker["worker_id"]
        if paused:
            response = client.post(f"/v1/workers/{wid}/pause")
            assert response.status_code == 202, response.text
        before = app.state.store.get_worker(wid)
        before_runs = app.state.store.list_runs_for_worker(wid)
        runtime.controls.clear()

        # API takeover remains a compatibility page; MCP exposes it as a
        # secondary runtime URL while preferring its configured Watch URL.
        takeover = client.get(f"/v1/workers/{wid}/takeover")
        assert takeover.status_code == 200, takeover.text
        assert urlsplit(takeover.json()["url"]).path == f"/ui/workers/{wid}/view"
        for path in (f"/ui/projects/{project['project_id']}", f"/ui/workers/{wid}",
                     urlsplit(takeover.json()["url"]).path):
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert "This desktop is shared with this worker, including anything you open or sign in to." in response.text
            assert "Open Watch / Steer" in response.text
            assert "Pause + Open Desktop" not in response.text
            assert "pauseAndOpen" not in response.text
            assert "press <code>Pause</code>" not in response.text
            links = [urlsplit(href) for href in _Links(response.text).hrefs]
            assert any(link.netloc == "operator.example.test" and link.path == f"/watch/{wid}"
                       and parse_qs(link.query) == {"surface": ["desktop"], "project_id": [project["project_id"]]}
                       for link in links)
        after = app.state.store.get_worker(wid)
        assert after["state"] == before["state"] == ("paused" if paused else "ready")
        assert after == before
        assert app.state.store.list_runs_for_worker(wid) == before_runs
        assert runtime.controls == [], "Opening compatibility pages must not pause, interrupt or launch a desktop"
