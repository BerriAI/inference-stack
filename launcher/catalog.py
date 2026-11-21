"""Hugging Face catalogue metadata; estimates are never a runtime guarantee."""

import math
import re
from urllib.parse import parse_qs, urlsplit

import httpx

import settings

MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)?")
DTYPE_BYTES = {"F64": 8, "F32": 4, "F16": 2, "BF16": 2, "I64": 8, "I32": 4,
               "I16": 2, "I8": 1, "U8": 1, "BOOL": 1, "F8_E4M3": 1, "F8_E5M2": 1}


def valid_id(value: str) -> str:
    if len(value) > 180 or not MODEL_ID.fullmatch(value) or ".." in value or "--" in value:
        raise ValueError("Enter a Hugging Face model ID, such as Qwen/Qwen3-0.6B.")
    return value


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
    params = [("search", query), ("sort", "downloads"), ("direction", "-1"), ("limit", "20")]
    params += [("expand[]", field) for field in ("safetensors", "pipeline_tag", "gated")]
    if cursor:
        params.append(("cursor", cursor))
    response = _get("api/models", params=params)
    next_url = response.links.get("next", {}).get("url", "")
    next_cursor = parse_qs(urlsplit(next_url).query).get("cursor", [""])[0]
    return {"models": [summarize(row) for row in response.json()], "cursor": next_cursor}


def details(model: str) -> dict:
    valid_id(model)
    result = summarize(_get(f"api/models/{model}").json())
    # Reading config verifies access to gated weights without downloading the model.
    response = httpx.get(f"https://huggingface.co/{model}/resolve/main/config.json",
                         headers={"Authorization": f"Bearer {settings.HF_TOKEN}"} if settings.HF_TOKEN else {},
                         follow_redirects=True, timeout=15)
    if response.status_code in (401, 403):
        result["blocker"] = "Accept this model's terms on Hugging Face and configure HF_TOKEN with access on this installation."
    elif response.status_code == 404:
        result["blocker"] = "This repository has no Transformers config.json. Choose a vLLM-compatible model repository."
    else:
        response.raise_for_status()
        config = response.json()
        result["max_context"] = config.get("max_position_embeddings")
        result["architecture"] = (config.get("architectures") or [None])[0]
    return result
