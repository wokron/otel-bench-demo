import json
import subprocess

from demo_api.models import APIError, Entity, Relation


class Kubectl:
    def __init__(self, config):
        self.context = config["context"]
        self.namespace = config["namespace"]
        self.binary = config.get("kubectl", "kubectl")

    def run(self, *args, document=None, json_output=True):
        command = [self.binary, f"--context={self.context}", f"--namespace={self.namespace}",
                   "--request-timeout=15s", *args]
        try:
            result = subprocess.run(command, input=json.dumps(document) if document is not None else None,
                                    capture_output=True, text=True, timeout=25, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise APIError("backend_unavailable", f"kubectl could not complete: {exc}") from exc
        if result.returncode:
            raise APIError("backend_unavailable", result.stderr.strip()[:1500])
        try:
            return json.loads(result.stdout) if json_output else result.stdout
        except ValueError as exc:
            raise APIError("backend_invalid_response", "kubectl returned invalid JSON") from exc


class KubernetesDiscovery:
    def __init__(self, config, context):
        self.kube = Kubectl(config)
        self.target = context["target_id"]
        self.selector = config.get("selector", "")
        self.service_label = config.get("service_label", "app.kubernetes.io/name")
        self.telemetry_names = config.get("telemetry_service_names", {})

    def snapshot(self):
        # Scope is fixed by trusted config; never accept arbitrary namespace/selectors from clients.
        args = ["get", "pods", "-o", "json"]
        if self.selector:
            args += ["-l", self.selector]
        return self.kube.run(*args)["items"]

    def service_name(self, pod):
        return pod["metadata"].get("labels", {}).get(self.service_label)

    def telemetry_name(self, service):
        return self.telemetry_names.get(service, service)

    def resolve(self, service):
        pods = [p for p in self.snapshot() if self.service_name(p) == service]
        if not pods:
            raise APIError("entity_not_found", f"No pods for service {service!r} in this target", 404)
        return sorted(pods, key=lambda p: p["metadata"]["name"])

    def service_id(self, service):
        return f"{self.target}/service/{service}"

    def pod_id(self, pod):
        return f'{self.target}/pod/{pod["metadata"]["uid"]}'

    def entities(self):
        entities, services = [], set()
        for pod in self.snapshot():
            service = self.service_name(pod)
            if not service:
                continue
            services.add(service)
            entities.append(Entity(id=self.pod_id(pod), kind="pod", name=pod["metadata"]["name"], attributes={
                "service": service, "namespace": self.kube.namespace,
                "node": pod.get("spec", {}).get("nodeName"),
                "phase": pod.get("status", {}).get("phase"),
                "containers": [c["name"] for c in pod["spec"]["containers"]],
            }))
        entities += [Entity(id=self.service_id(s), kind="service", name=s, attributes={
            "namespace": self.kube.namespace, "telemetry_service_name": self.telemetry_name(s),
        }) for s in sorted(services)]
        return sorted(entities, key=lambda e: e.id)

    def relations(self):
        return [Relation(source=self.service_id(self.service_name(p)), target=self.pod_id(p),
                         kind="contains", evidence="kubernetes:pod-label")
                for p in self.snapshot() if self.service_name(p)]
