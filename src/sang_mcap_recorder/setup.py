from setuptools import find_packages, setup


package_name = "sang_mcap_recorder"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=("test",)),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    extras_require={"test": ["pytest"]},
    install_requires=["PyYAML", "setuptools"],
    zip_safe=True,
    maintainer="sa08050",
    maintainer_email="kb083509@gmail.com",
    description="Allowlisted read-only MCAP capture worker for SANG observation data.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "record_session = sang_mcap_recorder.cli:main",
        ],
    },
)
