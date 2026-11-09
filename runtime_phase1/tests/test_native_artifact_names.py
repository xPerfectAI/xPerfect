"""Use exact completed-file metadata for presentation, without changing file authority."""
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest

from workers_projects_runtime import api, profile_runtime
from workers_projects_runtime.signed_links import sign_link_token
from test_account_api import (
    account_client, account_headers, delegation_payload_with_origin, _truthfully_invoke_run,
)
from test_native_media import JPEG, PNG


class Preview(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.headings, self.images, self.heading = [], [], False
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        if tag == "h1":
            self.heading = True
        if tag == "img":
            self.images.append(dict(attrs)["src"])

    def handle_endtag(self, tag):
        if tag == "h1":
            self.heading = False

    def handle_data(self, data):
        if self.heading:
            self.headings.append(data)


@pytest.fixture
def completed_file(account_client, tmp_path, monkeypatch, request):
    service = account_client.app.state.service
    service.executor.submit = lambda *_a, **_k: None
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-artifact-name-secret")
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(tmp_path / "name-links.sqlite3"))
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "http://testserver")
    accepted = account_client.post("/v1/delegations", headers=account_headers(
        idempotency_key="artifact-name"), json=delegation_payload_with_origin())
    assert accepted.status_code == 202
    store = account_client.app.state.store
    work = store.get_delegation(accepted.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    run = _truthfully_invoke_run(store, work["current_run_id"], suffix="artifact-name")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    worker = store.update_worker(work["worker_id"], workspace_dir=str(workspace))
    native = profile_runtime.HostCodexCliRuntime(base_dir=str(tmp_path / "native"))
    name, data, *options = getattr(request, "param", ("trip-modes.csv", b"mode,cost\nWalking,low\nCycling,low\nBus,fare\n"))
    selected = workspace / name
    link = name
    if options == ["protected"]:
        monkeypatch.setenv("GLASSHIVE_PROTECTED_ARTIFACT_ROOTS", str(workspace))
        state = tmp_path / "runtime-state"
        state.mkdir()
        worker = store.update_worker(worker["worker_id"], state_dir=str(state))
        selected = native._home_dir(worker["worker_id"]) / ".tmp" / name
        selected.parent.mkdir(parents=True)
        (workspace / "private-source.png").write_bytes(JPEG)
        link = f"<{selected}>"
    scope = {"run_id": run["run_id"], "worker_id": worker["worker_id"], "owner_id": worker["owner_id"],
             "workspace_dir": str(workspace), "attempt_id": run["active_attempt_id"],
             "file_output_transport": "artifact_sha256"}
    selected.write_bytes(data)
    native._capture_native_output_files({**worker, "_run_attempt_id": run["active_attempt_id"]},
        str(workspace), scope, run["run_id"], f"[Result]({link})")
    service.runtime = native
    run = store.update_run(run["run_id"], state="completed", output_text="File verified.",
                           ended_at=datetime.now(timezone.utc).isoformat())
    callback = service._emit_callback(worker, "run.completed", run=run, submit_delivery=False)
    payload = json.loads(callback["payload_json"])
    descriptor = payload["output_files"]["files"][0]
    delivered_name = descriptor["filename"]
    suffix = profile_runtime.NATIVE_IMAGE_SUFFIXES.get(descriptor["mime_type"]) or Path(delivered_name).suffix
    relative = f"artifacts/native-media/{run['run_id']}/{hashlib.sha256(data).hexdigest()}{suffix}"
    return {"client": account_client, "store": store, "worker": worker, "run": run,
            "payload": payload, "relative": relative, "data": data, "name": delivered_name,
            "source_name": name}


@pytest.mark.parametrize("completed_file,expected_name,expected_mime", [
    (("capture.png", JPEG), "capture.jpg", "image/jpeg"),
    (("capture.jpg", PNG), "capture.png", "image/png"),
    (("capture.jpeg", JPEG), "capture.jpeg", "image/jpeg"),
], indirect=["completed_file"])
def test_selected_image_download_uses_captured_type_and_correct_name(
    completed_file, expected_name, expected_mime,
):
    case = completed_file
    descriptor = case['payload']['output_files']['files'][0]
    assert descriptor['filename'] == expected_name and descriptor['mime_type'] == expected_mime
    source = Path(case['worker']['workspace_dir']) / case['source_name']
    assert source.read_bytes() == case['data']
    response = case['client'].get(descriptor['download_url'])
    assert response.status_code == 200 and response.content == case['data']
    assert response.headers['content-type'] == expected_mime
    assert f'filename="{expected_name}"' in response.headers['content-disposition']
    assert response.headers['x-content-type-options'] == 'nosniff'
    ranged = case['client'].get(descriptor['download_url'], headers={'Range': 'bytes=2-9'})
    assert ranged.status_code == 206 and ranged.content == case['data'][2:10]
    assert ranged.headers['content-type'] == expected_mime
    opened = case['client'].get(artifact_url(case, 'artifact_open'))
    assert opened.status_code == 200
    preview = Preview(opened.text)
    assert preview.headings == [expected_name] and len(preview.images) == 1
    image = case['client'].get(preview.images[0])
    assert image.status_code == 200 and image.content == case['data']
    assert image.headers['content-type'] == expected_mime


def artifact_url(case, kind):
    worker = case["worker"]
    token = sign_link_token(kind=kind, worker_id=worker["worker_id"],
                           tenant_id=worker["tenant_id"], owner_id=worker["owner_id"], path=case["relative"])
    return f"/v1/signed-links/{token}"



@pytest.mark.parametrize("completed_file", [("result.png", PNG, "protected")], indirect=True)
def test_selected_output_is_delivered_without_exposing_a_protected_workspace(completed_file, monkeypatch):
    case = completed_file
    workspace = Path(case["worker"]["workspace_dir"])
    state = Path(case["worker"]["state_dir"])
    descriptor = case["payload"]["output_files"]["files"][0]
    assert not (workspace / "artifacts").exists()
    assert (state / case["relative"]).read_bytes() == PNG
    download = case["client"].get(descriptor["download_url"])
    assert download.status_code == 200 and download.content == PNG
    assert 'filename="result.png"' in download.headers["content-disposition"]
    assert case["client"].get(artifact_url(case, "artifact_open")).status_code == 200
    private = dict(case, relative="private-source.png")
    assert case["client"].get(artifact_url(private, "artifact_download")).status_code == 404
    other_owner = dict(case, worker={**case["worker"], "owner_id": "other-owner"})
    assert case["client"].get(artifact_url(other_owner, "artifact_download")).status_code == 401
    monkeypatch.setenv("GLASSHIVE_PROTECTED_ARTIFACT_ROOTS", os.pathsep.join(map(str, (workspace, state))))
    assert case["client"].get(descriptor["download_url"]).status_code == 404

def replace_metadata(case, monkeypatch, change):
    original = case["store"].list_callback_outbox_for_run
    def changed(*args, **kwargs):
        records = original(*args, **kwargs)
        return [dict(record, payload_json=json.dumps(change(json.loads(record["payload_json"]))))
                if record["event_type"] == "run.completed" else record for record in records]
    monkeypatch.setattr(case["store"], "list_callback_outbox_for_run", changed)


def test_native_preview_and_download_use_exact_selected_name(completed_file, monkeypatch):
    case = completed_file
    checked = []
    original = api.verified_native_file
    def selected_only(root, relative, digest, size):
        checked.append(relative.as_posix())
        assert relative.as_posix() == case["relative"]
        return original(root, relative, digest, size)
    monkeypatch.setattr(api, "verified_native_file", selected_only)
    opened = case["client"].get(artifact_url(case, "artifact_open"))
    assert opened.status_code == 200
    assert Preview(opened.text).headings == [case['name']]
    assert f"<title>{case['name']} · xPerfect</title>" in opened.text
    downloaded = case["client"].get(case["payload"]["output_files"]["files"][0]["download_url"])
    assert downloaded.status_code == 200 and downloaded.content == case["data"]
    assert f'filename="{case["name"]}"' in downloaded.headers["content-disposition"]
    assert "no-store" in downloaded.headers["cache-control"]
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert downloaded.headers["accept-ranges"] == "bytes"
    assert checked and set(checked) == {case["relative"]}


@pytest.mark.parametrize("invalid", ["missing", "ambiguous", "owner", "revision", "attempt", "filename"])
def test_missing_or_invalid_name_metadata_keeps_hash_basename(completed_file, monkeypatch, invalid):
    case = completed_file
    def change(payload):
        envelope = payload["output_files"]
        if invalid == "missing": payload.pop("output_files")
        elif invalid == "ambiguous":
            envelope["files"].append({**envelope["files"][0], "filename": "another.csv"})
        elif invalid == "owner": envelope["owner_id"] = "other-owner"
        elif invalid == "revision": envelope["result_revision"] += 1
        elif invalid == "attempt": envelope["attempt_id"] = "other-attempt"
        else: envelope["files"][0]["filename"] = "unsafe\r\nname.csv"
        return payload
    replace_metadata(case, monkeypatch, change)
    downloaded = case["client"].get(artifact_url(case, "artifact_download"))
    assert downloaded.status_code == 200 and downloaded.content == case["data"]
    assert f'filename="{Path(case["relative"]).name}"' in downloaded.headers["content-disposition"]
    opened = case["client"].get(artifact_url(case, "artifact_open"))
    assert Preview(opened.text).headings == [Path(case['relative']).name]


def test_name_lookup_ignores_unrelated_files_and_preserves_range_and_auth(completed_file, monkeypatch):
    case = completed_file
    def change(payload):
        payload["output_files"]["files"].append({"filename": "other.csv", "mime_type": "text/csv",
            "bytes": 4096, "sha256": "a" * 64, "download_url": "unused"})
        return payload
    replace_metadata(case, monkeypatch, change)
    original = api.verified_native_file
    def selected_only(root, relative, digest, size):
        assert relative.as_posix() == case["relative"]
        return original(root, relative, digest, size)
    monkeypatch.setattr(api, "verified_native_file", selected_only)
    response = case["client"].get(artifact_url(case, "artifact_download"), headers={"Range": "bytes=2-9"})
    assert response.status_code == 206 and response.content == case["data"][2:10]
    assert response.headers["content-range"] == f"bytes 2-9/{len(case['data'])}"
    assert f'filename="{case["name"]}"' in response.headers["content-disposition"]
    direct = f"/v1/workers/{case['worker']['worker_id']}/artifacts/download?path={quote(case['relative'])}"
    assert case["client"].get(direct).status_code == 401
    wrong_owner = sign_link_token(kind="artifact_download", worker_id=case["worker"]["worker_id"],
        tenant_id=case["worker"]["tenant_id"], owner_id="other-owner", path=case["relative"])
    assert case["client"].get(f"/v1/signed-links/{wrong_owner}").status_code == 401
    assert case["client"].get(artifact_url(case, "artifact_download") + "x").status_code == 401


def test_selected_utf8_name_uses_existing_safe_content_disposition(completed_file, monkeypatch):
    case = completed_file
    name = 'résumé "table".csv'
    def change(payload):
        payload["output_files"]["files"][0]["filename"] = name
        return payload
    replace_metadata(case, monkeypatch, change)
    response = case["client"].get(artifact_url(case, "artifact_download"))
    assert response.status_code == 200 and response.content == case["data"]
    assert 'filename="r_sum_ _table_.csv"' in response.headers["content-disposition"]
    assert f"filename*=UTF-8''{quote(name)}" in response.headers["content-disposition"]
    opened = case["client"].get(artifact_url(case, "artifact_open"))
    assert Preview(opened.text).headings == ['résumé "table".csv']


def test_stale_mission_selection_keeps_hash_basename(completed_file, monkeypatch):
    case = completed_file
    original = case["store"].get_delegation_for_worker
    def newer_mission(*args, **kwargs):
        return {**original(*args, **kwargs), "current_run_id": "newer-run"}
    monkeypatch.setattr(case["store"], "get_delegation_for_worker", newer_mission)
    downloaded = case["client"].get(artifact_url(case, "artifact_download"))
    assert downloaded.status_code == 200 and downloaded.content == case["data"]
    assert f'filename="{Path(case["relative"]).name}"' in downloaded.headers["content-disposition"]
