import argparse
import os

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Microservice Demo API")
    parser.add_argument("--config", default="configs/otel-catalog.yaml")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get("DEMO_API_TOKEN"):
        parser.error("Set DEMO_API_TOKEN when binding a non-loopback address")
    os.environ["DEMO_API_CONFIG"] = args.config
    uvicorn.run("demo_api.app:create_app", factory=True, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
