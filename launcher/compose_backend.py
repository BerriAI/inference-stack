import json
import os
import socket
import subprocess
import time
from pathlib import Path

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
    return _docker().containers.list(all=True, filters={"label": f"{ROLE_LABEL}=model-server", "network": settings.DOCKER_NETWORK})


def _address(container) -> str:
    container.reload()
    return container.attrs["NetworkSettings"]["Networks"][settings.DOCKER_NETWORK]["IPAddress"]


def _write_atomically(filename: str, content: str) -> None:
    path = os.path.join(settings.STATE_DIR, filename)
    os.makedirs(settings.STATE_DIR, exist_ok=True)
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
        "gpus_per_replica": int(servers[0].labels.get("inference-stack.gpus", "1")),
        "context": int(servers[0].labels.get("inference-stack.context", "4096")),
        "simulate": servers[0].labels.get("inference-stack.simulate") == "true",
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


def pull_image(simulate: bool) -> None:
    """Download the model server image, raising the registry's own error if it fails."""
    image = settings.SIMULATOR_IMAGE if simulate else settings.VLLM_IMAGE
    if _docker().images.list(name=image):
        return
    for event in _docker().api.pull(image, stream=True, decode=True):
        if "error" in event:
            raise RuntimeError("Could not download the model server image. Check the image tag and registry access, then retry.")


def start_model_servers(served_model: str, replicas: int, simulate: bool, gpus: int = 1, context: int = 4096) -> None:
    remove_model_servers()
    for index in range(replicas):
        options = _simulator_options(served_model) if simulate else _vllm_options(served_model, index * gpus, gpus, context)
        _docker().containers.run(
            name=f"vllm-{index}",
            detach=True,
            network=settings.DOCKER_NETWORK,
            labels={ROLE_LABEL: "model-server", MODEL_LABEL: served_model, "inference-stack.gpus": str(gpus),
                    "inference-stack.context": str(context), "inference-stack.simulate": str(simulate).lower()},
            restart_policy={"Name": "unless-stopped"},
            **options,
        )


def _simulator_options(served_model: str) -> dict:
    return {
        "image": settings.SIMULATOR_IMAGE,
        "command": ["--model", served_model, "--port", str(PORT)],
    }


def _vllm_options(served_model: str, gpu_index: int, gpus: int = 1, context: int = 4096) -> dict:
    return {
        "image": settings.VLLM_IMAGE,
        "entrypoint": "vllm",
        "command": ["serve", served_model, "--port", str(PORT), "--tensor-parallel-size", str(gpus), "--max-model-len", str(context)],
        "environment": {"HUGGING_FACE_HUB_TOKEN": settings.HF_TOKEN},
        "volumes": {"inference-stack-huggingface": {"bind": "/root/.cache/huggingface", "mode": "rw"}},
        "shm_size": "8g",
        "device_requests": [docker.types.DeviceRequest(device_ids=[str(n) for n in range(gpu_index, gpu_index + gpus)], capabilities=[["gpu"]])],
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


def gpu_inventory() -> dict:
    """Probe the Docker host, including when the launcher itself has no GPU devices."""
    command = ["nvidia-smi", "--query-gpu=name,memory.total,memory.used", "--format=csv,noheader,nounits"]
    probe = None
    try:
        if not has_gpu():
            return {"count": 0, "devices": [], "error": "No NVIDIA runtime detected. Install the NVIDIA Container Toolkit to serve models."}
        try:
            own_image = _docker().containers.get(socket.gethostname()).image.id
        except docker.errors.NotFound:
            output = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True).stdout
        else:
            probe = _docker().containers.run(own_image, command, entrypoint="", detach=True,
                network_disabled=True, environment={"NVIDIA_DRIVER_CAPABILITIES": "utility"},
                device_requests=[docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])])
            if probe.wait(timeout=15)["StatusCode"] != 0:
                raise RuntimeError("GPU probe failed")
            output = probe.logs().decode()
        devices = []
        for line in output.strip().splitlines():
            name, memory, used = line.rsplit(",", 2)
            devices.append({"name": name.strip(), "memory_gb": round(float(memory) / 1024, 1),
                            "used_gb": round(float(used) / 1024, 1), "count": 1, "available": 1})
        return {"count": len(devices), "devices": devices, "error": ""}
    except Exception:
        return {"count": None, "devices": [], "error": "Could not inspect GPU capacity. Check Docker and the NVIDIA driver, then retry."}
    finally:
        if probe:
            try:
                probe.remove(force=True)
            except docker.errors.DockerException:
                pass


def routing_config() -> dict:
    path = Path(settings.STATE_DIR) / "epp-config.yaml"
    if not path.exists():
        _write_atomically("epp-config.yaml", Path(settings.ROUTING_CONFIG).read_text())
    return yaml.safe_load(path.read_text())


def save_routing(config: dict) -> None:
    _write_atomically("epp-config.yaml", yaml.safe_dump(config))
    containers = _docker().containers.list(filters={"label": "com.docker.compose.service=epp", "network": settings.DOCKER_NETWORK})
    if len(containers) != 1:
        raise RuntimeError("Could not locate the llm-d router. Check Docker Compose, then retry.")
    containers[0].restart(timeout=10)
