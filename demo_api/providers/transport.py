"""HTTP endpoint or short-lived Kubernetes port-forward, owned by this request."""
import contextlib
import socket
import subprocess
import tempfile
import time

import httpx

from demo_api.models import APIError


@contextlib.contextmanager
def endpoint(config, kube):
    if config.get("url"):
        yield config["url"].rstrip("/")
        return
    with tempfile.TemporaryFile(mode="w+") as output:
        # Let kubectl choose the free port; parse its announcement to avoid a bind/rebind race.
        command = [kube.binary, f"--context={kube.context}", f"--namespace={kube.namespace}",
                   "--request-timeout=15s", "port-forward", "--address=127.0.0.1",
                   f'svc/{config["service"]}', f':{int(config["port"])}']
        try:
            process = subprocess.Popen(command, stdout=output, stderr=output, text=True)
        except OSError as exc:
            raise APIError("backend_unavailable", "Cannot start kubectl port-forward") from exc
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                output.seek(0)
                lines = output.read()
                for line in lines.splitlines():
                    if line.startswith("Forwarding from 127.0.0.1:"):
                        port = int(line.split("127.0.0.1:")[1].split(" ")[0])
                        yield f"http://127.0.0.1:{port}"
                        return
                if process.poll() is not None:
                    raise APIError("backend_unavailable", lines[:1500])
                time.sleep(0.05)
            raise APIError("backend_unavailable", "port-forward readiness timed out")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def request_json(method, url, **kwargs):
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            response = client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise APIError("backend_unavailable", f"Telemetry request failed: {exc}") from exc
