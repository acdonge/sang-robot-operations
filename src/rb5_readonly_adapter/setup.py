from setuptools import find_packages, setup


package_name = "rb5_readonly_adapter"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=("test",)),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    extras_require={"test": ["pytest"]},
    install_requires=["rbpodo==0.16.14", "setuptools"],
    zip_safe=True,
    maintainer="sa08050",
    maintainer_email="kb083509@gmail.com",
    description="Read-only RB5 state adapter using the vendor data channel.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "state_reader = rb5_readonly_adapter.node:main",
        ],
    },
)
