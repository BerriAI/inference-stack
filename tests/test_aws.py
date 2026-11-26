"""AWS setup checks. No test contacts AWS or changes the host configuration."""
import hashlib
import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bootstrap', ROOT / 'aws/bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class TemplateLoader(yaml.SafeLoader):
    pass


TemplateLoader.add_multi_constructor('!', lambda loader, tag, node: loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode) else loader.construct_sequence(node))
TEMPLATE = yaml.load((ROOT / 'aws/stack.yaml').read_text(), Loader=TemplateLoader)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'stack'
        self.patch = patch.object(bootstrap, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def archive(self, unsafe=False):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for name, body in [('VERSION', b'1.2.3\n'), ('charts/inference-stack/Chart.yaml', b'name: inference-stack')]:
                member = tarfile.TarInfo('inference-stack-1.2.3/' + name)
                member.size = len(body)
                archive.addfile(member, io.BytesIO(body))
            if unsafe:
                member = tarfile.TarInfo('inference-stack-1.2.3/escape')
                member.type = tarfile.SYMTYPE
                member.linkname = '/etc/passwd'
                archive.addfile(member)
        return output.getvalue()

    def test_release_checksum_and_retry_preserve_installation(self):
        archive = self.archive()
        checksum = f'{hashlib.sha256(archive).hexdigest()}  inference-stack-1.2.3.tar.gz\n'.encode()
        with patch.object(bootstrap, 'download', side_effect=[checksum, archive]) as download:
            bootstrap.install_bundle('1.2.3')
            (self.root / 'keep').write_text('preserved')
            bootstrap.install_bundle('1.2.3')
        self.assertEqual(download.call_count, 2)
        self.assertEqual((self.root / 'keep').read_text(), 'preserved')

    def test_corrupt_or_unsafe_release_is_not_installed(self):
        for unsafe in (False, True):
            archive = self.archive(unsafe)
            digest = hashlib.sha256(archive).hexdigest() if unsafe else '0' * 64
            with patch.object(bootstrap, 'download', side_effect=[f'{digest}  inference-stack-1.2.3.tar.gz\n'.encode(), archive]):
                with self.assertRaises(RuntimeError):
                    bootstrap.install_bundle('1.2.3')
            self.assertFalse(self.root.exists())

    def test_runtime_configuration_stays_private(self):
        path = self.root / 'private'
        bootstrap.write(path, 'test-only-value')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        bootstrap.write(path, 'replacement')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_worker_labels_describe_the_actual_gpu_and_instance(self):
        def run(*args):
            if '--query-gpu=memory.total' in args: return '46068\n46068\n'
            if '--query-gpu=name' in args: return 'NVIDIA L40S\nNVIDIA L40S\n'
            return ''
        with patch.object(bootstrap,'run',side_effect=run), \
             patch.object(bootstrap,'aws',return_value={'SecretString':json.dumps({'server':'https://example.com','token':'test-only-join-token'})}), \
             patch.object(bootstrap,'metadata',side_effect=lambda field:'test-instance' if field=='instance-id' else 'g6e.12xlarge'), \
             patch.dict(os.environ,JOIN_SECRET='test-only-secret'), \
             patch.object(bootstrap,'install_k3s') as install:
            bootstrap.install_worker()
        labels=install.call_args.args[0]['node-label']
        self.assertIn('nvidia.com/gpu.product=NVIDIA-L40S',labels)
        self.assertIn('nvidia.com/gpu.memory=46068',labels)
        self.assertIn('node.kubernetes.io/instance-type=g6e.12xlarge',labels)

    def test_credentials_and_untrusted_host_do_not_enter_proxy_config(self):
        credentials = {'masterkey': 'test-only-generated-masterkey', 'database': 'test-only-generated-database'}
        values = bootstrap.chart_values('https://example.cloudfront.net', 'test-only-admin-password-0123456789', credentials)
        self.assertFalse(values['grafana']['grafana.ini']['auth.anonymous']['enabled'])
        self.assertEqual(values['litellm']['extraEnvVars'][0]['valueFrom']['secretKeyRef']['name'], 'inference-stack-admin')
        self.assertTrue(values['launcher']['awsCapacity'])
        for host in ('https://example.com', 'https://example.cloudfront.net; bad', 'http://example.cloudfront.net'):
            with self.assertRaises(RuntimeError):
                bootstrap.chart_values(host, 'test-only-admin-password-0123456789', credentials)
        self.assertFalse(self.root.exists())

    def test_lens_uses_cloudfront_for_its_launcher_link_and_exporter_endpoint(self):
        public_url = 'https://example.cloudfront.net'
        credentials = {'masterkey': 'test-only-generated-masterkey', 'database': 'test-only-generated-database'}
        values = bootstrap.chart_values(public_url, 'test-only-admin-password-0123456789', credentials)
        self.assertTrue(values['lens']['enabled'])
        self.assertEqual(values['lens']['publicUrl'], public_url)
        self.assertEqual(values['launcher']['lensPublicUrl'], public_url)
        self.assertFalse(values['lens'].get('gateway', {}).get('enabled', False))

        cache = TEMPLATE['Resources']['Distribution']['Properties']['DistributionConfig']['DefaultCacheBehavior']
        self.assertIn('POST', cache['AllowedMethods'])
        self.assertIn('DELETE', cache['AllowedMethods'])
        self.assertEqual(cache['CachePolicyId'], '4135ea2d-6df8-44a3-9df3-4b5a84be39ad')  # CachingDisabled
        self.assertEqual(cache['OriginRequestPolicyId'], 'b689b0a8-53d0-40ab-baf2-68738e2966ac')  # AllViewerExceptHostHeader

        config = bootstrap.proxy_config('example.cloudfront.net', '10.0.0.1', '10.0.0.2', '10.0.0.3', '10.0.0.4')
        self.assertIn('location = /litellm { return 302 /litellm/ui/lens/; }', config)
        self.assertIn('location = /litellm/litellm/lens/ui/ { return 308 /litellm/lens/ui/$is_args$args; }', config)
        self.assertIn('location /litellm/ { proxy_pass http://10.0.0.2:4000; }', config)
        pattern = re.search(r'location ~ (\S+) \{ proxy_pass http://10\.0\.0\.4:4318; \}', config)[1]
        for path in ('/ui', '/ui/', '/ui/_next/static/app.js', '/auth/session', '/auth/methods',
                     '/v1/traces', '/v1/logs', '/v1/traces/receipt', '/v1/logs/id', '/lens/admin/tracing/keys'):
            self.assertRegex(path, pattern)
        for path in ('/', '/api/status', '/litellm/ui', '/litellm/v1/traces', '/litellm/v1/logs',
                     '/grafana/', '/internal/read', '/v1/chat/completions', '/v1/traces-other'):
            self.assertNotRegex(path, pattern)

    def test_existing_aws_install_upgrades_only_lens_values_and_reuses_its_own_chart(self):
        public_url = 'https://example.cloudfront.net'
        def helm(*args):
            if args[1] == 'get':
                return json.dumps({'launcher': {'litellmPublicUrl': public_url + '/litellm'}})
            self.assertEqual(args[:6], ('/usr/local/bin/helm', 'upgrade', 'inference-stack',
                                      str(self.root / 'charts/inference-stack'), '-n', 'inference'))
            self.assertIn('--reset-then-reuse-values', args)
            self.assertIn('--atomic', args)
            overrides = json.loads(Path(args[args.index('-f') + 1]).read_text())
            self.assertEqual(set(overrides), {'lens', 'launcher'})
            self.assertEqual(overrides['launcher'], {'lensPublicUrl': public_url})
            self.assertEqual(overrides['lens'], bootstrap.lens_values(public_url)['lens'])

        for gateway_lens in (False, True):
            with self.subTest(gateway_lens=gateway_lens), patch.dict(os.environ), \
                 patch.object(bootstrap, 'run', side_effect=helm) as run, \
                 patch.object(bootstrap, 'metadata', return_value='t3.xlarge'), \
                 patch.object(bootstrap, 'configure_proxy') as proxy:
                if gateway_lens:
                    template = self.root / 'charts/inference-stack/templates/lens-connection.yaml'
                    template.parent.mkdir(parents=True)
                    template.touch()
                bootstrap.enable_lens()
                self.assertEqual(run.call_count, 2)
                proxy.assert_called_once_with(public_url, gateway_lens=gateway_lens)

    def test_t3_large_lens_upgrade_persists_small_cpu_requests_without_overriding_custom_requests(self):
        public_url = 'https://example.cloudfront.net'
        for saved_cpu in (None, '40m'):
            with self.subTest(saved_cpu=saved_cpu):
                saved_lens = {'enabled': False}
                if saved_cpu:
                    saved_lens['resources'] = {'requests': {'cpu': saved_cpu}}
                def helm(*args):
                    if args[1] == 'get':
                        return json.dumps({'launcher': {'litellmPublicUrl': public_url + '/litellm'}, 'lens': saved_lens})
                    lens = json.loads(Path(args[args.index('-f') + 1]).read_text())['lens']
                    self.assertEqual(lens['clickhouse']['resources'], {'requests': {'cpu': '50m'}})
                    self.assertEqual(lens.get('resources'), None if saved_cpu else {'requests': {'cpu': '50m'}})

                with patch.dict(os.environ), patch.object(bootstrap, 'run', side_effect=helm), \
                     patch.object(bootstrap, 'metadata', return_value='t3.large') as metadata, \
                     patch.object(bootstrap, 'configure_proxy'):
                    bootstrap.enable_lens()
                metadata.assert_called_once_with('instance-type')

    def test_command_errors_hide_raw_output(self):
        with patch.object(bootstrap.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='private-value', stderr='private-value')):
            with self.assertRaises(RuntimeError) as error:
                bootstrap.run('example-tool', 'private-argument')
        self.assertNotIn('private-', str(error.exception))

    def test_startup_waits_for_services_without_installing_any_model(self):
        ready = {'models': [], 'components': {key: {'healthy': True} for key in ('litellm', 'router', 'grafana')}}
        waiting = {**ready, 'components': {**ready['components'], 'litellm': {'healthy': False}}}
        with patch.object(bootstrap, 'request', side_effect=[waiting, ready]) as request, patch.object(bootstrap.time, 'sleep'):
            bootstrap.wait_for_services('localhost', 'test-only-password')
        self.assertEqual(request.call_count, 2)
        self.assertFalse(hasattr(bootstrap, 'starter_model'))



class PrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.ec2 = Mock()
        self.ec2.meta.region_name = 'us-east-2'
        self.ec2.describe_instance_type_offerings.return_value = {'InstanceTypeOfferings': [{'Location': 'us-east-2a'}]}
        self.quotas = Mock()
        self.quotas.get_service_quota.return_value = {'Quota': {'Value': 0}}
        fake_boto3 = SimpleNamespace(client=lambda name: self.ec2 if name == 'ec2' else self.quotas)
        self.code = {}
        with patch.dict(sys.modules, boto3=fake_boto3):
            exec(TEMPLATE['Resources']['PrerequisiteFunction']['Properties']['Code']['ZipFile'], self.code)
        self.properties = {'Operation': 'Prepare'}

    def test_zero_quota_explains_recovery_before_any_machine_is_created(self):
        with self.assertRaisesRegex(ValueError, 'Request an EC2 quota increase'):
            self.code['prepare'](self.properties)
        self.ec2.describe_instance_type_offerings.assert_not_called()

    def test_launcher_needs_only_standard_quota_and_starts_without_gpus(self):
        self.quotas.get_service_quota.return_value = {'Quota': {'Value': 2}}
        result = self.code['prepare'](self.properties)
        self.assertEqual(result['InstanceType'], 't3.large')
        self.quotas.get_service_quota.assert_called_once_with(ServiceCode='ec2', QuotaCode='L-1216C47A')
        self.assertEqual(TEMPLATE['Resources']['GpuGroup']['Properties']['DesiredCapacity'], '0')
        self.assertEqual(result['Zone'], 'us-east-2a')
        self.assertEqual(TEMPLATE['Resources']['StopSchedule']['Properties']['State'], 'DISABLED')

    def test_new_install_checks_cpu_quota_for_lens_sized_launcher(self):
        properties = TEMPLATE['Resources']['Prerequisites']['Properties']
        self.quotas.get_service_quota.return_value = {'Quota': {'Value': 2}}
        with self.assertRaisesRegex(ValueError, 'needs 4 Standard on-demand vCPUs'):
            self.code['prepare'](properties)
        self.quotas.get_service_quota.return_value = {'Quota': {'Value': 4}}
        self.assertEqual(self.code['prepare'](properties)['InstanceType'], 't3.xlarge')

    def test_wrong_region_fails_before_using_ohio_price(self):
        self.ec2.meta.region_name = 'us-west-2'
        with self.assertRaisesRegex(ValueError, 'Ohio'):
            self.code['prepare'](self.properties)

    def test_control_plane_and_gpu_worker_use_ubuntu_26_04_ssm_amis(self):
        resources = TEMPLATE['Resources']
        self.assertEqual(
            resources['Machine']['Properties']['ImageId'],
            '{{resolve:ssm:/aws/service/canonical/ubuntu/server/26.04/stable/current/amd64/hvm/ebs-gp3/ami-id}}',
        )
        self.assertEqual(
            resources['WorkerTemplate']['Properties']['LaunchTemplateData']['ImageId'],
            '{{resolve:ssm:/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-26.04/latest/ami-id}}',
        )

    def test_template_has_no_public_ingress_or_simulation_setup_question(self):
        self.assertNotIn('Simulate', TEMPLATE['Parameters'])
        group = TEMPLATE['Resources']['MachineSecurityGroup']['Properties']
        self.assertNotIn('SecurityGroupIngress', group)
        machine = TEMPLATE['Resources']['Machine']['Properties']
        self.assertEqual(machine['MetadataOptions']['HttpPutResponseHopLimit'], 1)
        self.assertEqual(TEMPLATE['Resources']['Ready']['CreationPolicy']['ResourceSignal']['Count'], 1)

    def test_all_gpu_groups_are_stack_owned_empty_and_individually_bounded(self):
        groups=[r['Properties'] for r in TEMPLATE['Resources'].values() if r['Type']=='AWS::AutoScaling::AutoScalingGroup']
        self.assertEqual(len(groups),4)
        for group in groups:
            self.assertEqual((group['MinSize'],group['MaxSize'],group['DesiredCapacity']),('0','1','0'))
            self.assertEqual(group['MixedInstancesPolicy']['InstancesDistribution']['OnDemandPercentageAboveBaseCapacity'],100)

    def test_shutdown_attempts_every_group_and_retries_partial_failure(self):
        client=Mock()
        client.set_desired_capacity.side_effect=[RuntimeError('test failure'),None,None,None]
        code={}
        with patch.dict(sys.modules,boto3=SimpleNamespace(client=lambda _:client)):
            exec(TEMPLATE['Resources']['StopFunction']['Properties']['Code']['ZipFile'],code)
        with patch.dict(os.environ,GPU_GROUPS='test-1,test-2,test-3,test-4'):
            with self.assertRaisesRegex(RuntimeError,'retry all groups'):code['handler']({},None)
        self.assertEqual(client.set_desired_capacity.call_count,4)
        self.assertTrue(all(c.kwargs['DesiredCapacity']==0 for c in client.set_desired_capacity.call_args_list))


if __name__ == '__main__':
    unittest.main()
