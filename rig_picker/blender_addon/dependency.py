"""
dependency.py

Responsible for preparing the Qt environment.

This module is the ONLY place that knows how to:

- Check/install PySide6
- Check BQT
- Initialize BQT
- Return the QApplication
"""

import importlib.util
import subprocess
import sys

from pathlib import Path

import bpy
import zipfile
import shutil


import sysconfig


# ----------------------------------------------------------
# Utilities
# ----------------------------------------------------------

def blender_python():

    return Path(sys.executable)


def pip_install(*packages):

    python = blender_python()

    subprocess.check_call([
        str(python),
        "-m",
        "ensurepip",
        "--upgrade",
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
        *packages,
    ])


# ----------------------------------------------------------
# PySide6
# ----------------------------------------------------------

import importlib.util

def has_pyside():

    add_python_path()

    return importlib.util.find_spec(
        "PySide6"
    ) is not None





def install_pyside():

    python = Path(sys.executable)

    target = python_folder()

    # Always install into a clean folder, not on top of whatever's
    # already there. `pip install --upgrade --target` layers new files
    # over old ones rather than removing files an older version had
    # that the new one doesn't - across several upgrades this can leave
    # DLLs/pyd files from different PySide6 releases coexisting in the
    # same folder. Mismatched native Qt/shiboken binaries loaded
    # together are a well-known cause of an access violation crash the
    # instant QtCore is first imported (Blender crashing on startup with
    # a fault inside shiboken6.abi3.dll/QtCore.pyd, before any of the
    # addon's own UI code runs, is the signature of exactly this).
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Installing PySide6")
    print("Target:", target)
    print("=" * 60)

    subprocess.check_call([
        str(python),
        "-m",
        "ensurepip",
        "--upgrade",
    ])

    subprocess.check_call([
        str(python),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
    ])

    # Pinned, not "--upgrade" to whatever is newest on PyPI. PySide6
    # only reaches PyPI after Qt's own release, and brand-new Qt/
    # shiboken6 builds have repeatedly shown first-import native crashes
    # specifically when embedded inside a host interpreter like
    # Blender's (rather than run as a normal standalone application) -
    # see e.g. the Blender project's own "PySide6 shiboken6 conflict"
    # tracker issue. Pinning to a release that's had time to prove
    # itself in this exact embedded-Qt-in-Blender scenario, rather than
    # always grabbing the latest, avoids installing straight into a
    # known-bad combination on a fresh machine. Bump this deliberately
    # (and re-test in Blender) rather than letting it float.
    PYSIDE6_VERSION = "6.8.3"

    subprocess.check_call([
        str(python),
        "-m",
        "pip",
        "install",
        f"PySide6=={PYSIDE6_VERSION}",
        f"shiboken6=={PYSIDE6_VERSION}",
        "blender-qt-stylesheet",
        "qtpy",
        "--target",
        str(target),
    ])

    add_python_path()


# ----------------------------------------------------------
# BQT
# ----------------------------------------------------------

def has_bqt():

    add_python_path()

    return (
        importlib.util.find_spec("bqt")
        is not None
    )


# ----------------------------------------------------------
# Initialization
# ----------------------------------------------------------

_BQT_INITIALIZED = False


def initialize_bqt():
    global _BQT_INITIALIZED

    add_python_path()

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()

    if app is not None and _BQT_INITIALIZED:
        return app

    if not _BQT_INITIALIZED:
        import bqt
        bqt.register()
        _BQT_INITIALIZED = True

    app = QApplication.instance()

    if app is None:
        raise RuntimeError("Unable to initialize BQT.")

    return app


# ----------------------------------------------------------
# Public API
# ----------------------------------------------------------

def ensure_qt():

    if not has_pyside():

        print("PySide6 not found.")
        install_pyside()

    print("PySide6 OK")

    if not has_bqt():

        print("Installing BQT...")
        install_bqt()

    return initialize_bqt()

def addon_root():

    return Path(__file__).parent


def vendor_folder():

    return addon_root() / "vendor"


def bqt_zip():

    return vendor_folder() / "bqt.zip"

import zipfile
import shutil


def bqt_folder():

    return python_folder() / "bqt"


def install_bqt():

    target = bqt_folder()

    if target.exists():
        return

    print("=" * 60)
    print("Extracting bundled BQT")
    print("=" * 60)

    with zipfile.ZipFile(
        bqt_zip(),
        "r",
    ) as z:

        z.extractall(
            python_folder()
        )


def python_folder():

    folder = addon_root() / "python"

    folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    return folder


def add_python_path():

    folder = str(python_folder())

    if folder not in sys.path:

        sys.path.insert(
            0,
            folder,
        )

