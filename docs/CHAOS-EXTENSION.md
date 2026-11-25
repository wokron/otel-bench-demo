# 混沌注入能力扩展调查

调查日期：2026-11-25。本文是实施建议，候选动作和配置尚未实现或注入验收。

后续实施状态：首批三个业务动作和 composite provider 已实现，使用实际实现的配置而非本文草案，见 [业务故障接口](BUSINESS-FAULTS.md)。其余候选能力仍按调查建议处理。

## 结论与实施顺序

推荐继续复用统一 experiments 接口和现有实验状态机，先修复 ChaosBlade 的 Docker 客户端，再扩充网络和资源动作；通过可组合 actuator 接入 otel-demo 的业务故障开关。Chaos Mesh 可作为独立后端选项，不需要为扩展动作立即迁移整个系统。

建议按以下顺序交付：

1. 增加只读运行时预检，明确区分“配置了动作”和“当前环境能执行”；修复并构建兼容当前节点的 ChaosBlade tool，先验收现有 CPU、网络延迟动作。
2. 增加 network.loss、network.partition 和 memory.load；网络动作支持只影响指定服务之间的通信。
3. 引入 composite actuator，加入 otel-demo 专用的业务失败 provider；先做 payment 和 cart 的失败率，随后做商品特定请求失败。
4. 按需加入 container.kill、带宽限制、网络包重复/损坏，以及受控的磁盘 IO 故障。需要其他 Kubernetes demo 后端时，再验收 Chaos Mesh provider。

当前 minikube 为 Stopped，本次只调查代码、上游版本和部署资料，没有启动集群、升级工具或注入故障。此前 Pod 删除成功、CPU 注入失败的依据见 [VALIDATION.md](VALIDATION.md)。本次根分区可用空间约 3.8 GiB，后续构建/试验需据实际容量选择构建位置。

## 当前实现的两个限制

`demo_api/providers/chaosblade.py` 只注册了 pod.kill、cpu.load、network.delay。即使在配置的 allowed_actions 中加入新名字，也会因未注册而报错。新增动作需要参数模型、后端映射、状态/恢复验证；不能直接把 CLI 或任意 matcher 透传给调用方。

`TargetConfig.actions` 目前只有一个 provider；故障接口本身可以复用，但同一 target 要同时使用 ChaosBlade、flagd 或 Chaos Mesh，需要组合 provider。当前 capabilities 的 actions 是静态配置声明，不代表运行时已就绪。当前网络延迟只指定网卡，没有限定通信对端，会影响选中 Pod 的较大范围流量。

## Docker 兼容性：单纯升级还不够

之前实测节点为 Docker 29.2.1，最低 API 1.44；旧 ChaosBlade exec-cri 1.7.4 固定请求 API 1.24。此次进一步检查发现：

- exec-cri **v1.8.1** 的 `exec/container/docker/docker.go` 仍在 `checkAndCreateClient()` 中使用 `client.FromEnv` 后再显式 `client.WithVersion("1.24")`。后者覆盖环境版本，设置 DOCKER_API_VERSION 不能解决。
- 同文件 `ping()` 只包含服务端版本更低时的降级逻辑，没有解决客户端版本低于服务端最低版本的问题；Ping 成功也不证明后续 ContainerInspect 可用。
- ChaosBlade **v1.8.2** 的 Makefile 使用 exec-cri **v1.8.1**，因此不能因版本较新就断言 Docker 29 兼容。GitHub releases/latest 目前返回 blade-ai 产品版本，不能拿它当 operator/tool 的匹配版本。
- operator **v1.8.0** 源码内 Helm values 仍默认 operator/tool **1.7.5**。升级脚本要验证渲染后的镜像，不能只更换 chart tag。

推荐修复路径是：针对 exec-cri 的 Docker client 使用 SDK 的 API 协商机制（例如支持该机制的 SDK 中的 WithAPIVersionNegotiation），移除固定 1.24 的覆盖；同时检查所选 SDK 的最高支持版本与节点最低版本是否相交。不能只把字符串改成 1.44 就宣称通用兼容。验证 ContainerInspect、容器选择、执行、停止及网络清理全过程，再将修复版本封装成独立 tool 镜像，以 digest 固定。保留镜像来源、补丁、构建命令和回滚配置。

operator/tool 不同版本组合必须验收生成命令及其参数兼容性；还需检查 operator 所用 exec-cri 的代码路径是否也需修复，不能只替换任意一个二进制。Pod UID 校验、container ID 解析、cgroup v2 的资源归属、tc/nsenter 支持都是后续独立验证项。

另一个选择是在独立 profile 中使用兼容的 containerd。Docker driver 与 Kubernetes container runtime 是两个层次，Docker driver 的 minikube 也可以选择 containerd；但这不是对现有部署原地切换一个参数。旧 operator 的节点 tool 默认只挂载 Docker socket，必须检查和配置 containerd socket、namespace、权限及恢复路径。该方案需要额外部署与存储，不作为本机第一步。

## 候选动作及接入难点

以下 API 名称为建议；除已有三个动作外均未注册。ChaosBlade 原语必须以选定版本的 spec 和实际 tool help 为准。

| 建议动作 | 执行路径 | 实施要点与边界 | 优先级 |
| --- | --- | --- | --- |
| cpu.load（已有） | ChaosBlade cpu/fullload | 修复 Docker；显式定义 worker/核数和百分比含义，不能把节点 CPU 百分比当容器配额百分比 | 第一批 |
| network.delay（已有） | ChaosBlade network/delay | 增加对端 service、方向、端口和抖动；解析为本 target 的 IP/Pod，记录实际匹配范围 | 第一批 |
| network.loss | ChaosBlade network/loss | 丢包率、对端和端口；初始验收采用低丢包率，独立测量成功率 | 第一批 |
| network.partition | ChaosBlade network/drop 等映射，或 Chaos Mesh NetworkChaos partition | 明确方向及协议；不能简单宣称 100% loss 与所有协议的双向隔离等价 | 第一批 |
| memory.load | ChaosBlade mem/load | 优先绝对 MiB；按目标 memory limit 限制，不使用不明分母的百分比；释放压力不代表 OOM 后应用已恢复 | 第一批 |
| container.kill | CRI 或 Chaos Mesh PodChaos container-kill | 固定业务 container name 和 ID，区别于 Pod 删除；容器通常由 kubelet 原 Pod 内重启 | 第二批 |
| network.bandwidth / duplicate / corrupt | 支持相应 spec 的 ChaosBlade 或 Chaos Mesh | 带宽、包参数必须结构化；测量实际吞吐和包行为 | 第二批 |
| process.kill / hang | ChaosBlade process | PID namespace 和多容器选择；进程匹配不可接收任意 shell；恢复 hang 与恢复 kill 语义不同 | 第二批 |
| disk.io / disk.fill | ChaosBlade disk 或合适的 IO 后端 | 仅显式批准的临时测试卷；限定字节数、文件路径、清理归属。当前磁盘空间不足，不优先做 fill | 后续 |
| http.abort / delay | 支持 HTTPChaos 的后端或代理 | 与 TCP/gRPC 故障分开；确认协议、TLS、端口、流量路径，不能假定对所有语言和 gRPC 自动有效 | 后续 |
| dns.failure | DNSChaos 或专用实现 | 验证应用 DNS 缓存、UDP/TCP DNS 和恢复后的解析结果 | 后续 |

不建议把节点级 CPU、磁盘写满、时钟偏移作为第一批动作：本机是单节点，两套 demo 和观测组件共享节点，难以保持 target 隔离。这是当前拓扑下的范围判断，而非这些功能永远不支持。

## otel-demo 原生业务故障：有价值的第二条路径

otel-demo 3.0.0 的 flagd 配置包含多个业务故障，不需要通过 Docker API 注入。可以实现独立的 `otel-demo-flags` provider，向统一接口发布有业务含义的动作，而不是开放任意开关修改。

| 建议动作 | 上游 flag | 需要说明的语义 |
| --- | --- | --- |
| payment.failure | paymentFailure | 使用当前配置实际定义的 variant 与数值，不能只解析 variant 名字 |
| payment.unavailable | paymentUnreachable | 应用主动模拟支付不可达，区别于真实网络分区 |
| cart.failure | cartFailure | 指定支持的失败比例，并验证请求足够多时的实际结果 |
| product-catalog.failure | productCatalogFailure | 按产品 ID 命中；上游带 targeting，单改 defaultVariant 可能仍被规则覆盖 |
| recommendation.cache-failure | recommendationCacheFailure | 缓存故障及其请求触发条件需要源码与业务验收 |
| ad.cpu / ad.gc | adHighCpu / adManualGc | 应用内部负载；不能等同任意服务的通用 CPU 注入 |
| email.memory-leak | emailMemoryLeak | 关闭开关可能只停止继续泄漏，不能假定已分配内存被回收 |

上游文件中 paymentFailure 的 `90%` variant 数值为 **0.95**，而 cartFailure 对应值为 0.9；adapter 必须检查配置内容，不能把 variant 标签直接当准确失败率。

Helm chart 0.37.1 的 flagd 部署会用 init container 将 ConfigMap 文件复制到共享 emptyDir，再由 flagd 和 flagd-ui 读写。因此仅 patch ConfigMap **未必会改变当前运行中的开关**。实施前必须读取实际 release manifest 和运行文件：确认使用 flagd-ui 的哪种写入接口、还是需要部署一个受控配置写入组件。不能假设 OFREP/flag evaluation 接口就是管理写接口。

故障计划应保存原 flag 及 targeting、实际写入值、配置版本、flagd Pod UID 和生效证据。撤销只恢复本实验修改的字段，并做版本/值比较；检测到外部修改时保留冲突，不覆盖人工更新。恢复目标是原值，不是一律设成 off。应用故障可能需要特定商品、购物车、支付或异步消息流量触发，报告必须显示这些触发条件。provider 的动作仅对配置和服务都存在的 target 发布。

## 保持接口不变，组合执行后端

核心 HTTP 路径、ActionRequest 和 `plan/apply/status/cancel` 协议可以保留。增加一个 `composite` actuator，通过明确的动作路由组合子 provider，避免在 API 路由中判断 demo 名字。

```yaml
# 设计草案，当前不能直接加载此配置。
actions:
  plugin: composite
  config:
    providers:
      infra:
        plugin: chaosblade
        config:
          allowed_actions: [pod.kill, cpu.load, network.delay, network.loss]
      business:
        plugin: otel-demo-flags
        config:
          allowed_actions: [payment.failure, cart.failure]
    routes:
      pod.kill: infra
      cpu.load: infra
      network.delay: infra
      network.loss: infra
      payment.failure: business
      cart.failure: business
```

组合层需要拒绝动作名冲突。plan/持久化 handle 中保存子 provider ID、实现版本、配置指纹、资源身份及恢复快照；status/cancel 必须按创建时的后端恢复。当前 SQLite 文档没有独立的后端身份，直接更改 target 的 actions provider 可能把旧实验交给错误后端，实施组合 provider 时要提供旧记录兼容和配置变更检查。保留现有“每 target 一个未恢复实验”限制，暂不引入多故障编排。

建议为动作描述增加可选的 `availability`（available/unavailable/unknown）、reason、checked_at、backend_version、services、effect_scope、recovery_semantics。现有 `reversible` 保留兼容，但不足以表达 kill、内存泄漏的后果。预检不创建故障；执行前再次检查，unavailable 时在创建后端实验前拒绝，unknown 不能冒充已验证。availability=available 也只表示前置条件满足，实际注入仍须检查 backend_status 和独立效果证据。

针对网络动作，parameters 中的 peer_service 由服务端在同一个 target 解析；禁止调用方自行覆盖 namespace、selector 或任意 IP。记录解析时的 Pod UID、IP、Service VIP 和实际匹配策略，验证 kube-proxy DNAT 前后的匹配位置。源/目标 Pod 替换后默认停止并要求重新计划，避免静默扩大到新实例。新 IP、方向和端口必须和后端实际支持的过滤能力一致。

## Chaos Mesh 是否值得接入

值得作为候选 provider。调查到的 v2.8.8 包含 NetworkChaos 的 delay/loss/partition/bandwidth 等原语，以及 StressChaos 的 CPU/memory；其 Docker client 使用新 Moby API，并未沿用 ChaosBlade exec-cri 的固定 1.24 初始化方式。但这只消除了一个已知代码阻碍，不等于已证明它与本机 Kubernetes 1.35、Docker 29、cgroup v2、内核及网络配置兼容。

接入方式仍是独立 actuator，转换相同业务动作，配置运行时和 socket。需要 controller、daemon、CRD/RBAC 等额外组件；初始可关闭不需要的 dashboard。CPU worker 百分比、内存大小、目标筛选及恢复状态必须明确映射，不能因为动作名称相同就认定语义相同。HTTPChaos、DNSChaos、IOChaos 各有额外部署和协议条件，按需单独验收。

NetworkChaos/StressChaos 支持后端 duration，可减少对本 API 进程定时器的依赖；控制器/daemon/节点不可用时仍可能延迟恢复，不能称为无条件硬 TTL。当前 ChaosBlade adapter 仍只有 API reconciler 到期撤销，行为不因这篇调查发生变化。

## 验收交付

每个新增动作需交付参数 schema、作用范围、前置条件、dry-run 示例、执行/撤销示例、独立效果观测、恢复后业务检查、失败/未知结果恢复记录。HTTP 200、CR 创建、tool Ready 或后端 Running 任一个单独信号，都不构成完整验收。

优先验证以下闭环：

1. CPU：固定容器与低 worker 数，比较注入前/中/后的容器 CPU 和业务时延。
2. 网络：选择一条明确调用边，注入小延迟/低丢包；测量该边及一个不应受影响的对照服务，清理后检查 tc/连接及业务恢复。
3. 内存：有限 MiB 压力，观测容器使用量；确认压力进程退出、内存变化与业务状态。
4. 业务失败：固定交易/商品流量，观察错误率和错误 spans；还原 flag 后重新请求。
5. API 重启/请求超时/Pod 替换/恢复冲突：确认不重复注入、不误伤替代实例、不覆盖外部配置，并保留可恢复实验记录。

现有 report 能按时间窗口查询遥测与实验，但不自动构成因果实验。先提供一个顺序的 baseline → inject → observe → cancel → verify 示例程序即可，不必增加复杂工作流引擎。

## 上游依据

以下按本次调查固定 tag；本机部署历史与其他项目的旧实测不能替代这些版本的验证。

- [exec-cri v1.8.1 Docker 客户端初始化与 ping](https://github.com/chaosblade-io/chaosblade-exec-cri/blob/v1.8.1/exec/container/docker/docker.go)
- [exec-cri v1.7.4 同一代码路径](https://github.com/chaosblade-io/chaosblade-exec-cri/blob/v1.7.4/exec/container/docker/docker.go)
- [ChaosBlade v1.8.2 构建组件版本](https://github.com/chaosblade-io/chaosblade/blob/v1.8.2/Makefile)
- [operator v1.8.0 chart 默认镜像](https://github.com/chaosblade-io/chaosblade-operator/blob/v1.8.0/deploy/helm/chaosblade-operator/values.yaml)
- [otel-demo 3.0.0 故障开关配置](https://github.com/open-telemetry/opentelemetry-demo/blob/3.0.0/src/flagd/demo.flagd.json)
- [Helm demo 0.37.1 的 flagd 共享文件部署](https://github.com/open-telemetry/opentelemetry-helm-charts/blob/opentelemetry-demo-0.37.1/charts/opentelemetry-demo/values.yaml)
- [Chaos Mesh v2.8.8 NetworkChaos](https://github.com/chaos-mesh/chaos-mesh/blob/v2.8.8/api/v1alpha1/networkchaos_types.go)
- [Chaos Mesh v2.8.8 StressChaos](https://github.com/chaos-mesh/chaos-mesh/blob/v2.8.8/api/v1alpha1/stresschaos_types.go)
- [Chaos Mesh v2.8.8 Docker client](https://github.com/chaos-mesh/chaos-mesh/blob/v2.8.8/pkg/chaosdaemon/crclients/docker/client.go)
- [Chaos Mesh v2.8.8 运行时与 socket 配置](https://github.com/chaos-mesh/chaos-mesh/blob/v2.8.8/helm/chaos-mesh/values.yaml)
