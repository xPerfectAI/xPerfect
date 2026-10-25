"""Settings use the existing signed owner-session facade."""
import httpx
from pathlib import Path
import re
import subprocess
from fastapi import FastAPI
from fastapi.testclient import TestClient

from glass_drive_ui.spend_control_routes import install_spend_control_ui_routes


def test_read_write_resume_forwarding_preserves_scope_and_generation():
    calls = []
    class Client:
        def __init__(self, human): self.human = human
        def _request(self, method, path, json_body=None):
            calls.append((self.human, method, path, json_body)); return {"ok": True}
    app = FastAPI()
    install_spend_control_ui_routes(app, lambda request, human_confirmation=False: Client(human_confirmation))
    client = TestClient(app)
    assert client.get("/api/spend-policy").status_code == 200
    assert client.get("/api/projects/project-a/spend-policy").status_code == 200
    assert client.put("/api/spend-policy", json={"expected_revision": 3, "policy": {"enabled": False}}).status_code == 200
    assert client.post("/api/admission-holds/hold-a/resume", json={"expected_generation": 4}).status_code == 200
    assert calls[0][:3] == (False, "GET", "/v1/spend-policy")
    assert calls[1][:3] == (False, "GET", "/v1/projects/project-a/spend-policy")
    assert calls[-1] == (True, "POST", "/v1/admission-holds/hold-a/resume", {"expected_generation": 4})


def test_failure_guidance_omits_untrusted_runtime_details():
    class Client:
        def _request(self, *args, **kwargs):
            request = httpx.Request("PUT", "https://runtime.invalid/spend-policy")
            response = httpx.Response(422, request=request, json={"detail": "untrusted-provider-detail"})
            raise httpx.HTTPStatusError("failure", request=request, response=response)
    app = FastAPI()
    install_spend_control_ui_routes(app, lambda request, human_confirmation=False: Client())
    response = TestClient(app).put("/api/spend-policy", json={})
    assert response.status_code == 422
    assert "currency" in response.json()["detail"]
    assert "untrusted-provider-detail" not in response.text


def test_watch_resume_reuses_the_existing_csrf_header():
    source = (Path(__file__).parents[1] / "src/glass_drive_ui/static/watch.js").read_text()
    functions = []
    for name in ("pauseRequest", "currentCsrfToken", "csrfHeaders"):
        functions.append(re.search(rf"^(?:async )?function {name}\(.*?^}}", source, re.M | re.S).group(0))
    script = "import assert from 'node:assert/strict';\n" + r'''
const withAuth=value=>value;
const readAuthCookie=name=>{assert.equal(name,'glasshive_csrf');return 'synthetic-csrf';};
let sent;
const fetch=async(url,options)=>{sent={url,options};return {ok:true,json:async()=>({active:false})};};
''' + "\n".join(functions) + r'''
await pauseRequest('/api/admission-holds/hold-a/resume','POST',{expected_generation:1});
assert.equal(sent.options.headers['X-GlassHive-CSRF'],'synthetic-csrf');
assert.equal(sent.options.headers['X-CSRF-Token'],undefined);
assert.deepEqual(JSON.parse(sent.options.body),{expected_generation:1});
'''
    subprocess.run(["node", "--input-type=module", "--eval", script], check=True, text=True, capture_output=True)
