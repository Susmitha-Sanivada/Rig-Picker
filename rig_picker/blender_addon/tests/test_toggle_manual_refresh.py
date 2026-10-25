"""A Toggle whose property is changed by a script is NOT noticed until a sync
runs, and refresh_quick_toggle_states() is that sync."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import AddonTestCase, PKG_DIR  # noqa: E402

ns = types.SimpleNamespace
CMD = "rig.props['IK_FK']"


def make_action(bones=("Hand",), command=CMD, state=False):
    rule = ns(command=command, bones=[ns(name=b) for b in bones])
    return ns(action_type='TOGGLE', rules=[rule], toggle_state=state,
              panel_uid="", armature_name="Rig", panel_name="")


class ManualRefreshTests(AddonTestCase):
    def setUp(self):
        super().setUp()
        self.caq, self.backend = self.load_quick()
        self.rig = ns(name="Rig", props={"IK_FK": 0.0})
        self.backend.arm = lambda: self.rig

        self.action = make_action()
        self.other = make_action(bones=("Foot",))     # rule doesn't match the selection
        settings = ns(multi_actions=[self.action, self.other], panels=[])
        self.scene = ns(rp_quick_action_settings=settings)
        self.bpy = self.caq.bpy
        self.bpy.context.scene = self.scene
        self.bpy.context.selected_pose_bones = [ns(name="Hand")]

    def test_no_change_reports_false(self):
        self.assertIs(self.caq.sync_quick_toggle_states(self.scene), False)
        self.assertFalse(self.action.toggle_state)

    def test_script_change_is_stale_until_refreshed(self):
        # A script flips the property. Nothing runs, so the switch is stale:
        # this is the documented limit.
        self.rig.props["IK_FK"] = 1.0
        self.assertFalse(self.action.toggle_state)

        self.assertIs(self.caq.refresh_quick_toggle_states(self.scene), True)
        self.assertTrue(self.action.toggle_state)
        self.assertFalse(self.other.toggle_state, "a non-matching rule is left alone")

        self.assertIs(self.caq.refresh_quick_toggle_states(self.scene), False)  # now a no-op

    def test_scene_defaults_to_bpy_context(self):
        self.rig.props["IK_FK"] = 1.0
        self.assertIs(self.caq.refresh_quick_toggle_states(), True)
        self.assertTrue(self.action.toggle_state)
        self.rig.props["IK_FK"] = 0.0
        self.assertIs(self.caq.refresh_quick_toggle_states(), True)
        self.assertFalse(self.action.toggle_state)

    def test_pickers_tracked_selection_beats_the_viewports(self):
        self.bpy.context.selected_pose_bones = []     # viewport says nothing selected
        self.backend._ACTIVE_CONTROLLER = ns(selected_bones={"Hand"})
        self.rig.props["IK_FK"] = 1.0
        self.assertIs(self.caq.refresh_quick_toggle_states(self.scene), True)
        self.assertTrue(self.action.toggle_state)

    def test_falls_back_to_viewport_selection_without_a_tracked_one(self):
        self.backend._ACTIVE_CONTROLLER = ns(selected_bones=None)
        self.rig.props["IK_FK"] = 1.0
        self.assertIs(self.caq.refresh_quick_toggle_states(self.scene), True)
        self.assertTrue(self.action.toggle_state)

    def test_unreadable_target_resets_a_stale_on(self):
        self.action.toggle_state = True
        self.rig.props.pop("IK_FK")
        self.assertIs(self.caq.refresh_quick_toggle_states(self.scene), True)
        self.assertFalse(self.action.toggle_state)

    def test_no_settings_or_no_rig_is_harmless(self):
        self.assertIs(self.caq.sync_quick_toggle_states(ns()), False)
        self.backend.arm = lambda: None
        self.assertIs(self.caq.refresh_quick_toggle_states(self.scene), False)

    def test_operator_and_panel_button_are_registered(self):
        # The operator class is a mock under the stub, so check the source.
        with open(os.path.join(PKG_DIR, "custom_actions_quick.py"), encoding="utf8") as f:
            src = f.read()
        self.assertEqual(src.count('bl_idname = "rp.quick_refresh_toggles"'), 1)
        self.assertIn("    RP_OT_quick_refresh_toggles,\n", src)
        self.assertIn('add_row.operator("rp.quick_refresh_toggles"', src)


if __name__ == "__main__":
    unittest.main()
