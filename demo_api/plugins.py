"""Provider boundary. Third-party plugins register factories using entry points."""
from importlib.metadata import entry_points
from typing import Any, Protocol

from demo_api.models import ActionRequest, Entity, Observation, Query, Relation


class Discovery(Protocol):
    def entities(self) -> list[Entity]: ...
    def relations(self) -> list[Relation]: ...
    def resolve(self, service: str) -> list[dict]: ...
    def telemetry_name(self, service: str) -> str: ...


class Telemetry(Protocol):
    def describe(self) -> dict[str, Any]: ...
    def query(self, request: Query) -> Observation: ...


class Actuator(Protocol):
    def describe(self) -> list[dict]: ...
    def plan(self, request: ActionRequest) -> dict: ...
    def apply(self, experiment_id: str, plan: dict) -> dict: ...
    def status(self, experiment_id: str, handle: dict) -> dict: ...
    def cancel(self, experiment_id: str, handle: dict) -> dict: ...


def load_provider(name: str, config: dict, context: dict):
    from demo_api.providers.kubernetes import KubernetesDiscovery
    from demo_api.providers.telemetry import Jaeger, KubernetesLogs, Prometheus, OpenSearch
    from demo_api.providers.chaosblade import ChaosBlade

    builtins = {
        "kubernetes": KubernetesDiscovery, "jaeger": Jaeger,
        "kubernetes-logs": KubernetesLogs, "prometheus": Prometheus,
        "opensearch": OpenSearch, "chaosblade": ChaosBlade,
    }
    if name in builtins:
        return builtins[name](config, context)
    matches = list(entry_points(group="demo_api.providers", name=name))
    if len(matches) != 1:
        raise ValueError(f"Expected one provider factory for {name!r}, got {len(matches)}")
    return matches[0].load()(config, context)
