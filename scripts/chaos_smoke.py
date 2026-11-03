"""Exercise a real fault through the public API and always request cleanup."""
import argparse
import os
import time
import uuid
from urllib.parse import quote

import httpx


def exercise(client, target, service, action, parameters, duration, timeout):
    prefix = "/v1/targets/" + quote(target, safe="")

    def call(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    body = {"action": action, "service": service, "parameters": parameters,
            "duration_seconds": duration, "dry_run": True}
    plan = call("POST", prefix + "/experiments", json=body)
    print("PLAN", plan, flush=True)
    body["dry_run"] = False
    key = "smoke-" + uuid.uuid4().hex
    created = call("POST", prefix + "/experiments", json=body, headers={"Idempotency-Key": key})
    path = prefix + "/experiments/" + created["id"]
    print("CREATED", created["id"], flush=True)
    observed_running = False
    cleanup_confirmed = False
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = call("GET", path)
            print("STATUS", result["state"], result.get("backend_status", {}), flush=True)
            if result["state"] == "running":
                observed_running = True
                break
            if result["state"] in {"failed", "unknown", "cancelled", "completed"}:
                raise RuntimeError(f"Fault did not reach running: {result}")
            time.sleep(1)
        if not observed_running:
            raise RuntimeError("Timed out waiting for fault injection")
    finally:
        result = call("DELETE", path)
        deadline = time.monotonic() + timeout
        while result["state"] not in {"cancelled", "completed"} and time.monotonic() < deadline:
            time.sleep(1)
            result = call("GET", path)
        cleanup_confirmed = result["state"] in {"cancelled", "completed"}
        print("CLEANUP", created["id"], result["state"], flush=True)
        if not cleanup_confirmed:
            raise RuntimeError(f"Recovery not confirmed; inspect experiment {created['id']}")
    return {"id": created["id"], "injection_observed": observed_running,
            "cleanup_confirmed": cleanup_confirmed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--target", default="otel-demo")
    parser.add_argument("--service", default="product-catalog")
    parser.add_argument("--action", choices=["pod.kill", "cpu.load", "network.delay"], default="pod.kill")
    parser.add_argument("--duration", type=int, default=30, choices=range(5, 301), metavar="5..300")
    parser.add_argument("--timeout", type=int, default=60, choices=range(1, 301), metavar="1..300")
    parser.add_argument("--execute", action="store_true", help="Required: actually injects a fault into the selected service")
    args = parser.parse_args()
    if not args.execute:
        parser.error("Pass --execute to run an actual fault; pod.kill deletes one pod")
    token = os.environ.get("DEMO_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    parameters = {"percent": 5} if args.action == "cpu.load" else {"milliseconds": 100} if args.action == "network.delay" else {}
    with httpx.Client(base_url=args.url, headers=headers, timeout=120, trust_env=False) as client:
        print(exercise(client, args.target, args.service, args.action, parameters, args.duration, args.timeout))


if __name__ == "__main__":
    main()
