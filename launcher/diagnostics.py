"""Bounded deployment logs and commands, with credentials removed before display."""

import json
import re
import shlex

import compose_backend
import kubernetes_backend as kubernetes
import settings


def redact(text):
    for value in (settings.HF_TOKEN, settings.LITELLM_MASTER_KEY, settings.LAUNCHER_PASSWORD):
        if value:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"(?im)^.*(?:authorization|api[_ -]?key|password|secret|hf_token|hugging_face_hub_token).*$", "[credential detail omitted]", text)
    text = re.sub(r"\b(?:sk-|hf_)[A-Za-z0-9_-]{12,}", "[redacted]", text)
    text = re.sub(r"https?://[^\s<>\"']+", "[address]", text)
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[address]", text)
    return text[-24000:]


def inspect(model):
    name = model["name"]
    config = {key: model.get(key) for key in ("served_model", "replicas", "gpus_per_replica", "context", "simulate")}
    config["engine"] = "simulator" if model["simulate"] else "vLLM"
    logs = []
    error = ""
    if settings.MODE == "kubernetes":
        ns = settings.HELM_NAMESPACE
        commands = [shlex.join(["kubectl", "logs", "-n", ns, "-l", f"model={name}", "-c", "vllm", "--tail=200", "--prefix"]),
                    shlex.join(["kubectl", "describe", "pods", "-n", ns, "-l", f"model={name}"]),
                    shlex.join(["helm", "get", "values", name, "-n", ns, "-o", "yaml"])]
        note = "Run these with kubectl connected to this cluster. Save Helm values to a file and use helm upgrade with that file for advanced changes. The launcher reads the resulting deployment."
    else:
        commands = ["docker logs --tail 200 vllm-0", "docker compose -f compose/docker-compose.yaml ps"]
        note = "Run these on the Docker host from your Inference Stack installation folder. Set launcher environment variables in compose/docker-compose.yaml, recreate the launcher, then redeploy the model."
    try:
        if settings.MODE == "kubernetes":
            pods = kubernetes.api("GET", kubernetes.resource_path("pods"), params={"labelSelector": f"model={name}"})["items"]
            for index, pod in enumerate(pods[:8]):
                logs.append(f"Replica {index + 1}: {pod.get('status', {}).get('phase', 'Pending')}")
                for container in pod.get("status", {}).get("containerStatuses", []):
                    for state in container.get("state", {}).values():
                        if state.get("reason"):
                            logs.append(state["reason"] + ": " + state.get("message", ""))
                try:
                    logs.append(kubernetes.api("GET", kubernetes.resource_path(f"pods/{pod['metadata']['name']}/log"),
                                               raw=True, params={"container": "vllm", "tailLines": 150, "limitBytes": 16000}))
                except Exception:
                    logs.append("Logs are not available yet.")
        else:
            for index, container in enumerate(compose_backend._model_servers()[:8]):
                if container.labels.get(compose_backend.MODEL_LABEL) == model["served_model"]:
                    logs.append(f"Replica {index + 1}: {container.status}\n" + container.logs(tail=150).decode(errors="replace"))
    except Exception:
        error = "Could not read deployment logs. Use the commands below on the host or cluster."
    return {"logs": redact("\n\n".join(logs)) or "No model logs yet.", "error": error,
            "configuration": json.dumps(config, indent=2), "commands": "\n\n".join(commands), "note": note}
