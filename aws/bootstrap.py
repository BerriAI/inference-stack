#!/usr/bin/env python3
"""Install the AWS controller or a GPU worker. No models are installed."""

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
        if not (source / 'charts/inference-stack/Chart.yaml').is_file():
            raise RuntimeError('Release is missing the Inference Stack chart.')
        shutil.move(str(source), ROOT)


K3S_VERSION = 'v1.37.2+k3s1'
K3S_SHA256 = '265b3ee6a3198ef14033c63ef46cbb32370d83795199fb6d1b01f21575eddbda'
HELM_VERSION = 'v3.19.0'
HELM_SHA256 = 'a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496'


def checked_download(url, digest):
    data = download(url)
    if hashlib.sha256(data).hexdigest() != digest:
        raise RuntimeError('Dependency checksum did not match.')
    return data


def metadata(field):
    request = urllib.request.Request('http://169.254.169.254/latest/api/token', method='PUT',
                                     headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'})
    with urllib.request.urlopen(request, timeout=5) as response:
        token = response.read().decode()
    request = urllib.request.Request('http://169.254.169.254/latest/meta-data/' + field,
                                     headers={'X-aws-ec2-metadata-token': token})
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read().decode()


def install_k3s(config, worker=False):
    binary = Path('/usr/local/bin/k3s')
    if not binary.exists():
        binary.write_bytes(checked_download(f'https://github.com/k3s-io/k3s/releases/download/{K3S_VERSION}/k3s', K3S_SHA256))
        binary.chmod(0o755)
    write('/etc/rancher/k3s/config.yaml', json.dumps(config))
    mode = 'agent' if worker else 'server'
    write('/etc/systemd/system/k3s.service', f"""[Unit]
Description=Inference Stack Kubernetes
After=network-online.target
Wants=network-online.target
[Service]
Type=notify
ExecStart=/usr/local/bin/k3s {mode}
KillMode=process
Delegate=yes
LimitNOFILE=1048576
LimitNPROC=infinity
TasksMax=infinity
TimeoutStartSec=0
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
""", 0o644)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'k3s')


def install_helm():
    if not Path('/usr/local/bin/helm').exists():
        import io
        data = checked_download(f'https://get.helm.sh/helm-{HELM_VERSION}-linux-amd64.tar.gz', HELM_SHA256)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            content = archive.extractfile('linux-amd64/helm').read()
        path = Path('/usr/local/bin/helm')
        path.write_bytes(content)
        path.chmod(0o755)


def kubectl(*args):
    return run('/usr/local/bin/k3s', 'kubectl', *args)


def proxy_config(host, launcher, litellm, grafana):
    return f"""map $http_upgrade $connection_upgrade {{ default upgrade; '' close; }}
server {{
    listen 80 default_server;
    server_name _;
    server_tokens off;
    absolute_redirect off;
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
    location /litellm/ {{ proxy_pass http://{litellm}:4000; }}
    location = /grafana {{ return 302 /grafana/; }}
    location /grafana/ {{ proxy_pass http://{grafana}:80; }}
    location / {{ proxy_pass http://{launcher}:8080; }}
}}
"""


def chart_values(public_url, password, credentials):
    if len(password) < 20 or len(set(password)) < 5 or not re.fullmatch(r'[A-Za-z0-9_@%+=:,./!?~-]+', password):
        raise RuntimeError('Choose a password with at least 20 characters and five distinct characters.')
    if not re.fullmatch(r'https://[a-z0-9-]+\.cloudfront\.net', public_url):
        raise RuntimeError('The HTTPS address is not a CloudFront hostname.')
    control = {'inference-stack/role': 'control'}
    return {
        'launcher': {'nodeSelector': control, 'adminSecret': 'inference-stack-admin',
                     'awsCapacity': True, 'litellmPublicUrl': public_url + '/litellm', 'grafanaPublicUrl': public_url + '/grafana'},
        'litellm': {
            'nodeSelector': control, 'masterkey': credentials['masterkey'],
            'resources': {'requests': {'cpu': '500m', 'memory': '4Gi'}, 'limits': {'memory': '5Gi'}},
            'envVars': {'SERVER_ROOT_PATH': '/litellm', 'PROXY_BASE_URL': public_url + '/litellm', 'UI_USERNAME': 'admin', 'FORWARDED_ALLOW_IPS': '*'},
            'extraEnvVars': [{'name': 'UI_PASSWORD', 'valueFrom': {'secretKeyRef': {'name': 'inference-stack-admin', 'key': 'password'}}}],
            'postgresql': {'primary': {'nodeSelector': control}, 'auth': {
                'password': credentials['database'], 'postgres-password': credentials['database']}},
        },
        'prometheus': {'server': {'nodeSelector': control, 'retention': '1d'}},
        'grafana': {'nodeSelector': control, 'admin': {'existingSecret': 'inference-stack-admin', 'userKey': 'username', 'passwordKey': 'password'},
                    'grafana.ini': {'auth.anonymous': {'enabled': False}, 'server': {
                        'root_url': public_url + '/grafana/', 'serve_from_sub_path': True}}},
    }


def configure_stack(public_url, password):
    path = Path('/etc/inference-stack/credentials.json')
    if not path.exists():
        write(path, json.dumps({'masterkey': 'sk-' + secrets.token_hex(24), 'database': secrets.token_hex(24), 'password': password}))
    credentials = json.loads(path.read_text())
    if credentials['password'] != password:
        raise RuntimeError('The installation password changed. Restore the original password before retrying.')
    values = chart_values(public_url, password, credentials)
    # Apply secret via stdin: never pass the password in a command line.
    namespace = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'inference'}}
    secret = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'inference-stack-admin', 'namespace': 'inference'},
              'stringData': {'username': 'admin', 'password': password}}
    for resource in (namespace, secret):
        result = subprocess.run(['/usr/local/bin/k3s', 'kubectl', 'apply', '-f', '-'], input=json.dumps(resource), capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Could not configure the installation credentials.')
    write('/etc/inference-stack/values.json', json.dumps(values))
    run('/usr/local/bin/helm', 'upgrade', '--install', 'inference-stack', str(ROOT / 'charts/inference-stack'),
        '-n', 'inference', '-f', '/etc/inference-stack/values.json', '--wait', '--timeout', '20m')
    addresses = []
    for name in ('inference-stack-launcher', 'inference-stack-litellm', 'inference-stack-grafana'):
        addresses.append(json.loads(kubectl('get', 'service', name, '-n', 'inference', '-o', 'json'))['spec']['clusterIP'])
    write('/etc/nginx/sites-available/inference-stack', proxy_config(public_url.removeprefix('https://'), *addresses), 0o644)
    Path('/etc/nginx/sites-enabled/default').unlink(missing_ok=True)
    link = Path('/etc/nginx/sites-enabled/inference-stack')
    if not link.exists():
        link.symlink_to('/etc/nginx/sites-available/inference-stack')
    run('nginx', '-t')
    run('systemctl', 'enable', '--now', 'nginx')
    run('systemctl', 'reload', 'nginx')
    return addresses[0]


def configure_capacity(stack):
    region = os.environ['AWS_DEFAULT_REGION']
    config = {key: os.environ[name] for key, name in (
        ('group', 'GPU_GROUP'), ('schedule', 'GPU_SCHEDULE'), ('stop_role', 'GPU_STOP_ROLE'))}
    config['hours'] = int(os.environ['STOP_AFTER_HOURS'])
    write('/etc/inference-stack/capacity.json', json.dumps(config))
    console = f'https://{region}.console.aws.amazon.com'
    info = {'provider': 'aws', 'instance_type': 't3.large', 'region': region, 'hourly_usd': 0.0832,
            'delete_url': f'{console}/cloudformation/home?region={region}#/stacks/stackinfo?stackId={quote(stack, safe="")}' }
    write('/etc/inference-stack/public/info.json', json.dumps(info), 0o644)
    write('/etc/systemd/system/inference-stack-capacity.service', f"""[Unit]
Description=Inference Stack GPU capacity
After=k3s.service network-online.target
Requires=k3s.service
[Service]
ExecStart=/usr/bin/python3 {ROOT}/aws/capacity.py
Environment=AWS_DEFAULT_REGION={region}
RuntimeDirectory=inference-stack
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
""", 0o644)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'inference-stack-capacity')


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


def request(address, password):
    headers = {'Authorization': 'Basic ' + base64.b64encode(f'admin:{password}'.encode()).decode()}
    with urllib.request.urlopen(urllib.request.Request(f'http://{address}:8080/api/status', headers=headers), timeout=30) as response:
        return json.load(response)


def wait_for_services(address, password):
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            status = request(address, password)
            if all(status['components'][key]['healthy'] for key in ('litellm', 'router', 'grafana')) and not status.get('capacity', {}).get('error'):
                return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(5)
    raise RuntimeError('The stack services did not become ready. Check the installation services.')


def install_worker():
    memory = int(run('nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits').strip().splitlines()[0])
    join = json.loads(aws('secretsmanager', 'get-secret-value', '--secret-id', os.environ['JOIN_SECRET'])['SecretString'])
    run('apt-get', 'update', '-qq')
    run('apt-get', 'install', '-y', 'conntrack', 'socat', 'iptables')
    install_k3s({'server': join['server'], 'token': join['token'], 'node-name': metadata('instance-id'),
                 'node-label': ['inference-stack/role=gpu', 'nvidia.com/gpu.product=NVIDIA-A10G', f'nvidia.com/gpu.memory={memory}'],
                 'default-runtime': 'nvidia'}, worker=True)


def main():
    stage = 'Starting installation'
    stack = os.environ['STACK_ID']
    try:
        stage = 'Installing prerequisites'
        print(stage, flush=True)
        run('apt-get', 'update', '-qq')
        run('apt-get', 'install', '-y', 'nginx', 'conntrack', 'socat', 'iptables')
        stage = 'Downloading Inference Stack'
        print(stage, flush=True)
        install_bundle(os.environ['STACK_VERSION'])
        install_helm()
        stage = 'Starting Kubernetes'
        print(stage, flush=True)
        config_path = Path('/etc/rancher/k3s/config.yaml')
        config = json.loads(config_path.read_text()) if config_path.exists() else {
            'agent-token': secrets.token_hex(32), 'node-label': ['inference-stack/role=control'],
            'disable': ['traefik', 'servicelb', 'metrics-server'], 'write-kubeconfig-mode': '600'}
        install_k3s(config)
        os.environ['KUBECONFIG'] = '/etc/rancher/k3s/k3s.yaml'
        token = Path('/var/lib/rancher/k3s/server/agent-token').read_text().strip()
        join = {'server': f'https://{metadata("local-ipv4")}:6443', 'token': token}
        join_path = Path('/etc/inference-stack/join.json')
        write(join_path, json.dumps(join))
        aws('secretsmanager', 'put-secret-value', '--secret-id', os.environ['JOIN_SECRET'], '--secret-string', f'file://{join_path}')
        join_path.unlink()
        run('/usr/local/bin/helm', 'repo', 'add', 'nvdp', 'https://nvidia.github.io/k8s-device-plugin')
        run('/usr/local/bin/helm', 'upgrade', '--install', 'nvidia-device-plugin', 'nvdp/nvidia-device-plugin',
            '--version', '0.20.2', '-n', 'kube-system', '--set-json', 'nodeSelector={"inference-stack/role":"gpu"}')
        configure_capacity(stack)
        stage = 'Creating the HTTPS address'
        print(stage, flush=True)
        public_url = distribution_url(stack)
        password = aws('secretsmanager', 'get-secret-value', '--secret-id', os.environ['PASSWORD_SECRET'])['SecretString']
        stage = 'Starting Inference Stack'
        print(stage, flush=True)
        address = configure_stack(public_url, password)
        stage = 'Checking stack services'
        print(stage, flush=True)
        wait_for_services(address, password)
        run('aws', 'cloudformation', 'signal-resource', '--stack-name', stack, '--logical-resource-id', 'Ready', '--unique-id', 'installer', '--status', 'SUCCESS')
        print('Inference Stack is ready. No GPUs or models were installed.', flush=True)
    except Exception:
        print(f'{stage} failed. Inspect this machine through AWS Systems Manager.', flush=True)
        run('aws', 'cloudformation', 'signal-resource', '--stack-name', stack, '--logical-resource-id', 'Ready', '--unique-id', 'installer', '--status', 'FAILURE')
        raise SystemExit(1) from None


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['--worker']:
        try:
            install_worker()
        except Exception:
            print('GPU setup failed. Inspect this worker through AWS Systems Manager.', flush=True)
            raise SystemExit(1) from None
    else:
        main()
