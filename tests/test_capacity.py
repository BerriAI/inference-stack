"""Billing lifecycle checks, without AWS calls or real instances."""
import datetime as dt
import importlib.util
from pathlib import Path
import unittest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "aws"))
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('capacity', Path(__file__).resolve().parents[1] / 'aws/capacity.py')
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.config = {'groups': ['test-only-group', 'test-group-2'], 'schedule': 'test-only-schedule', 'stop_role': 'test-only-role', 'hours': 4, 'subnet': 'test-subnet', 'template': 'test-template', 'template_version': '1'}
        self.current = {'AutoScalingGroupName': 'test-only-group', 'DesiredCapacity': 0, 'MaxSize': 1, 'Instances': []}
        self.other = {'AutoScalingGroupName': 'test-group-2', 'DesiredCapacity': 0, 'MaxSize': 1, 'Instances': []}
        self.group = patch.object(capacity, 'groups', return_value=[self.current, self.other]).start()
        self.aws = patch.object(capacity, 'aws', return_value={'Quota': {'Value': 32}}).start()
        self.selection = patch.object(capacity, 'selected_gpu').start()
        self.nodes = patch.object(capacity, 'gpu_nodes', return_value=[]).start()
        self.clock = dt.datetime(2026, 11, 22, 12, tzinfo=dt.timezone.utc)
        patch.object(capacity, 'now', return_value=self.clock).start()
        self.deadline = patch.object(capacity, 'deadline', return_value=None).start()
        self.schedule = patch.object(capacity, 'set_deadline').start()
        self.addCleanup(patch.stopall)

    def test_cordon_does_not_use_unsupported_json_output_flag(self):
        with patch.object(capacity, 'run') as run:
            capacity.kubectl('cordon', 'test-node')
        run.assert_called_once_with('/usr/local/bin/k3s', 'kubectl', 'cordon', 'test-node', json_output=False)

    def test_quota_failure_creates_no_schedule_or_capacity(self):
        self.selection.side_effect = capacity.CapacityError('Quota is zero. No GPU was added.')
        with self.assertRaisesRegex(capacity.CapacityError, 'No GPU was added'):
            capacity.resize(self.config, 0, 1)
        self.schedule.assert_not_called()
        self.aws.assert_not_called()

    def test_schedule_is_created_before_billable_capacity(self):
        order = []
        self.schedule.side_effect = lambda *a: order.append('schedule')
        def aws(service, action, **kw):
            if action == 'set-desired-capacity': order.append('launch')
            return {'Quota': {'Value': 32}}
        self.aws.side_effect = aws
        capacity.resize(self.config, 0, 1)
        self.assertEqual(order, ['schedule', 'launch'])
        self.schedule.assert_called_once_with(self.config, self.clock + dt.timedelta(hours=4))

    def test_failed_schedule_does_not_launch(self):
        self.schedule.side_effect = capacity.CapacityError('Schedule unavailable')
        with self.assertRaises(capacity.CapacityError): capacity.resize(self.config, 0, 1)
        self.assertFalse(any(c.args[1] == 'set-desired-capacity' for c in self.aws.call_args_list))

    def test_repeated_submission_does_not_add_another_gpu(self):
        self.current['DesiredCapacity'] = 1
        with self.assertRaisesRegex(capacity.CapacityError, 'capacity changed'):
            capacity.resize(self.config, 0, 1)
        self.aws.assert_not_called()

    def test_adding_gpu_does_not_extend_existing_deadline(self):
        self.current['DesiredCapacity'] = 1
        original = self.clock + dt.timedelta(hours=1)
        self.deadline.return_value = original
        capacity.resize(self.config, 1, 2)
        self.schedule.assert_called_once_with(self.config, original)

    def test_expired_session_cannot_silently_rent_more(self):
        self.current['DesiredCapacity'] = 1
        self.deadline.return_value = self.clock - dt.timedelta(seconds=1)
        with self.assertRaisesRegex(capacity.CapacityError, 'session is ending'):
            capacity.resize(self.config, 1, 2)
        self.schedule.assert_not_called()

    def test_cancelling_pending_capacity_goes_to_zero(self):
        self.current['DesiredCapacity'] = 1
        capacity.resize(self.config, 1, 0)
        self.aws.assert_called_once_with('autoscaling', 'set-desired-capacity', auto_scaling_group_name='test-only-group', desired_capacity=0)

    def test_removal_checks_workloads_after_cordoning_and_restores_used_node(self):
        self.current.update(DesiredCapacity=1, Instances=[{'InstanceId': 'i-0123456789abcdef0', 'LifecycleState': 'InService'}])
        self.nodes.return_value = [{'metadata': {'name': 'i-0123456789abcdef0'}}]
        order = []
        with patch.object(capacity, 'kubectl', side_effect=lambda *a: order.append(a[0])), patch.object(capacity, 'workloads', side_effect=lambda *a: order.append('check') or [object()]):
            with self.assertRaisesRegex(capacity.CapacityError, 'in use'):
                capacity.remove(self.config, 'i-0123456789abcdef0')
        self.assertEqual(order, ['cordon', 'check', 'uncordon'])
        self.aws.assert_not_called()

    def test_removal_decrements_capacity_and_retry_cannot_remove_another(self):
        self.current.update(DesiredCapacity=1, Instances=[{'InstanceId': 'i-0123456789abcdef0', 'LifecycleState': 'InService'}])
        capacity.remove(self.config, 'i-0123456789abcdef0')
        self.aws.assert_called_once_with('autoscaling', 'terminate-instance-in-auto-scaling-group', instance_id='i-0123456789abcdef0', should_decrement_desired_capacity=True)
        self.current['Instances'][0]['LifecycleState'] = 'Terminating'
        capacity.remove(self.config, 'i-0123456789abcdef0')
        self.assertEqual(self.aws.call_count, 1)

    def test_cancel_cannot_bypass_workload_checks(self):
        self.current['DesiredCapacity'] = 1
        self.current['Instances'] = [{'InstanceId': 'test-node'}]
        self.nodes.return_value = [{'metadata': {'name': 'test-node'}}]
        with self.assertRaises(capacity.CapacityError): capacity.resize(self.config, 1, 0)
        self.aws.assert_not_called()

    def test_new_type_updates_only_an_empty_group(self):
        self.current.update(DesiredCapacity=1, Instances=[{'InstanceId':'test-existing'}])
        self.deadline.return_value=self.clock+dt.timedelta(hours=1)
        capacity.resize(self.config,1,2,'g6e.xlarge',1.86)
        update=next(call for call in self.aws.call_args_list if call.args[1]=='update-auto-scaling-group')
        self.assertEqual(update.kwargs['auto_scaling_group_name'],'test-group-2')
        self.assertEqual(update.kwargs['mixed_instances_policy']['LaunchTemplate']['Overrides'],[{'InstanceType':'g6e.xlarge'}])
        self.assertEqual(self.current['Instances'],[{'InstanceId':'test-existing'}])

    def test_failed_type_update_never_launches_a_default_machine(self):
        self.aws.side_effect=capacity.CapacityError('Could not set type')
        with self.assertRaises(capacity.CapacityError):capacity.resize(self.config,0,1,'g6e.xlarge',1.86)
        self.assertFalse(any(call.args[1]=='set-desired-capacity' for call in self.aws.call_args_list))

    def test_cancelling_one_pending_machine_keeps_other_groups(self):
        self.current['DesiredCapacity']=1
        self.other['DesiredCapacity']=1
        capacity.remove(self.config,'pending-1')
        self.aws.assert_called_once_with('autoscaling','set-desired-capacity',auto_scaling_group_name='test-group-2',desired_capacity=0)

    def test_pending_cancellation_cannot_remove_a_machine_that_has_started(self):
        self.other.update(DesiredCapacity=1,Instances=[{'InstanceId':'test-new'}])
        with self.assertRaisesRegex(capacity.CapacityError,'has started'):capacity.remove(self.config,'pending-1')
        self.aws.assert_not_called()


class SelectionTests(unittest.TestCase):
    def test_unknown_type_stale_price_and_quota_are_rejected(self):
        row={'instance_type':'g5.xlarge','hourly_usd':1.006,'blocker':''}
        with patch.object(capacity.gpus,'options',return_value={'options':[row]}):
            for name,price in [('p5.4xlarge',1.006),('g5.xlarge',1),('g5.xlarge',None)]:
                with self.assertRaises(capacity.CapacityError):capacity.selected_gpu({},[],name,price)
            self.assertEqual(capacity.selected_gpu({},[],'g5.xlarge',1.006),row)
            row['blocker']='Quota exceeded'
            with self.assertRaisesRegex(capacity.CapacityError,'No GPU was added'):
                capacity.selected_gpu({},[],'g5.xlarge',1.006)


if __name__ == '__main__':
    unittest.main()
