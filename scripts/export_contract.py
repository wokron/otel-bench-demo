"""Regenerate the versioned HTTP and configuration contracts from source."""
import json
from pathlib import Path

from demo_api.app import create_app
from demo_api.config import Config


def main():
    root = Path(__file__).resolve().parents[1]
    for name, document in {
        "openapi.json": create_app(str(root / "configs/otel-catalog.yaml")).openapi(),
        "config.schema.json": Config.model_json_schema(),
    }.items():
        (root / "docs" / name).write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
