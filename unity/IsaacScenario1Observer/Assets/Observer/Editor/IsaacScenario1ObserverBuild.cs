using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Text.Json;
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering;
using Object = UnityEngine.Object;

namespace Sang.IsaacScenario1Observer.Editor
{
    public static class IsaacScenario1ObserverBuild
    {
        const string SourceProject = "/home/sa08050/app developer/unity_IITP/unity_IITP/100.Whole_integration/Scenario1_app";
        const string SourceScene = SourceProject + "/Assets/Scenes/SampleScene.unity";
        const string ScenePath = "Assets/Observer/Scenes/Scenario1Observer.unity";
        const string BuildPath = "Builds/Linux/IsaacScenario1Observer.x86_64";
        const string EvidencePath = "Builds/Evidence";
        const string MeshFolder = "Assets/Observer/Visuals";
        static readonly CultureInfo Culture = CultureInfo.InvariantCulture;
        static readonly List<string> checks = new List<string>();
        static readonly Dictionary<string, string> sourceHashes = new Dictionary<string, string>();
        static readonly Dictionary<string, string> meshPaths = new Dictionary<string, string>();
        static readonly Dictionary<string, Transform> mappedJoints = new Dictionary<string, Transform>();
        static Material material;
        static JsonElement layout;
        static readonly Dictionary<long, Transform> cloned = new Dictionary<long, Transform>();

        sealed class Block
        {
            public long Id;
            public int Type;
            public string Text;
        }

        static void Require(bool condition, string message)
        {
            if (!condition) throw new BuildFailedException(message);
        }

        static string Match(string text, string pattern)
        {
            var result = Regex.Match(text, pattern, RegexOptions.Multiline);
            Require(result.Success, "Source shape changed: " + pattern);
            return result.Groups[1].Value;
        }

        static long Reference(string text, string key) => long.Parse(Match(text, @"^  " + key + @": \{fileID: (-?\d+)"), Culture);
        static string Property(string text, string key) => Match(text, @"^  " + key + @": (.*)$").Trim();
        static float Number(string text) => float.Parse(text, Culture);

        static Vector3 Vector(string text, string key)
        {
            var value = Property(text, key);
            return new Vector3(Number(Match(value, @"x: ([^,}]+)")), Number(Match(value, @"y: ([^,}]+)")), Number(Match(value, @"z: ([^,}]+)")));
        }

        static Quaternion Rotation(string text, string key)
        {
            var v = Vector(text, key);
            return new Quaternion(v.x, v.y, v.z, Number(Match(Property(text, key), @"w: ([^,}]+)")));
        }

        static string Hash(string path)
        {
            using (var hash = SHA256.Create())
                return string.Concat(hash.ComputeHash(File.ReadAllBytes(path)).Select(value => value.ToString("x2")));
        }

        static Dictionary<long, Block> ReadSource()
        {
            var text = File.ReadAllText(SourceScene).Replace("\r\n", "\n");
            sourceHashes[SourceScene] = Hash(SourceScene);
            return Regex.Matches(text, @"^--- !u!(\d+) &(-?\d+)[^\n]*\n([\s\S]*?)(?=^--- !u!|\z)", RegexOptions.Multiline)
                .Cast<Match>().Select(match => new Block { Type = int.Parse(match.Groups[1].Value),
                    Id = long.Parse(match.Groups[2].Value), Text = match.Groups[3].Value }).ToDictionary(block => block.Id);
        }

        static void ImportMeshes()
        {
            using var doc = JsonDocument.Parse(File.ReadAllText("../../sim/isaac_scenario1/layout.json"));
            layout = doc.RootElement.Clone();
            foreach (var entry in layout.GetProperty("source_hashes").EnumerateArray())
            {
                var path = SourceProject + "/" + entry.GetProperty("path").GetString();
                Require(Hash(path) == entry.GetProperty("sha256").GetString(), "Pinned source changed: " + path);
                sourceHashes[path] = Hash(path);
            }
            foreach (var entry in layout.GetProperty("visual_assets").EnumerateArray())
            {
                var path = entry.GetProperty("source_asset").GetString();
                var destination = MeshFolder + "/" + path.Substring("Assets/IsaacSource/".Length);
                Require(new[] { ".prefab", ".asset", ".dae", ".obj" }.Contains(Path.GetExtension(path)), "Nonvisual dependency");
                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                File.Copy(SourceProject + "/" + path, destination, true);
                File.Copy(SourceProject + "/" + path + ".meta", destination + ".meta", true);
                meshPaths[entry.GetProperty("guid").GetString()] = destination;
            }
            AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
        }

        static Transform CloneModel(Transform source, Transform parent, Dictionary<long, Transform> idMap)
        {
            Require(source.GetComponents<Component>().All(component => component is Transform || component is MeshFilter
                || component is MeshRenderer), "Nonvisual component in mesh asset");
            var target = new GameObject(source.name).transform;
            target.SetParent(parent, false);
            target.localPosition = source.localPosition;
            target.localRotation = source.localRotation;
            target.localScale = source.localScale;
            target.gameObject.SetActive(source.gameObject.activeSelf);
            if (AssetDatabase.TryGetGUIDAndLocalFileIdentifier(source, out string ignored, out long localId))
                idMap.Add(localId, target);
            var filter = source.GetComponent<MeshFilter>();
            if (filter != null)
            {
                Require(filter.sharedMesh != null, "Missing imported mesh");
                target.gameObject.AddComponent<MeshFilter>().sharedMesh = filter.sharedMesh;
                var renderer = target.gameObject.AddComponent<MeshRenderer>();
                renderer.sharedMaterials = Enumerable.Repeat(material, filter.sharedMesh.subMeshCount).ToArray();
                renderer.enabled = source.GetComponent<MeshRenderer>().enabled;
            }
            foreach (Transform child in source) CloneModel(child, target, idMap);
            return target;
        }

        static Transform CopyPrefab(Block transform, Transform parent, Dictionary<long, Block> blocks)
        {
            var instance = blocks[Reference(transform.Text, "m_PrefabInstance")];
            Require(instance.Type == 1001, "Unexpected visual prefab instance");
            var guid = Match(instance.Text, @"m_SourcePrefab: \{fileID: -?\d+, guid: ([0-9a-f]+)");
            Require(meshPaths.ContainsKey(guid), "Visual prefab not in pinned inventory: " + guid);
            Require(new[] { "m_RemovedComponents", "m_RemovedGameObjects", "m_AddedGameObjects", "m_AddedComponents" }
                .All(key => instance.Text.Contains(key + ": []")), "Visual prefab has structural overrides");
            var model = AssetDatabase.LoadAssetAtPath<GameObject>(meshPaths[guid]);
            Require(model != null, "RB5 model import failed");
            var map = new Dictionary<long, Transform>();
            var result = CloneModel(model.transform, parent, map);
            var overrides = new Dictionary<long, SerializedObject>();
            foreach (Match modification in Regex.Matches(instance.Text,
                @"- target: \{fileID: (-?\d+),[^\n]+\n\s+propertyPath: ([^\n]+)\n\s+value: ([^\n]*)\n"))
            {
                var path = modification.Groups[2].Value;
                if (path == "m_Name") { result.name = modification.Groups[3].Value; continue; }
                if (!path.StartsWith("m_LocalPosition.") && !path.StartsWith("m_LocalRotation.") && !path.StartsWith("m_LocalScale.")) continue;
                Require(map.TryGetValue(long.Parse(modification.Groups[1].Value), out var target), "Unknown transform override");
                var targetId = long.Parse(modification.Groups[1].Value);
                if (!overrides.TryGetValue(targetId, out var serialized))
                    overrides.Add(targetId, serialized = new SerializedObject(target));
                var property = serialized.FindProperty(path);
                Require(property != null, "Unknown pose property");
                property.floatValue = Number(modification.Groups[3].Value);
            }
            foreach (var serialized in overrides.Values) serialized.ApplyModifiedPropertiesWithoutUndo();
            return result;
        }

        static Transform CopyTransform(long id, Transform parent, Dictionary<long, Block> blocks)
        {
            var block = blocks[id];
            Require(block.Type == 4, "Expected source Transform");
            if (!block.Text.Contains("  m_GameObject:")) return CopyPrefab(block, parent, blocks);
            var go = blocks[Reference(block.Text, "m_GameObject")];
            var name = Property(go.Text, "m_Name");
            if (name == "Collisions" || name == "Plugins") return null;
            var result = new GameObject(name).transform;
            cloned.Add(id, result);
            result.SetParent(parent, false);
            result.localPosition = Vector(block.Text, "m_LocalPosition");
            result.localRotation = Rotation(block.Text, "m_LocalRotation");
            result.localScale = Vector(block.Text, "m_LocalScale");
            result.gameObject.SetActive(Property(go.Text, "m_IsActive") == "1");
            var components = Regex.Matches(go.Text, @"component: \{fileID: (-?\d+)\}").Cast<Match>()
                .Select(item => blocks[long.Parse(item.Groups[1].Value)]).ToArray();
            var filter = components.SingleOrDefault(item => item.Type == 33);
            if (filter != null)
            {
                var meshId = Reference(filter.Text, "m_Mesh");
                var meshName = meshId == 10202 ? "Cube.fbx" : meshId == 10206 ? "Cylinder.fbx" : null;
                Require(meshName != null, "Unreviewed direct mesh " + meshId);
                var mesh = Resources.GetBuiltinResource<Mesh>(meshName);
                Require(mesh != null, "Missing built-in visual mesh");
                result.gameObject.AddComponent<MeshFilter>().sharedMesh = mesh;
                var rendererBlock = components.Single(item => item.Type == 23);
                var materialId = long.Parse(Match(rendererBlock.Text, @"m_Materials:\n  - \{fileID: (-?\d+)"));
                var colorText = blocks.TryGetValue(materialId, out var savedMaterial)
                    ? Match(savedMaterial.Text, @"_BaseColor: (\{[^\n]+\})") : "{r: 0.46, g: 0.52, b: 0.57, a: 1}";
                var color = new Color(Number(Match(colorText, @"r: ([^,}]+)")), Number(Match(colorText, @"g: ([^,}]+)")),
                    Number(Match(colorText, @"b: ([^,}]+)")), Number(Match(colorText, @"a: ([^,}]+)")));
                var materialPath = MeshFolder + "/SourceColor-" + materialId + ".mat";
                var sourceMaterial = AssetDatabase.LoadAssetAtPath<Material>(materialPath);
                if (sourceMaterial == null)
                {
                    sourceMaterial = new Material(Shader.Find("Standard")) { color = color };
                    AssetDatabase.CreateAsset(sourceMaterial, materialPath);
                }
                sourceMaterial.color = color;
                result.gameObject.AddComponent<MeshRenderer>().sharedMaterial = sourceMaterial;
            }
            var joint = components.SingleOrDefault(item => item.Text.Contains("  jointName:"));
            if (joint != null) mappedJoints.Add(PathOf(result).Split('/')[0] + ":" + Property(joint.Text, "jointName"), result);
            var children = Match(block.Text, @"m_Children:([^\n]*(?:\n  - \{fileID: -?\d+\})*)");
            foreach (Match child in Regex.Matches(children, @"fileID: (-?\d+)")) CopyTransform(long.Parse(child.Groups[1].Value), result, blocks);
            return result;
        }


        static double[] Numbers(JsonElement element) => element.EnumerateArray().Select(x => x.GetDouble()).ToArray();
        static Vector3 V(JsonElement e) { var a = Numbers(e); return new Vector3((float)a[0], (float)a[1], (float)a[2]); }
        static Quaternion Q(JsonElement e) { var a = Numbers(e); return new Quaternion((float)a[0], (float)a[1], (float)a[2], (float)a[3]); }
        static bool SameRotation(Quaternion a, Quaternion b)
        {
            a = a.normalized; b = b.normalized;
            return Math.Min(new Vector4(a.x-b.x, a.y-b.y, a.z-b.z, a.w-b.w).magnitude,
                new Vector4(a.x+b.x, a.y+b.y, a.z+b.z, a.w+b.w).magnitude) < 0.000001;
        }
        static void Check(string name, bool passed) { Require(passed, "Test failed: " + name); checks.Add(name); }

        static Transform Root(string name, Dictionary<long, Block> blocks)
        {
            var go = blocks.Values.Single(x => x.Type == 1 && x.Text.Contains("  m_Name: " + name + "\n"));
            var transform = blocks.Values.Single(x => x.Type == 4 && x.Text.Contains("  m_GameObject: {fileID: " + go.Id + "}"));
            return CopyTransform(transform.Id, null, blocks);
        }

        public static void PrepareScene()
        {
            Require(Application.unityVersion == "6000.5.6f1", "Exact Editor 6000.5.6f1 required");
            Require(Path.GetFileName(Path.GetDirectoryName(Application.dataPath)) == "IsaacScenario1Observer", "Only separate observer project permitted");
            sourceHashes.Clear(); meshPaths.Clear(); mappedJoints.Clear(); cloned.Clear(); checks.Clear();
            var blocks = ReadSource();
            ImportMeshes();
            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            material = AssetDatabase.LoadAssetAtPath<Material>(MeshFolder + "/Observer.mat");
            if (material == null)
            {
                material = new Material(Shader.Find("Standard")) { name = "Observer", color = new Color(0.46f, 0.52f, 0.57f) };
                AssetDatabase.CreateAsset(material, MeshFolder + "/Observer.mat");
            }
            var omy = Root("omy_f3m", blocks);
            var rb5 = Root("rb5_850e", blocks);
            var tables = Root("Isaac PC Preview Layout", blocks);
            var carrier = Root("MobileRobot", blocks);
            var cameraReference = Root("Main Camera", blocks);
            var sourceCamera = cameraReference.gameObject.AddComponent<Camera>();
            sourceCamera.enabled = false;
            sourceCamera.fieldOfView = 60;
            // Preserve the saved camera frame; overview framing never recenters any asset.
            var allRenderers = scene.GetRootGameObjects().SelectMany(x => x.GetComponentsInChildren<MeshRenderer>()).ToArray();
            var bounds = allRenderers[0].bounds;
            foreach (var renderer in allRenderers) bounds.Encapsulate(renderer.bounds);
            bounds.Encapsulate(new Vector3(-2.8f, 0, -1.1f));
            var camera = new GameObject("ObserverOverviewCamera").AddComponent<Camera>();
            camera.transform.rotation = Quaternion.Euler(28, 0, 0);
            camera.transform.position = bounds.center - camera.transform.forward * (bounds.extents.magnitude * 2.8f);
            camera.fieldOfView = 45; camera.nearClipPlane = 0.01f; camera.farClipPlane = 30;
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = new Color(0.09f, 0.13f, 0.16f);
            var light = new GameObject("ObserverLight").AddComponent<Light>();
            light.type = LightType.Directional; light.intensity = 1.4f;
            light.transform.rotation = Quaternion.Euler(40, -35, 0);
            RenderSettings.ambientLight = new Color(0.65f, 0.65f, 0.65f);
            var plate = cloned[1088478273];
            var mirror = new GameObject("IsaacMeasuredStateObserver").AddComponent<Scenario1Mirror>();
            using var profile = JsonDocument.Parse(File.ReadAllText("build-profile.json"));
            mirror.trustedSceneSha256 = profile.RootElement.GetProperty("scene_sha256").GetString();
            Require(mirror.trustedSceneSha256 == "" || Regex.IsMatch(mirror.trustedSceneSha256, "\\A[0-9a-f]{64}\\z"), "Invalid trusted scene digest");
            var roots = new[] { omy, rb5, carrier, plate };
            mirror.bindings = new VisualBinding[4];
            for (var i = 0; i < 4; i++)
            {
                var names = SnapshotGate.JointNames[i].ToList();
                if (i == 0) names.AddRange(new[] { "rh_l1", "rh_l2", "rh_r2" });
                var prefix = roots[i].name + ":";
                var entries = names.Select(name => layout.GetProperty("joints").EnumerateArray().Single(x =>
                    x.GetProperty("joint_name").GetString() == name)).ToArray();
                foreach (var entry in entries.Where(e => e.TryGetProperty("mimic", out _)))
                {
                    var mimic = entry.GetProperty("mimic");
                    Require(mimic.GetProperty("joint_name").GetString() == "rh_r1_joint"
                        && mimic.GetProperty("multiplier").GetDouble() == 1 && mimic.GetProperty("offset_rad").GetDouble() == 0, "Mimic source changed");
                }
                mirror.bindings[i] = new VisualBinding { root = roots[i],
                    joints = names.Select(name => mappedJoints[prefix + name]).ToArray(),
                    zeroRotations = names.Select(name => mappedJoints[prefix + name].localRotation).ToArray(),
                    axes = entries.Select(x => V(x.GetProperty("axis_local_unity"))).ToArray(),
                    visualJointNames = names.ToArray(),
                    sourceJointIndices = names.Select(name => Array.IndexOf(SnapshotGate.JointNames[i], name) >= 0 ?
                        Array.IndexOf(SnapshotGate.JointNames[i], name) : 6).ToArray() };
            }
            foreach (var frame in layout.GetProperty("frames").EnumerateArray())
            {
                var transform = cloned[frame.GetProperty("source_transform_file_id").GetInt64()];
                Check("world position retained: " + frame.GetProperty("path").GetString(),
                    Vector3.Distance(transform.position, V(frame.GetProperty("position_unity_world_m"))) < 0.000002);
                Check("world rotation retained: " + frame.GetProperty("path").GetString(),
                    SameRotation(transform.rotation, Q(frame.GetProperty("rotation_unity_world_xyzw"))));
            }
            foreach (var entry in layout.GetProperty("tables").EnumerateArray())
            {
                var table = tables.Cast<Transform>().Single(x => x.name == entry.GetProperty("name").GetString());
                Check("table origin retained: " + table.name, Vector3.Distance(table.position, V(entry.GetProperty("position_unity_world_m"))) < 0.000001);
                Check("table rotation retained: " + table.name, SameRotation(table.rotation, Q(entry.GetProperty("rotation_unity_world_xyzw"))));
                Check("table scale retained: " + table.name, Vector3.Distance(table.localScale, V(entry.GetProperty("scale_local"))) < 0.000001);
            }
            // Detach once with world pose preserved. Ownership labels never parent or animate the plate.
            plate.SetParent(null, true);
            Check("plate independent root", plate.parent == null);
            Check("OMY six axes plus measured gripper and three passive mimic visuals", mirror.bindings[0].joints.Length == 10);
            Check("RB5 six measured joints", mirror.bindings[1].joints.Length == 6);
            Check("carrier visuals retained", carrier.GetComponentsInChildren<MeshRenderer>().Length == 10);
            Check("two original table mesh instances", tables.childCount == 2);
            Check("all meshes loaded", allRenderers.All(x => x.GetComponent<MeshFilter>().sharedMesh != null));
            Directory.CreateDirectory(Path.GetDirectoryName(ScenePath));
            Require(EditorSceneManager.SaveScene(scene, ScenePath), "Scene save failed");
            AssetDatabase.SaveAssets();
            AuditScene();
            Directory.CreateDirectory(EvidencePath);
            var inventory = scene.GetRootGameObjects().SelectMany(x => x.GetComponentsInChildren<Transform>(true)).Select(x => new {
                path = PathOf(x), position_m = new[] { x.position.x, x.position.y, x.position.z },
                rotation_xyzw = new[] { x.rotation.x, x.rotation.y, x.rotation.z, x.rotation.w },
                scale = new[] { x.lossyScale.x, x.lossyScale.y, x.lossyScale.z } }).ToArray();
            File.WriteAllText(EvidencePath + "/scene-world-inventory.json", JsonSerializer.Serialize(inventory, new JsonSerializerOptions { WriteIndented = true }));
            var tableBounds = tables.Cast<Transform>().Select(t => {
                var rs = t.GetComponentsInChildren<MeshRenderer>(); var bb = rs[0].bounds;
                foreach (var rr in rs) bb.Encapsulate(rr.bounds);
                return new { name = t.name, center_m = new[] { bb.center.x, bb.center.y, bb.center.z }, size_m = new[] { bb.size.x, bb.size.y, bb.size.z } };
            }).ToArray();
            File.WriteAllText(EvidencePath + "/table-bounds.json", JsonSerializer.Serialize(tableBounds, new JsonSerializerOptions { WriteIndented = true }));
            ExportFingerGeometry(blocks, mirror);
            File.WriteAllLines(EvidencePath + "/source-sha256.txt", sourceHashes.OrderBy(x => x.Key).Select(x => x.Value + "  " + x.Key));
        }

        static void ExportFingerGeometry(Dictionary<long, Block> blocks, Scenario1Mirror mirror)
        {
            float[] Vec(Vector3 p) => new[] { p.x, p.y, p.z };
            float[] Quat(Quaternion q) => new[] { q.x, q.y, q.z, q.w };
            var entries = new List<object>();
            var centers = new List<Vector3>();
            foreach (var jointName in new[] { "rh_l1", "rh_l2", "rh_r1_joint", "rh_r2" })
            {
                var body = mappedJoints["omy_f3m:" + jointName];
                var sourceId = cloned.Single(x => x.Value == body).Key;
                var go = blocks[Reference(blocks[sourceId].Text, "m_GameObject")];
                var articulation = Regex.Matches(go.Text, @"component: \{fileID: (-?\d+)\}").Cast<Match>()
                    .Select(m => blocks[long.Parse(m.Groups[1].Value)]).Single(x => x.Type == 171741748);
                var visual = body.Find("Visuals");
                Require(visual != null, "Finger own visual subtree missing");
                var own = visual.GetComponentsInChildren<MeshRenderer>(true);
                Require(own.Length == 1, "Own-body center requires the single source finger renderer");
                var all = body.GetComponentsInChildren<MeshRenderer>(true);
                Bounds BoundsOf(MeshRenderer[] renderers)
                {
                    var bb = renderers[0].bounds; foreach (var rr in renderers) bb.Encapsulate(rr.bounds); return bb;
                }
                var ownBounds = BoundsOf(own); var subtreeBounds = BoundsOf(all);
                centers.Add(subtreeBounds.center);
                entries.Add(new { joint_name = jointName, body_path = PathOf(body), source_transform_file_id = sourceId,
                    home_position_unity_world_m = Vec(body.position), home_rotation_unity_world_xyzw = Quat(body.rotation),
                    home_local_position_m = Vec(body.localPosition), home_local_rotation_xyzw = Quat(body.localRotation),
                    home_local_scale = Vec(body.localScale), home_lossy_scale = Vec(body.lossyScale),
                    anchor_position_local_m = Vec(Vector(articulation.Text, "m_AnchorPosition")),
                    anchor_rotation_local_xyzw = Quat(Rotation(articulation.Text, "m_AnchorRotation")),
                    parent_anchor_position_local_m = Vec(Vector(articulation.Text, "m_ParentAnchorPosition")),
                    parent_anchor_rotation_local_xyzw = Quat(Rotation(articulation.Text, "m_ParentAnchorRotation")),
                    own_visual_center_body_local_m = Vec(body.InverseTransformPoint(ownBounds.center)),
                    own_visual_center_unity_world_m = Vec(ownBounds.center),
                    subtree_renderer_center_body_local_m_at_home = Vec(body.InverseTransformPoint(subtreeBounds.center)),
                    subtree_renderer_center_unity_world_m_at_home = Vec(subtreeBounds.center),
                    own_renderer_count = own.Length, subtree_renderer_count = all.Length });
            }
            var source = SourceProject + "/Assets/Scripts/IitpOmyPickPlaceDemo.cs";
            sourceHashes[source] = Hash(source);
            File.WriteAllText(EvidencePath + "/gripper-geometry.json", JsonSerializer.Serialize(new {
                source_scene_sha256 = sourceHashes[SourceScene], sampler_source = source, sampler_source_sha256 = Hash(source),
                note = "Source GetCenter uses collider subtree bounds first; renderer subtree bounds only as fallback. No collider qualification claimed. l1/r1 subtree includes moving l2/r2; its local center is home-only, not a fixed FK offset.",
                finger_bodies = entries, renderer_fallback_midpoint_unity_world_m_at_home = Vec(centers.Aggregate(Vector3.zero, (a,b) => a+b)/4),
                renderer_fallback_midpoint_ros_world_m_at_home = Scenario1Mirror.ToRosPosition(centers.Aggregate(Vector3.zero, (a,b) => a+b)/4)
            }, new JsonSerializerOptions { WriteIndented = true }));
        }

        static string PathOf(Transform t) => t.parent == null ? t.name : PathOf(t.parent) + "/" + t.name;

        public static void RenderPreview()
        {
            Require(Application.unityVersion == "6000.5.6f1", "Exact Editor required");
            Require(Path.GetFileName(Path.GetDirectoryName(Application.dataPath)) == "IsaacScenario1Observer", "Separate project only");
            EditorSceneManager.OpenScene(ScenePath);
            var camera = GameObject.Find("ObserverOverviewCamera").GetComponent<Camera>();
            var target = new RenderTexture(1280, 800, 24);
            var texture = new Texture2D(1280, 800, TextureFormat.RGB24, false);
            var previous = RenderTexture.active;
            try
            {
                camera.targetTexture = target;
                camera.Render();
                RenderTexture.active = target;
                texture.ReadPixels(new Rect(0, 0, 1280, 800), 0, 0);
                texture.Apply();
                File.WriteAllBytes(EvidencePath + "/editor-layout-preview.png", texture.EncodeToPNG());
            }
            finally
            {
                camera.targetTexture = null; RenderTexture.active = previous;
                target.Release(); Object.DestroyImmediate(target); Object.DestroyImmediate(texture);
            }
            Debug.Log("SCENARIO1_EDITOR_RENDER_PASS | no Play Mode or Player launched");
        }

        public static void CompareGeometryPreflight()
        {
            Require(Path.GetFileName(Path.GetDirectoryName(Application.dataPath)) == "IsaacScenario1Observer", "Separate project only");
            EditorSceneManager.OpenScene(ScenePath);
            var mirror = Object.FindAnyObjectByType<Scenario1Mirror>();
            const string input = "/tmp/sang-isaac-scenario1/geometry-preflight.json";
            var bytes = File.ReadAllBytes(input);
            using var doc = JsonDocument.Parse(bytes);
            var positions = mirror.bindings.Select(b => Scenario1Mirror.ToRosPosition(b.root.position)).ToArray();
            var rotations = mirror.bindings.Select(b => Scenario1Mirror.ToRosRotation(b.root.rotation)).ToArray();
            var rows = new List<object>();
            foreach (var row in doc.RootElement.GetProperty("measurements").EnumerateArray())
            {
                var measured = Numbers(row.GetProperty("measured"));
                Require(measured.Length == 16, "Expected main's seven OMY, six RB5, three carrier measured values");
                var sample = new MeasuredSnapshot { Assets = Enumerable.Range(0, 4).Select(i => new AssetSample {
                    Id = SnapshotGate.AssetIds[i], Position = positions[i], Rotation = rotations[i],
                    Joints = i == 0 ? measured.Take(7).ToArray() : i == 1 ? measured.Skip(7).Take(6).ToArray() : new double[0]
                }).ToArray() };
                mirror.ApplyMeasured(sample);
                var own = Vector3.zero; var subtree = Vector3.zero;
                foreach (var name in new[] { "rh_l1", "rh_l2", "rh_r1_joint", "rh_r2" })
                {
                    var index = Array.IndexOf(mirror.bindings[0].visualJointNames, name);
                    var finger = mirror.bindings[0].joints[index];
                    Bounds Union(Transform root)
                    {
                        var rs = root.GetComponentsInChildren<MeshRenderer>(true); var bounds = rs[0].bounds;
                        foreach (var r in rs) bounds.Encapsulate(r.bounds);
                        return bounds;
                    }
                    own += Union(finger.Find("Visuals")).center / 4;
                    subtree += Union(finger).center / 4;
                }
                var sourceGrip = Scenario1Mirror.ToRosPosition(own);
                var sourceTcp = Scenario1Mirror.ToRosPosition(mirror.bindings[1].root.GetComponentsInChildren<Transform>(true).Single(t => t.name == "tcp").position);
                var isaacGrip = Numbers(row.GetProperty("grip_position_m"));
                var isaacTcp = Numbers(row.GetProperty("rb5_tcp_m"));
                double Distance(double[] a, double[] b) => Math.Sqrt(a.Select((v,i) => (v-b[i])*(v-b[i])).Sum());
                rows.Add(new { phase = row.GetProperty("phase").GetString(),
                    source_own_visual_grip_ros_m = sourceGrip, source_renderer_subtree_grip_ros_m = Scenario1Mirror.ToRosPosition(subtree),
                    isaac_grip_ros_m = isaacGrip, own_visual_grip_difference_m = Distance(sourceGrip, isaacGrip),
                    source_tcp_ros_m = sourceTcp, isaac_tcp_ros_m = isaacTcp, tcp_difference_m = Distance(sourceTcp, isaacTcp) });
            }
            using var digest = SHA256.Create();
            File.WriteAllText(EvidencePath + "/geometry-preflight-comparison.json", JsonSerializer.Serialize(new {
                kind = "offline_editor_fk_comparison_not_execution", preflight_sha256 = string.Concat(digest.ComputeHash(bytes).Select(b => b.ToString("x2"))),
                note = "Applies main's measured joint values to the saved visual hierarchy only. No physics, Play Mode, Player, or scene save. Own visual center differs from original collider-subtree sampler.",
                measurements = rows
            }, new JsonSerializerOptions { WriteIndented = true }));
            Debug.Log("SCENARIO1_PREFLIGHT_COMPARISON_PASS | " + rows.Count);
        }

        static void AuditScene()
        {
            var components = EditorSceneManager.GetActiveScene().GetRootGameObjects().SelectMany(x => x.GetComponentsInChildren<Component>(true)).ToArray();
            Require(components.All(c => c is Transform || c is MeshFilter || c is MeshRenderer || c is Camera || c is Light || c is Scenario1Mirror), "Nonvisual scene component");
            Require(components.Count(c => c is Scenario1Mirror) == 1, "Exactly one observer required");
            Directory.CreateDirectory(EvidencePath);
            File.WriteAllLines(EvidencePath + "/scene-components.txt", components.Select(c => PathOf(c.transform) + " | " + c.GetType().FullName));
        }

        public static void BuildLinux()
        {
            PrepareScene();
            Scenario1ObserverTests.Run(checks);
            PlayerSettings.productName = "Isaac Scenario1 Passive Observer";
            PlayerSettings.companyName = "SANG";
            PlayerSettings.runInBackground = true;
            PlayerSettings.SetScriptingBackend(NamedBuildTarget.Standalone, ScriptingImplementation.Mono2x);
            PlayerSettings.SetGraphicsAPIs(BuildTarget.StandaloneLinux64, new[] { GraphicsDeviceType.OpenGLCore });
            Directory.CreateDirectory(Path.GetDirectoryName(BuildPath));
            var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions { scenes = new[] { ScenePath },
                locationPathName = BuildPath, target = BuildTarget.StandaloneLinux64, options = BuildOptions.StrictMode });
            Require(report.summary.result == BuildResult.Succeeded, "Linux build failed");
            AuditPlayer();
            foreach (var source in sourceHashes) Require(Hash(source.Key) == source.Value, "Source changed during build: " + source.Key);
            var mirror = Object.FindAnyObjectByType<Scenario1Mirror>();
            File.WriteAllLines(EvidencePath + "/tests-passed.txt", checks);
            File.WriteAllText(EvidencePath + "/build-result.json", JsonSerializer.Serialize(new {
                build_role = "observer", profile_id = "scenario1", unity_version = Application.unityVersion,
                scene_sha256 = mirror.trustedSceneSha256, build_profile_sha256 = Hash("build-profile.json"),
                observer_dll_sha256 = Hash("Builds/Linux/IsaacScenario1Observer_Data/Managed/Sang.IsaacScenario1Observer.dll"),
                result = "PASS", test_count = checks.Count, physical_output_connected = false, live_player_launched = false
            }, new JsonSerializerOptions { WriteIndented = true }));
            Debug.Log("ISAAC_SCENARIO1_OBSERVER_BUILD_PASS | " + checks.Count);
        }

        static IEnumerable<object> AllTypes(dynamic module)
        {
            foreach (dynamic type in module.Types)
                foreach (var item in TypeAndChildren(type)) yield return item;
        }

        static IEnumerable<object> TypeAndChildren(dynamic type)
        {
            yield return type;
            foreach (dynamic child in type.NestedTypes)
                foreach (var nested in TypeAndChildren(child)) yield return nested;
        }

        public static void AuditPlayer()
        {
            var cecil = Assembly.LoadFrom(Path.Combine(EditorApplication.applicationContentsPath, "Managed/Unity.Cecil.dll"));
            var read = cecil.GetType("Mono.Cecil.AssemblyDefinition").GetMethod("ReadAssembly", new[] { typeof(string) });
            var dlls = Directory.GetFiles("Builds/Linux", "*.dll", SearchOption.AllDirectories);
            Require(dlls.Count(path => Path.GetFileName(path) == "Sang.IsaacScenario1Observer.dll") == 1, "Missing unique observer assembly");
            var lines = new List<string>();
            var banned = new[] { "Rb5SimToReal", "OmySimToReal", "SangUnityScenarioAgent", "Scenario1_noAnomaly",
                "SangScenarioBoundController", "IitpOmyPickPlaceDemo", "SangUnitySimulationBridge", "Unity.Robotics" };
            foreach (var path in dlls)
            {
                var name = Path.GetFileNameWithoutExtension(path);
                Require(name != "Assembly-CSharp" && name != "Assembly-CSharp-firstpass" && !name.EndsWith(".Editor"), "Forbidden Player assembly: " + name);
                Require(name == "Sang.IsaacScenario1Observer" || name.StartsWith("UnityEngine") || name.StartsWith("System")
                    || name == "mscorlib" || name == "netstandard" || name == "Mono.Security" || name == "Microsoft.CSharp"
                    || name == "Microsoft.Bcl.AsyncInterfaces" || name == "Unity.Scripting",
                    "Unreviewed Player assembly: " + name);
                dynamic assembly = read.Invoke(null, new object[] { path });
                try
                {
                    lines.Add(Hash(path) + "  " + path);
                    foreach (dynamic type in AllTypes(assembly.MainModule))
                    {
                        string typeName = type.FullName;
                        Require(!banned.Any(value => typeName.Contains(value)), "Forbidden Player type: " + typeName);
                        if (name != "Sang.IsaacScenario1Observer") continue;
                        lines.Add("TYPE " + typeName);
                        foreach (dynamic method in type.Methods)
                        {
                            if (!(bool)method.HasBody) continue;
                            foreach (dynamic instruction in method.Body.Instructions)
                            {
                                string operand = instruction.Operand == null ? "" : instruction.Operand.ToString();
                                Require(!new[] { "System.Net.", "UnityWebRequest", "ArticulationBody", "Rigidbody", "System.Diagnostics.Process",
                                    "System.Reflection", "DllImport", "SangUnity", "Scenario1_" }.Any(value => operand.Contains(value)),
                                    "Forbidden observer IL reference: " + operand);
                            }
                        }
                    }
                    if (name == "Sang.IsaacScenario1Observer")
                        foreach (dynamic reference in assembly.MainModule.AssemblyReferences) lines.Add("REFERENCE " + reference.FullName);
                }
                finally { ((IDisposable)assembly).Dispose(); }
            }
            File.WriteAllLines(EvidencePath + "/player-assembly-audit.txt", lines);
            Debug.Log("ISAAC_SCENARIO1_OBSERVER_ASSEMBLIES_PASS | " + dlls.Length);
        }
    }
}
