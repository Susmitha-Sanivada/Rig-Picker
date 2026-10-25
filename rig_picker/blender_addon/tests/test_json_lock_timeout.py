"""json_manager's write-lock timeout, against a REAL flock held by a second
process (POSIX only)."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import AddonTestCase  # noqa: E402

try:
    import fcntl  # noqa: F401
except ImportError:
    fcntl = None

HOLDER = (
    "import fcntl,sys,time\n"
    "f=open(sys.argv[1]+'.lock','a+b');fcntl.flock(f.fileno(),fcntl.LOCK_EX)\n"
    "print('held',flush=True);time.sleep(float(sys.argv[2]))\n"
)


@unittest.skipIf(fcntl is None, "needs POSIX flock")
class LockTimeoutTests(AddonTestCase):
    def setUp(self):
        super().setUp()
        self.jm = self.load_json_manager()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.path = os.path.join(self.tmp, "data.json")
        with open(self.path, "w") as f:
            json.dump({"marker": "other-instance"}, f)

        # Waits are short so the tests are fast.
        orig = self.jm._cross_process_lock
        self.jm._cross_process_lock = (
            lambda path, timeout=10.0, required=False: orig(path, 0.5, required)
        )
        self._orig_lock = orig

    def hold(self, path, secs=5):
        p = subprocess.Popen([sys.executable, "-c", HOLDER, path, str(secs)],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.terminate(), p.wait(), p.stdout.close()))
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def make_store(self):
        folder = os.path.join(self.tmp, "x.rigpicker")
        os.makedirs(folder)
        store = self.jm._Store("k", folder, "")
        store.data = {"settings": {"window": {}}, "armatures": {"Rig": {}}}
        store.loaded_hash = None
        self.jm._store = store
        self.jm.current_store = lambda: store
        errors = []
        self.jm.set_error_handler(errors.append)
        return store, errors

    def test_required_lock_raises_and_skips_the_body(self):
        self.hold(self.path)
        ran = False
        t = time.time()
        with self.assertRaises(self.jm.LockTimeoutError):
            with self._orig_lock(self.path, timeout=0.5, required=True):
                ran = True
        self.assertFalse(ran, "the body ran without the lock")
        self.assertGreater(time.time() - t, 0.4, "did not wait for the timeout")

    def test_read_lock_still_proceeds_after_the_timeout(self):
        self.hold(self.path)
        with self._orig_lock(self.path, timeout=0.3):
            ran = True
        self.assertTrue(ran)

    def test_required_lock_raises_when_lock_file_cannot_open(self):
        bad = os.path.join(self.tmp, "no_such_dir", "data.json")
        ran = False
        with self.assertRaises(self.jm.LockUnavailableError):
            with self._orig_lock(bad, timeout=0.5, required=True):
                ran = True
        self.assertFalse(ran, "the body ran without a lock")
        with self._orig_lock(bad, timeout=0.5):   # reads stay permissive
            ran = True
        self.assertTrue(ran)

    def test_required_lock_raises_without_locking_support(self):
        saved = self.jm.msvcrt, self.jm.fcntl
        self.jm.msvcrt = self.jm.fcntl = None
        self.addCleanup(lambda: (setattr(self.jm, "msvcrt", saved[0]),
                                 setattr(self.jm, "fcntl", saved[1])))
        ran = False
        with self.assertRaises(self.jm.LockUnavailableError):
            with self._orig_lock(self.path, timeout=0.5, required=True):
                ran = True
        self.assertFalse(ran)
        with self._orig_lock(self.path, timeout=0.5):
            ran = True
        self.assertTrue(ran)

    def test_flush_aborts_when_lock_unavailable(self):
        store, errors = self.make_store()
        saved = self.jm.msvcrt, self.jm.fcntl
        self.jm.msvcrt = self.jm.fcntl = None
        self.addCleanup(lambda: (setattr(self.jm, "msvcrt", saved[0]),
                                 setattr(self.jm, "fcntl", saved[1])))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIs(self.jm._flush(), False)
        self.assertFalse(os.path.exists(store.path), "wrote without a lock")
        self.assertTrue(any("could not be locked" in e for e in errors), errors)

    def test_free_lock_is_acquired_when_required(self):
        with self._orig_lock(self.path, timeout=0.5, required=True):
            pass

    def test_flush_aborts_keeps_memory_then_succeeds_on_retry(self):
        store, errors = self.make_store()
        holder = self.hold(store.path)
        with contextlib.redirect_stderr(io.StringIO()):   # _report_error prints a traceback
            self.assertIs(self.jm._flush(), False)
        self.assertFalse(os.path.exists(store.path), "wrote while locked")
        self.assertEqual(store.data["armatures"], {"Rig": {}}, "in-memory data lost")
        self.assertTrue(any("locked by another" in e for e in errors), errors)

        holder.terminate()
        holder.wait()
        self.assertIs(self.jm._flush(), True)
        with open(store.path) as f:
            self.assertIn("Rig", json.load(f)["armatures"])

    def test_save_quick_actions_aborts_before_merging_when_lock_is_held(self):
        store, errors = self.make_store()
        merged = []
        orig_merge = self.jm._merge_external_changes
        self.jm._merge_external_changes = lambda *a, **k: (merged.append(1), orig_merge(*a, **k))[1]
        self.hold(store.path)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIs(self.jm.save_quick_actions({"version": 1, "scenes": {}}), False)
        self.assertEqual(merged, [], "read/merge ran without the lock")
        self.assertFalse(os.path.exists(store.path), "wrote while locked")
        self.assertNotIn(self.jm.QUICK_ACTIONS_KEY, store.data, "payload applied in memory")
        self.assertTrue(any("locked by another" in e for e in errors), errors)

    def test_save_quick_actions_holds_one_lock_for_merge_decision_and_write(self):
        store, errors = self.make_store()
        lock_path = store.path + ".lock"
        seen = {}
        orig_merge = self.jm._merge_external_changes
        orig_flush = self.jm._flush

        def merge(*a, **k):
            seen["merge"] = self.jm._HELD_LOCKS.paths.get(lock_path, 0)
            return orig_merge(*a, **k)

        def flush():
            seen["flush"] = self.jm._HELD_LOCKS.paths.get(lock_path, 0)
            return orig_flush()

        self.jm._merge_external_changes = merge
        self.jm._flush = flush
        payload = {"version": 1, "scenes": {"S": {"multi_actions": []}}}
        self.assertIs(self.jm.save_quick_actions(payload), True)
        self.assertGreaterEqual(seen["merge"], 1, "merge ran without the lock")
        self.assertGreaterEqual(seen["flush"], 1, "flush ran without the lock")
        with open(store.path) as f:
            self.assertEqual(json.load(f)[self.jm.QUICK_ACTIONS_KEY], payload)
        self.assertEqual(self.jm._HELD_LOCKS.paths.get(lock_path, 0), 0)

    def test_lock_is_released_after_save_quick_actions(self):
        store, errors = self.make_store()
        self.jm.save_quick_actions({"version": 1, "scenes": {}})
        with self._orig_lock(store.path, timeout=0.5, required=True):
            pass
        probe = subprocess.run(
            [sys.executable, "-c",
             "import fcntl,sys\nf=open(sys.argv[1]+'.lock','a+b')\n"
             "fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)\nprint('free')", store.path],
            capture_output=True, text=True)
        self.assertEqual(probe.stdout.strip(), "free", probe.stderr)

    def test_nested_required_lock_in_same_thread_does_not_deadlock(self):
        t = time.time()
        with self._orig_lock(self.path, timeout=0.5, required=True):
            with self._orig_lock(self.path, timeout=0.5, required=True):
                pass
            with self._orig_lock(self.path, timeout=0.5):
                pass
        self.assertLess(time.time() - t, 0.4, "nested lock waited for its timeout")

    def test_lock_held_by_another_thread_is_not_shared(self):
        import threading
        got = []
        with self._orig_lock(self.path, timeout=0.5, required=True):
            def other():
                try:
                    with self._orig_lock(self.path, timeout=0.3, required=True):
                        got.append("entered")
                except self.jm.LockTimeoutError:
                    got.append("timeout")
            th = threading.Thread(target=other)
            th.start()
            th.join()
        self.assertEqual(got, ["timeout"])

    def test_registry_edit_aborts_while_locked(self):
        self.jm._detached = False
        self.jm.get_storage_path = lambda: self.path
        errors = []
        self.jm.set_error_handler(errors.append)
        self.hold(self.path)
        with contextlib.redirect_stderr(io.StringIO()):
            self.jm._registry_edit(lambda r: r.update(poke=1) or True)
        with open(self.path) as f:
            self.assertNotIn("poke", json.load(f), "registry written while locked")
        self.assertTrue(any("not updated" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
