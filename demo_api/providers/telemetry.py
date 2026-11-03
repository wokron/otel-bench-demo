import json
import math
from datetime import datetime, timezone

from demo_api.models import APIError, Observation
from demo_api.providers.transport import endpoint, request_json


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


class Backend:
    def __init__(self, config, context):
        self.config = config
        self.discovery = context["discovery"]
        self.kube = getattr(self.discovery, "kube", None)

    def describe(self):
        return {"provider": self.__class__.__name__.lower(), "configured": True}


class Jaeger(Backend):
    def query(self, q):
        service = self.discovery.telemetry_name(q.service)
        with endpoint(self.config, self.kube) as base:
            # Search rather than trace-by-ID keeps service and time constraints meaningful.
            data = request_json("GET", base + self.config.get("api_path", "/api") + "/traces", params={
                "service": service, "start": int(q.start.timestamp() * 1e6),
                "end": int(q.end.timestamp() * 1e6), "limit": q.limit,
            })
        if data.get("errors"):
            raise APIError("backend_query_failed", str(data["errors"])[:1000])
        records = []
        for trace in data.get("data", []):
            if q.trace_id and trace["traceID"].lower() != q.trace_id.lower():
                continue
            processes = trace.get("processes", {})
            spans = []
            for span in trace.get("spans", []):
                tags = {t["key"]: t.get("value") for t in span.get("tags", [])}
                spans.append({
                    "trace_id": span["traceID"], "span_id": span["spanID"],
                    "parent_span_id": next((r["spanID"] for r in span.get("references", [])
                                            if r.get("refType") == "CHILD_OF"), None),
                    "service": processes.get(span.get("processID"), {}).get("serviceName"),
                    "name": span["operationName"], "timestamp": iso(span["startTime"] / 1e6),
                    "duration_ms": span["duration"] / 1000, "attributes": tags,
                    "status": "error" if tags.get("error") is True else "unset",
                })
            records.append({"trace_id": trace["traceID"], "spans": spans})
        return Observation(signal="traces", source="jaeger", records=records[:q.limit],
                           truncated=len(data.get("data", [])) >= q.limit,
                           warnings=["Search returns sampled/stored traces; cap applies to traces, not spans."])


class Prometheus(Backend):
    def describe(self):
        return {**super().describe(), "metrics": [
            {"name": name, "unit": spec["unit"], "description": spec["description"],
             **({"services": spec["services"]} if "services" in spec else {})}
            for name, spec in self.config["metrics"].items()]}

    def query(self, q):
        spec = self.config["metrics"].get(q.metric)
        if spec is None:
            raise APIError("unsupported_metric", f"Unknown metric {q.metric!r}; see capabilities", 422)
        # Quoted string substitution only; metric templates are trusted administrator input.
        expression = spec["promql"].replace("$service", json.dumps(self.discovery.telemetry_name(q.service)))
        with endpoint(self.config, self.kube) as base:
            data = request_json("GET", base + "/api/v1/query_range", params={
                "query": expression, "start": q.start.timestamp(), "end": q.end.timestamp(),
                "step": q.step_seconds,
            })
        if data.get("status") != "success":
            raise APIError("backend_query_failed", str(data.get("error", "Prometheus query failed")))
        if data["data"].get("resultType") != "matrix":
            raise APIError("backend_invalid_response", "Expected Prometheus matrix")
        results = data["data"]["result"]
        warnings = list(data.get("warnings", []))
        records = []
        for series in results[:q.limit]:
            points = []
            for timestamp, value in series["values"]:
                number = float(value)
                points.append({"timestamp": iso(timestamp), "value": number if math.isfinite(number) else None})
            records.append({"metric": q.metric, "unit": spec["unit"], "labels": series["metric"], "points": points})
        if any(p["value"] is None for s in records for p in s["points"]):
            warnings.append("Non-finite Prometheus values represented as null.")
        return Observation(signal="metrics", source="prometheus", records=records,
                           truncated=len(results) >= q.limit, warnings=warnings)


class KubernetesLogs(Backend):
    def describe(self):
        return {**super().describe(), "retention": "current containers only; bounded by kubelet rotation",
                "correlation": "pod and service; trace_id only if present in structured application log"}

    def query(self, q):
        records, truncated = [], False
        pods = self.discovery.resolve(q.service)
        for pod in pods:
            for container in pod["spec"]["containers"]:
                output = self.kube.run("logs", pod["metadata"]["name"], "-c", container["name"],
                                       "--timestamps=true", f"--since-time={q.start.isoformat()}",
                                       f"--tail={q.limit + 1}", json_output=False)
                lines = output.splitlines()
                truncated |= len(lines) > q.limit
                for line in lines:
                    timestamp, _, body = line.partition(" ")
                    try:
                        parsed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if not q.start <= parsed_time <= q.end:
                        continue
                    try:
                        structured = json.loads(body)
                    except ValueError:
                        structured = {}
                    if not isinstance(structured, dict):
                        structured = {}
                    trace_id = structured.get("trace_id") or structured.get("traceId")
                    if q.trace_id and trace_id != q.trace_id:
                        continue
                    records.append({"timestamp": iso(parsed_time.timestamp()), "service": q.service,
                                    "pod": pod["metadata"]["name"], "container": container["name"],
                                    "body": body, "trace_id": trace_id,
                                    "severity": structured.get("severity") or structured.get("level")})
        records.sort(key=lambda r: r["timestamp"], reverse=True)
        return Observation(signal="logs", source="kubernetes-logs", records=records[:q.limit],
                           truncated=truncated or len(records) > q.limit,
                           warnings=["Application stdout/stderr, not Collector OTLP log storage; no previous-container logs."])


class OpenSearch(Backend):
    def describe(self):
        return {**super().describe(), "field_mapping": self.config.get("fields", {})}

    def query(self, q):
        fields = {"timestamp": "@timestamp", "service": "resource.attributes.service.name.keyword",
                  "body": "body", "trace_id": "trace_id", "severity": "severity_text",
                  **self.config.get("fields", {})}
        filters = [{"range": {fields["timestamp"]: {"gte": q.start.isoformat(), "lte": q.end.isoformat()}}},
                   {"term": {fields["service"]: self.discovery.telemetry_name(q.service)}}]
        if q.trace_id:
            filters.append({"term": {fields["trace_id"]: q.trace_id}})
        with endpoint(self.config, self.kube) as base:
            data = request_json("POST", base + "/" + self.config.get("index", "otel-logs-*") + "/_search",
                                json={"size": q.limit, "query": {"bool": {"filter": filters}},
                                      "sort": [{fields["timestamp"]: "desc"}]})
        if data.get("timed_out") or data.get("_shards", {}).get("failed", 0):
            raise APIError("backend_query_failed", "OpenSearch timed out or returned failed shards")
        def get(document, path):
            parts = path.removesuffix(".keyword").split(".")
            while parts:
                if not isinstance(document, dict):
                    return None
                # OTel attribute keys may themselves contain dots, e.g. "service.name".
                for count in range(len(parts), 0, -1):
                    key = ".".join(parts[:count])
                    if key in document:
                        document = document[key]
                        parts = parts[count:]
                        break
                else:
                    return None
            return document
        hits = data["hits"]["hits"]
        records = [{**{name: get(h["_source"], field) for name, field in fields.items()},
                    "service": q.service} for h in hits]
        return Observation(signal="logs", source="opensearch", records=records, truncated=len(hits) >= q.limit)
