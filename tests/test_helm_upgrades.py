"""Exercise Helm upgrades on a disposable kind cluster; no GPUs or model pulls."""
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from test_launcher import app


@unittest.skipUnless(os.environ.get("INFERENCE_STACK_TEST_KUBECONFIG"), "Requires a disposable kind cluster")
class HelmUpgradeTests(unittest.TestCase):
    def test_bundled_postgresql_upgrade_preserves_data_and_credentials(self):
        env = {**os.environ, "KUBECONFIG": os.environ["INFERENCE_STACK_TEST_KUBECONFIG"]}
        context = subprocess.check_output(["kubectl", "config", "current-context"], env=env, text=True).strip()
        self.assertTrue(context.startswith("kind-inference-stack-"), "Use a disposable Inference Stack kind cluster")

        namespace = "inference-stack-postgresql-upgrade-tests"
        name = "stack-pg-upgrade"
        database = f"{name}-postgresql"
        pod = f"{database}-0"
        claim = f"data-{pod}"
        password = "test-only-database-password-0123456789"
        values = {
            "litellm": {
                "replicaCount": 0,
                "migrationJob": {"enabled": False},
                "postgresql": {"auth": {"password": password, "postgres-password": "test-only-database-admin-0123456789"}},
            },
            "launcher": {"nodeSelector": {"inference-stack-test": "unscheduled"}},
            "lens": {"enabled": False},
            "grafana": {"replicas": 0, "rbac": {"create": False}},
            "prometheus": {"server": {"enabled": False}},
        }

        def run(args, values=None):
            result = subprocess.run(args, env=env, input=json.dumps(values) if values is not None else None,
                                    capture_output=True, text=True, timeout=210)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        def kubectl(*args):
            return run(["kubectl", "-n", namespace, *args])

        def get(kind, name):
            return json.loads(kubectl("get", kind, name, "-o", "json"))

        def sql(statement):
            return kubectl("exec", pod, "--", "env", f"PGPASSWORD={password}", "psql", "-XAt",
                           "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1", "-U", "litellm", "-d", "litellm",
                           "-c", statement)

        chart = Path(__file__).resolve().parents[1] / "charts/inference-stack"
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "old"
            shutil.copytree(chart, old)
            dependencies = old / "charts"
            (dependencies / "litellm-helm-1.110.2.tgz").unlink()
            run(["helm", "pull", "oci://ghcr.io/berriai/litellm-helm", "--version", "1.110.2",
                 "--destination", str(dependencies)])

            try:
                run(["helm", "install", name, str(old), "-n", namespace, "--create-namespace", "--no-hooks",
                     "-f", "-"], values)
                kubectl("rollout", "status", f"statefulset/{database}", "--timeout=180s")
                before = get("statefulset", database)["metadata"]
                self.assertEqual(before["labels"]["helm.sh/chart"], "postgresql-14.3.1")
                old_pod = get("pod", pod)["metadata"]["uid"]
                old_claim = get("pvc", claim)["metadata"]["uid"]
                old_credentials = get("secret", database)["data"]
                self.assertEqual(sql("SELECT current_setting('server_version_num')::int / 10000"), "16")
                sql("CREATE TABLE upgrade_check (id int PRIMARY KEY, note text);"
                    "INSERT INTO upgrade_check VALUES (1, 'before'), (2, 'retained');")

                run(["helm", "upgrade", name, str(chart), "-n", namespace, "--reset-then-reuse-values", "--no-hooks"])
                kubectl("rollout", "status", f"statefulset/{database}", "--timeout=180s")
                after = get("statefulset", database)["metadata"]
                self.assertEqual(after["labels"]["helm.sh/chart"], "postgresql-18.12.27")
                self.assertEqual(after["uid"], before["uid"])
                self.assertNotEqual(get("pod", pod)["metadata"]["uid"], old_pod)
                self.assertEqual(get("pvc", claim)["metadata"]["uid"], old_claim)
                self.assertTrue(get("secret", database)["data"] == old_credentials, "Database credentials changed")
                self.assertEqual(sql("SELECT current_setting('server_version_num')::int / 10000"), "16")
                self.assertEqual(sql("SELECT * FROM upgrade_check ORDER BY id"), "1|before\n2|retained")
            finally:
                run(["helm", "uninstall", name, "-n", namespace, "--no-hooks", "--ignore-not-found"])
                kubectl("delete", "namespace", namespace, "--ignore-not-found", "--timeout=60s")

    def test_current_defaults_and_explicit_overrides_survive_all_launcher_upgrade_paths(self):
        env = {**os.environ, "KUBECONFIG": os.environ["INFERENCE_STACK_TEST_KUBECONFIG"]}
        context = subprocess.check_output(["kubectl", "config", "current-context"], env=env, text=True).strip()
        self.assertTrue(context.startswith("kind-inference-stack-"), "Use a disposable Inference Stack kind cluster")

        def run(args, values=None):
            result = subprocess.run(args, env=env, input=json.dumps(values) if values is not None else None,
                                    capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, "Helm fixture operation failed")
            return result.stdout

        def deployments(manifest):
            return {r["metadata"]["name"].rsplit("-", 1)[-1]: r["spec"]["template"]["spec"]
                    for r in yaml.safe_load_all(manifest) if r and r["kind"] == "Deployment"}

        def images(rows):
            return {c["name"]: c["image"] for row in rows.values() for c in row["containers"]}

        chart = Path(__file__).resolve().parents[1] / "charts/model"
        namespace = "inference-stack-upgrade-tests"
        request = app.AddModel(model="example/model", replicas=2, context=8192)
        with tempfile.TemporaryDirectory() as directory:
            old = Path(directory) / "old"
            shutil.copytree(chart, old)
            defaults = yaml.safe_load((old / "values.yaml").read_text())
            defaults["vllmImage"] = "example.invalid/vllm:old"
            router = defaults["llm-d-router-standalone"]["router"]
            router["epp"].pop("flags", None)
            router["epp"]["image"] = {"registry": "example.invalid", "repository": "epp", "tag": "old"}
            router["proxy"]["image"] = "example.invalid/envoy:old"
            (old / "values.yaml").write_text(yaml.safe_dump(defaults))

            with patch.object(app.settings, "HELM_NAMESPACE", namespace), \
                 patch.object(app.settings, "HELM_MODEL_CHART", str(chart)), \
                 patch.object(app.settings, "BUNDLED_MODEL_CHART", str(chart)):
                try:
                    for operation in ("baseline", "deploy", "routing", "command", "pinned"):
                        with self.subTest(operation=operation):
                            name = "upgrade-" + operation
                            saved = app.kubernetes.values(request, name)
                            # Keep fixture pods unscheduled: this test checks Helm merging, not inference.
                            saved["nodeSelector"] = {"inference-stack-test": "unscheduled"}
                            epp = saved["llm-d-router-standalone"]["router"].setdefault("epp", {})
                            epp.update(nodeSelector={"inference-stack-test": "unscheduled"},
                                       resources={"limits": {"memory": "3Gi"}})
                            if operation == "pinned":
                                saved["vllmImage"] = "example.invalid/vllm:pinned"
                                epp["image"] = {"registry": "example.invalid", "repository": "epp", "tag": "pinned"}
                                saved["llm-d-router-standalone"]["router"]["proxy"]["image"] = "example.invalid/envoy:pinned"
                            run(["helm", "install", name, str(old), "-n", namespace, "--create-namespace", "--no-hooks", "-f", "-"], saved)
                            before = deployments(run(["helm", "get", "manifest", name, "-n", namespace]))

                            def upgrade(args, values_input=None):
                                # Readiness and registration are separate runtime checks. No fixture image runs.
                                return run(["helm", *[a for a in args if a != "--wait"],
                                            "--no-hooks", "--namespace", namespace], values_input)

                            with patch.object(app.kubernetes, "helm", side_effect=upgrade):
                                if operation == "baseline":
                                    upgrade(["upgrade", name, str(chart), "--reuse-values", "-f", "-"], saved)
                                elif operation == "routing":
                                    policy = {"apiVersion": "llm-d.ai/v1", "kind": "EndpointPickerConfig",
                                              "plugins": [{"type": "queue-scorer"}],
                                              "schedulingProfiles": [{"name": "default", "plugins": [{"pluginRef": "queue-scorer", "weight": 7}]}]}
                                    app.kubernetes.save_routing(name, policy)
                                elif operation == "command":
                                    args = shlex.split(app.kubernetes.command(request, name).splitlines()[1])
                                    run([*[a for a in args if a != "--wait"], "--no-hooks"])
                                else:
                                    app.kubernetes.deploy(request, name)

                            after = deployments(run(["helm", "get", "manifest", name, "-n", namespace]))
                            effective = copy.deepcopy(saved)
                            if operation in ("deploy", "command", "pinned"):
                                effective["nodeSelector"] = {}
                            expected = deployments(run(["helm", "template", name, str(chart), "-n", namespace, "-f", "-"], effective))
                            self.assertEqual(images(after), images(before) if operation == "baseline" else images(expected))
                            epp = next(c for c in after["epp"]["containers"] if c["name"] == "epp")
                            self.assertEqual(epp["resources"]["limits"]["memory"], "3Gi")
                            self.assertIn("8192", after["vllm"]["containers"][0]["command"])
                            if operation != "baseline":
                                self.assertIn("--feature-gates=flowControl=true", epp["args"])
                            if operation == "routing":
                                values = yaml.safe_load(run(["helm", "get", "values", name, "-n", namespace, "-o", "yaml"]))
                                self.assertEqual(values["llm-d-router-standalone"]["router"]["epp"]["pluginsConfig"], policy)
                            run(["helm", "uninstall", name, "-n", namespace, "--no-hooks"])
                finally:
                    run(["kubectl", "delete", "namespace", namespace, "--ignore-not-found", "--timeout=60s"])
