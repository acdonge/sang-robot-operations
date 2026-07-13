from setuptools import find_packages, setup


package_name = "recorder_core"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=("test",)),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["PyYAML"],
    zip_safe=True,
    maintainer="sa08050",
    maintainer_email="sa08050@localhost",
    description="Manifest-driven ROS 2 recorder core skeleton.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "recorder_core = recorder_core.recorder_node:main",
        ],
    },
)

