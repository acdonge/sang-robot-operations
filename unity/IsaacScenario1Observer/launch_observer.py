"""Bounded, startup-pinned launch of only the separate passive Scenario1 Player."""

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

PROJECT = Path(__file__).resolve().parent
ROOT = Path('/tmp/sang-isaac-scenario1/observer')
PLAYER = PROJECT / 'Builds/Linux/IsaacScenario1Observer.x86_64'
JOINTS = {
    'isaac-scenario1-omy': ['joint1', 'joint2', 'joint3', 'joint4', 'joint5', 'joint6', 'rh_r1_joint'],
    'isaac-scenario1-rb5': ['base', 'shoulder', 'elbow', 'wrist1', 'wrist2', 'wrist3'],
    'isaac-scenario1-carrier': [], 'isaac-scenario1-plate': [],
}
IDENTITY = dict(schema_version=2, runtime_kind='isaac-sim-executor', profile_id='scenario1',
                physical_output_connected=False, frame_id='cell')
FIELDS = set(IDENTITY) | {'scene_sha256', 'session_id', 'epoch', 'sequence', 'sim_time_ns',
                          'received_at_unix_ms', 'phase', 'plate_owner', 'assets'}
ASSET_FIELDS = {'asset_id', 'joint_names', 'joint_positions_rad', 'position_m', 'rotation_xyzw'}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def validate_snapshot(snapshot, now_ms, trusted_scene_hash):
    if not isinstance(trusted_scene_hash, str) or not re.fullmatch('[0-9a-f]{64}', trusted_scene_hash):
        raise ValueError('Trusted build scene hash is not finalized')
    if not isinstance(snapshot, dict) or set(snapshot) != FIELDS:
        raise ValueError('Unexpected telemetry fields')
    if any(type(snapshot[k]) is not type(v) or snapshot[k] != v for k, v in IDENTITY.items()):
        raise ValueError('Untrusted telemetry source')
    if snapshot['scene_sha256'] != trusted_scene_hash:
        raise ValueError('Scene hash differs from trusted build profile')
    if not isinstance(snapshot['session_id'], str) or not re.fullmatch('[0-9a-f]{32}', snapshot['session_id']):
        raise ValueError('Invalid session')
    for key in ('epoch', 'sequence', 'sim_time_ns', 'received_at_unix_ms'):
        if type(snapshot[key]) is not int or not 0 <= snapshot[key] <= 2**63-1:
            raise ValueError('Invalid counter or timestamp')
    if snapshot['received_at_unix_ms'] == 0 or not -100 <= now_ms-snapshot['received_at_unix_ms'] <= 500:
        raise ValueError('Stale or future telemetry')
    phase = snapshot['phase']
    if not isinstance(phase, str) or len(phase) > 128 or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in phase):
        raise ValueError('Invalid phase')
    if snapshot['plate_owner'] not in ('carrier', 'omy', 'table'):
        raise ValueError('Invalid plate owner')
    if not isinstance(snapshot['assets'], list) or len(snapshot['assets']) != 4:
        raise ValueError('Exactly four assets required')
    seen = set()
    for asset in snapshot['assets']:
        if not isinstance(asset, dict) or set(asset) != ASSET_FIELDS:
            raise ValueError('Unexpected asset fields')
        identity = asset['asset_id']
        if not isinstance(identity, str) or identity not in JOINTS or identity in seen:
            raise ValueError('Unexpected or duplicate asset')
        seen.add(identity)
        names = asset['joint_names']
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names) or len(names) != len(JOINTS[identity]) or set(names) != set(JOINTS[identity]):
            raise ValueError('Exact named joints required')
        for key, count in (('joint_positions_rad', len(names)), ('position_m', 3), ('rotation_xyzw', 4)):
            values = asset[key]
            if not isinstance(values, list) or len(values) != count or any(type(v) not in (int, float)
                    or abs(v) > 3.4028234663852886e38 or not math.isfinite(v) for v in values):
                raise ValueError('Invalid measured vector')
        if abs(sum(v*v for v in asset['rotation_xyzw'])-1) > 0.001:
            raise ValueError('Quaternion must be unit length')
    return snapshot['session_id'], snapshot['epoch']


def trusted_hash():
    profile_bytes = (PROJECT / 'build-profile.json').read_bytes()
    profile = json.loads(profile_bytes, object_pairs_hook=unique_object)
    digest = profile['scene_sha256']
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('Main must finalize the manifest hash in the trusted build profile and rebuild first')
    evidence = json.loads((PROJECT / 'Builds/Evidence/build-result.json').read_bytes(), object_pairs_hook=unique_object)
    dll = PLAYER.parent / 'IsaacScenario1Observer_Data/Managed/Sang.IsaacScenario1Observer.dll'
    if evidence.get('result') != 'PASS' or evidence.get('scene_sha256') != digest or evidence.get('build_profile_sha256') != hashlib.sha256(profile_bytes).hexdigest():
        raise ValueError('Profile changed or build evidence does not match')
    if evidence.get('observer_dll_sha256') != hashlib.sha256(dll.read_bytes()).hexdigest():
        raise ValueError('Observer assembly changed since audited build')
    return digest


def read_pin(digest):
    info = ROOT.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Receiver root must be private and user-owned')
    info = ROOT.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o002:
        raise ValueError('Observer directory must be user-owned and not world-writable')
    fd = os.open(ROOT / 'telemetry.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o002:
            raise ValueError('Telemetry must be a regular user-owned file')
        raw = stream.read(16385)
    if not 0 < len(raw) <= 16384:
        raise ValueError('Telemetry size is invalid')
    return validate_snapshot(json.loads(raw, object_pairs_hook=unique_object), time.time_ns() // 1_000_000, digest)


def run_child(session, epoch, duration):
    environment = os.environ.copy()
    environment.update(SANG_SCENARIO1_SESSION_ID=session, SANG_SCENARIO1_EPOCH=str(epoch))
    command = [str(PLAYER), '-screen-fullscreen', '0', '-screen-width', '1280', '-screen-height', '800',
               '-logFile', str(ROOT / 'observer-player.log')]
    child = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        return child.wait(timeout=duration)
    except subprocess.TimeoutExpired:
        return 0
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration-seconds', type=int, default=240)
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.duration_seconds <= 3600:
        parser.error('duration must be 1..3600 seconds')
    digest = trusted_hash()
    session, epoch = read_pin(digest)
    print(json.dumps(dict(session_id=session, epoch=epoch, scene_sha256=digest, build_role='observer', auto_rebind=False)), flush=True)
    if args.check_only:
        return 0
    if not PLAYER.is_file():
        raise ValueError('Separate observer Player is not built')
    fd = os.open(ROOT / 'observer-player.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'w') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o002:
            raise ValueError('Invalid observer launch lock')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return run_child(session, epoch, args.duration_seconds)


if __name__ == '__main__':
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as error:
        sys.exit('Observer launch refused: ' + str(error))
    except KeyboardInterrupt:
        sys.exit(130)
