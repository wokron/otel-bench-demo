from pydantic import Field

from demo_api.models import APIError, Model


class CPU(Model):
    percent: int = Field(default=20, ge=1, le=50, strict=True)


class NetworkDelay(Model):
    milliseconds: int = Field(default=100, ge=1, le=3000, strict=True)
    interface: str = Field(default="eth0", pattern=r"^[a-zA-Z0-9_.-]{1,15}$")


class PodKill(Model):
    pass


ACTIONS = {
    "pod.kill": (PodKill, "pod", "delete", "Delete one pod once; its workload controller may recreate it"),
    "cpu.load": (CPU, "cpu", "fullload", "CPU load in one pod; percent may refer to node CPUs"),
    "network.delay": (NetworkDelay, "network", "delay", "Delay on one pod network interface"),
}


class ChaosBlade:
    def __init__(self, config, context):
        self.discovery = context["discovery"]
        self.kube = self.discovery.kube
        self.target = context["target_id"]
        self.allowed = config.get("allowed_actions", list(ACTIONS))
        if set(self.allowed) - ACTIONS.keys():
            raise ValueError("Unknown ChaosBlade allowed_actions")

    def describe(self):
        return [{"name": name, "description": ACTIONS[name][3], "parameters_schema": ACTIONS[name][0].model_json_schema(),
                 "max_duration_seconds": 300, "max_pods": 1, "reversible": name != "pod.kill",
                 "requires": "ChaosBlade operator" if name == "pod.kill" else "ChaosBlade operator and compatible per-node tool runtime"}
                for name in self.allowed]

    def plan(self, request):
        if request.action not in self.allowed:
            raise APIError("unsupported_action", f"Action {request.action!r} is not enabled", 422)
        schema, target, action, _ = ACTIONS[request.action]
        params = schema.model_validate(request.parameters).model_dump()
        pods = [p for p in self.discovery.resolve(request.service)
                if p.get("status", {}).get("phase") == "Running" and not p["metadata"].get("deletionTimestamp")]
        if not pods:
            raise APIError("target_not_ready", "No running pod is available", 409)
        pod = pods[0]
        matchers = [{"name": "names", "value": [pod["metadata"]["name"]]},
                    {"name": "namespace", "value": [self.kube.namespace]}]
        if target == "cpu":
            matchers.append({"name": "cpu-percent", "value": [str(params["percent"])]})
        elif target == "network":
            matchers += [{"name": "time", "value": [str(params["milliseconds"])]},
                         {"name": "interface", "value": [params["interface"]]}]
        return {"target_id": self.target, "service": request.service,
                "pod": pod["metadata"]["name"], "pod_uid": pod["metadata"]["uid"],
                "namespace": self.kube.namespace, "duration_seconds": request.duration_seconds,
                "experiment": {"scope": "pod", "target": target, "action": action, "matchers": matchers}}

    def _owned(self, experiment_id):
        result = self.kube.run("get", "chaosblades.chaosblade.io", experiment_id,
                               "--ignore-not-found", "-o", "json", json_output=False)
        if not result.strip():
            return None
        import json
        cr = json.loads(result)
        if cr["metadata"].get("labels", {}).get("demo-api-target") != self.target:
            raise APIError("ownership_conflict", "ChaosBlade belongs to another target", 409)
        return cr

    def apply(self, experiment_id, plan):
        # Pin name AND UID at execution time; never retarget a replacement pod implicitly.
        current = self.kube.run("get", "pod", plan["pod"], "-o", "json")
        if current["metadata"]["uid"] != plan["pod_uid"]:
            raise APIError("target_changed", "Pod was replaced; request a new plan", 409)
        document = {"apiVersion": "chaosblade.io/v1alpha1", "kind": "ChaosBlade",
                    "metadata": {"name": experiment_id, "labels": {"demo-api-target": self.target,
                                                                                "app.kubernetes.io/managed-by": "demo-api"}},
                    "spec": {"experiments": [plan["experiment"]]}}
        # create, never apply/upsert, to avoid overwriting another experiment.
        self.kube.run("create", "-f", "-", "-o", "json", document=document)
        return {"name": experiment_id}

    def status(self, experiment_id, handle):
        cr = self._owned(experiment_id)
        if cr is None:
            return {"state": "absent"}
        phase = cr.get("status", {}).get("phase", "")
        state = {"Running": "running", "Error": "failed", "Destroyed": "completed"}.get(phase, "pending")
        if cr["metadata"].get("deletionTimestamp"):
            state = "cancelling"
        return {"state": state, "backend_status": cr.get("status", {})}

    def cancel(self, experiment_id, handle):
        if self._owned(experiment_id) is None:
            return {"state": "cancelled"}
        self.kube.run("delete", "chaosblades.chaosblade.io", experiment_id,
                      "--ignore-not-found", "--wait=false", json_output=False)
        # CR finalizers perform recovery asynchronously; deletion acceptance is not recovery.
        return {"state": "cancelling"}
