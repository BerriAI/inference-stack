import os


def validate_password(name: str, value: str) -> str:
    placeholders = ("change-me", "changeme", "replace-me", "notagreatpassword")
    if len(value) < 20 or len(set(value)) < 5 or any(p in value.lower() for p in placeholders):
        raise ValueError(f"{name} must be a generated password of at least 20 characters; example passwords are not accepted.")
    return value


MODE = os.environ.get("MODE", "kubernetes" if os.environ.get("KUBERNETES_SERVICE_HOST") else "compose")
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")
LITELLM_MASTER_KEY = validate_password("LITELLM_MASTER_KEY", os.environ["LITELLM_MASTER_KEY"])
LAUNCHER_PASSWORD = validate_password("LAUNCHER_PASSWORD", os.environ.get("LAUNCHER_PASSWORD", LITELLM_MASTER_KEY))
ROUTER_URL = os.environ.get("ROUTER_URL", "http://envoy:8081")
ROUTER_HEALTH_URL = os.environ.get("ROUTER_HEALTH_URL", "http://envoy:19000/ready")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana:3000")
LITELLM_PUBLIC_URL = os.environ.get("LITELLM_PUBLIC_URL", "http://localhost:4000").rstrip("/")
GRAFANA_PUBLIC_URL = os.environ.get("GRAFANA_PUBLIC_URL", "http://localhost:3000").rstrip("/")
LENS_URL = os.environ.get("LENS_URL", "http://lens:4318")
LENS_PUBLIC_URL = os.environ.get("LENS_PUBLIC_URL", "http://localhost:4318").rstrip("/")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
HF_SECRET = os.environ.get("HF_SECRET", "")
STATE_DIR = os.environ.get("STATE_DIR", "/state")
HF_TOKEN_FILE = os.environ.get("HF_TOKEN_FILE", STATE_DIR + "/huggingface-token")
MACHINE_INFO = os.environ.get("MACHINE_INFO", "")
AWS_CAPACITY_SOCKET = os.environ.get("AWS_CAPACITY_SOCKET", "")
DOCKER_NETWORK = os.environ.get("DOCKER_NETWORK", "inference-stack")
VLLM_IMAGE = os.environ.get("VLLM_IMAGE", "vllm/vllm-openai:v0.31.0")
SIMULATOR_IMAGE = os.environ.get("SIMULATOR_IMAGE", "ghcr.io/llm-d/llm-d-inference-sim:v1.2.0")
HELM_MODEL_CHART = os.environ.get("HELM_MODEL_CHART", "./charts/model")
BUNDLED_MODEL_CHART = os.environ.get("BUNDLED_MODEL_CHART", "/opt/charts/model")
HELM_STACK_RELEASE = os.environ.get("HELM_STACK_RELEASE", "inference-stack")
HELM_NAMESPACE = os.environ.get("HELM_NAMESPACE", "inference")
ROUTING_CONFIG = os.environ.get("ROUTING_CONFIG", "/config/epp-config.yaml")
