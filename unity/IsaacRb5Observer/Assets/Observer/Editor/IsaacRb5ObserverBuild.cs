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
using UnityEditor;
using UnityEditor.Build;
using UnityEditor.Build.Reporting;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering;
using Object = UnityEngine.Object;

namespace Sang.IsaacRb5Observer.Editor
{
    public static class IsaacRb5ObserverBuild
    {
        const string SourceProject = "/home/sa08050/app developer/unity_IITP/unity_IITP/100.Whole_integration/Scenario1_app";
        const string SourceScene = SourceProject + "/Assets/Scenes/SampleScene.unity";
        const string ScenePath = "Assets/Observer/Scenes/Rb5Observer.unity";
        const string BuildPath = "Builds/Linux/IsaacRb5Observer.x86_64";
        const string EvidencePath = "Builds/Evidence";
        const string MeshFolder = "Assets/Observer/Visuals";
        static readonly CultureInfo Culture = CultureInfo.InvariantCulture;
        static readonly List<string> checks = new List<string>();
        static readonly Dictionary<string, string> sourceHashes = new Dictionary<string, string>();
        static readonly Dictionary<string, string> meshPaths = new Dictionary<string, string>();
        static readonly Dictionary<string, Transform> mappedJoints = new Dictionary<string, Transform>();
        static Material material;

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
            Directory.CreateDirectory(MeshFolder);
            for (var i = 0; i < 7; i++)
            {
                var source = SourceProject + "/Assets/IsaacSource/RB5Official/rbpodo_description/meshes/rb5_850e/visual/link" + i + ".dae";
                var destination = MeshFolder + "/link" + i + ".dae";
                sourceHashes[source] = Hash(source);
                sourceHashes[source + ".meta"] = Hash(source + ".meta");
                File.Copy(source, destination, true);
                File.Copy(source + ".meta", destination + ".meta", true);
                meshPaths[Match(File.ReadAllText(source + ".meta"), @"^guid: ([0-9a-f]+)$")] = destination;
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
            Require(meshPaths.ContainsKey(guid), "Visual prefab is not one of the seven RB5 meshes: " + guid);
            Require(new[] { "m_RemovedComponents", "m_RemovedGameObjects", "m_AddedGameObjects", "m_AddedComponents" }
                .All(key => instance.Text.Contains(key + ": []")), "Visual prefab has structural overrides");
            var model = AssetDatabase.LoadAssetAtPath<GameObject>(meshPaths[guid]);
            Require(model != null, "RB5 model import failed");
            var map = new Dictionary<long, Transform>();
            var result = CloneModel(model.transform, parent, map);
            foreach (Match modification in Regex.Matches(instance.Text,
                @"- target: \{fileID: (-?\d+),[^\n]+\n\s+propertyPath: ([^\n]+)\n\s+value: ([^\n]*)\n"))
            {
                var path = modification.Groups[2].Value;
                if (!path.StartsWith("m_LocalPosition.") && !path.StartsWith("m_LocalRotation.") && !path.StartsWith("m_LocalScale.")) continue;
                Require(map.TryGetValue(long.Parse(modification.Groups[1].Value), out var target), "Unknown transform override");
                var serialized = new SerializedObject(target);
                var property = serialized.FindProperty(path);
                Require(property != null, "Unknown pose property");
                property.floatValue = Number(modification.Groups[3].Value);
                serialized.ApplyModifiedPropertiesWithoutUndo();
            }
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
            result.SetParent(parent, false);
            result.localPosition = Vector(block.Text, "m_LocalPosition");
            result.localRotation = Rotation(block.Text, "m_LocalRotation");
            result.localScale = Vector(block.Text, "m_LocalScale");
            result.gameObject.SetActive(Property(go.Text, "m_IsActive") == "1");
            var components = Regex.Matches(go.Text, @"component: \{fileID: (-?\d+)\}").Cast<Match>()
                .Select(item => blocks[long.Parse(item.Groups[1].Value)]).ToArray();
            Require(components.All(item => item.Type != 33 && item.Type != 23), "Unexpected direct source mesh");
            var joint = components.SingleOrDefault(item => item.Text.Contains("  jointName:") && !item.Text.Contains("  jointName: tcp_joint"));
            if (joint != null)
            {
                var jointName = Property(joint.Text, "jointName");
                var index = Array.IndexOf(SnapshotGate.JointNames, jointName);
                Require(index >= 0 && name == "link" + (index + 1) && !mappedJoints.ContainsKey(jointName), "Joint mapping mismatch");
                var articulation = components.Single(item => item.Type == 171741748);
                var axis = Rotation(articulation.Text, "m_AnchorRotation") * Vector3.right;
                Require(Vector3.Distance(axis, IsaacRb5Mirror.Axes[index]) < 0.00001, "Joint axis mismatch: " + jointName);
                Require(Quaternion.Angle(result.localRotation, Quaternion.identity) < 0.001, "Source joint is not at zero pose");
                mappedJoints.Add(jointName, result);
            }
            var children = Match(block.Text, @"m_Children:([^\n]*(?:\n  - \{fileID: -?\d+\})*)");
            foreach (Match child in Regex.Matches(children, @"fileID: (-?\d+)")) CopyTransform(long.Parse(child.Groups[1].Value), result, blocks);
            return result;
        }

        public static void PrepareScene()
        {
            Require(Application.unityVersion == "6000.5.6f1", "Exact Editor 6000.5.6f1 required");
            Require(!Path.GetFullPath(Application.dataPath).StartsWith(SourceProject + "/", StringComparison.Ordinal), "Never run in legacy project");
            sourceHashes.Clear(); meshPaths.Clear(); mappedJoints.Clear();
            var blocks = ReadSource();
            ImportMeshes();
            var scene = EditorSceneManager.NewScene(NewSceneSetup.EmptyScene, NewSceneMode.Single);
            material = new Material(Shader.Find("Standard")) { name = "ObserverRobot", color = new Color(0.38f, 0.46f, 0.51f) };
            var materialPath = MeshFolder + "/ObserverRobot.mat";
            if (AssetDatabase.LoadAssetAtPath<Material>(materialPath) != null) AssetDatabase.DeleteAsset(materialPath);
            AssetDatabase.CreateAsset(material, materialPath);
            var root = blocks.Values.Single(item => item.Type == 1 && Regex.IsMatch(item.Text, @"^  m_Name: rb5_850e$", RegexOptions.Multiline));
            var rootTransform = blocks.Values.Single(item => item.Type == 4 && item.Text.Contains("  m_GameObject: {fileID: " + root.Id + "}"));
            var robot = CopyTransform(rootTransform.Id, null, blocks);
            Require(mappedJoints.Count == 6, "Six named joints not found");
            var observer = new GameObject("IsaacMeasuredStateObserver").AddComponent<IsaacRb5Mirror>();
            observer.joints = SnapshotGate.JointNames.Select(name => mappedJoints[name]).ToArray();
            observer.zeroRotations = observer.joints.Select(joint => joint.localRotation).ToArray();
            var renderers = robot.GetComponentsInChildren<MeshRenderer>(true);
            Require(renderers.Length == 8 && renderers.All(renderer => renderer.enabled), "Expected eight RB5 visual mesh renderers");
            var bounds = renderers[0].bounds;
            foreach (var renderer in renderers) bounds.Encapsulate(renderer.bounds);
            Require(bounds.size.magnitude > 0.5f && bounds.size.magnitude < 3, "Implausible RB5 visual bounds");
            var camera = new GameObject("ObserverCamera").AddComponent<Camera>();
            camera.transform.position = bounds.center + new Vector3(1.8f, 0.9f, -2.2f);
            camera.transform.LookAt(bounds.center);
            camera.clearFlags = CameraClearFlags.SolidColor;
            camera.backgroundColor = new Color(0.09f, 0.13f, 0.16f);
            camera.nearClipPlane = 0.01f;
            camera.farClipPlane = 20;
            var light = new GameObject("ObserverLight").AddComponent<Light>();
            light.type = LightType.Directional;
            light.intensity = 1.4f;
            light.transform.rotation = Quaternion.Euler(40, -35, 0);
            RenderSettings.ambientLight = new Color(0.6f, 0.6f, 0.6f);
            Directory.CreateDirectory(Path.GetDirectoryName(ScenePath));
            Require(EditorSceneManager.SaveScene(scene, ScenePath), "Clean scene save failed");
            AssetDatabase.SaveAssets();
            AuditScene();
            Directory.CreateDirectory(EvidencePath);
            File.WriteAllLines(EvidencePath + "/source-sha256.txt", sourceHashes.OrderBy(pair => pair.Key).Select(pair => pair.Value + "  " + pair.Key));
        }

        static void AuditScene()
        {
            var scene = EditorSceneManager.GetActiveScene();
            var components = scene.GetRootGameObjects().SelectMany(root => root.GetComponentsInChildren<Component>(true)).ToArray();
            Require(components.All(component => component is Transform || component is MeshFilter || component is MeshRenderer
                || component is Camera || component is Light || component is IsaacRb5Mirror), "Non-allowlisted component in observer scene");
            Require(components.Count(component => component is IsaacRb5Mirror) == 1, "Exactly one observer required");
            Directory.CreateDirectory(EvidencePath);
            File.WriteAllLines(EvidencePath + "/scene-components.txt", components.Select(component => PathOf(component.transform) + " | " + component.GetType().FullName));
        }

        static string PathOf(Transform target) => target.parent == null ? target.name : PathOf(target.parent) + "/" + target.name;

        public static void BuildLinux()
        {
            PrepareScene();
            RunTests();
            PlayerSettings.productName = "Isaac RB5 Measured Observer";
            PlayerSettings.companyName = "SANG";
            PlayerSettings.runInBackground = true;
            PlayerSettings.SetScriptingBackend(UnityEditor.Build.NamedBuildTarget.Standalone, ScriptingImplementation.Mono2x);
            PlayerSettings.SetGraphicsAPIs(BuildTarget.StandaloneLinux64, new[] { GraphicsDeviceType.OpenGLCore });
            Directory.CreateDirectory(Path.GetDirectoryName(BuildPath));
            var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions { scenes = new[] { ScenePath },
                locationPathName = BuildPath, target = BuildTarget.StandaloneLinux64, options = BuildOptions.StrictMode });
            Require(report.summary.result == BuildResult.Succeeded, "Observer Linux build failed: " + report.summary.result);
            AuditPlayer();
            foreach (var source in sourceHashes) Require(Hash(source.Key) == source.Value, "Legacy source changed during build: " + source.Key);
            File.WriteAllText(EvidencePath + "/build-result.json", "{\"build_role\":\"observer\",\"unity_version\":\"" + Application.unityVersion
                + "\",\"result\":\"PASS\",\"test_count\":" + checks.Count + ",\"physical_output_connected\":false,\"signed_attestation\":false}");
            Debug.Log("ISAAC_RB5_OBSERVER_BUILD_PASS | " + checks.Count + " checks | " + Path.GetFullPath(BuildPath));
        }

        static void Check(string name, bool passed)
        {
            Require(passed, "Test failed: " + name);
            checks.Add(name);
        }

        static string Fixture(long sequence = 1, long simTime = 100, long received = 1000000)
        {
            return "{\"schema_version\":1,\"runtime_kind\":\"isaac-sim-executor\",\"profile_id\":\"rb5\",\"asset_id\":\"isaac-twin-rb5\","
                + "\"scene_sha256\":\"" + SnapshotGate.SceneSha256 + "\",\"session_id\":\"0123456789abcdef0123456789abcdef\",\"epoch\":0,"
                + "\"sequence\":" + sequence + ",\"sim_time_ns\":" + simTime + ",\"received_at_unix_ms\":" + received
                + ",\"physical_output_connected\":false,\"joint_names\":[\"base\",\"shoulder\",\"elbow\",\"wrist1\",\"wrist2\",\"wrist3\"],"
                + "\"joint_positions_rad\":[0.1,0,0,0,0,0]}";
        }

        static SnapshotGate Gate() => new SnapshotGate("0123456789abcdef0123456789abcdef", 0);
        static bool Accept(SnapshotGate gate, string json, long now = 1000000, double monotonic = 1) => gate.TryAccept(Encoding.UTF8.GetBytes(json), now, monotonic);

        public static void RunTests()
        {
            checks.Clear();
            var valid = Fixture();
            var gate = Gate();
            Check("valid snapshot accepted", Accept(gate, valid));
            Check("duplicate sequence rejected", !Accept(gate, valid));
            Check("out-of-order sequence rejected", !Accept(gate, Fixture(0, 200)));
            Check("simulation clock regression rejected", !Accept(gate, Fixture(2, 50)));
            Check("paused simulation time rejected", !Accept(gate, Fixture(2, 100)));
            Check("rejected inputs preserve watermark", gate.Last.Sequence == 1 && gate.Accepted == 1);
            Check("following valid sample recovers", Accept(gate, Fixture(2, 200), monotonic: 2));
            Check("monotonic timeout freezes despite unchanged wall clock", !gate.IsFresh(1000000, 503));
            Check("wall clock timeout", !gate.IsFresh(1000501, 3));
            Check("wall clock rollback fails freshness", !gate.IsFresh(999899, 3));
            var invalid = new Dictionary<string, string>
            {
                ["truncated JSON"] = valid.Substring(0, valid.Length - 1),
                ["trailing JSON"] = valid + "{}",
                ["missing physical output field"] = valid.Replace("\"physical_output_connected\":false,", ""),
                ["physical output connected"] = valid.Replace(":false", ":true"),
                ["wrong schema"] = valid.Replace("\"schema_version\":1", "\"schema_version\":2"),
                ["wrong runtime"] = valid.Replace("isaac-sim-executor", "unity-sim-executor"),
                ["wrong profile"] = valid.Replace("\"profile_id\":\"rb5\"", "\"profile_id\":\"omy\""),
                ["wrong asset"] = valid.Replace("isaac-twin-rb5", "physical-rb5"),
                ["wrong scene digest"] = valid.Replace(SnapshotGate.SceneSha256, new string('0', 64)),
                ["wrong session"] = valid.Replace("0123456789abcdef0123456789abcdef", new string('0', 32)),
                ["reset epoch"] = valid.Replace("\"epoch\":0", "\"epoch\":1"),
                ["negative epoch"] = valid.Replace("\"epoch\":0", "\"epoch\":-1"),
                ["duplicate joint"] = valid.Replace("\"wrist3\"", "\"base\""),
                ["unknown joint"] = valid.Replace("\"wrist3\"", "\"joint6\""),
                ["missing joint"] = valid.Replace(",\"wrist3\"", ""),
                ["missing position"] = valid.Replace("[0.1,0,0,0,0,0]", "[0.1,0,0,0,0]"),
                ["nonfinite position"] = valid.Replace("[0.1,", "[1e999,"),
                ["NaN position"] = valid.Replace("[0.1,", "[NaN,"),
                ["joint beyond source range"] = valid.Replace("[0.1,", "[3.15,"),
                ["fractional sequence"] = valid.Replace("\"sequence\":1", "\"sequence\":1.2"),
                ["overflow sequence"] = valid.Replace("\"sequence\":1", "\"sequence\":9223372036854775808"),
                ["negative sequence"] = valid.Replace("\"sequence\":1", "\"sequence\":-1"),
                ["duplicate field"] = valid.Replace("\"schema_version\":1,", "\"schema_version\":1,\"schema_version\":1,"),
                ["unexpected field"] = valid.Replace("\"schema_version\":1,", "\"schema_version\":1,\"command\":0,"),
                ["stale receive time"] = Fixture(received: 999499),
                ["future receive time"] = Fixture(received: 1000101),
                ["string sequence"] = valid.Replace("\"sequence\":1", "\"sequence\":\"1\"")
            };
            foreach (var pair in invalid) Check(pair.Key, !Accept(Gate(), pair.Value));
            Check("oversized snapshot", !Accept(Gate(), new string(' ', SnapshotGate.MaximumBytes + 1)));
            var reordered = Gate();
            Check("joint order is name based", Accept(reordered, valid.Replace("\"base\",\"shoulder\"", "\"shoulder\",\"base\""))
                && reordered.Last.Positions[0] == 0 && reordered.Last.Positions[1] == 0.1);
            Check("initial zero simulation timestamp", Accept(Gate(), Fixture(0, 0)));
            gate = Gate();
            Accept(gate, Fixture(received: 1000000));
            Check("receive timestamp regression", !Accept(gate, Fixture(2, 200, 999999)));
            var observer = Object.FindAnyObjectByType<IsaacRb5Mirror>();
            Require(observer != null, "PrepareScene must run before transform tests");
            var sample = new[] { 0.1, -0.2, 0.3, -0.4, 0.5, -0.6 };
            observer.ApplyMeasured(sample);
            observer.Measure(sample);
            Check("six-axis signed readback below 1e-6 rad", observer.MaximumErrorRad < 0.000001);
            Check("base +0.1 rad is local -5.72958 degrees Y", Vector3.Distance(observer.joints[0].localRotation * Vector3.forward,
                new Vector3(-(float)Math.Sin(0.1), 0, (float)Math.Cos(0.1))) < 0.000001);
            observer.joints[2].localRotation *= Quaternion.AngleAxis(1, Vector3.right);
            observer.Measure(sample);
            Check("readback detects independent transform perturbation", observer.MaximumErrorRad > 0.017 && observer.MaximumErrorRad < 0.018);
            observer.ApplyMeasured(new double[6]);
            EditorSceneManager.SaveScene(EditorSceneManager.GetActiveScene(), ScenePath);
            File.WriteAllLines(EvidencePath + "/tests-passed.txt", checks);
            Debug.Log("ISAAC_RB5_OBSERVER_TESTS_PASS | " + checks.Count);
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
            Require(dlls.Count(path => Path.GetFileName(path) == "Sang.IsaacRb5Observer.dll") == 1, "Missing unique observer assembly");
            var lines = new List<string>();
            var banned = new[] { "Rb5SimToReal", "OmySimToReal", "SangUnityScenarioAgent", "Scenario1_noAnomaly",
                "SangScenarioBoundController", "IitpOmyPickPlaceDemo", "SangUnitySimulationBridge", "Unity.Robotics" };
            foreach (var path in dlls)
            {
                var name = Path.GetFileNameWithoutExtension(path);
                Require(name != "Assembly-CSharp" && name != "Assembly-CSharp-firstpass" && !name.EndsWith(".Editor"), "Forbidden Player assembly: " + name);
                Require(name == "Sang.IsaacRb5Observer" || name.StartsWith("UnityEngine") || name.StartsWith("System")
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
                        if (name != "Sang.IsaacRb5Observer") continue;
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
                    if (name == "Sang.IsaacRb5Observer")
                        foreach (dynamic reference in assembly.MainModule.AssemblyReferences) lines.Add("REFERENCE " + reference.FullName);
                }
                finally { ((IDisposable)assembly).Dispose(); }
            }
            File.WriteAllLines(EvidencePath + "/player-assembly-audit.txt", lines);
            Debug.Log("ISAAC_RB5_OBSERVER_ASSEMBLIES_PASS | " + dlls.Length);
        }
    }
}
