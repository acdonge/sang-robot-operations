import copy
import unittest

import launch_observer as launcher


class LauncherValidationTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = dict(launcher.IDENTITY, session_id="0123456789abcdef0123456789abcdef",
                             epoch=1, sequence=10, sim_time_ns=1000, received_at_unix_ms=1_000_000,
                             joint_names=sorted(launcher.JOINTS), joint_positions_rad=[0.0] * 6)

    def test_fresh_epoch_is_pinned(self):
        self.assertEqual(launcher.validate_snapshot(self.snapshot, 1_000_000), (self.snapshot["session_id"], 1))

    def test_invalid_snapshots_are_rejected(self):
        cases = [("epoch", -1), ("epoch", True), ("sequence", 1.2),
                 ("physical_output_connected", True), ("runtime_kind", "other"), ("session_id", "bad"),
                 ("joint_names", ["base"] * 6), ("joint_positions_rad", [float("nan")] * 6),
                 ("joint_positions_rad", [10**400] * 6), ("received_at_unix_ms", 999_499)]
        for key, value in cases:
            with self.subTest(field=key, value=value):
                snapshot = copy.deepcopy(self.snapshot)
                snapshot[key] = value
                with self.assertRaises(ValueError):
                    launcher.validate_snapshot(snapshot, 1_000_000)

    def test_duplicate_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            launcher.unique_object([("epoch", 0), ("epoch", 1)])


if __name__ == "__main__":
    unittest.main()
