"""_flush() three-way merges edits made by another instance instead of
overwriting them."""
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


class FlushMergeTests(AddonTestCase):
    def setUp(self):
        super().setUp()
        self.jm = self.load_json_manager()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = os.path.join(tmp.name, "x.rigpicker")
        os.makedirs(folder)
        self.store = self.jm._Store("k", folder, "")
        self.store.data = {"settings": {"window": {}}, "armatures": {}, "quick_actions": {"a": 1, "b": 1}}
        self.jm._store = self.store
        self.jm.current_store = lambda: self.store
        self.errors = []
        self.jm.set_error_handler(self.errors.append)
        self.assertIs(self.jm._flush(), True)

    def other_instance_writes(self, mutate):
        with open(self.store.path) as f:
            d = json.load(f)
        mutate(d)
        with open(self.store.path, "w") as f:
            json.dump(d, f)

    def test_same_mtime_and_size_still_detected(self):
        st = os.stat(self.store.path)
        with open(self.store.path) as f:
            raw = f.read()
        self.assertIn('"a": 1', raw)
        with open(self.store.path, "w") as f:
            f.write(raw.replace('"a": 1', '"a": 2'))   # same length
        os.utime(self.store.path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(os.stat(self.store.path).st_size, st.st_size)
        self.store.data["quick_actions"]["b"] = 3
        self.assertIs(self.jm._flush(), True)
        qa = self.disk()["quick_actions"]
        self.assertEqual((qa["a"], qa["b"]), (2, 3))

    def test_unchanged_file_skips_merge(self):
        self.store.data["quick_actions"]["b"] = 3
        self.assertIs(self.jm._flush(), True)
        self.assertEqual(self.disk()["quick_actions"]["b"], 3)
        self.assertEqual(self.errors, [])

    def raw(self):
        with open(self.store.path, "rb") as f:
            return f.read()

    def disk(self):
        with open(self.store.path) as f:
            return json.load(f)

    def test_edits_to_different_keys_both_survive(self):
        self.other_instance_writes(lambda d: d["quick_actions"].update(a=2))
        self.store.data["quick_actions"]["b"] = 3
        self.assertIs(self.jm._flush(), True)
        qa = self.disk()["quick_actions"]
        self.assertEqual((qa["a"], qa["b"]), (2, 3))
        self.assertEqual(self.store.data["quick_actions"], qa)
        self.assertEqual(self.errors, [])

    def test_other_instance_added_and_deleted_keys_survive(self):
        def mut(d):
            d["armatures"]["RigB"] = {"p": 1}
            del d["quick_actions"]["a"]
        self.other_instance_writes(mut)
        self.store.data["armatures"]["RigA"] = {}
        self.assertIs(self.jm._flush(), True)
        d = self.disk()
        self.assertEqual(set(d["armatures"]), {"RigA", "RigB"})
        self.assertNotIn("a", d["quick_actions"])

    def test_same_key_conflict_keeps_ours_and_reports(self):
        self.other_instance_writes(lambda d: d["quick_actions"].update(a=2))
        self.store.data["quick_actions"]["a"] = 9
        self.assertIs(self.jm._flush(), True)
        self.assertEqual(self.disk()["quick_actions"]["a"], 9)
        self.assertTrue(any("quick_actions/a" in e for e in self.errors), self.errors)

    def test_unreadable_disk_file_aborts_write(self):
        self.other_instance_writes(lambda d: None)
        with open(self.store.path, "w") as f:
            f.write("{not json")
        self.store.data["quick_actions"]["b"] = 3
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIs(self.jm._flush(), False)
        with open(self.store.path) as f:
            self.assertEqual(f.read(), "{not json")
        self.assertEqual(self.store.data["quick_actions"]["b"], 3)

    def test_failed_write_after_merge_rolls_back_current_data(self):
        from unittest import mock
        held = self.jm._load()          # a reference taken before the merge
        previous = copy.deepcopy(held["quick_actions"])

        def mut(d):
            d["armatures"]["RigB"] = {"p": 1}      # other instance, other key
            d["quick_actions"]["a"] = 2            # other instance, N-panel
        self.other_instance_writes(mut)

        payload = {"a": 1, "b": 7}                 # our edit: b 1 -> 7
        with mock.patch.object(self.jm.os, "replace", side_effect=OSError("disk full")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertIs(self.jm.save_quick_actions(payload), False)

        cur = self.store.data
        self.assertIs(cur, held, "store.data was replaced, not updated in place")
        self.assertEqual(cur["quick_actions"]["b"], 1, "our failed edit stayed in memory")
        self.assertEqual(cur["quick_actions"]["a"], 2, "other instance's edit lost")
        self.assertIn("RigB", cur["armatures"], "other instance's edit lost")
        self.assertNotEqual(cur["quick_actions"], previous)
        # Nothing reached the disk besides the other instance's own write.
        self.assertEqual(self.disk()["quick_actions"], {"a": 2, "b": 1})

        # Retry must actually write, not report "already saved".
        self.assertIs(self.jm.save_quick_actions(payload), True)
        d = self.disk()
        self.assertEqual(d["quick_actions"]["b"], 7)
        self.assertIn("RigB", d["armatures"])

    def test_failed_first_write_removes_new_quick_actions_key(self):
        from unittest import mock
        del self.store.data["quick_actions"]
        self.assertIs(self.jm._flush(), True)
        with mock.patch.object(self.jm.os, "replace", side_effect=OSError("x")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertIs(self.jm.save_quick_actions({"a": 1}), False)
        self.assertNotIn("quick_actions", self.store.data)
        self.assertIs(self.jm.save_quick_actions({"a": 1}), True)

    def test_unchanged_payload_does_not_clobber_other_instances_edit(self):
        stale = copy.deepcopy(self.store.data["quick_actions"])   # what A saw
        self.other_instance_writes(lambda d: d["quick_actions"].update(a=2))
        on_disk = self.raw()

        # A saves its unchanged snapshot: must NOT push the stale value
        # over B's edit, and must notice B's edit instead of short-circuiting.
        self.assertIs(self.jm.save_quick_actions(stale), True)
        self.assertEqual(self.raw(), on_disk,
                         "wrote although this instance changed nothing")
        self.assertEqual(self.disk()["quick_actions"]["a"], 2)
        self.assertEqual(self.jm.get_quick_actions()["a"], 2,
                         "external change was not merged into memory")

    def test_changed_payload_keeps_other_instances_edit(self):
        stale = copy.deepcopy(self.store.data["quick_actions"])
        self.other_instance_writes(lambda d: d["quick_actions"].update(a=2))
        mine = dict(stale, b=7)                     # A really changed b
        self.assertIs(self.jm.save_quick_actions(mine), True)
        qa = self.disk()["quick_actions"]
        self.assertEqual((qa["a"], qa["b"]), (2, 7))

    def test_equal_payload_without_external_change_is_still_a_noop(self):
        same = copy.deepcopy(self.store.data["quick_actions"])
        before = self.raw()
        self.assertIs(self.jm.save_quick_actions(same), True)
        self.assertEqual(self.raw(), before)

    def test_unreadable_changed_file_is_not_reported_as_saved(self):
        same = copy.deepcopy(self.store.data["quick_actions"])
        with open(self.store.path, "w") as f:
            f.write("{not json")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIs(self.jm.save_quick_actions(same), False)
        with open(self.store.path) as f:
            self.assertEqual(f.read(), "{not json")

    def test_deleted_file_is_restored_not_reported_as_saved(self):
        same = copy.deepcopy(self.store.data["quick_actions"])
        os.remove(self.store.path)
        self.assertIs(self.jm.save_quick_actions(same), True)
        self.assertTrue(os.path.exists(self.store.path), "file not restored")
        self.assertEqual(self.disk()["quick_actions"], same)
        self.assertTrue(any("deleted" in e for e in self.errors), self.errors)
        self.assertEqual(len([e for e in self.errors if "deleted" in e]), 1)
        # Back in sync: an equal payload is a no-op again, no new report.
        self.assertIs(self.jm.save_quick_actions(same), True)
        self.assertEqual(len([e for e in self.errors if "deleted" in e]), 1)

    def test_deleted_file_failed_restore_is_not_success(self):
        from unittest import mock
        same = copy.deepcopy(self.store.data["quick_actions"])
        os.remove(self.store.path)
        with mock.patch.object(self.jm.os, "replace", side_effect=OSError("x")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertIs(self.jm.save_quick_actions(same), False)
        self.assertFalse(os.path.exists(self.store.path))
        self.assertIs(self.jm.save_quick_actions(same), True)    # retry restores
        self.assertTrue(os.path.exists(self.store.path))

    def test_success_is_never_reported_while_data_json_is_missing(self):
        """Regression: after a successful save, data.json is deleted and the
        SAME payload is saved again. It used to return True (payload equals
        the in-memory value) with the file still missing."""
        from unittest import mock
        payload = {"a": 5, "b": 6}
        self.assertIs(self.jm.save_quick_actions(payload), True)
        self.assertEqual(self.disk()["quick_actions"], payload)

        os.remove(self.store.path)
        self.assertFalse(os.path.exists(self.store.path))

        # Restoring is impossible (write fails): must not claim success,
        # and the file must still be missing.
        with mock.patch.object(self.jm.os, "replace", side_effect=OSError("x")), \
                contextlib.redirect_stderr(io.StringIO()):
            ok = self.jm.save_quick_actions(copy.deepcopy(payload))
        self.assertIs(ok, False)
        self.assertFalse(os.path.exists(self.store.path))

        # Restoring works: success is reported only together with the file.
        ok = self.jm.save_quick_actions(copy.deepcopy(payload))
        self.assertIs(ok, True)
        self.assertTrue(os.path.exists(self.store.path))
        self.assertEqual(self.disk()["quick_actions"], payload)

    def test_never_existing_file_is_not_reported_as_deleted(self):
        os.remove(self.store.path)
        self.store.loaded_hash = None            # as for a store never written
        self.assertIs(self.jm._flush(), True)
        self.assertFalse(any("deleted" in e for e in self.errors), self.errors)

    def test_merge3_unit(self):
        m = self.jm._merge3
        c = []
        self.assertEqual(m({"x": {"a": 1, "b": 1}}, {"x": {"a": 2, "b": 1}},
                           {"x": {"a": 1, "b": 5}}, conflicts=c),
                         {"x": {"a": 2, "b": 5}})
        self.assertEqual(c, [])
        self.assertEqual(m({"l": [1]}, {"l": [2]}, {"l": [3]}, conflicts=c), {"l": [2]})
        self.assertEqual(c, ["l"])


if __name__ == "__main__":
    unittest.main()
