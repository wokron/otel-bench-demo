# 微服务 Demo API 与 otel-demo 运行环境

新增的统一 API 已实现实体发现、可观测性查询和 ChaosBlade 故障操作，并支持独立 provider 插件与多 target 配置。

- [API 架构、运行与使用](docs/API.md)
- [ChaosBlade 安装、API 接入和实际故障验收](docs/CHAOS.md)
- [系统近期图景样例：文本、拓扑与指标图](examples/README.md)
- [供其他 agent 实现后端的接入规范](docs/SPEC.md)
- [OpenAPI 契约](docs/openapi.json) / [配置 JSON Schema](docs/config.schema.json)
- [本机集成验证记录与兼容性限制](docs/VALIDATION.md)

```bash
uv sync --extra test
uv run demo-api --config configs/otel-catalog-metrics.yaml
# http://127.0.0.1:8000/docs
```

当前机器已为 `otel-demo` namespace 的 catalog 启用可查询的 Prometheus，并安装 ChaosBlade。原始 catalog 可用 `configs/otel-catalog.yaml`（不声明 metrics）；其他机器需先按 API 文档部署对应组件。Pod 删除故障已验证，CPU/网络依赖的容器运行时兼容性限制见 API 文档。以下保留原有部署调查记录；其中的五 Pod 数量指未启用指标 overlay 的原始 catalog。

如果报告的观测数据较少，可以运行 `otel-demo/traffic.py --duration 120` 产生一段商品查询流量（需要 grpcurl），再用 `uv run python examples/system_overview.py --minutes 15` 生成报告。catalog metrics 配置同时查询 RPC 速率、错误速率和 P95 延迟；空结果会被标记为“无法判断”，不会当作系统健康。

---

# 在 minikube 上运行 OpenTelemetry Demo

## 调查结论

**minikube 可以运行 otel-demo；当前机器已实测业务应用及追踪，也已独立部署并验证一个较小的商品目录子集。完整默认配置（包括 OpenSearch）的端到端运行尚未在本机验证。** 瓶颈是资源、镜像下载和具体配置，没有发现 minikube 本身的兼容性阻碍。

2026-10-27 本机检查结果：

| 项目 | 证据及含义 |
| --- | --- |
| 主机 | Linux Mint 22.3，x86_64，32 个逻辑 CPU，约 15 GiB 内存 |
| 集群 | minikube v1.38.1，Docker driver，Kubernetes v1.35.1；已有 profile `minikube` 配置 4 CPU、6144 MiB 内存 |
| 磁盘 | Docker 所在根分区可用空间约 5–6 GiB；minikube 报磁盘使用率 97% 警告，不适合再下载整套镜像 |
| 已有部署 | `default` namespace 的 Helm release `my-otel-demo`，chart `0.41.0` / demo `3.0.0`，用户配置仅 `opensearch.enabled: false` |
| 已有部署验证 | 启动原有集群后 29 个 Pod 全部 Ready；前端代理 `/` 返回 HTTP 200 和 HTML，Jaeger 查到新生成的 frontend traces |
| 已有部署限制 | 没有 OpenSearch Service，应用/Collector 出现 `lookup opensearch ...`；`check full` 在日志存储检查处按预期失败。Pod Ready 不能证明所有遥测链路正常，也没有验证聊天等所有业务功能 |
| 新部署子集 | 在独立 namespace `otel-demo` 安装本目录脚本提供的 `catalog` 配置，5 个 Pod 全部 Ready；真实 gRPC `ListProducts` 返回 10 件商品，Jaeger 存有本次调用的新 traces，包含 PostgreSQL span；Collector 日志也确认收到 metrics 和 logs |

官方 Kubernetes 文档要求 Kubernetes 1.24+、**应用可用内存至少 6 GB**、Helm 3.14+。这一数值不包含主机和集群开销，也不是对所有新版本默认组件的资源保证。对本次固定 chart 渲染清单核算：

| 方案 | 工作负载 | 容器 memory limits 合计（单节点、各一副本） | 建议起点 |
| --- | --- | --- | --- |
| `full` | 30 个工作负载：完整默认应用、Collector、Jaeger、Prometheus、Grafana、OpenSearch 等 | 9060 MiB，约 8.85 GiB | minikube 6 CPU / 12 GiB；主机建议 24 GiB+ 内存，Docker 分区预留至少 20 GiB 空间 |
| `catalog` | 5 个 Deployment：product-catalog、astronomy-db、flagd（含 UI sidecar）、Collector、Jaeger | 1289 MiB，约 1.26 GiB | minikube 4 CPU / 6 GiB；本机实际复用了已缓存镜像 |

limits 合计是配置核算，不是实测 RSS，也不是最小运行内存。Docker driver 的节点 allocatable 可能反映宿主机内存；本机节点报告约 15 GiB，但 `docker inspect minikube` 的容器内存上限实际是 6 GiB，不能据 allocatable 认为内存足够。

## 目录和版本

- `otel-demo/demo.sh`：启动、渲染、部署、状态、检查、访问和卸载入口。
- `otel-demo/values-full.yaml`：保留官方 chart 默认组件及遥测管线。
- `otel-demo/values-catalog.yaml`：保留商品查询及其依赖，将 traces 发往 Jaeger；metrics/logs 发往 Collector `debug` exporter，不提供指标/日志数据库。
- `otel-demo/check.py`：调用应用并查询新 traces；catalog 还检查 PostgreSQL span，full 还检查 OpenSearch 中存在日志。

固定使用 **Helm chart 0.41.0 / demo 3.0.0**，与本机已有缓存和部署一致，避免在磁盘紧张时下载另一套镜像。调查时最新 demo release 为 3.1.0，本次没有验证该版本。脚本从官方 GitHub Release 下载包含子 chart 的归档到 `impl/.cache/otel-demo/`，无需 `helm dependency update`。chart 版本固定不等于全部镜像 digest 固定：上游 init container 使用 `busybox:latest` / `busybox`，其内容仍可能变化。

## 快速运行已验证的子集

前提：Bash、Docker（当前用户可访问 daemon）、minikube、kubectl、Helm 3.14+、curl、Python 3；`check catalog` 额外需要 [grpcurl](https://github.com/fullstorydev/grpcurl/releases)。kubectl 应与集群版本匹配（本机的 kubectl 1.32.9 跨版本检查成功，但超出对 1.35 的官方版本偏差支持范围）。需要能访问 GitHub 和各镜像仓库，或已预载镜像。

以下命令在 `impl` 目录执行：

```bash
bash otel-demo/demo.sh start catalog
bash otel-demo/demo.sh render catalog > /tmp/otel-catalog.yaml
bash otel-demo/demo.sh deploy catalog
bash otel-demo/demo.sh status catalog
bash otel-demo/demo.sh check catalog
```

默认使用 profile `minikube`、namespace `otel-demo`、release `otel-demo`；每条 kubectl/Helm 命令明确指定 context/namespace，启动时保留当前 context。不会修改已有 `default/my-otel-demo` release。若使用其他 profile，设置 `export PROFILE=otel-catalog`，其余命令不变。`HELM=/path/to/helm`、`GRPCURL=/path/to/grpcurl` 支持使用未加入 PATH 的工具。

成功检查输出示例（trace 数量会变化）：

```text
PASS: ListProducts returned 10 products
PASS: Jaeger stores 2 fresh product-catalog trace(s)
PASS: traces include PostgreSQL database spans
```

访问和手工发请求：

```bash
bash otel-demo/demo.sh forward catalog
```

保持上述进程运行，在另一终端执行：

```bash
grpcurl -plaintext -d '{}' 127.0.0.1:18080 oteldemo.ProductCatalogService/ListProducts
```

浏览器打开 <http://127.0.0.1:16686/jaeger/ui/>，选择 `product-catalog` 查询。gRPC 使用服务端 reflection，无需下载 proto。默认端口可用 `GRPC_PORT`、`JAEGER_PORT` 修改；Ctrl-C 关闭转发。`check` 自动分配临时端口并清理转发进程，不依赖手工 `forward`。

该子集没有商城网页、购物车、结算、Kafka 消费链路和负载发生器，适合对服务调用、数据库访问和 trace 进行实验。flagd 提供应用所需的功能开关服务；保留官方 flagd-ui sidecar 和初始化配置，以保持配置文件生命周期一致。Jaeger 使用内存存储；数据库使用 chart 默认临时存储，Pod 重建可能丢数据。不要用于需持久化的实验结果存储。

## 运行完整默认配置

完整方案提供可执行入口和渲染检查，**本机未完成全新 full 部署及其端到端验收**。已有缓存部署只证明不带 OpenSearch 的业务/追踪链路可运行。资源更充足的环境可按下列命令验收 full；包括 AI 在内的全部业务功能仍需另外配置并验证其外部依赖。

建议先停止不再使用的旧集群以释放内存（停止前确认其工作负载可中断），并确保 Docker 分区空间足够。使用独立 profile，避免已有 release 的同名集群级 RBAC 冲突：

```bash
export PROFILE=otel-full
bash otel-demo/demo.sh start full
bash otel-demo/demo.sh render full > /tmp/otel-full.yaml
bash otel-demo/demo.sh deploy full
bash otel-demo/demo.sh check full
bash otel-demo/demo.sh forward full
```

`start full` 默认 6 CPU、12288 MiB；`CPUS`、`MEMORY` 可覆盖。访问：商城 <http://127.0.0.1:8080/>，Jaeger <http://127.0.0.1:8080/jaeger/ui/>，Grafana <http://127.0.0.1:8080/grafana/>，负载发生器 <http://127.0.0.1:8080/loadgen/>，功能开关 <http://127.0.0.1:8080/feature/>。可用 `LOCAL_PORT` 修改代理本地端口。

`check full` 检查首页、当次请求之后的 frontend traces 和 OpenSearch 日志计数大于零；不覆盖所有页面、所有服务或指标查询。检查成功也不等于负载稳定性、故障注入或 AI 功能已经验证。`values-full.yaml` 保留 OpenSearch，避免单纯关闭存储后仍保留导出目标的问题。

在同一个 release 上切换模式时，`deploy` 使用 `--reset-values`：仅采用指定模式配置，避免残留上次覆盖值。切换会增删组件并可能丢失临时数据，部署前先 `render` 检查。

## 故障排查与清理

```bash
bash otel-demo/demo.sh status catalog
kubectl --context=minikube -n otel-demo logs deploy/otel-collector --tail=100
kubectl --context=minikube -n otel-demo logs deploy/product-catalog --tail=100
kubectl --context=minikube -n otel-demo describe pod <pod-name>
```

- `Pending` / `OOMKilled`：检查 requests、节点可调度资源、Docker 容器内存上限及其他 namespace 负载。不要只看主机空闲内存或 Pod Ready。
- `ImagePullBackOff`：检查事件中的具体仓库、DNS、代理和凭据。主机 `127.0.0.1` 代理在节点内不代表主机；使用节点可达的代理地址，并配置 Kubernetes Service/Pod 网段的 `NO_PROXY`。不要把带密码的代理配置写入仓库。
- `lookup opensearch`：说明日志管线仍指向被关闭的 OpenSearch。使用 full 保留存储，或像 catalog 一样通过 `alternateConfig` 明确替换管线；只设置 `opensearch.enabled: false` 不够。
- Helm `invalid ownership metadata`：同名 ClusterRole 已属于另一个 release；full 应使用干净 profile。catalog 已关闭需要集群级 RBAC 的 Collector presets，可以与旧 full 部署共存。chart 固定了多项资源名，同一 namespace 不支持多套 release。
- `deploy` 超时：查看 `status`，保留失败现场后修复再重跑；脚本没有自动回滚或自动降级到子集。`TIMEOUT=25m` 可延长等待。
- 本机磁盘紧张：先检查 Docker 数据实际所在分区。Docker driver 的磁盘仍受宿主机分区限制，仅改 `--disk-size` 不能增加该分区空间；脚本不会自动执行 prune 或删除其他镜像。
- Helm 打印的官方 NOTES 在 catalog 模式仍提示 frontend-proxy URL，但该组件已被裁剪；请用本 README 的 catalog 访问命令。

卸载本脚本部署的 release，保留 namespace 和集群（确认 `PROFILE`、`NAMESPACE`、`RELEASE` 与安装时一致）：

```bash
bash otel-demo/demo.sh delete catalog
```

需要停止集群时另行执行 `minikube stop -p "$PROFILE"`（未设置 `PROFILE` 时使用 `minikube`）。脚本不删除集群，不清理其他 release 或镜像。验证后本次新增的 catalog release 保留运行，原有 default release 配置未改动。

## 参考

- [官方 Kubernetes 部署文档](https://opentelemetry.io/docs/demo/kubernetes-deployment/)
- [固定 chart 0.41.0 发布归档](https://github.com/open-telemetry/opentelemetry-helm-charts/releases/tag/opentelemetry-demo-0.41.0)
- [Demo 3.0.0 源代码](https://github.com/open-telemetry/opentelemetry-demo/tree/3.0.0)
- [调查时最新 Demo 3.1.0](https://github.com/open-telemetry/opentelemetry-demo/releases/tag/3.1.0)
- [minikube 代理配置](https://minikube.sigs.k8s.io/docs/handbook/vpn_and_proxy/)
