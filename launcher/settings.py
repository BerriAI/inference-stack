import os

MODE = os.environ.get("MODE", "compose")
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = os.environ["LITELLM_MASTER_KEY"]
ROUTER_URL = os.environ.get("ROUTER_URL", "http://envoy:8081")
ROUTER_HEALTH_URL = os.environ.get("ROUTER_HEALTH_URL", "http://envoy:19000/ready")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana:3000")
LITELLM_PUBLIC_URL = os.environ.get("LITELLM_PUBLIC_URL", "http://localhost:4000")
GRAFANA_PUBLIC_URL = os.environ.get("GRAFANA_PUBLIC_URL", "http://localhost:3000")
HF_TOKEN = os.environ.get("HF_TOKEN", "")

STATE_DIR = os.environ.get("STATE_DIR", "/state")
DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "inference-stack")
VLLM_IMAGE = os.environ.get("VLLM_IMAGE", "vllm/vllm-openai:v0.31.0")
SIMULATOR_IMAGE = os.environ.get("SIMULATOR_IMAGE", "ghcr.io/llm-d/llm-d-inference-sim:v1.2.0")

HELM_MODEL_CHART = os.environ.get("HELM_MODEL_CHART", "./charts/model")
HELM_STACK_RELEASE = os.environ.get("HELM_STACK_RELEASE", "inference-stack")
HELM_NAMESPACE = os.environ.get("HELM_NAMESPACE", "inference")
