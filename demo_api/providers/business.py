"""Business faults using a versioned ConfigMap watched by otel-demo flagd."""
import copy
import json
from typing import Literal

from pydantic import field_validator

from demo_api.models import APIError, Model
from demo_api.providers.transport import endpoint, request_json


class FailureRate(Model):
    percent: Literal[10, 25, 50, 75, 100] = 25

    @field_validator("percent", mode="before")
    @classmethod
    def integer_percent(cls, value):
        if type(value) is not int:
            raise ValueError("percent must be an integer")
        return value


class Unavailable(Model):
    pass


ACTIONS = {
    "business.payment.failure": ("payment", "paymentFailure", FailureRate),
    "business.cart.failure": ("cart", "cartFailure", FailureRate),
    "business.payment.unavailable": ("payment", "paymentUnreachable", Unavailable),
}


class BusinessFlags:
    def __init__(self, config, context):
        self.config = config
        self.discovery = context["discovery"]
        self.kube = self.discovery.kube
        self.target = context["target_id"]
        self.allowed = config.get("allowed_actions", list(ACTIONS))
        if set(self.allowed) - ACTIONS.keys():
            raise ValueError("Unknown business action")
        self.cm = config.get("configmap", "flagd-config")
        self.key = config.get("key", "demo.flagd.json")
        self.deployment = config.get("deployment", "flagd")
        self.evaluation = config.get("evaluation", {"service": "flagd", "port": 8016})

    def describe(self):
        return [{"name": name, "category": "business", "services": [ACTIONS[name][0]],
                 "description": f"Application-level fault via {ACTIONS[name][1]}; requires business traffic",
                 "parameters_schema": ACTIONS[name][2].model_json_schema(),
                 "max_duration_seconds": 300, "reversible": True,
                 "requires": "Direct ConfigMap volume in flagd and OFREP evaluation endpoint",
                 "recovery_semantics": "Restore original flag; verify evaluation, then verify business separately"}
                for name in self.allowed]

    def _read(self):
        cm = self.kube.run("get", "configmap", self.cm, "-o", "json")
        try:
            document = json.loads(cm["data"][self.key])
            assert isinstance(document["flags"], dict)
        except (KeyError, ValueError, TypeError, AssertionError) as exc:
            raise APIError("backend_invalid_response", "Invalid flagd ConfigMap") from exc
        return cm, document

    def _check_mount(self):
        deployment = self.kube.run("get", "deployment", self.deployment, "-o", "json")
        spec = deployment["spec"]["template"]["spec"]
        volumes = {v["name"]: v for v in spec.get("volumes", [])}
        flagd = next((c for c in spec["containers"] if c["name"] == "flagd"), {})
        args = flagd.get("command", []) + flagd.get("args", [])
        mounts = [m for m in flagd.get("volumeMounts", []) if m.get("mountPath") == "/etc/flagd"]
        volume = volumes.get(mounts[0]["name"], {}) if mounts else {}
        items = volume.get("configMap", {}).get("items")
        if (deployment["spec"].get("replicas", 1) != 1 or not mounts or mounts[0].get("subPath") or
                volume.get("configMap", {}).get("name") != self.cm or
                (items is not None and not any(i.get("key") == self.key and i.get("path") == self.key for i in items)) or
                "file:./etc/flagd/demo.flagd.json" not in args or self.key != "demo.flagd.json"):
            raise APIError("business_backend_not_ready", "Run scripts/business_flags_setup.py to enable direct ConfigMap projection", 409)

    def plan(self, request):
        if request.action not in self.allowed:
            raise APIError("unsupported_action", "Business action is not enabled", 422)
        service, flag, schema = ACTIONS[request.action]
        params = schema.model_validate(request.parameters)
        if request.service != service:
            raise APIError("unsupported_action", f"This action only applies to {service}", 422)
        self.discovery.resolve(service)
        self._check_mount()
        cm, document = self._read()
        original = document["flags"].get(flag)
        if not isinstance(original, dict) or original.get("targeting") or original.get("state") != "ENABLED":
            raise APIError("unsupported_flag", "Flag must exist, be enabled, and have no targeting rules", 409)
        value = params.percent / 100 if isinstance(params, FailureRate) else True
        variants = original.get("variants", {})
        if not isinstance(variants, dict) or original.get("defaultVariant") not in variants:
            raise APIError("backend_invalid_response", "Flag has invalid variants/defaultVariant")
        variant = next((k for k, v in variants.items() if type(v) is type(value) and v == value), None)
        # JSON 1 and 1.0 are equivalent numeric flag values; booleans are not.
        if variant is None and not isinstance(value, bool):
            variant = next((k for k, v in variants.items() if type(v) in (int, float) and v == value), None)
        if variant is None:
            raise APIError("unsupported_parameter", "Requested failure rate is not defined by this flag's actual variant values", 422)
        if original.get("defaultVariant") == variant:
            raise APIError("experiment_conflict", "Flag already has the requested variant", 409)
        desired = {**copy.deepcopy(original), "defaultVariant": variant}
        return {"target_id": self.target, "namespace": self.kube.namespace, "service": service,
                "configmap": self.cm, "configmap_uid": cm["metadata"]["uid"], "key": self.key,
                "flag": flag, "original": original, "desired": desired,
                "duration_seconds": request.duration_seconds,
                "warnings": ["ConfigMap projection is asynchronous and may take over a minute; duration includes this delay.",
                             "Running confirms flag evaluation, not that a business request has failed."]}

    def _snapshot(self, handle):
        plan = handle["plan"]
        if (plan["target_id"] != self.target or plan["namespace"] != self.kube.namespace or
                plan["configmap"] != self.cm or plan["key"] != self.key):
            raise APIError("backend_changed", "Restore original business backend configuration", 409)
        cm, document = self._read()
        if cm["metadata"]["uid"] != plan["configmap_uid"]:
            raise APIError("target_changed", "Flag ConfigMap was replaced", 409)
        return plan, cm, document

    def _owner_key(self, flag):
        return "demo-api.chaos-bench/" + flag

    def _write(self, cm, document, flag, owner):
        updated = copy.deepcopy(cm)
        updated["data"][self.key] = json.dumps(document, ensure_ascii=False)
        annotations = updated["metadata"].setdefault("annotations", {})
        if owner is None:
            annotations.pop(self._owner_key(flag), None)
        else:
            annotations[self._owner_key(flag)] = owner
        # replace includes resourceVersion: Kubernetes rejects concurrent modifications.
        self.kube.run("replace", "-f", "-", "-o", "json", document=updated)

    def apply(self, experiment_id, plan):
        self._check_mount()
        handle = {"name": experiment_id, "plan": plan}
        plan, cm, document = self._snapshot(handle)
        owner = cm["metadata"].get("annotations", {}).get(self._owner_key(plan["flag"]))
        if owner is not None or document["flags"].get(plan["flag"]) != plan["original"]:
            raise APIError("experiment_conflict", "Flag changed or is owned by another experiment", 409)
        document["flags"][plan["flag"]] = plan["desired"]
        self._write(cm, document, plan["flag"], self.target + ":" + experiment_id)
        return handle

    def _evaluated(self, plan, expected):
        with endpoint(self.evaluation, self.kube) as base:
            path = self.evaluation.get("api_path", "/ofrep/v1/evaluate/flags")
            data = request_json("POST", base + path.rstrip("/") + "/" + plan["flag"], json={"context": {}})
        if not isinstance(data, dict) or data.get("errorCode"):
            raise APIError("backend_invalid_response", "flagd evaluation returned an error")
        variant = expected["defaultVariant"]
        value = expected["variants"][variant]
        actual = data.get("value")
        return (data.get("variant") == variant and actual == value and
                isinstance(actual, bool) == isinstance(value, bool))

    def status(self, experiment_id, handle):
        plan, cm, document = self._snapshot(handle)
        owner = cm["metadata"].get("annotations", {}).get(self._owner_key(plan["flag"]))
        if owner != self.target + ":" + experiment_id or document["flags"].get(plan["flag"]) != plan["desired"]:
            return {"state": "failed", "backend_status": {"reason": "Flag ownership or value changed; cancel to inspect recovery"}}
        ready = self._evaluated(plan, plan["desired"])
        return {"state": "running" if ready else "pending",
                "backend_status": {"flag": plan["flag"], "variant": plan["desired"]["defaultVariant"],
                                   "evaluation_confirmed": ready, "business_effect_verified": False}}

    def cancel(self, experiment_id, handle):
        plan, cm, document = self._snapshot(handle)
        owner = cm["metadata"].get("annotations", {}).get(self._owner_key(plan["flag"]))
        current = document["flags"].get(plan["flag"])
        if owner == self.target + ":" + experiment_id and current == plan["desired"]:
            document["flags"][plan["flag"]] = plan["original"]
            self._write(cm, document, plan["flag"], None)
        elif owner is not None or current != plan["original"]:
            raise APIError("recovery_conflict", "Flag was externally modified; original value retained in plan for manual recovery", 409)
        ready = self._evaluated(plan, plan["original"])
        return {"state": "cancelled" if ready else "cancelling",
                "backend_status": {"flag": plan["flag"], "restored_evaluation_confirmed": ready,
                                   "business_recovery_verified": False}}
