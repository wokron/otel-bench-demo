"""Read-only black-box checks for any demo-api/v1 target (no fault execution)."""
import argparse
import os
from datetime import datetime, timedelta, timezone

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--target", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--require-records", nargs="*", default=[], choices=["logs", "metrics", "traces"])
    args = parser.parse_args()
    token = os.environ.get("DEMO_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=args.url, headers=headers, timeout=120, trust_env=False) as client:
        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        prefix = f"/v1/targets/{args.target}"
        assert call("GET", "/healthz")["spec_version"] == "demo-api/v1"
        caps = call("GET", prefix + "/capabilities")
        entities = call("GET", prefix + "/entities")["entities"]
        ids = {e["id"] for e in entities}
        assert len(ids) == len(entities)
        assert any(e["kind"] == "service" and e["name"] == args.service for e in entities)
        for relation in call("GET", prefix + "/relations")["relations"]:
            assert relation["source"] in ids and relation["target"] in ids
        print("PASS discovery: unique IDs, service present, relation endpoints resolve")
        now = datetime.now(timezone.utc)
        for signal, capability in caps["telemetry"].items():
            if not capability["configured"]:
                assert signal not in args.require_records, f"Required signal {signal} is unsupported"
                print(f"SKIP {signal}: no provider")
                continue
            body = {"signal": signal, "service": args.service,
                    "start": (now - timedelta(minutes=5)).isoformat(), "end": now.isoformat(), "limit": 10}
            if signal == "metrics":
                assert capability["metrics"], "Metrics provider must describe metric names and units"
                body["metric"] = capability["metrics"][0]["name"]
            result = call("POST", prefix + "/query", json=body)["data"]
            assert result["signal"] == signal and isinstance(result["records"], list)
            if signal in args.require_records:
                assert result["records"], f"No {signal} records; generate traffic and wait for export"
            print(f'PASS {signal}: {len(result["records"])} records, source={result["source"]}')
        for action in caps["actions"]:
            result = call("POST", prefix + "/experiments", json={"action": action["name"], "service": args.service})
            assert result["dry_run"] and result["plan"]
            print(f'PASS action preview: {action["name"]}')


if __name__ == "__main__":
    main()
