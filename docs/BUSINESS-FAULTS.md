# 业务故障接口

业务故障作为 `category: business` 的独立动作类型，通过已有 experiments HTTP 接口使用。当前实现使用 otel-demo 的 flagd，provider 为 `otel-demo-flags`；核心路由和状态机不依赖 otel-demo。基础设施动作标记为 `category: infrastructure`。

## 已实现动作

| action | service | parameters | 效果 |
| --- | --- | --- | --- |
| business.payment.failure | payment | percent: 10/25/50/75/100，默认 25 | 应用按比例模拟支付失败 |
| business.cart.failure | cart | 同上 | 应用按比例模拟购物车操作失败 |
| business.payment.unavailable | payment | {} | 应用模拟支付不可达，区别于真实网络隔离 |

比例按目标配置中的实际 variant 数值匹配，未定义的比例返回 422；不根据 variant 名字猜测数值。暂不支持带 targeting 的 flag、商品定向失败、内存泄漏或任意开关修改。请求只能选择上述动作及对应 service，不能覆盖 namespace、ConfigMap、flag 名称或执行命令。

`configs/otel-multi.yaml` 中，`otel-full-existing` 通过 composite 同时保留三个 ChaosBlade 动作并增加三个业务动作。catalog 的 `otel-demo` 没有 payment/cart，继续保留原来的基础设施动作，不发布不适用的业务能力。capabilities 的 services 字段供调用方筛选，category 不需要放入执行请求。

## 一次性准备 flagd

原 demo 使用 init container 将 ConfigMap 复制到 emptyDir，后续 UI 修改的是该运行文件。为获得 Kubernetes resourceVersion 并发校验，本实现改为让单副本 flagd 直接监听 ConfigMap 投影；flagd v0.11.1/v0.16.0 的文件监听器支持 Kubernetes symlink 更新，求值接口为 OFREP v1。

在 minikube 已启动后，从 impl 目录执行：

```bash
# 默认只打印准备后的资源，不修改集群。
uv run python scripts/business_flags_setup.py --namespace default

# 确认基线后执行：滚动更新 flagd Deployment，并在必要时暴露 Service 8016 端口。
uv run python scripts/business_flags_setup.py --namespace default \
  --apply --backup .state/flagd-default-before-business.json
kubectl --context=minikube -n default rollout status deployment/flagd --timeout=120s

uv run demo-api --config configs/otel-multi.yaml
```

准备脚本适用于本项目 otel-demo 的 `/etc/flagd/demo.flagd.json` 布局，支持 `--context/--namespace/--deployment/--service/--configmap`。拒绝多副本、subPath、未知 init-volume 用法及已有业务故障占用。备份文件以独占方式创建，避免覆盖；两次资源写入不是跨资源事务，失败时保留备份供检查，不能把脚本退出当成自动回滚。脚本中的 rollout 等待为 15 秒，超时后可用上面的独立命令继续检查。

**准备后 ConfigMap 成为权威配置，flagd-ui 共享挂载变为只读。** UI 中未同步回 ConfigMap 的临时修改不会被自动保留；应在准备前结束旧实验、核实 ConfigMap 是期望的基线。脚本备份原 Deployment、Service、ConfigMap，但不备份旧 emptyDir 文件。不要在仍有实验时用 Helm 升级覆盖这次配置；Helm 重新部署可能恢复原来的 emptyDir，届时新实验预览会拒绝执行，需重新检查准备脚本。需要撤回后端改造时，先确认所有实验已恢复，再对照备份恢复 Deployment/Service 的原 spec；不能直接 replace 带旧 resourceVersion 的备份覆盖并发修改。

未运行准备脚本时，业务故障预览返回 409 `business_backend_not_ready`；capabilities 中列出动作只表示配置支持。插件不会自行重启 flagd 来注入故障。

## 使用统一接口

预览不产生故障，返回原 flag、预期 flag、ConfigMap UID、服务、命名空间和延迟说明：

```bash
curl -s http://127.0.0.1:8000/v1/targets/otel-full-existing/capabilities

curl -s -X POST http://127.0.0.1:8000/v1/targets/otel-full-existing/experiments \
  -H 'Content-Type: application/json' \
  -d '{"action":"business.payment.failure","service":"payment","parameters":{"percent":25},"duration_seconds":300,"dry_run":true}'
```

真实执行、查询和恢复：

```bash
# 真实改变支付服务行为；每次新实验使用新的幂等键。
curl -s -X POST http://127.0.0.1:8000/v1/targets/otel-full-existing/experiments \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: payment-failure-example-001' \
  -d '{"action":"business.payment.failure","service":"payment","parameters":{"percent":25},"duration_seconds":300,"dry_run":false}'

# 替换为创建结果中的 ID。
curl -s http://127.0.0.1:8000/v1/targets/otel-full-existing/experiments/da-REPLACE
curl -s -X DELETE http://127.0.0.1:8000/v1/targets/otel-full-existing/experiments/da-REPLACE
```

ConfigMap 投影是异步的，可能超过一分钟。`pending` 包括等待投影和求值更新；`running` 表示 OFREP 返回期望的 variant 和 value，**不表示已经验证业务请求失败**。backend_status 显式提供 evaluation_confirmed 和 business_effect_verified=false。调用方需要产生支付/购物车流量，并查询 metrics/traces/logs 验证实际效果；样本不足时不能把观察到的比例当准确失败率。

`duration_seconds` 仍是接受请求起算的 1..300 秒，包含投影等待时间，建议首次验收使用 300 秒。到期会请求恢复，短实验可能在 flag 真正生效前就到期。不要把这一配置后端用于要求毫秒级生效的实验。

DELETE 后，插件先还原配置，再等待 OFREP 确认原始 variant/value；等待期间是 cancelling，确认后才 cancelled。业务恢复仍需重新请求和观测。API 停止期间没有独立硬 TTL；相同配置重启继续恢复，或在 API 停止后执行：

```bash
uv run python scripts/recover.py --config configs/otel-multi.yaml
```

也提供端到端的实际开关验收脚本，会预览、执行、等待求值确认、请求撤销并等待还原确认：

```bash
# 此命令真实改变业务行为；不自动发送支付交易。
uv run python scripts/business_smoke.py --execute \
  --target otel-full-existing --action business.payment.failure --percent 25
```

脚本通过 DEMO_API_TOKEN 读取认证令牌，默认 localhost:8000。若创建请求在返回 ID 前失败，需通过实验列表找回记录并清理，不能盲目换新键重试。后端不可达时清理可能尚未完成，应保留状态库并检查实验错误。

## 并发与恢复边界

- 计划和恢复快照在后端写入前存入 SQLite。即使后端写成功但响应丢失，仍能通过持久化的 plan 恢复。
- ConfigMap UID 固定；同名资源被替换时拒绝继续操作。写入使用 resourceVersion 校验，保留其他 flag 和无关配置。
- 每个 flag 使用实验所有权 annotation；恢复前检查所有权和整个 flag 内容。人工修改同一 flag 后返回 recovery_conflict，保留快照供处理，不覆盖人工修改。其他 flag 的更新会被保留。
- 恢复原 variant/规则，不统一恢复成 off。原来 50% 失败的 flag 在本实验结束后仍恢复为 50%。
- ConfigMap 内容和 OFREP 端点必须属于同一套 flagd；本实现支持单副本。手工改变后端挂载、求值 endpoint 或 target 配置可能使恢复无法确认。
- 组合 provider 保存子后端配置指纹；变更后端配置时会拒绝恢复并要求还原原配置。`legacy_provider: infrastructure` 专门承接之前的 ChaosBlade 历史记录，不能随意改指其他后端。

## 插件配置与验证状态

```yaml
actions:
  plugin: composite
  config:
    legacy_provider: infrastructure
    providers:
      infrastructure:
        plugin: chaosblade
        config:
          allowed_actions: [pod.kill, cpu.load, network.delay]
      business:
        plugin: otel-demo-flags
        config:
          configmap: flagd-config
          deployment: flagd
          allowed_actions: [business.payment.failure, business.cart.failure, business.payment.unavailable]
          evaluation:
            service: flagd
            port: 8016
            api_path: /ofrep/v1/evaluate/flags
```

组合层根据子 provider describe 的动作名称自动路由，拒绝重名与嵌套 composite。其他 demo 可以实现自己的业务 actuator，通过 entry point 注册；不需要改 HTTP 路由。单 target 一个未恢复实验的限制保持不变。

本次验证包括自动测试、HTTP TestClient 生命周期、响应丢失后重启恢复，以及实际 chart 0.41.0 的离线渲染/部署转换/三种动作计划。核对了 flagd v0.11.1/v0.16.0 的 OFREP 路径及文件监听源码。本次没有启动停止中的 minikube，没有对真实 deployment 执行准备脚本，也没有对真实业务注入；实际投影延迟、求值及业务效果仍需运行上述验收命令确认。
