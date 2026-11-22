"""Installer recovery checks without Docker or Kubernetes access."""
import hashlib
import io
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import unittest


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.version = (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip()
        self.install = self.root / 'install.sh'
        shutil.copy(Path(__file__).resolve().parents[1] / 'install.sh', self.install)
        self.log = self.root / 'calls'
        self.env = {**os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}',
                    'CALLS': str(self.log), 'INFERENCE_STACK_DIR': str(self.root / 'stack')}
        for name, body in {
            'docker': 'echo "$*" >> "$CALLS"\n',
            'helm': 'echo "$*" >> "$CALLS"\ncase " $* " in *" --all "*) echo "unknown flag: --all" >&2; exit 1;; esac\nif [ "$1" = list ]; then\n [ "${HELM_DENIED:-}" != yes ] || exit 1\n [ "${HELM_EXISTS:-}" != yes ] || echo inference-stack\nfi\n',
            'kubectl': '''echo "$*" >> "$CALLS"
if [ "$1" = get ] && [ "$2" = pods ]; then
  [ "${NO_PROXY_POD:-}" != yes ] || exit 0
  case "$*" in
    *pod-template-hash*) echo pod/inference-stack-litellm-proxy;;
    *) echo pod/inference-stack-litellm-migrations;;
  esac
fi
if [ "$1" = port-forward ]; then
  case "$*" in
    *svc/inference-stack-litellm*|*pod/inference-stack-litellm-migrations*)
      echo "error: migration pod does not have a named port http" >&2; exit 1;;
  esac
  exec sleep 30
fi
''',
        }.items():
            self.tool(name, body)

    def tool(self, name, body):
        p = self.bin / name
        p.write_text('#!/bin/sh\nset -eu\n' + body)
        p.chmod(0o755)

    def checkout(self):
        (self.root / 'compose').mkdir()
        (self.root / 'compose/docker-compose.yaml').write_text('services: {}\n')
        (self.root / 'VERSION').write_text(self.version + '\n')
        (self.root / 'charts/inference-stack/charts').mkdir(parents=True)

    def run_install(self, mode='docker'):
        return subprocess.run(['/bin/bash', str(self.install), mode], env=self.env,
                              cwd=self.root, capture_output=True, text=True, timeout=10)

    def bundle(self, valid=True, corrupt=False):
        archive = self.root / 'archive.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name, value in {'VERSION': (self.version + '\n').encode(), 'compose/docker-compose.yaml': b'services: {}\n'}.items():
                info = tarfile.TarInfo(f'inference-stack-{self.version}/' + name)
                info.size = len(value)
                tar.addfile(info, io.BytesIO(value))
        if corrupt:
            archive.write_bytes(b'not an archive')
        digest = hashlib.sha256(archive.read_bytes()).hexdigest() if valid else '0' * 64
        (self.root / 'sums').write_text(digest + f'  inference-stack-{self.version}.tar.gz\n')
        self.env['FIXTURES'] = str(self.root)
        self.tool('curl', 'case "$5" in\n */SHA256SUMS) cp "$FIXTURES/sums" "$7";;\n *) cp "$FIXTURES/archive.tar.gz" "$7";;\nesac\n')

    def test_fresh_compose_credentials_are_private_and_retained(self):
        self.checkout()
        first = self.run_install()
        self.assertEqual(first.returncode, 0, first.stderr)
        env = self.root / 'compose/.env'
        content = env.read_bytes()
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(content, env.read_bytes())
        self.assertIn('up -d --no-build --wait', self.log.read_text())
        self.assertNotIn(content.decode().splitlines()[0].split('=')[1], first.stdout)

    def test_download_extracts_complete_bundle(self):
        self.bundle()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'stack/compose/.env').is_file())

    def test_checksum_failure_leaves_no_installation(self):
        self.bundle(valid=False)
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum', result.stderr)
        self.assertFalse((self.root / 'stack').exists())

    def test_corrupt_archive_leaves_no_installation(self):
        self.bundle(corrupt=True)
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assertFalse((self.root / 'stack').exists())

    def test_unrelated_directory_is_preserved(self):
        (self.root / 'stack').mkdir()
        marker = self.root / 'stack/keep'
        marker.write_text('keep')
        self.assertNotEqual(self.run_install().returncode, 0)
        self.assertEqual(marker.read_text(), 'keep')

    def existing(self, version='0.3.0', image=None):
        stack = self.root / 'stack'
        (stack / 'compose').mkdir(parents=True)
        (stack / 'VERSION').write_text(version + '\n')
        (stack / 'compose/docker-compose.yaml').write_text('old recipe\n')
        image = image or f'ghcr.io/berriai/inference-stack/launcher:{version}'
        (stack / 'compose/.env').write_text(
            f'LITELLM_MASTER_KEY=keep-me\nPOSTGRES_PASSWORD=also-keep\nINFERENCE_STACK_LAUNCHER_IMAGE={image}\nCOMPOSE_PROJECT_NAME=my-stack\n')
        (stack / 'compose/.env').chmod(0o600)
        return stack

    def test_upgrade_swaps_recipe_and_preserves_credentials(self):
        self.bundle()
        stack = self.existing()
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((stack / 'VERSION').read_text().strip(), self.version)
        self.assertEqual((stack / 'compose/docker-compose.yaml').read_text(), 'services: {}\n')
        env = stack / 'compose/.env'
        self.assertIn('LITELLM_MASTER_KEY=keep-me\nPOSTGRES_PASSWORD=also-keep\n', env.read_text())
        self.assertIn('COMPOSE_PROJECT_NAME=my-stack\n', env.read_text())
        self.assertIn(f'launcher:{self.version}\n', env.read_text())
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        backup = self.root / 'stack.v0.3.0'
        self.assertEqual((backup / 'compose/docker-compose.yaml').read_text(), 'old recipe\n')
        self.assertIn(str(backup), result.stdout)
        self.assertIn('up -d --no-build --wait', self.log.read_text())

    def test_upgrade_preserves_custom_image(self):
        self.bundle()
        stack = self.existing(image='registry.example/launcher:custom')
        before = (stack / 'compose/.env').read_bytes()
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual((stack / 'compose/.env').read_bytes(), before)

    def test_upgrade_rewrites_old_pinned_image(self):
        self.bundle()
        stack = self.existing(image='registry.example/launcher@sha256:old')
        (stack / 'launcher-image.txt').write_text('registry.example/launcher@sha256:old\n')
        self.assertEqual(self.run_install().returncode, 0)
        self.assertIn(f'launcher:{self.version}\n', (stack / 'compose/.env').read_text())

    def test_equal_and_newer_versions_are_unchanged(self):
        for version in (self.version, '99.0.0'):
            with self.subTest(version=version):
                stack = self.existing(version)
                before = (stack / 'compose/.env').read_bytes()
                self.assertEqual(self.run_install().returncode, 0)
                self.assertEqual((stack / 'VERSION').read_text(), version + '\n')
                self.assertEqual((stack / 'compose/docker-compose.yaml').read_text(), 'old recipe\n')
                self.assertEqual((stack / 'compose/.env').read_bytes(), before)
                shutil.rmtree(stack)

    def test_upgrade_backup_conflict_preserves_install(self):
        stack = self.existing()
        (self.root / 'stack.v0.3.0').mkdir()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('backup already exists', result.stderr)
        self.assertEqual((stack / 'VERSION').read_text(), '0.3.0\n')

    def test_upgrade_checksum_failure_preserves_install(self):
        self.bundle(valid=False)
        stack = self.existing()
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum', result.stderr)
        self.assertEqual((stack / 'compose/docker-compose.yaml').read_text(), 'old recipe\n')

    def test_helm_permission_failure_does_not_install(self):
        self.checkout()
        self.env['HELM_DENIED'] = 'yes'
        self.assertNotEqual(self.run_install('kubernetes').returncode, 0)
        self.assertNotIn('install inference-stack', self.log.read_text())

    def test_existing_helm_install_is_reused_and_forwards_stop(self):
        self.checkout()
        self.env['HELM_EXISTS'] = 'yes'
        process = subprocess.Popen(['/bin/bash', str(self.install), 'kubernetes'],
                                   cwd=self.root, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.log.exists() and self.log.read_text().count('port-forward') == 3:
                    break
                time.sleep(0.05)
            process.send_signal(signal.SIGTERM)
            out, err = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143, err)
            calls = self.log.read_text()
            self.assertNotIn('install inference-stack', calls)
            self.assertEqual(calls.count('port-forward'), 3)
            self.assertIn('port-forward --namespace inference pod/inference-stack-litellm-proxy 4000:4000', calls)
            self.assertIn('wait --namespace inference --for=condition=Ready pod/inference-stack-litellm-proxy', calls)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_missing_proxy_pod_fails_without_starting_connections(self):
        self.checkout()
        self.env.update(HELM_EXISTS='yes', NO_PROXY_POD='yes')
        result = self.run_install('kubernetes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No running LiteLLM proxy pod found', result.stderr)
        self.assertNotIn('port-forward', self.log.read_text())


if __name__ == '__main__':
    unittest.main()
