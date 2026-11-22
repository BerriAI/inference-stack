"""AWS catalogue parsing and account-wide On-Demand quota accounting."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'aws'))
import gpus


def spec(name='g5.xlarge', count=1, manufacturer='NVIDIA'):
    return {'InstanceType': name, 'SupportedUsageClasses': ['on-demand'], 'SupportedRootDeviceTypes': ['ebs'],
            'VCpuInfo': {'DefaultVCpus': 4}, 'MemoryInfo': {'SizeInMiB': 16384},
            'GpuInfo': {'Gpus': [{'Manufacturer': manufacturer, 'Name': 'A10G', 'Count': count,
                                 'MemoryInfo': {'SizeInMiB': 22888}}]}}


def price(name, amount, usage=None):
    return json.dumps({'product': {'attributes': {'instanceType': name, 'operation': 'RunInstances',
                         'usagetype': usage or 'USE2-BoxUsage:' + name}},
                      'terms': {'OnDemand': {'term': {'priceDimensions': {
                          'hour': {'unit': 'Hrs', 'pricePerUnit': {'USD': str(amount)}}}}}}})


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        gpus._cache.clear()

    def test_catalogue_uses_aws_specs_offerings_and_positive_hourly_price(self):
        data = {'describe-instance-types': {'InstanceTypes': [spec(), spec('g6.xlarge', count=4), spec('g6f.large', count=0), spec('g4ad.xlarge', manufacturer='AMD'), spec('g5.8xlarge')]},
                'describe-instance-type-offerings': {'InstanceTypeOfferings': [{'InstanceType': n} for n in ('g5.xlarge','g6.xlarge','g6f.large','g4ad.xlarge')]},
                'get-products': {'PriceList': [price('g5.xlarge',1.006), price('g6.xlarge',2), price('g5.xlarge',0), price('g5.xlarge',999,'USE2-UnusedBox:g5.xlarge')]}}
        aws = Mock(side_effect=lambda service, action, **kw: data[action])
        rows = gpus.catalogue(aws, 'us-east-2', 'example-zone')
        self.assertEqual([r['instance_type'] for r in rows], ['g5.xlarge', 'g6.xlarge'])
        self.assertEqual(rows[0]['hourly_usd'], 1.006)
        self.assertEqual(rows[1]['gpu_count'], 4)
        self.assertEqual(rows[1]['memory_gb'], 22.4)  # Memory is per device, not multiplied by count.
        self.assertEqual(gpus.catalogue(aws, 'us-east-2', 'example-zone'), rows)
        self.assertEqual(aws.call_count, 3)
        offerings = aws.call_args_list[1].kwargs
        self.assertEqual(offerings['location_type'], 'availability-zone')
        self.assertEqual(offerings['filters'][0]['Values'], ['example-zone'])

    def test_missing_or_ambiguous_price_is_not_free(self):
        data = {'describe-instance-types': {'InstanceTypes': [spec()]},
                'describe-instance-type-offerings': {'InstanceTypeOfferings': [{'InstanceType':'g5.xlarge'}]},
                'get-products': {'PriceList': [price('g5.xlarge',1),price('g5.xlarge',2)]}}
        rows = gpus.catalogue(lambda service, action, **kw: data[action], 'us-east-2','example-zone')
        self.assertIsNone(rows[0]['hourly_usd'])

    def test_quota_counts_other_instances_and_pending_requests_but_not_spot(self):
        rows = [{'instance_type':'g5.xlarge','vcpus':4,'hourly_usd':1,'quota_code':gpus.QUOTAS['g']},
                {'instance_type':'p5.4xlarge','vcpus':16,'hourly_usd':4,'quota_code':gpus.QUOTAS['p']}]
        groups = [{'DesiredCapacity':1, 'Instances':[], 'MixedInstancesPolicy':{'LaunchTemplate':{'Overrides':[{'InstanceType':'g5.xlarge'}]}}}]
        def aws(service, action, **kw):
            if action == 'get-service-quota': return {'Quota':{'Value':12}}
            if action == 'describe-instances': return {'Reservations':[{'Instances':[
                {'InstanceId':'test-other','InstanceType':'g5.xlarge'},
                {'InstanceId':'test-spot','InstanceType':'g5.xlarge','InstanceLifecycle':'spot'}]}]}
            return {'InstanceTypes':[spec()]}
        with patch.object(gpus,'catalogue',return_value=rows):
            result = gpus.options(aws,{'region':'us-east-2','zone':'example-zone'},groups)['options']
        self.assertEqual(result[0]['quota_available_vcpus'],4)
        self.assertEqual(result[0]['blocker'],'')
        self.assertIn('Needs 16 vCPUs',result[1]['blocker'])
        self.assertEqual(result[1]['quota_available_vcpus'],12)

    def test_materialized_pending_request_is_not_counted_twice(self):
        row={'instance_type':'g5.xlarge','vcpus':4,'hourly_usd':1,'quota_code':gpus.QUOTAS['g']}
        group={'DesiredCapacity':1,'Instances':[{'InstanceId':'test-worker'}],
               'MixedInstancesPolicy':{'LaunchTemplate':{'Overrides':[{'InstanceType':'g5.xlarge'}]}}}
        def aws(service,action,**kw):
            if action=='get-service-quota':return {'Quota':{'Value':8}}
            if action=='describe-instances':return {'Reservations':[{'Instances':[{'InstanceId':'test-worker','InstanceType':'g5.xlarge'}]}]}
            return {'InstanceTypes':[spec()]}
        with patch.object(gpus,'catalogue',return_value=[row]):
            result=gpus.options(aws,{'region':'us-east-2','zone':'example-zone'},[group])
        self.assertEqual(result['options'][0]['quota_available_vcpus'],4)
