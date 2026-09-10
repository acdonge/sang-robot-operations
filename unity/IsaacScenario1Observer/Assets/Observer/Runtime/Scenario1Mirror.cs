using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;
using UnityEngine;

namespace Sang.IsaacScenario1Observer
{
    public sealed class AssetSample
    {
        public string Id;
        public double[] Joints, Position, Rotation;
    }

    public sealed class MeasuredSnapshot
    {
        public string Session, Phase, PlateOwner;
        public long Epoch, Sequence, SimTimeNs, ReceivedAtUnixMs;
        public AssetSample[] Assets;
    }

    public sealed class SnapshotGate
    {
        public const int MaximumBytes = 16384;
        public static readonly string[] AssetIds = { "isaac-scenario1-omy", "isaac-scenario1-rb5",
            "isaac-scenario1-carrier", "isaac-scenario1-plate" };
        public static readonly string[][] JointNames = {
            new[] { "joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "rh_r1_joint" },
            new[] { "base", "shoulder", "elbow", "wrist1", "wrist2", "wrist3" },
            new string[0], new string[0] };
        static readonly string[] Fields = { "schema_version", "runtime_kind", "profile_id", "scene_sha256", "session_id",
            "epoch", "sequence", "sim_time_ns", "received_at_unix_ms", "physical_output_connected", "frame_id", "phase", "plate_owner", "assets" };
        static readonly string[] AssetFields = { "asset_id", "joint_names", "joint_positions_rad", "position_m", "rotation_xyzw" };
        readonly string session, sceneHash;
        readonly long epoch;
        double acceptedMonotonicMs;
        public MeasuredSnapshot Last { get; private set; }
        public long Accepted { get; private set; }
        public long Rejected { get; private set; }
        public string Reason { get; private set; } = "waiting";

        public SnapshotGate(string expectedSession, long expectedEpoch, string trustedSceneHash)
        {
            if (!Regex.IsMatch(expectedSession ?? "", "\\A[0-9a-f]{32}\\z") || expectedEpoch < 0
                || !Regex.IsMatch(trustedSceneHash ?? "", "\\A[0-9a-f]{64}\\z"))
                throw new ArgumentException("Pinned session, epoch and trusted build scene hash are required");
            session = expectedSession; epoch = expectedEpoch; sceneHash = trustedSceneHash;
        }

        static void Require(bool condition, string reason)
        {
            if (!condition) throw new FormatException(reason);
        }

        static string Text(JsonElement value)
        {
            Require(value.ValueKind == JsonValueKind.String, "string-required");
            return value.GetString();
        }

        static long Integer(JsonElement value)
        {
            Require(value.ValueKind == JsonValueKind.Number, "integer-required");
            var raw = value.GetRawText();
            Require(Regex.IsMatch(raw, "\\A(?:0|[1-9][0-9]*)\\z"), "integer-required");
            return long.Parse(raw, NumberStyles.None, CultureInfo.InvariantCulture);
        }

        static Dictionary<string, JsonElement> ObjectFields(JsonElement value, string[] expected)
        {
            Require(value.ValueKind == JsonValueKind.Object, "object-required");
            var items = value.EnumerateObject().ToArray();
            Require(items.Length == expected.Length && items.Select(x => x.Name).Distinct().Count() == expected.Length
                && items.All(x => expected.Contains(x.Name)), "field-closure");
            return items.ToDictionary(x => x.Name, x => x.Value);
        }

        static JsonElement[] Items(JsonElement value, int count)
        {
            Require(value.ValueKind == JsonValueKind.Array && value.GetArrayLength() == count, "array-length");
            return value.EnumerateArray().ToArray();
        }

        static double[] Numbers(JsonElement value, int count)
        {
            return Items(value, count).Select(item => {
                Require(item.ValueKind == JsonValueKind.Number, "number-required");
                var number = item.GetDouble();
                Require(!double.IsNaN(number) && !double.IsInfinity(number) && Math.Abs(number) <= float.MaxValue, "nonfinite-number");
                return number;
            }).ToArray();
        }

        public bool TryAccept(byte[] bytes, long nowUnixMs, double monotonicMs)
        {
            try
            {
                Require(bytes != null && bytes.Length > 0 && bytes.Length <= MaximumBytes, "file-size");
                using var doc = JsonDocument.Parse(bytes, new JsonDocumentOptions { MaxDepth = 5 });
                var f = ObjectFields(doc.RootElement, Fields);
                Require(Integer(f["schema_version"]) == 2, "schema-version");
                Require(Text(f["runtime_kind"]) == "isaac-sim-executor" && Text(f["profile_id"]) == "scenario1"
                    && Text(f["scene_sha256"]) == sceneHash && Text(f["frame_id"]) == "cell", "source-identity");
                Require(f["physical_output_connected"].ValueKind == JsonValueKind.False, "physical-output");
                var sample = new MeasuredSnapshot { Session = Text(f["session_id"]), Epoch = Integer(f["epoch"]),
                    Sequence = Integer(f["sequence"]), SimTimeNs = Integer(f["sim_time_ns"]), ReceivedAtUnixMs = Integer(f["received_at_unix_ms"]),
                    Phase = Text(f["phase"]), PlateOwner = Text(f["plate_owner"]), Assets = new AssetSample[4] };
                Require(sample.Session == session && sample.Epoch == epoch, "session-or-epoch-reset");
                Require(sample.ReceivedAtUnixMs > 0 && sample.ReceivedAtUnixMs - nowUnixMs <= 100
                    && nowUnixMs - sample.ReceivedAtUnixMs <= 500, "stale-or-future");
                Require(sample.Phase.Length <= 128 && !sample.Phase.Any(char.IsControl), "phase-text");
                Require(new[] { "carrier", "omy", "table" }.Contains(sample.PlateOwner), "plate-owner");
                foreach (var element in Items(f["assets"], 4))
                {
                    var a = ObjectFields(element, AssetFields);
                    var id = Text(a["asset_id"]);
                    var index = System.Array.IndexOf(AssetIds, id);
                    Require(index >= 0 && sample.Assets[index] == null, "asset-identity");
                    var names = Items(a["joint_names"], JointNames[index].Length).Select(Text).ToArray();
                    Require(names.Distinct().Count() == names.Length && names.All(JointNames[index].Contains), "joint-names");
                    var values = Numbers(a["joint_positions_rad"], names.Length);
                    var rotation = Numbers(a["rotation_xyzw"], 4);
                    Require(Math.Abs(rotation.Sum(x => x*x) - 1) <= 0.001, "quaternion-norm");
                    sample.Assets[index] = new AssetSample { Id = id, Joints = JointNames[index].Select(name => values[System.Array.IndexOf(names, name)]).ToArray(),
                        Position = Numbers(a["position_m"], 3), Rotation = rotation };
                }
                if (Last != null)
                {
                    Require(sample.Sequence > Last.Sequence, "sequence-not-increasing");
                    Require(sample.SimTimeNs > Last.SimTimeNs, "simulation-clock-not-increasing");
                    Require(sample.ReceivedAtUnixMs >= Last.ReceivedAtUnixMs, "receive-clock-regressed");
                }
                Last = sample; acceptedMonotonicMs = monotonicMs; Accepted++; Reason = "mirroring";
                return true;
            }
            catch (Exception e) when (e is FormatException || e is JsonException || e is OverflowException || e is ArgumentException)
            {
                Reject(e is FormatException ? e.Message : "malformed-json");
                return false;
            }
        }

        public void Reject(string reason) { Reason = reason; Rejected++; }
        public bool IsFresh(long nowUnixMs, double monotonicMs) => Last != null
            && nowUnixMs - Last.ReceivedAtUnixMs >= -100 && nowUnixMs - Last.ReceivedAtUnixMs <= 500
            && monotonicMs >= acceptedMonotonicMs && monotonicMs - acceptedMonotonicMs <= 500;
    }

    [Serializable]
    public sealed class VisualBinding
    {
        public Transform root;
        public Transform[] joints;
        public Quaternion[] zeroRotations;
        public Vector3[] axes;
        public int[] sourceJointIndices;
        public string[] visualJointNames;
    }

    [Serializable]
    public sealed class AssetReadback
    {
        public string asset_id;
        public string[] joint_names, visual_joint_names;
        public double[] measured_joint_positions_rad, rendered_joint_positions_rad, joint_error_rad, visual_joint_error_rad;
        public double[] measured_position_m, rendered_position_m, measured_rotation_xyzw, rendered_rotation_xyzw;
        public double max_abs_joint_error_rad, position_error_m, rotation_error_rad;
    }

    public sealed class Scenario1Mirror : MonoBehaviour
    {
        public const string Root = "/tmp/sang-isaac-scenario1/observer";
        public const string TelemetryPath = Root + "/telemetry.json";
        public const string StatusPath = Root + "/mirror-status.json";
        public string trustedSceneSha256;
        public VisualBinding[] bindings;
        SnapshotGate gate;
        string expectedSession, configurationError, state = "unconfigured";
        long expectedEpoch;
        byte[] previousBytes;
        double nextPollMs, nextReportMs;
        public AssetReadback[] Readback { get; private set; }

        public static Vector3 RosPosition(double[] p) => new Vector3((float)-p[1], (float)p[2], (float)p[0]);
        public static Quaternion RosRotation(double[] q) => new Quaternion((float)q[1], (float)-q[2], (float)-q[0], (float)q[3]).normalized;
        public static double[] ToRosPosition(Vector3 p) => new double[] { p.z, -p.x, p.y };
        public static double[] ToRosRotation(Quaternion q) => new double[] { -q.z, q.x, -q.y, q.w };

        void Awake()
        {
            Application.runInBackground = true;
            try
            {
                expectedSession = Environment.GetEnvironmentVariable("SANG_SCENARIO1_SESSION_ID");
                if (!long.TryParse(Environment.GetEnvironmentVariable("SANG_SCENARIO1_EPOCH"), NumberStyles.None,
                    CultureInfo.InvariantCulture, out expectedEpoch)) throw new ArgumentException("Explicit epoch required");
                gate = new SnapshotGate(expectedSession, expectedEpoch, trustedSceneSha256);
                if (bindings == null || bindings.Length != 4 || bindings.Any(b => b.root == null
                    || b.joints.Length != b.axes.Length || b.joints.Length != b.zeroRotations.Length
                    || b.joints.Length != b.sourceJointIndices.Length || b.joints.Any(j => j == null)))
                    throw new ArgumentException("Invalid visual bindings");
            }
            catch (ArgumentException e) { gate = null; configurationError = e.Message; Debug.LogError("SCENARIO1_OBSERVER_UNCONFIGURED | " + e.Message); }
        }

        public void ApplyMeasured(MeasuredSnapshot sample)
        {
            for (var i = 0; i < 4; i++)
            {
                var binding = bindings[i]; var asset = sample.Assets[i];
                binding.root.SetPositionAndRotation(RosPosition(asset.Position), RosRotation(asset.Rotation));
                for (var j = 0; j < binding.joints.Length; j++)
                    binding.joints[j].localRotation = binding.zeroRotations[j] * Quaternion.AngleAxis(
                        (float)(asset.Joints[binding.sourceJointIndices[j]] % (2*Math.PI) * 180 / Math.PI), binding.axes[j]);
            }
        }

        static double RotationError(Quaternion a, Quaternion b)
        {
            var q = Quaternion.Inverse(a) * b;
            return 2*Math.Atan2(Math.Sqrt((double)q.x*q.x + (double)q.y*q.y + (double)q.z*q.z), Math.Abs(q.w));
        }

        public void Measure(MeasuredSnapshot sample)
        {
            Readback = new AssetReadback[4];
            for (var i = 0; i < 4; i++)
            {
                var b = bindings[i]; var a = sample.Assets[i];
                var r = new AssetReadback { asset_id = a.Id, joint_names = SnapshotGate.JointNames[i], visual_joint_names = b.visualJointNames,
                    measured_joint_positions_rad = a.Joints, rendered_joint_positions_rad = new double[a.Joints.Length],
                    joint_error_rad = new double[a.Joints.Length], visual_joint_error_rad = new double[b.joints.Length],
                    measured_position_m = a.Position, rendered_position_m = ToRosPosition(b.root.position),
                    measured_rotation_xyzw = a.Rotation, rendered_rotation_xyzw = ToRosRotation(b.root.rotation) };
                for (var j = 0; j < b.joints.Length; j++)
                {
                    var index = b.sourceJointIndices[j];
                    var relative = Quaternion.Inverse(b.zeroRotations[j]) * b.joints[j].localRotation;
                    var twist = relative.x*b.axes[j].x + relative.y*b.axes[j].y + relative.z*b.axes[j].z;
                    var angle = 2*Math.Atan2(twist, relative.w);
                    var expected = Quaternion.AngleAxis((float)(a.Joints[index] % (2*Math.PI)*180/Math.PI), b.axes[j]);
                    var error = RotationError(expected, relative);
                    r.visual_joint_error_rad[j] = error;
                    r.max_abs_joint_error_rad = Math.Max(r.max_abs_joint_error_rad, error);
                    if (b.visualJointNames[j] == SnapshotGate.JointNames[i][index])
                    {
                        r.rendered_joint_positions_rad[index] = Math.Atan2(Math.Sin(angle), Math.Cos(angle));
                        r.joint_error_rad[index] = error;
                    }
                }
                r.position_error_m = Math.Sqrt(r.rendered_position_m.Select((v, k) => (v-a.Position[k])*(v-a.Position[k])).Sum());
                r.rotation_error_rad = RotationError(RosRotation(a.Rotation), b.root.rotation);
                Readback[i] = r;
            }
        }

        void Update()
        {
            var monotonic = Time.realtimeSinceStartupAsDouble*1000;
            if (gate == null || monotonic < nextPollMs) return;
            nextPollMs = monotonic + 20;
            try
            {
                byte[] bytes;
                using (var stream = new FileStream(TelemetryPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    if (stream.Length < 1 || stream.Length > SnapshotGate.MaximumBytes) { gate.Reject("file-size"); return; }
                    bytes = new byte[(int)stream.Length];
                    var offset = 0;
                    while (offset < bytes.Length)
                    {
                        var count = stream.Read(bytes, offset, bytes.Length-offset);
                        if (count == 0) throw new IOException("Incomplete snapshot");
                        offset += count;
                    }
                }
                if (previousBytes != null && previousBytes.SequenceEqual(bytes)) return;
                previousBytes = bytes;
                if (gate.TryAccept(bytes, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), monotonic)) ApplyMeasured(gate.Last);
            }
            catch (IOException) { gate.Reject("telemetry-unavailable"); }
            catch (UnauthorizedAccessException) { gate.Reject("telemetry-unavailable"); }
        }

        [Serializable]
        sealed class Report
        {
            public int schema_version = 2;
            public string build_role = "observer", profile_id = "scenario1", frame_id = "cell";
            public bool physical_output_connected = false, fresh;
            public string scene_sha256, session_id, state, reason, phase, plate_owner;
            public long epoch, sequence, sim_time_ns, received_at_unix_ms, reported_at_unix_ms, accepted, rejected;
            public AssetReadback[] assets;
        }

        void LateUpdate()
        {
            var now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            var monotonic = Time.realtimeSinceStartupAsDouble*1000;
            var fresh = gate != null && gate.IsFresh(now, monotonic) && gate.Reason == "mirroring";
            if (gate?.Last != null) Measure(gate.Last);
            state = gate == null ? "unconfigured" : gate.Last == null ? "waiting" : !fresh ? "stale-or-rejected-frozen"
                : Readback.Any(r => r.position_error_m > 0.00001 || r.rotation_error_rad > 0.00001 || r.max_abs_joint_error_rad > 0.00001) ? "mirror-error" : "mirroring";
            if (monotonic < nextReportMs) return;
            nextReportMs = monotonic + 200;
            var report = new Report { fresh = fresh, scene_sha256 = trustedSceneSha256, session_id = expectedSession, epoch = expectedEpoch,
                state = state, reason = gate?.Reason ?? configurationError, phase = gate?.Last?.Phase, plate_owner = gate?.Last?.PlateOwner,
                sequence = gate?.Last?.Sequence ?? -1, sim_time_ns = gate?.Last?.SimTimeNs ?? -1,
                received_at_unix_ms = gate?.Last?.ReceivedAtUnixMs ?? 0, reported_at_unix_ms = now,
                accepted = gate?.Accepted ?? 0, rejected = gate?.Rejected ?? 0, assets = Readback };
            try
            {
                // Launcher requires the receiver's private directory; do not create an untrusted root here.
                var temporary = StatusPath + ".tmp";
                File.WriteAllText(temporary, JsonUtility.ToJson(report, true));
                if (File.Exists(StatusPath)) File.Replace(temporary, StatusPath, null); else File.Move(temporary, StatusPath);
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }

        void OnGUI()
        {
            GUI.Box(new Rect(18, 18, 620, 108), "SCENARIO 1 / ISAAC MEASURED STATE / PASSIVE OBSERVER");
            GUI.Label(new Rect(30, 45, 596, 22), state + " | " + (gate?.Reason ?? configurationError));
            GUI.Label(new Rect(30, 68, 596, 22), "Phase: " + gate?.Last?.Phase + " | Plate: " + gate?.Last?.PlateOwner + " | Sequence: " + gate?.Last?.Sequence);
            GUI.Label(new Rect(30, 91, 596, 22), "No physics, trajectories, IK, commands, networking, or automatic session rebind.");
        }
    }
}
