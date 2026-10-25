"""Shared helpers for the addon's tests: load its modules OUTSIDE Blender.

The addon imports `bpy` (and friends) at module level, so each test installs
a stub `bpy` first. `AddonTestCase` snapshots `sys.modules` before each test
and restores it afterwards, so the stubs, the fake `blender_addon` package
and the freshly loaded addon modules never leak from one test into another
(or into the rest of the process, e.g. when run under a single runner).

Not named test*.py on purpose: discovery must not treat it as a test module.
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PKG_DIR = os.path.dirname(TESTS_DIR)
PKG_NAME = "blender_addon"

_BPY_MODULES = (
    "bpy", "bpy.utils", "bpy.utils.previews", "bpy.app", "bpy.app.handlers",
    "bpy.types", "bpy.props",
)


def _load(module_name, filename):
    spec = importlib.util.spec_from_file_location(
        module_name, os.path.join(PKG_DIR, filename)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class AddonTestCase(unittest.TestCase):
    def setUp(self):
        before = dict(sys.modules)

        def restore():
            for name in [n for n in sys.modules if n not in before]:
                del sys.modules[name]
            sys.modules.update(before)

        self.addCleanup(restore)

    def load_quick(self):
        """Imports custom_actions_quick.py against a MagicMock `bpy` and a
        fake `backend` module. Returns (module, backend). The module's bpy
        classes become mocks, which is fine: only its plain functions are
        tested here."""
        for name in _BPY_MODULES:
            sys.modules[name] = mock.MagicMock(name=name)
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [PKG_DIR]
        sys.modules[PKG_NAME] = pkg

        backend = types.ModuleType(f"{PKG_NAME}.backend")
        backend.arm = lambda: None
        backend._ACTIVE_CONTROLLER = None
        backend._ACTIVE_WINDOW = None
        sys.modules[backend.__name__] = backend
        pkg.backend = backend

        module = _load(f"{PKG_NAME}.custom_actions_quick", "custom_actions_quick.py")
        return module, backend

    def load_json_manager(self):
        """Imports json_manager.py against a minimal `bpy`."""
        bpy = types.ModuleType("bpy")
        bpy.data = types.SimpleNamespace(filepath="")
        bpy.app = types.SimpleNamespace(tempdir=tempfile.gettempdir())
        sys.modules["bpy"] = bpy
        return _load("json_manager", "json_manager.py")
