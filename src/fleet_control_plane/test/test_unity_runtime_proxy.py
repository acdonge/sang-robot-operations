import pytest
from fastapi.testclient import TestClient

from fleet_control_plane.unity_runtime_proxy import (
    UnityRuntimeProxy,
    unity_runtime_router,
)
from fastapi import FastAPI


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:18093",
        "http://192.0.2.10:18093",
        "http://user:password@127.0.0.1:18093",
        "http://127.0.0.1:18093?target=elsewhere",
    ],
)
def test_runtime_proxy_rejects_non_loopback_or_credentialed_urls(url):
    with pytest.raises(ValueError, match="loopback"):
        UnityRuntimeProxy(url)


def test_unconfigured_runtime_contract_is_explicit():
    app = FastAPI()
    app.include_router(unity_runtime_router(None))
    client = TestClient(app)
    status = client.get("/api/v1/workflow-authoring/runtime")
    assert status.status_code == 200
    assert status.json() == {
        "schema_version": 1,
        "configured": False,
        "running": False,
        "ready": False,
        "preview_available": False,
        "physical_output_connected": False,
    }
    assert client.post("/api/v1/workflow-authoring/runtime/start").status_code == 503
