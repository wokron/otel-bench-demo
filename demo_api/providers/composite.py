"""Route actions while retaining the backend identity needed for recovery."""
import hashlib
import json

from demo_api.models import APIError


class Composite:
    def __init__(self, config, context):
        from demo_api.plugins import load_provider
        self.target = context["target_id"]
        self.kube = context["discovery"].kube
        self.providers = {}
        self.fingerprints = {}
        self.routes = {}
        for name, spec in config["providers"].items():
            if spec["plugin"] == "composite":
                raise ValueError("Nested composite providers are not supported")
            provider = load_provider(spec["plugin"], spec.get("config", {}), context)
            self.providers[name] = provider
            identity = {"provider": spec, "target": self.target,
                        "context": getattr(self.kube, "context", None), "namespace": self.kube.namespace}
            self.fingerprints[name] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            for action in provider.describe():
                if action["name"] in self.routes:
                    raise ValueError(f"Duplicate action {action['name']}")
                self.routes[action["name"]] = name
        self.legacy = config.get("legacy_provider")
        if self.legacy is not None and self.legacy not in self.providers:
            raise ValueError("Unknown legacy_provider")

    def describe(self):
        return [{**action, "backend": name} for name, provider in self.providers.items()
                for action in provider.describe()]

    def plan(self, request):
        name = self.routes.get(request.action)
        if name is None:
            raise APIError("unsupported_action", "Action is not configured", 422)
        inner = self.providers[name].plan(request)
        return {"backend": name, "backend_fingerprint": self.fingerprints[name],
                "service": request.service, "target_id": self.target, "plan": inner}

    def _resolve(self, handle):
        plan = handle.get("plan", {})
        if "backend" not in plan:
            if self.legacy is None:
                raise APIError("backend_unavailable", "Legacy experiment requires legacy_provider")
            return self.providers[self.legacy], handle
        name = plan["backend"]
        if name not in self.providers or plan["backend_fingerprint"] != self.fingerprints[name]:
            raise APIError("backend_changed", "Restore the original provider configuration to recover this experiment", 409)
        return self.providers[name], handle.get("backend_handle", {"name": handle["name"], "plan": plan["plan"]})

    def apply(self, experiment_id, plan):
        handle = {"name": experiment_id, "plan": plan}
        provider, _ = self._resolve(handle)
        handle["backend_handle"] = provider.apply(experiment_id, plan["plan"])
        return handle

    def status(self, experiment_id, handle):
        provider, inner = self._resolve(handle)
        return provider.status(experiment_id, inner)

    def cancel(self, experiment_id, handle):
        provider, inner = self._resolve(handle)
        return provider.cancel(experiment_id, inner)
