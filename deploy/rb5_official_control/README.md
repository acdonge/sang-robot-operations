# RB5 Official Control on lab1

This lab-only wrapper builds the official Rainbow Robotics `rbpodo` and
`rbpodo_ros2` repositories at pinned commits inside a ROS 2 Humble container.

The pinned upstream driver starts its speed bar at `1.0` (100%). This build
applies the included `rbpodo_ros2-startup-speed.patch` so physical startup is
limited to `0.01` (1%). The patch is intentionally small and auditable.

- `RB5 Safe Simulation` never connects to the physical controller.
- `RB5 Hardware Control (Attended)` connects to `10.0.2.7` only after an
  operator safety confirmation.
- `Stop RB5 Control UI` stops either control container.
- The command ROS graph uses domain 52 and `LOCALHOST` discovery. SANG
  observation remains read-only on domain 42.
- Desktop launchers temporarily enter the already-authorized `docker` group
  when the current GNOME session predates group enrollment. No Docker socket
  permission is widened; a normal logout/login also refreshes the group.

## Manufacturer Linux UI

Rainbow Robotics `Rainbow-RB` 8.44 is installed separately from the ROS 2
wrapper at `/home/lab1/Applications/Rainbow-RB-8.44/RB_Linux`. The source
archive is retained at `/home/lab1/Downloads/RB_Linux_844.zip` with SHA-256:

`a85e46a785cf8ddc5105124d3dfc03eba361f906feb11ae8dc4713017dafbed8`

`Rainbow RB Official UI (Manufacturer)` launches the vendor UI through
`start_vendor_ui.sh`. Opening the UI does not replace the physical E-stop or
authorize unattended motion; controller connection, initialization, and jog
operations require an attending operator.

The upstream project explicitly states that it is under active development and
must not be treated as production-safe. The physical E-stop and vendor safety
system remain authoritative.
