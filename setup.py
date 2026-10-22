# Fallback for older pip/setuptools without PEP 660 editable support.
from setuptools import setup

setup(
    name="icue-linux",
    version="0.1.0",
    license="MIT",
    author="Martin Brook",
    packages=["icuehost", "icuehost.plugins"],
    package_data={"icuehost": ["shim.js", "deck.example.toml", "gnome/*.in"]},
    python_requires=">=3.10",
    install_requires=[
        # PySide6 6.12.0 leaks a reference to True on every signal emit; on
        # Python < 3.12 that eventually aborts the process (see icuehost/qtfix.py).
        "PySide6>=6.9,<6.12; python_version < '3.12'",
        "PySide6>=6.9; python_version >= '3.12'",
        "tomli; python_version < '3.11'",
    ],
    entry_points={"console_scripts": ["icue-linux=icuehost.__main__:main"]},
)
