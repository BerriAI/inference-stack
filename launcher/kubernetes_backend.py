"""Operate this installation's namespace using its service account and Helm."""

import json
import os
import shlex
import ssl
import subprocess
import time
from functools import lru_cache
from pathlib import Path

import httpx
import yaml

import litellm_client
import settings

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def api(method: str, path: str, raw=False, **kwargs):
    context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    # Read the projected token for each request so rotation works.
    token = (SERVICE_ACCOUNT / "token").read_text().strip()
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
    with httpx.Client(base_url=f"https://{host}:{port}", verify=context, timeout=10,
                      headers={"Authorization": f"Bearer {token}"}) as client:
        response = client.request(method, path, **kwargs).raise_for_status()
        return response.text if raw else response.json()


def resource_path(resource: str, group: str = "") -> str:
    prefix = f"/apis/{group}/v1" if group else "/api/v1"
    return f"{prefix}/namespaces/{settings.HELM_NAMESPACE}/{resource}"


@lru_cache(maxsize=2)
def capabilities(bucket: int) -> dict:
    try:
        result = api("POST", "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews", json={
            "apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectRulesReview",
            "spec": {"namespace": settings.HELM_NAMESPACE}})
        rules = result.get("status", {}).get("resourceRules", [])
        def permits(verb, resource, group=""):
            return any(not rule.get("resourceNames") and
                       (verb in rule["verbs"] or "*" in rule["verbs"]) and
                       (group in rule["apiGroups"] or "*" in rule["apiGroups"]) and
                       (resource in rule["resources"] or "*" in rule["resources"]) for rule in rules)
        read = permits("list", "deployments", "apps")
        resources = [("deployments", "apps"), ("jobs", "batch"), ("secrets", ""),
                     ("services", ""), ("configmaps", ""), ("serviceaccounts", ""),
                     ("roles", "rbac.authorization.k8s.io"), ("rolebindings", "rbac.authorization.k8s.io")]
        direct = read and all(permits(verb, resource, group) for resource, group in resources
                              for verb in ("get", "list", "watch", "create", "update", "patch", "delete"))
        direct = direct and all(permits(verb, "pods") for verb in ("get", "list", "watch"))
        return {"read": read, "deploy": direct, "error": ""}
    except Exception:
        return {"read": False, "deploy": False, "error": "Could not check Kubernetes permissions. You can still generate a deployment command."}


def permissions() -> dict:
    return capabilities(int(time.monotonic() // 60))


def gpu_inventory(exclude_model: str = "") -> dict:
    try:
        nodes = api("GET", "/api/v1/nodes")["items"]
        pods = api("GET", "/api/v1/pods")["items"]
        occupied = {}
        for pod in pods:
            if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            if exclude_model and pod.get("metadata", {}).get("namespace") == settings.HELM_NAMESPACE and pod.get("metadata", {}).get("labels", {}).get("model") == exclude_model:
                continue
            spec = pod["spec"]
            def gpu(container):
                resources = container.get("resources", {})
                return int(resources.get("requests", resources.get("limits", {})).get("nvidia.com/gpu", 0))
            count = max(sum(gpu(c) for c in spec.get("containers", [])),
                        max((gpu(c) for c in spec.get("initContainers", [])), default=0))
            node = spec.get("nodeName")
            occupied[node] = occupied.get(node, 0) + count
        devices = []
        for node in nodes:
            if node.get("spec", {}).get("unschedulable"):
                continue
            if not any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"].get("conditions", [])):
                continue
            if any(t.get("effect") in ("NoSchedule", "NoExecute") and t.get("key") != "nvidia.com/gpu" for t in node.get("spec", {}).get("taints", [])):
                continue
            count = int(node["status"].get("allocatable", {}).get("nvidia.com/gpu", 0))
            if not count:
                continue
            labels = node["metadata"].get("labels", {})
            memory = labels.get("nvidia.com/gpu.memory")
            devices.append({"name": labels.get("nvidia.com/gpu.product", "NVIDIA GPU"), "count": count,
                            "instance_type": labels.get("node.kubernetes.io/instance-type", ""),
                            "available": max(0, count - occupied.get(node["metadata"]["name"], 0)),
                            "memory_gb": round(float(memory) / 1024, 1) if memory else None})
        return {"count": sum(d["count"] for d in devices), "devices": devices, "error": ""}
    except Exception:
        return {"count": None, "devices": [], "error": "GPU capacity is unknown. Node and pod read access is needed to check allocation."}


def legacy_model(row: dict, registered: list[dict]) -> dict | None:
    """Recognize old chart releases through this stack's LiteLLM registration."""
    metadata = row["metadata"]
    annotations = metadata.get("annotations", {})
    name = annotations.get("meta.helm.sh/release-name")
    if (not name or metadata["name"] != f"{name}-vllm"
            or metadata.get("labels", {}).get("app.kubernetes.io/managed-by") != "Helm"
            or annotations.get("meta.helm.sh/release-namespace") != settings.HELM_NAMESPACE
            or row["spec"].get("selector", {}).get("matchLabels", {}).get("model") != name):
        return None
    container = next((c for c in row["spec"]["template"]["spec"]["containers"] if c["name"] == "vllm"), {})
    command = container.get("command", [])
    args = container.get("args", [])
    simulate = len(args) >= 2 and args[0] == "--model"
    if simulate:
        model = args[1]
    elif len(command) >= 3 and command[:2] == ["vllm", "serve"]:
        model = command[2]
    else:
        return None
    if not any(m["name"] == name and m["served_model"] == model
               and m.get("api_base") == f"http://{name}-epp:8081/v1" for m in registered):
        return None
    return {"name": name, "served_model": model, "simulate": simulate, "context": None,
            "gpus_per_replica": int(container.get("resources", {}).get("limits", {}).get("nvidia.com/gpu", 1))}


def models(registered: list[dict] | None = None) -> list[dict]:
    rows = api("GET", resource_path("deployments", "apps"))["items"]
    result = []
    by_name = {row["metadata"]["name"]: row for row in rows}
    for row in rows:
        metadata = row["metadata"]
        labels = metadata.get("labels", {})
        stack = labels.get("inference-stack/stack")
        if stack and stack != settings.HELM_STACK_RELEASE:
            continue
        annotations = metadata.get("annotations", {})
        if stack:
            model = {"name": labels["inference-stack/model"], "served_model": annotations["inference-stack/model-id"],
                     "gpus_per_replica": int(annotations.get("inference-stack/gpus", 1)),
                     "context": int(annotations.get("inference-stack/context", 4096)),
                     "simulate": annotations.get("inference-stack/simulate") == "true"}
        else:
            if not metadata["name"].endswith("-vllm"):
                continue
            if registered is None:
                try:
                    registered = litellm_client.list_models()
                except httpx.HTTPError:
                    registered = []
            model = legacy_model(row, registered)
            if not model:
                continue
        name = model["name"]
        spec = row["spec"]
        router = by_name.get(f"{name}-epp", {})
        observed = row.get("status", {}).get("observedGeneration", 0) >= metadata.get("generation", 1)
        ready = row.get("status", {}).get("readyReplicas", 0) if observed else 0
        result.append({**model,
                       "replicas": spec.get("replicas", 1), "ready": ready,
                       "router_ready": router.get("status", {}).get("readyReplicas", 0) > 0})
    return result


def values(request, name: str) -> dict:
    return {"model": request.model, "replicas": request.replicas, "gpusPerReplica": request.gpus_per_replica,
            "maxModelLen": request.context, "simulate": request.simulate,
            "minGpuMemoryMiB": request.min_gpu_memory_mib,
            "nodeSelector": {"node.kubernetes.io/instance-type": request.instance_type} if request.add_gpu else {},
            "huggingFaceSecret": settings.HF_SECRET, "stackRelease": settings.HELM_STACK_RELEASE,
            "llm-d-router-standalone": {"router": {"modelServers": {"matchLabels": {"model": name}},
                                                    "proxy": {"presets": {"envoy": {"configMap": {"name": f"{name}-envoy"}}}}}}}


def command(request, name: str) -> str:
    args = ["helm", "upgrade", "--install", name, settings.HELM_MODEL_CHART,
            "--namespace", settings.HELM_NAMESPACE, "--reuse-values", "--wait", "--timeout", "60m"]
    for key, value in values(request, name).items():
        args += ["--set-json", f"{key}={json.dumps(value, separators=(',', ':'))}"]
    return f"helm dependency build {shlex.quote(settings.HELM_MODEL_CHART)}\n" + shlex.join(args)


def helm(args: list[str], values_input: dict | None = None) -> str:
    result = subprocess.run(["helm", *args, "--namespace", settings.HELM_NAMESPACE],
                            input=json.dumps(values_input) if values_input is not None else None,
                            capture_output=True, text=True, timeout=3700)
    if result.returncode:
        # Helm errors may contain rendered Secrets or infrastructure details.
        raise RuntimeError("Helm could not finish. Check the release and pod events in this namespace, then retry.")
    return result.stdout


def deploy(request, name: str) -> None:
    helm(["upgrade", "--install", name, settings.BUNDLED_MODEL_CHART, "--reuse-values", "--values", "-",
          "--wait", "--timeout", "60m", "--history-max", "5"], values(request, name))


def remove(name: str) -> None:
    helm(["uninstall", name, "--ignore-not-found", "--wait", "--timeout", "5m"])


def routing_config(name: str) -> dict:
    row = api("GET", resource_path(f"configmaps/{name}-epp"))
    return yaml.safe_load(row["data"]["default-plugins.yaml"])


def save_routing(name: str, config: dict) -> None:
    # Merge into saved Helm values, preserving the rest of the routing policy.
    helm(["upgrade", name, settings.BUNDLED_MODEL_CHART, "--reuse-values", "--values", "-",
          "--wait", "--timeout", "5m", "--history-max", "5"],
         {"llm-d-router-standalone": {"router": {"epp": {"pluginsConfig": config}}}})
