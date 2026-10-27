# 从统一接口生成系统近期图景

`system_overview.py` 是一个独立的 API 消费方，只依赖 httpx 和 Python 标准库，不导入 API 服务内部模块，也不直接访问 Kubernetes、Prometheus 或 Jaeger。默认自动查询 `/v1/targets` 中的全部目标，也可以选择一个或多个目标。

在 `impl` 目录启动 API，然后在另一个终端运行样例：

```bash
uv run demo-api --config configs/otel-catalog-metrics.yaml
```

```bash
# 默认覆盖所有 target，查询最近 15 分钟，打印文本并保存三种报告
uv run python examples/system_overview.py

# 最近一小时，只看指定 target
uv run python examples/system_overview.py \
  --url http://127.0.0.1:8000 --target otel-demo \
  --minutes 60 --limit 100 --workers 4 --output .reports/overview

# 离线查看报告
cat .reports/overview.txt
xdg-open .reports/overview.html
```

输出：

- **终端及 `.txt`**：目标及能力、服务/实例、查询覆盖与失败、指标统计、日志样本、span 数与错误标志、采样 span p95、近期/未结束实验。
- **`.html`**：可离线打开，无 JavaScript/CDN 依赖；展示服务和实例结构图、从 trace 直接观察到的跨服务调用边、各指标的 SVG 时间曲线，以及完整文本。原始日志和实体名称经过 HTML 转义。
- **`.json`**：保存本次读取的资源、查询结果、错误及摘要，便于后续 agent 复核或分析。文件可能包含应用日志、SQL 和其他遥测内容，输出目录默认由 Git 忽略。

支持通过环境变量 `DEMO_API_TOKEN` 传递 Bearer token。`--target` 可重复指定；`--minutes` 为 1..60，符合现有 API 一小时时间窗限制。查询使用共同的 UTC 时间窗，`--limit` 对每个服务、每种信号及每个指标分别生效，不是整个报告总上限。用 `--workers` 限制并发，默认最多 4 次遥测查询并行；源系统较慢时可以改为 1。指标自动从 capabilities 中逐一读取，没有写死 otel-demo 的服务名或指标名称。

## 报告如何解释

1. 先读取 capabilities、entities、relations 和 experiments；随后为发现的每个服务查询已配置的 signals，并查询每个已公布的 metric。个别数据源失败会保留错误，其余采集继续；target 列表无法读取时程序失败退出。
2. 按 `(trace_id, span_id)` 去重，以服务的遥测名称映射回实体。只统计 span 开始时间处于查询窗口内的样本；跨服务边只来自相同 trace 的直接父子 span，不凭数据库地址猜测服务身份。
3. Pod 及 `contains` 关系来自实体发现。蓝色箭头表示采样 trace 的跨服务调用，灰色虚线表示服务成员。只在 trace 中出现的服务单独标注为实体目录未发现。
4. 指标保留单位、序列标签、最新样本时间、min/max 与趋势。不会默认把任意序列视作 counter、计算请求速率或套用未经配置的异常阈值。空/非有限样本不作为数值参与统计。
5. 日志展示记录数、最多 3 条样例和结构化 ERROR/FATAL/CRITICAL/PANIC 数量；不通过简单匹配正文里的 `error` 推断故障。截断、采样和 provider warnings 会保留在报告中。
6. 实验只读取最近 100 条本地记录，展示窗口内创建或尚未结束的实验。不会请求单条实验刷新端点，因为该端点可能触发到期撤销；也不会执行或预览故障。

“整个系统”指 API 配置可发现的作用域。当前 catalog 配置只发现应用组件，不包括 Jaeger、Collector、Prometheus 等基础设施；报告不是集群资产清单。各接口分次读取，不是原子快照，扩缩容时可能出现暂时不一致。实体目录也不提供已退出 Pod 的历史。

Running 不等于 Ready，观测到的 span p95 不是端到端请求 p95，查询为空不是健康证明。未配置的资源指标、完整调用依赖、历史实例和被采样掉的请求无法由此样例补齐。结构图没有跨服务边，只表示本次样本缺少对应证据。

## 实测

2026-10-27 对本机 API 的运行发现 `astronomy-db`、`flagd`、`product-catalog` 三个服务及其 Running Pod，返回 product-catalog RPC 指标与去重 traces，读取到此前两条已清理故障实验。三十分钟窗口下其他若干查询为空，报告保留了这些空结果及其数据源限制；没有将服务标为健康。当前实际图形产物在 `.reports/overview.html`。

随后对最近 60 分钟窗口的实测：9 次信号查询中 5 次非空、4 次空结果；共返回 14 个去重 span、75 条 astronomy-db 日志和 21 条 flagd 日志。product-catalog 的 `rpc.calls` 返回一条指标序列；近期两条实验均已取消。flagd 样本包括长时间 span，报告按原始数值展示，不将它直接判定为请求超时或服务故障。
