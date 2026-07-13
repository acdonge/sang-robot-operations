from types import SimpleNamespace

import numpy as np
import pytest

from recorder_core.assembly import (
    ExtraFeatureError,
    MissingFeatureError,
    NonFiniteValueError,
    assemble_joint_state,
    assemble_joint_trajectory,
)


FEATURE_NAMES = ("left_a", "left_b", "right_a", "right_b")


def joint_state(names, positions):
    return SimpleNamespace(name=names, position=positions)


def trajectory(names, positions):
    point = SimpleNamespace(positions=positions)
    return SimpleNamespace(joint_names=names, points=[point])


def test_joint_state_split_inputs_assemble_in_manifest_order():
    messages = [
        joint_state(["left_a", "left_b"], [1.0, 2.0]),
        joint_state(["right_a", "right_b"], [3.0, 4.0]),
    ]

    result = assemble_joint_state(messages, FEATURE_NAMES)

    np.testing.assert_array_equal(result, np.array([1, 2, 3, 4], np.float32))
    assert result.dtype == np.float32


def test_joint_trajectory_name_order_is_irrelevant():
    messages = [
        trajectory(["right_b", "right_a"], [4.0, 3.0]),
        trajectory(["left_b", "left_a"], [2.0, 1.0]),
    ]

    result = assemble_joint_trajectory(messages, FEATURE_NAMES)

    np.testing.assert_array_equal(result, np.array([1, 2, 3, 4], np.float32))


def test_missing_name_is_explicit():
    messages = [joint_state(["left_a", "left_b", "right_a"], [1, 2, 3])]

    with pytest.raises(MissingFeatureError, match="right_b"):
        assemble_joint_state(messages, FEATURE_NAMES)


def test_extra_name_is_explicit():
    messages = [
        joint_state(
            ["left_a", "left_b", "right_a", "right_b", "surprise"],
            [1, 2, 3, 4, 5],
        )
    ]

    with pytest.raises(ExtraFeatureError, match="surprise"):
        assemble_joint_state(messages, FEATURE_NAMES)


def test_nan_is_explicit():
    messages = [joint_state(FEATURE_NAMES, [1, 2, np.nan, 4])]

    with pytest.raises(NonFiniteValueError, match="right_a"):
        assemble_joint_state(messages, FEATURE_NAMES)
