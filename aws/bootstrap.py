#!/usr/bin/env python3
"""Install one AWS machine. Secrets remain in root-only runtime configuration."""

import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import quote


ROOT = Path('/opt/inference-stack')
MODEL = 'Qwen/Qwen3-4B'


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    if result.returncode:
        # Command output may contain credentials or deployment addresses.
        raise RuntimeError(f'{args[0]} failed; inspect the installation on this machine.')
    return result.stdout


def aws(*args):
    return json.loads(run('aws', *args, '--output', 'json'))


def write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, 'w') as file:
        file.write(content)
    path.chmod(mode)


def download(url):
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def install_bundle(version):
    if ROOT.exists():
        if (ROOT / 'VERSION').read_text().strip() != version:
            raise RuntimeError('Existing installation has a different version; preserve it and use the upgrade guide.')
        return
    release = f'https://github.com/BerriAI/inference-stack/releases/download/v{version}'
    filename = f'inference-stack-{version}.tar.gz'
    checksums = dict(line.split()[::-1] for line in download(f'{release}/SHA256SUMS').decode().splitlines())
    archive = download(f'{release}/{filename}')
    if hashlib.sha256(archive).hexdigest() != checksums.get(filename):
        raise RuntimeError('Release checksum did not match.')
    with tempfile.TemporaryDirectory() as directory:
        archive_path = Path(directory) / filename
        archive_path.write_bytes(archive)
        with tarfile.open(archive_path) as tar:
            for member in tar.getmembers():
                path = Path(member.name)
                if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                    raise RuntimeError('Release contains an unsafe archive entry.')
            tar.extractall(directory)
        source = Path(directory) / f'inference-stack-{version}'
        if not (source / 'compose/docker-compose.yaml').is_file():
            raise RuntimeError('Release is missing Docker Compose configuration.')
        shutil.move(str(source), ROOT)


def compose_override(public_url):
    return f'''services:
  launcher:
    environment:
      LAUNCHER_PASSWORD: ${{APP_PASSWORD}}
      LITELLM_PUBLIC_URL: {public_url}/litellm
      GRAFANA_PUBLIC_URL: {public_url}/grafana
      FORWARDED_ALLOW_IPS: "*"
      MACHINE_INFO: /machine/info.json
    volumes:
      - ./machine:/machine:ro
  litellm:
    environment:
      SERVER_ROOT_PATH: /litellm
      FORWARDED_ALLOW_IPS: "*"
      PROXY_BASE_URL: {public_url}/litellm
      UI_USERNAME: admin
      UI_PASSWORD: ${{APP_PASSWORD}}
  grafana:
    environment:
      GF_AUTH_ANONYMOUS_ENABLED: "false"
      GF_SECURITY_ADMIN_USER: admin
      GF_SECURITY_ADMIN_PASSWORD: ${{APP_PASSWORD}}
      GF_SERVER_ROOT_URL: {public_url}/grafana/
      GF_SERVER_SERVE_FROM_SUB_PATH: "true"
'''


def proxy_config(host):
    return f'''map $http_upgrade $connection_upgrade {{ default upgrade; '' close; }}
server {{
    listen 80 default_server;
    server_name _;
    server_tokens off;
    client_max_body_size 20m;
    proxy_http_version 1.1;
    proxy_set_header Host {host};
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_buffering off;
    proxy_read_timeout 120s;
    location = /litellm {{ return 302 /litellm/ui; }}
    location /litellm/ {{ proxy_pass http://127.0.0.1:4000; }}
    location /grafana/ {{ proxy_pass http://127.0.0.1:3000; }}
    location / {{ proxy_pass http://127.0.0.1:8080; }}
}}
'''


def configure(public_url, password):
    if len(password) < 20 or len(set(password)) < 5 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@%+=:,./!?~-' for c in password):
        raise RuntimeError('Choose a password with at least 20 characters and five distinct characters.')
    if not re.fullmatch(r'https://[a-z0-9-]+\.cloudfront\.net', public_url):
        raise RuntimeError('The HTTPS address is not a CloudFront hostname.')
    environment = ROOT / 'compose/.env'
    if not environment.exists():
        write(environment, f'LITELLM_MASTER_KEY=sk-{secrets.token_hex(24)}\nPOSTGRES_PASSWORD={secrets.token_hex(24)}\nAPP_PASSWORD={password}\n')
    elif f'APP_PASSWORD={password}\n' not in environment.read_text():
        raise RuntimeError('The installation password changed. Restore the original password before retrying.')
    write(ROOT / 'compose/docker-compose.override.yaml', compose_override(public_url))
    write('/etc/nginx/sites-available/inference-stack', proxy_config(public_url.removeprefix('https://')), 0o644)
    Path('/etc/nginx/sites-enabled/default').unlink(missing_ok=True)
    link = Path('/etc/nginx/sites-enabled/inference-stack')
    if not link.exists():
        link.symlink_to('/etc/nginx/sites-available/inference-stack')
    write('/etc/systemd/system/inference-stack.service', f'''[Unit]
Description=Inference Stack
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory={ROOT}/compose
ExecStartPre=/usr/bin/python3 {ROOT}/aws/bootstrap.py --machine-info
ExecStart=/usr/bin/docker compose up -d --no-build --wait
ExecStop=/usr/bin/docker compose stop
TimeoutStartSec=1800

[Install]
WantedBy=multi-user.target
''', 0o644)


def machine_info():
    config = json.loads((ROOT / 'aws/machine.json').read_text())
    uptime = float(Path('/proc/uptime').read_text().split()[0])
    now = datetime.datetime.now(datetime.timezone.utc)
    stop = now + datetime.timedelta(hours=config['hours'], seconds=-uptime)
    initial = datetime.datetime.fromisoformat(config['initial_stop_at'])
    # The independent AWS schedule only applies to the first boot.
    if initial > now:
        stop = min(stop, initial)
    config['stop_at'] = stop.isoformat()
    write(ROOT / 'compose/machine/info.json', json.dumps(config), 0o644)


def configure_machine(stack, hours, simulate):
    region = os.environ['AWS_DEFAULT_REGION']
    machine = aws('cloudformation', 'describe-stack-resource', '--stack-name', stack, '--logical-resource-id', 'Machine')['StackResourceDetail']['PhysicalResourceId']
    console = f'https://{region}.console.aws.amazon.com'
    write(ROOT / 'aws/machine.json', json.dumps({
        'provider': 'aws', 'instance_type': 'm6i.xlarge' if simulate else 'g5.2xlarge',
        'region': region, 'hourly_usd': 0.192 if simulate else 1.212,
        'hours': hours, 'initial_stop_at': os.environ['INITIAL_STOP_AT'] + '+00:00',
        'manage_url': f'{console}/ec2/home?region={region}#InstanceDetails:instanceId={machine}',
        'delete_url': f'{console}/cloudformation/home?region={region}#/stacks/stackinfo?stackId={quote(stack, safe="")}',
    }))
    machine_info()


def configure_stop(hours):
    write('/etc/systemd/system/inference-stack-stop.service', '''[Unit]
Description=Stop the Inference Stack trial machine
[Service]
Type=oneshot
ExecStart=/usr/sbin/shutdown -h now
''', 0o644)
    write('/etc/systemd/system/inference-stack-stop.timer', f'''[Unit]
Description=Stop Inference Stack after {hours} hours on each boot
[Timer]
OnBootSec={hours}h
Unit=inference-stack-stop.service
[Install]
WantedBy=timers.target
''', 0o644)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'inference-stack-stop.timer')


def distribution_url(stack):
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        try:
            resource = aws('cloudformation', 'describe-stack-resource', '--stack-name', stack, '--logical-resource-id', 'Distribution')
            distribution = resource['StackResourceDetail']['PhysicalResourceId']
            domain = aws('cloudfront', 'get-distribution', '--id', distribution)['Distribution']['DomainName']
            return f'https://{domain}'
        except (KeyError, RuntimeError):
            time.sleep(10)
    raise RuntimeError('The HTTPS address was not created. Check CloudFormation events.')


def request(path, password, body=None):
    headers = {'Authorization': 'Basic ' + base64.b64encode(f'admin:{password}'.encode()).decode()}
    data = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(body).encode()
    with urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:8080{path}', data=data, headers=headers), timeout=30) as response:
        return json.load(response)


def starter_model(password, simulate):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            status = request('/api/status', password)
            if all(status['components'][key]['healthy'] for key in ('litellm', 'router')):
                break
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(5)
    else:
        raise RuntimeError('LiteLLM or llm-d did not become ready. Check the installation services.')
    status = request('/api/status', password)
    if not any(model['served_model'] == MODEL for model in status['models']):
        request('/api/models', password, {'model': MODEL, 'replicas': 1, 'gpus_per_replica': 1, 'context': 4096, 'simulate': simulate})
    deadline = time.monotonic() + 3600
    while time.monotonic() < deadline:
        status = request('/api/status', password)
        if any(model['served_model'] == MODEL and model['status'] == 'ready' for model in status['models']):
            return
        if status['deployment']['error']:
            raise RuntimeError('The starter model failed. Check the model page for recovery.')
        time.sleep(10)
    raise RuntimeError('The starter model did not become ready in time.')


def main():
    stage = 'Starting installation'
    stack = os.environ['STACK_ID']
    try:
        configure_stop(int(os.environ['STOP_AFTER_HOURS']))
        stage = 'Installing prerequisites'
        print(stage, flush=True)
        run('apt-get', 'update', '-qq')
        packages = ['nginx', 'openssl']
        if not shutil.which('docker'):
            packages.extend(['docker.io', 'docker-compose-v2'])
        elif subprocess.run(['docker', 'compose', 'version'], capture_output=True).returncode:
            packages.append('docker-compose-v2')
        run('apt-get', 'install', '-y', *packages)
        simulate = os.environ.get('SIMULATE') == 'true'
        if not simulate:
            run('nvidia-smi')
            run('nvidia-ctk', 'runtime', 'configure', '--runtime=docker')
        run('systemctl', 'enable', '--now', 'docker')
        run('systemctl', 'restart', 'docker')
        stage = 'Downloading Inference Stack'
        print(stage, flush=True)
        install_bundle(os.environ['STACK_VERSION'])
        stage = 'Creating the HTTPS address'
        print(stage, flush=True)
        public_url = distribution_url(stack)
        password = aws('secretsmanager', 'get-secret-value', '--secret-id', os.environ['PASSWORD_SECRET'])['SecretString']
        configure_machine(stack, int(os.environ['STOP_AFTER_HOURS']), simulate)
        configure(public_url, password)
        stage = 'Starting Inference Stack'
        print(stage, flush=True)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', 'inference-stack')
        run('nginx', '-t')
        run('systemctl', 'enable', '--now', 'nginx')
        run('systemctl', 'reload', 'nginx')
        stage = 'Preparing the starter model'
        print(stage, flush=True)
        starter_model(password, simulate)
        run('aws', 'cloudformation', 'signal-resource', '--stack-name', stack, '--logical-resource-id', 'Ready', '--unique-id', 'installer', '--status', 'SUCCESS')
        print('Inference Stack is ready.', flush=True)
    except Exception:
        print(f'{stage} failed. Inspect this machine through AWS Systems Manager.', flush=True)
        run('aws', 'cloudformation', 'signal-resource', '--stack-name', stack, '--logical-resource-id', 'Ready', '--unique-id', 'installer', '--status', 'FAILURE')
        raise SystemExit(1) from None


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['--machine-info']:
        machine_info()
    else:
        main()
