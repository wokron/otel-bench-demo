"""Prepare a single-replica flagd to watch a versioned ConfigMap. Default: preview."""
import argparse
import copy
import json
from pathlib import Path

from demo_api.providers.kubernetes import Kubectl


def prepare(deployment, service, configmap):
    deployment, service = copy.deepcopy(deployment), copy.deepcopy(service)
    json.loads(configmap["data"]["demo.flagd.json"])["flags"]
    if deployment["spec"].get("replicas", 1) != 1:
        raise ValueError("Only a single flagd replica is supported")
    if any(k.startswith("demo-api.chaos-bench/") for k in configmap["metadata"].get("annotations", {})):
        raise ValueError("Recover active business experiments before changing the deployment")
    spec = deployment["spec"]["template"]["spec"]
    container = next(c for c in spec["containers"] if c["name"] == "flagd")
    args = container.get("command", []) + container.get("args", [])
    if "file:./etc/flagd/demo.flagd.json" not in args:
        raise ValueError("Expected otel-demo flagd file source")
    mount = next(m for m in container["volumeMounts"] if m["mountPath"] == "/etc/flagd")
    volume = mount["name"]
    if mount.get("subPath"):
        raise ValueError("subPath mounts cannot watch ConfigMap updates")
    for i, v in enumerate(spec["volumes"]):
        if v["name"] == volume:
            spec["volumes"][i] = {"name": volume, "configMap": {"name": configmap["metadata"]["name"],
                                      "items": [{"key": "demo.flagd.json", "path": "demo.flagd.json"}]}}
    for c in spec["containers"]:
        for m in c.get("volumeMounts", []):
            if m["name"] == volume:
                m["readOnly"] = True
    init = spec.get("initContainers", [])
    for c in init:
        if c["name"] != "init-config" and any(m["name"] == volume for m in c.get("volumeMounts", [])):
            raise ValueError("Unexpected init container uses the flag volume")
    spec["initContainers"] = [c for c in init if c["name"] != "init-config"]
    # Keep OFREP explicit, including in older deployments without a service port.
    if "--ofrep-port" not in args:
        container.setdefault("args", []).extend(["--ofrep-port", "8016"])
    elif args[args.index("--ofrep-port") + 1] != "8016":
        raise ValueError("Expected OFREP on 8016")
    existing = next((p for p in service["spec"]["ports"] if p.get("port") == 8016), None)
    if existing:
        target_port = existing.get("targetPort", 8016)
        if isinstance(target_port, str):
            target_port = next((p["containerPort"] for p in container.get("ports", []) if p.get("name") == target_port), None)
        if target_port != 8016:
            raise ValueError("Existing service port 8016 does not target flagd OFREP")
    else:
        if any(p.get("name") == "demo-api-ofrep" for p in service["spec"]["ports"]):
            raise ValueError("Service port name conflict")
        service["spec"]["ports"].append({"name": "demo-api-ofrep", "port": 8016, "targetPort": 8016, "protocol": "TCP"})
    return deployment, service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="minikube")
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--deployment", default="flagd")
    parser.add_argument("--service", default="flagd")
    parser.add_argument("--configmap", default="flagd-config")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", default=".state/flagd-backend-backup.json")
    args = parser.parse_args()
    kube = Kubectl({"context": args.context, "namespace": args.namespace})
    deployment = kube.run("get", "deployment", args.deployment, "-o", "json")
    service = kube.run("get", "service", args.service, "-o", "json")
    cm = kube.run("get", "configmap", args.configmap, "-o", "json")
    new_deployment, new_service = prepare(deployment, service, cm)
    if not args.apply:
        print(json.dumps({"notice": "Preview only. ConfigMap becomes authoritative; UI editing becomes read-only.",
                          "deployment": new_deployment, "service": new_service}, indent=2))
        return
    backup = Path(args.backup)
    backup.parent.mkdir(parents=True, exist_ok=True)
    with backup.open("x") as f:
        json.dump({"context": args.context, "namespace": args.namespace,
                   "deployment": deployment, "service": service, "configmap": cm}, f, indent=2)
    # Each replace retains resourceVersion. A partial failure is reviewable in the backup;
    # do not automatically overwrite resources after a concurrent edit.
    kube.run("replace", "-f", "-", "-o", "json", document=new_service)
    kube.run("replace", "-f", "-", "-o", "json", document=new_deployment)
    print(kube.run("rollout", "status", "deployment/" + args.deployment, "--timeout=15s", json_output=False))
    print(f"Prepared flagd; original resources saved to {backup}. Projection/evaluation must still be verified.")


if __name__ == "__main__":
    main()
