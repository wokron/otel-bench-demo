from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from demo_api.app import create_app
from demo_api.experiments import Experiments
from demo_api.models import APIError, ActionRequest, Entity, Observation, Query


class Discovery:
    def resolve(self, name):
        if name != "checkout":
            raise APIError("entity_not_found", "Unknown service", 404)
        return [{"metadata": {"name": "checkout-1", "uid": "uid-1"}}]

    def entities(self):
        return [Entity(id="test/service/checkout", name="checkout", kind="service")]

    def relations(self):
        return []


class Telemetry:
    def describe(self):
        return {"configured": True}

    def query(self, q):
        return Observation(signal=q.signal, source="fake", records=[])


class Actuator:
    def __init__(self):
        self.applied = []
        self.cancelled = []
        self.fail_apply = False
        self.fail_cancel = False

    def describe(self):
        return [{"name": "cpu.load"}]

    def plan(self, request):
        Discovery().resolve(request.service)
        return {"pod": "checkout-1", "pod_uid": "uid-1"}

    def apply(self, ident, plan):
        self.applied.append(ident)
        if self.fail_apply:
            raise APIError("backend_unavailable", "Connection lost after create")
        return {"name": ident}

    def status(self, ident, handle):
        return {"state": "running"}

    def cancel(self, ident, handle):
        self.cancelled.append(ident)
        if self.fail_cancel:
            raise APIError("backend_unavailable", "Disconnected")
        return {"state": "cancelled"}


@pytest.fixture
def actuator():
    return Actuator()


@pytest.fixture
def client(tmp_path, monkeypatch, actuator):
    from demo_api import app as module
    def factory(name, config, context):
        return {"discovery": Discovery(), "telemetry": Telemetry(), "actions": actuator}[name]
    monkeypatch.setattr(module, "load_provider", factory)
    config = {"spec_version": "demo-api/v1", "state_path": str(tmp_path / "state.db"), "targets": [
        {"id": "test", "description": "Fake", "discovery": {"plugin": "discovery"},
         "telemetry": {"logs": {"plugin": "telemetry"}}, "actions": {"plugin": "actions"}}]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("DEMO_API_TOKEN", "test-token")
    with TestClient(create_app(str(path)), headers={"Authorization": "Bearer test-token"}) as client:
        yield client


def query(**kw):
    now = datetime.now(timezone.utc)
    return {"signal": "logs", "service": "checkout", "start": (now - timedelta(minutes=1)).isoformat(),
            "end": now.isoformat(), **kw}


def test_auth(client):
    assert client.get("/v1/targets", headers={"Authorization": "bad"}).status_code == 401
    assert client.get("/v1/targets").status_code == 200


def test_discovery_and_openapi(client):
    assert client.get("/v1/targets/test/entities").json()["entities"][0]["name"] == "checkout"
    assert client.get("/v1/targets/missing/entities").status_code == 404
    assert not client.get("/v1/targets/test/capabilities").json()["telemetry"]["metrics"]["configured"]
    assert "/v1/targets/{target_id}/query" in client.get("/openapi.json").json()["paths"]


def test_query_empty_vs_unsupported(client):
    assert client.post("/v1/targets/test/query", json=query()).json()["data"]["records"] == []
    assert client.post("/v1/targets/test/query", json=query(signal="traces")).json()["error"]["code"] == "unsupported_signal"
    assert client.post("/v1/targets/test/query", json=query(service="other")).status_code == 404


@pytest.mark.parametrize("changes", [{"start": "2026-01-01T00:00:00"}, {"limit": 1001},
                                      {"signal": "metrics"}, {"namespace": "default"},
                                      {"trace_id": "bad"}, {"start": "2000-01-01T00:00:00Z"}])
def test_query_validation(client, changes):
    assert client.post("/v1/targets/test/query", json=query(**changes)).status_code == 422


def test_dry_run_and_execution(client, actuator):
    body = {"action": "cpu.load", "service": "checkout"}
    assert client.post("/v1/targets/test/experiments", json=body).json()["dry_run"]
    assert not actuator.applied
    body["dry_run"] = False
    assert client.post("/v1/targets/test/experiments", json=body).status_code == 422
    headers = {"Idempotency-Key": "request-1"}
    first = client.post("/v1/targets/test/experiments", json=body, headers=headers).json()
    second = client.post("/v1/targets/test/experiments", json=body, headers=headers).json()
    assert first["id"] == second["id"] and len(actuator.applied) == 1
    body["duration_seconds"] = 2
    assert client.post("/v1/targets/test/experiments", json=body, headers=headers).status_code == 409
    endpoint = "/v1/targets/test/experiments/" + first["id"]
    assert client.delete(endpoint).json()["state"] == "cancelled"
    assert client.delete(endpoint).json()["state"] == "cancelled"
    assert len(actuator.cancelled) == 1


def test_restart_recovery_and_retry(tmp_path, actuator):
    path = str(tmp_path / "state.db")
    targets = {"test": SimpleNamespace(actuator=actuator)}
    first = Experiments(path, targets)
    doc = first.create("test", ActionRequest(action="cpu.load", service="checkout", dry_run=False), "key")
    doc["expires_at"] = 0
    first.save(doc)
    first.db.close()
    second = Experiments(path, targets)
    actuator.fail_cancel = True
    second.reconcile()
    assert "last_error" in second.get("test", doc["id"])
    actuator.fail_cancel = False
    second.reconcile()
    assert second.get("test", doc["id"])["state"] == "cancelled"
    assert len(actuator.applied) == 1
    second.db.close()


def test_ambiguous_write_is_durable_and_blocks_overlap(tmp_path, actuator):
    store = Experiments(str(tmp_path / "state.db"), {"test": SimpleNamespace(actuator=actuator)})
    actuator.fail_apply = True
    body = ActionRequest(action="cpu.load", service="checkout", dry_run=False)
    with pytest.raises(APIError, match="Inspect/cancel"):
        store.create("test", body, "key")
    docs = store.list("test")
    assert len(docs) == 1 and docs[0]["state"] == "unknown"
    assert store.create("test", body, "key")["id"] == docs[0]["id"]
    with pytest.raises(APIError) as err:
        store.create("test", body, "other-key")
    assert err.value.code == "experiment_conflict"
    store.cancel("test", docs[0]["id"])
    store.db.close()
