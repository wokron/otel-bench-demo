# 接入已有的 default/my-otel-demo

`configs/otel-multi.yaml` 同时配置两套已存在的部署，默认先展示业务范围较大的旧部署：

| target ID | context / namespace | 数据源 |
| --- | --- | --- |
| otel-full-existing | minikube / default | 旧 release my-otel-demo 的 jaeger、prometheus 和 Pod 日志 |
| otel-demo | minikube / otel-demo | catalog 的 jaeger、otel-demo-prometheus 和 Pod 日志 |

没有重新部署或修改旧 release，没有停止 catalog，也没有将 catalog 的 target ID 改指旧环境。继续使用 `.state/experiments.sqlite3`，catalog 历史实验仍只属于 otel-demo。两个 ChaosBlade adapter 的作用域分别固定为 default 和 otel-demo；旧环境此次只做查询验收，没有执行故障。

## 启动和生成报告

在 impl 目录执行；先停止原 API 进程，确保同一 SQLite 数据库只有一个 worker：

```bash
uv run demo-api --config configs/otel-multi.yaml
```

另一个终端：

```bash
# 同一份报告中比较两套部署
uv run python examples/system_overview.py --minutes 10 --limit 50
xdg-open .reports/overview.html

# 只看旧部署
uv run python examples/system_overview.py --target otel-full-existing \
  --minutes 10 --limit 50 --output .reports/full
```

limit 是每次信号查询的上限，尤其 full 的单条 trace 含多个服务的 spans；几十条每服务的 trace 已能形成大量样本。报告为每个 target 分别去重和统计，不能把两套部署中的同名服务合并。HTML 支持离线打开，只有少量内联 JavaScript 用于点击服务突出相邻边，不访问 CDN 或服务器；禁用 JS 时仍有完整静态图和依赖明细表。

旧部署本来就在运行官方负载发生器，此次没有另加负载。应用实体按 `opentelemetry.io/name` 发现，本机为 25 个应用组件；Grafana、Collector、Jaeger、Prometheus 等基础设施没有该标签，仍不在应用故障目标中。浏览器 frontend-web 可出现在 traces 中，但它没有 Kubernetes Pod，图中会标明“仅在 trace 中出现”。

## 指标映射

实际数据中存在新旧 HTTP/gRPC 名称和秒/毫秒混用，且 job 有 `frontend`、`ad` 与 `opentelemetry-demo/checkout` 等多种格式，因此不把 catalog 的 PromQL 直接套给所有服务。

新配置基于旧 Collector 已生成的 span metrics 统一查询四类服务端观测指标：

| 指标 | 单位 | 语义 |
| --- | --- | --- |
| server.span.rate | spans/s | 五分钟服务端 span 速率 |
| server.span.errors.rate | errors/s | 服务端 span ERROR 状态速率 |
| server.span.errors.ratio | ratio | ERROR 服务端 span 比例；无流量时不填为零 |
| server.span.latency.p95 | ms | span metrics 直方图估算的服务端 span P95 |

它们是采集到的服务端 span 指标，可能受到采样和多层服务端 instrumentation 影响，不是精确的独立用户请求数，也不是端到端业务 P95。应用 trace 样本中的操作分位数另行展示。

某些语言的资源标签缺少 k8s_namespace_name；模板允许 default 或空标签，并限定 host_name 为旧部署的 otel-collector-agent-*。这是针对当前部署核验的范围规则，若未来同一 Prometheus 接入其他相同命名 Collector，需要重新调整隔离条件。其他实例数据不能仅按 service_name 混合。

另外按实际存在的指标配置：Go runtime 内存（checkout/product-catalog/load-generator）、JVM 内存（ad/fraud-detection/kafka）、SQL 池连接（product-catalog）、Valkey 内存（valkey-cart）。不宣称覆盖所有服务的 CPU/RSS：当前 cAdvisor 序列没有可靠的 namespace/container 关联标签，不能安全地按服务归属。

## 依赖与错误识别

图中蓝边来自直接 parent-child span，绿色边来自数据库/缓存/消息地址精确匹配已发现服务。明确 Redis span 可关联 cart → valkey-cart。消息 span 按消息类型单独统计，但仅在有 broker 地址证据时画到 broker 的边；本版本没有按 span links 或消息 ID 补全异步生产者到消费者关系，缺边不证明不存在依赖。

HTTP 5xx、非 OK gRPC 状态、OTel ERROR 状态和 Jaeger error=true 均规范化为错误。HTTP 4xx 不自动判为服务端故障。实例 Ready 和累计重启数是当前状态，不表示窗口内重启次数。

## 已知日志缺口

旧 release 关闭 OpenSearch，Collector 的日志导出仍指向它，实际日志中可见 DNS 错误和 OTLP logs HTTP 500。此次接入没有修改这个部署配置，也没有宣称已修复。API 使用 Pod stdout/stderr 查询，capabilities 和报告会提示缺少 OTLP 持久化日志；安静服务的 Pod 日志仍可为空。

如要修复 OTLP 日志管线，应另行选择安装日志存储或修改 Collector exporter，并考虑磁盘空间。当前宿主分区余量很少，不应把安装 OpenSearch 作为数据源接入的隐式步骤。
