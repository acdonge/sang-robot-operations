uint8 HAND_UNKNOWN=0
uint8 HAND_LEFT=1
uint8 HAND_RIGHT=2

uint32 BUTTON_PRIMARY=1
uint32 BUTTON_SECONDARY=2
uint32 BUTTON_MENU=4
uint32 BUTTON_THUMBSTICK=8
uint32 BUTTON_TRIGGER_TOUCH=16
uint32 BUTTON_THUMBSTICK_TOUCH=32

uint8 hand
bool connected

bool grip_pose_valid
bool grip_pose_tracked
geometry_msgs/Pose grip_pose

bool aim_pose_valid
bool aim_pose_tracked
geometry_msgs/Pose aim_pose

geometry_msgs/Vector3 linear_velocity
geometry_msgs/Vector3 angular_velocity

float32 trigger
float32 squeeze
float32 thumbstick_x
float32 thumbstick_y
uint32 buttons
