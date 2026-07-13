"""Name-based assembly of manifest-ordered feature vectors."""

from collections.abc import Iterable, Sequence
from typing import Any

import numpy as np


class AssemblyError(ValueError):
    """Base class for feature assembly failures."""


class MissingFeatureError(AssemblyError):
    """Raised when a manifest feature has no input value."""


class ExtraFeatureError(AssemblyError):
    """Raised when an input contains a name outside the manifest."""


class DuplicateFeatureError(AssemblyError):
    """Raised when more than one input provides the same feature."""


class NonFiniteValueError(AssemblyError):
    """Raised when an assembled value is NaN or infinite."""


class MalformedMessageError(AssemblyError):
    """Raised when a message does not contain aligned names and positions."""


def _assemble(
    named_positions: Iterable[tuple[Sequence[str], Sequence[Any]]],
    feature_names: Sequence[str],
) -> np.ndarray:
    expected = tuple(feature_names)
    expected_set = set(expected)
    if len(expected) != len(expected_set):
        raise DuplicateFeatureError("manifest feature_names contains duplicates")

    values: dict[str, Any] = {}
    for names, positions in named_positions:
        if len(names) != len(positions):
            raise MalformedMessageError(
                f"names/positions length mismatch: {len(names)} != {len(positions)}"
            )
        for name, position in zip(names, positions, strict=True):
            if name not in expected_set:
                raise ExtraFeatureError(f"unexpected feature name: {name}")
            if name in values:
                raise DuplicateFeatureError(f"duplicate feature name: {name}")
            values[name] = position

    missing = [name for name in expected if name not in values]
    if missing:
        raise MissingFeatureError(
            "missing feature names: " + ", ".join(missing)
        )

    try:
        with np.errstate(over="ignore", invalid="ignore"):
            vector = np.asarray([values[name] for name in expected], dtype=np.float32)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MalformedMessageError(f"positions must be numeric: {exc}") from exc

    if not np.all(np.isfinite(vector)):
        invalid = [
            name for name, value in zip(expected, vector, strict=True)
            if not np.isfinite(value)
        ]
        raise NonFiniteValueError(
            "non-finite values for feature names: " + ", ".join(invalid)
        )
    return vector


def assemble_joint_state(
    messages: Iterable[Any], feature_names: Sequence[str]
) -> np.ndarray:
    """Assemble JointState-like messages in manifest feature order."""
    named_positions = []
    for message in messages:
        try:
            named_positions.append((message.name, message.position))
        except AttributeError as exc:
            raise MalformedMessageError(
                "JointState-like message requires name and position"
            ) from exc
    return _assemble(named_positions, feature_names)


def assemble_joint_trajectory(
    messages: Iterable[Any], feature_names: Sequence[str]
) -> np.ndarray:
    """Assemble first-point positions from JointTrajectory-like messages."""
    named_positions = []
    for message in messages:
        try:
            if not message.points:
                raise MalformedMessageError(
                    "JointTrajectory-like message requires points[0]"
                )
            named_positions.append(
                (message.joint_names, message.points[0].positions)
            )
        except AttributeError as exc:
            raise MalformedMessageError(
                "JointTrajectory-like message requires joint_names and points[0].positions"
            ) from exc
    return _assemble(named_positions, feature_names)
