"""Deployment-first journey: spending, recovery, credentials and diagnostics."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import httpx

os.environ.setdefault('LITELLM_MASTER_KEY', 'test-only-not-a-real-key-0123456789')
os.environ.setdefault('STATE_DIR', tempfile.mkdtemp(prefix='inference-stack-journey-'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'launcher'))
import app
import catalog
import diagnostics
from fastapi.testclient import TestClient


class JourneyTests(unittest.TestCase):
    def setUp(self):
        self.client=TestClient(app.app)
        self.client.auth=('admin',app.settings.LAUNCHER_PASSWORD)
        app.deployment.update(action='',name='',step='',error='',request=None)
        app.cancel_deployment.clear()
        if app.operation_lock.locked(): app.operation_lock.release()

    def test_expensive_or_unsupported_allocation_cannot_rent_a_gpu(self):
        with patch.object(app.settings,'AWS_CAPACITY_SOCKET','/test.sock'), \
             patch.object(app,'model_details',return_value={'blocker':'','estimated_gb':48,'weights_gb':32}), \
             patch.object(app.kubernetes,'gpu_inventory',return_value={'count':0,'devices':[]}), \
             patch.object(app,'capacity_request',return_value={'options':[{'instance_type':'g5.2xlarge','hourly_usd':1.212,'memory_gb':24,'gpu_count':1,'blocker':''}]}) as capacity:
            response=self.client.post('/api/models',json={'model':'example/model','add_gpu':True,'expected_capacity':0,'instance_type':'g5.2xlarge','hourly_usd':1.212})
        self.assertEqual(response.status_code,422)
        self.assertIn('smaller model',response.json()['detail'])
        self.assertFalse(any(call.args and call.args[0]=='POST' for call in capacity.call_args_list))
        self.assertFalse(app.operation_lock.locked())

    def test_gpu_deployment_waits_for_capacity_then_registers_the_model(self):
        hardware={'count':1,'devices':[{'available':1,'memory_gb':24,'instance_type':'g5.2xlarge'}]}
        name=app.public_name('example/model')
        with patch.object(app.settings,'MODE','kubernetes'), \
             patch.object(app.settings,'AWS_CAPACITY_SOCKET','/test.sock'), \
             patch.object(app,'validate_allocation') as validate, \
             patch.object(app,'capacity_request',return_value={'error':''}) as capacity, \
             patch.object(app.kubernetes,'permissions',return_value={'deploy':True}), \
             patch.object(app.kubernetes,'gpu_inventory',return_value=hardware), \
             patch.object(app.kubernetes,'deploy') as deploy, \
             patch.object(app.litellm_client,'list_models',return_value=[{'name':name,'served_model':'example/model'}]):
            response=self.client.post('/api/models',json={'model':'example/model','add_gpu':True,'expected_capacity':0,'instance_type':'g5.2xlarge','hourly_usd':1.212})
        self.assertEqual(response.status_code,200)
        capacity.assert_any_call('POST',body={'expected':0,'desired':1,'instance_type':'g5.2xlarge','hourly_usd':1.212})
        self.assertEqual(validate.call_count,2)
        deploy.assert_called_once()
        self.assertEqual(app.deployment['error'],'')
        self.assertFalse(app.operation_lock.locked())

    def test_multiple_gpus_per_machine_and_mixed_memory_validation(self):
        request=app.AddModel(model='example/model',gpus_per_replica=4,add_gpu=True,
                             instance_type='g6e.12xlarge',hourly_usd=10.345)
        selected={'instance_type':'g6e.12xlarge','hourly_usd':10.345,'gpu_count':4,'memory_gb':44.7,'blocker':''}
        hardware={'count':5,'devices':[{'available':1,'memory_gb':16,'instance_type':'g4dn.xlarge'},
                                      {'available':4,'memory_gb':44.7,'instance_type':'g6e.12xlarge'}]}
        with patch.object(app.settings,'MODE','kubernetes'), \
             patch.object(app.settings,'AWS_CAPACITY_SOCKET','/test.sock'), \
             patch.object(app,'model_details',return_value={'blocker':'','estimated_gb':120,'weights_gb':100}), \
             patch.object(app.kubernetes,'gpu_inventory',return_value=hardware), \
             patch.object(app,'capacity_request',return_value={'options':[selected]}):
            app.validate_allocation(request,adding_gpu=True)
            self.assertEqual(request.min_gpu_memory_mib,30720)
            app.validate_allocation(request)
            request.gpus_per_replica=1
            with self.assertRaises(app.HTTPException):
                app.validate_allocation(request)
            request.gpus_per_replica=4
            request.replicas=2
            with self.assertRaises(app.HTTPException):
                app.validate_allocation(request,adding_gpu=True)

    def test_quota_rejection_does_not_start_model_or_lose_the_error(self):
        with patch.object(app.settings,'MODE','kubernetes'), \
             patch.object(app.settings,'AWS_CAPACITY_SOCKET','/test.sock'), \
             patch.object(app,'validate_allocation'), \
             patch.object(app.kubernetes,'permissions',return_value={'deploy':True}), \
             patch.object(app,'capacity_request',side_effect=app.HTTPException(409,'Quota is zero. No GPU was added.')), \
             patch.object(app.kubernetes,'deploy') as deploy:
            response=self.client.post('/api/models',json={'model':'example/model','add_gpu':True,'expected_capacity':0,'instance_type':'g5.2xlarge','hourly_usd':1.212})
        self.assertEqual(response.status_code,409)
        self.assertIn('No GPU was added',response.json()['detail'])
        deploy.assert_not_called()
        self.assertFalse(app.operation_lock.locked())

    def test_cancel_during_gpu_start_retains_visible_capacity(self):
        app.deployment.update(step='Starting GPU')
        with patch.object(app,'capacity_request') as capacity:
            self.assertEqual(self.client.post('/api/deployment/cancel').status_code,200)
        self.assertTrue(app.cancel_deployment.is_set())
        capacity.assert_not_called()
        app.deployment['step']='Downloading and starting model'
        self.assertEqual(self.client.post('/api/deployment/cancel').status_code,409)

    def test_diagnostics_never_echo_credentials_or_addresses(self):
        raw=f'Loading model\nAuthorization: Bearer private-example\nHF_TOKEN=hf_testonlyprivatevalue012345\n{app.settings.LITELLM_MASTER_KEY}\nGET http://example.internal:8000/file?token=example\n10.10.0.2\nCUDA out of memory'
        redacted=diagnostics.redact(raw)
        for secret in ('private-example','hf_testonlyprivatevalue012345',app.settings.LITELLM_MASTER_KEY,'example.internal','10.10.0.2'):
            self.assertNotIn(secret,redacted)
        self.assertIn('CUDA out of memory',redacted)

    def test_huggingface_url_is_normalized_and_token_is_never_echoed(self):
        self.assertEqual(catalog.valid_id('https://huggingface.co/example/model'),'example/model')
        with patch.object(catalog,'save_token') as save:
            response=self.client.post('/api/huggingface',json={'token':'hf_test_only_not_real_012345'})
        self.assertEqual(response.status_code,200)
        self.assertNotIn('hf_test',response.text)
        save.assert_called_once()
        anonymous=TestClient(app.app)
        self.assertEqual(anonymous.post('/api/huggingface',json={'token':'hf_test_only_not_real_012345'}).status_code,401)

    def test_logs_cannot_inspect_an_unmanaged_model(self):
        with patch.object(app,'managed_model',side_effect=app.HTTPException(404,'Not managed')), \
             patch.object(diagnostics,'inspect') as inspect:
            response=self.client.get('/api/models/not-managed/diagnostics')
        self.assertEqual(response.status_code,404)
        inspect.assert_not_called()

    def test_log_access_failure_keeps_recovery_commands(self):
        with patch.object(app.settings, 'MODE', 'kubernetes'), \
             patch.object(app.kubernetes, 'api', side_effect=RuntimeError('unavailable')):
            result = diagnostics.inspect({'name': 'example-model', 'simulate': False})
        self.assertIn('Could not read', result['error'])
        self.assertIn('kubectl logs', result['commands'])
        self.assertIn('helm get values', result['commands'])

    def test_private_model_offers_token_recovery(self):
        response = httpx.Response(401, request=httpx.Request('GET', 'https://huggingface.co/api/models/example/private-model'))
        with patch.object(catalog, '_get', side_effect=httpx.HTTPStatusError('access required', request=response.request, response=response)):
            result = catalog.details('example/private-model')
        self.assertTrue(result['needs_token'])
        self.assertIn('read access', result['blocker'])

    def test_failed_deployment_can_be_removed_before_any_pod_exists(self):
        name = app.public_name('example/model')
        app.deployment.update(action='deploy', name=name, request={'model': 'example/model'}, error='Failed to install')
        with patch.object(app.settings, 'MODE', 'kubernetes'), \
             patch.object(app, 'managed_model', side_effect=app.HTTPException(404, 'No deployment yet')), \
             patch.object(app.kubernetes, 'permissions', return_value={'deploy': True}), \
             patch.object(app.kubernetes, 'remove') as remove, \
             patch.object(app.litellm_client, 'remove_model'):
            response = self.client.delete('/api/models/' + name)
        self.assertEqual(response.status_code, 200)
        remove.assert_called_once_with(name)
        self.assertEqual(app.deployment['action'], '')

    def test_saved_token_is_private_and_restores_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'credentials' / 'token'
            with patch.object(app.settings, 'MODE', 'compose'), \
                 patch.object(app.settings, 'HF_TOKEN_FILE', str(path)), \
                 patch.object(app.settings, 'HF_TOKEN', ''), \
                 patch.object(catalog.httpx, 'get'):
                catalog.save_token('hf_test_only_not_real_012345')
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                app.settings.HF_TOKEN = ''
                catalog.restore_token()
                self.assertEqual(app.settings.HF_TOKEN, 'hf_test_only_not_real_012345')
