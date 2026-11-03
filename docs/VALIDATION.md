# 本机验证记录

时间：2026-10-27，Asia/Shanghai。目标：context `minikube`、namespace `otel-demo`、release `otel-demo`，Helm chart 0.41.0 / demo 3.0.0。以下是实际运行结果；不代表其他 demo 或 full 默认配置已经验证。

## 自动测试与契约

`uv run pytest -q`：**21 passed**。覆盖认证、实体/能力路由、空结果与不支持能力的区别、查询约束、无副作用预览、幂等键冲突、重启后按期恢复、恢复失败重试、结果不确定时持久化意图、限制重叠实验、Jaeger 字段/时间转换、Prometheus 单位与字符串转义及非有限值、Pod UID、ChaosBlade 作用域/参数/撤销状态、Pod 删除语义、OpenSearch 部分失败和带点属性键、第三方 entry point 加载。

当前锁定依赖中的 Starlette 对 httpx TestClient 发出一条弃用提示；不影响测试通过，后续升级测试客户端时可处理。

`bash -n otel-demo/demo.sh scripts/chaosblade.sh` 通过；catalog observability overlay 已用 Helm 渲染并实际部署；全部四份配置已通过 Pydantic 校验；OpenAPI 与配置 JSON Schema 已从源码导出。

## 集成验证

以 `uv run demo-api --config configs/otel-catalog-metrics.yaml` 启动真实 HTTP 服务后运行：

```bash
uv run python scripts/conformance.py --target otel-demo --service product-catalog \
  --require-records metrics traces
```

实际输出：

```text
PASS discovery: unique IDs, service present, relation endpoints resolve
PASS metrics: 1 records, source=prometheus
PASS logs: 0 records, source=kubernetes-logs
PASS traces: 2 records, source=jaeger
PASS action preview: pod.kill
PASS action preview: cpu.load
PASS action preview: network.delay
```

- 发现 3 个应用 service 和对应 Pod；包含关系引用的实体 ID 均存在。
- `rpc.calls` 通过统一 query API 返回非空序列。实测源指标为 `rpc_server_call_duration_seconds_count`，服务标签 `job=product-catalog`。
- `product-catalog` 的当前 stdout 无记录，因此五分钟 logs 查询为空；一次要求 logs 必须非空的检查如实失败。没有把这个结果当作日志内容已验证。
- 另以统一 query API 查询 `astronomy-db` 最近 59 分钟日志，返回 HTTP 200 和 5 条非空 PostgreSQL 日志（limit=5，truncated=true），验证了真实日志字段与时间转换。此来源是 stdout/stderr，不是 OTLP 日志数据库。
- 原脚本 `check catalog` 再次通过：ListProducts 返回 10 件商品；Jaeger 包含新 traces 和 PostgreSQL 数据库 spans。

## ChaosBlade 与实际故障

安装脚本成功部署 operator 和 tool daemonset，两个 Pod 均 Running/Ready。源码 tag v1.7.4，镜像明确设为 1.7.4，webhook 关闭。

实际执行 `cpu.load`，5%、10 秒：

- API 创建持久化实验并返回 pending。
- 后端返回 Error，详情为 Docker 客户端 API 1.24 低于节点要求的 1.44，故障未生效。
- API 保留完整 backend_status，截止时间后从 cancelling 进入 cancelled，CR 被删除。
- 该限制来自 exec-cri 的固定客户端版本；没有改变 Docker daemon 配置来绕过。

实际执行 `pod.kill`，目标 `product-catalog`，10 秒清理期限：

- 原 Pod：`product-catalog-6d6b5cbfbd-8x6wm`，UID `9c18f77d-feaf-4379-acda-4b7a400f21a4`。
- ChaosBlade 状态 Running，resStatuses.success=true，identifier 精确指向该 namespace/Pod。
- 原 Pod 被删除，Deployment 创建替代 Pod `product-catalog-6d6b5cbfbd-ch4f2` 并 Ready。
- 实验由 running 经 cancelling 到 cancelled；最终集群无 ChaosBlade 实验 CR。
- 重建后 `check catalog` 再次通过真实商品查询、fresh traces 与数据库 span 验证。

`network.delay` 只验证了参数、计划生成和后端配置，未实际注入；它依赖容器运行时，不宣称在本机 Docker 29 上可用。

## 保留的环境变更与边界

- `otel-demo` 的 catalog 现在运行 6 个 Pod：原 5 个组件加 `otel-demo-prometheus`；Collector metrics 管线改为 Prometheus OTLP receiver。
- 新增 `chaosblade` namespace、operator、每节点 tool，以及相关 CRD/RBAC；没有修改 default namespace 原有 release 的配置。
- 测试实验已清理，SQLite 历史记录位于被 Git 忽略的 `.state/`。
- 未验证完整 full 部署、真实 OpenSearch 管线、第二套 demo、Docker Compose、网络注入、API 停机期间独立硬 TTL。停机恢复依赖 API 重启或离线恢复脚本；没有硬 TTL 保证。

## 系统概览消费方样例

新增 `examples/system_overview.py` 后测试总数为 **24 passed**。新增测试验证全服务自动查询、缺失能力跳过、部分后端失败保留、重复 trace 去重、时间窗过滤、服务名映射、直接父子调用边、成员关系、HTML 内容转义及缺失数值处理。

真实 HTTP 样例以 60 分钟窗口查询当前 API：发现 3 个服务，执行 9 次遥测查询（5 次非空、4 次空结果），得到 14 个去重 span、75 条数据库日志、21 条 flagd 日志及 product-catalog RPC 指标序列。生成 `.reports/overview.txt`、`.reports/overview.html`、`.reports/overview.json`，实验只读取列表，未执行或刷新实验。

## 2026-11-03 ChaosBlade 安装与 HTTP 故障复验

启动原有 minikube 后，通过 `HELM="$PWD/.cache/tools/linux-amd64/helm" bash otel-demo/demo.sh chaos-install catalog` 成功升级 ChaosBlade release 至 revision 2。新增 `chaos-install/chaos-check` 入口；检查 CRD、operator Deployment 和 tool DaemonSet 均通过，节点仍为 Docker 29.2.1，CPU/网络运行时限制不变。

通过真实 HTTP API 运行 `scripts/chaos_smoke.py --execute --action pod.kill`，实验 `da-1e198a5295e94ce499b7aace9ce08d62` 从 pending 进入 running，backend resStatuses.success=true；目标为 `otel-demo/minikube/product-catalog-6d6b5cbfbd-ch4f2`。脚本通过 DELETE 请求清理并确认 cancelled，最终无 ChaosBlade CR。新 Pod `product-catalog-6d6b5cbfbd-nqhrv` Ready；原业务检查返回 10 件商品，并确认 fresh traces 和 PostgreSQL spans。

测试总数 **26 passed**；新增用例验证 smoke 脚本成功和后端失败时均请求清理。Shell 语法检查通过。此次未重新执行 CPU/网络注入，也没有调整 Docker daemon。

## 2026-11-03 概览数据质量改进

实际排查发现 catalog 没有持续负载，改进前仅公布一个累计 RPC counter；将该指标查向数据库和 flagd 会产生不适用的空结果。真实 Prometheus 查询进一步确认 1 分钟 rate 窗口经常没有足够的导出样本，而 5 分钟窗口可返回速率。

新增有限流量脚本运行 120 秒，208 次 ListProducts 全部成功；报告仍是只读消费方。catalog 配置扩充为 7 类指标，声明仅适用 product-catalog。业务速率/错误/分位数排除 reflection，错误比率无流量时保留 null，不填充为健康的零。

使用真实 API 生成最近 10 分钟报告（limit=500），得到 624 个去重 span，其中 208 个数据库 span 的 server.address 精确匹配 astronomy-db。示例快照：5 分钟业务速率约 0.3583 calls/s，错误比率 0%，Prometheus 直方图 P95 4.75 ms，Go runtime 内存 17578248 bytes，SQL 池 idle=1/inuse=0。这是有限流量后的窗口数据，不是持续负载基准；直方图 P95 和 trace 样本分位数算法不同。

HTML 新增就绪/重启/指标卡片、重点发现、明确数据库依赖、操作级追踪表与有数值刻度的趋势图；当前图形报告含 9 个有效 SVG。28 项测试通过。未增加基础设施 CPU/RSS、数据库 exporter、持久化日志或源采集时间检测；未观测到的能力仍按缺口呈现。
