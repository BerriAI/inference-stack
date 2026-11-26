import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("LITELLM_MASTER_KEY", "test-only-not-a-real-key-0123456789")
os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="inference-stack-tests-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "launcher"))
import app
import catalog
from fastapi.testclient import TestClient


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app.app)
        self.client.auth = ("admin", app.settings.LAUNCHER_PASSWORD)
        app.deployment.update(action="", name="", step="", error="", request=None)
        if app.operation_lock.locked():
            app.operation_lock.release()

    def test_status_links_to_lens_ui_only_when_lens_is_installed(self):
        probed = []
        def healthy(url):
            probed.append(url)
            return "lens" not in url
        with patch.object(app, "is_healthy", side_effect=healthy), \
             patch.object(app.litellm_client, "list_models", return_value=[]), \
             patch.object(app, "gpu_inventory", return_value={"count": 0, "devices": []}):
            with patch.object(app.settings, "LENS_URL", "http://lens.internal:4318"), \
                 patch.object(app.settings, "LITELLM_PUBLIC_URL", "https://stack.example/litellm"), \
                 patch.object(app.settings, "LENS_PUBLIC_URL", "https://traces.example"):
                lens = self.client.get("/api/status").json()["components"]["lens"]
            self.assertEqual(lens, {"healthy": False, "url": "https://stack.example/litellm/ui/lens/"})
            self.assertIn("http://lens.internal:4318/health/ready", probed)
            with patch.object(app.settings, "LENS_PUBLIC_URL", ""):
                self.assertNotIn("lens", self.client.get("/api/status").json()["components"])

    def test_admin_access_is_required_for_reads_and_mutations(self):
        anonymous = TestClient(app.app)
        for method, path, body in [("GET", "/api/status", None), ("POST", "/api/models", {"model":"org/model","simulate":True}), ("DELETE", "/api/models/example", None),
                                   ("POST", "/api/gpus", {"expected": 0, "desired": 1}), ("DELETE", "/api/gpus/i-0123456789abcdef0", None)]:
            response = anonymous.request(method, path, json=body)
            self.assertEqual(response.status_code, 401)
            self.assertIn("Basic", response.headers["www-authenticate"])
        self.assertEqual(anonymous.get("/healthz").status_code, 200)
        self.assertEqual(anonymous.get("/api/status", auth=("admin", "incorrect")).status_code, 401)
        self.assertEqual(anonymous.get("/api/status", headers={"Authorization":"Basic invalid"}).status_code, 401)

    def test_gpu_changes_are_disabled_without_aws_and_reject_concurrent_deployment(self):
        with patch.object(app.settings, "AWS_CAPACITY_SOCKET", ""):
            self.assertEqual(self.client.post('/api/gpus', json={'expected': 0, 'desired': 1}).status_code, 404)
        app.operation_lock.acquire()
        with patch.object(app, 'capacity_request') as request:
            self.assertEqual(self.client.post('/api/gpus', json={'expected': 0, 'desired': 1}).status_code, 409)
            self.assertEqual(self.client.delete('/api/gpus/i-0123456789abcdef0').status_code, 409)
            request.assert_not_called()
        app.operation_lock.release()

    def test_gpu_api_preserves_quota_errors_and_releases_operation_lock(self):
        with patch.object(app, 'capacity_request', side_effect=app.HTTPException(409, 'GPU quota is zero. No GPU was added.')):
            response = self.client.post('/api/gpus', json={'expected': 0, 'desired': 1})
        self.assertEqual(response.status_code, 409)
        self.assertIn('No GPU was added', response.json()['detail'])
        self.assertFalse(app.operation_lock.locked())

    def test_example_passwords_are_rejected(self):
        for value in ("", "change-me", "sk-change-me", "NoTaGrEaTpAsSwOrD", "a" * 40):
            with self.assertRaises(ValueError):
                app.settings.validate_password("EXAMPLE", value)
        self.assertEqual(app.settings.validate_password("EXAMPLE", "test-only-not-a-real-key-0123456789"), "test-only-not-a-real-key-0123456789")

    def test_browser_login_uses_cookie_without_repeated_password_dialogs(self):
        browser = TestClient(app.app, base_url="https://testserver")
        response = browser.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        self.assertEqual(browser.get("/login").status_code, 200)
        response = browser.get("/api/status", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("www-authenticate", response.headers)
        self.assertEqual(browser.post("/api/login", json={"password": "incorrect"}).status_code, 401)
        oversized = "test-only-private-value-" * 200
        response = browser.post("/api/login", json={"password": oversized})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("test-only-private-value", response.text)
        response = browser.post("/api/login", json={"password": app.settings.LAUNCHER_PASSWORD}, headers={"Origin": "https://testserver"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("Secure", response.headers["set-cookie"])
        self.assertIn("SameSite=lax", response.headers["set-cookie"])
        self.assertEqual(browser.get("/").status_code, 200)
        self.assertEqual(browser.post("/api/logout", headers={"Origin": "https://testserver"}).status_code, 200)
        self.assertEqual(browser.get("/api/status", headers={"Sec-Fetch-Site": "same-origin"}).status_code, 401)

    def test_session_expiry_tampering_and_cross_origin_login(self):
        value = f"{int(app.time.time()) + 60}.test-nonce"
        signed = f"{value}.{app.session_signature(value)}"
        self.assertTrue(app.valid_session(signed))
        self.assertFalse(app.valid_session(signed + "tampered"))
        with patch.object(app.time, "time", return_value=app.time.time() + 61):
            self.assertFalse(app.valid_session(signed))
        with patch.object(app.settings, "LAUNCHER_PASSWORD", "test-only-changed-password-0123456789"):
            self.assertFalse(app.valid_session(signed))
        response = self.client.post("/api/login", json={"password": app.settings.LAUNCHER_PASSWORD}, headers={"Origin": "https://example.com"})
        self.assertEqual(response.status_code, 403)

    def test_gpu_memory_use_is_reported_separately_from_detected_devices(self):
        from unittest.mock import Mock
        docker = Mock()
        docker.info.return_value = {"Runtimes": {"nvidia": {}}}
        probe = docker.containers.run.return_value
        probe.wait.return_value = {"StatusCode": 0}
        probe.logs.return_value = b"NVIDIA A10G, 23028, 10240\n"
        with patch.object(app.compose_backend, "_docker", return_value=docker):
            hardware = app.compose_backend.gpu_inventory()
        self.assertEqual(hardware["count"], 1)
        self.assertEqual(hardware["devices"][0]["used_gb"], 10)
        self.assertEqual(hardware["devices"][0]["memory_gb"], 22.5)
        probe.remove.assert_called_once_with(force=True)

    def test_removal_can_retry_registration_cleanup_after_workload_is_gone(self):
        app.deployment.update(action="remove", name="example", error="Removal failed")
        with patch.object(app.settings, "MODE", "kubernetes"), \
             patch.object(app, "managed_model", side_effect=app.HTTPException(404, "Gone")), \
             patch.object(app.kubernetes, "permissions", return_value={"deploy": True}), \
             patch.object(app.kubernetes, "remove") as uninstall, \
             patch.object(app.litellm_client, "remove_model") as unregister:
            response = self.client.delete("/api/models/example")
        self.assertEqual(response.status_code, 200)
        uninstall.assert_called_once_with("example")
        unregister.assert_called_once_with("example")
        self.assertFalse(app.deployment["error"])

    def test_legacy_kubernetes_models_remain_visible_and_removable(self):
        name = "legacy-model"
        server = {
            "metadata": {"name": f"{name}-vllm", "generation": 1,
                         "labels": {"app.kubernetes.io/managed-by": "Helm"},
                         "annotations": {"meta.helm.sh/release-name": name,
                                         "meta.helm.sh/release-namespace": app.settings.HELM_NAMESPACE}},
            "spec": {"replicas": 2, "selector": {"matchLabels": {"model": name}},
                     "template": {"spec": {"containers": [{"name": "vllm",
                         "command": ["vllm", "serve", "org/legacy-model", "--port", "8000"],
                         "resources": {"limits": {"nvidia.com/gpu": 1}}}]}}},
            "status": {"observedGeneration": 1, "readyReplicas": 2},
        }
        router = {"metadata": {"name": f"{name}-epp"}, "status": {"readyReplicas": 1}}
        registered = [{"name": name, "served_model": "org/legacy-model",
                       "api_base": f"http://{name}-epp:8081/v1"}]
        with patch.object(app.settings, "MODE", "kubernetes"), \
             patch.object(app.kubernetes, "api", return_value={"items": [server, router]}), \
             patch.object(app.litellm_client, "list_models", return_value=registered), \
             patch.object(app, "is_healthy", return_value=True), \
             patch.object(app, "gpu_inventory", return_value={"count": 2, "devices": []}), \
             patch.object(app.kubernetes, "permissions", return_value={"read": True, "deploy": False}):
            rows = self.client.get("/api/status").json()["models"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["name"], name)
            self.assertEqual(rows[0]["status"], "ready")
            self.assertEqual(rows[0]["replicas"], 2)
            response = self.client.delete(f"/api/models/{name}")
            self.assertEqual(response.status_code, 200)
            self.assertIn(f"helm uninstall {name}", response.json()["command"])
            with patch.object(app.kubernetes, "permissions", return_value={"read": True, "deploy": True}), \
                 patch.object(app.kubernetes, "remove") as uninstall, \
                 patch.object(app.litellm_client, "remove_model") as unregister:
                self.assertEqual(self.client.delete(f"/api/models/{name}").status_code, 200)
                uninstall.assert_called_once_with(name)
                unregister.assert_called_once_with(name)

            simulated = copy.deepcopy(server)
            simulated["spec"]["template"]["spec"]["containers"] = [
                {"name": "vllm", "args": ["--model", "org/legacy-model", "--port", "8000"]}]
            with patch.object(app.kubernetes, "api", return_value={"items": [simulated, router]}):
                self.assertTrue(self.client.get("/api/status").json()["models"][0]["simulate"])

            # Similar deployments from other installations must not be adopted.
            for change in ("stack", "helm", "endpoint", "model"):
                with self.subTest(change=change):
                    other = copy.deepcopy(server)
                    registration = copy.deepcopy(registered)
                    if change == "stack":
                        other["metadata"]["labels"]["inference-stack/stack"] = "another-stack"
                    elif change == "helm":
                        other["metadata"]["annotations"]["meta.helm.sh/release-name"] = "different"
                    elif change == "endpoint":
                        registration[0]["api_base"] = "https://example.com/v1"
                    else:
                        registration[0]["served_model"] = "org/different-model"
                    with patch.object(app.kubernetes, "api", return_value={"items": [other, router]}), \
                         patch.object(app.litellm_client, "list_models", return_value=registration):
                        self.assertEqual(self.client.get("/api/status").json()["models"], [])

    def test_duplicate_deployment_does_not_start_work(self):
        app.operation_lock.acquire()
        with patch.object(app.compose_backend, "pull_image") as pull:
            response = self.client.post("/api/models", json={"model": "Qwen/Qwen3-0.6B", "simulate": True})
        self.assertEqual(response.status_code, 409)
        pull.assert_not_called()

    def test_read_only_command_does_not_mutate_models(self):
        with patch.object(app.settings, "MODE", "kubernetes"), \
             patch.object(app.kubernetes, "permissions", return_value={"deploy": False}), \
             patch.object(app.kubernetes, "deploy") as deploy:
            response = self.client.post("/api/models", json={"model": "Qwen/Qwen3-0.6B", "simulate": True})
        self.assertEqual(response.status_code, 200)
        command = response.json()["command"]
        self.assertIn("helm dependency build", command)
        self.assertIn("--wait", command)
        self.assertIn("gpusPerReplica", command)
        deploy.assert_not_called()
        self.assertFalse(app.operation_lock.locked())
        self.assertFalse(app.deployment["step"])

    def test_invalid_model_id_never_reaches_execution(self):
        with patch.object(app.compose_backend, "pull_image") as pull:
            for model in ["org/model;echo hello", "../../model", "org/model/extra", "$(whoami)"]:
                self.assertEqual(self.client.post("/api/models", json={"model":model,"simulate":True}).status_code, 422)
        pull.assert_not_called()

    def test_failed_pull_preserves_running_model_and_hides_raw_errors(self):
        with patch.object(app.compose_backend, "running_model", return_value=None), \
             patch.object(app.compose_backend, "pull_image", side_effect=RuntimeError("sensitive diagnostic")), \
             patch.object(app.compose_backend, "start_model_servers") as start:
            response = self.client.post("/api/models", json={"model":"Qwen/Qwen3-0.6B","simulate":True})
        self.assertEqual(response.status_code, 200)
        start.assert_not_called()
        self.assertNotIn("sensitive diagnostic", app.deployment["error"])
        self.assertEqual(app.deployment["request"]["model"], "Qwen/Qwen3-0.6B")
        self.assertFalse(app.operation_lock.locked())

    def test_replacement_requires_confirmation(self):
        with patch.object(app.compose_backend, "running_model", return_value={"served_model":"org/previous"}), \
             patch.object(app.compose_backend, "pull_image") as pull:
            response=self.client.post("/api/models",json={"model":"org/new","simulate":True})
        self.assertEqual(response.status_code,409)
        pull.assert_not_called()
        self.assertFalse(app.operation_lock.locked())

    def test_read_only_removal_keeps_registration_until_uninstall(self):
        with patch.object(app.settings,"MODE","kubernetes"), \
             patch.object(app,"managed_model", return_value={"name":"example"}), \
             patch.object(app.kubernetes,"permissions",return_value={"deploy":False}), \
             patch.object(app.litellm_client,"remove_model") as remove:
            response=self.client.delete("/api/models/example")
        self.assertEqual(response.status_code,200)
        self.assertIn("helm uninstall",response.json()["command"])
        remove.assert_not_called()

    def test_routing_update_preserves_other_scorers(self):
        config={"schedulingProfiles":[{"name":"default","plugins":[
            {"pluginRef":"queue-scorer","weight":2},
            {"pluginRef":"prefix-cache-scorer","weight":3},
            {"pluginRef":"no-hit-lru-scorer","weight":2}]}]}
        with patch.object(app.compose_backend,"routing_config",return_value=config), \
             patch.object(app.compose_backend,"save_routing") as save:
            response=self.client.put("/api/routing",json={"queue":4,"prefix_cache":1})
        self.assertEqual(response.status_code,200)
        self.assertEqual(save.call_args.args[0]["schedulingProfiles"][0]["plugins"][-1],
                         {"pluginRef":"no-hit-lru-scorer","weight":2})
        self.assertFalse(app.operation_lock.locked())

    def test_cross_origin_write_is_rejected(self):
        response=self.client.post("/api/models",json={"model":"org/model","simulate":True},
                                  headers={"Origin":"https://example.com"})
        self.assertEqual(response.status_code,403)

    def test_memory_and_task_metadata_are_honest(self):
        unknown=catalog.summarize({"id":"org/model"})
        self.assertIsNone(unknown["estimated_gb"])
        image=catalog.summarize({"id":"org/image","pipeline_tag":"text-to-image"})
        self.assertTrue(image["blocker"])
        known=catalog.summarize({"id":"org/model","safetensors":{"parameters":{"BF16":10**9}}})
        self.assertGreater(known["estimated_gb"],known["weights_gb"])

    def test_same_short_model_names_do_not_collide(self):
        self.assertNotEqual(app.public_name("team-a/model"),app.public_name("team-b/model"))

    def test_capacity_checks_single_node_allocation(self):
        info={"blocker":"","max_context":4096,"weights_gb":1}
        hardware={"count":2,"devices":[{"available":1,"memory_gb":24},{"available":1,"memory_gb":24}]}
        with patch.object(app.settings,"MODE","kubernetes"), \
             patch.object(app,"model_details",return_value=info), \
             patch.object(app.kubernetes,"gpu_inventory",return_value=hardware):
            with self.assertRaises(app.HTTPException) as error:
                app.validate_allocation(app.AddModel(model="org/model",gpus_per_replica=2))
        self.assertEqual(error.exception.status_code,422)
        self.assertIn("one node",error.exception.detail)


if __name__ == "__main__":
    unittest.main()
