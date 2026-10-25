"""_cross_process_lock() on Windows (msvcrt.locking), against a REAL lock
held by a second process. Skipped everywhere except Windows."""
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import AddonTestCase  # noqa: E402

try:
    import msvcrt
except ImportError:
    msvcrt = None

# Holds the same byte lock json_manager takes: byte 0 of <path>.lock.
HOLDER = (
    "import msvcrt,sys,time\n"
    "f=open(sys.argv[1]+'.lock','a+b')\n"
    "if f.seek(0, 2) == 0:\n"
    "    f.write(b'\\0'); f.flush()\n"
    "f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)\n"
    "print('held', flush=True); time.sleep(float(sys.argv[2]))\n"
)


@unittest.skipIf(msvcrt is None, "needs Windows msvcrt")
class WindowsLockTests(AddonTestCase):
    def setUp(self):
        super().setUp()
        self.jm = self.load_json_manager()
        self.assertIsNotNone(self.jm.msvcrt, "json_manager did not pick msvcrt")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "data.json")
        with open(self.path, "w") as f:
            f.write("{}")

    def hold(self, secs=30):
        p = subprocess.Popen([sys.executable, "-c", HOLDER, self.path, str(secs)],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.kill(), p.wait(), p.stdout.close()))
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def test_required_lock_times_out_and_skips_body(self):
        self.hold()
        ran = False
        t = time.time()
        with self.assertRaises(self.jm.LockTimeoutError):
            with self.jm._cross_process_lock(self.path, timeout=0.5, required=True):
                ran = True
        self.assertFalse(ran)
        self.assertGreater(time.time() - t, 0.4)

    def test_read_lock_proceeds_after_timeout(self):
        self.hold()
        with self.jm._cross_process_lock(self.path, timeout=0.3):
            ran = True
        self.assertTrue(ran)

    def test_lock_is_released_after_holder_exits(self):
        holder = self.hold(secs=30)
        with self.assertRaises(self.jm.LockTimeoutError):
            with self.jm._cross_process_lock(self.path, timeout=0.3, required=True):
                pass
        holder.kill()
        holder.wait()
        with self.jm._cross_process_lock(self.path, timeout=5, required=True):
            pass

    def test_lock_is_released_by_our_own_exit_of_the_with(self):
        with self.jm._cross_process_lock(self.path, timeout=1, required=True):
            pass
        # A second process can now take it immediately.
        p = subprocess.run([sys.executable, "-c", HOLDER, self.path, "0"],
                           capture_output=True, text=True, timeout=20)
        self.assertIn("held", p.stdout, p.stderr)


if __name__ == "__main__":
    unittest.main()
