from setuptools import find_packages, setup


package_name = "fleet_control_plane"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=("test",)),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    extras_require={"test": ["pytest"]},
    install_requires=[
        "asyncua==2.0.1",
        "fastapi",
        "mcap==1.4.0",
        "PyYAML",
        "pydantic",
        "uvicorn",
    ],
    zip_safe=True,
    maintainer="sa08050",
    maintainer_email="kb083509@gmail.com",
    description="Read-only central fleet registry and Admin Console.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "fleet_control_plane = fleet_control_plane.serve:main",
            "fleet_seed = fleet_control_plane.seed:main",
            "fleet_soak = fleet_control_plane.soak:main",
            "fleet_soak_compare = fleet_control_plane.soak_compare:main",
            "fleet_opcua_shadow = fleet_control_plane.opcua_shadow:main",
            "fleet_agent_ingress = fleet_control_plane.agent_ingress:main",
            "fleet_recording_ingress = fleet_control_plane.recording_ingress:main",
            "fleet_security_enroll = fleet_control_plane.security_enroll:main",
            "fleet_security_rotate = fleet_control_plane.security_rotate:main",
            "fleet_security_revoke = fleet_control_plane.security_revoke:main",
            "fleet_runtime_generate = fleet_control_plane.runtime_deploy:main",
            "fleet_identity = fleet_control_plane.identity:main",
            "fleet_recording_upload = fleet_control_plane.recording_upload:main",
            "fleet_recording_auto_upload = fleet_control_plane.recording_auto_upload:main",
            "fleet_recording_worker = fleet_control_plane.recording_worker:main",
            "fleet_recording_quality = fleet_control_plane.recording_quality:main",
            "fleet_opcua_aggregate = fleet_control_plane.central_opcua:main",
            "fleet_backup = fleet_control_plane.backup:main",
        ],
    },
)
