"""HTTP and MCP use the same owner-scoped durable admission state."""
from fastapi.testclient import TestClient
from workers_projects_runtime.api import create_app
from workers_projects_runtime.mcp_server import WorkersProjectsApiClient


def test_policy_revision_hold_resume_and_mcp_projection(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "synthetic-token")
    app = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    client = TestClient(app); headers = {"Authorization": "Bearer synthetic-token"}
    initial = client.get("/v1/spend-policy", headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json()["revision"] == 0
    policy = {"version": 1, "enabled": True, "currency": "USD", "limit_amount": "12.345",
        "unknown_policy": "reserve", "reserve_amount": "0.25"}
    assert client.put("/v1/spend-policy", headers=headers, json={"expected_revision": 0, "policy": policy}).status_code == 200
    assert client.put("/v1/spend-policy", headers=headers, json={"expected_revision": 0, "policy": policy}).status_code == 409
    assert client.get("/v1/spend-policy", headers=headers).json()["policy"]["limit_amount"] == "12.345"
    hold = client.post("/v1/admission-holds", headers=headers, json={"reason_code": "transport_integrity"})
    assert hold.status_code == 201, hold.text
    held = hold.json()
    conflict = client.post(f"/v1/admission-holds/{held['hold_id']}/resume", headers=headers,
        json={"expected_generation": held["generation"] + 1})
    assert conflict.status_code == 409
    resumed = client.post(f"/v1/admission-holds/{held['hold_id']}/resume", headers=headers,
        json={"expected_generation": held["generation"]})
    assert resumed.status_code == 200 and resumed.json()["active"] is False
    runtime = WorkersProjectsApiClient(base_url="http://runtime.invalid")
    calls = []
    runtime._request = lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {"items": [held]}
    assert runtime.admission_holds() == [held]
    runtime.resume_admission_hold(held["hold_id"], expected_generation=held["generation"])
    assert calls[-1][2]["json_body"]["expected_generation"] == held["generation"]
