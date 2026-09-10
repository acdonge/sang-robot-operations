"""Pinned recorded paths, not an IK solver or arbitrary action executor."""

from bisect import bisect_right
import json
import math

from .contract import HERE, STEPS, load_manifest, unity_position_to_ros


class MotionProgram:
    def __init__(self):
        self.manifest, self.scene_sha256 = load_manifest()
        data = json.loads((HERE / "motion.json").read_text())
        self.curves = data["steps"]
        if tuple(self.curves) != STEPS:
            raise ValueError("Unexpected Scenario 1 sequence")
        self.home = unity_position_to_ros([-2.5, 0., -.818])
        self.dock = unity_position_to_ros([-1.25, 0., -.818])
        for step, curve in self.curves.items():
            points = curve["points"]
            if len(points) < 2 or points[0]["t"] != 0 or not 0 < points[-1]["t"] <= 390:
                raise ValueError(f"Invalid recorded duration: {step}")
            previous = None
            for point in points:
                if not self.valid_measurement(point["positions"]):
                    raise ValueError(f"Recorded pose outside simulation envelope: {step}")
                if previous:
                    dt = point["t"]-previous["t"]
                    if dt <= 0 or max(abs(a-b)/dt for a,b in zip(point["positions"][:13], previous["positions"][:13])) > .61:
                        raise ValueError(f"Recorded path time/velocity invalid: {step}")
                previous = point
            curve["times"] = [p["t"] for p in points]

    def valid_measurement(self, values):
        return (len(values) == 16 and all(math.isfinite(v) for v in values)
                and all(abs(v) <= 3.16 for v in values[:13])
                and -.03 <= values[6] <= 1.14
                and abs(values[13]+.818) <= .05 and 1.2 <= values[14] <= 2.55
                and abs(values[15]) <= .03)

    def duration(self, step):
        return self.curves[step]["points"][-1]["t"]

    def start(self, step):
        return list(self.curves[step]["points"][0]["positions"])

    def end(self, step):
        return list(self.curves[step]["points"][-1]["positions"])

    def sample(self, step, elapsed):
        curve = self.curves[step]
        index = min(max(0, bisect_right(curve["times"], elapsed)-1), len(curve["points"])-2)
        a, b = curve["points"][index:index+2]
        f = min(1., max(0., (elapsed-a["t"])/(b["t"]-a["t"])))
        return [x+(y-x)*f for x,y in zip(a["positions"],b["positions"])]

    def phase(self, step, elapsed):
        phases = self.curves[step]["phases"]
        return next(p["name"] for p in reversed(phases) if elapsed >= p["t"])
