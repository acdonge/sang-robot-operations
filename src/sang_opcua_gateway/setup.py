from setuptools import find_packages, setup


package_name = "sang_opcua_gateway"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=("test",)),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    extras_require={"test": ["pytest"]},
    install_requires=["asyncua==2.0.1", "PyYAML", "setuptools"],
    zip_safe=True,
    maintainer="sa08050",
    maintainer_email="kb083509@gmail.com",
    description="Read-only ROS 2 to OPC UA Edge gateway for SANG robot observations.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "sang_opcua_gateway = sang_opcua_gateway.node:main",
            "sang_opcua_validate = sang_opcua_gateway.validate:main",
        ],
    },
)
