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
    install_requires=[
        "PyYAML",
        "fastapi",
        "numpy",
        "opencv-python-headless",
        "uvicorn",
    ],
    zip_safe=True,
    maintainer="sa08050",
    maintainer_email="kb083509@gmail.com",
    description="Manifest-driven ROS 2 recorder core.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "recorder_core = recorder_core.recorder_node:main",
            "fake_robot = recorder_core.fake_robot:main",
            "recorder_gateway = recorder_core.gateway:main",
            "validate_dataset = recorder_core.validate_cli:main",
        ],
    },
)
