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
            for name, body in [('VERSION', b'1.2.3\n'), ('compose/docker-compose.yaml', b'services: {}')]:
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
        override = yaml.safe_load(bootstrap.compose_override('https://example.cloudfront.net'))
        self.assertEqual(override['services']['grafana']['environment']['GF_AUTH_ANONYMOUS_ENABLED'], 'false')
        self.assertEqual(override['services']['litellm']['environment']['UI_PASSWORD'], '${APP_PASSWORD}')
        for host in ('https://example.com', 'https://example.cloudfront.net; bad', 'http://example.cloudfront.net'):
            with self.assertRaises(RuntimeError):
                bootstrap.configure(host, 'test-only-admin-password-0123456789')
        self.assertFalse(self.root.exists())

    def test_changed_password_cannot_silently_leave_old_password_active(self):
        bootstrap.write(self.root / 'compose/.env', 'APP_PASSWORD=test-only-original-password-0123456789\n')
        with self.assertRaisesRegex(RuntimeError, 'password changed'):
            bootstrap.configure('https://example.cloudfront.net', 'test-only-replacement-password-0123456789')
        self.assertIn('original', (self.root / 'compose/.env').read_text())

    def test_command_errors_hide_raw_output(self):
        with patch.object(bootstrap.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='private-value', stderr='private-value')):
            with self.assertRaises(RuntimeError) as error:
                bootstrap.run('example-tool', 'private-argument')
        self.assertNotIn('private-', str(error.exception))

    def test_stop_deadline_refreshes_after_restart_and_respects_initial_schedule(self):
        now = datetime.datetime(2026, 11, 22, 12, tzinfo=datetime.timezone.utc)
        config = {'hours': 4, 'initial_stop_at': (now + datetime.timedelta(hours=2)).isoformat()}
        bootstrap.write(self.root / 'aws/machine.json', json.dumps(config))
        original_read = Path.read_text

        def read(path, *args, **kwargs):
            return '3600 0' if str(path) == '/proc/uptime' else original_read(path, *args, **kwargs)

        # Use a subclass so the timestamp parser and arithmetic stay real.
        class Clock(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        with patch.object(Path, 'read_text', read), patch.object(bootstrap.datetime, 'datetime', Clock):
            bootstrap.machine_info()
            first = json.loads((self.root / 'compose/machine/info.json').read_text())
            self.assertEqual(first['stop_at'], config['initial_stop_at'])
            now += datetime.timedelta(days=1)
            bootstrap.machine_info()
            restarted = json.loads((self.root / 'compose/machine/info.json').read_text())
            self.assertEqual(restarted['stop_at'], (now + datetime.timedelta(hours=3)).isoformat())

    def test_starter_waits_for_services_and_reuses_ready_model(self):
        ready = {'models': [{'served_model': bootstrap.MODEL, 'status': 'ready'}],
                 'deployment': {'error': ''}, 'components': {'litellm': {'healthy': True}, 'router': {'healthy': True}}}
        waiting = {**ready, 'components': {'litellm': {'healthy': False}, 'router': {'healthy': True}}}
        with patch.object(bootstrap, 'request', side_effect=[waiting, ready, ready, ready]) as request, patch.object(bootstrap.time, 'sleep'):
            bootstrap.starter_model('test-only-password', True)
        self.assertTrue(all(call.args[0] == '/api/status' for call in request.call_args_list))


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
        self.properties = {'Operation': 'Prepare', 'Hours': '4', 'Simulate': 'false'}

    def test_zero_quota_explains_recovery_before_any_machine_is_created(self):
        with self.assertRaisesRegex(ValueError, 'Request an EC2 quota increase'):
            self.code['prepare'](self.properties)
        self.ec2.describe_instance_type_offerings.assert_not_called()

    def test_enough_quota_selects_gpu_and_does_not_claim_capacity_is_reserved(self):
        self.quotas.get_service_quota.return_value = {'Quota': {'Value': 8}}
        result = self.code['prepare'](self.properties)
        self.assertEqual(result['InstanceType'], 'g5.2xlarge')
        self.assertEqual(result['Zone'], 'us-east-2a')
        self.assertIn('StopAt', result)

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
