# Run Inference Stack

Run on AWS, one machine with Docker Compose, or an existing Kubernetes cluster. Deploy a Hugging Face model with vLLM. LiteLLM lets you manage models and send requests through one API or Chat. llm-d selects a replica using cache and queue signals; Grafana shows metrics collected by Prometheus.

[Docker Compose](#docker-compose) · [Kubernetes](#kubernetes) · [AWS](#aws) · [Your first request](#your-first-request) · [Configuration](#configuration) · [Update](#update) · [Recovery and cleanup](#recovery-and-cleanup) · [Development](#development)

## Docker Compose

Use this path for one machine. You need Docker with Compose, curl, and OpenSSL. Real models also need an NVIDIA GPU and the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). You can try the flow without a GPU using simulation.

```bash
curl -fsSL https://github.com/BerriAI/inference-stack/releases/download/v0.3.1/install.sh | bash -s -- docker
```

The installer verifies the release bundle, extracts it into `./inference-stack`, generates a private `compose/.env`, and starts the prebuilt stack. To choose another directory, export `INFERENCE_STACK_DIR` before running it. An existing installation and its credentials are reused.

Open [the launcher](http://localhost:8080). Sign in as `admin` with the `LITELLM_MASTER_KEY` from `inference-stack/compose/.env`, then [deploy a model and send a request](#your-first-request).

Keep `.env` for future starts; the database retains its password. Blank, short, and known example passwords are rejected. Compose runs one model at a time. **Replace deployment** downloads the server image, then stops the previous model before starting its replacement.

All published ports bind to `127.0.0.1`. The launcher controls Docker, so keep local access or put it behind an HTTPS reverse proxy with your organization's access controls.

## Kubernetes

Use this path for a cluster. You need Kubernetes 1.25 or later, Helm 3.14 or later, `kubectl`, curl, OpenSSL, and permission to install the stack's resources. Confirm that `kubectl config current-context` selects the intended cluster. Real models need GPU nodes with the [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/getting-started.html). Simulation does not require GPUs.

```bash
curl -fsSL https://github.com/BerriAI/inference-stack/releases/download/v0.3.1/install.sh | bash -s -- kubernetes
```

The installer verifies and extracts the release into `./inference-stack`, installs the stack in namespace `inference` with generated credentials, waits for readiness, and forwards the launcher, LiteLLM, and Grafana to localhost. Leave the terminal open; Ctrl-C closes these connections while the stack keeps running. Running the command again reconnects to an existing healthy release without changing its values.

Open [the launcher](http://localhost:8080). In your Kubernetes secret viewer, open namespace `inference`, Secret `inference-stack-litellm-masterkey`, field `masterkey`. Sign in as `admin` with that value. The same key signs in to LiteLLM. Continue to [your first request](#your-first-request).

The launcher installs each model and its router as a Helm release in this namespace. It waits for readiness and registers the model in LiteLLM. Removing a model uninstalls its release and removes its registration. Redeploying a model releases its GPUs before starting replacement replicas, so requests pause during that update.

Use a dedicated namespace: the launcher has deployment access there, including access to Helm release secrets. It also reads GPU nodes and pod allocations to estimate capacity. Keep the launcher behind port-forwarding or an HTTPS reverse proxy with your organization's access controls. [Limited-permission installations](#kubernetes-permissions) can generate commands instead.

## AWS

AWS installs the stack in your own account. You need a paid AWS account with permission to create the resources in the launch template; AWS bills your account directly.

The template in `aws/stack.yaml` starts with **0 models and 0 GPUs**. It creates a `t3.large` CPU machine in US East (Ohio) for the launcher, LiteLLM, Grafana, and a small Kubernetes cluster. Add GPU machines from the launcher when you need them.

To start:

1. Open [Launch on AWS](https://us-east-2.console.aws.amazon.com/cloudformation/home?region=us-east-2#/stacks/create/review?templateURL=https%3A%2F%2Finference-stack-release-templates-templates-e0eqnr6ih8gk.s3.us-east-2.amazonaws.com%2Freleases%2Fv0.3.1%2Faws-stack.yaml&stackName=inference-stack). Sign into the AWS account that will own and pay for the installation.
2. Enter an **Admin password** of at least 20 characters. Keep the other defaults, review the cost, acknowledge the IAM permissions, and choose **Create stack**.
3. Wait for `CREATE_COMPLETE`, then open **Outputs → OpenInferenceStack**. Sign in as `admin` with that password. The homepage shows **0 deployments · 0 GPUs**.
4. Open **Deployments**, choose a model, and review its GPU allocation. If capacity is needed, select an **AWS GPU**, review its memory and hourly price, and choose **Add GPU and deploy**. You can also add GPUs separately from **GPUs**.
5. Wait for the GPU and model to become ready. Leave the page and return while setup continues; progress is retained.
6. When the model is **Ready**, choose **Try in LiteLLM**. Sign in as `admin` with the same password. In **Chat**, select the API model name shown on the deployment page and send a message. A model response confirms your first request.

The launcher costs $0.0832/hour for compute and has a 40 GB disk. The GPU picker lists supported NVIDIA machines offered in the installation’s availability zone, with GPU count, memory, and current On-Demand price per machine. Choose up to four machines, including different GPU types; each gets a 200 GB disk. Disk, public IPv4, network, and other AWS charges are additional. The launcher keeps running when there are no GPUs.

**GPU quota:** the launcher checks the selected machine’s vCPU requirement against your remaining regional G/VT or P instance quota. If more quota is needed, choose **AWS quotas** to open the relevant limit in Service Quotas. AWS confirms spare capacity at launch; a failed launch appears on the GPUs page and can be cancelled before choosing another type.

**Removing GPUs:** open **GPUs**, remove any models using the machine, then choose **Remove**. Its compute and disk charges end once AWS finishes termination. Removing a model alone does not remove its GPU.

**Automatic removal:** GPU machines and their disks are removed four hours after the first GPU is added by default. Adding another GPU keeps that deadline. Running models stop at the deadline; the launcher and model configurations remain. Add capacity again to resume them. You can choose a different duration in the AWS template before installation.

**Removing everything:** on **GPUs**, choose **Delete installation**, then delete the CloudFormation stack in AWS. This removes the CPU machine, GPU machines, disks, database, and access configuration. Check that deletion finishes; if AWS reports `DELETE_FAILED`, resolve the named resource and retry deletion.

If setup fails, read the failed event in CloudFormation. For a quota error, request an increase in AWS Service Quotas, then launch again after approval. For a bootstrap failure, inspect the instance with AWS Systems Manager. Do not put a key in a URL or open public SSH access. Keep the original password on an existing installation; changing only the CloudFormation password parameter does not rotate every service's stored credentials.

## Your first request

1. Open **Deployments** in the launcher. Choose **Deploy a model** if deployments already exist.
2. Search Hugging Face, select a model, and review its GPU allocation. Replicas and context length are optional under **Settings**. Without a GPU, enable **Simulate without a GPU (canned responses)** there.
3. Choose **Deploy**, or **Add GPU and deploy** after selecting an **AWS GPU** and reviewing its price. Wait until the model page shows **Ready**.
4. Choose **Try in LiteLLM** and sign in as `admin` with the same admin password. In Chat, select the API model name shown on the deployment page, enter a prompt, and send it.

A response confirms the path from LiteLLM through llm-d to the model server. Simulation returns canned text; it does not validate a real model or GPU. To connect an application, create a virtual key in LiteLLM and copy the **API base** from the deployment page. Local Docker and port-forwarded Kubernetes addresses work from the same machine; configure a reachable HTTPS address for applications elsewhere.

| Tool | Local address |
| --- | --- |
| Launcher | http://localhost:8080 |
| LiteLLM | http://localhost:4000/ui |
| Grafana | http://localhost:3000 |

LiteLLM and Grafana open in new tabs. Use **Replica scheduling → Settings** to adjust inference scheduling, and **Grafana** to inspect the stack overview.

## Configuration

The default settings are enough for an ungated model that fits your GPUs. Use the options below when your installation needs them.

### Models and GPU allocation

The launcher searches the full Hugging Face catalogue and estimates GPU memory. Estimates are not a guarantee of [vLLM compatibility](https://docs.vllm.ai/en/stable/models/supported_models/). Each replica's GPUs must fit on one machine or Kubernetes node. Missing access, unsupported tasks, and known capacity blockers appear before deployment.

Gated models link to their Hugging Face terms and offer **Connect Hugging Face** on the deployment page. Accept the terms, then enter a read token. The launcher saves it server-side in a private state file (Docker) or a Kubernetes Secret. To configure a token at installation:

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
| Admin password | AWS launcher, LiteLLM, and Grafana sign-in | Chosen during AWS setup |
| LiteLLM master key | Docker/Kubernetes launcher and LiteLLM admin login | Compose `.env`, or the generated Kubernetes Secret described above |
| Postgres passwords | Stack services | Generated during installation |
| Virtual key | Application requests | Create in the LiteLLM dashboard |

To use a separate launcher password, set `LAUNCHER_PASSWORD` in the launcher's container environment. It must be a generated value of at least 20 characters. Keep the existing database and gateway credentials when restarting an installation.

### Routing, teams, and fairness

Routing works as soon as a model is deployed. Under **Replica scheduling → Settings**, adjust prefix-cache and queue-depth weights for a model. Other scorers are preserved. Applying changes restarts the router and can briefly pause requests. Custom policies without these scorers stay managed through their existing configuration.

Flow control queues requests in llm-d while replicas are saturated. The upstream default limits queue waits to 60 seconds. Keep the router’s access to model metrics working; stale metrics pause dispatch.

Create teams and keys in LiteLLM. Requests carry the team's `x-llm-d-inference-fairness-id` header to llm-d, which can use it to share capacity between teams. See [the header callback](../charts/inference-stack/files/llmd_headers.py).

## Recovery and cleanup

If deployment fails, the model page keeps your settings and offers **Retry deployment**. Open **Logs** for the cause. **Advanced** shows the deployed configuration and commands for your installation. Cancelling while an AWS GPU starts cancels deployment; check **GPUs** to remove rented capacity and stop its charges. **Remove deployment** is also available for models that are not ready.

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

Keep your existing credentials and volumes when updating to v0.3.1. Use the instructions for your installation method.

For Docker, rerun the installer from the directory containing `inference-stack`. It keeps `.env`, volumes and routing settings, recreates changed services, and keeps the previous recipe as a backup:

```bash
curl -fsSL https://github.com/BerriAI/inference-stack/releases/download/v0.3.1/install.sh | bash -s -- docker
```

For an existing Kubernetes cluster, update the chart with the new defaults and retain your explicit overrides:

```bash
helm upgrade inference-stack https://github.com/BerriAI/inference-stack/releases/download/v0.3.1/inference-stack-0.3.1.tgz \
  --namespace inference --reset-then-reuse-values --set-string launcher.image=ghcr.io/berriai/inference-stack/launcher:0.3.1 --wait
```

After the chart update, rerun the [Kubernetes setup command](#kubernetes) to reconnect. Open the launcher and LiteLLM to confirm both are reachable.

For AWS, launch a new stack with this release and a different stack name. Deploy your models and create application keys there. Keep the original installation until requests work in the new one, then [delete the original stack](#aws). Updating only the launcher image does not update its AWS infrastructure.

## Deploy with your agent

Copy the prompt for your environment:

<details>
<summary>Docker Compose</summary>

```text
Set up Inference Stack v0.3.1 on this machine with Docker Compose.
Read https://github.com/BerriAI/inference-stack/blob/v0.3.1/docs/setup.md
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
Set up Inference Stack v0.3.1 in Kubernetes.
Read https://github.com/BerriAI/inference-stack/blob/v0.3.1/docs/setup.md
and inspect its release installer before running it. Confirm the current
cluster is the intended target, check permissions and GPU capacity, and
preserve existing releases and credentials. Keep secrets out of chat and
logs. Install the stack, verify its pods, and open the launcher through
port-forwarding. Help me deploy a model and send a request in LiteLLM.
Use simulation only if I choose it. Ask before provisioning paid resources.
```

</details>

<details>
<summary>AWS</summary>

```text
Set up Inference Stack v0.3.1 in my AWS account.
Read https://github.com/BerriAI/inference-stack/blob/v0.3.1/docs/setup.md#aws
and inspect the release template. Confirm the account, permissions, and
cost before creating the stack. Keep credentials out of chat and logs.
Open the empty launcher, help me choose a GPU and model, and ask before
starting paid GPU capacity. Verify a response in LiteLLM Chat and show
me how to remove the GPU and delete the installation.
```

</details>

## Development

Keep the launcher as FastAPI with plain HTML, CSS, and JavaScript. Integrations are ordinary modules; deployment configuration stays in Compose and Helm.

From the repository root, with Python 3.12, Helm, and Docker installed:

```bash
python3 -m venv .venv
.venv/bin/pip install -r launcher/requirements.txt
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana-community https://grafana-community.github.io/helm-charts
helm dependency build charts/model
helm dependency build charts/inference-stack
.venv/bin/python -m unittest discover -s tests
docker build -f launcher/Dockerfile -t inference-stack-launcher:dev .
```

| Path | Contents |
| --- | --- |
| `compose/` | Docker Compose stack and configuration |
| `launcher/` | Web app and deployment integrations |
| `aws/` | CloudFormation, bootstrap, and GPU capacity management |
| `charts/inference-stack/` | LiteLLM, Postgres, Prometheus, Grafana, and launcher |
| `charts/model/` | One model's vLLM deployment, llm-d router, and LiteLLM registration |

The browser regression tests use Playwright with Chromium:

```bash
npm install --prefix /tmp/inference-stack-browser-tests --no-package-lock playwright@1.62.1
/tmp/inference-stack-browser-tests/node_modules/.bin/playwright install chromium
NODE_PATH=/tmp/inference-stack-browser-tests/node_modules node --test tests/test_model_settings.cjs
```

The Grafana dashboards in `charts/inference-stack/files/dashboards/` are shared by Compose and Helm. LiteLLM and llm-d dashboards come from their upstream repositories.
