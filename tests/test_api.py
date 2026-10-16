import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


def test_api_run_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("ATH_DB", str(tmp_path / "api.db"))
    import importlib

    import ath_server.api.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    body = {"product": "settlenxt", "commit": "mr001", "merge_base": "base000", "env": "uat1"}
    assert client.post("/runs", json=body).status_code == 401  # identity required
    r = client.post("/runs", json=body, headers={"X-ATH-User": "u123"})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert client.get(f"/runs/{run_id}").json()["status"] == "succeeded"
    assert "ATH report" in client.get(f"/runs/{run_id}/report").text
    assert any(e["kind"] == "stage.done" for e in client.get(f"/runs/{run_id}/events").json())
