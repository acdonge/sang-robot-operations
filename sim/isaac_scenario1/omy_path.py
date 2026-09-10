"""Source-path endpoint corrections for the virtual gripper attachment model."""

import json
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

from .contract import HERE, unity_rotation_to_ros


class OmyGeometry:
    def __init__(self, layout):
        self.model = json.loads((HERE/"omy_model.json").read_text())
        frame = next(f for f in layout["frames"] if f["path"] == "omy_f3m")
        self.base = (np.asarray(frame["position_ros_world_m"]), Rotation.from_quat(unity_rotation_to_ros(frame["rotation_unity_world_xyzw"])))

    def forward(self, q):
        poses = {"/World/OMY/world": self.base}
        for index, joint in enumerate(self.model["joints"]):
            position, rotation = poses[joint["body0"]]
            angle = q[index] if index < 6 else q[6]
            axis = np.eye(3)["XYZ".index(joint["axis"])]
            r = rotation * Rotation.from_quat(joint["q0"]) * Rotation.from_rotvec(axis*angle) * Rotation.from_quat(joint["q1"]).inv()
            p = position + rotation.apply(joint["p0"]) - r.apply(joint["p1"])
            poses[joint["body1"]] = (p, r)
        centers = [poses[f["body"]][0]+poses[f["body"]][1].apply(f["center"]) for f in self.model["fingers"]]
        return np.mean(centers, axis=0)

    def solve(self, source, target):
        source = np.asarray(source)
        def residual(arm):
            return np.r_[10*(self.forward(np.r_[arm, source[6]])-target), .001*(arm-source[:6])]
        fit = least_squares(residual, source[:6], bounds=(-3.14, 3.14), max_nfev=300,
                            xtol=1e-11, ftol=1e-11, gtol=1e-11)
        result = np.r_[fit.x, source[6]]
        error = float(np.linalg.norm(self.forward(result)-target))
        if not fit.success or error > .001 or max(abs(result-source)) > .25:
            raise ValueError(f"OMY endpoint retarget rejected: {error:.6f}m")
        return result-source


def retarget(curve, layout, prefix):
    geometry = OmyGeometry(layout)
    phases = {p["name"].removeprefix(prefix+"_"): p["t"] for p in curve["phases"]}
    points = curve["points"]
    def at(t):
        return min(points, key=lambda p: abs(p["t"]-t))["positions"][:7]
    pick = np.array([-.818, 1.25, .91]) if prefix == "pre" else np.array([-.24, .819, .91])
    place = np.array([-.24, .819, .91]) if prefix == "pre" else np.array([-.818, 1.25, .91])
    # Flat-plate fixture uses a fixed 40 mm own-visual grip-center offset.
    # This is not a calibrated physical grasp/contact constraint.
    offset = np.array([0., 0., .04])
    grasp = geometry.solve(at(phases["transfer_continuous"]), pick+offset)
    release = geometry.solve(at(phases["return_home"]), place+offset)
    def smooth(t, start, end):
        f = np.clip((t-start)/(end-start), 0., 1.)
        return f*f*(3-2*f)
    for point in points:
        t = point["t"]
        if t < phases["transfer_continuous"]:
            correction = grasp*smooth(t, phases["approach_retimed"], phases["grasp"])
        elif t < phases["return_home"]:
            blend = smooth(t, phases["transfer_continuous"], phases["release"])
            correction = grasp*(1-blend)+release*blend
        else:
            correction = release*(1-smooth(t, phases["return_home"], points[-1]["t"]))
        point["positions"][:7] = (np.asarray(point["positions"][:7])+correction).tolist()
    curve["endpoint_retarget"] = dict(grasp_delta_rad=grasp.tolist(), release_delta_rad=release.tolist(),
        grip_center_offset_m=offset.tolist(), model="virtual_visual_attachment_not_contact_grasp")
