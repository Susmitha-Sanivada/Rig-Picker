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

    subprocess.check_call([
        str(python),
        "-m",
        "pip",
        "install",
        "PySide6",
        "--upgrade",
        "--target",
        str(target),
    ])

    add_python_path()


# ----------------------------------------------------------
# BQT
# ----------------------------------------------------------

def has_bqt():

    try:

        import bl_ext.user_default.bqt

        return True

    except Exception:

        return False


# ----------------------------------------------------------
# Initialization
# ----------------------------------------------------------

def initialize_bqt():
    add_python_path()
    
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()

    if app is not None:
        return app

    import bl_ext.user_default.bqt as bqt

    bqt.register()

    app = QApplication.instance()

    if app is None:

        raise RuntimeError(
            "Unable to initialize BQT."
        )

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

def install_bqt():

    zip_path = bqt_zip()

    if not zip_path.exists():

        raise RuntimeError(
            f"BQT archive not found:\n{zip_path}"
        )

    result = bpy.ops.extensions.package_install_files(
        repo=get_local_repo(),

        filepath=str(zip_path),

        enable_on_install=True,

        overwrite=False,

    )

    print(result)


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

def get_local_repo():

    prefs = bpy.context.preferences

    for repo in prefs.extensions.repos:

        # We want a writable local repository
        if not repo.remote_url:
            return repo.module

    raise RuntimeError("No writable extension repository found.")