from glob import glob
import os

from setuptools import find_packages, setup


package_name = "xr_teleop_retarget"


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
        (
            os.path.join("share", package_name, "launch"),
            glob("launch/*.launch.py"),
        ),
        (os.path.join("share", package_name, "rviz"), glob("rviz/*.rviz")),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="sa08050",
    maintainer_email="kb083509@gmail.com",
    description="Safe Galaxy XR to SO-ARM target preview for ROS 2.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "xr_retarget_preview = xr_teleop_retarget.node:main",
            "xr_isaac_gateway = xr_teleop_retarget.isaac_gateway:main",
        ],
    },
)
