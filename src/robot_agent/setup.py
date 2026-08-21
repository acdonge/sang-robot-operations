from setuptools import find_packages, setup


package_name = "robot_agent"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=("test",)),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    extras_require={"test": ["pytest"]},
    install_requires=["PyYAML"],
    zip_safe=True,
    maintainer="sa08050",
    maintainer_email="kb083509@gmail.com",
    description="Read-only manifest-driven ROS 2 health Agent.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "robot_agent = robot_agent.node:main",
            "sensor_sync_probe = robot_agent.sync_probe:main",
        ],
    },
)
