import importlib.util
import subprocess
import sys
from pathlib import Path


def is_package_installed(package_name: str) -> bool:
    """Return True if a package can be imported."""
    return importlib.util.find_spec(package_name) is not None


def blender_python():
    """
    Return Blender's bundled Python executable.
    """
    return Path(sys.executable)


def install_package(package_name: str):
    """
    Install a package into Blender's Python environment.
    """

    python = blender_python()

    subprocess.check_call([
        str(python),
        "-m",
        "ensurepip",
    ])

    subprocess.check_call([
        str(python),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
    ])

    subprocess.check_call([
        str(python),
        "-m",
        "pip",
        "install",
        package_name,
    ])


def ensure_package(package_name: str):
    """
    Install package if it doesn't exist.
    """
    if not is_package_installed(package_name):
        install_package(package_name)