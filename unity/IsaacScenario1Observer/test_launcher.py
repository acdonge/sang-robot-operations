import copy
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import launch_observer as observer

HASH = '0123456789abcdef' * 4
SESSION = '0123456789abcdef' * 2


def fixture():
    return dict(observer.IDENTITY, scene_sha256=HASH, session_id=SESSION, epoch=0,
                sequence=1, sim_time_ns=100, received_at_unix_ms=1000000,
                phase='mobile_delivery', plate_owner='carrier', assets=[
                    dict(asset_id=identity, joint_names=list(names), joint_positions_rad=[0.0]*len(names),
                         position_m=[0.0, 0.0, 0.0], rotation_xyzw=[0.0, 0.0, 0.0, 1.0])
                    for identity, names in observer.JOINTS.items()])


class LauncherTests(unittest.TestCase):
    def test_fresh_pin(self):
        self.assertEqual(observer.validate_snapshot(fixture(), 1000000, HASH), (SESSION, 0))

    def test_named_order(self):
        value = fixture()
        value['assets'].reverse()
        for asset in value['assets']:
            asset['joint_names'].reverse()
        self.assertEqual(observer.validate_snapshot(value, 1000000, HASH), (SESSION, 0))

    def test_rejections(self):
        changes = [('schema_version', 1), ('schema_version', True), ('scene_sha256', 'a'*64),
                   ('session_id', 'a'*31), ('epoch', -1), ('epoch', 0.5), ('epoch', False),
                   ('sequence', 2**63), ('sim_time_ns', '10'), ('physical_output_connected', True),
                   ('frame_id', 'world'), ('received_at_unix_ms', 999499), ('received_at_unix_ms', 1000101),
                   ('plate_owner', 'invalid'), ('phase', 'x\n'), ('assets', [])]
        for key, val in changes:
            with self.subTest(key=key, value=val):
                value = fixture(); value[key] = val
                with self.assertRaises(ValueError):
                    observer.validate_snapshot(value, 1000000, HASH)

    def test_nested_rejections(self):
        mutations = [lambda a: a.update(extra=1), lambda a: a.pop('joint_names'),
                     lambda a: a.update(asset_id='physical-omy'),
                     lambda a: a.update(joint_names=['joint1']*7),
                     lambda a: a.update(joint_positions_rad=[float('inf')]*7),
                     lambda a: a.update(joint_positions_rad=[False]*7),
                     lambda a: a.update(position_m=[0, 0]),
                     lambda a: a.update(position_m=[float('nan'), 0, 0]),
                     lambda a: a.update(rotation_xyzw=[0, 0, 0, 0])]
        for mutation in mutations:
            value = fixture(); mutation(value['assets'][0])
            with self.assertRaises(ValueError):
                observer.validate_snapshot(value, 1000000, HASH)

    def test_duplicate_asset(self):
        value = fixture(); value['assets'][3] = copy.deepcopy(value['assets'][2])
        with self.assertRaises(ValueError):
            observer.validate_snapshot(value, 1000000, HASH)

    def test_duplicate_json(self):
        with self.assertRaises(ValueError):
            json.loads('{"epoch":0,"epoch":0}', object_pairs_hook=observer.unique_object)

    def test_scene_hash_never_from_snapshot(self):
        for digest in ('', None, 'a'*64):
            with self.assertRaises(ValueError):
                observer.validate_snapshot(fixture(), 1000000, digest)

    def test_private_receiver_file_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'observer'
            root.mkdir()
            value = fixture(); value['received_at_unix_ms'] = time.time_ns() // 1_000_000
            snapshot = root / 'telemetry.json'
            snapshot.write_text(json.dumps(value))
            with patch.object(observer, 'ROOT', root):
                self.assertEqual(observer.read_pin(HASH), (SESSION, 0))
                snapshot.chmod(0o666)
                with self.assertRaises(ValueError):
                    observer.read_pin(HASH)
                snapshot.chmod(0o600)
                root.chmod(0o777)
                with self.assertRaises(ValueError):
                    observer.read_pin(HASH)
                root.chmod(0o700)
                snapshot.rename(root / 'real.json')
                snapshot.symlink_to(root / 'real.json')
                with self.assertRaises(OSError):
                    observer.read_pin(HASH)

    def test_missing_and_extra_fields(self):
        for change in (lambda x: x.pop('physical_output_connected'), lambda x: x.update(command=1)):
            value = fixture(); change(value)
            with self.assertRaises(ValueError):
                observer.validate_snapshot(value, 1000000, HASH)

    @patch.object(observer.subprocess, 'Popen')
    def test_child_timeout_cleans_only_child(self, popen):
        child = Mock(); popen.return_value = child
        child.wait.side_effect = [subprocess.TimeoutExpired('observer', 2), 0]
        child.poll.return_value = None
        self.assertEqual(observer.run_child(SESSION, 7, 2), 0)
        child.terminate.assert_called_once_with()
        child.kill.assert_not_called()
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], str(observer.PLAYER))
        self.assertEqual(kwargs['env']['SANG_SCENARIO1_SESSION_ID'], SESSION)
        self.assertEqual(kwargs['env']['SANG_SCENARIO1_EPOCH'], '7')

    @patch.object(observer.subprocess, 'Popen')
    def test_child_timeout_escalation(self, popen):
        child = Mock(); popen.return_value = child; child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('observer', 2), subprocess.TimeoutExpired('observer', 5), 0]
        self.assertEqual(observer.run_child(SESSION, 0, 2), 0)
        child.kill.assert_called_once_with()

    @patch.object(observer.subprocess, 'Popen')
    def test_interrupt_cleanup(self, popen):
        child = Mock(); popen.return_value = child; child.poll.return_value = None
        child.wait.side_effect = [KeyboardInterrupt, 0]
        with self.assertRaises(KeyboardInterrupt):
            observer.run_child(SESSION, 0, 240)
        child.terminate.assert_called_once_with()

    @patch.object(observer.subprocess, 'Popen')
    def test_completed_child_untouched(self, popen):
        child = Mock(); popen.return_value = child; child.wait.return_value = 4; child.poll.return_value = 4
        self.assertEqual(observer.run_child(SESSION, 0, 240), 4)
        child.terminate.assert_not_called()

    @patch.object(observer, 'run_child')
    @patch.object(observer, 'read_pin', return_value=(SESSION, 0))
    @patch.object(observer, 'trusted_hash', return_value=HASH)
    def test_check_only_never_launches(self, trusted, pin, child):
        with patch('sys.argv', ['launch_observer.py', '--check-only']):
            self.assertEqual(observer.main(), 0)
        child.assert_not_called()


if __name__ == '__main__':
    unittest.main()
