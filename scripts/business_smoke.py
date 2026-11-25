"""Exercise one business fault through the API, then request and confirm restoration."""
import argparse
import os

import httpx

from scripts.chaos_smoke import exercise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--target", default="otel-full-existing")
    parser.add_argument("--action", choices=["business.payment.failure", "business.cart.failure",
                                           "business.payment.unavailable"], default="business.payment.failure")
    parser.add_argument("--percent", type=int, choices=[10, 25, 50, 75, 100], default=25)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute is required: this changes a real application's behavior")
    service = "cart" if args.action == "business.cart.failure" else "payment"
    parameters = {} if args.action.endswith("unavailable") else {"percent": args.percent}
    token = os.environ.get("DEMO_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=args.url, headers=headers, timeout=120, trust_env=False) as client:
        print(exercise(client, args.target, service, args.action, parameters, duration=300, timeout=240))
    print("Flag evaluation and restoration confirmed. Verify business requests/telemetry separately.")


if __name__ == "__main__":
    main()
