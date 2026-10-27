from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from demo_api.models import Model


class ProviderConfig(Model):
    plugin: str
    config: dict[str, Any] = Field(default_factory=dict)


class TargetConfig(Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    description: str
    discovery: ProviderConfig
    telemetry: dict[Literal["metrics", "logs", "traces"], ProviderConfig]
    actions: ProviderConfig | None = None


class Config(Model):
    spec_version: Literal["demo-api/v1"]
    state_path: str = ".state/experiments.sqlite3"
    targets: list[TargetConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def unique(self):
        if len({t.id for t in self.targets}) != len(self.targets):
            raise ValueError("target ids must be unique")
        return self


def read_config(path: str) -> Config:
    return Config.model_validate(yaml.safe_load(Path(path).read_text()))
