"""A Toggle undo/redo snapshot pins its rig by identity (RigRef: name +
session_uid), and resolve_rig_ref() follows the rig by uid.

Scope: the resolver (make_rig_ref / resolve_rig_ref), which is what decides
which rig an undo/redo is applied to. The Controller's undo/redo stack
itself needs Blender and is not covered. (The picker's Ctrl+Z no longer restores
Toggles - see tests/test_undo_gaps.py - so nothing in the picker currently pins
a rig through it; the resolver is kept and stays tested.)
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import AddonTestCase  # noqa: E402


class FakeObjects(list):
    """bpy.data.objects: iterable, with .get(name)."""

    def get(self, name, default=None):
        return next((o for o in self if o.name == name), default)


def obj(name, uid, type="ARMATURE"):
    return types.SimpleNamespace(name=name, session_uid=uid, type=type)


class RigIdentityTests(AddonTestCase):
    def setUp(self):
        super().setUp()
        self.caq, _ = self.load_quick()
        self.objects = FakeObjects()
        self.caq.bpy.data.objects = self.objects

    def add(self, name, uid, type="ARMATURE"):
        o = obj(name, uid, type)
        self.objects.append(o)
        return o

    def test_none_in_none_out(self):
        self.assertIsNone(self.caq.make_rig_ref(None))
        self.assertIsNone(self.caq.resolve_rig_ref(None))

    def test_ref_pins_name_and_uid(self):
        rig = self.add("Rig", 11)
        ref = self.caq.make_rig_ref(rig)
        self.assertEqual((ref.name, ref.session_uid), ("Rig", 11))
        self.assertIs(self.caq.resolve_rig_ref(ref), rig)

    def test_rename_is_followed(self):
        rig = self.add("Rig", 11)
        ref = self.caq.make_rig_ref(rig)
        rig.name = "Hero"            # renamed between the click and the undo
        self.assertIs(self.caq.resolve_rig_ref(ref), rig)

    def test_rename_before_any_poll_tick(self):
        """Resolution reads the live datablock; it does not depend on any
        name registry the poll timer would have had to refresh first."""
        rig = self.add("Rig", 11)
        ref = self.caq.make_rig_ref(rig)
        rig.name = "Hero"
        self.assertIs(self.caq.resolve_rig_ref(ref), rig)   # no poll in between
        rig.name = "Hero2"
        self.assertIs(self.caq.resolve_rig_ref(ref), rig)

    def test_name_reuse_does_not_hit_the_wrong_rig(self):
        rig = self.add("Rig", 11)
        ref = self.caq.make_rig_ref(rig)
        rig.name = "Hero"
        self.add("Rig", 22)          # an unrelated armature takes the old name
        self.assertIs(self.caq.resolve_rig_ref(ref), rig)

    def test_name_swap(self):
        a, b = self.add("A", 1), self.add("B", 2)
        ref_a, ref_b = self.caq.make_rig_ref(a), self.caq.make_rig_ref(b)
        a.name, b.name = "B", "A"
        self.assertIs(self.caq.resolve_rig_ref(ref_a), a)
        self.assertIs(self.caq.resolve_rig_ref(ref_b), b)

    def test_deleted_rig_resolves_to_none(self):
        rig = self.add("Rig", 11)
        ref = self.caq.make_rig_ref(rig)
        self.objects.remove(rig)
        self.assertIsNone(self.caq.resolve_rig_ref(ref))

    def test_deleted_rig_whose_name_was_reused_is_not_redirected(self):
        rig = self.add("Rig", 11)
        ref = self.caq.make_rig_ref(rig)
        self.objects.remove(rig)
        self.add("Rig", 22)          # a different armature now has that name
        self.assertIsNone(self.caq.resolve_rig_ref(ref))   # skipped, not restored onto it

    def test_rename_then_delete(self):
        rig = self.add("Rig", 11)
        ref = self.caq.make_rig_ref(rig)
        rig.name = "Hero"
        self.objects.remove(rig)
        self.assertIsNone(self.caq.resolve_rig_ref(ref))

    def test_uids_reassigned_after_a_file_reload_are_skipped(self):
        rig = self.add("Rig", 11)
        ref = self.caq.make_rig_ref(rig)
        self.objects.clear()
        self.add("Rig", 99)          # same name, new session uid
        self.assertIsNone(self.caq.resolve_rig_ref(ref))

    def test_only_armatures_resolve(self):
        mesh = self.add("Rig", 11, type="MESH")
        self.assertIsNone(self.caq.resolve_rig_ref(self.caq.make_rig_ref(mesh)))
        self.assertIsNone(self.caq.resolve_rig_ref("Rig"))

    def test_legacy_bare_string_is_looked_up_by_name(self):
        rig = self.add("Rig", 11)
        self.assertIs(self.caq.resolve_rig_ref("Rig"), rig)
        self.assertIsNone(self.caq.resolve_rig_ref("Missing"))


if __name__ == "__main__":
    unittest.main()
