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
            metrics = [m["name"] for m in capability.get("metrics", [])] if signal == "metrics" else [None]
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
    return {"services": services, "call_edges": [{"source": a, "target": b, "observed_spans": count}
                                                 for (a, b), count in sorted(edges.items())],
            "unique_spans_returned": len(spans)}


def metric_svg(points, label):
    valid = [(utc(p["timestamp"]).timestamp(), p["value"]) for p in points
             if isinstance(p.get("value"), (float, int)) and math.isfinite(p["value"])]
    if not valid:
        return "<p>无有效数值样本</p>"
    lo, hi = min(v for _, v in valid), max(v for _, v in valid)
    t0, t1 = min(t for t, _ in valid), max(t for t, _ in valid)
    coords = [(15 + 450 * (t - t0) / (t1 - t0 or 1), 85 - 65 * (v - lo) / (hi - lo or 1)) for t, v in valid]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    return (f'<svg viewBox="0 0 480 110" role="img" aria-label="{escape(label, quote=True)}">'
            f'<title>{escape(label)}; min={lo:g}, max={hi:g}</title>'
            '<path d="M15 10V85H470" fill="none" stroke="#adb5bd"/>'
            f'<polyline points="{path}" fill="none" stroke="#1971c2" stroke-width="2"/>'
            + "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2" fill="#1971c2"/>' for x, y in coords)
            + f'<text x="15" y="105" font-size="10">{escape(datetime.fromtimestamp(t0, timezone.utc).strftime("%H:%M:%S"))} UTC</text>'
            f'<text x="350" y="105" font-size="10">{escape(datetime.fromtimestamp(t1, timezone.utc).strftime("%H:%M:%S"))} UTC</text></svg>')


def topology_svg(summary):
    """Left: observed cross-service calls; right: deployment membership, not calls."""
    services = summary["services"]
    edges = summary["call_edges"]
    names = sorted(set(services) | {e[k] for e in edges for k in ("source", "target")})
    if not names:
        return "<p>未发现服务；请检查实体发现请求结果。</p>"
    width, height = 1000, max(200, len(names) * 110 + 60)
    positions = {name: (210, 65 + i * 110) for i, name in enumerate(names)}
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="服务调用与实例成员关系">',
           '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0 0L8 4L0 8" fill="#1971c2"/></marker></defs>',
           '<text x="15" y="20" font-size="13">蓝色箭头：采样 span 的跨服务调用；灰色虚线：部署成员关系</text>']
    for e in edges:
        x1, y1 = positions[e["source"]]
        x2, y2 = positions[e["target"]]
        out.append(f'<path d="M{x1-140} {y1} C 5 {y1},5 {y2},{x2-140} {y2}" stroke="#1971c2" fill="none" marker-end="url(#arrow)"/>'
                   f'<text x="8" y="{(y1+y2)/2}" font-size="11">{e["observed_spans"]}</text>')
    for name in names:
        x, y = positions[name]
        service = services.get(name)
        out.append(f'<rect x="{x-140}" y="{y-22}" width="280" height="44" rx="7" fill="#e7f5ff" stroke="#1971c2"/>'
                   f'<text x="{x}" y="{y+5}" text-anchor="middle" font-size="14">{escape(name)}</text>')
        pods = service["pods"] if service else []
        text = "; ".join(p["name"] + " [" + str(p.get("attributes", {}).get("phase", "unknown")) + "]" for p in pods)
        if not service:
            text = "仅在 trace 中出现；实体目录未发现"
        elif not pods:
            text = "未发现实例"
        out.append(f'<path d="M350 {y}H390" stroke="#868e96" stroke-dasharray="5 4"/>'
                   f'<foreignObject x="400" y="{y-35}" width="585" height="90"><div xmlns="http://www.w3.org/1999/xhtml" style="font:12px sans-serif;overflow-wrap:anywhere">{escape(text)}</div></foreignObject>')
    out.append('</svg>')
    return "".join(out)


def render_text(report):
    lines = ["微服务系统近期观测报告", f'窗口：{report["start"]} → {report["end"]}',
             "范围：API 已配置 target 及其实体发现作用域；查询为分次采集，非原子快照。",
             "结论边界：Running 不代表 Ready；采样 trace 的 span 时延不等于请求时延；无错误记录不证明健康。",
             "指标按原单位展示，不自动把累计计数当速率；最新样本时间可用于判断数据陈旧程度。"]
    for target in report["targets"]:
        lines += ["", f'系统 {target["target"]["id"]} — {target["target"].get("description", "") }']
        resources = target["resources"]
        summary = target["summary"]
        lines.append(f'发现服务 {len(summary["services"])} 个；返回去重 span {summary["unique_spans_returned"]} 个。')
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
            for q in s["queries"]:
                lines.append(f'    {q["signal"]}/{q["metric"] or "-"}: {q["status"]}, records={q["records"]}, source={q["source"]}, truncated={q["truncated"]}')
                if q["error"]:
                    lines.append("      " + q["error"])
                lines.extend("      限制: " + w for w in q["warnings"])
            for m in s["metrics"]:
                latest = m["latest"]
                lines.append(f'    指标 {m["name"]} {m["labels"]}: 最新={latest["value"] if latest else None} {m["unit"]}, 时间={latest["timestamp"] if latest else None}, min={m["min"]}, max={m["max"]}')
            for log in s["log_examples"]:
                body = re.sub(r"[\x00-\x1f\x7f]", " ", str(log.get("body", "")))[:300]
                lines.append(f'    日志样本 {log.get("timestamp")}: {body}')
        if not summary["call_edges"]:
            lines.append("  调用图：未从本次样本观察到跨服务 parent-child 关系；不代表服务之间没有依赖。")
        for edge in summary["call_edges"]:
            lines.append(f'  观测调用 {edge["source"]} → {edge["target"]}: {edge["observed_spans"]} 个子 span')
        experiments = resources["experiments"].get("value", {}).get("experiments", [])
        recent = [e for e in experiments if e.get("created_at", 0) >= utc(report["start"]).timestamp()
                  or e.get("state") not in {"cancelled", "completed"}]
        lines.append(f'  最近/未结束实验 {len(recent)} 条（最近100条本地记录；不刷新后端状态）：')
        for e in recent:
            lines.append(f'    {e["id"]}: {e["action"]}, state={e["state"]}, service={e.get("plan", {}).get("service")}')
    return "\n".join(lines) + "\n"


def render_html(report, text):
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
             '<title>微服务系统近期观测报告</title><style>body{font:15px system-ui;max-width:1200px;margin:32px auto;padding:0 20px;color:#212529;background:#f8f9fa}section,details{background:white;padding:20px;margin:16px 0;border:1px solid #dee2e6;border-radius:8px}svg{width:100%;height:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}h1,h2,h3{color:#1864ab}.metric{max-width:600px}</style>',
             '<h1>微服务系统近期观测报告</h1>', f'<p>{escape(report["start"])} → {escape(report["end"])}</p>',
             '<p>按 API 能力发现采集；没有数据与查询失败分别记录。图中的连线仅表示已观察到的关系。</p>']
    for target in report["targets"]:
        parts.append(f'<section><h2>{escape(target["target"]["id"])}</h2>' + topology_svg(target["summary"]))
        if not target["summary"]["call_edges"]:
            parts.append('<p>未观察到跨服务调用边；同一服务内的数据库 span 不自动解释为两个服务之间的调用。</p>')
        for name, service in target["summary"]["services"].items():
            for metric in service["metrics"]:
                label = f'{name} / {metric["name"]} [{metric["unit"]}] {metric["labels"]}'
                parts.append('<div class="metric"><h3>' + escape(label) + '</h3>' + metric_svg(metric["points"], label) + '</div>')
        parts.append('</section>')
    parts += ['<details open><summary>完整文本、数据覆盖及限制</summary><pre>', escape(text), '</pre></details></html>']
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
