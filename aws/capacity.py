#!/usr/bin/env python3
"""Manage this stack's GPU machines. Only the launcher can reach this Unix socket."""

import datetime as dt
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socketserver
import subprocess

import gpus

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


def groups(config):
    rows = aws('autoscaling', 'describe-auto-scaling-groups', auto_scaling_group_names=config['groups'])['AutoScalingGroups']
    if {row['AutoScalingGroupName'] for row in rows} != set(config['groups']):
        raise CapacityError('A GPU group is missing. Check this installation in AWS.')
    return rows


def selected_gpu(config, current, instance_type, hourly_usd):
    rows = gpus.options(aws, config, current)['options']
    selected = next((row for row in rows if row['instance_type'] == instance_type), None)
    if selected is None:
        raise CapacityError('Choose a GPU offered in this installation’s availability zone.')
    if selected['blocker']:
        raise CapacityError(selected['blocker'] + ' No GPU was added.')
    if type(hourly_usd) not in (float, int) or hourly_usd != selected['hourly_usd']:
        raise CapacityError('The GPU price changed. Refresh and review its price before launching.')
    return selected


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
    current = groups(config)
    nodes = {n['metadata']['name']: n for n in gpu_nodes()}
    pods = kubectl('get', 'pods', '--all-namespaces')['items']
    members = {i['InstanceId'] for row in current for i in row['Instances']}
    for name, node in nodes.items():
        ready = any(c['type'] == 'Ready' and c['status'] == 'True' for c in node.get('status', {}).get('conditions', []))
        if name not in members and not ready:
            kubectl('delete', 'node', name, '--ignore-not-found')
    machines = []
    specs = {}
    if any(row['Instances'] or row['DesiredCapacity'] for row in current):
        try:
            specs = {row['instance_type']: row for row in gpus.catalogue(aws, config['region'], config['zone'])}
        except CapacityError:
            pass  # Keep removal available if the catalogue or price API is down.
    for item in [item for row in current for item in row['Instances']]:
        # Worker names are their instance IDs; no address is exposed to the browser.
        node = nodes.get(item['InstanceId'], {})
        ready = any(c['type'] == 'Ready' and c['status'] == 'True' for c in node.get('status', {}).get('conditions', []))
        ready = ready and int(node.get('status', {}).get('allocatable', {}).get('nvidia.com/gpu', 0)) > 0
        state = 'removing' if item['LifecycleState'].startswith('Terminating') else 'ready' if ready else 'starting'
        models = sorted({p['metadata']['labels']['model'] for p in pods
                         if p.get('spec', {}).get('nodeName') == item['InstanceId']
                         and p.get('status', {}).get('phase') not in ('Succeeded', 'Failed')
                         and p['metadata'].get('labels', {}).get('model')})
        machines.append({**specs.get(item['InstanceType'], {'instance_type': item['InstanceType'], 'name': item['InstanceType'], 'memory_gb': None, 'gpu_count': None, 'hourly_usd': None}), 'id': item['InstanceId'], 'state': state, 'models': models})
    stop = deadline(config)
    error = ''
    pending = 0
    for row in current:
        if row['DesiredCapacity'] and not row['Instances']:
            instance_type = gpus.group_type(row)
            machines.append({**specs.get(instance_type, {'instance_type': instance_type, 'name': instance_type}),
                             'id': 'pending-' + str(config['groups'].index(row['AutoScalingGroupName'])),
                             'state': 'starting', 'models': []})
        if row['DesiredCapacity'] > sum(item.get('LifecycleState') == 'InService' and any(m['id'] == item['InstanceId'] and m['state'] == 'ready' for m in machines) for item in row['Instances']):
            pending += 1
            activities = aws('autoscaling', 'describe-scaling-activities', auto_scaling_group_name=row['AutoScalingGroupName'], max_records=1)['Activities']
            if activities and activities[0]['StatusCode'] in ('Failed', 'Cancelled'):
                error = 'AWS could not start a selected GPU. Check quota and capacity in AWS, or cancel the pending machine and choose another type.'
    return {'desired': sum(row['DesiredCapacity'] for row in current), 'limit': len(current), 'machines': machines,
            'pending': pending, 'stop_at': stop.isoformat() if stop else None, 'error': error,
            'hours': config['hours'], 'region': config['region']}


def set_deadline(config, stop):
    aws('scheduler', 'update-schedule', name=config['schedule'], state='ENABLED',
        schedule_expression=f'at({stop.strftime("%Y-%m-%dT%H:%M:%S")})', schedule_expression_timezone='UTC',
        flexible_time_window={'Mode': 'OFF'}, target={
            'Arn': config['stop_function'],
            'RoleArn': config['stop_role'],
            'Input': '{}',
            'RetryPolicy': {'MaximumEventAgeInSeconds': 3600, 'MaximumRetryAttempts': 3}})


def resize(config, expected, target, instance_type=None, hourly_usd=None):
    current = groups(config)
    desired = sum(row['DesiredCapacity'] for row in current)
    if type(expected) is not int or expected != desired:
        raise CapacityError('GPU capacity changed. Refresh and try again.')
    if type(target) is not int or not 0 <= target <= len(current):
        raise CapacityError('Choose a machine count within this installation’s limit.')
    if target == expected + 1:
        selected_gpu(config, current, instance_type, hourly_usd)
        available = next((row for row in current if row['DesiredCapacity'] == 0 and not row['Instances']), None)
        if available is None:
            raise CapacityError('Wait for a removed GPU to finish shutting down, then retry.')
        stop = deadline(config)
        if expected and (not stop or stop <= now() + dt.timedelta(minutes=2)):
            raise CapacityError('This GPU session is ending. Remove the GPUs before starting another session.')
        if not expected:
            if any(row['Instances'] for row in current):
                raise CapacityError('Wait for the previous GPUs to finish shutting down, then retry.')
            stop = now() + dt.timedelta(hours=config['hours'])
        # Schedule first: never create billable capacity without its independent deadline.
        set_deadline(config, stop)
        # Each CloudFormation-owned group contains one machine. Changing an empty
        # group cannot change the type or replacement cost of an existing GPU.
        aws('autoscaling', 'update-auto-scaling-group', auto_scaling_group_name=available['AutoScalingGroupName'],
            min_size=0, max_size=1, vpc_zone_identifier=config['subnet'], mixed_instances_policy={
                'LaunchTemplate': {'LaunchTemplateSpecification': {'LaunchTemplateId': config['template'], 'Version': config['template_version']},
                                   'Overrides': [{'InstanceType': instance_type}]},
                'InstancesDistribution': {'OnDemandPercentageAboveBaseCapacity': 100}})
        aws('autoscaling', 'set-desired-capacity', auto_scaling_group_name=available['AutoScalingGroupName'], desired_capacity=1)
        return {'status': 'accepted', 'instance_type': instance_type}
    if target != 0:
        raise CapacityError('Add one machine at a time, or remove a specific GPU.')
    members = {i['InstanceId'] for row in current for i in row['Instances']}
    if any(n['metadata']['name'] in members for n in gpu_nodes()):
        raise CapacityError('Remove each GPU before cancelling the remaining pending capacity.')
    for row in current:
        if row['DesiredCapacity']:
            aws('autoscaling', 'set-desired-capacity', auto_scaling_group_name=row['AutoScalingGroupName'], desired_capacity=0)


def remove(config, instance):
    current = groups(config)
    if isinstance(instance, str) and instance.startswith('pending-'):
        slot = int(instance.removeprefix('pending-'))
        if not 0 <= slot < len(config['groups']):
            raise CapacityError('Unknown GPU machine.')
        row = next(row for row in current if row['AutoScalingGroupName'] == config['groups'][slot])
        if row['Instances']:
            raise CapacityError('This machine has started. Refresh and remove it from GPUs.')
        if row['DesiredCapacity']:
            aws('autoscaling', 'set-desired-capacity', auto_scaling_group_name=row['AutoScalingGroupName'], desired_capacity=0)
        return
    item = next((i for row in current for i in row['Instances'] if i['InstanceId'] == instance), None)
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
            if self.command == 'GET' and self.path == '/options':
                return self.respond(200, gpus.options(aws, config, groups(config)))
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 1024:
                return self.respond(400, {'detail': 'Invalid capacity request.'})
            body = json.loads(self.rfile.read(length))
            if self.command == 'POST' and self.path == '/':
                resize(config, body.get('expected'), body.get('desired'), body.get('instance_type'), body.get('hourly_usd'))
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
