import json
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

from test_launcher import app


class ChartTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("helm"), "Helm is required for chart contract checks")
    def test_grafana_image_and_dashboard_provisioning(self):
        root = Path(__file__).resolve().parents[1]
        chart = root / "charts/inference-stack"
        compose = yaml.safe_load((root / "compose/docker-compose.yaml").read_text())
        compose_image = compose["services"]["grafana"]["image"]
        result = subprocess.run(
            ["helm", "template", "inference-stack", str(chart),
             "--set", "grafana.persistence.enabled=true",
             "--set-string", "litellm.postgresql.auth.password=test-only-database-password-0123456789",
             "--set-string", "litellm.postgresql.auth.postgres-password=test-only-database-admin-0123456789"],
            text=True, capture_output=True, timeout=20, check=True,
        )
        resources = {(r["kind"], r["metadata"]["name"]): r for r in yaml.safe_load_all(result.stdout) if r}
        pod = resources["Deployment", "inference-stack-grafana"]["spec"]["template"]["spec"]
        grafana = next(c for c in pod["containers"] if c["name"] == "grafana")
        self.assertEqual(grafana["image"].removeprefix("docker.io/").removesuffix("-distroless"), compose_image)
        init_chown = next(c for c in pod["initContainers"] if c["name"] == "init-chown-data")
        self.assertRegex(init_chown["image"], r"^docker\.io/library/busybox:1\.36\.1@sha256:[0-9a-f]{64}$")

        provisioning = resources["ConfigMap", "inference-stack-grafana"]["data"]
        provider = yaml.safe_load(provisioning["dashboardproviders.yaml"])["providers"][0]
        volumes = {v["name"]: v for v in pod["volumes"]}
        mount = next(m for m in grafana["volumeMounts"] if m["mountPath"] == provider["options"]["path"])
        dashboards = resources["ConfigMap", volumes[mount["name"]]["configMap"]["name"]]["data"]
        self.assertEqual({json.loads(v)["title"] for v in dashboards.values()},
                         {json.loads(p.read_text())["title"] for p in (chart / "files/dashboards").rglob("*.json")})
        datasource = yaml.safe_load(provisioning["datasources.yaml"])["datasources"][0]
        self.assertEqual((datasource["uid"], datasource["type"], datasource["url"]),
                         ("prometheus", "prometheus", "http://inference-stack-prometheus"))

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
    def test_lens_runs_standalone_by_default_and_launcher_links_to_it(self):
        chart = Path(__file__).resolve().parents[1] / "charts/inference-stack"
        auth = {"litellm": {"postgresql": {"auth": {"password": "test-only-database-password-0123456789", "postgres-password": "test-only-database-admin-0123456789"}}}}
        for enabled in (True, False):
            values = {**auth, **({} if enabled else {"lens": {"enabled": False}})}
            result = subprocess.run(["helm", "template", "inference-stack", str(chart), "--values", "-"],
                                    input=json.dumps(values), text=True, capture_output=True, timeout=20, check=True)
            resources = [r for r in yaml.safe_load_all(result.stdout) if r]
            names = {(r["kind"], r["metadata"]["name"]) for r in resources}
            launcher = next(r for r in resources if r["kind"] == "Deployment" and r["metadata"]["name"] == "inference-stack-launcher")
            environment = {v["name"]: v.get("value") for v in launcher["spec"]["template"]["spec"]["containers"][0]["env"]}
            self.assertEqual(("Service", "inference-stack-lens") in names, enabled)
            self.assertEqual(("StatefulSet", "inference-stack-lens-clickhouse") in names, enabled)
            if not enabled:
                self.assertEqual(environment["LENS_PUBLIC_URL"], "")
                continue
            service = next(r for r in resources if r["kind"] == "Service" and r["metadata"]["name"] == "inference-stack-lens")
            self.assertEqual(environment["LENS_URL"], f"http://inference-stack-lens:{service['spec']['ports'][0]['port']}")
            self.assertEqual(environment["LENS_PUBLIC_URL"], "http://localhost:4318")
            lens = next(r for r in resources if r["kind"] == "Deployment" and r["metadata"]["name"] == "inference-stack-lens")
            container = lens["spec"]["template"]["spec"]["containers"][0]
            self.assertRegex(container["image"], r"^ghcr\.io/berriai/lens@sha256:[0-9a-f]{64}$")
            self.assertEqual({v["name"]: v.get("value") for v in container["env"]}["LENS_MODE"], "standalone")

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
