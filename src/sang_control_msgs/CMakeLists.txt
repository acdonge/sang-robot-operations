cmake_minimum_required(VERSION 3.8)
project(sang_control_msgs)

find_package(ament_cmake REQUIRED)
find_package(action_msgs REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "action/ExecuteSkill.action"
  "msg/ExecutionEvent.msg"
  "msg/PolicyDemand.msg"
  "srv/CancelExecution.srv"
  "srv/QueryExecution.srv"
  "srv/RenewLease.srv"
  DEPENDENCIES action_msgs builtin_interfaces
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
