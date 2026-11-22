"""Hugging Face catalogue metadata; estimates are never a runtime guarantee."""

import math
import base64
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

import settings

MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)?")
DTYPE_BYTES = {"F64": 8, "F32": 4, "F16": 2, "BF16": 2, "I64": 8, "I32": 4,
               "I16": 2, "I8": 1, "U8": 1, "BOOL": 1, "F8_E4M3": 1, "F8_E5M2": 1}


def valid_id(value: str) -> str:
    if value.startswith("https://huggingface.co/"):
        parsed = urlsplit(value)
        value = "/".join(parsed.path.strip("/").split("/")[:2])
    if len(value) > 180 or not MODEL_ID.fullmatch(value) or ".." in value or "--" in value:
        raise ValueError("Enter a Hugging Face model ID, such as Qwen/Qwen3-0.6B.")
    return value


def restore_token():
    if settings.MODE == "kubernetes":
        import kubernetes_backend as kubernetes
        name = settings.HELM_STACK_RELEASE + "-huggingface"
        try:
            secret = kubernetes.api("GET", kubernetes.resource_path("secrets/" + name))
            settings.HF_TOKEN = base64.b64decode(secret["data"]["HF_TOKEN"]).decode()
            settings.HF_SECRET = name
        except Exception:
            pass  # Existing environment/Secret configuration still applies.
    else:
        path = Path(settings.HF_TOKEN_FILE)
        if path.exists():
            settings.HF_TOKEN = path.read_text()


def save_token(token):
    httpx.get("https://huggingface.co/api/whoami-v2", headers={"Authorization": f"Bearer {token}"}, timeout=15).raise_for_status()
    if settings.MODE == "kubernetes":
        import kubernetes_backend as kubernetes
        if not kubernetes.permissions()["deploy"]:
            raise RuntimeError("Credential storage requires deployment access")
        name = settings.HELM_STACK_RELEASE + "-huggingface"
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name}, "stringData": {"HF_TOKEN": token}}
        path = kubernetes.resource_path("secrets")
        try:
            kubernetes.api("PATCH", path + "/" + name, json=secret, headers={"Content-Type": "application/merge-patch+json"})
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                raise
            kubernetes.api("POST", path, json=secret)
        settings.HF_SECRET = name
    else:
        path = Path(settings.HF_TOKEN_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
            file.write(token)
        path.chmod(0o600)
    settings.HF_TOKEN = token


def _get(path: str, **kwargs) -> httpx.Response:
    headers = {"Authorization": f"Bearer {settings.HF_TOKEN}"} if settings.HF_TOKEN else {}
    return httpx.get(f"https://huggingface.co/{path}", headers=headers, timeout=15, **kwargs).raise_for_status()


def summarize(row: dict) -> dict:
    tensors = (row.get("safetensors") or {}).get("parameters", {})
    weight_bytes = sum(count * DTYPE_BYTES.get(dtype, 2) for dtype, count in tensors.items())
    weights_gb = round(weight_bytes / 2**30, 1) if weight_bytes else None
    estimate = math.ceil(weight_bytes * 1.25 / 2**30 + 2) if weight_bytes else None
    task = row.get("pipeline_tag")
    unsupported = task and task not in {"text-generation", "image-text-to-text", "text2text-generation"}
    return {"id": row.get("id") or row["modelId"], "task": task, "gated": bool(row.get("gated")),
            "weights_gb": weights_gb, "estimated_gb": estimate, "parameters": sum(tensors.values()) or None,
            "blocker": f"This model's task ({task}) is not supported by this chat-serving flow." if unsupported else "",
            "note": "Estimate includes weights and a runtime allowance. Context, cache, architecture and quantization can change GPU requirements."}


def search(query: str = "", cursor: str = "") -> dict:
    if query.startswith("https://huggingface.co/"):
        query = valid_id(query)
    params = [("search", query), ("sort", "downloads"), ("direction", "-1"), ("limit", "20")]
    if not query:
        params.append(("pipeline_tag", "text-generation"))
    params += [("expand[]", field) for field in ("safetensors", "pipeline_tag", "gated")]
    if cursor:
        params.append(("cursor", cursor))
    response = _get("api/models", params=params)
    next_url = response.links.get("next", {}).get("url", "")
    next_cursor = parse_qs(urlsplit(next_url).query).get("cursor", [""])[0]
    models = [summarize(row) for row in response.json()]
    # Private repositories are absent from anonymous search; allow an exact ID
    # to reach the access check and token-entry flow.
    if "/" in query and MODEL_ID.fullmatch(query) and not any(row["id"] == query for row in models):
        models.insert(0, summarize({"id": valid_id(query)}))
    if not query and not cursor:
        starters = ["Qwen/Qwen3-0.6B", "Qwen/Qwen3-4B-Instruct-2507", "Qwen/Qwen3-8B"]
        models.sort(key=lambda model: starters.index(model["id"]) if model["id"] in starters else len(starters))
    return {"models": models, "cursor": next_cursor}


def details(model: str) -> dict:
    model = valid_id(model)
    try:
        result = summarize(_get(f"api/models/{model}").json())
    except httpx.HTTPStatusError as error:
        if error.response.status_code not in (401, 403):
            raise
        return {**summarize({"id": model}), "needs_token": True,
                "blocker": "This repository requires access. Connect a Hugging Face token with read access below."}
    # Reading config verifies access to gated weights without downloading the model.
    response = httpx.get(f"https://huggingface.co/{model}/resolve/main/config.json",
                         headers={"Authorization": f"Bearer {settings.HF_TOKEN}"} if settings.HF_TOKEN else {},
                         follow_redirects=True, timeout=15)
    if response.status_code in (401, 403):
        result["needs_token"] = True
        result["blocker"] = "Accept this model's terms on Hugging Face, then connect a token with read access below."
    elif response.status_code == 404:
        result["blocker"] = "This repository has no Transformers config.json. Choose a vLLM-compatible model repository."
    else:
        response.raise_for_status()
        config = response.json()
        result["max_context"] = config.get("max_position_embeddings")
        result["architecture"] = (config.get("architectures") or [None])[0]
    return result
