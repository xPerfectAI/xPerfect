"""Import must not load service environments, open databases or start lifecycle work."""

import json
import os
from pathlib import Path
import subprocess
import sys


_SCRIPT = r"""
import gc, hashlib, json, os, pathlib, sqlite3, sys, threading
root = pathlib.Path(sys.argv[1]); root.mkdir()
from workers_projects_runtime.store import Store
sentinel = root / "unrelated.sqlite"
store = Store(str(sentinel))
project = store.create_project("unrelated", "Unrelated fixture", "Sentinel only", "codex-cli")
ids = []
for i in range(11):
    w = store.create_worker(project["project_id"], "unrelated", f"Sentinel {i}", "fixture", "codex-cli", "docker", "codex-cli", "fixture")
    store.update_worker(w["worker_id"], state="ready")
    ids.append(w["worker_id"])
def readonly_rows():
    with sqlite3.connect(sentinel.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM workers ORDER BY worker_id")]
gc.collect()  # Flush fixture-only unclosed connection/WAL cleanup before the byte baseline.
rows_before = readonly_rows()
gc.collect()
before = sentinel.read_bytes()
trap = root / "unrelated.env"
trap.write_text("WPR_API_TOKEN=synthetic-unrelated-secret\nWPR_DB_PATH=" + str(sentinel) + "\n")
os.environ["VIVENTIUM_ENV_FILE"] = str(trap)
os.environ["WPR_DB_PATH"] = str(sentinel)
os.environ.pop("WPR_API_TOKEN", None)
before_env = dict(os.environ)
before_threads = {t.ident for t in threading.enumerate()}
forbidden_reads = []
import_phase = True
def guard(event, args):
    if import_phase and event == "sqlite3.connect":
        raise AssertionError("API import attempted to open a database")
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = pathlib.Path(os.fsdecode(args[0])).absolute()
        if path == trap or path.name in {"runtime.env", "runtime.local.env"}:
            forbidden_reads.append(str(path))
            raise AssertionError("API import attempted to load a service environment")
sys.addaudithook(guard)
from workers_projects_runtime import api
import_phase = False
assert "app" not in vars(api), "No global application may be constructed during import"
assert dict(os.environ) == before_env
assert {t.ident for t in threading.enumerate()} == before_threads
assert sentinel.read_bytes() == before
assert readonly_rows() == rows_before
assert all(w["state"] == "ready" for w in readonly_rows())
assert not forbidden_reads
os.environ.pop("VIVENTIUM_ENV_FILE")
os.environ.pop("WPR_DB_PATH")
from workers_projects_runtime.openclaw_runtime import StubRuntime
from fastapi.testclient import TestClient
own_db = root / "explicit-test.sqlite"
with TestClient(api.create_app(str(own_db), runtime=StubRuntime())) as client:
    assert client.get("/health").status_code == 200
assert own_db.is_file()
assert sentinel.read_bytes() == before
assert readonly_rows() == rows_before
print(json.dumps({"sentinel_sha256_before": hashlib.sha256(before).hexdigest(), "sentinel_sha256_after": hashlib.sha256(sentinel.read_bytes()).hexdigest(), "unrelated_ready_workers_unchanged": len(ids), "environment_unchanged_on_import": True, "environment_file_reads_on_import": 0, "threads_added_on_import": 0, "explicit_factory_health": "PASS"}))
"""


def test_api_import_and_explicit_factory_preserve_unrelated_database(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(tmp_path / "sentinel-proof")],
        env=dict(os.environ),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["unrelated_ready_workers_unchanged"] == 11
    assert proof["sentinel_sha256_before"] == proof["sentinel_sha256_after"]
    (tmp_path / "proof.json").write_text(json.dumps(proof, indent=2))
    print(result.stdout.strip())


def test_explicit_factory_loads_only_deliberate_test_environment(tmp_path, monkeypatch):
    from workers_projects_runtime import api
    from workers_projects_runtime.openclaw_runtime import StubRuntime
    from fastapi.testclient import TestClient

    fixture_env = tmp_path / "explicit.env"
    fixture_db = tmp_path / "explicit.sqlite"
    fixture_env.write_text(
        f"WPR_DB_PATH={fixture_db}\nWPR_API_TOKEN=synthetic-factory-token\n"
    )
    monkeypatch.setenv("WPR_DB_PATH", "")
    monkeypatch.setenv("WPR_API_TOKEN", "")
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(fixture_env))
    with TestClient(api.create_app(runtime=StubRuntime())) as client:
        assert client.get("/health").status_code == 200
        assert os.environ["WPR_API_TOKEN"] == "synthetic-factory-token"
        assert fixture_db.is_file()
    # The pre-load setenv calls ensure fixture-loaded values are restored at teardown.


def test_explicit_uvicorn_factory_entrypoint_starts_and_stops(tmp_path):
    import socket
    import time
    import urllib.error
    import urllib.request

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = dict(os.environ)
    env.pop("VIVENTIUM_ENV_FILE", None)
    env.update(
        VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV="1",
        WPR_DB_PATH=str(tmp_path / "server.sqlite"),
        WPR_RUNTIME_BACKEND="stub",
        WPR_API_TOKEN="synthetic-server-token",
        GLASSHIVE_ENTERPRISE_MODE="0",
        GLASSHIVE_LINK_REF_STATE_PATH=str(tmp_path / "links.json"),
    )
    log = tmp_path / "uvicorn.log"
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "workers_projects_runtime.api:create_app",
        "--factory",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-access-log",
    ]
    with log.open("w") as output:
        process = subprocess.Popen(
            command, env=env, stdout=output, stderr=subprocess.STDOUT
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                assert process.poll() is None, log.read_text()
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=1
                    ) as response:
                        assert response.status == 200
                        break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.05)
            else:
                raise AssertionError("Explicit factory did not become healthy")
            assert (tmp_path / "server.sqlite").is_file()
        finally:
            process.terminate()
            process.wait(timeout=10)
    import signal

    assert process.returncode in (0, -signal.SIGTERM), log.read_text()
    assert "Application shutdown complete." in log.read_text()
