# Fallback for older pip/setuptools without PEP 660 editable support.
from setuptools import setup

setup(
    name="icue-linux",
    version="0.1.0",
    packages=["icuehost", "icuehost.plugins"],
    package_data={"icuehost": ["shim.js"]},
    python_requires=">=3.11",
    install_requires=["PySide6>=6.9"],
    entry_points={"console_scripts": ["icue-linux=icuehost.__main__:main"]},
)
