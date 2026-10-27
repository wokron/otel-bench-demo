#!/usr/bin/env bash
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
profile=${PROFILE:-minikube}
namespace=${CHAOS_NAMESPACE:-chaosblade}
helm_bin=${HELM:-helm}
version=1.7.4
cache="$root/.cache/chaosblade"
chart="$cache/chaosblade-operator-$version/deploy/helm/chaosblade-operator"
case "${1:-help}" in
  fetch|render|install)
    if [[ ! -f "$chart/Chart.yaml" ]]; then
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
    fi
    ;;
  status)
    kubectl --context="$profile" -n "$namespace" get pods,deploy,ds
    kubectl --context="$profile" get chaosblades.chaosblade.io
    ;;
  *) echo 'Usage: scripts/chaosblade.sh fetch|render|install|status';;
esac
