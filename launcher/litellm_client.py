import httpx

import settings

client = httpx.Client(
    base_url=settings.LITELLM_URL,
    headers={"Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}"},
    timeout=120,
)


def list_models() -> list[dict]:
    rows = client.get("/model/info").raise_for_status().json()["data"]
    return [
        {
            "name": row["model_name"],
            "id": row["model_info"]["id"],
            "served_model": row["litellm_params"]["model"].removeprefix("hosted_vllm/"),
        }
        for row in rows
    ]


def register_model(name: str, served_model: str, api_base: str) -> None:
    remove_model(name)
    client.post(
        "/model/new",
        json={
            "model_name": name,
            "litellm_params": {"model": f"hosted_vllm/{served_model}", "api_base": api_base},
        },
    ).raise_for_status()


def remove_model(name: str) -> None:
    for model in list_models():
        if model["name"] == name:
            client.post("/model/delete", json={"id": model["id"]}).raise_for_status()


def create_key() -> str:
    return client.post("/key/generate", json={"key_alias": None}).raise_for_status().json()["key"]


def chat(model: str, message: str) -> str:
    response = client.post(
        "/v1/chat/completions",
        json={"model": model, "messages": [{"role": "user", "content": message}], "max_tokens": 200},
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]
