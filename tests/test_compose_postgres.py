"""Copy a Postgres 16 Compose database into Postgres 19 with the real recipe; needs Docker, no GPUs."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

COMPOSE = Path(__file__).resolve().parents[1] / 'compose/docker-compose.yaml'


def docker_available():
    return shutil.which('docker') is not None and subprocess.run(
        ['docker', 'info'], capture_output=True).returncode == 0


@unittest.skipUnless(docker_available(), 'Requires Docker')
class ComposePostgresUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.project = f'inference-stack-test-{uuid.uuid4().hex[:8]}'
        self.env = {**os.environ, 'POSTGRES_PASSWORD': 'test-only-database-password-0123456789',
                    'LITELLM_MASTER_KEY': 'sk-test-only-master-key-0123456789'}
        # The recipe names its network, so give each test its own to stay off a running stack.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.override = Path(directory.name) / 'network.yaml'
        self.override.write_text(f'networks:\n  default:\n    name: {self.project}\n')
        self.addCleanup(self.compose, 'down', '--volumes', check=False)

    def run_command(self, *args, check=True, input=None):
        result = subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=300, input=input)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def compose(self, *args, check=True):
        return self.run_command('docker', 'compose', '-p', self.project, '-f', str(COMPOSE), '-f', str(self.override), *args, check=check)

    def sql(self, statement):
        return self.compose('exec', '-T', 'postgres', 'psql', '-U', 'litellm', '-XAtc', statement).stdout.strip()

    def upgrade(self):
        self.compose('up', '-d', '--wait', 'postgres')
        self.compose('up', '--exit-code-from', 'postgres-upgrade', 'postgres-upgrade')

    def seed_previous_volume(self):
        volume = f'{self.project}_postgres'
        self.run_command('docker', 'volume', 'create', '--label', f'com.docker.compose.project={self.project}',
                         '--label', 'com.docker.compose.volume=postgres', volume)
        script = '''
docker-entrypoint.sh postgres &
until pg_isready -h 127.0.0.1 -U litellm >/dev/null 2>&1; do sleep 1; done
psql -v ON_ERROR_STOP=1 -U litellm -d litellm -c 'create table "LiteLLM_VerificationToken" (token text primary key, spend double precision)' \
  -c "insert into \\"LiteLLM_VerificationToken\\" values ('kept-key', 1.5)"
gosu postgres pg_ctl -D "$PGDATA" -w -m fast stop
'''
        self.run_command('docker', 'run', '--rm', '-e', 'POSTGRES_DB=litellm', '-e', 'POSTGRES_USER=litellm',
                         '-e', f'POSTGRES_PASSWORD={self.env["POSTGRES_PASSWORD"]}', '-v', f'{volume}:/var/lib/postgresql/data',
                         '--entrypoint', 'bash', 'postgres:16', '-ec', script)

    def test_previous_database_is_copied_once_and_left_in_place(self):
        self.seed_previous_volume()
        self.upgrade()
        self.assertTrue(self.sql('show server_version').startswith('19.'))
        self.assertEqual(self.sql('select token, spend from "LiteLLM_VerificationToken"'), 'kept-key|1.5')

        self.sql('''update "LiteLLM_VerificationToken" set spend = 2''')
        self.compose('restart', 'postgres')
        self.upgrade()
        self.assertEqual(self.sql('select token, spend from "LiteLLM_VerificationToken"'), 'kept-key|2')
        previous = self.run_command('docker', 'run', '--rm', '-v', f'{self.project}_postgres:/data:ro',
                                    'postgres:16', 'cat', '/data/PG_VERSION')
        self.assertEqual(previous.stdout.strip(), '16')

    def test_fresh_install_starts_with_an_empty_database(self):
        self.upgrade()
        self.assertTrue(self.sql('show server_version').startswith('19.'))
        self.assertEqual(self.sql("select count(*) from pg_tables where schemaname = 'public'"), '0')


if __name__ == '__main__':
    unittest.main()
