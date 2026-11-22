<div align="center">

<p>
  <a href="https://huggingface.co/models"><img src="assets/hugging-face.svg" alt="Hugging Face" width="48" height="48"></a>&nbsp;&nbsp;&nbsp;
  <a href="https://github.com/vllm-project/vllm"><img src="launcher/static/logos/vllm.png" alt="vLLM" width="46" height="48"></a>&nbsp;&nbsp;&nbsp;
  <a href="https://github.com/llm-d/llm-d"><img src="launcher/static/logos/llmd.png" alt="llm-d" width="42" height="48"></a>&nbsp;&nbsp;&nbsp;
  <a href="https://github.com/BerriAI/litellm"><img src="launcher/static/logos/litellm.svg" alt="LiteLLM" width="48" height="48"></a>&nbsp;&nbsp;&nbsp;
  <a href="https://grafana.com/oss/grafana/"><img src="launcher/static/logos/grafana.svg" alt="Grafana" width="46" height="48"></a>
</p>

# Inference Stack

### Everything you need to own your inference. Deploy with one command.

**[Set up with Docker](#docker-compose)** &nbsp; · &nbsp; **[Set up with Kubernetes](#kubernetes)**

</div>

<img src="assets/stack-flow.gif" alt="Apps send requests to LiteLLM for model routing, then llm-d selects a vLLM replica. Hugging Face supplies model weights; Grafana shows metrics collected by Prometheus." width="100%">

<a id="get-started"></a>

## Quickstart

Use [Docker Compose](docs/setup.md#docker-compose) on one machine, or [Kubernetes with Helm](docs/setup.md#kubernetes) on a cluster. Real models need NVIDIA GPUs; simulation works without one.

### Docker Compose

```bash
curl -fsSL https://github.com/BerriAI/inference-stack/releases/download/v0.1.2/install.sh | bash -s -- docker
```

### Kubernetes

```bash
curl -fsSL https://github.com/BerriAI/inference-stack/releases/download/v0.1.2/install.sh | bash -s -- kubernetes
```

Open [localhost:8080](http://localhost:8080) and sign in as `admin`. The installer tells you where to find the generated password.

**vLLM → choose a model → Deploy → Open LiteLLM → Playground.** Sign in to LiteLLM with the same password, select your model, and send a prompt.

No GPU? Enable **Simulate without a GPU** in the model's **Settings** for canned responses.

[Setup and prerequisites](docs/setup.md) · [Deploy with your agent](docs/setup.md#deploy-with-your-agent)

## The stack

### vLLM

Runs models from Hugging Face on your GPUs.

<img src="assets/vllm.png" alt="Search the Hugging Face catalogue and compare estimated GPU memory before deployment" width="100%">

### LiteLLM

Routes requests to your models and manages API keys, teams, and budgets.

<img src="assets/litellm.png" alt="LiteLLM Playground sending a request to a locally deployed simulated model" width="100%">

### llm-d

Picks a vLLM replica for each request using cache and queue data.

<img src="assets/llmd.png" alt="Routing across two simulated replicas, with optional cache and queue weight settings" width="100%">

### Grafana

Shows model health, request traffic, and response times.

<img src="assets/grafana.png" alt="Grafana stack overview showing the local simulation's model servers and request traffic" width="100%">
<br>

[Setup and configuration](docs/setup.md) · [Releases](https://github.com/BerriAI/inference-stack/releases) · [Development](docs/setup.md#development) · [MIT license](LICENSE)
