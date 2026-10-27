import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def get_json(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)


def retry(operation, timeout=90):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return operation()
        except (OSError, ValueError, AssertionError, subprocess.SubprocessError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


@contextlib.contextmanager
def forward(profile, namespace, service, remote_port):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with tempfile.TemporaryFile(mode="w+") as output:
        process = subprocess.Popen(
            ["kubectl", f"--context={profile}", "-n", namespace, "port-forward",
             "--address=127.0.0.1", f"svc/{service}", f"{port}:{remote_port}"],
            stdout=output, stderr=output,
        )
        try:
            def ready():
                if process.poll() is not None:
                    output.seek(0)
                    raise RuntimeError(output.read())
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return port
            retry(ready, timeout=30)
            yield port
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    mode, profile, namespace = sys.argv[1:]
    grpcurl = os.environ.get("GRPCURL", "grpcurl")
    if mode == "catalog" and not shutil.which(grpcurl):
        raise RuntimeError("Install grpcurl or set GRPCURL=/path/to/grpcurl")
    with contextlib.ExitStack() as stack:
        jaeger_port = stack.enter_context(forward(profile, namespace, "jaeger", 16686))
        started = int(time.time() * 1_000_000)
        if mode == "catalog":
            port = stack.enter_context(forward(profile, namespace, "product-catalog", 8080))
            result = subprocess.check_output(
                [grpcurl, "-plaintext", "-max-time", "15", "-d", "{}",
                 f"127.0.0.1:{port}", "oteldemo.ProductCatalogService/ListProducts"],
                text=True,
            )
            products = json.loads(result)["products"]
            assert len(products) > 0, "Product catalog returned no products"
            print(f"PASS: ListProducts returned {len(products)} products")
            service = "product-catalog"
        else:
            port = stack.enter_context(forward(profile, namespace, "frontend-proxy", 8080))
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=30) as response:
                assert response.status == 200
                assert b"<html" in response.read().lower()
            print("PASS: storefront returned HTTP 200 and HTML")
            service = "frontend"

        def traces():
            data = get_json(
                f"http://127.0.0.1:{jaeger_port}/jaeger/ui/api/traces"
                f"?service={service}&start={started}&limit=20"
            )["data"]
            assert any(trace.get("spans") for trace in data), "No fresh application traces"
            return data

        data = retry(traces)
        print(f"PASS: Jaeger stores {len(data)} fresh {service} trace(s)")
        if mode == "catalog":
            spans = [span for trace in data for span in trace["spans"]]
            assert any(
                tag["key"] in ("db.system", "db.system.name")
                and tag["value"] in ("postgresql", "postgres")
                for span in spans for tag in span.get("tags", [])
            ), "Fresh traces contain no PostgreSQL spans"
            print("PASS: traces include PostgreSQL database spans")
        else:
            port = stack.enter_context(forward(profile, namespace, "opensearch", 9200))

            def logs():
                result = get_json(f"http://127.0.0.1:{port}/otel-logs-*/_count")
                assert result["count"] > 0, "No stored application logs"
                return result["count"]

            print(f"PASS: OpenSearch stores {retry(logs)} log records")


if __name__ == "__main__":
    main()
