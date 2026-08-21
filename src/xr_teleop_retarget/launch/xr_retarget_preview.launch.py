from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
import os


def generate_launch_description() -> LaunchDescription:
    package_share = get_package_share_directory("xr_teleop_retarget")
    rviz_config = os.path.join(package_share, "rviz", "xr_retarget_preview.rviz")
    bind_host = LaunchConfiguration("bind_host")
    port = LaunchConfiguration("port")
    use_rviz = LaunchConfiguration("rviz")
    use_isaac_gateway = LaunchConfiguration("isaac_gateway")
    isaac_endpoint = LaunchConfiguration("isaac_endpoint")
    translation_scale = LaunchConfiguration("translation_scale")

    return LaunchDescription(
        [
            DeclareLaunchArgument("bind_host", default_value="0.0.0.0"),
            DeclareLaunchArgument("port", default_value="5556"),
            DeclareLaunchArgument("rviz", default_value="false"),
            DeclareLaunchArgument("translation_scale", default_value="0.5"),
            DeclareLaunchArgument("isaac_gateway", default_value="true"),
            DeclareLaunchArgument(
                "isaac_endpoint", default_value="tcp://0.0.0.0:5558"
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
                package="xr_teleop_retarget",
                executable="xr_retarget_preview",
                name="xr_retarget_preview",
                output="screen",
                parameters=[
                    {
                        "translation_scale": ParameterValue(
                            translation_scale, value_type=float
                        )
                    }
                ],
            ),
            Node(
                package="xr_teleop_retarget",
                executable="xr_isaac_gateway",
                name="xr_isaac_gateway",
                parameters=[{"bind_endpoint": isaac_endpoint}],
                condition=IfCondition(use_isaac_gateway),
                output="screen",
            ),
            Node(
                package="rviz2",
                executable="rviz2",
                name="xr_retarget_rviz",
                arguments=["-d", rviz_config],
                condition=IfCondition(use_rviz),
                output="screen",
            ),
        ]
    )
