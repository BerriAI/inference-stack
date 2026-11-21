# inference-stack

Run your own models behind one OpenAI-compatible endpoint.

- [vLLM](https://github.com/vllm-project/vllm) serves the model on your GPUs.
- [llm-d](https://github.com/llm-d/llm-d) routes each request to the best vLLM replica (queue depth, KV cache, prefix cache).
- [LiteLLM](https://github.com/BerriAI/litellm) is the gateway: keys, teams, budgets and spend.
- Grafana shows all three in one place.
- The launcher is a small web page to add a model, try it and open the other tools.

```
your app -> LiteLLM -> llm-d router -> vLLM replicas
```

## One machine (Docker Compose)

Needs Docker. To serve real models you also need an NVIDIA GPU and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
Without a GPU you can still try everything with a simulated model.

```bash
git clone https://github.com/BerriAI/inference-stack && cd inference-stack/compose
cp .env.example .env    # set LITELLM_MASTER_KEY and POSTGRES_PASSWORD
docker compose up -d
```

Open the launcher at http://localhost:8080, pick a model and click **Add model**.
The launcher starts vLLM, tells llm-d about it and registers it in LiteLLM.
Then click **Create an API key** and call it like OpenAI.

| What | URL |
|---|---|
| Launcher | http://localhost:8080 |
| LiteLLM API and UI | http://localhost:4000 (UI at `/ui`, login `admin` and your master key) |
| Grafana | http://localhost:3000 |

Each replica uses one GPU, so use as many replicas as you have GPUs.
Gated Hugging Face models need `HF_TOKEN` in `.env`.

The launcher controls Docker and has no login, so every port is bound to `127.0.0.1`. Put your own auth in front before exposing it.

To stop: remove the model in the launcher, then `docker compose down`.

## Kubernetes (Helm)

Needs a cluster with GPU nodes and the [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/getting-started.html).

```bash
helm dependency build charts/inference-stack
helm install inference-stack charts/inference-stack --namespace inference --create-namespace
kubectl port-forward -n inference svc/inference-stack-launcher 8080:8080
```

In the launcher, pick a model and click **Add model**. On Kubernetes the launcher has no
cluster permissions, so it prints the `helm install` command for you to run:

```bash
helm dependency build charts/model
helm install qwen3-8b charts/model --namespace inference \
  --set model=Qwen/Qwen3-8B --set replicas=2 \
  --set llm-d-router-standalone.router.modelServers.matchLabels.model=qwen3-8b
```

That creates the vLLM replicas and an llm-d router for the model, and registers it in LiteLLM.

## Teams and fairness

Create teams and keys in the LiteLLM UI. LiteLLM sends each request's team to llm-d in the
`x-llm-d-inference-fairness-id` header ([llmd_headers.py](charts/inference-stack/files/llmd_headers.py)),
so llm-d can share GPU capacity fairly between teams.

## Layout

| Path | What |
|---|---|
| `compose/` | Docker Compose stack and its config |
| `launcher/` | Launcher web app (FastAPI and one static page) |
| `charts/inference-stack/` | Helm chart: LiteLLM, Postgres, Prometheus, Grafana, launcher |
| `charts/model/` | Helm chart: one model (vLLM, llm-d router, LiteLLM registration) |
| `charts/inference-stack/files/dashboards/` | Grafana dashboards, used by both Compose and Helm |

The LiteLLM and llm-d dashboards are copied from their upstream repositories.
