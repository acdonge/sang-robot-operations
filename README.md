# SANG

**An operations platform connecting robot observation, task management, data collection, and digital twins.**

Manage SO-101, OMY, RB5, and cameras as workcells. Collect ROS 2 observations, preserve selected
recording intervals in MCAP, and inspect device status, recordings, and simulation results through
one web interface. OPC UA provides read-only information exchange; AAS connects asset documents.

[Platform overview (Korean)](docs/SANG_PLATFORM_OVERVIEW_KO.md) ·
[Installation & operations](docs/ROBOT_CAMERA_OPERATIONS_V1.md) ·
[Detailed usage (Korean)](docs/USAGE.md) ·
[Requirements](docs/requirements.md)

## System Architecture

![SANG architecture: robots and cameras feed Edge and Central services, with user interfaces, observation data export, and a separate simulation execution path](docs/assets/sang-system-overview-en.svg)

1. **Workcell** — Observe robot joint states and camera images through ROS 2 adapters.
2. **Edge** — Robot Agent reports health; MCAP Recorder captures selected recording intervals.
3. **Central** — Store assets, tasks, permissions, and metadata in SQLite; preserve verified recordings and derived objects in the Object Store.
4. **Interfaces** — Use Admin and Operator for observation and authorized task operations, and OPC UA clients for information exchange.

A separate worker runs validated, fixed simulation profiles. Unity execution and
Isaac execution with Unity observation operate independently of the physical observation path.

## Features

| Area | Capabilities |
|---|---|
| **Fleet & Task** | Register assets, Processing PCs, and workcells; assign tasks; track Agent health and events. |
| **Camera & Recording** | Preview selected cameras, record Task Run intervals, verify uploads, and replay multiple views. |
| **OPC UA & AAS** | Read-only Edge and Central projections, pinned NodeSets and mapping contracts, and asset document links. |
| **Workflow & Simulation** | Versioned Actions and Workflows, with isolated Unity and Isaac simulation execution. |
| **Data Export** | Derive LeRobot v3 datasets containing joint states and camera observations from quality-checked MCAP sessions. |

**Current scope:** The physical path supports observation and recording. Derived LeRobot datasets
contain no `action` feature. VLA training, policy execution, and actuation of physical robots or
welding equipment are outside this repository's supported functionality.
No OPC UA conformance certification is claimed.

## Quick Start

Clone the repository and build the web interface. You will need Git, Docker, and a Node.js/npm
version supported by the project's dependencies.

```bash
git clone https://github.com/acdonge/sang-robot-operations.git
cd sang-robot-operations

mkdir -p ui-admin ui-operator
cd frontend/admin
npm ci
npm run build
cd ../..
```

Open the ROS 2 Jazzy development container:

```bash
docker/run.sh
```

Inside the container, build and source the workspace:

```bash
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install
source install/setup.bash
```

Next, follow [first-time installation and administrator setup](docs/ROBOT_CAMERA_OPERATIONS_V1.md#first-local-installation)
to start Central. Device connections, simulator execution, and remote deployment require configuration
for your environment. Unity and Isaac runtimes, external models, and scenes must be supplied separately;
see the [reproduction requirements](docs/GITHUB_SOURCE_POLICY.md#reproduction-requirements).

## Repository Layout

```text
src/
├── fleet_control_plane/    # Central API, assets/tasks, data, simulation workers
├── robot_agent/            # ROS 2 topic health and heartbeats
├── so101_readonly_adapter/ # SO-101 observation
├── rb5_readonly_adapter/   # RB5 observation
├── sang_mcap_recorder/     # MCAP recording of allowlisted topics
├── sang_opcua_gateway/     # Read-only OPC UA
├── sang_security/          # Certificates, TLS, and security
└── xr_*/                   # XR input and target preview
frontend/admin/            # React + TypeScript + Ant Design
contracts/                 # Versioned schemas, mappings, and execution contracts
manifests/ · config/        # Device, topic, and camera profiles
sim/ · unity/              # Isolated simulation and observation interfaces
docker/ · deploy/          # Development environment and deployment configuration
scripts/                   # Launch, validation, and operations tools
docs/                      # Public guides and architecture diagrams
```

## Documentation

| Purpose | Guide |
|---|---|
| Understand the platform and data flow | [Platform overview (Korean)](docs/SANG_PLATFORM_OVERVIEW_KO.md) |
| Install, deploy remotely, back up, and upgrade | [Operations guide](docs/ROBOT_CAMERA_OPERATIONS_V1.md) |
| Find device observation, recording, and simulation commands | [Detailed usage (Korean)](docs/USAGE.md) |
| Review functionality, permissions, and security boundaries | [Requirements](docs/requirements.md) |
| Configure OPC UA mappings and security | [OPC UA contracts](contracts/opcua/README.md) |
| Review Action and Workflow interfaces | [Orchestration contracts](contracts/orchestration/README.md) |
| Understand the derived LeRobot dataset format | [Data contract](docs/LEROBOT_DERIVATION_CONTRACT.md) |
| Check included source and external dependencies | [Source publication policy](docs/GITHUB_SOURCE_POLICY.md) |

This repository contains public source code and usage documentation. Internal plans and working notes
are maintained in a separate private repository.
