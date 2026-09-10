using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;

namespace Sang.Observer
{
    [Serializable]
    public sealed class SangFrameBinding
    {
        public string semantic;
        public string frame_id;
        public Transform target;
    }

    [Serializable]
    public sealed class SangSensorBinding
    {
        public string sensor_id;
        public Component component;
    }

    [Serializable]
    public sealed class SangJointBinding
    {
        public string joint_id;
        public Component target;
    }

    public sealed class SangAssetBinding : MonoBehaviour
    {
        [SerializeField] private string asset_id;
        [SerializeField] private string global_asset_id;
        [SerializeField] private string aas_id;
        [SerializeField, Min(1)] private int identity_revision = 1;
        [SerializeField] private string runtime_class = "simulation";
        [SerializeField] private string asset_type = "robot";
        [SerializeField] private string asset_package_id;
        [SerializeField, Min(1)] private int asset_package_version = 1;
        [SerializeField] private string asset_package_sha256;
        [SerializeField] private string unity_binding_id;
        [SerializeField] private string articulation_root_binding_id;
        [SerializeField] private string[] ordered_joints = Array.Empty<string>();
        [SerializeField] private string controller_component_binding_id;
        [SerializeField] private SangFrameBinding[] frames = Array.Empty<SangFrameBinding>();
        [SerializeField] private SangSensorBinding[] sensors = Array.Empty<SangSensorBinding>();
        [SerializeField] private Transform articulation_root;
        [SerializeField] private Component controller_component;
        [SerializeField] private SangJointBinding[] joint_targets = Array.Empty<SangJointBinding>();

        public string BindingId => unity_binding_id;

        public void Configure(SangAssetInventory metadata, Transform articulationRoot,
            Component controller, SangJointBinding[] joints, SangFrameBinding[] frameBindings,
            SangSensorBinding[] sensorBindings)
        {
            asset_id = metadata.identity.asset_id;
            global_asset_id = metadata.identity.global_asset_id;
            aas_id = metadata.identity.aas_id;
            identity_revision = metadata.identity.identity_revision;
            runtime_class = metadata.identity.runtime_class;
            asset_type = metadata.asset_type;
            asset_package_id = metadata.package_ref.asset_package_id;
            asset_package_version = metadata.package_ref.version;
            asset_package_sha256 = metadata.package_ref.sha256;
            unity_binding_id = metadata.unity_binding_id;
            articulation_root_binding_id = metadata.articulation_root_binding_id;
            controller_component_binding_id = metadata.controller_component_binding_id;
            ordered_joints = metadata.ordered_joints ?? Array.Empty<string>();
            articulation_root = articulationRoot;
            controller_component = controller;
            joint_targets = joints ?? Array.Empty<SangJointBinding>();
            frames = frameBindings ?? Array.Empty<SangFrameBinding>();
            sensors = sensorBindings ?? Array.Empty<SangSensorBinding>();
        }

        internal void Validate(List<string> errors)
        {
            if (runtime_class != "simulation" || string.IsNullOrWhiteSpace(asset_id)
                || string.IsNullOrWhiteSpace(global_asset_id) || string.IsNullOrWhiteSpace(aas_id)
                || identity_revision < 1 || string.IsNullOrWhiteSpace(unity_binding_id)
                || string.IsNullOrWhiteSpace(asset_package_id) || asset_package_version < 1
                || asset_package_sha256 == null || asset_package_sha256.Length != 64
                || asset_package_sha256.Any(character => !"0123456789abcdef".Contains(character)))
                errors.Add($"{unity_binding_id}: invalid simulation identity/package metadata");
            if (frames.Length == 0 || frames.Any(frame => frame == null || frame.target == null
                    || string.IsNullOrWhiteSpace(frame.semantic) || string.IsNullOrWhiteSpace(frame.frame_id))
                || frames.Select(frame => frame?.semantic).Distinct().Count() != frames.Length
                || frames.Select(frame => frame?.frame_id).Distinct().Count() != frames.Length)
                errors.Add($"{unity_binding_id}: missing or duplicate frame");
            if (sensors.Any(sensor => sensor == null || sensor.component == null
                    || string.IsNullOrWhiteSpace(sensor.sensor_id))
                || sensors.Select(sensor => sensor?.sensor_id).Distinct().Count() != sensors.Length)
                errors.Add($"{unity_binding_id}: missing or duplicate sensor");
            if (!string.IsNullOrEmpty(controller_component_binding_id) && controller_component == null)
                errors.Add($"{unity_binding_id}: controller component is missing");
            if (!string.IsNullOrEmpty(articulation_root_binding_id)
                && (articulation_root == null || ordered_joints.Length == 0
                    || !ordered_joints.SequenceEqual(joint_targets.Select(joint => joint?.joint_id))
                    || joint_targets.Any(joint => joint == null || joint.target == null)
                    || joint_targets.Select(joint => joint?.target).Distinct().Count() != joint_targets.Length
                    || ordered_joints.Distinct().Count() != ordered_joints.Length))
                errors.Add($"{unity_binding_id}: articulation/joint references are missing or ambiguous");
            if ((asset_type == "robot" || asset_type == "mobile_robot")
                && (string.IsNullOrEmpty(articulation_root_binding_id)
                    || string.IsNullOrEmpty(controller_component_binding_id)))
                errors.Add($"{unity_binding_id}: robot controller/articulation is required");
        }

        internal SangAssetInventory Capture()
        {
            var frameInventory = new SangFrameInventory[frames.Length];
            for (var index = 0; index < frames.Length; index++)
            {
                frameInventory[index] = new SangFrameInventory
                {
                    semantic = frames[index].semantic,
                    frame_id = frames[index].frame_id,
                    present = frames[index].target != null
                };
            }
            var sensorInventory = new SangSensorInventory[sensors.Length];
            for (var index = 0; index < sensors.Length; index++)
            {
                sensorInventory[index] = new SangSensorInventory
                {
                    sensor_id = sensors[index].sensor_id,
                    present = sensors[index].component != null
                };
            }
            return new SangAssetInventory
            {
                identity = new SangAssetIdentity
                {
                    asset_id = asset_id,
                    global_asset_id = global_asset_id,
                    aas_id = aas_id,
                    identity_revision = identity_revision,
                    runtime_class = runtime_class
                },
                asset_type = asset_type,
                package_ref = new SangAssetPackageRef
                {
                    asset_package_id = asset_package_id,
                    version = asset_package_version,
                    sha256 = asset_package_sha256
                },
                unity_binding_id = unity_binding_id,
                hierarchy_path = HierarchyPath(transform),
                articulation_root_binding_id = string.IsNullOrEmpty(articulation_root_binding_id) ? null : articulation_root_binding_id,
                ordered_joints = ordered_joints,
                controller_component_binding_id = string.IsNullOrEmpty(controller_component_binding_id) ? null : controller_component_binding_id,
                frames = frameInventory,
                sensors = sensorInventory
            };
        }

        private static string HierarchyPath(Transform item)
        {
            var path = item.name;
            while (item.parent != null)
            {
                item = item.parent;
                path = $"{item.name}/{path}";
            }
            return path;
        }
    }
}
