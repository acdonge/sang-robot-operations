uint8 PROTOCOL_VERSION=1

std_msgs/Header header
uint8 protocol_version
uint32 sequence

string target_link
geometry_msgs/PoseStamped target
float32 gripper
bool enabled
string inhibit_reason
uint32 clutch_epoch
