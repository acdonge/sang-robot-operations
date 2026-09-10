"""Read-only extraction of the saved Scenario1 layout; stdout is the contract.

This deliberately parses only the observed Unity text serialization subset. It
does not open Unity, import source code, or evaluate a scenario controller.
"""

import hashlib
import json
import math
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

SOURCE = Path('/home/sa08050/app developer/unity_IITP/unity_IITP/100.Whole_integration/Scenario1_app')
SCENE = 'Assets/Scenes/SampleScene.unity'
CARRIER = 'Assets/Scripts/XrMobileBasePlateCarrier.cs'
IMPORTER = 'Library/PackageCache/com.unity.robotics.urdf-importer@4ed58f8d00fe/Runtime'


def blocks(text):
    return {int(i): (int(t), v) for t, i, v in re.findall(
        r'^--- !u!(\d+) &(-?\d+)[^\n]*\n([\s\S]*?)(?=^--- !u!|\Z)', text, re.M)}


def prop(text, key):
    return re.search(r'^  ' + re.escape(key) + r': (.*)$', text, re.M).group(1)


def ref(text, key):
    return int(re.search(r'fileID: (-?\d+)', prop(text, key)).group(1))


def vector(text, key, axes='xyz'):
    value = prop(text, key)
    return [float(re.search(a + r': ([^,}]+)', value).group(1)) for a in axes]


def multiply(a, b):
    x, y, z, w = a
    X, Y, Z, W = b
    return [w*X+x*W+y*Z-z*Y, w*Y-x*Z+y*W+z*X,
            w*Z+x*Y-y*X+z*W, w*W-x*X-y*Y-z*Z]


def rotate(q, v):
    n = math.sqrt(sum(x*x for x in q))
    q = [x/n for x in q]
    return multiply(multiply(q, v+[0]), [-q[0], -q[1], -q[2], q[3]])[:3]


def extract():
    source_hashes = {}

    def read(path):
        data = (SOURCE / path).read_bytes()
        source_hashes[path] = hashlib.sha256(data).hexdigest()
        return data.decode('utf-8')

    scene = blocks(read(SCENE))
    names = {i: prop(v, 'm_Name') for i, (t, v) in scene.items() if t == 1 and '  m_Name:' in v}
    transforms = {i: v for i, (t, v) in scene.items() if t == 4 and '  m_Father:' in v}

    def frame(i):
        v = transforms[i]
        parent = ref(v, 'm_Father')
        p, q, s = vector(v, 'm_LocalPosition'), vector(v, 'm_LocalRotation', 'xyzw'), vector(v, 'm_LocalScale')
        name = names[ref(v, 'm_GameObject')]
        wp, wq, ws, path = p, q, s, name
        if parent:
            f = frame(parent)
            pp, pq, ps = f['position_unity_world_m'], f['rotation_unity_world_xyzw'], f['scale_world']
            delta = rotate(pq, [a*b for a, b in zip(p, ps)])
            wp, wq, ws = [a+b for a, b in zip(pp, delta)], multiply(pq, q), [a*b for a, b in zip(ps, s)]
            path = f['path'] + '/' + name
        return dict(path=path, source_transform_file_id=i, parent_transform_file_id=parent,
                    position_local_m=p, rotation_local_xyzw=q, scale_local=s,
                    position_unity_world_m=wp, rotation_unity_world_xyzw=wq, scale_world=ws,
                    position_ros_world_m=[wp[2], -wp[0], wp[1]])

    selected = [i for i in transforms if names[ref(transforms[i], 'm_GameObject')] in
                ('rb5_850e', 'omy_f3m', 'MobileRobot', 'base_plate_attach_point', 'BasePlate', 'Main Camera', 'Isaac PC Preview Layout')]
    joints = []
    for i, v in transforms.items():
        go = scene[ref(v, 'm_GameObject')][1]
        components = [scene[int(c)] for c in re.findall(r'component: \{fileID: (-?\d+)\}', go)]
        joint = next((t for _, t in components if '  jointName:' in t), None)
        if joint is None:
            continue
        selected.append(i)
        articulation = next((t for typ, t in components if typ == 171741748), None)
        item = dict(joint_name=prop(joint, 'jointName'), frame_path=frame(i)['path'],
                    source_component=prop(joint, 'm_EditorClassIdentifier').split('.')[-1])
        if articulation:
            q = vector(articulation, 'm_AnchorRotation', 'xyzw')
            item['anchor_rotation_local_xyzw'] = q
            item['axis_local_unity'] = [round(x, 9) for x in rotate(q, [1, 0, 0])]
        joints.append(item)

    metas = {}
    for path in (SOURCE / 'Assets/IsaacSource').rglob('*.meta'):
        match = re.search(r'^guid: (.*)$', path.read_text(), re.M)
        if match:
            metas[match.group(1)] = str(path.relative_to(SOURCE))[:-5]
    tables = []
    for i, (typ, text) in scene.items():
        if typ != 1001:
            continue
        guid = re.search(r'm_SourcePrefab: \{fileID: -?\d+, guid: ([a-f0-9]+)', text).group(1)
        if '/WeldingTable/' not in metas.get(guid, ''):
            continue
        values = dict(re.findall(r'propertyPath: ([^\n]+)\n\s+value: ([^\n]*)', text))
        p = [float(values['m_LocalPosition.' + a]) for a in 'xyz']
        tables.append(dict(name=values['m_Name'], source_prefab_instance_file_id=i,
                           source_asset=metas[guid], source_guid=guid,
                           position_unity_world_m=p,
                           rotation_unity_world_xyzw=[float(values['m_LocalRotation.'+a]) for a in 'xyzw'],
                           scale_local=[float(values['m_LocalScale.'+a]) for a in 'xyz'],
                           position_ros_world_m=[p[2], -p[0], p[1]],
                           note='Imported mesh origin, not geometric tabletop center. Parent is identity.'))

    visual_assets = {}

    def asset(guid):
        if guid in visual_assets:
            return
        path = metas[guid]
        if Path(path).suffix not in ('.prefab', '.asset', '.dae', '.obj'):
            raise ValueError('Nonvisual source dependency: ' + path)
        visual_assets[guid] = path
        data = read(path)
        read(path + '.meta')
        if path.endswith('.prefab'):
            for typ, text in blocks(data).values():
                if typ not in (1, 4, 23, 33):
                    raise ValueError('Nonvisual prefab component: ' + path)
                if typ == 33:
                    for dep in re.findall(r'm_Mesh: \{fileID: -?\d+, guid: ([a-f0-9]+)', text):
                        asset(dep)

    def visual(i):
        typ, text = scene[i]
        if '  m_GameObject:' not in text:
            instance = scene[ref(text, 'm_PrefabInstance')][1]
            asset(re.search(r'm_SourcePrefab: \{fileID: -?\d+, guid: ([a-f0-9]+)', instance).group(1))
            return
        name = names[ref(text, 'm_GameObject')]
        if name in ('Collisions', 'Plugins'):
            return
        children = re.search(r'm_Children:([^\n]*(?:\n  - \{fileID: -?\d+\})*)', text).group(1)
        for child in re.findall(r'fileID: (-?\d+)', children):
            visual(int(child))

    for i in transforms:
        if names[ref(transforms[i], 'm_GameObject')] in ('rb5_850e', 'omy_f3m', 'Isaac PC Preview Layout'):
            visual(i)
    code = read(CARRIER)
    constants = {k: float(v) for k, v in re.findall(r'const float (\w+) = (-?[\d.]+)f;', code)}
    for path in ('Extensions/BuiltInExtensions.cs', 'UrdfComponents/UrdfJoints/UrdfJointRevolute.cs'):
        read(IMPORTER + '/' + path)
    gripper_source = 'Assets/IsaacSource/OMY/omy_f3m_unity.urdf'
    for joint in ET.fromstring(read(gripper_source)).findall('joint'):
        mimic = joint.find('mimic')
        if mimic is not None:
            item = next(j for j in joints if j['joint_name'] == joint.attrib['name'])
            item['mimic'] = dict(joint_name=mimic.attrib['joint'], multiplier=float(mimic.attrib.get('multiplier', 1)),
                                offset_rad=float(mimic.attrib.get('offset', 0)), source=gripper_source)
    start = [constants['MobileStartX'], 0, constants['MobileDockZ']]
    dock = [constants['MobileDockX'], 0, constants['MobileDockZ']]
    target = [constants['FinalPlateTarget'+a] for a in 'XYZ']
    return dict(schema_version=1, contract_kind='scenario1-source-layout', source_project=str(SOURCE), source_scene=SCENE,
                scope='Saved visual geometry and frame inventory only; not a motion plan, telemetry schema, or safety qualification.',
                coordinate_system=dict(units='meters', unity_axes='left-handed: +X right, +Y up, +Z forward',
                    ros_axes='right-handed: +X forward, +Y left, +Z up',
                    ros_to_unity_position='(-y, z, x)', unity_to_ros_position='(z, -x, y)',
                    unity_to_ros_matrix=[[0, 0, 1], [-1, 0, 0], [0, 1, 0]],
                    rotation_conversion='R_ros = C * R_unity * transpose(C); do not swap Euler angles.',
                    axis_conversion='Revolute angular axes are pseudovectors: Unity axis = -C_inverse * ROS axis. Serialized articulation anchor * local +X is recorded below.',
                    registration_note='Defines a source-aligned ROS/Isaac frame. Does not assert that an existing USD world or imported robot root already uses that frame.'),
                frames=sorted((frame(i) for i in set(selected)), key=lambda f: f['path']),
                joints=sorted(joints, key=lambda j: j['frame_path']), tables=tables,
                carrier=dict(source=CARRIER, serialized_pose_note='Saved MobileRobot x=-1.283 differs from runtime dock x=-1.25.',
                    start_unity_world_m=start, dock_unity_world_m=dock, return_unity_world_m=start,
                    path_unity_world_m=[start, dock], path_geometry='straight segment parallel to Unity +X; return reverses the endpoints',
                    start_ros_world_m=[start[2], -start[0], start[1]], dock_ros_world_m=[dock[2], -dock[0], dock[1]],
                    rotation_unity_world_xyzw=[0, 0, 0, 1], tray_top_height_m=constants['TableHeightMeters'],
                    plate_attach_local_m=[0, 0.91, 0], plate_size_m=[0.07, 0.01, 0.07],
                    plate_table_target_unity_world_m=target, plate_table_target_ros_world_m=[target[2], -target[0], target[1]],
                    note='Endpoint and visual dimensions only; no timing/controller logic is implemented by observer.'),
                visual_assets=[dict(guid=g, source_asset=p) for g, p in sorted(visual_assets.items())],
                source_hashes=[dict(path=p, sha256=h) for p, h in sorted(source_hashes.items())])


if __name__ == '__main__':
    result = extract()
    if '--check' in sys.argv:
        existing = Path(__file__).resolve().parents[2] / 'sim/isaac_scenario1/layout.json'
        assert json.loads(existing.read_text()) == result, 'Layout/source hashes changed'
        print('PASS: layout reproduces exactly from source, including all consumed asset hashes')
    else:
        print(json.dumps(result, indent=2, allow_nan=False))
