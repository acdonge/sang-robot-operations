# LeRobot Observation Derivation Contract

Status: **MVP CONTRACT / READ-ONLY**

This contract defines how SANG derives a training-oriented LeRobot dataset from an immutable,
quality-passed MCAP recording. The MCAP session remains the authoritative record. A LeRobot dataset
is reproducible derived data and may be deleted and rebuilt without changing the source session.

## Frozen runtime

| Component | Frozen value |
|---|---|
| LeRobot package | `0.4.3` |
| LeRobot dataset format | `v3.0` |
| Source commit | `b0c3f2291b3f7753fd530172ec4bca0ed0a0d8ce` |
| Python | `3.11.15` (`leisaac` conda environment) |
| Video codec | H.264 (`h264`) |
| `pyproject.toml` SHA-256 | `e8c2381b79caa575185f44250e47a2851de4cb72630c2acc3f4b27809b6c5b28` |
| `lerobot_dataset.py` SHA-256 | `3fc9731a29e4395215d882cc6c052b4f98866d3f583fa769451a71218cb5a45b` |
| `datasets/utils.py` SHA-256 | `b1d32074e843880ef81436d7357bd1ac7b8d26ad9d30a0594c38620d36fae692` |

The three frozen LeRobot files are clean relative to the source commit. Other local LeRobot robot and
policy extensions are not part of this converter contract.

## Admission gate

A derivation is accepted only when all of the following are true:

1. The source recording session is `COMPLETE`.
2. The source session has a quality report whose state is `PASS`.
3. Every selected topic belongs to the immutable session manifest and has a complete MCAP object.
4. Exactly one selected state topic has type `sensor_msgs/msg/JointState`.
5. Every selected camera has type `sensor_msgs/msg/CompressedImage` and contains complete JPEG data.
6. The selected interval is within the common source-time range and is no longer than 1,800 seconds.
7. The requested frame rate is an integer from 1 through 60 Hz.

An admission or conversion failure creates no published dataset object. A retry with the same source,
time bounds, selected features, task text, frame rate, and converter version has the same idempotency
key.

## Timeline and sampling

- Sampling uses ROS message header source time, not file arrival order or browser playback time.
- The target timeline is a fixed-rate grid in the common time range of all selected streams.
- The nearest observation on either side of each target timestamp is selected.
- A frame is rejected if any selected observation is more than 50 ms from its target timestamp.
- Timestamps in LeRobot are seconds from the first emitted target timestamp.
- The derivation manifest preserves target time, each source time, MCAP log time, and measured skew.
- No interpolation is applied to robot joints or images in this MVP.

This is software synchronization based on chrony-locked host timestamps. It does not claim camera
hardware-trigger or PTP exposure-time synchronization.

## LeRobot features

The state feature follows the source robot manifest exactly:

```json
{
  "observation.state": {
    "dtype": "float32",
    "shape": [6],
    "names": ["base", "shoulder", "elbow", "wrist1", "wrist2", "wrist3"]
  }
}
```

Each selected camera becomes `observation.images.<topic_key>` with its recorded native
`[height, width, 3]` shape and `video` dtype. JPEG frames are decoded to RGB and re-encoded into the
LeRobot H.264 video layout. The original JPEG bytes remain unchanged in MCAP.

The dataset deliberately has **no `action` feature**. The current recordings contain measured robot
state only. A future converter may add `action` only from an independently recorded command topic
whose meaning, unit, ordering, and timestamp provenance are explicitly declared.

One selected interval becomes one LeRobot episode. The caller must provide a non-empty task label,
even for observation-only data, because LeRobot v3 requires a task per frame.

## Provenance and storage

Each completed derivation records:

- derivation ID, deterministic request hash, state, creator, and timestamps;
- source session ID, Task Run ID, robot ID, session manifest SHA-256, quality report SHA-256, and
  source MCAP object hashes;
- selected topics, asset IDs, calibration references, timestamp provenance, time bounds, rate,
  frame count, joint order, image shapes, and per-stream maximum skew;
- frozen converter/LeRobot versions and file hashes;
- output archive key, size, SHA-256, and derivation-manifest SHA-256.

The published artifact is an immutable tar archive containing the complete local LeRobot v3 dataset
plus `sang-derivation.json`. It is stored under `derivations/<derivation_id>/`. Catalog rows are stored
in Central SQLite; bulk dataset files stay in the filesystem Object Store.

## Safety boundary

The converter opens MCAP and catalog objects read-only. It has no ROS publisher, vendor SDK command,
robot control, policy execution, welding, wire-feeder, or digital-output route. Dataset derivation must
remain usable while all physical robot command interfaces are disabled.

## First acceptance source

The initial end-to-end source is session `capture-c0d93d51f619448ca378a5ce3d669e64`, attached to Task
Run `rb5-ui-capture-acceptance-20260824`. Its quality state is `PASS`, clock state is `LOCKED`, and its
selected streams are `rb5_joint`, `d405_color`, `d455_color`, and `welding_view` at 30 Hz.
