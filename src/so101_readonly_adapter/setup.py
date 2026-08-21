from setuptools import find_packages, setup


package_name = "so101_readonly_adapter"


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
    description="Read-only SO-101 and OpenCV observation adapters.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "arm_probe = so101_readonly_adapter.probe:main",
            "arm_reader = so101_readonly_adapter.arm_node:main",
            "camera_reader = so101_readonly_adapter.camera_node:main",
        ],
    },
)
