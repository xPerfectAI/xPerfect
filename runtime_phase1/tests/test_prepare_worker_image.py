from pathlib import Path

from workers_projects_runtime import prepare_worker_image


def test_prepare_worker_image_uses_runtime_database_parent(tmp_path, monkeypatch):
    db_path = tmp_path / "state" / "runtime.sqlite3"
    calls: list[Path] = []

    class FakeManager:
        def __init__(self, *, base_dir: str) -> None:
            calls.append(Path(base_dir))

        def prepare_image(self) -> None:
            calls.append(Path("prepared"))

    monkeypatch.setenv("WPR_DB_PATH", str(db_path))
    monkeypatch.setattr(prepare_worker_image, "DockerSandboxManager", FakeManager)

    assert prepare_worker_image.main() == 0
    assert calls == [db_path.parent.resolve(), Path("prepared")]
