"""Shared headless Controller harness for the undo tests.

Loads the REAL ui/controller.py and custom_actions_quick.py against stub
bpy / PySide6 modules and builds a bare Controller (no Blender, no Qt), with
the UI-facing methods mocked, so the undo/redo stack logic itself runs.

Not named test*.py on purpose: discovery must not treat it as a test module.
"""
import os
import sys
import types
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import AddonTestCase, PKG_DIR, PKG_NAME, _BPY_MODULES, _load  # noqa: E402


class ControllerTestCase(AddonTestCase):
    def setUp(self):
        super().setUp()
        for name in _BPY_MODULES + ("PySide6", "PySide6.QtCore", "PySide6.QtWidgets"):
            sys.modules[name] = mock.MagicMock(name=name)
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [PKG_DIR]
        sys.modules[PKG_NAME] = pkg

        backend = types.ModuleType(f"{PKG_NAME}.backend")
        backend.arm = lambda: None
        backend._ACTIVE_CONTROLLER = None
        sys.modules[backend.__name__] = backend
        pkg.backend = backend

        jm = mock.MagicMock(name="json_manager")
        jm.new_panel_data = lambda: {"background": "", "items": []}
        sys.modules[f"{PKG_NAME}.json_manager"] = jm
        pkg.json_manager = jm

        self.caq = _load(f"{PKG_NAME}.custom_actions_quick", "custom_actions_quick.py")
        pkg.custom_actions_quick = self.caq
        self.caq._PENDING_RIG_CLEARS.clear()
        # No scene rows involved: the picker-level history is what is tested.
        self.caq._ensure_default_panel_for_rig = lambda *a, **k: None
        self.caq._sync_quick_picker_ui = lambda: None
        self.caq.keep_clear_scene_snapshot = lambda entry, *a: entry.update(scene={})

        ui = types.ModuleType(f"{PKG_NAME}.ui")
        ui.__path__ = [os.path.join(PKG_DIR, "ui")]
        sys.modules[ui.__name__] = ui
        self.ctl_mod = _load(f"{PKG_NAME}.ui.controller", os.path.join("ui", "controller.py"))

        C = self.ctl_mod.Controller
        c = object.__new__(C)
        c.armature_name, c.panel_name = "Rig", "Face"
        c.data = {"background": "", "items": []}
        c.selected_bones = set()
        c.undo_snapshots, c.redo_snapshots = {}, {}
        c._pending_deselect_baseline = None
        c._undo_backup_counter = 0
        c.default_control_size = 18
        c.default_control_shape = "CIRCLE"
        c.default_control_color = "GREEN"
        c.window = None
        c._quick_synced_key = None
        for name in ("save", "refresh", "_save_panel_defaults", "_sync_viewport_selection",
                     "refresh_custom_action_enabled_states", "_show_all_and_deselect",
                     "_settle_snapshot_pending_image"):
            setattr(c, name, mock.MagicMock())
        c._show_snapshot_in_viewport = mock.MagicMock()
        c._visible_selected_bones = lambda names: set(names)
        c.window = mock.MagicMock()           # clear_all() calls window.* directly
        # No drag/grab in progress, so undo()/redo() run (see _interrupted_by_drag()).
        c.window.control_list.container.interrupt_for_undo.return_value = False
        backend._ACTIVE_CONTROLLER = c
        self.c = c

    def op(self, bone):
        """One undoable operation: add a control (snapshot first, like the UI)."""
        self.c._record_undo_snapshot()
        self.c.data["items"].append({"bone_name": bone})

    def bones(self):
        return [i["bone_name"] for i in self.c.data["items"]]
