"""AWS setup checks. No test contacts AWS or changes the host configuration."""
import hashlib
import datetime
import importlib.util
import io
import json
from pathlib import Path
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

    def test_wrong_region_fails_before_using_ohio_price(self):
        self.ec2.meta.region_name = 'us-west-2'
        with self.assertRaisesRegex(ValueError, 'Ohio'):
            self.code['prepare'](self.properties)

    def test_template_has_no_public_ingress_or_simulation_setup_question(self):
        self.assertNotIn('Simulate', TEMPLATE['Parameters'])
        group = TEMPLATE['Resources']['MachineSecurityGroup']['Properties']
        self.assertNotIn('SecurityGroupIngress', group)
        machine = TEMPLATE['Resources']['Machine']['Properties']
        self.assertEqual(machine['MetadataOptions']['HttpPutResponseHopLimit'], 1)
        self.assertEqual(TEMPLATE['Resources']['Ready']['CreationPolicy']['ResourceSignal']['Count'], 1)


if __name__ == '__main__':
    unittest.main()
