"""Billing lifecycle checks, without AWS calls or real instances."""
import datetime as dt
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('capacity', Path(__file__).resolve().parents[1] / 'aws/capacity.py')
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.config = {'group': 'test-only-group', 'schedule': 'test-only-schedule', 'stop_role': 'test-only-role', 'hours': 4}
        self.current = {'DesiredCapacity': 0, 'MaxSize': 4, 'Instances': []}
        self.group = patch.object(capacity, 'group', return_value=self.current).start()
        self.aws = patch.object(capacity, 'aws', return_value={'Quota': {'Value': 32}}).start()
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
        self.aws.return_value = {'Quota': {'Value': 0}}
        with self.assertRaisesRegex(capacity.CapacityError, 'No GPU was added'):
            capacity.resize(self.config, 0, 1)
        self.schedule.assert_not_called()
        self.assertEqual(self.aws.call_count, 1)

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


if __name__ == '__main__':
    unittest.main()
