"""Merging N-panel payloads from two Blender instances, using the real
serialized structure produced by custom_actions_quick._collect_payload():
{"version", "scenes": {scene: {panels, multi_actions, single_actions,
*_index, ...}}}, with Multi/Single buttons keyed by "uid" and panels by
_hidden._uid."""
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import AddonTestCase  # noqa: E402


def multi(uid, name, commands=("bpy.ops.x()",)):
    return {
        "uid": uid, "name": name, "action_type": "BUTTON",
        "rules": [{"command": c, "bones": [{"name": "b1"}], "bones_index": 0,
                   "expanded": True} for c in commands],
        "rules_index": 0, "expanded": True, "toggle_state": False,
        "armature_name": "Rig",
    }


def single(uid, name, command="bpy.ops.y()"):
    return {"uid": uid, "name": name,
            "rules": [{"command": command, "expanded": True}],
            "expanded": True}


def panel(uid, name):
    return {"name": name, "armature_name": "Rig",
            "_hidden": {"_uid": uid, "_prev_name": name}}


def payload():
    return {"version": 1, "scenes": {"Scene": {
        "new_multi_type": "BUTTON",
        "panels": [panel("p1", "Face"), panel("p2", "Body")],
        "panels_index": 0,
        "multi_actions": [multi("m1", "Button 1"), multi("m2", "Button 2"),
                          multi("m3", "Button 3")],
        "multi_actions_index": 0,
        "single_actions": [single("s1", "Single 1"), single("s2", "Single 2")],
        "single_actions_index": 0,
    }}}


def scene(p):
    return p["scenes"]["Scene"]


def by_uid(items):
    return {i.get("uid") or i["_hidden"]["_uid"]: i for i in items}


class QuickPayloadMergeTests(AddonTestCase):
    def setUp(self):
        super().setUp()
        self.jm = self.load_json_manager()
        self.base = payload()

    def merge(self, mine, theirs):
        conflicts = []
        out = self.jm._merge3(self.base, mine, theirs, conflicts=conflicts)
        return out, conflicts

    # ---- the reported scenario -------------------------------------
    def test_renaming_different_buttons_keeps_both(self):
        a, b = payload(), payload()
        scene(a)["multi_actions"][0]["name"] = "Renamed by A"
        scene(b)["multi_actions"][1]["name"] = "Renamed by B"
        out, conflicts = self.merge(a, b)
        names = [x["name"] for x in scene(out)["multi_actions"]]
        self.assertEqual(names, ["Renamed by A", "Renamed by B", "Button 3"])
        self.assertEqual(conflicts, [])

    def test_single_actions_and_panels_merge_by_uid_too(self):
        a, b = payload(), payload()
        scene(a)["single_actions"][0]["name"] = "S1 by A"
        scene(b)["single_actions"][1]["rules"][0]["command"] = "bpy.ops.z()"
        scene(a)["panels"][0]["name"] = "Face by A"
        scene(b)["panels"][1]["name"] = "Body by B"
        out, conflicts = self.merge(a, b)
        s = scene(out)
        self.assertEqual([x["name"] for x in s["single_actions"]], ["S1 by A", "Single 2"])
        self.assertEqual(s["single_actions"][1]["rules"][0]["command"], "bpy.ops.z()")
        self.assertEqual([x["name"] for x in s["panels"]], ["Face by A", "Body by B"])
        self.assertEqual(conflicts, [])

    def test_edits_in_different_fields_of_the_same_button_both_survive(self):
        a, b = payload(), payload()
        scene(a)["multi_actions"][0]["name"] = "New label"
        scene(b)["multi_actions"][0]["rules"][0]["command"] = "bpy.ops.new()"
        out, conflicts = self.merge(a, b)
        m1 = scene(out)["multi_actions"][0]
        self.assertEqual(m1["name"], "New label")
        self.assertEqual(m1["rules"][0]["command"], "bpy.ops.new()")
        self.assertEqual(conflicts, [])

    # ---- additions and deletions -----------------------------------
    def test_additions_from_both_instances_are_kept(self):
        a, b = payload(), payload()
        scene(a)["multi_actions"].append(multi("mA", "Added by A"))
        scene(b)["multi_actions"].append(multi("mB", "Added by B"))
        out, conflicts = self.merge(a, b)
        names = [x["name"] for x in scene(out)["multi_actions"]]
        self.assertEqual(names[:3], ["Button 1", "Button 2", "Button 3"])
        self.assertEqual(set(names[3:]), {"Added by A", "Added by B"})
        self.assertEqual(conflicts, [])

    def test_deletion_on_one_side_and_edit_of_another_button_on_the_other(self):
        a, b = payload(), payload()
        del scene(a)["multi_actions"][0]                       # A deletes Button 1
        scene(b)["multi_actions"][1]["name"] = "Renamed by B"  # B edits Button 2
        out, conflicts = self.merge(a, b)
        names = [x["name"] for x in scene(out)["multi_actions"]]
        self.assertEqual(names, ["Renamed by B", "Button 3"])
        self.assertEqual(conflicts, [])

    def test_deletion_of_an_untouched_button_is_not_resurrected(self):
        a, b = payload(), payload()
        del scene(b)["multi_actions"][2]                       # B deletes Button 3
        scene(a)["multi_actions"][0]["name"] = "Renamed by A"
        out, _ = self.merge(a, b)
        self.assertEqual([x["name"] for x in scene(out)["multi_actions"]],
                         ["Renamed by A", "Button 2"])

    # ---- genuine conflicts -----------------------------------------
    def test_same_button_same_field_conflict_keeps_ours_and_names_the_button(self):
        a, b = payload(), payload()
        scene(a)["multi_actions"][0]["name"] = "A's name"
        scene(b)["multi_actions"][0]["name"] = "B's name"
        scene(b)["multi_actions"][1]["name"] = "B renamed 2"
        out, conflicts = self.merge(a, b)
        names = [x["name"] for x in scene(out)["multi_actions"]]
        self.assertEqual(names, ["A's name", "B renamed 2", "Button 3"])
        self.assertEqual(len(conflicts), 1, conflicts)
        self.assertIn("multi_actions", conflicts[0])
        self.assertIn("Button 1", conflicts[0])      # the button, by name
        self.assertTrue(conflicts[0].endswith("name"))

    def test_delete_versus_edit_of_the_same_button_is_a_conflict(self):
        a, b = payload(), payload()
        del scene(a)["multi_actions"][0]
        scene(b)["multi_actions"][0]["name"] = "B edited it"
        out, conflicts = self.merge(a, b)
        self.assertEqual(len(conflicts), 1, conflicts)
        self.assertEqual([x["name"] for x in scene(out)["multi_actions"]],
                         ["Button 2", "Button 3"])

    # ---- order and UI state ----------------------------------------
    def test_reorder_on_one_side_is_kept_with_edits_from_the_other(self):
        a, b = payload(), payload()
        scene(a)["multi_actions"].reverse()                    # A reorders
        scene(b)["multi_actions"][0]["name"] = "B renamed 1"
        out, conflicts = self.merge(a, b)
        names = [x["name"] for x in scene(out)["multi_actions"]]
        self.assertEqual(names, ["Button 3", "Button 2", "B renamed 1"])
        self.assertEqual(conflicts, [])

    def test_selection_and_expanded_state_do_not_raise_conflicts(self):
        a, b = payload(), payload()
        scene(a)["multi_actions_index"] = 1
        scene(b)["multi_actions_index"] = 2
        scene(a)["multi_actions"][0]["expanded"] = False
        scene(b)["multi_actions"][0]["expanded"] = True
        scene(b)["multi_actions"][0]["toggle_state"] = True
        scene(a)["panels_index"] = 1
        scene(b)["panels_index"] = 0
        scene(b)["single_actions"][1]["name"] = "B real edit"
        out, conflicts = self.merge(a, b)
        self.assertEqual(conflicts, [])
        self.assertEqual(scene(out)["multi_actions_index"], 1)      # ours
        self.assertEqual(scene(out)["single_actions"][1]["name"], "B real edit")

    # ---- fallbacks ---------------------------------------------------
    def test_items_without_uid_fall_back_to_whole_list(self):
        base = {"l": [{"name": "x"}, {"name": "y"}]}
        a = {"l": [{"name": "X"}, {"name": "y"}]}
        b = {"l": [{"name": "x"}, {"name": "Y"}]}
        c = []
        out = self.jm._merge3(base, a, b, conflicts=c)
        self.assertEqual(out, a)
        self.assertEqual(c, ["l"])

    def test_duplicate_uids_fall_back_to_whole_list(self):
        dup = lambda n: [{"uid": "u", "name": n}, {"uid": "u", "name": "2"}]
        c = []
        out = self.jm._merge3({"l": dup("0")}, {"l": dup("A")}, {"l": dup("B")}, conflicts=c)
        self.assertEqual(out, {"l": dup("A")})
        self.assertEqual(c, ["l"])

    def test_rules_lists_stay_whole_values(self):
        a, b = payload(), payload()
        scene(a)["multi_actions"][0]["rules"].append(
            {"command": "A", "bones": [], "bones_index": 0, "expanded": True})
        scene(b)["multi_actions"][0]["rules"].append(
            {"command": "B", "bones": [], "bones_index": 0, "expanded": True})
        out, conflicts = self.merge(a, b)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(scene(out)["multi_actions"][0]["rules"][-1]["command"], "A")


class QuickPayloadSaveTests(AddonTestCase):
    """Same thing end to end: two instances, one data.json, through
    save_quick_actions()."""

    def setUp(self):
        super().setUp()
        self.jm = self.load_json_manager()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = os.path.join(tmp.name, "x.rigpicker")
        os.makedirs(folder)
        self.store = self.jm._Store("k", folder, "")
        self.store.data = {"settings": {"window": {}}, "armatures": {}}
        self.jm._store = self.store
        self.jm.current_store = lambda: self.store
        self.errors = []
        self.jm.set_error_handler(self.errors.append)
        self.assertIs(self.jm.save_quick_actions(payload()), True)   # both start here

    def disk(self):
        with open(self.store.path) as f:
            return json.load(f)["quick_actions"]

    def test_instance_a_and_b_rename_different_buttons(self):
        # Instance B saved its rename of Button 2 to the file.
        with open(self.store.path) as f:
            d = json.load(f)
        scene(d["quick_actions"])["multi_actions"][1]["name"] = "Renamed by B"
        with open(self.store.path, "w") as f:
            json.dump(d, f)

        # Instance A (loaded before B saved) renames Button 1 and saves.
        a = payload()
        scene(a)["multi_actions"][0]["name"] = "Renamed by A"
        self.assertIs(self.jm.save_quick_actions(a), True)

        names = [x["name"] for x in scene(self.disk())["multi_actions"]]
        self.assertEqual(names, ["Renamed by A", "Renamed by B", "Button 3"])
        self.assertEqual(self.errors, [])
        mem = [x["name"] for x in scene(self.jm.get_quick_actions())["multi_actions"]]
        self.assertEqual(mem, names)

    def test_real_conflict_is_reported_with_the_button_name(self):
        with open(self.store.path) as f:
            d = json.load(f)
        scene(d["quick_actions"])["multi_actions"][0]["name"] = "B's name"
        with open(self.store.path, "w") as f:
            json.dump(d, f)
        a = payload()
        scene(a)["multi_actions"][0]["name"] = "A's name"
        self.assertIs(self.jm.save_quick_actions(a), True)
        self.assertEqual(scene(self.disk())["multi_actions"][0]["name"], "A's name")
        self.assertTrue(any("Button 1" in e for e in self.errors), self.errors)


if __name__ == "__main__":
    unittest.main()
