from setuptools import find_packages, setup

setup(
    name="ha-custom-component-manager",
    version="0.1.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "click>=8.0.0",
        "paramiko>=3.0.0",
        "PyYAML>=6.0",
        "requests>=2.28.0",
    ],
    entry_points={
        "console_scripts": [
            "ha-ccm=ha_ccm.cli:main",
        ],
    },
)
