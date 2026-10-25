"""Clear All must not erase the picker's undo history.

Before the fix, Controller.clear_all() discarded every undo/redo stack of the
rig, so undoing the Clear All brought the layout back but Ctrl+Z could not go
any further back than that. The history is now parked on the Clear All's entry
and restored when the Clear All is undone (and re-parked when it is redone).

Runs the real Controller undo/redo/clear_all methods and the real
custom_actions_quick Clear All helpers against stub bpy/Qt modules.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _controller_harness import ControllerTestCase  # noqa: E402
from _stubs import PKG_NAME  # noqa: E402


class ClearAllHistoryTests(ControllerTestCase):
    def clear_all(self):
        # clear_all() resets to a fresh "Panel 1"; json_manager is a stub, so
        # give backup_armature_data a token (None would mean "nothing stored").
        sys.modules[f"{PKG_NAME}.json_manager"].backup_armature_data.return_value = {
            "armature": "Rig", "images": {}}
        self.c.clear_all()

    def test_history_survives_clear_all_and_its_undo(self):
        for b in ("b1", "b2", "b3", "b4", "b5"):
            self.op(b)
        self.assertEqual(len(self.c.undo_snapshots[("Rig", "Face")]), 5)

        self.clear_all()
        self.assertEqual(self.bones(), [])
        self.assertFalse([k for k in self.c.undo_snapshots if k[0] == "Rig"])

        # 1st Ctrl+Z: nothing on the stack -> undoes the Clear All itself.
        self.c.undo()
        self.assertEqual(self.caq._PENDING_RIG_CLEARS[0]["state"], "undone")
        # The layout comes back from the json backup in the real addon; here
        # put back the data the poll tick would reload.
        self.c.panel_name = "Face"
        self.c.data = {"background": "", "items": [{"bone_name": b} for b in
                       ("b1", "b2", "b3", "b4", "b5")]}

        # Ctrl+Z now has to walk back through ALL five earlier operations.
        steps = 0
        while self.c.undo_snapshots.get(("Rig", "Face")):
            self.c.undo()
            steps += 1
        self.assertEqual(steps, 5)
        self.assertEqual(self.bones(), [])

    def test_redo_of_clear_all_parks_history_again(self):
        for b in ("b1", "b2", "b3"):
            self.op(b)
        self.clear_all()
        self.c.undo()                                   # undo the clear
        self.assertEqual(len(self.c.undo_snapshots[("Rig", "Face")]), 3)
        self.c.redo()                                   # redo the clear
        self.assertFalse([k for k in self.c.undo_snapshots if k[0] == "Rig"])
        self.c.undo()                                   # and undo it again
        self.assertEqual(len(self.c.undo_snapshots[("Rig", "Face")]), 3)

    def test_ops_done_while_cleared_do_not_leak_into_restored_history(self):
        self.op("b1")
        self.clear_all()
        self.c.panel_name = "Panel 1"
        self.op("new")                                  # work on the blank panel
        self.assertEqual(len(self.c.undo_snapshots[("Rig", "Panel 1")]), 1)
        self.c.undo_snapshots.pop(("Rig", "Panel 1"))   # picker stack empty again
        self.c.undo()                                   # undo the Clear All
        self.assertNotIn(("Rig", "Panel 1"), self.c.undo_snapshots)
        self.assertEqual(len(self.c.undo_snapshots[("Rig", "Face")]), 1)

    def test_expired_clear_settles_parked_history(self):
        self.op("b1")
        self.clear_all()
        entry = self.caq._PENDING_RIG_CLEARS[0]
        entry["deadline"] = 0                           # grace period over
        self.c._settle_snapshot_pending_image.reset_mock()
        self.caq._purge_expired_rig_clears()
        self.assertEqual(self.caq._PENDING_RIG_CLEARS, [])
        self.assertTrue(self.c._settle_snapshot_pending_image.called)


if __name__ == "__main__":
    unittest.main()
