"""Extract vendor joint frames and finger geometry for offline retargeting."""

import json
from pathlib import Path


def main():
    from pxr import Usd, UsdGeom, UsdPhysics
    here = Path(__file__).resolve().parent
    source = here.parents[2] / "issacsim_setting_usd/OMY_ai3m/usd/OMY.usd"
    stage = Usd.Stage.Open(str(source))
    joints = []
    def quaternion(value):
        return [*map(float, value.GetImaginary()), float(value.GetReal())]
    for prim in stage.Traverse():
        if prim.GetTypeName() != "PhysicsRevoluteJoint":
            continue
        joint = UsdPhysics.Joint(prim)
        joints.append(dict(name=prim.GetName(), body0=str(joint.GetBody0Rel().GetTargets()[0]),
            body1=str(joint.GetBody1Rel().GetTargets()[0]),
            p0=list(joint.GetLocalPos0Attr().Get()), q0=quaternion(joint.GetLocalRot0Attr().Get()),
            p1=list(joint.GetLocalPos1Attr().Get()), q1=quaternion(joint.GetLocalRot1Attr().Get()),
            axis=prim.GetAttribute("physics:axis").Get()))
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
    fingers = []
    for name in ("l1", "l2", "r1", "r2"):
        path = f"/World/OMY/gripper/rh_p12_rn_{name}"
        bounds = cache.ComputeRelativeBound(stage.GetPrimAtPath(path+"/visuals"),stage.GetPrimAtPath(path)).ComputeAlignedRange()
        fingers.append(dict(body=path, center=list(bounds.GetMidpoint()), minimum=list(bounds.GetMin()), maximum=list(bounds.GetMax())))
    (here / "omy_model.json").write_text(json.dumps(dict(joints=joints, fingers=fingers), indent=2))


if __name__ == "__main__":
    main()
