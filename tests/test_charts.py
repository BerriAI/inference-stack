import json
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

from test_launcher import app


class ChartTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("helm"), "Helm is required for chart contract checks")
    def test_mixed_gpu_fleet_schedules_on_matching_capacity(self):
        chart = Path(__file__).resolve().parents[1] / "charts/model"
        for simulated in (False, True):
            request = app.AddModel(model="example/model", simulate=simulated, add_gpu=True,
                                   instance_type="g6e.12xlarge", min_gpu_memory_mib=40000, gpus_per_replica=4)
            result = subprocess.run(["helm", "template", "model", str(chart), "--values", "-"],
                                    input=json.dumps(app.kubernetes.values(request, "model")),
                                    text=True, capture_output=True, timeout=20, check=True)
            resources = [r for r in yaml.safe_load_all(result.stdout) if r]
            pod = next(r for r in resources if r["kind"] == "Deployment" and r["metadata"]["name"] == "model-vllm")["spec"]["template"]["spec"]
            if simulated:
                self.assertNotIn("affinity", pod)
                self.assertNotIn("nodeSelector", pod)
            else:
                self.assertEqual(pod["nodeSelector"], {"node.kubernetes.io/instance-type": "g6e.12xlarge"})
                expressions = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0]["matchExpressions"]
                self.assertEqual(expressions, [{"key": "nvidia.com/gpu.memory", "operator": "Gt", "values": ["39999"]}])
                self.assertEqual(pod["containers"][0]["resources"]["limits"]["nvidia.com/gpu"], 4)

    @unittest.skipUnless(shutil.which("helm"), "Helm is required for chart contract checks")
    def test_router_tokenizer_uses_the_same_vllm_release_as_the_model_server(self):
        chart = Path(__file__).resolve().parents[1] / "charts/model"
        values = {"model": "Qwen/Qwen3-0.6B", "llm-d-router-standalone": {"router": {
            "modelServers": {"matchLabels": {"model": "model"}}, "tokenizer": {"enabled": True, "modelName": "Qwen/Qwen3-0.6B"}}}}
        result = subprocess.run(["helm", "template", "model", str(chart), "--values", "-"],
                                input=json.dumps(values), text=True, capture_output=True, timeout=20, check=True)
        images = [c["image"] for r in yaml.safe_load_all(result.stdout) if r and r["kind"] == "Deployment"
                  for c in r["spec"]["template"]["spec"]["containers"]]
        vllm = next(i for i in images if i.startswith("vllm/vllm-openai:"))
        self.assertIn("docker.io/vllm/vllm-openai-cpu:" + vllm.split(":")[1], images)

    @unittest.skipUnless(shutil.which("helm"), "Helm is required for chart contract checks")
    def test_aws_host_access_is_opt_in_and_uses_admin_secret(self):
        chart = Path(__file__).resolve().parents[1] / "charts/inference-stack"
        for enabled in (False, True):
            values = {"litellm": {"postgresql": {"auth": {"password": "test-only-database-password-0123456789", "postgres-password": "test-only-database-admin-0123456789"}}},
                      "launcher": {"awsCapacity": enabled, "adminSecret": "test-only-admin" if enabled else "", "nodeSelector": {"inference-stack/role": "control"} if enabled else {}}}
            result = subprocess.run(["helm", "template", "inference-stack", str(chart), "--values", "-"],
                                    input=json.dumps(values), text=True, capture_output=True, timeout=20, check=True)
            resources = [r for r in yaml.safe_load_all(result.stdout) if r]
            launcher = next(r for r in resources if r["kind"] == "Deployment" and r["metadata"]["name"] == "inference-stack-launcher")["spec"]["template"]["spec"]
            environment = {v["name"]: v for v in launcher["containers"][0]["env"]}
            self.assertEqual("AWS_CAPACITY_SOCKET" in environment, enabled)
            self.assertEqual(any("hostPath" in v for v in launcher["volumes"]), enabled)
            if enabled:
                self.assertEqual(launcher["nodeSelector"], {"inference-stack/role": "control"})
                self.assertEqual(environment["LAUNCHER_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"], "test-only-admin")

    @unittest.skipUnless(shutil.which("helm"), "Helm is required for chart contract checks")
    def test_each_model_mounts_its_own_rendered_router_configuration(self):
        chart = Path(__file__).resolve().parents[1] / "charts/model"
        request = app.AddModel(model="Qwen/Qwen3-0.6B", simulate=True)
        for name in ("first-model", "second-model"):
            result = subprocess.run(
                ["helm", "template", name, str(chart), "--values", "-"],
                input=json.dumps(app.kubernetes.values(request, name)),
                text=True, capture_output=True, timeout=20, check=True,
            )
            resources = [r for r in yaml.safe_load_all(result.stdout) if r]
            server = next(r for r in resources if r["kind"] == "Deployment" and r["metadata"]["name"] == f"{name}-vllm")
            # Allocation validation reuses this model's GPUs; updates must release them first.
            self.assertEqual(server["spec"]["strategy"]["type"], "Recreate")
            configs = {r["metadata"]["name"] for r in resources if r["kind"] == "ConfigMap"}
            self.assertTrue(configs)
            self.assertTrue(all(config.startswith(name) for config in configs))
            router = next(r for r in resources if r["kind"] == "Deployment" and r["metadata"]["name"] == f"{name}-epp")
            epp = next(c for c in router["spec"]["template"]["spec"]["containers"] if c["name"] == "epp")
            self.assertIn("--feature-gates=flowControl=true", epp["args"])
            for volume in router["spec"]["template"]["spec"].get("volumes", []):
                if "configMap" in volume:
                    self.assertIn(volume["configMap"]["name"], configs)
