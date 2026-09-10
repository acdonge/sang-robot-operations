"""Compose vendor articulations in the source cell frame; original USD stays untouched."""

import json
import numpy as np
from scipy.spatial.transform import Rotation

from .contract import HERE, JOINTS, unity_position_to_ros, unity_rotation_to_ros


class ScenarioScene:
    def __init__(self, stage):
        from pxr import Gf, UsdGeom, UsdLux, UsdPhysics
        self.stage=stage
        self.layout=json.loads((HERE/"layout.json").read_text())
        frames={f["path"]:f for f in self.layout["frames"]}
        self.bases={}
        assets=HERE.parents[2]/"issacsim_setting_usd"
        for name,source,prim,frame in (
            ("omy",assets/"OMY_ai3m/usd/OMY.usd","/World/OMY","omy_f3m"),
            ("rb5",assets/"RB5_850E/assets/rb5_850e.usd","/rb5_850e","rb5_850e")):
            path=f"/World/{name}"
            root=UsdGeom.Xform.Define(stage,path).GetPrim()
            root.GetReferences().AddReference(str(source),prim)
            p=frames[frame]["position_ros_world_m"]
            q=unity_rotation_to_ros(frames[frame]["rotation_unity_world_xyzw"])
            q=(np.asarray(q)/np.linalg.norm(q)).tolist()
            self.bases[name]=(p,q)
            xform=UsdGeom.Xformable(root);xform.ClearXformOpOrder()
            xform.AddTranslateOp().Set(Gf.Vec3d(*p))
            double = str(root.GetAttribute("xformOp:orient").GetTypeName()) == "quatd"
            precision = UsdGeom.XformOp.PrecisionDouble if double else UsdGeom.XformOp.PrecisionFloat
            quaternion = Gf.Quatd(q[3], Gf.Vec3d(*q[:3])) if double else Gf.Quatf(q[3], Gf.Vec3f(*q[:3]))
            xform.AddOrientOp(precision).Set(quaternion)
            fixed=UsdPhysics.FixedJoint(stage.GetPrimAtPath(path+"/root_joint"))
            fixed.GetLocalPos0Attr().Set(Gf.Vec3f(*p))
            fixed.GetLocalRot0Attr().Set(Gf.Quatf(q[3],Gf.Vec3f(*q[:3])))
            # Acceleration drives are a declared virtual tracking model, not
            # calibrated motor/gearbox dynamics or manufacturer safety limits.
            for child in list(UsdGeom.Xformable(root).GetPrim().GetChildren()):
                if child.GetName()=="joints":
                    for joint in child.GetChildren():
                        if joint.GetTypeName()!="PhysicsRevoluteJoint":continue
                        drive=UsdPhysics.DriveAPI.Apply(joint,"angular")
                        drive.CreateTypeAttr("acceleration")
                        drive.CreateStiffnessAttr(10000.)
                        drive.CreateDampingAttr(200.)
                        drive.CreateMaxForceAttr(10000.)
            for child in stage.Traverse():
                if str(child.GetPath()).startswith(path) and child.GetTypeName() in ("OmniGraph","OmniGraphNode"):
                    child.SetActive(False)
        self.cube("floor",[0,1.3,-.03],[4,4,.06],[.15,.17,.19])
        # Top surfaces match the exported source renderer bounds. Legs/slab
        # thickness remain explicit proxies, not a qualified collision mesh.
        for name, x, y, width, depth, top in (
            ("omy_table", -.262393534, 1.14899993, .610, 1.010, .905),
            ("rb5_table", -.143999994, .323606402, 1.010, .610, .904),
        ):
            self.cube(name+"_top", [x,y,top-.02], [width,depth,.04], [.50,.55,.60])
            for i, dx in enumerate((-.5,.5)):
                for j, dy in enumerate((-.5,.5)):
                    self.cube(f"{name}_leg_{i}_{j}", [x+dx*(width-.08),y+dy*(depth-.08),(top-.04)/2],
                              [.04,.04,top-.04], [.35,.4,.45])
        # The source task also uses a kinematic plate attachment. Proxy slabs
        # and convex finger hulls are not a validated grasp-contact pair. Keep
        # arm/environment contacts, but explicitly exclude this fixture pair.
        for finger in ("l1", "l2", "r1", "r2"):
            body = stage.GetPrimAtPath(f"/World/omy/gripper/rh_p12_rn_{finger}")
            pairs = UsdPhysics.FilteredPairsAPI.Apply(body)
            pairs.CreateFilteredPairsRel().SetTargets(["/World/omy_table_top", "/World/rb5_table_top"])
        UsdGeom.Xform.Define(stage,"/World/carrier")
        self.cube("carrier/base",[0,0,.15],[.46,.58,.3],[.08,.15,.2],collision=False)
        self.cube("carrier/tray",[0,0,.885],[.4,.5,.04],[.18,.23,.28],collision=False)
        for x in (-.15,.15):
            for y in (-.2,.2):
                self.cube(f"carrier/post_{x}_{y}".replace("-","n").replace(".","p"),[x,y,.58],[.035,.035,.58],[.4,.45,.5],collision=False)
        self.cube("plate",[-.818,2.5,.91],[.07,.07,.01],[.95,.55,.12],collision=False)
        light=UsdLux.DomeLight.Define(stage,"/World/light");light.CreateIntensityAttr(1500.)
        self.carrier_position=np.array([-.818,2.5,0.])
        self.plate_position=self.carrier_position+np.array([0,0,.91])
        self.owner="carrier";self.plate_offset=np.array([0,0,.91]);self.grasp_offset=None
        self.events=[];self.last_phase=None;self.tracking_error=0.;self.max_tracking_error=0.
        self.tcp_checks=[]
        self.set_position("carrier",self.carrier_position)

    def cube(self,name,position,size,color,collision=True):
        from pxr import Gf,UsdGeom,UsdPhysics
        cube=UsdGeom.Cube.Define(self.stage,"/World/"+name);cube.CreateSizeAttr(1.)
        cube.AddTranslateOp().Set(Gf.Vec3d(*position));cube.AddScaleOp().Set(Gf.Vec3f(*size))
        cube.CreateDisplayColorAttr([Gf.Vec3f(*color)])
        if collision:UsdPhysics.CollisionAPI.Apply(cube.GetPrim())

    def set_position(self,name,position):
        from pxr import Gf,UsdGeom
        x=UsdGeom.Xformable(self.stage.GetPrimAtPath("/World/"+name))
        translate=next((op for op in x.GetOrderedXformOps() if op.GetOpType()==UsdGeom.XformOp.TypeTranslate),None)
        if translate is None:translate=x.AddTranslateOp()
        translate.Set(Gf.Vec3d(*[float(v) for v in position]))

    def initialize(self):
        from isaacsim.core.prims import Articulation,RigidPrim
        from pxr import UsdGeom,Usd
        self.robots={};self.indices={};self.all_indices={}
        for name in ("omy","rb5"):
            robot=Articulation("/World/"+name, reset_xform_properties=False);robot.initialize()
            self.robots[name]=robot
            self.indices[name]=[robot.get_dof_index(j) for j in JOINTS[name]]
            self.all_indices[name]=robot.dof_names
        self.fingers=[f"/World/omy/gripper/rh_p12_rn_{n}" for n in ("l1","l2","r1","r2")]
        self.bodies=RigidPrim(self.fingers+["/World/rb5/link6", "/World/omy/world", "/World/rb5/link0"],
                              reset_xform_properties=False)
        self.bodies.initialize()
        cache=UsdGeom.BBoxCache(Usd.TimeCode.Default(),[UsdGeom.Tokens.default_])
        self.finger_centers=[]
        for path in self.fingers:
            body=self.stage.GetPrimAtPath(path)
            bounds=cache.ComputeRelativeBound(self.stage.GetPrimAtPath(path+"/visuals"),body).ComputeAlignedRange()
            self.finger_centers.append(np.array(bounds.GetMidpoint()))

    def set_targets(self,vector,initialize=False):
        for name,values in (("omy",vector[:7]),("rb5",vector[7:13])):
            robot=self.robots[name]
            indices=list(self.indices[name]);targets=list(values)
            if name=="omy":
                for joint in ("rh_l1","rh_l2","rh_r2"):
                    if joint in self.all_indices[name]:
                        indices.append(robot.get_dof_index(joint));targets.append(values[6])
            target=np.asarray([targets],dtype=np.float32)
            if initialize:
                robot.set_joint_positions(target,joint_indices=indices)
                robot.set_joint_velocities(np.zeros_like(target),joint_indices=indices)
            robot.set_joint_position_targets(target,joint_indices=indices)
        self.carrier_position=np.asarray(vector[13:16],dtype=float)
        self.set_position("carrier",self.carrier_position)

    def measured(self):
        values=[]
        for name in ("omy","rb5"):
            values.extend(float(x) for x in np.asarray(self.robots[name].get_joint_positions())[0,self.indices[name]])
        # Carrier is an explicit kinematic fixture. Read its applied scene pose,
        # never label these three axes measured motor or mobile-controller data.
        from pxr import UsdGeom,Usd
        pose=UsdGeom.Xformable(self.stage.GetPrimAtPath("/World/carrier")).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        values.extend(float(x) for x in pose.ExtractTranslation())
        return values

    def tool_positions(self):
        positions,quaternions=self.bodies.get_world_poses()
        if not np.isfinite(positions).all() or not np.isfinite(quaternions).all():
            raise RuntimeError("Non-finite measured rigid-body pose")
        centers=[]
        for p,q,local in zip(positions[:4],quaternions[:4],self.finger_centers):
            centers.append(np.asarray(p)+Rotation.from_quat([*q[1:],q[0]]).apply(local))
        q=quaternions[4]
        tcp=positions[4]+Rotation.from_quat([*q[1:],q[0]]).apply([0,-.0967,0])
        return np.mean(centers,axis=0),tcp

    def check_path_point(self, program, elapsed):
        checks = program.curves["rb5_waam"]["tcp_checks"]
        if len(self.tcp_checks) >= len(checks) or elapsed < checks[len(self.tcp_checks)]["t"]:
            return
        _, tcp = self.tool_positions()
        target = checks[len(self.tcp_checks)]["target_m"]
        error = float(np.linalg.norm(tcp-target))
        _, rotations = self.bodies.get_world_poses()
        rotation = rotations[4]
        direction = Rotation.from_quat([*rotation[1:], rotation[0]]).apply([0,-1,0])
        angle = float(np.degrees(np.arccos(np.clip(direction @ [0,0,-1], -1., 1.))))
        if not np.isfinite(error) or not np.isfinite(angle) or error > .012 or angle > 5.:
            raise RuntimeError(f"Measured RB5 path point {len(self.tcp_checks)} missed: {error:.5f}m/{angle:.3f}deg")
        self.tcp_checks.append({"index":len(self.tcp_checks), "error_m":error, "tool_angle_deg":angle})

    def update_fixture(self,phase,measured,*,allow_transitions=False):
        grip,_=self.tool_positions()
        if self.owner=="carrier":self.plate_position=self.carrier_position+self.plate_offset
        elif self.owner=="omy":self.plate_position=grip+self.grasp_offset
        if not np.isfinite(self.plate_position).all() or not np.isfinite(measured).all():
            raise RuntimeError("Non-finite fixture position or measured joints")
        if allow_transitions and phase in ("pre_transfer_continuous","post_transfer_continuous") and self.last_phase!=phase:
            required="carrier" if phase.startswith("pre") else "table"
            error=float(np.linalg.norm(grip-self.plate_position))
            if self.owner!=required or measured[6]<.15 or not np.isfinite(error) or error>.05:
                raise RuntimeError(f"Grasp fixture blocked: owner={self.owner}, gripper={measured[6]:.4f}, measured distance={error:.4f}m")
            self.grasp_offset=self.plate_position-grip;self.owner="omy"
            self.events.append({"phase":phase,"event":"attach","measured_distance_m":error})
        if allow_transitions and phase in ("pre_return_home","post_return_home") and self.last_phase!=phase:
            target=(np.array([-.24,.819,.91]) if phase.startswith("pre") else self.carrier_position+np.array([0,0,.91]))
            error=float(np.linalg.norm(self.plate_position-target))
            if self.owner!="omy" or measured[6]>.04 or not np.isfinite(error) or error>.04:
                raise RuntimeError(f"Release fixture blocked: owner={self.owner}, gripper={measured[6]:.4f}, measured position error={error:.4f}m")
            self.owner="table" if phase.startswith("pre") else "carrier"
            if self.owner=="carrier":self.plate_offset=self.plate_position-self.carrier_position
            self.events.append({"phase":phase,"event":"release","measured_position_error_m":error})
        if allow_transitions:
            self.last_phase=phase
        self.set_position("plate",self.plate_position)

    def world_ready(self,step,active):
        if active:
            expected={"mobile_delivery":"carrier","omy_pre_pick_place":"table","rb5_waam":"table",
                      "omy_post_pick_place":"carrier","mobile_return":"carrier"}[step]
        else:
            expected={"mobile_delivery":"carrier","omy_pre_pick_place":"carrier","rb5_waam":"table",
                      "omy_post_pick_place":"table","mobile_return":"carrier"}[step]
        target=np.array([-.24,.819,.91]) if expected=="table" else self.carrier_position+np.array([0,0,.91])
        ready=self.owner==expected and np.linalg.norm(self.plate_position-target)<=.04
        if active and step == "rb5_waam":
            ready = ready and len(self.tcp_checks) == 25
        return bool(ready),f"plate owner={self.owner}; expected={expected}"

    def snapshot_assets(self,measured):
        result=[]
        positions, rotations = self.bodies.get_world_poses()
        for name,q in (("omy",measured[:7]),("rb5",measured[7:13]),("carrier",[]),("plate",[])):
            if name in self.bases:
                index = 5 if name == "omy" else 6
                position = positions[index]
                orientation = rotations[index]
                rotation = [*orientation[1:], orientation[0]]
            else:position,rotation=(measured[13:16] if name=="carrier" else self.plate_position),[0.,0.,0.,1.]
            result.append({"asset_id":"isaac-scenario1-"+name,"joint_names":list(JOINTS[name]),
                "joint_positions_rad":list(q),"position_m":[float(x) for x in position],"rotation_xyzw":[float(x) for x in rotation]})
        return result
