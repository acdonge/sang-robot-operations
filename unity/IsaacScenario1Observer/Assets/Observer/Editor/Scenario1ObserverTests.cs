using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using UnityEditor.Build;
using UnityEditor.SceneManagement;
using UnityEngine;
using Object = UnityEngine.Object;

namespace Sang.IsaacScenario1Observer.Editor
{
    public static class Scenario1ObserverTests
    {
        const string Session = "0123456789abcdef0123456789abcdef";
        const string Hash = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
        static string Fixture(long seq = 1, long sim = 100, long received = 1000000, bool reverse = false)
        {
            var assets = Enumerable.Range(0, 4).Select(i => new {
                asset_id = SnapshotGate.AssetIds[i],
                joint_names = (reverse ? SnapshotGate.JointNames[i].Reverse() : SnapshotGate.JointNames[i]).ToArray(),
                joint_positions_rad = (reverse ? Enumerable.Range(0, SnapshotGate.JointNames[i].Length).Reverse()
                    : Enumerable.Range(0, SnapshotGate.JointNames[i].Length)).Select(j => (j+1)*0.1).ToArray(),
                position_m = new[] { 0.2+i*0.1, 0.5, 0.9 }, rotation_xyzw = new[] { 0.0, 0.0, 0.0, 1.0 }
            }).ToArray();
            return JsonSerializer.Serialize(new { schema_version = 2, runtime_kind = "isaac-sim-executor", profile_id = "scenario1",
                scene_sha256 = Hash, session_id = Session, epoch = 0, sequence = seq, sim_time_ns = sim, received_at_unix_ms = received,
                physical_output_connected = false, frame_id = "cell", phase = "omy_pre", plate_owner = "carrier",
                assets = reverse ? assets.Reverse().ToArray() : assets });
        }

        static SnapshotGate Gate() => new SnapshotGate(Session, 0, Hash);
        static bool Accept(SnapshotGate gate, string json, long now = 1000000, double monotonic = 1)
            => gate.TryAccept(Encoding.UTF8.GetBytes(json), now, monotonic);

        public static void Run(List<string> checks)
        {
            void Check(string name, bool passed)
            {
                if (!passed) throw new BuildFailedException("Test failed: " + name);
                checks.Add(name);
            }
            var valid = Fixture();
            var gate = Gate();
            Check("valid four-asset snapshot", Accept(gate, valid));
            Check("duplicate sequence frozen", !Accept(gate, valid));
            Check("reordered sequence frozen", !Accept(gate, Fixture(0, 200)));
            Check("simulation clock rollback frozen", !Accept(gate, Fixture(2, 50)));
            Check("simulation paused frozen", !Accept(gate, Fixture(2, 100)));
            Check("watermarks unchanged by rejection", gate.Last.Sequence == 1 && gate.Accepted == 1);
            Check("recovery on fresh advancing sample", Accept(gate, Fixture(2, 200), monotonic: 2));
            Check("monotonic timeout", !gate.IsFresh(1000000, 503));
            Check("wall clock timeout", !gate.IsFresh(1000501, 3));
            Check("clock rollback rejected", !gate.IsFresh(999899, 3));
            Check("500ms boundary", Accept(Gate(), Fixture(received: 999500)));
            Check("100ms future boundary", Accept(Gate(), Fixture(received: 1000100)));
            Check("zero sequence and sim time accepted", Accept(Gate(), Fixture(0, 0)));
            gate = Gate(); Accept(gate, valid);
            Check("receive clock regression", !Accept(gate, Fixture(2, 200, 999999)));
            var invalid = new Dictionary<string, string> {
                ["truncated JSON"] = valid.Substring(0, valid.Length-1),
                ["trailing JSON"] = valid + "{}",
                ["duplicate top field"] = valid.Replace("\"schema_version\":2", "\"schema_version\":2,\"schema_version\":2"),
                ["unknown field"] = valid.Replace("\"schema_version\":2", "\"schema_version\":2,\"command\":0"),
                ["schema mismatch"] = valid.Replace("\"schema_version\":2", "\"schema_version\":1"),
                ["wrong runtime"] = valid.Replace("isaac-sim-executor", "unity-executor"),
                ["wrong profile"] = valid.Replace("\"profile_id\":\"scenario1\"", "\"profile_id\":\"rb5\""),
                ["wrong frame"] = valid.Replace("\"cell\"", "\"world\""),
                ["untrusted scene digest"] = valid.Replace(Hash, new string('a', 64)),
                ["session reset"] = valid.Replace(Session + "\"", new string('a', 32) + "\""),
                ["epoch reset"] = valid.Replace("\"epoch\":0", "\"epoch\":1"),
                ["fractional epoch"] = valid.Replace("\"epoch\":0", "\"epoch\":0.5"),
                ["negative epoch"] = valid.Replace("\"epoch\":0", "\"epoch\":-1"),
                ["boolean epoch"] = valid.Replace("\"epoch\":0", "\"epoch\":false"),
                ["fractional sequence"] = valid.Replace("\"sequence\":1", "\"sequence\":1.5"),
                ["overflow sequence"] = valid.Replace("\"sequence\":1", "\"sequence\":9223372036854775808"),
                ["quoted sequence"] = valid.Replace("\"sequence\":1", "\"sequence\":\"1\""),
                ["physical connected"] = valid.Replace("\"physical_output_connected\":false", "\"physical_output_connected\":true"),
                ["physical field absent"] = valid.Replace("\"physical_output_connected\":false,", ""),
                ["invalid plate owner"] = valid.Replace("\"plate_owner\":\"carrier\"", "\"plate_owner\":\"robot\""),
                ["wrong asset"] = valid.Replace("isaac-scenario1-plate", "physical-plate"),
                ["duplicate asset"] = valid.Replace("isaac-scenario1-plate", "isaac-scenario1-carrier"),
                ["duplicate asset field"] = valid.Replace("\"position_m\":", "\"joint_names\":[],\"position_m\":"),
                ["unknown asset field"] = valid.Replace("\"position_m\":", "\"command\":0,\"position_m\":"),
                ["duplicate OMY joint"] = valid.Replace("\"joint6\"", "\"joint1\""),
                ["missing OMY gripper"] = valid.Replace(",\"rh_r1_joint\"", ""),
                ["unknown RB5 joint"] = valid.Replace("\"wrist3\"", "\"joint6\""),
                ["nonfinite joint"] = Regex.Replace(valid, "(?<=\"joint_positions_rad\":\\[)[^,\\]]+", "1e999"),
                ["NaN joint"] = Regex.Replace(valid, "(?<=\"joint_positions_rad\":\\[)[^,\\]]+", "NaN"),
                ["wrong position length"] = Regex.Replace(valid, "\"position_m\":\\[[^\\]]+\\]", "\"position_m\":[0.2,0.5]"),
                ["nonfinite position"] = Regex.Replace(valid, "\"position_m\":\\[[^\\]]+\\]", "\"position_m\":[1e999,0.5,0.9]"),
                ["null rotation"] = valid.Replace("\"rotation_xyzw\":[0,0,0,1]", "\"rotation_xyzw\":null"),
                ["zero quaternion"] = valid.Replace("[0,0,0,1]", "[0,0,0,0]"),
                ["unnormalized quaternion"] = valid.Replace("[0,0,0,1]", "[0,0,0,2]"),
                ["stale receive"] = Fixture(received: 999499),
                ["future receive"] = Fixture(received: 1000101)
            };
            foreach (var item in invalid)
            {
                Check("fixture differs: " + item.Key, item.Value != valid);
                Check(item.Key, !Accept(Gate(), item.Value));
            }
            Check("oversized data", !Accept(Gate(), new string(' ', SnapshotGate.MaximumBytes+1)));
            gate = Gate(); Check("asset and joint reordering accepted", Accept(gate, Fixture(reverse: true)));
            Check("name-based OMY values", Math.Abs(gate.Last.Assets[0].Joints[6]-0.7) < 1e-12);
            Check("name-based RB5 values", Math.Abs(gate.Last.Assets[1].Joints[0]-0.1) < 1e-12);
            Check("ROS position conversion", Scenario1Mirror.RosPosition(new[] { 1.0, 2.0, 3.0 }) == new Vector3(-2, 3, 1));
            Check("ROS positive yaw rotates Unity forward toward negative X", Vector3.Distance(
                Scenario1Mirror.RosRotation(new[] { 0.0, 0.0, Math.Sqrt(0.5), Math.Sqrt(0.5) }) * Vector3.forward, Vector3.left) < 0.000001);
            var rotation = new[] { 0.2, -0.3, 0.4, Math.Sqrt(0.71) };
            Check("ROS quaternion roundtrip", Scenario1Mirror.ToRosRotation(Scenario1Mirror.RosRotation(rotation)).Zip(rotation, (a,b) => Math.Abs(a-b)).Max() < 0.000001);
            var mirror = Object.FindAnyObjectByType<Scenario1Mirror>();
            var roots = mirror.bindings.Select(b => (b.root.position, b.root.rotation)).ToArray();
            mirror.ApplyMeasured(gate.Last); mirror.Measure(gate.Last);
            foreach (var item in mirror.Readback)
                Check("world/joint readback: " + item.asset_id, item.position_error_m < 0.000001 && item.rotation_error_rad < 0.000001 && item.max_abs_joint_error_rad < 0.000001);
            var plateBefore = mirror.bindings[3].root.position;
            mirror.bindings[2].root.position += Vector3.right;
            Check("carrier cannot autonomously drag plate", mirror.bindings[3].root.position == plateBefore);
            mirror.Measure(gate.Last);
            Check("world translation perturbation detected", mirror.Readback[2].position_error_m > 0.99);
            mirror.bindings[0].root.rotation *= Quaternion.AngleAxis(1, Vector3.up);
            mirror.bindings[1].joints[1].localRotation *= Quaternion.AngleAxis(1, Vector3.forward);
            mirror.bindings[0].joints[7].localRotation *= Quaternion.AngleAxis(1, Vector3.up);
            mirror.Measure(gate.Last);
            Check("world rotation perturbation detected", mirror.Readback[0].rotation_error_rad > 0.017);
            Check("off-axis joint perturbation detected", mirror.Readback[1].max_abs_joint_error_rad > 0.017);
            Check("mimic perturbation detected", mirror.Readback[0].visual_joint_error_rad[7] > 0.017);
            for (var i = 0; i < 4; i++)
            {
                var b = mirror.bindings[i]; b.root.SetPositionAndRotation(roots[i].position, roots[i].rotation);
                for (var j = 0; j < b.joints.Length; j++) b.joints[j].localRotation = b.zeroRotations[j];
            }
            EditorSceneManager.SaveScene(EditorSceneManager.GetActiveScene());
        }
    }
}
