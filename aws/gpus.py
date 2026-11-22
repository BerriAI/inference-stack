"""AWS GPU catalogue and quota checks. AWS owns specifications and prices."""

import json
import time

_cache = {}
QUOTAS = {'g': 'L-DB2E81BA', 'p': 'L-417A185B'}


def catalogue(aws, region, zone):
    key = (region, zone)
    saved = _cache.get(key)
    if saved and time.monotonic() - saved[0] < 900:
        return saved[1]
    types = aws('ec2', 'describe-instance-types', filters=[
        {'Name': 'instance-type', 'Values': ['g*', 'p*']},
        {'Name': 'processor-info.supported-architecture', 'Values': ['x86_64']}])['InstanceTypes']
    offered = {row['InstanceType'] for row in aws('ec2', 'describe-instance-type-offerings',
        location_type='availability-zone', filters=[{'Name': 'location', 'Values': [zone]}])['InstanceTypeOfferings']}
    products = aws('pricing', 'get-products', region='us-east-1', service_code='AmazonEC2', filters=[
        {'Type': 'TERM_MATCH', 'Field': field, 'Value': value} for field, value in (
            ('regionCode', region), ('instanceFamily', 'GPU instance'), ('operatingSystem', 'Linux'),
            ('tenancy', 'Shared'), ('preInstalledSw', 'NA'), ('capacitystatus', 'Used'))])['PriceList']
    prices = {}
    for raw in products:
        product = json.loads(raw)
        attributes = product['product']['attributes']
        name = attributes['instanceType']
        if attributes.get('operation') != 'RunInstances' or not attributes.get('usagetype', '').endswith('BoxUsage:' + name):
            continue
        for term in product.get('terms', {}).get('OnDemand', {}).values():
            for dimension in term['priceDimensions'].values():
                price = float(dimension['pricePerUnit'].get('USD', 0))
                if dimension['unit'] == 'Hrs' and price > 0:
                    prices.setdefault(name, set()).add(price)
    options = []
    for item in types:
        name = item['InstanceType']
        devices = item.get('GpuInfo', {}).get('Gpus', [])
        # This release uses an x86 NVIDIA open-kernel AMI: Turing or newer,
        # whole GPUs, EBS boot. Older NVIDIA and fractional GPUs cannot run it.
        if (name not in offered or 'on-demand' not in item['SupportedUsageClasses']
                or 'ebs' not in item['SupportedRootDeviceTypes'] or len(devices) != 1):
            continue
        gpu = devices[0]
        if gpu['Manufacturer'] != 'NVIDIA' or gpu['Count'] < 1 or gpu['Name'] in ('K80', 'M60', 'V100'):
            continue
        price = next(iter(prices[name])) if len(prices.get(name, [])) == 1 else None
        options.append({'instance_type': name, 'name': 'NVIDIA ' + gpu['Name'], 'gpu_count': gpu['Count'],
                        'memory_gb': round(gpu['MemoryInfo']['SizeInMiB'] / 1024, 1),
                        'vcpus': item['VCpuInfo']['DefaultVCpus'], 'host_memory_gb': item['MemoryInfo']['SizeInMiB'] / 1024,
                        'hourly_usd': price, 'quota_code': QUOTAS[name[0]]})
    options.sort(key=lambda option: (option['hourly_usd'] is None, option['hourly_usd'] or 0, option['instance_type']))
    _cache[key] = (time.monotonic(), options)
    return options


def group_type(group):
    return group['MixedInstancesPolicy']['LaunchTemplate']['Overrides'][0]['InstanceType']


def options(aws, config, groups):
    rows = catalogue(aws, config['region'], config['zone'])
    quotas = {code: aws('service-quotas', 'get-service-quota', service_code='ec2', quota_code=code)['Quota']['Value']
              for code in QUOTAS.values()}
    reservations = aws('ec2', 'describe-instances', filters=[
        {'Name': 'instance-state-name', 'Values': ['pending', 'running']},
        {'Name': 'instance-type', 'Values': ['g*', 'p*', 'vt*']}])['Reservations']
    # Omit Spot instances: these quotas apply to On-Demand capacity only.
    instances = [item for reservation in reservations for item in reservation['Instances']
                 if not item.get('InstanceLifecycle')]
    types = {item['InstanceType'] for item in instances}
    types.update(group_type(group) for group in groups if group['DesiredCapacity'])
    specs = {item['InstanceType']: item['VCpuInfo']['DefaultVCpus'] for item in
             aws('ec2', 'describe-instance-types', instance_types=sorted(types))['InstanceTypes']} if types else {}
    used = {code: 0 for code in quotas}
    for item in instances:
        used[QUOTAS['p' if item['InstanceType'].startswith('p') else 'g']] += specs[item['InstanceType']]
    # Reserve requested capacity that EC2 has not yet materialized.
    visible = {item['InstanceId'] for item in instances}
    for group in groups:
        name = group_type(group)
        missing = max(0, group['DesiredCapacity'] - sum(item['InstanceId'] in visible for item in group['Instances']))
        if missing:
            used[QUOTAS[name[0]]] += missing * specs[name]
    result = []
    for row in rows:
        code = row['quota_code']
        remaining = max(0, quotas[code] - used[code])
        blocker = ''
        if row['hourly_usd'] is None:
            blocker = 'AWS did not return an On-Demand price. Refresh before launching.'
        elif row['vcpus'] > remaining:
            blocker = f"Needs {row['vcpus']} vCPUs; {int(remaining)} remain in your AWS quota. Request a quota increase or stop other instances."
        result.append({**row, 'blocker': blocker, 'quota_available_vcpus': remaining,
                       'quota_url': f"https://{config['region']}.console.aws.amazon.com/servicequotas/home?region={config['region']}#!/services/ec2/quotas/{code}"})
    return {'options': result, 'region': config['region']}
