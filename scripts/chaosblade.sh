#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
profile=${PROFILE:-minikube}
namespace=${CHAOS_NAMESPACE:-chaosblade}
helm_bin=${HELM:-helm}
version=1.7.4
command_name=${1:-help}
cache="$root/.cache/chaosblade"
chart="$cache/chaosblade-operator-$version/deploy/helm/chaosblade-operator"
need() { command -v "$1" >/dev/null || { echo "Required command missing: $1" >&2; exit 1; }; }
check() {
    need kubectl
    kubectl --context="$profile" get crd chaosblades.chaosblade.io
    kubectl --context="$profile" -n "$namespace" rollout status deployment/chaosblade-operator --timeout="${TIMEOUT:-120s}"
    kubectl --context="$profile" -n "$namespace" rollout status daemonset/chaosblade-tool --timeout="${TIMEOUT:-120s}"
    kubectl --context="$profile" get nodes -o custom-columns=NAME:.metadata.name,RUNTIME:.status.nodeInfo.containerRuntimeVersion
    echo 'Pod deletion uses the Kubernetes API. CPU/network actions also require a compatible container runtime.'
    echo 'ChaosBlade exec-cri 1.7.4 uses Docker API 1.24; Docker 29 rejects this version. Pod readiness does not prove CPU/network injection works.'
}
case "$command_name" in
  fetch|render|install)
    if [[ "$command_name" != fetch ]]; then need "$helm_bin"; fi
    if [[ "$command_name" == install ]]; then need kubectl; fi
    if [[ ! -f "$chart/Chart.yaml" ]]; then
      need curl
      need tar
      mkdir -p "$cache"
      curl --fail --location --retry 3 --max-time 180 \
        "https://codeload.github.com/chaosblade-io/chaosblade-operator/tar.gz/refs/tags/v$version" \
        -o "$cache/source.tar.gz.part"
      tar -xzf "$cache/source.tar.gz.part" -C "$cache"
      rm "$cache/source.tar.gz.part"
    fi
    # Upstream v1.7.4 source embeds a 1.7.3 chart. Pin actual images explicitly.
    values=(--set operator.version=1.7.4 --set blade.version=1.7.4 --set webhook.enable=false)
    if [[ ${1} == render ]]; then
      "$helm_bin" template chaosblade "$chart" --namespace "$namespace" --include-crds "${values[@]}"
    elif [[ ${1} == install ]]; then
      "$helm_bin" upgrade --install chaosblade "$chart" --kube-context "$profile" \
        --namespace "$namespace" --create-namespace "${values[@]}" --wait --timeout "${TIMEOUT:-5m}"
      kubectl --context="$profile" -n "$namespace" rollout status daemonset/chaosblade-tool --timeout=120s
      check
    fi
    ;;
  status)
    kubectl --context="$profile" -n "$namespace" get pods,deploy,ds
    kubectl --context="$profile" get chaosblades.chaosblade.io
    ;;
  check) check ;;
  help|-h|--help) echo 'Usage: bash scripts/chaosblade.sh fetch|render|install|check|status' ;;
  *) echo "Unknown command: $command_name" >&2; exit 1 ;;
esac
