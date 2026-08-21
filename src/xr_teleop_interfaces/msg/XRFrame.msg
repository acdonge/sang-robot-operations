uint8 PROTOCOL_VERSION=1

std_msgs/Header header
uint8 protocol_version
uint32 sequence
uint64 device_time_ns
string source

bool head_pose_valid
bool head_pose_tracked
geometry_msgs/Pose head_pose

xr_teleop_interfaces/XRControllerState left
xr_teleop_interfaces/XRControllerState right
