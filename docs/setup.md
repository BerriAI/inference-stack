# Run Inference Stack

Deploy a Hugging Face model with vLLM. LiteLLM sends requests to the model you choose and manages keys, teams, and budgets. llm-d selects a replica using cache and queue signals; Grafana shows metrics collected by Prometheus.

[Docker Compose](#docker-compose) · [Kubernetes](#kubernetes) · [Your first request](#your-first-request) · [Configuration](#configuration) · [Update](#update) · [Recovery and cleanup](#recovery-and-cleanup) · [Development](#development)

## Docker Compose

Use this path for one machine. You need Docker with Compose, curl, and OpenSSL. Real models also need an NVIDIA GPU and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). You can try the flow without a GPU using simulation.

```bash
curl -fsSL https://github.com/BerriAI/inference-stack/releases/download/v0.1.2/install.sh | bash -s -- docker
```

The installer verifies the release bundle, extracts it into `./inference-stack`, generates a private `compose/.env`, and starts the prebuilt stack. To choose another directory, export `INFERENCE_STACK_DIR` before running it. An existing installation and its credentials are reused.

Open [the launcher](http://localhost:8080). Sign in as `admin` with the `LITELLM_MASTER_KEY` from `inference-stack/compose/.env`, then [deploy a model and send a request](#your-first-request).

Keep `.env` for future starts; the database retains its password. Blank, short, and known example passwords are rejected. Compose runs one model at a time. **Replace model** downloads the server image, then stops the previous model before starting its replacement.

All published ports bind to `127.0.0.1`. The launcher controls Docker, so keep local access or put it behind an HTTPS reverse proxy with your organization's access controls.

## Kubernetes

Use this path for a cluster. You need Helm, `kubectl`, curl, OpenSSL, and permission to install the stack's resources. Confirm that `kubectl config current-context` selects the intended cluster. Real models need GPU nodes with the [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/getting-started.html). Simulation does not require GPUs.

```bash
curl -fsSL https://github.com/BerriAI/inference-stack/releases/download/v0.1.2/install.sh | bash -s -- kubernetes
```

The installer verifies and extracts the release into `./inference-stack`, installs the stack in namespace `inference` with generated credentials, waits for readiness, and forwards the launcher, LiteLLM, and Grafana to localhost. Leave the terminal open; Ctrl-C closes these connections while the stack keeps running. Running the command again reconnects to an existing healthy release without changing its values.

Open [the launcher](http://localhost:8080). In your Kubernetes secret viewer, open namespace `inference`, Secret `inference-stack-litellm-masterkey`, field `masterkey`. Sign in as `admin` with that value. The same key signs in to LiteLLM. Continue to [your first request](#your-first-request).

The launcher installs each model and its router as a Helm release in this namespace. It waits for readiness and registers the model in LiteLLM. Removing a model uninstalls its release and removes its registration. Redeploying a model releases its GPUs before starting replacement replicas, so requests pause during that update.

Use a dedicated namespace: the launcher has deployment access there, including access to Helm release secrets. It also reads GPU nodes and pod allocations to estimate capacity. Keep the launcher behind port-forwarding or an HTTPS reverse proxy with your organization's access controls. [Limited-permission installations](#kubernetes-permissions) can generate commands instead.

## AWS

The AWS launch flow is in development and is not included in v0.1.2. The website button stays disabled until a released template passes the full GPU and browser checks.

The template in `aws/stack.yaml` creates one `g5.2xlarge` in US East (Ohio): an NVIDIA A10G with 24 GB of GPU memory, a 200 GB encrypted disk, HTTPS access, and Qwen3-4B. It uses Docker Compose; no Kubernetes cluster is required. Model weights download from Hugging Face without an account.

The intended release flow is:

1. Choose **AWS → Launch on AWS** on the website. Sign into the AWS account that will own and pay for the installation.
2. Choose an admin password, review the template, acknowledge the IAM permissions, and choose **Create stack**. The default stop time is four hours.
3. Wait for `CREATE_COMPLETE`, then open **Outputs → OpenInferenceStack**. Sign in with your chosen password.
4. On the ready model, choose **Open LiteLLM**. Sign in as `admin` with the same password, choose the model in Playground, and send a message.

Compute costs $1.212/hour in Ohio, plus disk, public IPv4, network, and other AWS charges. An account needs at least 8 **Running On-Demand G and VT instances** vCPUs in its Ohio EC2 quota. The template checks this before creating a GPU machine. A sufficient quota does not guarantee instance capacity; AWS may still reject the launch when capacity is unavailable.

**Stopping and returning:** open the GPU details from a model page, then choose **Manage in AWS**. Stop or start the instance there. The stop timer runs again on each boot. Keep the same Inference Stack URL for your next visit; allow the services and model time to restart. Stopping compute preserves the disk and its charges. Removing a model does not stop compute.

**Removing everything:** choose **Delete installation**, then delete the CloudFormation stack in AWS. This deletes the instance and its disk, including downloaded models and the database. Check that deletion finishes; if AWS reports `DELETE_FAILED`, resolve the named resource and retry deletion.

If setup fails, read the failed event in CloudFormation. For a quota error, request an increase in AWS Service Quotas, then launch again after approval. For a bootstrap failure, inspect the instance with AWS Systems Manager. Do not put a key in a URL or open public SSH access. Keep the original password on an existing installation; changing only the CloudFormation password parameter does not rotate every service's stored credentials.

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

After installation, switch to a command-only launcher without replacing its generated credentials:

```bash
cd inference-stack
helm upgrade inference-stack charts/inference-stack --namespace inference \
  --reuse-values --set launcher.manageModels=false --wait
```

Run generated model commands from the extracted installation's root with your cluster credentials. Keep the model page open or return to it to check readiness. When capacity is unknown, verify the allocation yourself; Kubernetes still enforces GPU resource requests.

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

If a Kubernetes install times out, inspect the release before retrying:

```bash
kubectl get pods -n inference
helm list -n inference --deployed --failed --pending --uninstalling
```

After resolving the pod or storage failure, reuse the release's generated values:

```bash
cd inference-stack
helm upgrade inference-stack charts/inference-stack --namespace inference --reuse-values --wait --timeout 10m
bash install.sh kubernetes
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

## Update

v0.1.2 fixes Kubernetes startup connections and includes the Deploy-button fix from v0.1.1. Keep your existing credentials and volumes.

For Docker, change this line in `inference-stack/compose/.env`:

```dotenv
INFERENCE_STACK_LAUNCHER_IMAGE=ghcr.io/berriai/inference-stack/launcher:0.1.2
```

Then update the launcher:

```bash
cd inference-stack/compose
docker compose up -d --no-build --pull always launcher
```

For Kubernetes on v0.1.1, rerun the [setup command](#kubernetes) to reconnect with the fixed installer. No chart update is needed.

For Kubernetes on v0.1.0, update the chart while retaining your values:

```bash
helm upgrade inference-stack https://github.com/BerriAI/inference-stack/releases/download/v0.1.2/inference-stack-0.1.2.tgz \
  --namespace inference --reuse-values --set-string launcher.image=ghcr.io/berriai/inference-stack/launcher:0.1.2 --wait
```

After the chart update, rerun the [Kubernetes setup command](#kubernetes) to reconnect. Open the launcher and LiteLLM to confirm both are reachable.

## Deploy with your agent

Copy the prompt for your environment:

<details>
<summary>Docker Compose</summary>

```text
Set up Inference Stack v0.1.2 on this machine with Docker Compose.
Read https://github.com/BerriAI/inference-stack/blob/v0.1.2/docs/setup.md
and inspect its release installer before running it. Check prerequisites,
preserve any existing installation and credentials, and keep secrets out
of chat and logs. Start the stack, verify its health, and open the launcher.
Help me deploy a model that fits my GPUs and send a request in LiteLLM.
If no GPU is available, offer simulation and identify its canned response.
```

</details>

<details>
<summary>Kubernetes</summary>

```text
Set up Inference Stack v0.1.2 in Kubernetes.
Read https://github.com/BerriAI/inference-stack/blob/v0.1.2/docs/setup.md
and inspect its release installer before running it. Confirm the current
cluster is the intended target, check permissions and GPU capacity, and
preserve existing releases and credentials. Keep secrets out of chat and
logs. Install the stack, verify its pods, and open the launcher through
port-forwarding. Help me deploy a model and send a request in LiteLLM.
Use simulation only if I choose it. Ask before provisioning paid resources.
```

</details>

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
| `aws/` | Single-machine CloudFormation template and bootstrap |
| `charts/inference-stack/` | LiteLLM, Postgres, Prometheus, Grafana, and launcher |
| `charts/model/` | One model's vLLM deployment, llm-d router, and LiteLLM registration |

The browser regression tests use Playwright with Chromium:

```bash
npm install --prefix /tmp/inference-stack-browser-tests --no-package-lock playwright@1.62.1
/tmp/inference-stack-browser-tests/node_modules/.bin/playwright install chromium
NODE_PATH=/tmp/inference-stack-browser-tests/node_modules node --test tests/test_model_settings.cjs
```

The Grafana dashboards in `charts/inference-stack/files/dashboards/` are shared by Compose and Helm. LiteLLM and llm-d dashboards come from their upstream repositories.
