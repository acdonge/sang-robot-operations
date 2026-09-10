"""Receive the isolated ROS cell snapshot; never access an actuator or command file."""

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from .contract import CAMERA_TOPIC, JOINTS, ROOT, STATE_TOPIC, atomic_json, load_manifest, require_isolation

FIELDS = {"schema_version","runtime_kind","profile_id","scene_sha256","session_id","epoch","sequence",
          "sim_time_ns","source_unix_ms","physical_output_connected","frame_id","phase","plate_owner","assets"}
ASSET_FIELDS = {"asset_id","joint_names","joint_positions_rad","position_m","rotation_xyzw"}


def _hex(value, length):
    return (isinstance(value,str) and len(value)==length
            and all(c in "0123456789abcdef" for c in value))


def read_message(raw):
    if not isinstance(raw,str) or len(raw.encode("utf-8"))>65536:
        raise ValueError("Snapshot too large or not text")
    def unique_fields(pairs):
        data={}
        for key,value in pairs:
            if key in data:raise ValueError("Duplicate JSON field")
            data[key]=value
        return data
    def invalid_constant(value):
        raise ValueError("Non-finite JSON number")
    def finite_float(value):
        number=float(value)
        if not math.isfinite(number):raise ValueError("Non-finite JSON number")
        return number
    try:
        return json.loads(raw,object_pairs_hook=unique_fields,parse_constant=invalid_constant,parse_float=finite_float)
    except RecursionError as exc:
        raise ValueError("Snapshot nesting exceeds limit") from exc


def validate_runtime_identity(data,scene_sha256,now):
    expected={"schema_version":1,"runtime_kind":"isaac-sim-executor","profile_id":"scenario1",
              "scene_sha256":scene_sha256,"physical_output_connected":False}
    if (not _hex(scene_sha256,64) or not isinstance(data,dict)
            or set(data)!=set(expected)|{"session_id","updated_at"}
            or any(type(data[k]) is not type(v) or data[k]!=v for k,v in expected.items())
            or not _hex(data["session_id"],32) or not isinstance(data["updated_at"],str)):
        raise ValueError("Runtime identity differs")
    updated=datetime.fromisoformat(data["updated_at"].replace("Z","+00:00"))
    if updated.tzinfo is None or not -.1<=now-updated.timestamp()<=1:
        raise ValueError("Runtime identity stale or future-dated")
    return data


def validate_state(data, scene_sha256, session, now):
    if not _hex(scene_sha256,64) or not _hex(session,32):
        raise ValueError("Pinned scene/session identity is malformed")
    if not isinstance(data,dict) or set(data) != FIELDS:
        raise ValueError("Unexpected scene snapshot fields")
    expected = {"schema_version":2,"runtime_kind":"isaac-sim-executor","profile_id":"scenario1",
                "scene_sha256":scene_sha256,"session_id":session,"physical_output_connected":False,"frame_id":"cell"}
    if any(type(data[k]) is not type(v) or data[k] != v for k,v in expected.items()):
        raise ValueError("Scene/session/output identity differs")
    for key in ("epoch","sequence","sim_time_ns","source_unix_ms"):
        if type(data[key]) is not int or not 0 <= data[key] < 2**63:
            raise ValueError("Invalid counter")
    if not -.1 <= now-data["source_unix_ms"]/1000 <= .25:
        raise ValueError("Source snapshot stale")
    if (not isinstance(data["phase"],str) or not 0<len(data["phase"])<=80
            or any(ord(c)<32 or 127<=ord(c)<=159 for c in data["phase"])
            or data["plate_owner"] not in ("carrier","omy","table")):
        raise ValueError("Invalid scenario phase/owner")
    if not isinstance(data["assets"],list) or len(data["assets"]) != 4:
        raise ValueError("Four measured assets are required")
    seen=set()
    for asset in data["assets"]:
        if (not isinstance(asset,dict) or set(asset) != ASSET_FIELDS
                or not isinstance(asset["asset_id"],str)):
            raise ValueError("Invalid asset fields")
        name=asset["asset_id"].removeprefix("isaac-scenario1-")
        if name not in JOINTS or asset["asset_id"] != "isaac-scenario1-"+name or name in seen:
            raise ValueError("Unexpected/duplicate asset")
        seen.add(name)
        if asset["joint_names"] != list(JOINTS[name]):
            raise ValueError("Joint mapping changed")
        for key,n in (("joint_positions_rad",len(JOINTS[name])),("position_m",3),("rotation_xyzw",4)):
            values=asset[key]
            if not isinstance(values,list) or len(values)!=n or any(type(v) not in (int,float) or not -10<=v<=10 for v in values):
                raise ValueError("Malformed measured values")
        if any(abs(v)>3.16 for v in asset["joint_positions_rad"]) or any(abs(v)>10 for v in asset["position_m"]):
            raise ValueError("Measured values out of envelope")
        if name=="omy" and not -.03<=asset["joint_positions_rad"][6]<=1.14+.03:
            raise ValueError("Measured gripper out of envelope")
        if abs(sum(v*v for v in asset["rotation_xyzw"])-1)>.001:
            raise ValueError("Non-unit quaternion")
    return data


class SceneReceiver:
    def __init__(self, root, scene_sha256, session):
        if not _hex(scene_sha256,64) or not _hex(session,32):
            raise ValueError("Pinned scene digest and executor session are required")
        self.root=Path(root); self.scene_sha256=scene_sha256; self.session=session
        self.last=(-1,-1); self.last_stamp=-1; self.image_last=(-1,-1); self.image_stamp=-1
        self.last_source=-1; self.last_receipt=(float("-inf"),float("-inf"))
        self.accepted=0; self.images=0; self.rejected=0; self.metadata=OrderedDict(); self.pending=OrderedDict()

    def _check_clock(self,now,mono):
        if (not math.isfinite(now) or not math.isfinite(mono)
                or now<self.last_receipt[0] or mono<self.last_receipt[1]):
            raise ValueError("Receiver clock regressed or is non-finite")

    def state(self,data,now=None,mono=None):
        now=time.time() if now is None else now; mono=time.monotonic() if mono is None else mono
        try:
            self._check_clock(now,mono)
            validate_state(data,self.scene_sha256,self.session,now)
            order=(data["epoch"],data["sequence"]); stamp=data["sim_time_ns"]
            if (order<=self.last or (order[0]==self.last[0] and stamp<=self.last_stamp)
                    or data["source_unix_ms"]<self.last_source):
                raise ValueError("Reordered scene snapshot")
            data=deepcopy(data)
            out={k:v for k,v in data.items() if k!="source_unix_ms"}
            out["received_at_unix_ms"]=int(now*1000)
            atomic_json(self.root/"telemetry.json",out)
            atomic_json(self.root/"source-provenance.json",data)
            self.last,self.last_stamp=order,stamp
            self.last_source=data["source_unix_ms"]; self.last_receipt=(now,mono)
            key=(order[0],stamp); self.metadata[key]=(data,now,mono)
            while len(self.metadata)>90:self.metadata.popitem(last=False)
            self.accepted+=1
            self._image(key,now,mono)
        except (ValueError,TypeError,KeyError,AttributeError):self.rejected+=1

    def image(self,stamp,frame_id,jpeg,now=None,mono=None):
        now=time.time() if now is None else now; mono=time.monotonic() if mono is None else mono
        try:
            self._check_clock(now,mono)
            if type(stamp) is not int or not 0<=stamp<2**63:
                raise ValueError("Invalid image timestamp")
            prefix,session,epoch=frame_id.split("/")
            if (prefix!="isaac-scenario1" or session!=self.session or not 0<len(epoch)<=19
                    or not epoch.isascii() or not epoch.isdecimal() or str(int(epoch))!=epoch
                    or not 0<=int(epoch)<2**63 or int(epoch)<self.last[0]):
                raise ValueError("Image identity differs")
            if type(jpeg) is not bytes or len(jpeg)>2*1024*1024 or not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                raise ValueError("Invalid JPEG envelope")
            key=(int(epoch),stamp)
            if key in self.pending:raise ValueError("Duplicate pending image")
            self.last_receipt=(now,mono)
            self.pending[key]=(jpeg,now,mono)
            while len(self.pending)>90:self.pending.popitem(last=False)
            self._image(key,now,mono)
        except (ValueError,TypeError,AttributeError):self.rejected+=1

    def _image(self,key,now,mono):
        if key not in self.metadata or key not in self.pending:return
        meta,state_now,received=self.metadata[key]; jpeg,image_now,image_received=self.pending.pop(key)
        order=(meta["epoch"],meta["sequence"])
        if (not -.1<=now-meta["source_unix_ms"]/1000<=.25
                or any(not 0<=mono-arrived<=.25 for arrived in (received,image_received))
                or any(not 0<=now-arrived<=.25 for arrived in (state_now,image_now))
                or order<=self.image_last or order[0]<self.last[0]
                or (order[0]==self.image_last[0] and key[1]<=self.image_stamp)):
            self.rejected+=1;return
        self.root.mkdir(parents=True,exist_ok=True)
        tmp=self.root/".frame.jpg.tmp";tmp.write_bytes(jpeg);os.replace(tmp,self.root/"frame.jpg")
        atomic_json(self.root/"image-provenance.json",{
            "session_id":self.session,"epoch":key[0],"sim_time_ns":key[1],"sequence":order[1],
            "scene_sha256":self.scene_sha256,"physical_output_connected":False,
            "source_unix_ms":meta["source_unix_ms"],"sha256":hashlib.sha256(jpeg).hexdigest(),
            "received_at_unix_ms":int(min(state_now,image_now)*1000),
            "metadata_received_at_unix_ms":int(state_now*1000),"image_received_at_unix_ms":int(image_now*1000)})
        self.image_last,self.image_stamp=order,key[1];self.images+=1


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--run-seconds",type=float,default=900)
    args=parser.parse_args()
    if not 0<args.run_seconds<=7200:parser.error("Bounded duration required")
    require_isolation(); _,sha=load_manifest()
    try:
        identity=validate_runtime_identity(read_message((ROOT/"runtime-identity.json").read_text()),sha,time.time())
    except (OSError,ValueError,TypeError) as exc:
        raise RuntimeError("Fresh exact Scenario 1 identity is required") from exc
    receiver=SceneReceiver(ROOT/"observer",sha,identity["session_id"])
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from std_msgs.msg import String
    from sensor_msgs.msg import CompressedImage
    rclpy.init(args=[]); node=Node("sang_scenario1_observer_receiver",use_global_arguments=False,enable_rosout=False,start_parameter_services=False)
    def state(message):
        try:
            receiver.state(read_message(message.data))
        except (ValueError,TypeError):receiver.rejected+=1
    def image(message):
        if message.format=="jpeg":receiver.image(message.header.stamp.sec*10**9+message.header.stamp.nanosec,message.header.frame_id,bytes(message.data))
    node.create_subscription(String,STATE_TOPIC,state,qos_profile_sensor_data)
    node.create_subscription(CompressedImage,CAMERA_TOPIC,image,qos_profile_sensor_data)
    started=time.monotonic()
    try:
        while time.monotonic()-started<args.run_seconds:
            rclpy.spin_once(node,timeout_sec=.02)
            atomic_json(ROOT/"observer/receiver-status.json",{"received_at_unix_ms":int(time.time()*1000),"samples":receiver.accepted,
                "images":receiver.images,"rejected":receiver.rejected,"physical_output_connected":False})
    finally:node.destroy_node();rclpy.shutdown()


if __name__=="__main__":main()
