import json

import httpx
import pytest

from scripts.chaos_smoke import exercise


@pytest.mark.parametrize("backend_state", ["running", "failed"])
def test_smoke_always_cleans_up_after_backend_result(backend_state):
    calls = []
    def backend(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            body = json.loads(request.content)
            if body["dry_run"]:
                return httpx.Response(200, json={"dry_run": True, "plan": {"pod": "pod-1"}})
            assert request.headers["Idempotency-Key"].startswith("smoke-")
            return httpx.Response(200, json={"id": "da-test"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"state": "cancelled"})
        return httpx.Response(200, json={"state": backend_state, "backend_status": {"phase": backend_state}})
    with httpx.Client(base_url="http://api", transport=httpx.MockTransport(backend)) as client:
        if backend_state == "failed":
            with pytest.raises(RuntimeError, match="did not reach running"):
                exercise(client, "test", "checkout", "pod.kill", {}, 30, 1)
        else:
            result = exercise(client, "test", "checkout", "pod.kill", {}, 30, 1)
            assert result["injection_observed"] and result["cleanup_confirmed"]
    assert calls[-1] == ("DELETE", "/v1/targets/test/experiments/da-test")
