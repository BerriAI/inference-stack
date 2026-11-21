# inference-stack

Run your own models behind one OpenAI-compatible endpoint.

- [vLLM](https://github.com/vllm-project/vllm) serves the model on your GPUs.
- [llm-d](https://github.com/llm-d/llm-d) routes each request to the best vLLM replica (queue depth, KV cache, prefix cache).
- [LiteLLM](https://github.com/BerriAI/litellm) is the gateway: keys, teams, budgets and spend.
- Grafana shows all three in one place.
- The launcher opens each tool and handles model deployment on this installation.

```
your app -> LiteLLM -> llm-d router -> vLLM replicas
```

## One machine (Docker Compose)

Needs Docker. To serve real models you also need an NVIDIA GPU and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).
Without a GPU you can still try everything with a simulated model.

```bash
git clone https://github.com/BerriAI/inference-stack && cd inference-stack/compose
cp .env.example .env    # fill the two blank passwords with separate generated values
# Generate each value locally with: openssl rand -hex 24
docker compose up -d
```

Open the launcher at http://localhost:8080. Sign in as `admin` using your
`LITELLM_MASTER_KEY`, then choose **vLLM**, search Hugging Face,
and select a model. Review its GPU allocation, then click **Deploy**. Replicas,
context length and GPU simulation are under **Settings**. Simulation needs no GPU
and returns canned responses.

When the model shows **Ready**, choose **Open LiteLLM** to send requests and manage
keys and teams in the existing LiteLLM dashboard. **llm-d** shows replicas and
routing settings; **Grafana** opens the stack overview. Both external dashboards
open in a new tab.

| What | URL |
|---|---|
| Launcher | http://localhost:8080 |
| LiteLLM API and UI | http://localhost:4000 (UI at `/ui`, login `admin` and your master key) |
| Grafana | http://localhost:3000 |

The launcher detects GPU memory using the Docker host's NVIDIA runtime. GPU
requirements are estimates, not a guarantee of vLLM compatibility. Each replica's
GPUs must fit on one machine. Compose runs one model at a time: **Replace model**
stops the previous model after the server image downloads.

Gated Hugging Face models need accepted model terms and an `HF_TOKEN` with access
in `.env`. The token stays on the server. Missing or unsupported metadata is shown
before deployment; check the linked vLLM compatibility list for unfamiliar models.

The launcher controls Docker. Every port is bound to `127.0.0.1`, and the launcher
requires admin authentication. Keep these bindings or use an HTTPS reverse proxy
with your organization’s access controls. Plain HTTP is only for local access.
Blank, short and known example passwords are rejected before the launcher starts;
Postgres also refuses short or known example passwords. For an existing installation,
retain its current strong passwords when upgrading. Rotate example credentials through
your database and gateway procedures before applying the update.

To stop: remove the model in the launcher, then `docker compose down`.

## Kubernetes (Helm)

Needs a cluster with GPU nodes and the [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/getting-started.html).

```bash
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

The chart generates the LiteLLM master key in the `inference-stack-litellm-masterkey`
Kubernetes Secret. Retrieve its `masterkey` field through your secret-management
workflow and sign in to the launcher as `admin` with that key. The same admin key
signs in to LiteLLM. You can set `LAUNCHER_PASSWORD` on the launcher deployment for
a separate password without changing the gateway key.

Open **vLLM** and choose a model. The launcher uses its service account to install
the bundled model chart in this namespace. Helm waits for vLLM and llm-d, then
registers the model in LiteLLM. The model page shows **Ready** after registration
and readiness checks succeed. Removing a model uninstalls its release and removes
its LiteLLM registration. Redeploying an existing model releases its GPUs before
starting replacement replicas, so requests pause during that update.

Forward the existing dashboards in separate terminals to use their cards:

```bash
kubectl port-forward -n inference svc/inference-stack-litellm 4000:4000
kubectl port-forward -n inference svc/inference-stack-grafana 3000:80
```

For browser-accessible dashboard addresses, set `launcher.litellmPublicUrl` and
`launcher.grafanaPublicUrl` in your Helm values. These are addresses the browser
can reach, not Kubernetes service addresses.

### Permissions and gated models

The launcher requires admin authentication. Keep it behind port-forwarding or an
HTTPS reverse proxy with your organization’s access controls.
The chart grants deployment access within its namespace, including access to Helm
release secrets. Use a dedicated namespace. It also grants read access to GPU
nodes and pod allocations to estimate available capacity.

For a command-only installation, add these values to the install command:

```bash
--set launcher.manageModels=false
```

The launcher checks its actual permissions and offers a complete Helm command when
it cannot deploy. Run that command from this repository checkout using your cluster
credentials. Keep the model page open or return to it to check readiness.

Set `launcher.inspectGPUs=false` if cluster-wide node and pod reads are unavailable.
GPU capacity is then shown as unknown; verify the allocation before deploying.
Kubernetes still enforces the model pods’ GPU resource requests.

For gated models, create a Kubernetes secret containing `HF_TOKEN` through your
normal secret-management workflow. Set `launcher.huggingFaceSecret` to its name;
the launcher and model pods use the same secret.

### Routing and recovery

Routing defaults are applied when a model is deployed. Under **llm-d → Settings**,
you can adjust prefix-cache and queue-depth weights for a model. Other scorers are
preserved. Applying changes restarts the router and can briefly pause requests.
Custom policies without these scorers remain managed through their existing config.

If deployment fails, the model page retains its settings and offers **Retry deployment**.
For diagnostics, inspect the model pods and Helm release in the installation's namespace:

```bash
kubectl get pods -n inference
helm list -n inference --all
```

For Compose, inspect the model server with `docker logs vllm-0`. A failed image pull
leaves the existing model running. A launcher restart retains Compose deployment
progress in its state volume; Kubernetes discovers deployed resources from the cluster.

Remove models before uninstalling the stack:

```bash
helm uninstall inference-stack --namespace inference
```

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

## Development

Keep the launcher as FastAPI with plain HTML, CSS and JavaScript. The catalogue and
Kubernetes integrations are ordinary modules in `launcher/`; deployment packaging
stays in the existing Compose and Helm directories.

```bash
python -m venv .venv
.venv/bin/pip install -r launcher/requirements.txt
.venv/bin/python -m unittest discover -s tests
helm dependency build charts/model
helm dependency build charts/inference-stack
docker build -f launcher/Dockerfile -t inference-stack-launcher:dev .
```
