import json
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml

from test_launcher import app


class ChartTests(unittest.TestCase):
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
            for volume in router["spec"]["template"]["spec"].get("volumes", []):
                if "configMap" in volume:
                    self.assertIn(volume["configMap"]["name"], configs)
