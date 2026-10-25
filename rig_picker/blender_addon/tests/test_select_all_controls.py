"""Controller.select_all() (the picker's 'A' hotkey) selects the picker
controls only and goes through _sync_viewport_selection(); it must not
reveal everything or select every bone with pose.select_all(SELECT).

controller.py needs PySide6 and a live Blender, so the method's source is
extracted and run against fakes."""
import ast
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import PKG_DIR  # noqa: E402


METHODS = ("select_all", "invert_selection", "_selectable_control_names",
           "_set_picker_selection")


def load_methods(backend_mod, is_visible):
    path = os.path.join(PKG_DIR, "ui", "controller.py")
    with open(path, encoding="utf8") as f:
        tree = ast.parse(f.read())
    fns = [n for c in tree.body if isinstance(c, ast.ClassDef)
           for n in c.body
           if isinstance(n, ast.FunctionDef) and n.name in METHODS]
    assert {f.name for f in fns} == set(METHODS)
    mod = ast.Module(body=fns, type_ignores=[])
    ast.fix_missing_locations(mod)

    # `from ..backend import ...` inside the methods.
    fake_backend = types.SimpleNamespace(
        arm=lambda: backend_mod.rig,
        is_bone_collection_visible=is_visible,
    )
    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 2 and name == "backend":
            return fake_backend
        return real_import(name, globals, locals, fromlist, level)

    builtins = dict(vars(__builtins__)) if not isinstance(__builtins__, dict) \
        else dict(__builtins__)
    builtins["__import__"] = fake_import
    ns = {"backend": backend_mod, "__builtins__": builtins}
    exec(compile(mod, path, "exec"), ns)
    return {n: ns[n] for n in METHODS}


class Widget:
    def __init__(self):
        self.active = False
        self.enabled = True

    def isEnabled(self):
        return self.enabled

    def update(self):
        pass


class Bone:
    def __init__(self, name):
        self.name = name


class FakeController:
    def __init__(self, controls, rig, selected=()):
        self.no_selection_defaults = 0
        self.window = mock.MagicMock()
        self.window.control_list.controls = {n: Widget() for n in controls}
        self.selected_bones = set(selected)
        self.snapshots = []
        self.synced = []
        self.rig = rig
        self.items = {n: {"control_size": 1, "control_shape": "c", "control_color": "r"}
                      for n in controls}

    def _record_undo_snapshot(self, selection_only=False):
        self.snapshots.append(selection_only)

    def _sync_viewport_selection(self):
        self.synced.append(set(self.selected_bones))

    def find_item(self, name):
        return self.items.get(name)

    def _update_action_controls_enabled(self):
        pass

    def refresh_custom_action_enabled_states(self):
        pass


def make_rig(control_names, extra_bones=("DEF-spine", "MCH-x"), hidden=()):
    rig = types.SimpleNamespace(type="ARMATURE")
    rig.pose = types.SimpleNamespace(
        bones={n: Bone(n) for n in list(control_names) + list(extra_bones)})
    rig.hidden = set(hidden)
    return rig


def run_method(name, ctrl, rig):
    backend_mod = types.SimpleNamespace(rig=rig, _LAST_SELECTED_BONES=frozenset())
    visible = (lambda pb: pb.name not in rig.hidden) if rig else (lambda pb: True)
    for fname, fn in load_methods(backend_mod, visible).items():
        setattr(ctrl, fname, types.MethodType(fn, ctrl))
    getattr(ctrl, name)()
    return backend_mod


class SelectAllTests(unittest.TestCase):
    def run_select_all(self, ctrl, rig):
        return run_method("select_all", ctrl, rig)

    def test_selects_only_the_picker_controls_not_other_bones(self):
        rig = make_rig(["c1", "c2", "c3"])
        ctrl = FakeController(["c1", "c2", "c3"], rig)
        be = self.run_select_all(ctrl, rig)
        self.assertEqual(ctrl.selected_bones, {"c1", "c2", "c3"})
        self.assertNotIn("DEF-spine", ctrl.selected_bones)
        self.assertEqual(ctrl.synced, [{"c1", "c2", "c3"}])   # pushed once, via the shared sync
        self.assertTrue(all(w.active for w in ctrl.window.control_list.controls.values()))
        self.assertEqual(be._LAST_SELECTED_BONES, frozenset({"c1", "c2", "c3"}))

    def test_does_not_call_blender_operators_directly(self):
        # The old implementation called bpy.ops.pose.reveal() and
        # bpy.ops.pose.select_all(SELECT) itself (Show All + every bone).
        with open(os.path.join(PKG_DIR, "ui", "controller.py"), encoding="utf8") as f:
            tree = ast.parse(f.read())
        fns = [n for c in tree.body if isinstance(c, ast.ClassDef)
               for n in c.body
               if isinstance(n, ast.FunctionDef) and n.name in METHODS]
        calls = [ast.unparse(n.func) for fn in fns for n in ast.walk(fn)
                 if isinstance(n, ast.Call)]
        self.assertFalse([c for c in calls if ".ops." in c or c.startswith("bpy.ops")], calls)
        self.assertIn("self._sync_viewport_selection", calls)

    def test_controls_in_hidden_collections_are_left_unselected(self):
        rig = make_rig(["c1", "c2"], hidden={"c2"})
        ctrl = FakeController(["c1", "c2"], rig)
        self.run_select_all(ctrl, rig)
        self.assertEqual(ctrl.selected_bones, {"c1"})
        self.assertFalse(ctrl.window.control_list.controls["c2"].active)

    def test_greyed_out_controls_are_not_selected(self):
        rig = make_rig(["c1", "c2", "c3"])
        ctrl = FakeController(["c1", "c2", "c3"], rig)
        ctrl.window.control_list.controls["c2"].enabled = False   # greyed out
        self.run_select_all(ctrl, rig)
        self.assertEqual(ctrl.selected_bones, {"c1", "c3"})
        self.assertFalse(ctrl.window.control_list.controls["c2"].active)
        self.assertEqual(ctrl.synced, [{"c1", "c3"}])

    def test_greyed_out_control_is_skipped_even_if_its_collection_looks_visible(self):
        # The grey-out can lag the live collection state by a poll tick.
        rig = make_rig(["c1", "c2"], hidden=set())
        ctrl = FakeController(["c1", "c2"], rig, selected={"c2"})
        ctrl.window.control_list.controls["c2"].enabled = False
        self.run_select_all(ctrl, rig)
        self.assertEqual(ctrl.selected_bones, {"c1"})

    def test_greyed_out_controls_are_skipped_without_a_rig_too(self):
        ctrl = FakeController(["c1", "c2"], None)
        ctrl.window.control_list.controls["c1"].enabled = False
        self.run_select_all(ctrl, None)
        self.assertEqual(ctrl.selected_bones, {"c2"})

    def test_all_controls_greyed_out_selects_nothing(self):
        rig = make_rig(["c1"])
        ctrl = FakeController(["c1"], rig)
        ctrl.window.control_list.controls["c1"].enabled = False
        self.run_select_all(ctrl, rig)
        self.assertEqual(ctrl.selected_bones, set())

    def test_no_snapshot_when_everything_is_already_selected(self):
        rig = make_rig(["c1", "c2"])
        ctrl = FakeController(["c1", "c2"], rig, selected={"c1", "c2"})
        self.run_select_all(ctrl, rig)
        self.assertEqual(ctrl.snapshots, [])

    def test_selection_change_records_one_selection_only_snapshot(self):
        rig = make_rig(["c1", "c2"])
        ctrl = FakeController(["c1", "c2"], rig, selected={"c1"})
        self.run_select_all(ctrl, rig)
        self.assertEqual(ctrl.snapshots, [True])

    def test_no_controls_is_a_noop(self):
        rig = make_rig([])
        ctrl = FakeController([], rig, selected={"DEF-spine"})
        self.run_select_all(ctrl, rig)
        self.assertEqual(ctrl.synced, [])
        self.assertEqual(ctrl.snapshots, [])
        self.assertEqual(ctrl.selected_bones, {"DEF-spine"})

    def test_without_a_rig_all_controls_are_still_selected_in_the_picker(self):
        ctrl = FakeController(["c1", "c2"], None)
        be = self.run_select_all(ctrl, None)
        self.assertEqual(ctrl.selected_bones, {"c1", "c2"})
        self.assertEqual(be._LAST_SELECTED_BONES, frozenset({"c1", "c2"}))


class InvertSelectionTests(unittest.TestCase):
    def run_invert(self, ctrl, rig):
        return run_method("invert_selection", ctrl, rig)

    def test_selects_everything_that_was_not_selected_and_drops_the_rest(self):
        names = ["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8"]
        rig = make_rig(names)
        ctrl = FakeController(names, rig, selected=names[:5])
        be = self.run_invert(ctrl, rig)
        self.assertEqual(ctrl.selected_bones, {"c6", "c7", "c8"})
        self.assertEqual(ctrl.synced, [{"c6", "c7", "c8"}])
        active = {n for n, w in ctrl.window.control_list.controls.items() if w.active}
        self.assertEqual(active, {"c6", "c7", "c8"})
        self.assertEqual(be._LAST_SELECTED_BONES, frozenset({"c6", "c7", "c8"}))
        self.assertNotIn("DEF-spine", ctrl.selected_bones)
        self.assertEqual(ctrl.snapshots, [True])

    def test_greyed_out_controls_are_not_selected_by_invert(self):
        rig = make_rig(["c1", "c2", "c3", "c4"])
        ctrl = FakeController(["c1", "c2", "c3", "c4"], rig, selected={"c1"})
        ctrl.window.control_list.controls["c3"].enabled = False
        self.run_invert(ctrl, rig)
        self.assertEqual(ctrl.selected_bones, {"c2", "c4"})

    def test_controls_in_hidden_collections_are_not_selected_by_invert(self):
        rig = make_rig(["c1", "c2", "c3"], hidden={"c3"})
        ctrl = FakeController(["c1", "c2", "c3"], rig, selected={"c1"})
        self.run_invert(ctrl, rig)
        self.assertEqual(ctrl.selected_bones, {"c2"})

    def test_nothing_selected_inverts_to_all_selectable(self):
        rig = make_rig(["c1", "c2", "c3"])
        ctrl = FakeController(["c1", "c2", "c3"], rig)
        ctrl.window.control_list.controls["c2"].enabled = False
        self.run_invert(ctrl, rig)
        self.assertEqual(ctrl.selected_bones, {"c1", "c3"})

    def test_everything_selected_inverts_to_nothing_and_resets_defaults(self):
        rig = make_rig(["c1", "c2"])
        ctrl = FakeController(["c1", "c2"], rig, selected={"c1", "c2"})
        self.run_invert(ctrl, rig)
        self.assertEqual(ctrl.selected_bones, set())
        self.assertFalse(any(w.active for w in ctrl.window.control_list.controls.values()))
        ctrl.window.set_no_selection_defaults.assert_called_once()
        self.assertEqual(ctrl.synced, [set()])

    def test_inverting_twice_restores_the_selection(self):
        rig = make_rig(["c1", "c2", "c3", "c4"])
        ctrl = FakeController(["c1", "c2", "c3", "c4"], rig, selected={"c1", "c2"})
        self.run_invert(ctrl, rig)
        self.run_invert(ctrl, rig)
        self.assertEqual(ctrl.selected_bones, {"c1", "c2"})

    def test_a_selected_control_that_became_greyed_out_is_dropped(self):
        rig = make_rig(["c1", "c2", "c3"])
        ctrl = FakeController(["c1", "c2", "c3"], rig, selected={"c1", "c2"})
        ctrl.window.control_list.controls["c2"].enabled = False
        self.run_invert(ctrl, rig)
        self.assertEqual(ctrl.selected_bones, {"c3"})

    def test_no_controls_is_a_noop(self):
        rig = make_rig([])
        ctrl = FakeController([], rig, selected={"DEF-spine"})
        self.run_invert(ctrl, rig)
        self.assertEqual(ctrl.synced, [])
        self.assertEqual(ctrl.snapshots, [])
        self.assertEqual(ctrl.selected_bones, {"DEF-spine"})

    def test_without_a_rig(self):
        ctrl = FakeController(["c1", "c2", "c3"], None, selected={"c1"})
        self.run_invert(ctrl, None)
        self.assertEqual(ctrl.selected_bones, {"c2", "c3"})


if __name__ == "__main__":
    unittest.main()
