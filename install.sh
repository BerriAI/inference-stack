#!/usr/bin/env bash
set -euo pipefail

VERSION=0.1.1
RELEASE="https://github.com/BerriAI/inference-stack/releases/download/v$VERSION"
mode="${1:-}"
case "$mode" in
  docker|kubernetes) ;;
  *) printf 'Usage: bash install.sh docker|kubernetes\n' >&2; exit 2 ;;
esac

need() { command -v "$1" >/dev/null || { printf 'Install %s, then run this command again.\n' "$1" >&2; exit 1; }; }
need openssl
if [[ "$mode" == docker ]]; then
  need docker
  docker compose version >/dev/null
  docker info >/dev/null 2>&1 || { printf 'Start Docker, then try again.\n' >&2; exit 1; }
else
  need helm
  need kubectl
  kubectl cluster-info >/dev/null 2>&1 || { printf 'Connect kubectl to your cluster, then try again.\n' >&2; exit 1; }
fi

temporary=''
forwards=()
cleanup() {
  if (( ${#forwards[@]} )); then
    for pid in "${forwards[@]}"; do kill "$pid" 2>/dev/null || true; done
  fi
  if [[ -n "$temporary" ]]; then rm -rf -- "$temporary"; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-/dev/null}")" && pwd)"
if [[ -f "$script_dir/compose/docker-compose.yaml" ]]; then
  root="$script_dir"
else
  root="${INFERENCE_STACK_DIR:-$PWD/inference-stack}"
  if [[ ! -e "$root" ]]; then
    need curl
    need tar
    temporary="$(mktemp -d)"
    archive="inference-stack-$VERSION.tar.gz"
    curl --fail --silent --show-error --location "$RELEASE/$archive" -o "$temporary/$archive"
    curl --fail --silent --show-error --location "$RELEASE/SHA256SUMS" -o "$temporary/SHA256SUMS"
    expected="$(awk -v file="$archive" '$2 == file {print $1}' "$temporary/SHA256SUMS")"
    if command -v sha256sum >/dev/null; then
      actual="$(sha256sum "$temporary/$archive" | cut -d ' ' -f 1)"
    else
      need shasum
      actual="$(shasum -a 256 "$temporary/$archive" | cut -d ' ' -f 1)"
    fi
    [[ "$expected" =~ ^[a-f0-9]{64}$ && "$actual" == "$expected" ]] || { printf 'Download checksum did not match. Retry the installation.\n' >&2; exit 1; }
    mkdir "$temporary/unpacked"
    tar -xzf "$temporary/$archive" -C "$temporary/unpacked"
    extracted="$temporary/unpacked/inference-stack-$VERSION"
    [[ -f "$extracted/compose/docker-compose.yaml" && -f "$extracted/VERSION" ]] || { printf 'The release archive is incomplete.\n' >&2; exit 1; }
    mkdir -p -- "$(dirname -- "$root")"
    mv -- "$extracted" "$root"
  fi
  [[ -f "$root/compose/docker-compose.yaml" && -f "$root/VERSION" ]] || { printf 'The install directory already exists and is not an Inference Stack installation. Set INFERENCE_STACK_DIR to an empty location.\n' >&2; exit 1; }
fi

cd -- "$root"
version="$(cat VERSION)"
image="ghcr.io/berriai/inference-stack/launcher:$version"
if [[ -f launcher-image.txt ]]; then image="$(cat launcher-image.txt)"; fi

if [[ "$mode" == docker ]]; then
  cd compose
  if [[ ! -e .env ]]; then
    (umask 077; set -C; printf 'LITELLM_MASTER_KEY=sk-%s\nPOSTGRES_PASSWORD=%s\nINFERENCE_STACK_LAUNCHER_IMAGE=%s\n' \
      "$(openssl rand -hex 24)" "$(openssl rand -hex 24)" "$image" > .env)
  fi
  docker compose up -d --no-build --wait </dev/null
  printf '\nOpen http://localhost:8080\nSign in as admin with LITELLM_MASTER_KEY from compose/.env.\nChoose vLLM to deploy a model, or enable simulation in its Settings.\n'
else
  existing="$(helm list --namespace inference --deployed --failed --pending --uninstalled --superseded --uninstalling --filter '^inference-stack$' --short)"
  if [[ -n "$existing" ]]; then
    deployed="$(helm list --namespace inference --deployed --filter '^inference-stack$' --short)"
    [[ -n "$deployed" ]] || { printf 'The existing release needs recovery. Run helm status inference-stack -n inference before retrying; its credentials have been preserved.\n' >&2; exit 1; }
  else
    if [[ ! -d charts/inference-stack/charts ]]; then
      helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
      helm repo add grafana https://grafana.github.io/helm-charts
      helm dependency build charts/inference-stack
    fi
    helm install inference-stack charts/inference-stack --namespace inference --create-namespace \
      --wait --timeout 10m --values - <<VALUES
launcher:
  image: "$image"
litellm:
  postgresql:
    auth:
      password: "$(openssl rand -hex 24)"
      postgres-password: "$(openssl rand -hex 24)"
VALUES
  fi
  for mapping in inference-stack-launcher:8080:8080 inference-stack-litellm:4000:4000 inference-stack-grafana:3000:80; do
    service="${mapping%%:*}"
    ports="${mapping#*:}"
    kubectl port-forward --namespace inference "svc/$service" "$ports" >/dev/null 2>&1 &
    forwards+=("$!")
  done
  sleep 2
  for pid in "${forwards[@]}"; do
    kill -0 "$pid" 2>/dev/null || { printf 'Port forwarding failed. Check that ports 8080, 4000 and 3000 are free and your cluster permits port-forwarding.\n' >&2; exit 1; }
  done
  printf '\nOpen http://localhost:8080\nSign in as admin with the masterkey field of Secret inference-stack-litellm-masterkey in namespace inference.\nLeave this terminal open for the launcher and dashboards. Ctrl-C closes the connections; the stack keeps running.\n'
  wait
fi
