from setuptools import find_packages, setup


package_name = "sang_security"


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
        "cryptography>=50,<51",
        "pyOpenSSL>=26,<27",
        "setuptools",
    ],
    zip_safe=True,
    maintainer="sa08050",
    maintainer_email="kb083509@gmail.com",
    description="PKI lifecycle and secret reference utilities for SANG services.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "sang_pki = sang_security.pki:main",
        ],
    },
)
