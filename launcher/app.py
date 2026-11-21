import json
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import compose_backend
import litellm_client
import settings

app = FastAPI(title="Inference Stack Launcher")
CATALOG = json.loads(Path(__file__).with_name("catalog.json").read_text())
deployment = {"step": "", "error": ""}


class AddModel(BaseModel):
    model: str
    replicas: int = Field(default=1, ge=1, le=8)
    simulate: bool = False


class Chat(BaseModel):
    model: str
    message: str


def public_name(served_model: str) -> str:
    return served_model.split("/")[-1].lower().replace(".", "-")


def is_healthy(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def helm_command(request: AddModel) -> str:
    name = public_name(request.model)
    return (
        f"helm install {name} {settings.HELM_MODEL_CHART}"
        f" --namespace {settings.HELM_NAMESPACE}"
        f" --set model={request.model}"
        f" --set replicas={request.replicas}"
        f" --set simulate={str(request.simulate).lower()}"
        f" --set stackRelease={settings.HELM_STACK_RELEASE}"
        f" --set llm-d-router-standalone.router.modelServers.matchLabels.model={name}"
    )


def deploy_on_compose(request: AddModel) -> None:
    try:
        deployment.update(step="Starting model servers", error="")
        compose_backend.start_model_servers(request.model, request.replicas, request.simulate)
        deployment["step"] = "Loading model weights"
        compose_backend.wait_until_ready()
        deployment["step"] = "Registering with llm-d"
        compose_backend.publish_endpoints()
        deployment["step"] = "Registering with LiteLLM"
        for model in litellm_client.list_models():
            litellm_client.remove_model(model["name"])
        litellm_client.register_model(public_name(request.model), request.model, f"{settings.ROUTER_URL}/v1")
        deployment["step"] = ""
    except Exception as error:
        deployment.update(step="", error=str(error))


@app.on_event("startup")
def publish_existing_model_servers() -> None:
    if settings.MODE == "compose":
        compose_backend.publish_endpoints()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/status")
def status() -> dict:
    litellm_healthy = is_healthy(f"{settings.LITELLM_URL}/health/liveliness")
    on_compose = settings.MODE == "compose"
    components = [
        {"name": "LiteLLM", "healthy": litellm_healthy, "url": f"{settings.LITELLM_PUBLIC_URL}/ui"},
        {"name": "Grafana", "healthy": is_healthy(f"{settings.GRAFANA_URL}/api/health"),
         "url": f"{settings.GRAFANA_PUBLIC_URL}/d/inference-stack-overview"},
    ]
    if on_compose:
        # On Kubernetes each model has its own router, installed by the model chart.
        components.insert(1, {"name": "llm-d router", "healthy": is_healthy(settings.ROUTER_HEALTH_URL), "url": None})
    return {
        "mode": settings.MODE,
        "gpu": compose_backend.has_gpu() if on_compose else True,
        "catalog": CATALOG,
        "deployment": deployment,
        "model_servers": compose_backend.running_model() if on_compose else None,
        "models": litellm_client.list_models() if litellm_healthy else [],
        "components": components,
        "litellm_public_url": settings.LITELLM_PUBLIC_URL,
    }


@app.post("/api/models")
def add_model(request: AddModel, background: BackgroundTasks) -> dict:
    if settings.MODE == "kubernetes":
        return {"command": helm_command(request)}
    if deployment["step"]:
        raise HTTPException(409, "A model is already being deployed.")
    deployment.update(step="Starting model servers", error="")
    background.add_task(deploy_on_compose, request)
    return {"name": public_name(request.model)}


@app.delete("/api/models/{name}")
def remove_model(name: str) -> dict:
    litellm_client.remove_model(name)
    if settings.MODE == "kubernetes":
        return {"command": f"helm uninstall {name} --namespace {settings.HELM_NAMESPACE}"}
    compose_backend.remove_model_servers()
    return {"removed": name}


@app.post("/api/keys")
def create_key() -> dict:
    return {"key": litellm_client.create_key()}


@app.post("/api/chat")
def chat(request: Chat) -> dict:
    try:
        return {"reply": litellm_client.chat(request.model, request.message)}
    except httpx.HTTPStatusError as error:
        raise HTTPException(error.response.status_code, error.response.text)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("static") / "index.html")


app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
