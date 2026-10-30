import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

USER = {"X-ATH-User": "u123"}


def test_api_run_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("ATH_DB", str(tmp_path / "api.db"))
    import importlib

    import ath_server.api.app as appmod
    importlib.reload(appmod)
    client = TestClient(appmod.app)
    body = {"product": "settlenxt", "commit": "mr001", "merge_base": "base000", "env": "uat1"}
    assert client.post("/runs", json=body).status_code == 401  # identity required
    r = client.post("/runs", json=body, headers=USER)
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert client.get(f"/runs/{run_id}", headers=USER).json()["status"] == "succeeded"
    assert "ATH report" in client.get(f"/runs/{run_id}/report", headers=USER).text
    assert any(e["kind"] == "stage.done" for e in client.get(f"/runs/{run_id}/events", headers=USER).json())


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("ATH_DB", str(tmp_path / "api.db"))
    import importlib

    import ath_server.api.app as appmod
    importlib.reload(appmod)
    return TestClient(appmod.app)


def test_refused_run_is_an_error_not_a_queued_run(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = {"product": "settlenxt", "commit": "mr001", "merge_base": "base000", "env": "prod"}
    r = client.post("/runs", json=body, headers=USER)
    assert r.status_code == 400 and "not allowed by policy.environments" in r.json()["detail"]


def test_reads_need_an_identity(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    for path in ("/runs/x", "/runs/x/report", "/runs/x/events"):
        assert client.get(path).status_code == 401, path
