"""Manifest-driven ROS 2 subscriptions whose callbacks only update buffers."""

from dataclasses import dataclass
from typing import Any

from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import CompressedImage, JointState
from trajectory_msgs.msg import JointTrajectory

from recorder_core.buffers import LatestValueBuffer
from recorder_core.manifest import Manifest, ManifestValidationError


@dataclass(frozen=True)
class TopicBuffer:
    topic: str
    buffer: LatestValueBuffer


@dataclass(frozen=True)
class SourceBuffers:
    key: str
    freshness_ms: int
    topics: tuple[TopicBuffer, ...]


@dataclass
class ManifestSubscriptions:
    sources: dict[str, SourceBuffers]
    subscriptions: list[Any]


def _validate_message_type(actual: str, expected: str, source_key: str) -> None:
    if actual != expected:
        raise ManifestValidationError(
            f"interface {source_key!r} message_type must be {expected!r}, got {actual!r}"
        )


def create_subscriptions(node: Any, manifest: Manifest) -> ManifestSubscriptions:
    """Create all manifest subscriptions and their per-topic latest buffers."""
    sources: dict[str, SourceBuffers] = {}
    subscriptions: list[Any] = []

    def add_source(key, freshness_ms, topics, message_class):
        topic_buffers = []
        for topic in topics:
            buffer = LatestValueBuffer()
            subscription = node.create_subscription(
                message_class,
                topic,
                buffer.update,
                qos_profile_sensor_data,
            )
            topic_buffers.append(TopicBuffer(topic, buffer))
            subscriptions.append(subscription)
        sources[key] = SourceBuffers(key, freshness_ms, tuple(topic_buffers))

    for interface in manifest.interfaces.state:
        _validate_message_type(
            interface.message_type, "sensor_msgs/msg/JointState", interface.key
        )
        add_source(
            interface.key,
            interface.freshness_ms,
            interface.topics,
            JointState,
        )

    for interface in manifest.interfaces.action:
        _validate_message_type(
            interface.message_type,
            "trajectory_msgs/msg/JointTrajectory",
            interface.key,
        )
        add_source(
            interface.key,
            interface.freshness_ms,
            interface.topics,
            JointTrajectory,
        )

    for camera in manifest.interfaces.cameras:
        _validate_message_type(
            camera.message_type, "sensor_msgs/msg/CompressedImage", camera.key
        )
        add_source(
            camera.key,
            camera.freshness_ms,
            (camera.topic,),
            CompressedImage,
        )

    return ManifestSubscriptions(sources, subscriptions)
