"""Discover targets through Demo API and render recent evidence as text, HTML and JSON.

Run from impl: uv run python examples/system_overview.py --minutes 15
Only calls public HTTP endpoints. Never executes, previews or refreshes experiments.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

import httpx


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def request(client, method, path, **kwargs):
    """Preserve missing data and API failures as evidence, rather than abort a report."""
    try:
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return {"status": "ok", "value": response.json()}
    except (httpx.HTTPError, ValueError) as exc:
        message = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            message = f"HTTP {exc.response.status_code}: {exc.response.text[:600]}"
        return {"status": "error", "error": message}


def collect(client, target, start, end, limit=100, workers=4):
    prefix = "/v1/targets/" + quote(target["id"], safe="")
    resources = {}
    for name in ("capabilities", "entities", "relations", "experiments"):
        resources[name] = request(client, "GET", prefix + "/" + name)
    entities = resources["entities"].get("value", {}).get("entities", [])
    caps = resources["capabilities"].get("value", {}).get("telemetry", {})
    services = sorted({e["name"] for e in entities if e["kind"] == "service"})
    tasks = []
    for service in services:
        for signal in ("metrics", "logs", "traces"):
            capability = caps.get(signal, {})
            if not capability.get("configured"):
                continue
            metrics = [m["name"] for m in capability.get("metrics", [])
                       if "services" not in m or service in m["services"]] if signal == "metrics" else [None]
            for metric in metrics:
                tasks.append((service, signal, metric))

    def query(task):
        service, signal, metric = task
        body = {"signal": signal, "service": service, "start": start.isoformat(), "end": end.isoformat(),
                "limit": limit, "step_seconds": max(15, math.ceil((end - start).total_seconds() / 999))}
        if metric:
            body["metric"] = metric
        return {"service": service, "signal": signal, "metric": metric,
                **request(client, "POST", prefix + "/query", json=body)}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        observations = list(pool.map(query, tasks))
    return {"target": target, "resources": resources, "observations": observations,
            "collection_finished_at": datetime.now(timezone.utc).isoformat()}


def observation_data(item):
    return item.get("value", {}).get("data", {})


def format_value(value, unit):
    if value is None:
        return "无有效样本"
    if unit == "bytes":
        return f"{value / 1024**2:.2f} MiB"
    if unit == "seconds":
        return f"{value * 1000:.3f} ms"
    if unit == "ratio":
        return f"{value * 100:.2f}%"
    return f"{value:.4g} {unit}"


def summarize(snapshot, start, end):
    """Count unique spans in-window, and infer only directly evidenced service edges."""
    resources = snapshot["resources"]
    entities = resources["entities"].get("value", {}).get("entities", [])
    services = {e["name"]: {"entity": e, "pods": [], "queries": [], "span_count": 0,
                            "error_spans": 0, "durations_ms": [], "trace_ids": set(),
                            "log_count": 0, "error_logs": 0, "log_examples": [], "metrics": []}
                for e in entities if e["kind"] == "service"}
    by_id = {e["id"]: e for e in entities}
    relations = resources["relations"].get("value", {}).get("relations", [])
    # Prefer explicit membership; attributes.service provides a fallback for snapshot races.
    membership = {r["target"]: by_id[r["source"]]["name"] for r in relations
                  if r["kind"] == "contains" and r["source"] in by_id
                  and by_id[r["source"]]["kind"] == "service"}
    for entity in entities:
        if entity["kind"] == "pod":
            name = membership.get(entity["id"], entity.get("attributes", {}).get("service"))
            if name in services:
                services[name]["pods"].append(entity)
    telemetry_to_service = {e.get("attributes", {}).get("telemetry_service_name", name): name
                            for name, s in services.items() for e in [s["entity"]]}
    spans = {}
    for item in snapshot["observations"]:
        service = services[item["service"]]
        data = observation_data(item)
        records = data.get("records", [])
        service["queries"].append({"signal": item["signal"], "metric": item["metric"],
                                   "status": item["status"], "records": len(records),
                                   "error": item.get("error"), "source": data.get("source"),
                                   "truncated": data.get("truncated", False), "warnings": data.get("warnings", [])})
        if item["status"] != "ok":
            continue
        if item["signal"] == "traces":
            for trace in records:
                for span in trace.get("spans", []):
                    spans[(span["trace_id"], span["span_id"])] = span
        elif item["signal"] == "logs":
            service["log_count"] += len(records)
            for record in records:
                # Use structured severity only. Plain-text substring matching yields false positives.
                severity = str(record.get("severity") or "").upper()
                service["error_logs"] += severity in {"ERROR", "FATAL", "CRITICAL", "PANIC"}
            service["log_examples"] = records[:3]
        elif item["signal"] == "metrics":
            for series in records:
                points = sorted(series.get("points", []), key=lambda p: p["timestamp"])
                valid = [p for p in points if isinstance(p.get("value"), (float, int))
                         and math.isfinite(p["value"])]
                service["metrics"].append({"name": series["metric"], "unit": series["unit"],
                                           "labels": series["labels"], "points": points,
                                           "latest": valid[-1] if valid else None,
                                           "min": min((p["value"] for p in valid), default=None),
                                           "max": max((p["value"] for p in valid), default=None)})
    edges = Counter()
    dependencies = {}
    operations = {}
    for key, span in spans.items():
        try:
            in_window = start <= utc(span["timestamp"]) <= end
        except (ValueError, KeyError):
            continue
        if not in_window:
            continue
        source = telemetry_to_service.get(span.get("service"), span.get("service"))
        if source in services:
            s = services[source]
            s["span_count"] += 1
            s["trace_ids"].add(key[0])
            s["error_spans"] += span.get("status") == "error"
            if span.get("duration_ms") is not None:
                s["durations_ms"].append(span["duration_ms"])
            attrs = span.get("attributes", {})
            # Classify long-lived streams/reflection separately from business operations.
            span_kind = str(attrs.get("span.kind", "")).lower()
            kind = ("database" if attrs.get("db.system.name") or attrs.get("db.system") else
                    "messaging" if attrs.get("messaging.system") else
                    "http" if span_kind == "server" and (attrs.get("http.method") or attrs.get("http.request.method")) else
                    "rpc" if span_kind == "server" and (attrs.get("rpc.system") or attrs.get("rpc.system.name")) else
                    span_kind or "other")
            op_key = (source, span.get("name", "?"), kind)
            op = operations.setdefault(op_key, {"service": source, "name": span.get("name", "?"), "kind": kind,
                                                 "samples": 0, "errors": 0, "durations": []})
            op["samples"] += 1
            op["errors"] += span.get("status") == "error"
            if span.get("duration_ms") is not None:
                op["durations"].append(span["duration_ms"])
            db_system = attrs.get("db.system.name") or attrs.get("db.system")
            address = attrs.get("server.address") or attrs.get("net.peer.name")
            # Exact discovered service name only, with explicit database span evidence.
            if db_system and address in services and address != source:
                dep = dependencies.setdefault((source, address), {"source": source, "target": address,
                    "system": db_system, "observed_spans": 0, "evidence": "database span server.address"})
                dep["observed_spans"] += 1
            # Cache endpoints and broker addresses require an exact discovered name match.
            broker = address or attrs.get("messaging.kafka.bootstrap.servers")
            if attrs.get("messaging.system") and broker:
                for entry in str(broker).split(","):
                    host = entry.strip().split(":")[0]
                    if host in services and host != source:
                        dep = dependencies.setdefault((source, host), {"source": source, "target": host,
                            "system": attrs["messaging.system"], "observed_spans": 0,
                            "evidence": "messaging span broker address"})
                        dep["observed_spans"] += 1
        parent = spans.get((key[0], span.get("parent_span_id")))
        if parent:
            caller = telemetry_to_service.get(parent.get("service"), parent.get("service"))
            if caller and source and caller != source:
                edges[(caller, source)] += 1
    for s in services.values():
        s["trace_ids"] = sorted(s["trace_ids"])
        durations = sorted(s.pop("durations_ms"))
        s["span_p95_ms"] = durations[math.ceil(len(durations) * .95) - 1] if durations else None
        s["pod_phases"] = dict(Counter(p.get("attributes", {}).get("phase", "unknown") for p in s["pods"]))
    for op in operations.values():
        values = sorted(op.pop("durations"))
        op["p50_ms"] = values[math.ceil(len(values) * .5) - 1] if values else None
        op["p95_ms"] = values[math.ceil(len(values) * .95) - 1] if values else None
    findings = []
    caps = resources.get("capabilities", {}).get("value", {}).get("telemetry", {})
    for signal, capability in caps.items():
        findings.extend({"severity": "unknown", "service": None, "text": f"{signal}: {warning}"}
                        for warning in capability.get("warnings", []))
    for name, service in services.items():
        queries = service["queries"]
        failed = [q for q in queries if q["status"] != "ok"]
        if failed:
            findings.append({"severity": "unknown", "service": name,
                             "text": f"{name}: {len(failed)} 遥测查询失败；该服务状态无法完整判断"})
        elif not any(q["records"] for q in queries):
            findings.append({"severity": "unknown", "service": name,
                             "text": f"{name}: 窗口内没有观测记录；需确认采集覆盖和业务流量"})
        if service["error_spans"]:
            findings.append({"severity": "error", "service": name,
                             "text": f"{name}: {service['error_spans']} 条错误 span"})
        if service["error_logs"]:
            findings.append({"severity": "error", "service": name,
                             "text": f"{name}: {service['error_logs']} 条结构化 ERROR+ 日志"})
        if service["pod_phases"].get("Failed") or service["pod_phases"].get("Unknown"):
            findings.append({"severity": "error", "service": name,
                             "text": f"{name}: pod phases include {service['pod_phases']}"})
        for metric in service["metrics"]:
            if not metric["latest"]:
                findings.append({"severity": "unknown", "service": name,
                                 "text": f"{name}: {metric['name']} 没有有效数值样本（不是零）"})
            if metric["latest"]:
                metric["age_seconds"] = max(0, (end - utc(metric["latest"]["timestamp"])).total_seconds())
                if metric["age_seconds"] > 120:
                    findings.append({"severity": "unknown", "service": name,
                                     "text": f"{name}: {metric['name']} latest point is {metric['age_seconds']:.0f}s old"})
            if metric["latest"] and metric["name"] in {"rpc.errors.rate", "error.rate", "server.span.errors.rate"} and metric["latest"]["value"] > 0:
                findings.append({"severity": "error", "service": name,
                                 "text": f"{name}: {metric['name']} is {metric['latest']['value']} {metric['unit']}"})
        for pod in service["pods"]:
            if pod.get("attributes", {}).get("ready") is False:
                findings.append({"severity": "error", "service": name, "text": f"{name}: Pod {pod['name']} is not Ready"})
        rate = next((m for m in service["metrics"] if m["name"] == "rpc.rate" and m["latest"]), None)
        if rate and rate["latest"]["value"] == 0:
            findings.append({"severity": "info", "service": name, "text": f"{name}: 最新 RPC 速率为零；请检查近期业务流量"})
        if any(q["truncated"] for q in queries):
            findings.append({"severity": "info", "service": name,
                             "text": f"{name}: 查询达到上限；trace/日志统计仅代表返回样本，可增大 --limit 或缩短窗口"})
    return {"services": services, "findings": findings, "operations": list(operations.values()),
            "dependencies": list(dependencies.values()),
            "call_edges": [{"source": a, "target": b, "observed_spans": count}
                                                 for (a, b), count in sorted(edges.items())],
            "unique_spans_returned": len(spans)}


def metric_svg(points, label):
    valid = [(utc(p["timestamp"]).timestamp(), p["value"]) for p in points
             if isinstance(p.get("value"), (float, int)) and math.isfinite(p["value"])]
    if not valid:
        return "<p>无有效数值样本</p>"
    lo, hi = min(v for _, v in valid), max(v for _, v in valid)
    t0, t1 = min(t for t, _ in valid), max(t for t, _ in valid)
    coords = [(65 + 390 * (t - t0) / (t1 - t0 or 1), 85 - 65 * (v - lo) / (hi - lo or 1)) for t, v in valid]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    return (f'<svg viewBox="0 0 480 110" role="img" aria-label="{escape(label, quote=True)}">'
            f'<title>{escape(label)}; min={lo:g}, max={hi:g}</title>'
            '<path d="M65 10V85H470" fill="none" stroke="#adb5bd"/>'
            f'<text x="0" y="25" font-size="10">{hi:.4g}</text><text x="0" y="85" font-size="10">{lo:.4g}</text>'
            f'<polyline points="{path}" fill="none" stroke="#1971c2" stroke-width="2"/>'
            + "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="#1971c2"/>' for x, y in coords)
            + f'<text x="65" y="105" font-size="10">{escape(datetime.fromtimestamp(t0, timezone.utc).strftime("%H:%M:%S"))} UTC</text>'
            f'<text x="350" y="105" font-size="10">{escape(datetime.fromtimestamp(t1, timezone.utc).strftime("%H:%M:%S"))} UTC</text></svg>')


def topology_svg(summary):
    """Left: observed cross-service calls; right: deployment membership, not calls."""
    services = summary["services"]
    edges = summary["call_edges"] + summary.get("dependencies", [])
    names = sorted(set(services) | {e[k] for e in edges for k in ("source", "target")})
    if not names:
        return "<p>未发现服务；请检查实体发现请求结果。</p>"
    # A compact grid with focusable edges scales to dozens of services. Full evidence
    # remains in the edge table; it is never hidden by a graph layout heuristic.
    columns = min(4, len(names))
    width, height = 1200, max(200, math.ceil(len(names) / columns) * 130 + 60)
    positions = {name: (150 + (i % columns) * 300, 95 + (i // columns) * 130)
                 for i, name in enumerate(names)}
    out = [f'<div class="topology"><p>点击服务仅突出其相邻依赖；点击空白恢复全部。蓝色：父子 span；绿色：地址匹配依赖。</p>'
           f'<svg class="topology-svg" viewBox="0 0 {width} {height}" role="img" aria-label="服务调用与实例成员关系">',
           '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#1971c2"/></marker></defs>']
    for e in edges:
        x1, y1 = positions[e["source"]]
        x2, y2 = positions[e["target"]]
        y1 -= 27
        y2 -= 27
        color = "#087f5b" if "system" in e else "#1971c2"
        midpoint = min(y1, y2) - 48
        out.append(f'<path class="dependency" data-source="{escape(e["source"], quote=True)}" data-target="{escape(e["target"], quote=True)}" '
                   f'd="M{x1} {y1} C {x1} {midpoint},{x2} {midpoint},{x2} {y2}" stroke="{color}" stroke-opacity="0.45" fill="none" marker-end="url(#arrow)">'
                   f'<title>{escape(e["source"])} → {escape(e["target"])}: {e["observed_spans"]} spans; {escape(e.get("evidence", "parent-child span"))}</title></path>')
    for name in names:
        x, y = positions[name]
        service = services.get(name)
        pods = service["pods"] if service else []
        ready = sum(p.get("attributes", {}).get("ready") is True for p in pods)
        subtitle = f"Ready {ready}/{len(pods)}" if service else "仅在 trace 中出现"
        tooltip = "; ".join(p["name"] for p in pods) or subtitle
        out.append(f'<g class="service-node" data-node="{escape(name, quote=True)}" tabindex="0" role="button" aria-label="{escape(name, quote=True)}">'
                   f'<title>{escape(tooltip)}</title><rect x="{x-125}" y="{y-27}" width="250" height="64" rx="7" fill="#e7f5ff" stroke="#1971c2"/>'
                   f'<text x="{x}" y="{y}" text-anchor="middle" font-size="15">{escape(name)}</text>'
                   f'<text x="{x}" y="{y+22}" text-anchor="middle" font-size="12">{escape(subtitle)}</text></g>')
    out.append('</svg><details><summary>展开全部依赖及来源</summary><table><tr><th>源</th><th>目标</th><th>样本</th><th>证据</th></tr>')
    for edge in edges:
        out.append(f'<tr><td>{escape(edge["source"])}</td><td>{escape(edge["target"])}</td><td>{edge["observed_spans"]}</td><td>{escape(edge.get("evidence", "parent-child span"))}</td></tr>')
    out.append('</table></details></div>')
    return "".join(out)


def render_text(report):
    lines = ["微服务系统近期观测报告", f'窗口：{report["start"]} → {report["end"]}',
             "范围：API 已配置 target 及其实体发现作用域；查询为分次采集，非原子快照。",
             "结论边界：Running 不代表 Ready；采样 trace 的 span 时延不等于请求时延；无错误记录不证明健康。",
             "指标按原单位展示，不自动把累计计数当速率；Prometheus 点时间是查询求值时间，不等于源数据采集时间。"]
    for target in report["targets"]:
        lines += ["", f'系统 {target["target"]["id"]} — {target["target"].get("description", "") }']
        resources = target["resources"]
        summary = target["summary"]
        lines.append(f'发现服务 {len(summary["services"])} 个；返回去重 span {summary["unique_spans_returned"]} 个。')
        lines.append('  重点发现：')
        if summary["findings"]:
            lines.extend(f'    [{f["severity"]}] {f["text"]}' for f in summary["findings"])
        else:
            lines.append('    [info] 当前窗口未发现已证实的错误证据；这不是健康保证。')
        queries = [q for s in summary["services"].values() for q in s["queries"]]
        coverage = Counter("失败" if q["status"] != "ok" else "非空" if q["records"] else "空结果" for q in queries)
        lines.append(f'  数据覆盖：{len(queries)} 次信号查询 {dict(coverage)}；截断 {sum(q["truncated"] for q in queries)} 次。')
        caps = resources["capabilities"].get("value", {}).get("telemetry", {})
        for signal in ("metrics", "logs", "traces"):
            cap = caps.get(signal, {})
            lines.append(f'  能力 {signal}: ' + ("已配置" if cap.get("configured") else "不可用/未知：" + cap.get("reason", "能力发现未成功")))
        for name, resource in resources.items():
            if resource["status"] == "error":
                lines.append(f'  请求失败 {name}: {resource["error"]}')
        for warning in resources["relations"].get("value", {}).get("warnings", []):
            lines.append("  关系限制: " + warning)
        for name, s in summary["services"].items():
            lines += [f'  服务 {name}: Pod {len(s["pods"])} {s["pod_phases"]}',
                      f'    样本：trace {len(s["trace_ids"])}，span {s["span_count"]}，error span {s["error_spans"]}，span p95 {s["span_p95_ms"]} ms；日志 {s["log_count"]}，结构化 ERROR+ {s["error_logs"]}']
            for pod in s["pods"]:
                a = pod.get("attributes", {})
                lines.append(f'    实例 {pod["name"]}: Ready={a.get("ready", "未知")}, restarts={a.get("restarts", "未知")}')
            for q in s["queries"]:
                lines.append(f'    {q["signal"]}/{q["metric"] or "-"}: {q["status"]}, records={q["records"]}, source={q["source"]}, truncated={q["truncated"]}')
                if q["error"]:
                    lines.append("      " + q["error"])
                lines.extend("      限制: " + w for w in q["warnings"])
            for m in s["metrics"]:
                latest = m["latest"]
                lines.append(f'    指标 {m["name"]} {m["labels"]}: 最新={format_value(latest["value"] if latest else None, m["unit"])}, 时间={latest["timestamp"] if latest else None}, min={format_value(m["min"], m["unit"])}, max={format_value(m["max"], m["unit"])}')
            for log in s["log_examples"]:
                body = re.sub(r"[\x00-\x1f\x7f]", " ", str(log.get("body", "")))[:300]
                lines.append(f'    日志样本 {log.get("timestamp")}: {body}')
        if not summary["call_edges"]:
            lines.append("  调用图：未从本次样本观察到跨服务 parent-child 关系；不代表服务之间没有依赖。")
        for edge in summary["call_edges"]:
            lines.append(f'  观测调用 {edge["source"]} → {edge["target"]}: {edge["observed_spans"]} 个子 span')
        for edge in summary["dependencies"]:
            lines.append(f'  数据库访问 {edge["source"]} → {edge["target"]} ({edge["system"]}): {edge["observed_spans"]} spans, evidence={edge["evidence"]}')
        for op in summary["operations"]:
            lines.append(f'  操作 {op["service"]}/{op["name"]} [{op["kind"]}]: 样本={op["samples"]}, errors={op["errors"]}, p50={op["p50_ms"]} ms, p95={op["p95_ms"]} ms')
        experiments = resources["experiments"].get("value", {}).get("experiments", [])
        recent = [e for e in experiments if e.get("created_at", 0) >= utc(report["start"]).timestamp()
                  or e.get("state") not in {"cancelled", "completed"}]
        lines.append(f'  最近/未结束实验 {len(recent)} 条（最近100条本地记录；不刷新后端状态）：')
        for e in recent:
            lines.append(f'    {e["id"]}: {e["action"]}, state={e["state"]}, service={e.get("plan", {}).get("service")}')
    return "\n".join(lines) + "\n"


def render_html(report, text):
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
             '<title>微服务系统近期观测报告</title><style>body{font:15px system-ui;max-width:1200px;margin:32px auto;padding:0 20px;color:#212529;background:#f8f9fa}section,details{background:white;padding:20px;margin:16px 0;border:1px solid #dee2e6;border-radius:8px}svg{width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}h1,h2,h3{color:#1864ab}.metric{max-width:600px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}.card{padding:16px;background:#f1f3f5;border-radius:8px}.error{color:#c92a2a}.unknown{color:#9c5b00}table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:10px;text-align:left;border-bottom:1px solid #dee2e6;overflow-wrap:anywhere}.scroll{overflow-x:auto}</style>',
             '<h1>微服务系统近期观测报告</h1>', f'<p>{escape(report["start"])} → {escape(report["end"])}</p>',
             '<p>按 API 能力发现采集；没有数据与查询失败分别记录。图中的连线仅表示已观察到的关系。</p>']
    for target in report["targets"]:
        findings = target["summary"]["findings"]
        findings = sorted(findings, key=lambda f: {"error": 0, "unknown": 1, "info": 2}.get(f["severity"], 3))
        finding_html = ''.join(f'<li class="{escape(f["severity"])}">{escape(f["text"])}</li>' for f in findings[:8])
        if len(findings) > 8:
            finding_html += '</ul><details><summary>展开其余发现</summary><ul>' + ''.join(
                f'<li class="{escape(f["severity"])}">{escape(f["text"])}</li>' for f in findings[8:]) + '</ul></details><ul>'
        if not finding_html:
            finding_html = '<li>当前窗口未发现已证实的错误证据；这不是健康保证。</li>'
        summary = target["summary"]
        parts.append(f'<section><h2>{escape(target["target"]["id"])}</h2><p>{escape(target["target"].get("description", ""))}</p>'
                     f'<p>服务 {len(summary["services"])} · 去重 span {summary["unique_spans_returned"]} · 调用关系 {len(summary["call_edges"])} · 地址依赖 {len(summary["dependencies"])}</p>'
                     f'<h3>重点发现</h3><ul>{finding_html}</ul>')
        many_services = len(summary["services"]) > 8
        if many_services:
            parts.append('<details><summary>展开所有服务的实例状态与关键指标</summary>')
        parts.append('<div class="grid">')
        for name, service in summary["services"].items():
            ready = sum(p.get("attributes", {}).get("ready") is True for p in service["pods"])
            restarts = sum(p.get("attributes", {}).get("restarts", 0) for p in service["pods"])
            parts.append(f'<div class="card"><h3>{escape(name)}</h3><p>Ready {ready}/{len(service["pods"])} · 累计重启 {restarts}</p>')
            for metric in service["metrics"]:
                latest = metric["latest"]
                if latest and metric["name"] != "rpc.calls":
                    value = format_value(latest["value"], metric["unit"])
                    parts.append(f'<p><b>{escape(metric["name"])}</b>: {escape(value)}<br><small>{escape(str(metric["labels"]))} · {escape(latest["timestamp"])}</small></p>')
            successful = [q for q in service["queries"] if q["status"] == "ok"]
            parts.append(f'<p>数据查询 {len(successful)}/{len(service["queries"])} 成功 · 非空 {sum(q["records"] > 0 for q in successful)}</p>'
                         f'<p>观测 span {service["span_count"]} · 错误 span {service["error_spans"]} · 日志 {service["log_count"]}</p></div>')
        parts.append('</div>')
        if many_services:
            parts.append('</details>')
        parts.append('<h3>应用结构与本窗口观测依赖</h3>' + topology_svg(summary))
        if not target["summary"]["call_edges"]:
            parts.append('<p>未观察到跨服务父子 span。绿色依赖仅在数据库 span 的 server.address 精确匹配已发现服务时展示。</p>')
        parts.append('<details><summary>操作级追踪样本</summary><p>不同操作分别统计，避免把长连接或 reflection 混入业务请求延迟；这些是样本分位数。</p><div class="scroll"><table><tr><th>服务 / 操作</th><th>类型</th><th>样本</th><th>错误</th><th>P50 ms</th><th>P95 ms</th></tr>')
        for op in sorted(summary["operations"], key=lambda o: (-o["samples"], o["name"])):
            parts.append(f'<tr><td>{escape(op["service"])} / {escape(op["name"])}</td><td>{escape(op["kind"])}</td><td>{op["samples"]}</td><td>{op["errors"]}</td><td>{op["p50_ms"]}</td><td>{op["p95_ms"]}</td></tr>')
        parts.append('</table></div></details><h3>指标趋势（按服务展开）</h3><div class="grid">')
        for name, service in target["summary"]["services"].items():
            if not service["metrics"]:
                continue
            parts.append("<details><summary>" + escape(name) + " 指标曲线</summary>")
            for metric in service["metrics"]:
                label = f'{name} / {metric["name"]} [{metric["unit"]}] {metric["labels"]}'
                parts.append('<div class="metric"><h3>' + escape(label) + '</h3>' + metric_svg(metric["points"], label) + '</div>')
            parts.append('</details>')
        parts.append('</div></section>')
    parts.append("<script>\n document.querySelectorAll('.topology-svg').forEach(svg => {\n  function focus(node) {\n   svg.querySelectorAll('.dependency').forEach(edge => {\n    edge.style.opacity = !node || edge.dataset.source === node || edge.dataset.target === node ? '1' : '0.06';\n    edge.style.strokeWidth = node && (edge.dataset.source === node || edge.dataset.target === node) ? '3' : '1';\n   });\n  }\n  svg.addEventListener('click', event => { const node = event.target.closest('[data-node]'); focus(node ? node.dataset.node : null); });\n  svg.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { const node = event.target.closest('[data-node]'); if (node) { event.preventDefault(); focus(node.dataset.node); } } });\n });\n</script>")
    parts += ['<details><summary>展开完整文本、数据覆盖及限制</summary><pre>', escape(text), '</pre></details></html>']
    return "".join(parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--target", action="append", help="Target ID; repeatable. Default: all API targets")
    parser.add_argument("--minutes", type=int, default=15, choices=range(1, 61), metavar="1..60")
    parser.add_argument("--limit", type=int, default=100, choices=range(1, 1001), metavar="1..1000")
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 17), metavar="1..16")
    parser.add_argument("--output", type=Path, default=Path(".reports/overview"), help="Output filename stem")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=args.minutes)
    headers = {"Authorization": f'Bearer {os.environ["DEMO_API_TOKEN"]}'} if os.environ.get("DEMO_API_TOKEN") else {}
    with httpx.Client(base_url=args.url, headers=headers, timeout=120, trust_env=False) as client:
        targets = request(client, "GET", "/v1/targets")
        if targets["status"] != "ok":
            parser.exit(1, "Cannot discover targets: " + targets["error"] + "\n")
        available = targets["value"]["targets"]
        unknown = set(args.target or []) - {t["id"] for t in available}
        if unknown:
            parser.error("Unknown target(s): " + ", ".join(sorted(unknown)))
        selected = [t for t in available if not args.target or t["id"] in args.target]
        snapshots = []
        for target in selected:
            print(f'Collecting {target["id"]} ...', file=sys.stderr)
            snapshot = collect(client, target, start, now, args.limit, args.workers)
            snapshot["summary"] = summarize(snapshot, start, now)
            snapshots.append(snapshot)
    report = {"start": start.isoformat(), "end": now.isoformat(), "targets": snapshots}
    text = render_text(report)
    print(text, end="")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for suffix, content in ((".txt", text), (".html", render_html(report, text)),
                            (".json", json.dumps(report, ensure_ascii=False, indent=2))):
        path = Path(str(args.output) + suffix)
        path.write_text(content, encoding="utf-8")
        print("Saved " + str(path), file=sys.stderr)


if __name__ == "__main__":
    main()
