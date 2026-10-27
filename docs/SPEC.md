# Microservice Demo API 接入规范 v1

本规范的版本标识是 `demo-api/v1`。面向实现新 demo 后端的 agent，以及消费可观测性数据、执行混沌实验的 agent。文中的 MUST、SHOULD、MAY 分别表示必须、建议、可选。机器可读契约见 `openapi.json` 和 `config.schema.json`；Python 插件接口见 `demo_api/plugins.py`。协议与 otel-demo 的名称、Helm chart、Kubernetes context 解耦。

## 1. 简化的数据模型

只保留四个概念：Target（一次部署的作用域）、Entity（服务/实例）、Relation（有证据的关系）、Observation（按实体与时间窗查询的信号）；Action 作用于 Entity，Experiment 记录一次执行的生命周期。

这借鉴了 [UModel](https://arxiv.org/abs/2606.04799) 的实体中心建模、实体与数据关联以及实体工具设计。v1 不实现其通用对象集合、知识库、U-SPL、图查询引擎、MCP 和跨存储 join。调用方先发现实体，再用 service 名查询需要的信号；span 的父子关系支持调用方自行分析调用链。

- Target ID MUST 在配置内唯一，代表固定后端作用域。同一份 state 数据库中 MUST NOT 将已有 ID 重新用于另一套集群/namespace。
- Entity MUST 包含 `id/kind/name/attributes`。service 表示逻辑应用组件，不要求与 Kubernetes Service 同名；当前 Kubernetes provider 从 Pod 标签发现组件。
- v1 service ID 为 `{target}/service/{name}`；Pod ID 为 `{target}/pod/{uid}`。Pod 名重用 MUST 产生新 ID。非 Kubernetes 后端 MUST 使用相同 ID 语义，实例身份放入 pod 类型的 attributes，或另行定义新协议版本。
- Relation MUST 包含 `source/target/kind/evidence`。`contains` 是部署成员关系；`calls` 是调用关系。MUST NOT 用结构关系假充调用拓扑。当前实现只产生 `contains`，不声称具有完整服务依赖图。
- telemetry_service_names MAY 映射部署服务名与遥测资源的 `service.name`。metric 标签映射写在可信后端模板中，调用方无需知道 PromQL。

## 2. HTTP 契约

所有响应为 JSON。路径固定为：

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| GET | `/healthz` | API 存活，不表示目标数据源健康 |
| GET | `/v1/targets` | 已配置目标列表 |
| GET | `/v1/targets/{target}/capabilities` | 支持的实体/关系、信号、指标名称与单位、动作参数 schema、限制 |
| GET | `/v1/targets/{target}/entities` | 当前作用域内实体 |
| GET | `/v1/targets/{target}/relations` | 带证据的关系 |
| POST | `/v1/targets/{target}/query` | 统一信号查询 |
| POST | `/v1/targets/{target}/experiments` | 默认预览；显式执行才产生故障 |
| GET | `/v1/targets/{target}/experiments` | 最近 100 条本地实验记录 |
| GET | `/v1/targets/{target}/experiments/{id}` | 查询并刷新实验状态 |
| DELETE | `/v1/targets/{target}/experiments/{id}` | 幂等请求撤销 |

能力发现 MUST 区分“已配置支持”和“后端当前可访问”。当前 `configured` 表示前者；它不是实时健康检测。缺少某数据源 MUST 返回 `configured:false` 和原因。动作 `requires` MUST 说明运行前提，`reversible` MUST 如实描述。禁止静默换数据源或返回伪造数据。

### 查询

```json
{
  "signal": "metrics",
  "service": "product-catalog",
  "start": "2026-10-27T12:00:00Z",
  "end": "2026-10-27T12:05:00Z",
  "metric": "rpc.calls",
  "step_seconds": 15,
  "limit": 100
}
```

`signal` 为 metrics/logs/traces。时间 MUST 带时区，闭区间 `[start,end]`，最大一小时，end 不得明显晚于当前时间（实现容忍 60 秒时钟偏差）。`limit` 为 1..1000；metrics 限制序列数，logs 限制记录数，traces 限制 trace 数，每条 trace 内保留所需 spans。metrics 每序列最多请求约 1000 个间隔。MUST 返回 `target_id/service/window/data`，其中 data 包含 `signal/source/records/truncated/warnings`。

各信号 records 的规范化格式：

| 信号 | 必须字段 | 单位与语义 |
| --- | --- | --- |
| metrics | `metric,unit,labels,points[]` | point 含 UTC `timestamp` 和数值 `value`；NaN/Inf 为 null 并说明原因 |
| logs | `timestamp,service,body,trace_id,severity` | UTC 时间；无 trace 关联时为 null，不允许编造；Pod stdout provider 另含 pod/container |
| traces | `trace_id,spans[]` | span 含 `trace_id,span_id,parent_span_id,service,name,timestamp,duration_ms,attributes,status` |

metrics 的名称、description、unit MUST 在 capabilities 中自描述。调用方只能选择已公布的 metric；当前实现不开放任意 PromQL。`$service` MUST 作为 JSON 字符串转义替换，模板中 MUST 不再为它加引号。后端 MUST 根据实际采集数据验证指标和标签存在，不允许只凭仪表盘名称猜测。

`trace_id` 是可选过滤器。当前 Jaeger adapter 在有限的 service+time 搜索结果内过滤，不能保证找回窗口中任意指定 trace；它不是全库按 ID 检索接口。trace spans 可包含该 trace 的其他服务，以及搜索窗口边界之外的子 spans，MUST 保留关联语义。日志 trace_id 过滤只有在源日志提供字段时有效。

空结果 MUST 返回成功加 `records:[]`。不支持的信号、未知指标、未知实体、源系统不可达 MUST 分别报错，不得混为无数据。发生多 Pod/多容器日志查询失败时当前 provider 整体报错，不静默丢弃部分结果。`truncated:true` 表示命中容量上界或已确认截断；达到上界时可以保守标记。v1 无翻页，调用方 SHOULD 缩小时间窗。数据可见性受采样、导出延迟和源系统保留策略限制。

### 动作与实验

```json
{
  "action": "cpu.load",
  "service": "product-catalog",
  "parameters": {"percent": 5},
  "duration_seconds": 10,
  "dry_run": true
}
```

- `dry_run` 默认 true，MUST 只验证参数与解析作用目标，不能创建故障资源。预览绑定当前 Pod 名与 UID；当前执行请求会重新解析自己的计划，预览不是之后执行的锁定令牌。
- `dry_run:false` MUST 提供 `Idempotency-Key`，同一 target 同键同请求返回原记录，不得再次注入；同键异参返回 409。
- Provider MUST 校验动作白名单和参数 schema，不得透传任意 CLI/matcher。调用方不可覆盖 namespace/context/Pod selector。
- 当前 ChaosBlade provider 每次确定性选择服务名字典序第一个 Running Pod；执行前 MUST 再校验 UID。单 target 最多一个未恢复实验，持续时间 1..300 秒。
- 实验 ID MUST 在后端创建前持久化，并用作稳定后端资源名。超时或连接中断造成结果不确定时 MUST 保留 `unknown` 和可恢复记录，不得自动重复创建。
- `failed` 不是已恢复状态。MUST 保留源系统状态与错误，并允许撤销。DELETE 可返回 `cancelling`；只有后端删除/恢复被确认才返回 `cancelled`。
- 持续故障到期 MUST 触发恢复，恢复失败 MUST 保留错误并重试。当前实现通过单进程 SQLite + reconciler 实现；截止时间为执行接受时间加 duration，包含后端准备延迟，不保证实际生效持续相同时长。
- **当前 ChaosBlade adapter 不提供独立于 API 的硬 TTL。** API 停机、不可达、operator 失效时恢复可能延迟；API 重启会继续撤销，亦可停机运行 `scripts/recover.py`。不得宣称 API 崩溃期间仍可准时恢复。要求硬 TTL 的后端 SHOULD 另行实现后端自恢复或独立控制器。
- `pod.kill` 为一次性删除，`reversible:false`；duration 仅控制实验资源清理，不能撤销已发生的删除。业务恢复依靠工作负载控制器创建替代 Pod；不保证保留本地状态。

状态包括 `creating,pending,running,failed,unknown,cancelling,cancelled,completed`。后端接受不等于已生效。`completed` 表示后端明确完成恢复，`cancelled` 表示清理确认；二者都不单独证明业务性能恢复。消费方 SHOULD 通过查询信号和应用检查验证业务恢复。

### 错误与认证

统一错误体为 `{"error":{"code":"...","message":"..."}}`。401：认证失败；404：target/entity/experiment 不存在；409：幂等键冲突、实验冲突、UID 变化、资源所有权冲突；422：参数/不支持能力错误；502：后端请求失败或结果不确定。插件 SHOULD 抛出 `APIError`，不能泄漏密钥。

默认监听 loopback。配置 `DEMO_API_TOKEN` 后所有业务路由要求 `Authorization: Bearer ...`；CLI 在非 loopback 绑定时要求 token。Swagger/OpenAPI 文档由框架公开，不包含配置中的密钥。v1 不提供多租户授权或任意 shell。后端连接信息属于可信部署配置，不接受来自查询请求的地址。

## 3. 插件契约

插件分成三个独立角色，可分别替换；factory 为 `factory(config: dict, context: dict) -> provider`。调用同步执行，HTTP 服务将阻塞 I/O 放到线程池。插件 SHOULD 为外部调用设置超时并支持并发读取。

| 角色 | 接口 | 契约 |
| --- | --- | --- |
| Discovery | `entities,relations,resolve` | 枚举实体/关系；resolve(service) 返回实例；找不到报 404 |
| Telemetry | `describe,query` | describe 自描述能力；query 返回 Observation |
| Actuator | `describe,plan,apply,status,cancel` | plan 无副作用；apply 使用传入稳定 ID；status/cancel 不扩大范围；cancel 幂等 |

通用 context 含 `target_id`；telemetry/actuator 还可读取 `discovery`。内置 Kubernetes 数据源另外使用 discovery.kube 和 discovery.telemetry_name(service)；其他基础设施的插件 SHOULD 使用自己的连接，不能假设系统总有 kubectl。Jaeger/Prometheus/OpenSearch 配置 `url` 时不需要 Kubernetes 转发，但仍使用 discovery.telemetry_name 做服务名映射。

插件安装示例（第三方 Python 包的 pyproject.toml）：

```toml
[project.entry-points."demo_api.providers"]
compose-discovery = "my_demo.discovery:ComposeDiscovery"
loki = "my_demo.logs:Loki"
chaos-mesh = "my_demo.actions:ChaosMesh"
```

配置 `plugin: loki` 后，核心通过 entry point 加载，不需要修改路由或内置注册表。提供配置即可复用已有 Kubernetes/Jaeger/Prometheus/日志/ChaosBlade provider 的 demo，不需要编写新插件。`backend-template.yaml` 是这一接入路径的模板。

## 4. Agent 接入流程与验收

实现 agent MUST 按以下步骤交付：

1. 调查给定系统的版本、部署方式、作用域、服务标签、实例身份、遥测 endpoint、时间单位、service.name、日志字段和现有混沌工具。只读取指定系统，不复用其他环境的隐含结论。
2. 生成目标 YAML；缺失能力如实省略。尽量组合现有 provider；只有协议或基础设施不同才新增插件。补充源系统配置和部署脚本，明确固定版本和依赖。
3. 导出配置 JSON Schema 和 OpenAPI；为新增插件编写真实边界测试，至少验证字段/单位转换、范围隔离、后端失败。动作还需验证幂等、未知结果、撤销和恢复，不得只测试调用自身的模拟实现。
4. 产生一次真实业务请求，再用统一接口查到本次产生的 trace 或其他信号。对每个声称支持的信号执行查询；明确区分非空数据验证与空结果协议验证。建议执行 `scripts/conformance.py --require-records metrics traces`。
5. 只在明确授权的环境执行实际故障。至少验证一个实际注入的后端状态、实际目标身份、撤销确认、业务恢复；记录未验证或不兼容动作。操作系统/容器运行时兼容性属于验收内容，Pod Ready 不能作为注入成功证据。
6. 交付说明 MUST 含配置、部署/启动/恢复命令、版本、测试结果、限制。不得宣称未运行过的第二个 demo 已接入成功。

当前协议刻意不承诺自动推断指标、完整服务调用图、跨后端查询优化、所有微服务 demo 开箱即用，也不包含实验调度、根因分析或 agent 本身。后续添加 Loki、Tempo、Docker Compose、Chaos Mesh 等后端时，应保持上述 REST 契约和实体身份语义。
