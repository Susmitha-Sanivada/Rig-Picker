"""Picker undo/redo gaps found in an audit, now closed.

1. Picker undo is picker-only. Deleting a control that a Multi Command Button
   uses as a Control shape bone removes that reference from the button (scene
   data). Picker Ctrl+Z brings the control back and, by design, does NOT write
   into Blender (see Controller.undo()); the removal is undone by Blender's
   own undo via the native step. The old, never-applied `quick_control_bones`
   / `quick_toggle_restore` snapshot fields (and their stale comments) are gone.
2. A cancelled G grab is undone with discard_redo=True, so it leaves no redo
   entry (Ctrl+Shift+Z can't re-apply a move that was cancelled).
3. Show All / Hide All only take an undo snapshot when they change something.
4. The "RigPicker Undo: ..." viewport header text clears itself.

Runs the real Controller against stub bpy/Qt (see _controller_harness.py).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _controller_harness import ControllerTestCase  # noqa: E402

KEY = ("Rig", "Face")


def item(name, **extra):
    d = {"bone_name": name, "x": 1, "y": 1, "control_size": 18,
         "control_shape": "CIRCLE", "control_color": "GREEN"}
    d.update(extra)
    return d


class PickerUndoIsPickerOnlyTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        c, caq = self.c, self.caq
        c.data = {"background": "", "items": [item("b1")]}
        c.selected_bones = {"b1"}
        self.applied = []
        self.native = []
        caq.find_control_bone_references = lambda *a: [("btn", 0, ["b1"])]
        caq.remove_control_bone_references = lambda *a: [("btn-uid", "b1")]
        caq.register_control_delete = lambda *a: self.native.append("register")
        caq.push_native_undo_step = lambda *a: self.native.append("step")
        caq._sync_quick_picker_ui = lambda: None
        caq.apply_control_bone_presence = lambda e, present: self.applied.append(present)
        caq.note_control_bones_applied = lambda *a: None
        c._confirm_delete_used_controls = lambda refs: True

    def test_snapshot_no_longer_carries_the_dead_restore_fields(self):
        self.c.delete_selected()
        snap = self.c._current_snapshot()
        self.assertNotIn("quick_control_bones", snap)
        self.assertNotIn("quick_toggle_restore", snap)
        reverse = self.c._capture_reverse_entry(snap)
        self.assertNotIn("quick_control_bones", reverse)
        self.assertNotIn("quick_toggle_restore", reverse)

    def test_delete_still_registers_the_native_blender_undo_step(self):
        self.c.delete_selected()
        self.assertEqual(self.native, ["register", "step"])

    def test_picker_undo_brings_the_control_back_but_never_touches_buttons(self):
        self.c.delete_selected()
        self.assertEqual(self.bones(), [])
        self.c.undo()
        self.assertEqual(self.bones(), ["b1"])
        self.c.redo()
        self.assertEqual(self.bones(), [])
        self.assertEqual(self.applied, [])

    def test_record_snapshot_rejects_the_removed_argument(self):
        with self.assertRaises(TypeError):
            self.c._record_undo_snapshot(quick_toggle_restore=("multi",))

    def test_dead_restore_helpers_are_gone(self):
        C = self.ctl_mod.Controller
        self.assertFalse(hasattr(C, "_restore_quick_toggle"))
        self.assertFalse(hasattr(C, "_restore_quick_control_bones"))


class CancelledGrabTests(ControllerTestCase):
    def test_plain_undo_still_feeds_redo(self):
        self.op("b1")
        self.c.undo()
        self.assertEqual(len(self.c.redo_snapshots[KEY]), 1)

    def test_cancel_reverts_the_move_and_leaves_no_redo_entry(self):
        self.c.data = {"background": "", "items": [item("b1", x=10)]}
        self.c._record_undo_snapshot()            # first movement of the grab
        self.c.data["items"][0]["x"] = 99         # ...the grab moves it
        self.c.undo(discard_redo=True)            # Esc / right-click
        self.assertEqual(self.c.data["items"][0]["x"], 10)
        self.assertFalse(self.c.redo_snapshots.get(KEY))
        self.assertFalse(self.c.undo_snapshots.get(KEY))
        self.c.redo()                             # must not resurrect the move
        self.assertEqual(self.c.data["items"][0]["x"], 10)

    def test_cancel_after_a_new_operation_leaves_no_redo(self):
        self.op("b1")
        self.op("b2")
        self.c.undo()                             # a real undo: 1 redo entry
        self.c._record_undo_snapshot()            # a grab starts (a new op clears redo)
        self.c.undo(discard_redo=True)
        self.assertFalse(self.c.redo_snapshots.get(KEY))

    def test_cancel_with_nothing_to_cancel_never_undoes_a_clear_all(self):
        self.c.undo_snapshots.clear()
        with mock.patch.object(self.c, "_clear_all_entry") as entry:
            self.c.undo(discard_redo=True)
            entry.assert_not_called()

    def test_grab_cancel_in_control_list_requests_discard_redo(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "ui", "control_list.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("controller.undo(discard_redo=True)", src)


class UndoDuringDragTests(ControllerTestCase):
    """Ctrl+Z / Ctrl+Shift+Z while a drag or G grab is in progress is not an
    undo or redo: a grab is cancelled (like Esc), a mouse drag is ignored."""

    def canvas(self):
        return self.c.window.control_list.container

    def test_undo_and_redo_do_nothing_while_the_canvas_says_a_drag_is_in_progress(self):
        c = self.c
        self.op("b1")
        self.op("b2")
        c.undo()                                         # one real undo -> 1 redo entry
        before_data = [i["bone_name"] for i in c.data["items"]]
        undo_n, redo_n = len(c.undo_snapshots[KEY]), len(c.redo_snapshots[KEY])
        self.canvas().interrupt_for_undo.reset_mock()
        self.canvas().interrupt_for_undo.return_value = True
        c.undo()
        c.redo()
        self.assertEqual(self.bones(), before_data)
        self.assertEqual((len(c.undo_snapshots[KEY]), len(c.redo_snapshots[KEY])),
                         (undo_n, redo_n))
        self.assertEqual(self.canvas().interrupt_for_undo.call_count, 2)

    def test_undo_and_redo_work_again_once_the_drag_is_over(self):
        c = self.c
        self.op("b1")
        self.canvas().interrupt_for_undo.return_value = True
        c.undo()
        self.assertEqual(self.bones(), ["b1"])
        self.canvas().interrupt_for_undo.return_value = False
        c.undo()
        self.assertEqual(self.bones(), [])
        c.redo()
        self.assertEqual(self.bones(), ["b1"])

    def test_the_cancel_itself_is_not_blocked_by_the_check(self):
        # end_grab(False) -> undo(discard_redo=True) must run its revert even
        # if the canvas still reported a drag.
        c = self.c
        c.data = {"background": "", "items": [item("b1", x=10)]}
        c._record_undo_snapshot()
        c.data["items"][0]["x"] = 99
        self.canvas().interrupt_for_undo.return_value = True
        c.undo(discard_redo=True)
        self.assertEqual(c.data["items"][0]["x"], 10)
        self.canvas().interrupt_for_undo.assert_not_called()

    def test_ctrl_z_mid_grab_cancels_the_grab_and_takes_no_real_step(self):
        """Whole sequence: two real operations, then a grab, then Ctrl+Z.
        The grab is cancelled and the two operations are untouched; the NEXT
        Ctrl+Z undoes the second operation as usual."""
        c = self.c
        self.op("b1")
        self.op("b2")
        c.data["items"][0].update(item("b1", x=10))
        grab = {"active": False}

        def interrupt():                       # what PickerCanvas does for a grab
            if grab["active"]:
                grab["active"] = False
                c.undo(discard_redo=True)      # end_grab(False)
                return True
            return False
        self.canvas().interrupt_for_undo.side_effect = interrupt

        c._record_undo_snapshot()              # first movement of the grab
        grab["active"] = True
        c.data["items"][0]["x"] = 99
        steps_with_grab = len(c.undo_snapshots[KEY])      # b1, b2 + the grab's own

        c.undo()                               # Ctrl+Z mid-grab
        self.assertEqual(c.data["items"][0]["x"], 10)     # move taken back
        self.assertEqual(self.bones(), ["b1", "b2"])      # nothing real undone
        self.assertEqual(len(c.undo_snapshots[KEY]), steps_with_grab - 1)
        self.assertFalse(c.redo_snapshots.get(KEY))       # nothing redoable

        c.undo()                               # grab is over: a normal undo
        self.assertEqual(self.bones(), ["b1"])
        self.assertEqual(len(c.redo_snapshots[KEY]), 1)


class PanelRemovalHistoryTests(ControllerTestCase):
    """Removing a panel from Manage Panels is undoable by Blender's undo for a
    grace period, so the panel's picker undo/redo history is parked instead of
    discarded, and restored when the panel comes back (same idea as Clear All).
    Runs the real custom_actions_quick helpers; the operator's own call into
    _park_panel_history() needs Blender and is not covered."""

    def setUp(self):
        super().setUp()
        import time
        from types import SimpleNamespace
        self.time, self.ns = time, SimpleNamespace
        self.caq._PENDING_PANEL_DELETIONS.clear()
        self.caq._PENDING_PANEL_HISTORY.clear()
        self.caq._SAVED_PANEL_DELETIONS.clear()
        self.live = []
        self.caq._all_scene_panels = lambda: self.live
        self.jm = sys.modules["blender_addon.json_manager"]
        self.order = []
        self.c._settle_snapshot_pending_image.side_effect = (
            lambda s, k: self.order.append(("settle", k)))
        self.jm.delete_panel_data.side_effect = lambda *k: self.order.append(("delete", k))
        for b in ("b1", "b2", "b3"):
            self.op(b)

    def remove_panel(self):
        self.caq._schedule_panel_deletion("Rig", "Face")
        self.caq._park_panel_history(self.c, "Rig", "Face")

    def test_removing_a_panel_parks_its_history(self):
        self.remove_panel()
        self.assertNotIn(KEY, self.c.undo_snapshots)
        self.assertEqual(len(self.caq._PENDING_PANEL_HISTORY[KEY]["undo"]), 3)
        self.assertEqual(self.order, [])                     # nothing settled yet

    def test_undoing_the_removal_brings_the_history_back(self):
        self.remove_panel()
        self.live = [self.ns(armature_name="Rig", name="Face")]   # Blender undo: row is back
        self.caq._purge_expired_panel_deletions(None)
        self.assertEqual(len(self.c.undo_snapshots[KEY]), 3)
        self.assertEqual(self.caq._PENDING_PANEL_HISTORY, {})
        self.assertEqual(self.caq._PENDING_PANEL_DELETIONS, {})
        steps = 0
        while self.c.undo_snapshots.get(KEY):
            self.c.undo()
            steps += 1
        self.assertEqual((steps, self.bones()), (3, []))

    def test_an_expired_removal_settles_history_before_the_layout_is_deleted(self):
        self.remove_panel()
        self.caq._PENDING_PANEL_DELETIONS[KEY] = 0            # grace period over
        self.caq._purge_expired_panel_deletions(None)
        self.assertEqual(self.caq._PENDING_PANEL_HISTORY, {})
        kinds = [k for k, _ in self.order]
        self.assertEqual(kinds, ["settle"] * 3 + ["delete"])

    def test_a_new_panel_taking_the_name_settles_the_old_history(self):
        self.remove_panel()
        self.caq._finalize_pending_panel_deletion("Rig", "Face")
        self.assertEqual(self.caq._PENDING_PANEL_HISTORY, {})
        self.assertEqual([k for k, _ in self.order], ["settle"] * 3 + ["delete"])

    def test_a_file_change_settles_parked_history(self):
        self.remove_panel()
        self.caq._resolve_pending_panel_deletions_on_file_load()
        self.assertEqual(self.caq._PENDING_PANEL_HISTORY, {})
        self.assertEqual(len([1 for k, _ in self.order if k == "settle"]), 3)

    def test_a_rig_rename_moves_the_parked_history(self):
        self.remove_panel()
        self.caq._on_rig_renamed("Rig", "Rig2")
        self.assertIn(("Rig2", "Face"), self.caq._PENDING_PANEL_HISTORY)
        self.assertNotIn(KEY, self.caq._PENDING_PANEL_HISTORY)
        self.assertIn(("Rig2", "Face"), self.caq._PENDING_PANEL_DELETIONS)

    def test_parking_over_an_existing_entry_does_not_leak_the_old_one(self):
        self.remove_panel()
        for b in ("n1", "n2"):
            self.op(b)
        self.caq._park_panel_history(self.c, "Rig", "Face")
        self.assertEqual(len(self.caq._PENDING_PANEL_HISTORY[KEY]["undo"]), 2)
        self.assertEqual(len([1 for k, _ in self.order if k == "settle"]), 3)

    def test_save_as_repoints_parked_panel_and_clear_all_history(self):
        self.jm.translate_path = lambda p, a, b: p.replace(a, b)
        self.c.data = {"background": "/old/bg.png", "items": []}
        self.op("x")                                           # snapshot with the old path
        self.remove_panel()
        self.c.data = {"background": "/old/bg.png", "items": []}
        self.c.panel_name = "Other"
        self.op("y")
        self.jm.backup_armature_data.return_value = {"armature": "Rig", "images": {}}
        self.c.clear_all()                                     # parks "Other" history on the entry
        self.c.repoint_paths("/old", "/new")
        panel_snap = self.caq._PENDING_PANEL_HISTORY[KEY]["undo"][-1]
        clear_snap = self.caq._PENDING_RIG_CLEARS[0]["history"]["undo"][("Rig", "Other")][0]
        self.assertEqual(panel_snap["data"]["background"], "/new/bg.png")
        self.assertEqual(clear_snap["data"]["background"], "/new/bg.png")


class NoOpShowHideTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.ctl_mod.bpy.ops = mock.MagicMock()

    def steps(self):
        return len(self.c.undo_snapshots.get(KEY, ()))

    def test_show_all_with_nothing_hidden_adds_no_step(self):
        self.c.data = {"background": "", "items": [item("b1", hidden=False), item("b2")]}
        self.c.show_all()
        self.c.show_all()
        self.assertEqual(self.steps(), 0)
        self.ctl_mod.bpy.ops.rp.show_all.assert_called()     # still runs

    def test_show_all_that_reveals_something_is_one_undoable_step(self):
        self.c.data = {"background": "", "items": [item("b1", hidden=True), item("b2")]}
        self.c.show_all()
        self.assertEqual(self.steps(), 1)
        self.assertTrue(all(not i["hidden"] for i in self.c.data["items"]))
        self.c.undo()
        self.assertTrue(self.c.data["items"][0]["hidden"])
        self.c.show_all()
        self.c.show_all()                                     # second one is a no-op
        self.assertEqual(self.steps(), 1)

    def test_hide_all_with_everything_hidden_adds_no_step(self):
        self.c.data = {"background": "",
                       "items": [item("b1", hidden=True), item("b2", hidden=True)]}
        self.c.hide_all()
        self.assertEqual(self.steps(), 0)

    def test_hide_all_that_hides_something_is_one_undoable_step(self):
        self.c.data = {"background": "", "items": [item("b1", hidden=True), item("b2")]}
        self.c.hide_all()
        self.assertEqual(self.steps(), 1)
        self.assertTrue(all(i["hidden"] for i in self.c.data["items"]))
        self.c.undo()
        self.assertEqual([i.get("hidden", False) for i in self.c.data["items"]], [True, False])


class HeaderTextTests(ControllerTestCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        area = mock.MagicMock()
        area.type = "VIEW_3D"
        area.header_text_set.side_effect = lambda t: self.calls.append(t)
        window = mock.MagicMock()
        window.screen.areas = [area]
        self.ctl_mod.bpy.context.window_manager.windows = [window]
        self.timers = []
        self.ctl_mod.bpy.app.timers.register = (
            lambda fn, first_interval=0.0: self.timers.append((fn, first_interval)))
        self.ctl_mod._undo_header_generation = 0

    def test_text_is_set_then_cleared_by_its_own_timer(self):
        self.ctl_mod.Controller._show_snapshot_in_viewport(["b1.x: 1 -> 2"])
        self.assertEqual(self.calls, ["RigPicker Undo: b1.x: 1 -> 2"])
        self.assertEqual(len(self.timers), 1)
        self.assertEqual(self.timers[0][1], self.ctl_mod.UNDO_HEADER_SECONDS)
        self.assertIsNone(self.timers[0][0]())
        self.assertEqual(self.calls[-1], None)

    def test_an_older_timer_does_not_wipe_a_newer_message(self):
        Controller = self.ctl_mod.Controller
        Controller._show_snapshot_in_viewport(["first"])
        Controller._show_snapshot_in_viewport(["second"])
        old_clear, new_clear = self.timers[0][0], self.timers[1][0]
        old_clear()                                   # stale: must do nothing
        self.assertEqual(self.calls, ["RigPicker Undo: first", "RigPicker Undo: second"])
        new_clear()
        self.assertEqual(self.calls[-1], None)

    def test_clear_survives_windows_that_vanished(self):
        Controller = self.ctl_mod.Controller
        Controller._show_snapshot_in_viewport(["x"])
        type(self.ctl_mod.bpy.context.window_manager).windows = mock.PropertyMock(
            side_effect=ReferenceError("gone"))
        self.timers[0][0]()                           # must not raise


if __name__ == "__main__":
    unittest.main()
