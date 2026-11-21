import base64
import binascii
import hashlib
import json
import math
import re
import secrets
import shlex
import threading
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import catalog
import compose_backend
import kubernetes_backend as kubernetes
import litellm_client
import settings

operation_lock = threading.Lock()
deployment = {"action": "", "name": "", "step": "", "error": "", "request": None}
STATE_FILE = Path(settings.STATE_DIR) / "deployment.json"


class AddModel(BaseModel):
    model: str
    replicas: int = Field(default=1, ge=1, le=8)
    gpus_per_replica: int = Field(default=1, ge=1, le=8)
    context: int = Field(default=4096, ge=512, le=131072)
    simulate: bool = False
    replace_existing: bool = False

    _valid_model = field_validator("model")(catalog.valid_id)


class RoutingSettings(BaseModel):
    name: str = ""
    prefix_cache: int = Field(ge=0, le=5)
    queue: int = Field(ge=0, le=5)


def public_name(model: str) -> str:
    slug = re.sub(r"[^a-z0-9-]", "-", model.split("/")[-1].lower()).strip("-")[:40]
    return f"{slug}-{hashlib.sha256(model.encode()).hexdigest()[:8]}"


def set_deployment(**values) -> None:
    deployment.update(values)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(deployment))
    temp.replace(STATE_FILE)


@asynccontextmanager
async def lifespan(app):
    if STATE_FILE.exists():
        try:
            deployment.update(json.loads(STATE_FILE.read_text()))
            if deployment["step"]:
                set_deployment(step="", error="The launcher restarted during deployment. Check the model status, then retry if needed.")
        except (ValueError, OSError):
            deployment.update(step="", error="Could not restore deployment progress. Check the model status before retrying.")
    if settings.MODE == "compose":
        try:
            compose_backend.routing_config()
            compose_backend.publish_endpoints()
        except Exception:
            pass  # Status reports Docker availability; the launcher remains usable.
    yield


app = FastAPI(title="Inference Stack Launcher", lifespan=lifespan)


@app.middleware("http")
async def protect_admin(request: Request, call_next):
    if request.url.path != "/healthz":
        valid = False
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("basic ") and len(authorization) < 4096:
            try:
                username, password = base64.b64decode(authorization[6:], validate=True).decode("utf-8").split(":", 1)
                valid_user = secrets.compare_digest(username.encode(), b"admin")
                valid_password = secrets.compare_digest(password.encode(), settings.LAUNCHER_PASSWORD.encode())
                valid = valid_user and valid_password
            except (ValueError, binascii.Error, UnicodeError):
                pass
        if not valid:
            return JSONResponse({"detail": "Sign in as admin with this installation's launcher password."},
                                status_code=401, headers={"WWW-Authenticate": 'Basic realm="Inference Stack", charset="UTF-8"'})
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (origin and origin != str(request.base_url).rstrip("/")):
            return JSONResponse({"detail": "Use the launcher from its own address."}, status_code=403)
    return await call_next(request)


def is_healthy(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@lru_cache(maxsize=2)
def inventory(bucket: int) -> dict:
    return compose_backend.gpu_inventory() if settings.MODE == "compose" else kubernetes.gpu_inventory()


def gpu_inventory() -> dict:
    return inventory(int(time.monotonic() // 30))


def model_servers(registered: list[dict] | None = None) -> list[dict]:
    if settings.MODE == "kubernetes":
        return kubernetes.models(registered)
    row = compose_backend.running_model()
    return [{**row, "name": public_name(row["served_model"]), "router_ready": is_healthy(settings.ROUTER_HEALTH_URL)}] if row else []


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/status")
def status() -> dict:
    components = {
        "litellm": {"healthy": is_healthy(f"{settings.LITELLM_URL}/health/liveliness"), "url": f"{settings.LITELLM_PUBLIC_URL}/ui"},
        "grafana": {"healthy": is_healthy(f"{settings.GRAFANA_URL}/api/health"), "url": f"{settings.GRAFANA_PUBLIC_URL}/d/inference-stack-overview"},
    }
    permissions = kubernetes.permissions() if settings.MODE == "kubernetes" else {"read": True, "deploy": True, "error": ""}
    errors = []
    try:
        registered = litellm_client.list_models() if components["litellm"]["healthy"] else []
    except httpx.HTTPError:
        registered = []
        components["litellm"]["healthy"] = False
    try:
        rows = model_servers(registered)
    except Exception:
        rows = []
        errors.append("Could not read model deployments. Check installation access, then retry.")
    for row in rows:
        match = next((m for m in registered if m["served_model"] == row["served_model"]
                      and (settings.MODE == "compose" or m["name"] == row["name"])), None)
        if match and settings.MODE == "compose":
            row["name"] = match["name"]
        row["status"] = "ready" if match and row["ready"] == row["replicas"] and row["router_ready"] else "starting"
    components["router"] = {"healthy": all(r["router_ready"] for r in rows) if settings.MODE == "kubernetes" else is_healthy(settings.ROUTER_HEALTH_URL)}
    return {"mode": settings.MODE, "hardware": gpu_inventory(), "permissions": permissions,
            "models": rows, "deployment": dict(deployment), "components": components, "errors": errors}


@app.get("/api/catalog")
def search_models(q: str = Query("", max_length=180), cursor: str = Query("", max_length=4096)) -> dict:
    try:
        return catalog.search(q, cursor)
    except (httpx.HTTPError, ValueError, KeyError):
        raise HTTPException(502, "Could not reach Hugging Face. Retry the search.")


@app.get("/api/catalog/{model:path}")
def model_details(model: str) -> dict:
    try:
        info = catalog.details(model)
    except ValueError as error:
        raise HTTPException(422, str(error))
    except httpx.HTTPStatusError as error:
        if error.response.status_code == 404:
            raise HTTPException(404, "This model was not found on Hugging Face.")
        raise HTTPException(502, "Could not check model access on Hugging Face. Check HF_TOKEN or retry.")
    except httpx.HTTPError:
        raise HTTPException(502, "Could not reach Hugging Face. Retry the model check.")
    devices = gpu_inventory()["devices"]
    memory = max((d["memory_gb"] or 0 for d in devices), default=0)
    info["suggested_gpus"] = min(8, max(1, math.ceil((info["estimated_gb"] or 0) / memory))) if memory else 1
    info["suggested_context"] = min(4096, info.get("max_context") or 4096)
    return info


def validate_allocation(request: AddModel) -> None:
    if request.simulate:
        return
    info = model_details(request.model)
    if info["blocker"]:
        raise HTTPException(422, info["blocker"])
    if info.get("max_context") and request.context > info["max_context"]:
        raise HTTPException(422, f"This model supports a context of up to {info['max_context']} tokens.")
    hardware = kubernetes.gpu_inventory(public_name(request.model)) if settings.MODE == "kubernetes" else gpu_inventory()
    if hardware["count"] == 0:
        raise HTTPException(422, "No GPU is available. Check the NVIDIA driver and device plugin, or use simulation in Settings.")
    if hardware["count"] is None:
        # Kubernetes still enforces resource requests when inventory reads are unavailable.
        if settings.MODE == "compose":
            raise HTTPException(422, hardware["error"])
        return
    devices = hardware["devices"]
    required = request.replicas * request.gpus_per_replica
    available = sum(d["available"] for d in devices)
    if required > available:
        raise HTTPException(422, f"This allocation needs {required} GPUs; {available} are available.")
    memories = [d["memory_gb"] for d in devices if d["memory_gb"]]
    if memories and info["weights_gb"] and info["weights_gb"] >= max(memories) * request.gpus_per_replica:
        raise HTTPException(422, "The model weights exceed the selected GPU memory. Choose more GPUs per replica or a smaller model.")
    if settings.MODE == "kubernetes" and sum(d["available"] // request.gpus_per_replica for d in devices) < request.replicas:
        raise HTTPException(422, "Each replica needs its GPUs on one node. This allocation does not fit the available nodes.")


def deploy_model(request: AddModel, name: str) -> None:
    try:
        if settings.MODE == "kubernetes":
            set_deployment(step="Starting vLLM and llm-d. Waiting for model readiness")
            kubernetes.deploy(request, name)
        else:
            set_deployment(step="Downloading the model server image")
            compose_backend.pull_image(request.simulate)
            previous = compose_backend.running_model()
            if previous:
                for row in litellm_client.list_models():
                    if row["served_model"] == previous["served_model"]:
                        litellm_client.remove_model(row["name"])
            set_deployment(step="Starting model servers")
            compose_backend.start_model_servers(request.model, request.replicas, request.simulate, request.gpus_per_replica, request.context)
            set_deployment(step="Loading model weights")
            compose_backend.wait_until_ready()
            set_deployment(step="Registering with llm-d")
            compose_backend.publish_endpoints()
            set_deployment(step="Registering with LiteLLM")
            litellm_client.register_model(name, request.model, f"{settings.ROUTER_URL}/v1")
        set_deployment(step="", error="")
    except Exception:
        set_deployment(step="", error=f"{deployment['step']} failed. Check the model server and image availability, then retry. Your settings are saved.")
    finally:
        inventory.cache_clear()
        operation_lock.release()


@app.post("/api/models")
def add_model(request: AddModel, background: BackgroundTasks) -> dict:
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(409, "An operation is already running. Wait for it to finish.")
    try:
        validate_allocation(request)
        name = public_name(request.model)
        if settings.MODE == "kubernetes" and not kubernetes.permissions()["deploy"]:
            command = kubernetes.command(request, name)
            operation_lock.release()
            return {"name": name, "command": command}
        if settings.MODE == "compose":
            previous = compose_backend.running_model()
            if previous and previous["served_model"] != request.model and not request.replace_existing:
                raise HTTPException(409, "This machine runs one model at a time. Confirm replacement to continue.")
        set_deployment(action="deploy", name=name, step="Preparing deployment", error="", request=request.model_dump())
        background.add_task(deploy_model, request, name)
    except HTTPException:
        operation_lock.release()
        raise
    except Exception:
        operation_lock.release()
        raise HTTPException(503, "Could not start deployment. Check installation access, then retry.")
    else:
        return {"name": name}


def managed_model(name: str) -> dict:
    try:
        rows = model_servers()
    except Exception:
        raise HTTPException(503, "Could not inspect model deployments. Retry.")
    row = next((row for row in rows if row["name"] == name), None)
    if not row and settings.MODE == "compose":
        try:
            registered = litellm_client.list_models()
        except httpx.HTTPError:
            raise HTTPException(503, "Could not verify the model registration. Retry.")
        row = next((server for server in rows if any(model["name"] == name and model["served_model"] == server["served_model"] for model in registered)), None)
    if not row:
        raise HTTPException(404, "This model is not managed by this installation.")
    return row


@app.delete("/api/models/{name}")
def remove_model(name: str, background: BackgroundTasks) -> dict:
    try:
        model = managed_model(name)
    except HTTPException as error:
        if error.status_code != 404 or deployment.get("action") != "remove" or deployment["name"] != name:
            raise
        model = None
    if settings.MODE == "kubernetes" and not kubernetes.permissions()["deploy"]:
        return {"command": shlex.join(["helm", "uninstall", name, "--namespace", settings.HELM_NAMESPACE, "--wait"])}
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(409, "An operation is already running.")
    def remove():
        try:
            if settings.MODE == "kubernetes":
                kubernetes.remove(name)
                litellm_client.remove_model(name)
            else:
                compose_backend.remove_model_servers()
                litellm_client.remove_model(name)
            set_deployment(action="", name="", step="", error="", request=None)
        except Exception:
            set_deployment(step="", error="Could not remove the model. Check the deployment, then retry removal.")
        finally:
            inventory.cache_clear()
            operation_lock.release()
    request = deployment["request"]
    if model:
        request = {"model": model["served_model"], **{k: model[k] for k in ("replicas", "gpus_per_replica", "context", "simulate")}}
    set_deployment(action="remove", name=name, step="Removing model", error="", request=request)
    background.add_task(remove)
    return {"name": name}


def routing_profile(config: dict) -> list[dict]:
    for profile in config.get("schedulingProfiles", []):
        if profile.get("name") == "default":
            return profile["plugins"]
    raise HTTPException(409, "This router uses a custom policy. Edit its existing configuration directly.")


@app.get("/api/routing")
def routing(name: str = "") -> dict:
    try:
        if settings.MODE == "kubernetes":
            managed_model(name)
            config = kubernetes.routing_config(name)
        else:
            config = compose_backend.routing_config()
        weights = {p["pluginRef"]: p.get("weight", 1) for p in routing_profile(config)}
        if not {"prefix-cache-scorer", "queue-scorer"} <= weights.keys():
            raise HTTPException(409, "This router uses a custom policy. Edit its existing configuration directly.")
        return {"prefix_cache": weights["prefix-cache-scorer"], "queue": weights["queue-scorer"]}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Could not read routing settings. Check llm-d access, then retry.")


@app.put("/api/routing")
def save_routing(request: RoutingSettings, background: BackgroundTasks) -> dict:
    routing(request.name)
    if settings.MODE == "kubernetes" and not kubernetes.permissions()["deploy"]:
        raise HTTPException(403, "Updating routing requires deployment access. Ask your cluster administrator to update the model's Helm values.")
    if not operation_lock.acquire(blocking=False):
        raise HTTPException(409, "An operation is already running.")
    def save():
        try:
            config = kubernetes.routing_config(request.name) if settings.MODE == "kubernetes" else compose_backend.routing_config()
            for plugin in routing_profile(config):
                if plugin["pluginRef"] == "prefix-cache-scorer":
                    plugin["weight"] = request.prefix_cache
                if plugin["pluginRef"] == "queue-scorer":
                    plugin["weight"] = request.queue
            if settings.MODE == "kubernetes":
                kubernetes.save_routing(request.name, config)
            else:
                compose_backend.save_routing(config)
            set_deployment(step="", error="")
        except Exception:
            set_deployment(step="", error="Could not apply routing settings. Check llm-d, then retry.")
        finally:
            operation_lock.release()
    set_deployment(action="routing", name=request.name, step="Applying routing settings", error="", request=None)
    background.add_task(save)
    return {"saving": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("static") / "index.html")


app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
