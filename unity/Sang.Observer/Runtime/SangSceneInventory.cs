using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;

namespace Sang.Observer
{
    [Serializable] public sealed class SangAssetIdentity { public string asset_id; public string global_asset_id; public string aas_id; public int identity_revision; public string runtime_class; }
    [Serializable] public sealed class SangAssetPackageRef { public string asset_package_id; public int version; public string sha256; }
    [Serializable] public sealed class SangFrameInventory { public string semantic; public string frame_id; public bool present; }
    [Serializable] public sealed class SangSensorInventory { public string sensor_id; public bool present; }
    [Serializable] public sealed class SangAssetInventory
    {
        public SangAssetIdentity identity;
        public string asset_type;
        public SangAssetPackageRef package_ref;
        public string unity_binding_id;
        public string hierarchy_path;
        public string articulation_root_binding_id;
        public string[] ordered_joints;
        public string controller_component_binding_id;
        public SangFrameInventory[] frames;
        public SangSensorInventory[] sensors;
    }
    [Serializable] public sealed class SangSceneInventoryPayload
    {
        public int schema_version = 1;
        public string scene_id;
        public int scene_version;
        public string unity_version;
        public string time_source = "unity_simulation_clock";
        public bool physical_output_connected = false;
        public SangAssetInventory[] bindings;
    }

    public sealed class SangSceneInventory : MonoBehaviour
    {
        [SerializeField] private string scene_id;
        [SerializeField, Min(1)] private int scene_version = 1;
        [SerializeField] private string required_unity_version = "6000.5.6f1";

        public void Configure(string sceneId, int version, string unityVersion)
        {
            scene_id = sceneId;
            scene_version = version;
            required_unity_version = unityVersion;
        }

        public bool TryCapture(out SangSceneInventoryPayload payload, out string[] errors)
        {
            payload = null;
            var failures = new List<string>();
            var components = gameObject.scene.GetRootGameObjects()
                .SelectMany(root => root.GetComponentsInChildren<SangAssetBinding>(true)).ToArray();
            if (string.IsNullOrWhiteSpace(scene_id) || scene_version < 1 || components.Length == 0)
                failures.Add("Scene identity and at least one binding are required");
            if (Application.unityVersion != required_unity_version)
                failures.Add("Actual Unity version differs from the required version");
            foreach (var component in components) component.Validate(failures);
            if (components.Select(item => item.BindingId).Distinct(StringComparer.Ordinal).Count() != components.Length)
                failures.Add("Duplicate Unity binding ID");
            if (failures.Count == 0)
            {
                var bindings = components.Select(item => item.Capture())
                    .OrderBy(item => item.unity_binding_id, StringComparer.Ordinal).ToArray();
                if (bindings.Select(item => item.identity.asset_id).Distinct(StringComparer.Ordinal).Count() != bindings.Length
                    || bindings.Select(item => item.identity.global_asset_id).Distinct(StringComparer.Ordinal).Count() != bindings.Length
                    || bindings.Select(item => item.identity.aas_id).Distinct(StringComparer.Ordinal).Count() != bindings.Length)
                    failures.Add("Duplicate asset/global/AAS identity");
                else payload = new SangSceneInventoryPayload
                {
                    scene_id = scene_id, scene_version = scene_version,
                    unity_version = Application.unityVersion, bindings = bindings
                };
            }
            errors = failures.ToArray();
            return errors.Length == 0;
        }

        public bool TryCaptureJson(out string json)
        {
            var valid = TryCapture(out var payload, out _);
            json = valid ? JsonUtility.ToJson(payload) : null;
            return valid;
        }
    }
}
