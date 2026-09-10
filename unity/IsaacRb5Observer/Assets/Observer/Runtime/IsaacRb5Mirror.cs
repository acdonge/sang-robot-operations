using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;
using UnityEngine;

namespace Sang.IsaacRb5Observer
{
    public sealed class MeasuredSnapshot
    {
        public string Session;
        public long Epoch, Sequence, SimTimeNs, ReceivedAtUnixMs;
        public double[] Positions;
    }

    public sealed class SnapshotGate
    {
        public const string SceneSha256 = "5837e8eb41c67024a4d461f07e3d63dc53bde338209fd64c6c238ceb187972ce";
        public const int MaximumBytes = 8192;
        public const long MaximumAgeMs = 500;
        public static readonly string[] JointNames = { "base", "shoulder", "elbow", "wrist1", "wrist2", "wrist3" };
        static readonly string[] Fields = { "schema_version", "runtime_kind", "profile_id", "asset_id", "scene_sha256",
            "session_id", "epoch", "sequence", "sim_time_ns", "received_at_unix_ms", "physical_output_connected",
            "joint_names", "joint_positions_rad" };
        readonly string session;
        readonly long epoch;
        double acceptedMonotonicMs;
        public MeasuredSnapshot Last { get; private set; }
        public long Accepted { get; private set; }
        public long Rejected { get; private set; }
        public string Reason { get; private set; } = "waiting";

        public SnapshotGate(string sessionId, long expectedEpoch)
        {
            if (!Regex.IsMatch(sessionId ?? "", "\\A[0-9a-f]{32}\\z") || expectedEpoch < 0)
                throw new ArgumentException("A pinned lowercase uuidhex session and nonnegative epoch are required");
            session = sessionId;
            epoch = expectedEpoch;
        }

        static void Require(bool condition, string reason)
        {
            if (!condition) throw new FormatException(reason);
        }

        static string Scalar(JsonElement element, JsonValueKind type)
        {
            Require(element.ValueKind == type, "field-type");
            return type == JsonValueKind.String ? element.GetString() : element.GetRawText();
        }

        static long Integer(JsonElement element)
        {
            var raw = Scalar(element, JsonValueKind.Number);
            Require(Regex.IsMatch(raw, "\\A(?:0|[1-9][0-9]*)\\z"), "integer-required");
            return long.Parse(raw, NumberStyles.None, CultureInfo.InvariantCulture);
        }

        static JsonElement[] ArrayItems(JsonElement element)
        {
            Require(element.ValueKind == JsonValueKind.Array, "array-required");
            Require(element.GetArrayLength() == 6, "six-joints-required");
            return element.EnumerateArray().ToArray();
        }

        public bool TryAccept(byte[] json, long nowUnixMs, double monotonicMs)
        {
            try
            {
                Require(json != null && json.Length > 0 && json.Length <= MaximumBytes, "file-size");
                using var document = JsonDocument.Parse(json, new JsonDocumentOptions { MaxDepth = 4 });
                var root = document.RootElement;
                Require(root.ValueKind == JsonValueKind.Object, "object-required");
                var items = root.EnumerateObject().ToArray();
                Require(items.Length == Fields.Length && items.Select(item => item.Name).Distinct().Count() == Fields.Length
                    && items.All(item => Fields.Contains(item.Name)), "field-closure");
                var fields = items.ToDictionary(item => item.Name, item => item.Value);
                Require(Integer(fields["schema_version"]) == 1, "schema-version");
                Require(Scalar(fields["runtime_kind"], JsonValueKind.String) == "isaac-sim-executor"
                    && Scalar(fields["profile_id"], JsonValueKind.String) == "rb5"
                    && Scalar(fields["asset_id"], JsonValueKind.String) == "isaac-twin-rb5"
                    && Scalar(fields["scene_sha256"], JsonValueKind.String) == SceneSha256, "source-identity");
                Require(fields["physical_output_connected"].ValueKind == JsonValueKind.False, "physical-output");
                var candidate = new MeasuredSnapshot
                {
                    Session = Scalar(fields["session_id"], JsonValueKind.String), Epoch = Integer(fields["epoch"]),
                    Sequence = Integer(fields["sequence"]), SimTimeNs = Integer(fields["sim_time_ns"]),
                    ReceivedAtUnixMs = Integer(fields["received_at_unix_ms"]), Positions = new double[6]
                };
                Require(candidate.Session == session && candidate.Epoch == epoch, "session-or-epoch-reset");
                Require(candidate.ReceivedAtUnixMs > 0 && candidate.ReceivedAtUnixMs <= nowUnixMs + 100
                    && candidate.ReceivedAtUnixMs >= nowUnixMs - MaximumAgeMs, "stale-or-future");
                var names = ArrayItems(fields["joint_names"]);
                var positions = ArrayItems(fields["joint_positions_rad"]);
                var seen = new HashSet<string>();
                for (var i = 0; i < 6; i++)
                {
                    var name = Scalar(names[i], JsonValueKind.String);
                    var index = System.Array.IndexOf(JointNames, name);
                    Require(index >= 0 && seen.Add(name), "joint-name");
                    var value = double.Parse(Scalar(positions[i], JsonValueKind.Number), NumberStyles.Float, CultureInfo.InvariantCulture);
                    Require(!double.IsNaN(value) && !double.IsInfinity(value) && Math.Abs(value) <= 3.14, "joint-range");
                    candidate.Positions[index] = value;
                }
                if (Last != null)
                {
                    Require(candidate.Sequence > Last.Sequence, "sequence-not-increasing");
                    Require(candidate.SimTimeNs > Last.SimTimeNs, "simulation-clock-not-increasing");
                    Require(candidate.ReceivedAtUnixMs >= Last.ReceivedAtUnixMs, "receive-clock-regressed");
                }
                Last = candidate;
                acceptedMonotonicMs = monotonicMs;
                Accepted++;
                Reason = "mirroring";
                return true;
            }
            catch (Exception exception) when (exception is FormatException || exception is JsonException
                || exception is OverflowException
                || exception is ArgumentException)
            {
                Reject(exception is FormatException ? exception.Message : "malformed-json");
                return false;
            }
        }

        public void Reject(string reason) { Rejected++; Reason = reason; }

        public bool IsFresh(long nowUnixMs, double monotonicMs)
        {
            return Last != null && nowUnixMs >= Last.ReceivedAtUnixMs - 100
                && nowUnixMs - Last.ReceivedAtUnixMs <= MaximumAgeMs
                && monotonicMs >= acceptedMonotonicMs && monotonicMs - acceptedMonotonicMs <= MaximumAgeMs;
        }
    }

    public sealed class IsaacRb5Mirror : MonoBehaviour
    {
        public const string TelemetryPath = "/tmp/sang-isaac-rb5-twin/observer/telemetry.json";
        public const string StatusPath = "/tmp/sang-isaac-rb5-twin/observer/mirror-status.json";
        public static readonly Vector3[] Axes = { Vector3.down, Vector3.right, Vector3.right,
            Vector3.right, Vector3.down, Vector3.right };
        public Transform[] joints = new Transform[6];
        public Quaternion[] zeroRotations = new Quaternion[6];
        SnapshotGate gate;
        byte[] previousBytes;
        double nextPollMs, nextReportMs;
        string state = "unconfigured";
        string configurationError;
        string expectedSession;
        long expectedEpoch;
        public double[] Readback { get; private set; } = new double[6];
        public double MaximumErrorRad { get; private set; }
        public double RmsErrorRad { get; private set; }

        [Serializable]
        sealed class MirrorReport
        {
            public int schema_version = 1;
            public string build_role = "observer", profile_id = "rb5", asset_id = "isaac-twin-rb5";
            public bool physical_output_connected = false, fresh;
            public string state, reason, session_id;
            public long epoch, sequence = -1, sim_time_ns = -1, received_at_unix_ms, reported_at_unix_ms, accepted, rejected;
            public string[] joint_names = SnapshotGate.JointNames;
            public double[] measured_joint_positions_rad, rendered_joint_positions_rad, mirror_error_rad;
            public double max_abs_mirror_error_rad, rms_mirror_error_rad;
        }

        void Awake()
        {
            Application.runInBackground = true;
            try
            {
                expectedSession = Environment.GetEnvironmentVariable("SANG_TWIN_SESSION_ID");
                var epochText = Environment.GetEnvironmentVariable("SANG_TWIN_EPOCH") ?? "0";
                if (!long.TryParse(epochText, NumberStyles.None, CultureInfo.InvariantCulture, out expectedEpoch))
                    throw new ArgumentException("SANG_TWIN_EPOCH must be a nonnegative integer");
                gate = new SnapshotGate(expectedSession, expectedEpoch);
                if (joints.Length != 6 || zeroRotations.Length != 6 || joints.Any(joint => joint == null)
                    || joints.Distinct().Count() != 6) throw new ArgumentException("Invalid visual joint binding");
            }
            catch (ArgumentException exception)
            {
                configurationError = exception.Message;
                gate = null;
                Debug.LogError("ISAAC_RB5_OBSERVER_CONFIG_REJECTED | " + configurationError);
            }
        }

        public void ApplyMeasured(double[] positions)
        {
            for (var i = 0; i < 6; i++)
                joints[i].localRotation = zeroRotations[i] * Quaternion.AngleAxis((float)(positions[i] * 180.0 / Math.PI), Axes[i]);
        }

        public void Measure(double[] positions)
        {
            MaximumErrorRad = 0;
            var sum = 0.0;
            for (var i = 0; i < 6; i++)
            {
                // Recover the signed twist from the actual transform, not from the input or a drive target.
                var relative = Quaternion.Inverse(zeroRotations[i]) * joints[i].localRotation;
                var projected = relative.x * Axes[i].x + relative.y * Axes[i].y + relative.z * Axes[i].z;
                var radians = 2.0 * Math.Atan2(projected, relative.w);
                Readback[i] = Math.Atan2(Math.Sin(radians), Math.Cos(radians));
                var error = Math.Atan2(Math.Sin(Readback[i] - positions[i]), Math.Cos(Readback[i] - positions[i]));
                MaximumErrorRad = Math.Max(MaximumErrorRad, Math.Abs(error));
                sum += error * error;
            }
            RmsErrorRad = Math.Sqrt(sum / 6.0);
        }

        void Update()
        {
            var monotonicMs = Time.realtimeSinceStartupAsDouble * 1000;
            if (gate == null || monotonicMs < nextPollMs) return;
            nextPollMs = monotonicMs + 20;
            try
            {
                byte[] bytes;
                using (var stream = new FileStream(TelemetryPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    if (stream.Length == 0 || stream.Length > SnapshotGate.MaximumBytes)
                    { gate.Reject("file-size"); return; }
                    bytes = new byte[(int)stream.Length];
                    var offset = 0;
                    while (offset < bytes.Length)
                    {
                        var count = stream.Read(bytes, offset, bytes.Length - offset);
                        if (count == 0) throw new IOException("Incomplete snapshot");
                        offset += count;
                    }
                }
                if (previousBytes != null && previousBytes.SequenceEqual(bytes)) return;
                previousBytes = bytes;
                if (gate.TryAccept(bytes, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(), monotonicMs))
                    ApplyMeasured(gate.Last.Positions);
            }
            catch (IOException) { gate.Reject("telemetry-unavailable"); }
            catch (UnauthorizedAccessException) { gate.Reject("telemetry-unavailable"); }
        }

        void LateUpdate()
        {
            var now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
            var monotonicMs = Time.realtimeSinceStartupAsDouble * 1000;
            var fresh = gate != null && gate.IsFresh(now, monotonicMs);
            if (gate?.Last != null) Measure(gate.Last.Positions);
            state = gate == null ? "unconfigured" : gate.Last == null ? "waiting" : !fresh ? "stale-frozen"
                : gate.Reason != "mirroring" ? "rejected-frozen" : MaximumErrorRad > 0.00001 ? "mirror-error" : "mirroring";
            if (monotonicMs < nextReportMs) return;
            nextReportMs = monotonicMs + 200;
            var report = new MirrorReport
            {
                fresh = fresh && gate.Reason == "mirroring", state = state, reason = gate?.Reason ?? configurationError,
                session_id = expectedSession, epoch = expectedEpoch, sequence = gate?.Last?.Sequence ?? -1,
                sim_time_ns = gate?.Last?.SimTimeNs ?? -1, received_at_unix_ms = gate?.Last?.ReceivedAtUnixMs ?? 0,
                reported_at_unix_ms = now, accepted = gate?.Accepted ?? 0, rejected = gate?.Rejected ?? 0,
                measured_joint_positions_rad = gate?.Last?.Positions, rendered_joint_positions_rad = gate?.Last == null ? null : Readback,
                mirror_error_rad = gate?.Last == null ? null : Readback.Select((value, i) => value - gate.Last.Positions[i]).ToArray(),
                max_abs_mirror_error_rad = MaximumErrorRad, rms_mirror_error_rad = RmsErrorRad
            };
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(StatusPath));
                var temporary = StatusPath + ".tmp";
                File.WriteAllText(temporary, JsonUtility.ToJson(report, true));
                if (File.Exists(StatusPath)) File.Replace(temporary, StatusPath, null);
                else File.Move(temporary, StatusPath);
            }
            catch (IOException exception) { Debug.LogWarning("Observer status write failed: " + exception.Message); }
            catch (UnauthorizedAccessException exception) { Debug.LogWarning("Observer status write failed: " + exception.Message); }
        }

        void OnGUI()
        {
            GUI.Box(new Rect(18, 18, 510, 115), "RB5 / ISAAC MEASURED STATE / OBSERVER ONLY");
            GUI.Label(new Rect(32, 48, 485, 24), "State: " + state + " | " + (gate?.Reason ?? configurationError));
            GUI.Label(new Rect(32, 72, 485, 24), "Sequence: " + (gate?.Last?.Sequence.ToString() ?? "none")
                + " | mirror error: " + MaximumErrorRad.ToString("G4", CultureInfo.InvariantCulture) + " rad");
            GUI.Label(new Rect(32, 96, 485, 24), "No robot commands, physics, autonomous motion, or ROS connection.");
        }
    }
}
