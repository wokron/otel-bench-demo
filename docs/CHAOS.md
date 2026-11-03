# 在 otel-demo 中安装并通过 API 使用 ChaosBlade

ChaosBlade 是集群级故障执行框架，安装到独立的 `chaosblade` namespace；实验通过 `names` + `namespace` 精确指向 otel-demo 的单个 Pod。无需把二进制塞进各业务容器。当前 API 已配置 ChaosBlade provider，安装后即可使用统一 experiments 接口。

## 安装与检查

在 `impl` 目录执行，需要可用的 kubectl、Helm 和已启动的目标 Kubernetes 集群：

```bash
# 若当前 minikube 未启动
minikube start -p minikube --keep-context

# 安装/升级与检查：catalog/full 均可复用同一个集群级安装
bash otel-demo/demo.sh chaos-install catalog
bash otel-demo/demo.sh chaos-check catalog
```

等效的独立入口是 `bash scripts/chaosblade.sh install` 和 `bash scripts/chaosblade.sh check`。脚本固定 operator/tool 为 1.7.4，启用节点工具，关闭 webhook；安装包括 CRD、集群级 RBAC 和特权 DaemonSet。

`PROFILE` 指定 Kubernetes context，`CHAOS_NAMESPACE` 指定工具安装 namespace，`HELM` 可指定 Helm 路径，`TIMEOUT` 控制等待时间。工具 namespace 与业务 namespace 是两个概念；更换部署环境时还需同步调整 API 配置中的 discovery.context/namespace。

例如本机未将 Helm 加入 PATH 时：

```bash
HELM="$PWD/.cache/tools/linux-amd64/helm" bash otel-demo/demo.sh chaos-install catalog
```

脚本提供 `fetch/render/install/check/status`；render 不连接集群。check 验证 CRD、Deployment 和 DaemonSet rollout，并显示节点运行时。CPU/网络是否实际生效仍需注入验收，不能仅凭工具 Ready 判定。

## 通过统一 API 注入

```bash
uv run demo-api --config configs/otel-catalog-metrics.yaml
```

现有 catalog、catalog-metrics 和 full 配置均注册了 `pod.kill`、`cpu.load`、`network.delay`。用 capabilities 查看参数与限制：

```bash
curl -s http://127.0.0.1:8000/v1/targets/otel-demo/capabilities

# 默认只预览
curl -s -X POST http://127.0.0.1:8000/v1/targets/otel-demo/experiments \
  -H 'Content-Type: application/json' \
  -d '{"action":"pod.kill","service":"product-catalog","duration_seconds":30}'
```

执行、状态和撤销：

```bash
curl -s -X POST http://127.0.0.1:8000/v1/targets/otel-demo/experiments \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: my-pod-fault-001' \
  -d '{"action":"pod.kill","service":"product-catalog","duration_seconds":30,"dry_run":false}'

# 替换成上一步返回的 ID
curl -s http://127.0.0.1:8000/v1/targets/otel-demo/experiments/da-REPLACE
curl -s -X DELETE http://127.0.0.1:8000/v1/targets/otel-demo/experiments/da-REPLACE
```

状态需从 pending 进入 running 才说明后端报告已注入；检查 backend_status 获取每个目标的执行详情。撤销可返回 cancelling，之后查询到 cancelled 才表示资源清理确认。Pod 删除不可逆，业务恢复由 Deployment 重建实现。其他持续故障由 operator 在删除 CR 时恢复。

## 可复用的实际故障验收脚本

下面的命令会真实删除一个业务 Pod；脚本会预览、生成唯一幂等键、执行、等待 running，并在成功或后端失败时都请求清理，等待清理确认：

```bash
uv run python scripts/chaos_smoke.py --execute \
  --target otel-demo --service product-catalog --action pod.kill

# 后续确认业务恢复；check catalog 需要 grpcurl
bash otel-demo/demo.sh check catalog
```

认证通过环境变量 `DEMO_API_TOKEN` 传递。可指定 `--url`、`--duration`、`--timeout`。CPU 验收默认 percent=5，网络验收默认 milliseconds=100；两者都要求兼容运行时。创建请求在服务器返回 ID 前断开时可能结果不确定，应检查实验列表并按 ID 恢复，不能仅凭脚本报错断言未注入。

## 当前运行时限制与恢复

本机 minikube 使用 Docker 29.2.1，最低 API 1.44。ChaosBlade exec-cri 1.7.4 固定请求 API 1.24，因此 CPU 故障此前返回 `client version 1.24 is too old`；网络动作也不能假定可用。当前已实测成功的操作为 Kubernetes API 驱动的 Pod 删除。没有修改 Docker daemon 或放宽 API 下限。需要 CPU/网络实际注入时，应使用兼容运行时或另行构建修复版本，不能用设置 `DOCKER_API_VERSION` 规避固定版本代码。

API 的 SQLite 保留故障意图、期限和错误，运行时 reconciler 到期请求恢复；API 重启后继续恢复。当前没有独立于 API 的硬 TTL。API 停止后可运行：

```bash
uv run python scripts/recover.py --config configs/otel-catalog-metrics.yaml
kubectl --context=minikube get chaosblades.chaosblade.io -l demo-api-target=otel-demo
```

不移除 operator finalizer 来强制清理；资源消失后仍需业务验收。协议详细语义见 [SPEC](SPEC.md)，完整架构和用法见 [API 文档](API.md)。
