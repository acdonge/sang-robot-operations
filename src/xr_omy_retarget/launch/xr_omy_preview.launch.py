from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description() -> LaunchDescription:
    bind_host = LaunchConfiguration("bind_host")
    port = LaunchConfiguration("port")
    controller_hand = LaunchConfiguration("controller_hand")
    translation_scale = LaunchConfiguration("translation_scale")
    track_orientation = LaunchConfiguration("track_orientation")
    use_isaac_gateway = LaunchConfiguration("isaac_gateway")
    isaac_endpoint = LaunchConfiguration("isaac_endpoint")

    return LaunchDescription(
        [
            DeclareLaunchArgument("bind_host", default_value="0.0.0.0"),
            DeclareLaunchArgument("port", default_value="5556"),
            DeclareLaunchArgument("controller_hand", default_value="right"),
            DeclareLaunchArgument("translation_scale", default_value="1.0"),
            DeclareLaunchArgument("track_orientation", default_value="false"),
            DeclareLaunchArgument("isaac_gateway", default_value="true"),
            DeclareLaunchArgument(
                "isaac_endpoint", default_value="tcp://0.0.0.0:5559"
            ),
            Node(
                package="xr_teleop_bridge",
                executable="xr_teleop_bridge",
                name="xr_teleop_bridge",
                output="screen",
                parameters=[
                    {
                        "bind_host": bind_host,
                        "port": ParameterValue(port, value_type=int),
                    }
                ],
            ),
            Node(
                package="xr_omy_retarget",
                executable="xr_omy_retarget",
                name="xr_omy_retarget",
                output="screen",
                parameters=[
                    {
                        "controller_hand": controller_hand,
                        "translation_scale": ParameterValue(
                            translation_scale, value_type=float
                        ),
                        "track_orientation": ParameterValue(
                            track_orientation, value_type=bool
                        ),
                    }
                ],
            ),
            Node(
                package="xr_omy_retarget",
                executable="xr_omy_isaac_gateway",
                name="xr_omy_isaac_gateway",
                parameters=[{"bind_endpoint": isaac_endpoint}],
                condition=IfCondition(use_isaac_gateway),
                output="screen",
            ),
        ]
    )
