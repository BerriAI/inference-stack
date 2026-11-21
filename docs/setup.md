# Run inference stack

Deploy a model with vLLM, route requests through llm-d, and use LiteLLM for API access, keys, teams, and budgets. Grafana brings the stack's metrics together.

[Docker Compose](#docker-compose) · [Kubernetes](#kubernetes) · [Your first request](#your-first-request) · [Configuration](#configuration) · [Recovery and cleanup](#recovery-and-cleanup) · [Development](#development)

## Docker Compose

Use this path for one machine. You need Git, Docker with Compose, and OpenSSL. Real models also need an NVIDIA GPU and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). You can try the flow without a GPU using simulation.

Clone the repository and generate the two passwords locally. This command creates a private `.env` file and refuses to overwrite an existing one.

```bash
git clone https://github.com/BerriAI/inference-stack
cd inference-stack/compose
(umask 077; set -C; printf 'LITELLM_MASTER_KEY=%s\nPOSTGRES_PASSWORD=%s\n' \
  "$(openssl rand -hex 24)" "$(openssl rand -hex 24)" > .env)
docker compose up -d
```

Open [the launcher](http://localhost:8080). Sign in as `admin` with the `LITELLM_MASTER_KEY` from your local `.env`, then [deploy a model and send a request](#your-first-request).

Keep `.env` for future starts; the database retains its password. Blank, short, and known example passwords are rejected. Compose runs one model at a time. **Replace model** downloads the server image, then stops the previous model before starting its replacement.

All published ports bind to `127.0.0.1`. The launcher controls Docker, so keep local access or put it behind an HTTPS reverse proxy with your organization's access controls.

## Kubernetes

Use this path for a cluster. You need Git, Helm, `kubectl`, OpenSSL, and permission to install the stack's resources. Real models need GPU nodes with the [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/getting-started.html). Simulation does not require GPUs.

From a fresh checkout:

```bash
git clone https://github.com/BerriAI/inference-stack
cd inference-stack
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm dependency build charts/inference-stack
POSTGRES_PASSWORD="$(openssl rand -hex 24)"
POSTGRES_ADMIN_PASSWORD="$(openssl rand -hex 24)"
helm install inference-stack charts/inference-stack --namespace inference --create-namespace \
  --set-string litellm.postgresql.auth.password="$POSTGRES_PASSWORD" \
  --set-string litellm.postgresql.auth.postgres-password="$POSTGRES_ADMIN_PASSWORD"
unset POSTGRES_PASSWORD POSTGRES_ADMIN_PASSWORD
kubectl port-forward -n inference svc/inference-stack-launcher 8080:8080
```

Leave port-forwarding running and open [the launcher](http://localhost:8080). The chart generates an admin key: in your Kubernetes secret viewer, open namespace `inference`, Secret `inference-stack-litellm-masterkey`, field `masterkey`. Sign in as `admin` with that value. The same key signs in to LiteLLM.

To open LiteLLM and Grafana from their launcher cards, run each command in a separate terminal:

```bash
kubectl port-forward -n inference svc/inference-stack-litellm 4000:4000
```

```bash
kubectl port-forward -n inference svc/inference-stack-grafana 3000:80
```

Continue to [your first request](#your-first-request).

The launcher installs each model and its router as a Helm release in this namespace. It waits for readiness and registers the model in LiteLLM. Removing a model uninstalls its release and removes its registration. Redeploying a model releases its GPUs before starting replacement replicas, so requests pause during that update.

Use a dedicated namespace: the launcher has deployment access there, including access to Helm release secrets. It also reads GPU nodes and pod allocations to estimate capacity. Keep the launcher behind port-forwarding or an HTTPS reverse proxy with your organization's access controls. [Limited-permission installations](#kubernetes-permissions) can generate commands instead.

## Your first request

1. Open **vLLM** in the launcher. Choose **Add model** if models are already deployed.
2. Search Hugging Face, select a model, and review its GPU allocation. Replicas and context length are optional under **Settings**. Without a GPU, enable **Simulate without a GPU (canned responses)** there.
3. Click **Deploy**. Wait until the model page shows **Ready**.
4. Choose **Open LiteLLM** and sign in as `admin` with the same admin key. Open **Playground**, select your model, enter a prompt, and send it.

A response confirms the path from LiteLLM through llm-d to the model server. Simulation returns canned text; it does not validate a real model or GPU. To connect an application, create a virtual key in LiteLLM and use `http://localhost:4000/v1` as the OpenAI-compatible base URL from this machine.

| Tool | Local address |
| --- | --- |
| Launcher | http://localhost:8080 |
| LiteLLM | http://localhost:4000/ui |
| Grafana | http://localhost:3000 |

LiteLLM and Grafana open in new tabs. Use **llm-d → Settings** to adjust routing, and **Grafana** to inspect the stack overview.

## Configuration

The default settings are enough for an ungated model that fits your GPUs. Use the options below when your installation needs them.

### Models and GPU allocation

The launcher searches the full Hugging Face catalogue and estimates GPU memory. Estimates are not a guarantee of [vLLM compatibility](https://docs.vllm.ai/en/stable/models/supported_models/). Each replica's GPUs must fit on one machine or Kubernetes node. Missing access, unsupported tasks, and known capacity blockers appear before deployment.

Gated models require accepted model terms and a Hugging Face token with access:

- **Compose:** add `HF_TOKEN` to your local `compose/.env`, then recreate the launcher from the `compose` directory:

  ```bash
  docker compose up -d launcher
  ```

- **Kubernetes:** create a Secret with an `HF_TOKEN` field through your secret-management workflow. Set `launcher.huggingFaceSecret` to its name in your Helm values. The launcher and model pods use the same Secret.

Tokens stay on the server.

### Kubernetes permissions

The chart checks the launcher's actual permissions. These values control the access granted at installation:

| Helm value | Default | Effect when changed |
| --- | --- | --- |
| `launcher.manageModels` | `true` | Set to `false` to offer deployment and removal commands instead of running them. |
| `launcher.inspectGPUs` | `true` | Set to `false` to avoid cluster-wide node and pod reads. GPU capacity appears as unknown. |

For example, add `--set launcher.manageModels=false` to the install command for a command-only launcher. Run the generated Helm commands from this repository's root with your cluster credentials. Keep the model page open or return to it to check readiness. When capacity is unknown, verify the allocation yourself; Kubernetes still enforces GPU resource requests.

### Dashboard addresses and sign-in

For dashboards hosted beyond port-forwarding, set `launcher.litellmPublicUrl` and `launcher.grafanaPublicUrl` in your Helm values. Use addresses the user's browser can reach. Plain HTTP is for local access; use HTTPS and your organization's access controls for shared access.

| Credential | Used by | Source |
| --- | --- | --- |
| LiteLLM master key | Launcher and LiteLLM admin login | Compose `.env`, or the generated Kubernetes Secret described above |
| Postgres passwords | Stack services | Generated during installation |
| Virtual key | Application requests | Create in the LiteLLM dashboard |

To use a separate launcher password, set `LAUNCHER_PASSWORD` in the launcher's container environment. It must be a generated value of at least 20 characters. Keep the existing database and gateway credentials when restarting an installation.

### Routing, teams, and fairness

Routing works as soon as a model is deployed. Under **llm-d → Settings**, adjust prefix-cache and queue-depth weights for a model. Other scorers are preserved. Applying changes restarts the router and can briefly pause requests. Custom policies without these scorers stay managed through their existing configuration.

Create teams and keys in LiteLLM. Requests carry the team's `x-llm-d-inference-fairness-id` header to llm-d, which can use it to share capacity between teams. See [the header callback](../charts/inference-stack/files/llmd_headers.py).

## Recovery and cleanup

If deployment fails, the model page keeps your settings and offers **Retry deployment**. **Remove model** is also available for models that are not ready.

For Compose, run these from the `compose` directory:

```bash
docker compose ps
docker logs vllm-0
```

A failed image pull leaves the existing model running. A launcher restart retains Compose deployment progress in its state volume.

For Kubernetes:

```bash
kubectl get pods -n inference
helm list -n inference --all
```

Kubernetes model state is discovered from the cluster. If the launcher cannot deploy, use its generated command and return to the model page to check the result.

Remove models in the launcher before stopping the stack. For Compose:

```bash
docker compose down
```

This keeps the database and launcher-state volumes. For Kubernetes:

```bash
helm uninstall inference-stack --namespace inference
```

Database persistent volumes may remain; manage them separately according to your storage policy.

## Development

Keep the launcher as FastAPI with plain HTML, CSS, and JavaScript. Integrations are ordinary modules; deployment configuration stays in Compose and Helm.

From the repository root, with Python 3.12, Helm, and Docker installed:

```bash
python3 -m venv .venv
.venv/bin/pip install -r launcher/requirements.txt
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm dependency build charts/model
helm dependency build charts/inference-stack
.venv/bin/python -m unittest discover -s tests
docker build -f launcher/Dockerfile -t inference-stack-launcher:dev .
```

| Path | Contents |
| --- | --- |
| `compose/` | Docker Compose stack and configuration |
| `launcher/` | Web app and deployment integrations |
| `charts/inference-stack/` | LiteLLM, Postgres, Prometheus, Grafana, and launcher |
| `charts/model/` | One model's vLLM deployment, llm-d router, and LiteLLM registration |

The Grafana dashboards in `charts/inference-stack/files/dashboards/` are shared by Compose and Helm. LiteLLM and llm-d dashboards come from their upstream repositories.
