"""Build a reproducible recorded-path fixture; never executes motion."""

import json
import math
import os
from pathlib import Path

from .contract import HERE, STEPS, sha256, unity_position_to_ros
from .rb5_path import retarget
from .omy_path import retarget as retarget_omy

SOURCE = HERE.parents[2] / "unity_IITP/unity_IITP/100.Whole_integration"
ASSETS = HERE.parents[2] / "issacsim_setting_usd"


def main():
    layout = HERE / "layout.json"
    layout_data = json.loads(layout.read_text())
    sources = []
    for row in layout_data["source_hashes"]:
        path = SOURCE / "Scenario1_app" / row["path"]
        if sha256(path) != row["sha256"]:
            raise ValueError(f"Frozen Unity source changed: {row['path']}")
        sources.append({"path":os.path.relpath(path,HERE),"sha256":row["sha256"]})
    def read(path):
        sources.append({"path": os.path.relpath(path, HERE), "sha256": sha256(path)})
        return json.loads(path.read_text())
    curves = {}
    home = unity_position_to_ros([-2.5, 0., -.818])
    dock = unity_position_to_ros([-1.25, 0., -.818])
    zero = [0.]*13
    for step, start, end in ((STEPS[0],home,dock),(STEPS[-1],dock,home)):
        points = [{"t":i/30.,"positions":zero+[a+(b-a)*(i/150.)**2*(3-2*i/150.) for a,b in zip(start,end)]}
                  for i in range(151)]
        curves[step] = {"points":points,"phases":[{"t":0.,"name":step}]}
    for prefix, step in (("pre",STEPS[1]),("post",STEPS[3])):
        points, phases, time = [], [], 0.
        for suffix in ("open_gripper","approach_retimed","grasp","transfer_continuous","release","return_home"):
            name = f"{prefix}_{suffix}"
            path = SOURCE / "omy-main/trajectories" / f"{name}.json"
            data = read(path)
            root = data["calibration"]["omy_root"]
            frame = next(f for f in layout_data["frames"] if f["path"] == "omy_f3m")
            if max(abs(a-b) for a,b in zip(root["position_m"],frame["position_unity_world_m"])) > .0001:
                raise ValueError("OMY recorded root calibration differs from the cell")
            if max(abs(a-b) for a,b in zip(root["rotation_xyzw"],frame["rotation_unity_world_xyzw"])) > .0001:
                raise ValueError("OMY recorded root rotation differs from the cell")
            if data["names"] != [f"joint{i}" for i in range(1,7)]+["rh_r1_joint"]:
                raise ValueError("OMY joint mapping changed")
            phases.append({"t":time,"name":name})
            if points and max(abs(a-b) for a,b in zip(points[-1]["positions"][:7],data["points"][0]["positions"])) > .01:
                raise ValueError("OMY segment seam is discontinuous")
            for p in data["points"]:
                if points and p["t"] == 0:
                    continue
                points.append({"t":time+p["t"],"positions":p["positions"]+[0.]*6+dock})
            time += data["points"][-1]["t"]
        curves[step] = {"points":points,"phases":phases}
        retarget_omy(curves[step], layout_data, prefix)
    rb = read(SOURCE / "Scenario1_app/Assets/StreamingAssets/scenario1/rb5_waam_trajectory.json")
    if rb["joint_names"] != [f"link{i}" for i in range(1,7)] or rb["home_unity_degrees"] != [0.]*6:
        raise ValueError("RB5 source joint/home contract changed")
    points = [{"t":0., "positions":zero+dock}]
    phases = []
    def append(positions, duration, phase):
        start = points[-1]
        phases.append({"t":start["t"],"name":phase})
        goal = [math.radians(v) for v in positions]
        # Float-export overshoot at the vendor 3.14-rad limit is normalized only
        # within 1e-6 rad; larger changes are rejected rather than silently clipped.
        if max(abs(v) for v in goal) > 3.140001:
            raise ValueError("RB5 source exceeds vendor joint limits")
        goal = [min(3.14,max(-3.14,v)) for v in goal]
        before = start["positions"][7:13]
        duration = max(duration,1.5*max(abs(a-b) for a,b in zip(before,goal))/.21,.1)
        count = math.ceil(duration*30)
        for i in range(1,count+1):
            f=i/count; blend=f*f*(3-2*f)
            points.append({"t":start["t"]+duration*f,"positions":[0.]*7+[a+(b-a)*blend for a,b in zip(before,goal)]+dock})
    path = retarget(rb, layout_data)
    append([math.degrees(v) for v in path[0]["positions_rad"]], 5., "rb5_approach")
    checks = [{"t": points[-1]["t"], "target_m": path[0]["target_m"]}]
    for p in path[1:]:
        append([math.degrees(v) for v in p["positions_rad"]],p["duration_s"],"rb5_path")
        checks.append({"t":points[-1]["t"],"target_m":p["target_m"]})
    append(rb["home_unity_degrees"],5.,"rb5_return")
    curves[STEPS[2]]={"points":points,"phases":phases,"tcp_checks":checks,"retarget_evidence":path}
    if not layout.exists():
        raise ValueError("Source scene layout export is required before preparation")
    for path in (layout,ASSETS/"OMY_ai3m/usd/OMY.usd",ASSETS/"RB5_850E/assets/rb5_850e.usd",
                 Path(__file__),HERE/"scene.py",HERE/"motion.py",HERE/"rb5_path.py",
                 HERE/"omy_path.py",HERE/"export_omy_model.py",HERE/"omy_model.json",
                 HERE/"runner.py",HERE/"executor.py",HERE.parent/"isaac_twin/executor.py"):
        sources.append({"path":os.path.relpath(path,HERE),"sha256":sha256(path)})
    (HERE/"motion.json").write_text(json.dumps({"schema_version":1,"steps":{s:curves[s] for s in STEPS}}, separators=(",",":")))
    sources.append({"path":"motion.json","sha256":sha256(HERE/"motion.json")})
    (HERE/"manifest.json").write_text(json.dumps({"schema_version":1,"sources":sources,
        "physical_output_connected":False,"carrier_dynamics":"kinematic_fixture",
        "grasp_model":"measured_tool_attachment_fixture",
        "gripper_table_contacts":"excluded_for_kinematic_fixture", "frame":"cell_ros"},indent=2))
    print(json.dumps({"scene_sha256":sha256(HERE/"manifest.json"),"durations":{s:curves[s]["points"][-1]["t"] for s in STEPS}},indent=2))


if __name__ == "__main__":
    main()
