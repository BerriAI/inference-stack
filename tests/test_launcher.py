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

    def test_admin_access_is_required_for_reads_and_mutations(self):
        anonymous = TestClient(app.app)
        for method, path, body in [("GET", "/api/status", None), ("POST", "/api/models", {"model":"org/model","simulate":True}), ("DELETE", "/api/models/example", None)]:
            response = anonymous.request(method, path, json=body)
            self.assertEqual(response.status_code, 401)
            self.assertIn("Basic", response.headers["www-authenticate"])
        self.assertEqual(anonymous.get("/healthz").status_code, 200)
        self.assertEqual(anonymous.get("/api/status", auth=("admin", "incorrect")).status_code, 401)
        self.assertEqual(anonymous.get("/api/status", headers={"Authorization":"Basic invalid"}).status_code, 401)

    def test_example_passwords_are_rejected(self):
        for value in ("", "change-me", "sk-change-me", "NoTaGrEaTpAsSwOrD", "a" * 40):
            with self.assertRaises(ValueError):
                app.settings.validate_password("EXAMPLE", value)
        self.assertEqual(app.settings.validate_password("EXAMPLE", "test-only-not-a-real-key-0123456789"), "test-only-not-a-real-key-0123456789")

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
