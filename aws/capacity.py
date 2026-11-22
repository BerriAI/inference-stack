#!/usr/bin/env python3
"""Manage this stack's GPU group. Only the launcher can reach this Unix socket."""

import datetime as dt
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socketserver
import subprocess

CONFIG = Path('/etc/inference-stack/capacity.json')
SOCKET = '/run/inference-stack/capacity.sock'


class CapacityError(Exception):
    pass


def run(*args, json_output=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise CapacityError('Could not update AWS capacity. Check the installation in AWS, then retry.')
    return json.loads(result.stdout) if json_output and result.stdout.strip() else {}


def aws(service, action, **kwargs):
    args = ['aws', service, action, '--output', 'json']
    for key, value in kwargs.items():
        if isinstance(value, bool):
            args.append('--' + ('' if value else 'no-') + key.replace('_', '-'))
            continue
        args += ['--' + key.replace('_', '-'), value if isinstance(value, str) else json.dumps(value)]
    return run(*args)


def kubectl(*args):
    output = args[0] == 'get'
    return run('/usr/local/bin/k3s', 'kubectl', *args, *(['-o', 'json'] if output else []), json_output=output)


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def group(config):
    groups = aws('autoscaling', 'describe-auto-scaling-groups', auto_scaling_group_names=[config['group']])['AutoScalingGroups']
    if len(groups) != 1:
        raise CapacityError('The GPU group is missing. Check this installation in AWS.')
    return groups[0]


def deadline(config):
    schedule = aws('scheduler', 'get-schedule', name=config['schedule'])
    if schedule['State'] != 'ENABLED':
        return None
    return dt.datetime.fromisoformat(schedule['ScheduleExpression'][3:-1]).replace(tzinfo=dt.timezone.utc)


def gpu_nodes():
    return kubectl('get', 'nodes', '-l', 'inference-stack/role=gpu')['items']


def workloads(node):
    pods = kubectl('get', 'pods', '--all-namespaces', '--field-selector', f'spec.nodeName={node}')['items']
    return [p for p in pods if p.get('status', {}).get('phase') not in ('Succeeded', 'Failed')
            and not any(o['kind'] == 'DaemonSet' for o in p['metadata'].get('ownerReferences', []))]


def status(config):
    current = group(config)
    nodes = {n['metadata']['name']: n for n in gpu_nodes()}
    pods = kubectl('get', 'pods', '--all-namespaces')['items']
    members = {i['InstanceId'] for i in current['Instances']}
    for name, node in nodes.items():
        ready = any(c['type'] == 'Ready' and c['status'] == 'True' for c in node.get('status', {}).get('conditions', []))
        if name not in members and not ready:
            kubectl('delete', 'node', name, '--ignore-not-found')
    machines = []
    for item in current['Instances']:
        # Worker names are their instance IDs; no address is exposed to the browser.
        node = nodes.get(item['InstanceId'], {})
        ready = any(c['type'] == 'Ready' and c['status'] == 'True' for c in node.get('status', {}).get('conditions', []))
        ready = ready and int(node.get('status', {}).get('allocatable', {}).get('nvidia.com/gpu', 0)) > 0
        state = 'removing' if item['LifecycleState'].startswith('Terminating') else 'ready' if ready else 'starting'
        models = sorted({p['metadata']['labels']['model'] for p in pods
                         if p.get('spec', {}).get('nodeName') == item['InstanceId']
                         and p.get('status', {}).get('phase') not in ('Succeeded', 'Failed')
                         and p['metadata'].get('labels', {}).get('model')})
        machines.append({'id': item['InstanceId'], 'state': state, 'models': models})
    stop = deadline(config)
    error = ''
    if current['DesiredCapacity'] > sum(m['state'] == 'ready' for m in machines):
        activities = aws('autoscaling', 'describe-scaling-activities', auto_scaling_group_name=config['group'], max_records=1)['Activities']
        if activities and activities[0]['StatusCode'] in ('Failed', 'Cancelled'):
            error = 'AWS could not start the GPU. Check GPU quota and regional capacity in AWS, or cancel the pending GPU.'
    return {'desired': current['DesiredCapacity'], 'limit': current['MaxSize'], 'machines': machines,
            'stop_at': stop.isoformat() if stop else None, 'error': error,
            'instance_type': 'g5.2xlarge', 'name': 'NVIDIA A10G', 'memory_gb': 24,
            'hourly_usd': 1.212, 'hours': config['hours'], 'region': 'us-east-2'}


def set_deadline(config, stop):
    aws('scheduler', 'update-schedule', name=config['schedule'], state='ENABLED',
        schedule_expression=f'at({stop.strftime("%Y-%m-%dT%H:%M:%S")})', schedule_expression_timezone='UTC',
        flexible_time_window={'Mode': 'OFF'}, target={
            'Arn': 'arn:aws:scheduler:::aws-sdk:autoscaling:setDesiredCapacity',
            'RoleArn': config['stop_role'],
            'Input': json.dumps({'AutoScalingGroupName': config['group'], 'DesiredCapacity': 0, 'HonorCooldown': False}),
            'RetryPolicy': {'MaximumEventAgeInSeconds': 3600, 'MaximumRetryAttempts': 3}})


def resize(config, expected, target):
    current = group(config)
    if type(expected) is not int or expected != current['DesiredCapacity']:
        raise CapacityError('GPU capacity changed. Refresh and try again.')
    if type(target) is not int or not 0 <= target <= current['MaxSize']:
        raise CapacityError('Choose a GPU count within this installation’s limit.')
    if target == expected + 1:
        quota = aws('service-quotas', 'get-service-quota', service_code='ec2', quota_code='L-DB2E81BA')['Quota']['Value']
        if quota < target * 8:
            raise CapacityError(f'AWS allows {int(quota)} GPU vCPUs in Ohio. Request at least {target * 8} Running On-Demand G and VT vCPUs in Service Quotas, then retry. No GPU was added.')
        stop = deadline(config)
        # Expired rentals must finish terminating before starting a new one.
        if expected and (not stop or stop <= now() + dt.timedelta(minutes=2)):
            raise CapacityError('This GPU session is ending. Remove the GPUs before starting another session.')
        if not expected:
            if current['Instances']:
                raise CapacityError('Wait for the previous GPUs to finish shutting down, then retry.')
            stop = now() + dt.timedelta(hours=config['hours'])
        # Schedule first: never create billable capacity without its independent deadline.
        set_deadline(config, stop)
    elif target != 0:
        raise CapacityError('Add one GPU at a time, or remove a specific GPU.')
    else:
        # Cancel pending launches only. Live nodes must use the cordon/check/remove path.
        members = {i['InstanceId'] for i in current['Instances']}
        if any(n['metadata']['name'] in members for n in gpu_nodes()):
            raise CapacityError('Remove each GPU before cancelling the remaining pending capacity.')
    aws('autoscaling', 'set-desired-capacity', auto_scaling_group_name=config['group'], desired_capacity=target)


def remove(config, instance):
    current = group(config)
    item = next((i for i in current['Instances'] if i['InstanceId'] == instance), None)
    if item is None or item['LifecycleState'].startswith('Terminating'):
        return  # A retry must not remove a different GPU.
    node = next((n for n in gpu_nodes() if n['metadata']['name'] == instance), None)
    if node:
        kubectl('cordon', instance)
    try:
        if node and workloads(instance):
            raise CapacityError('This GPU is in use. Remove its models before removing the GPU.')
        aws('autoscaling', 'terminate-instance-in-auto-scaling-group', instance_id=instance,
            should_decrement_desired_capacity=True)
    except Exception:
        if node and not node.get('spec', {}).get('unschedulable'):
            kubectl('uncordon', instance)
        raise
    if node:
        kubectl('delete', 'node', instance, '--ignore-not-found')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_request(self):
        try:
            config = json.loads(CONFIG.read_text())
            if self.command == 'GET' and self.path == '/':
                return self.respond(200, status(config))
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024:
                return self.respond(400, {'detail': 'Invalid capacity request.'})
            body = json.loads(self.rfile.read(length))
            if self.command == 'POST' and self.path == '/':
                resize(config, body.get('expected'), body.get('desired'))
            elif self.command == 'POST' and self.path == '/remove':
                remove(config, body.get('id'))
            else:
                return self.respond(404, {'detail': 'Unknown capacity action.'})
            self.respond(200, {'status': 'accepted'})
        except CapacityError as error:
            self.respond(409, {'detail': str(error)})
        except Exception:
            self.respond(503, {'detail': 'Could not check GPU capacity. Refresh or check the installation in AWS.'})

    do_GET = handle_request
    do_POST = handle_request


if __name__ == '__main__':
    os.umask(0o077)
    Path(SOCKET).parent.mkdir(parents=True, exist_ok=True)
    Path(SOCKET).unlink(missing_ok=True)
    # A single worker serializes changes, including double clicks and concurrent tabs.
    with socketserver.UnixStreamServer(SOCKET, Handler) as server:
        server.serve_forever()
