from setuptools import find_packages, setup


package_name = "xr_teleop_bridge"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=("test",)),
    extras_require={"test": ["pytest"]},
    data_files=[
        (
            "share/ament_index/resource_index/packages",
            ["resource/" + package_name],
        ),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="sa08050",
    maintainer_email="kb083509@gmail.com",
    description="Validated TCP-to-ROS 2 gateway for XR teleoperation input.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "xr_mock_sender = xr_teleop_bridge.mock_sender:main",
            "xr_teleop_bridge = xr_teleop_bridge.node:main",
        ],
    },
)
