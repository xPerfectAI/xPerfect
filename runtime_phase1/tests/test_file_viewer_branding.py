"""The signed file viewer is a human page: it carries the product name."""
from pathlib import Path

from fastapi.testclient import TestClient

from test_api import DeliverableDesktopRuntime
from workers_projects_runtime.api import create_app


def test_file_viewer_title_and_masthead_say_xperfect(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "signed-link-secret")
    app = create_app(str(tmp_path / "runtime.db"), runtime_backend="stub",
                     runtime=DeliverableDesktopRuntime(tmp_path / "desktop"))
    with TestClient(app) as client:
        project = client.post("/v1/projects", json={"owner_id": "demo-owner", "title": "Brand",
                                                    "goal": "Open one file."}).json()
        worker = client.post(f"/v1/projects/{project['project_id']}/workers",
                             json={"owner_id": "demo-owner", "name": "Brand Worker", "role": "writer",
                                   "profile": "codex-cli"}).json()
        (Path(worker["workspace_dir"]) / "answer.md").write_text("# Result", encoding="utf-8")
        opened = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/open?path=answer.md")
    assert opened.status_code == 200
    assert "<title>xPerfect file - answer.md</title>" in opened.text
    assert '<div class="brand">xPerfect</div>' in opened.text
    assert "GlassHive file" not in opened.text
