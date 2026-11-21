import json
import os
import time

import docker
import httpx
import yaml

import settings

ROLE_LABEL = "inference-stack.role"
MODEL_LABEL = "inference-stack.model"
PORT = 8000


def _docker() -> docker.DockerClient:
    return docker.from_env()


def _model_servers() -> list:
    return _docker().containers.list(all=True, filters={"label": f"{ROLE_LABEL}=model-server"})


def _address(container) -> str:
    container.reload()
    return container.attrs["NetworkSettings"]["Networks"][settings.DOCKER_NETWORK]["IPAddress"]


def _write_atomically(filename: str, content: str) -> None:
    path = os.path.join(settings.STATE_DIR, filename)
    with open(f"{path}.tmp", "w") as file:
        file.write(content)
    os.replace(f"{path}.tmp", path)


def publish_endpoints() -> None:
    """Tell the llm-d router and Prometheus which model servers exist."""
    servers = [c for c in _model_servers() if c.status == "running"]
    endpoints = [{"name": c.name, "address": _address(c), "port": str(PORT)} for c in servers]
    targets = [
        {"targets": [f"{e['address']}:{PORT}"], "labels": {"pod": e["name"], "namespace": "compose"}}
        for e in endpoints
    ]
    _write_atomically("endpoints.yaml", yaml.safe_dump({"endpoints": endpoints}))
    _write_atomically("model-servers.json", json.dumps(targets))


def has_gpu() -> bool:
    return "nvidia" in _docker().info().get("Runtimes", {})


def running_model() -> dict | None:
    servers = _model_servers()
    if not servers:
        return None
    return {
        "served_model": servers[0].labels[MODEL_LABEL],
        "replicas": len(servers),
        "ready": sum(_is_ready(c) for c in servers),
    }


def _is_ready(container) -> bool:
    try:
        return httpx.get(f"http://{_address(container)}:{PORT}/health", timeout=2).status_code == 200
    except (httpx.HTTPError, KeyError, docker.errors.NotFound):
        return False


def remove_model_servers() -> None:
    for container in _model_servers():
        container.remove(force=True)
    publish_endpoints()


def start_model_servers(served_model: str, replicas: int, simulate: bool) -> None:
    remove_model_servers()
    for index in range(replicas):
        options = _simulator_options(served_model) if simulate else _vllm_options(served_model, index)
        _docker().containers.run(
            name=f"vllm-{index}",
            detach=True,
            network=settings.DOCKER_NETWORK,
            labels={ROLE_LABEL: "model-server", MODEL_LABEL: served_model},
            restart_policy={"Name": "unless-stopped"},
            **options,
        )


def _simulator_options(served_model: str) -> dict:
    return {
        "image": settings.SIMULATOR_IMAGE,
        "command": ["--model", served_model, "--port", str(PORT)],
    }


def _vllm_options(served_model: str, gpu_index: int) -> dict:
    return {
        "image": settings.VLLM_IMAGE,
        "entrypoint": "vllm",
        "command": ["serve", served_model, "--port", str(PORT)],
        "environment": {"HUGGING_FACE_HUB_TOKEN": settings.HF_TOKEN},
        "volumes": {"inference-stack-huggingface": {"bind": "/root/.cache/huggingface", "mode": "rw"}},
        "shm_size": "8g",
        "device_requests": [docker.types.DeviceRequest(device_ids=[str(gpu_index)], capabilities=[["gpu"]])],
    }


def wait_until_ready(timeout_seconds: int = 3600) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        servers = _model_servers()
        if any(c.status == "exited" for c in servers):
            raise RuntimeError("A model server exited. Run `docker logs vllm-0` to see why.")
        if servers and all(_is_ready(c) for c in servers):
            return
        time.sleep(2)
    raise RuntimeError("Model servers did not become ready in time.")
