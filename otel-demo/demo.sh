#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command_name=${1:-help}
mode=${2:-catalog}
profile=${PROFILE:-minikube}
namespace=${NAMESPACE:-otel-demo}
release=${RELEASE:-otel-demo}
helm_bin=${HELM:-helm}
chart_version=0.41.0
cache_dir=${CACHE_DIR:-"$script_dir/../.cache/otel-demo"}
chart="$cache_dir/opentelemetry-demo-$chart_version.tgz"

fail() { echo "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || fail "Required command missing: $1"; }
kube() { kubectl --context="$profile" --namespace="$namespace" "$@"; }
helm_cmd() { "$helm_bin" --kube-context "$profile" --namespace "$namespace" "$@"; }

case "$mode" in full|catalog) ;; *) fail 'Mode must be full or catalog' ;; esac
values="$script_dir/values-$mode.yaml"
value_args=(--values "$values")
if [[ ${OBSERVABILITY:-0} == 1 ]]; then
    [[ "$mode" == catalog ]] || fail 'OBSERVABILITY=1 overlay is for catalog only'
    value_args+=(--values "$script_dir/values-observability.yaml")
fi

fetch() {
    need curl
    mkdir -p "$cache_dir"
    if [[ ! -s "$chart" ]]; then
        curl --fail --location --retry 3 --connect-timeout 15 --max-time 180 \
            "https://github.com/open-telemetry/opentelemetry-helm-charts/releases/download/opentelemetry-demo-$chart_version/opentelemetry-demo-$chart_version.tgz" \
            -o "$chart.part"
        mv "$chart.part" "$chart"
    fi
}

case "$command_name" in
    help|-h|--help)
        cat <<'USAGE'
Usage: bash otel-demo/demo.sh COMMAND [catalog|full]
  start    Start minikube (catalog: 4 CPUs/6144 MiB; full: 6 CPUs/12288 MiB)
  render   Render pinned Helm chart without contacting Kubernetes
  deploy   Install/upgrade and wait for readiness (TIMEOUT=15m)
  status   Show workloads and recent events
  check    Verify application requests and stored traces (full also checks logs)
  chaos-install  Install/upgrade ChaosBlade in this cluster
  chaos-check    Check ChaosBlade readiness and runtime compatibility
  forward  Expose catalog gRPC + Jaeger, or full frontend proxy, on localhost
  delete   Uninstall this Helm release; keep minikube and namespace
Environment: PROFILE, NAMESPACE, RELEASE, HELM, CACHE_DIR, GRPCURL,
             CPUS, MEMORY, TIMEOUT, LOCAL_PORT, GRPC_PORT, JAEGER_PORT, OBSERVABILITY
USAGE
        ;;
    start)
        need minikube
        default_cpus=4
        default_memory=6144
        if [[ "$mode" == full ]]; then
            default_cpus=6
            default_memory=12288
        fi
        minikube start -p "$profile" --driver=docker --cpus="${CPUS:-$default_cpus}" \
            --memory="${MEMORY:-$default_memory}" --keep-context
        ;;
    render)
        need "$helm_bin"
        fetch
        helm_cmd template "$release" "$chart" "${value_args[@]}"
        ;;
    deploy)
        need "$helm_bin"
        need kubectl
        fetch
        helm_cmd upgrade --install "$release" "$chart" --create-namespace \
            --reset-values "${value_args[@]}" --wait --timeout "${TIMEOUT:-15m}"
        ;;
    status)
        need kubectl
        kube get deploy,daemonset,statefulset,pods,svc
        kube get events --sort-by=.lastTimestamp
        ;;
    check)
        need python3
        need kubectl
        exec python3 "$script_dir/check.py" "$mode" "$profile" "$namespace"
        ;;
    chaos-install|chaos-check)
        chaos_command=install
        if [[ "$command_name" == chaos-check ]]; then chaos_command=check; fi
        exec bash "$script_dir/../scripts/chaosblade.sh" "$chaos_command"
        ;;
    forward)
        need kubectl
        if [[ "$mode" == full ]]; then
            exec kubectl --context="$profile" -n "$namespace" port-forward \
                --address=127.0.0.1 svc/frontend-proxy "${LOCAL_PORT:-8080}:8080"
        fi
        kube port-forward --address=127.0.0.1 svc/product-catalog "${GRPC_PORT:-18080}:8080" &
        catalog_pid=$!
        trap 'kill "$catalog_pid" 2>/dev/null || true' EXIT
        kube port-forward --address=127.0.0.1 svc/jaeger "${JAEGER_PORT:-16686}:16686" &
        jaeger_pid=$!
        trap 'kill "$catalog_pid" "$jaeger_pid" 2>/dev/null || true' EXIT
        wait -n "$catalog_pid" "$jaeger_pid"
        ;;
    delete)
        need "$helm_bin"
        helm_cmd uninstall "$release" --wait --timeout "${TIMEOUT:-15m}"
        ;;
    *) fail "Unknown command: $command_name (see help)" ;;
esac
