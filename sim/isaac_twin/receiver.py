"""Subscriber-only ROS feedback export for the isolated Unity observer."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time

from .contract import (ASSET_ID, CAMERA_TOPIC, JOINTS, JOINT_TOPIC, PROFILE_ID, ROOT,
                       RUNTIME_KIND, SCENE_SHA256, STATE_TOPIC, atomic_json, require_isolation)


class ObservationJoin:
    """Join named JointState with the same simulation-stamped runtime identity."""
    def __init__(self, root, expected_session=None):
        self.root = Path(root)
        self.states = {}
        self.joints = {}
        self.current_session = expected_session
        self.metadata = {}
        self.images = {}
        self.image_order = (-1, -1)
        self.accepted_images = 0
        self.last_image_ns = -1
        self.last_order = (-1, -1)
        self.last_sim_ns = -1
        self.accepted = 0
        self.rejected = 0

    def state(self, meta, now=None, mono=None):
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        try:
            exact = {"schema_version": 1, "runtime_kind": RUNTIME_KIND, "profile_id": PROFILE_ID,
                     "asset_id": ASSET_ID, "scene_sha256": SCENE_SHA256, "physical_output_connected": False}
            if any(type(meta.get(k)) is not type(v) or meta.get(k) != v for k, v in exact.items()):
                raise ValueError("identity")
            if not isinstance(meta["session_id"], str) or len(meta["session_id"]) != 32:
                raise ValueError("session")
            for key in ("epoch", "sequence", "sim_time_ns", "source_unix_ms"):
                if type(meta[key]) is not int or meta[key] < 0:
                    raise ValueError(key)
            if not -0.1 <= now - meta["source_unix_ms"] / 1000 <= 0.25:
                raise ValueError("stale source")
            if self.current_session is None:
                self.current_session = meta["session_id"]
            if meta["session_id"] != self.current_session:
                raise ValueError("session changed; explicit receiver restart required")
            key = (meta["session_id"], meta["epoch"], meta["sim_time_ns"])
            self.states[key] = (meta, now, mono)
            self.metadata[key] = (meta, now, mono)
            self._trim(self.states)
            self._trim(self.metadata)
            self._join(key, now, mono)
            self._join_image(key, now, mono)
        except (KeyError, ValueError, TypeError):
            self.rejected += 1

    def joint(self, stamp_ns, names, positions, frame_id, now=None, mono=None):
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        if (len(names) != 6 or set(names) != set(JOINTS) or len(positions) != 6
                or any(not math.isfinite(x) for x in positions)):
            self.rejected += 1
            return
        try:
            key = self._frame_key(frame_id, stamp_ns)
        except (ValueError, TypeError):
            self.rejected += 1
            return
        self.joints[key] = ([float(positions[names.index(n)]) for n in JOINTS], now, mono)
        self._trim(self.joints)
        self._join(key, now, mono)

    def _frame_key(self, frame_id, stamp_ns):
        prefix, session, epoch = frame_id.split("/")
        if (prefix != ASSET_ID or len(session) != 32 or int(epoch) < 0
                or session != self.current_session):
            raise ValueError("frame identity")
        return session, int(epoch), stamp_ns

    @staticmethod
    def _trim(values):
        while len(values) > 90:
            values.pop(next(iter(values)))

    def _fresh(self, meta, now, mono, *received_mono):
        return (-0.1 <= now-meta["source_unix_ms"]/1000 <= .25
                and 0 <= mono-min(received_mono) <= .25)

    def _join(self, key, now, mono):
        if key not in self.states or key not in self.joints:
            return
        meta, state_received, state_mono = self.states.pop(key)
        positions, joint_received, joint_mono = self.joints.pop(key)
        stamp = key[2]
        if not self._fresh(meta, now, mono, state_mono, joint_mono):
            self.rejected += 1
            return
        order = (meta["epoch"], meta["sequence"])
        if order <= self.last_order or (order[0] == self.last_order[0] and stamp <= self.last_sim_ns):
            self.rejected += 1
            return
        self.last_order, self.last_sim_ns = order, stamp
        snapshot = {k: v for k, v in meta.items() if k != "source_unix_ms"}
        snapshot.update(received_at_unix_ms=int(min(state_received, joint_received)*1000), joint_names=list(JOINTS),
                        joint_positions_rad=positions)
        atomic_json(self.root / "sample-provenance.json", {
            "session_id": meta["session_id"], "epoch": meta["epoch"], "sequence": meta["sequence"],
            "sim_time_ns": stamp, "source_unix_ms": meta["source_unix_ms"],
            "joint_received_at_unix_ms": int(joint_received*1000),
            "metadata_received_at_unix_ms": int(state_received*1000),
        })
        atomic_json(self.root / "telemetry.json", snapshot)
        self.accepted += 1

    def image(self, stamp_ns, frame_id, data, now=None, mono=None):
        now = time.time() if now is None else now
        mono = time.monotonic() if mono is None else mono
        try:
            key = self._frame_key(frame_id, stamp_ns)
            if len(data) > 2*1024*1024 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
                raise ValueError("JPEG envelope")
            self.images[key] = (data, now, mono)
            self._trim(self.images)
            self._join_image(key, now, mono)
        except (ValueError, TypeError):
            self.rejected += 1

    def _join_image(self, key, now, mono):
        if key not in self.metadata or key not in self.images:
            return
        meta, state_received, state_mono = self.metadata[key]
        data, image_received, image_mono = self.images.pop(key)
        order = (meta["epoch"], meta["sequence"])
        if (not self._fresh(meta, now, mono, state_mono, image_mono)
                or order <= self.image_order or order[0] < self.last_order[0]
                or (order[0] == self.image_order[0] and key[2] <= self.last_image_ns)):
            self.rejected += 1
            return
        temporary = self.root / ".frame.jpg.tmp"
        self.root.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(data)
        os.replace(temporary, self.root / "frame.jpg")
        atomic_json(self.root / "image-provenance.json", {
            **meta, "received_at_unix_ms": int(image_received*1000),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
        self.image_order = order
        self.accepted_images += 1
        self.last_image_ns = meta["sim_time_ns"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-seconds", type=float, default=600.)
    options = parser.parse_args()
    if not 0 < options.run_seconds <= 7200:
        parser.error("run-seconds must be bounded to 7200")
    require_isolation()
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import JointState, CompressedImage
    from std_msgs.msg import String
    rclpy.init(args=[])
    node = Node("sang_unity_twin_receiver", use_global_arguments=False,
                enable_rosout=False, start_parameter_services=False)
    output = ROOT / "observer"
    output.mkdir(parents=True, exist_ok=True)
    runtime_identity = json.loads((ROOT / "runtime-identity.json").read_text())
    from datetime import datetime
    if (runtime_identity.get("runtime_kind") != RUNTIME_KIND
            or runtime_identity.get("scene_sha256") != SCENE_SHA256
            or runtime_identity.get("physical_output_connected") is not False
            or not -0.1 <= time.time()-datetime.fromisoformat(runtime_identity["updated_at"]).timestamp() <= 1.):
        raise RuntimeError("Fresh pinned Isaac runtime identity required before receiver startup")
    join = ObservationJoin(output, runtime_identity["session_id"])
    def state(message):
        try:
            if len(message.data) > 8192:
                raise ValueError("metadata limit")
            join.state(json.loads(message.data))
        except (ValueError, TypeError):
            join.rejected += 1
    def joint(message):
        ns = message.header.stamp.sec*1000000000 + message.header.stamp.nanosec
        join.joint(ns, list(message.name), list(message.position), message.header.frame_id)
    def image(message):
        if message.format != "jpeg":
            return
        stamp = message.header.stamp.sec*1000000000 + message.header.stamp.nanosec
        join.image(stamp, message.header.frame_id, bytes(message.data))
    node.create_subscription(String, STATE_TOPIC, state, qos_profile_sensor_data)
    node.create_subscription(JointState, JOINT_TOPIC, joint, qos_profile_sensor_data)
    node.create_subscription(CompressedImage, CAMERA_TOPIC, image, qos_profile_sensor_data)
    started = time.monotonic()
    try:
        while time.monotonic() - started < options.run_seconds:
            rclpy.spin_once(node, timeout_sec=0.02)
            atomic_json(output / "receiver-status.json", {
                "updated_at_unix_ms": int(time.time()*1000), "joint_samples": join.accepted,
                "rejected_samples": join.rejected, "images": join.accepted_images,
                "last_image_sim_time_ns": join.last_image_ns,
                "application_publishers": [p.topic_name for p in node.publishers if p.topic_name != "/parameter_events"],
                "physical_output_connected": False,
            })
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
