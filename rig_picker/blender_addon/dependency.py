"""
dependency.py

Responsible for preparing the Qt environment.

This module is the ONLY place that knows how to:

- Check/install PySide6
- Check BQT
- Initialize BQT
- Return the QApplication
"""

import importlib.util
import importlib.metadata
import subprocess
import sys
import time
import contextlib

import bpy

from pathlib import Path

import zipfile
import shutil

try:
    import fcntl  # POSIX (Linux/macOS)
except ImportError:
    fcntl = None

try:
    import msvcrt  # Windows
except ImportError:
    msvcrt = None


# Pinned, not left to float to whatever's newest on PyPI. PySide6 only
# reaches PyPI after Qt's own release, and brand-new Qt/shiboken6 builds
# have repeatedly shown first-import native crashes specifically when
# embedded inside a host interpreter like Blender's (rather than run as
# a normal standalone application) - see e.g. the Blender project's own
# "PySide6 shiboken6 conflict" tracker issue. Pinning to a release
# that's had time to prove itself in this exact embedded-Qt-in-Blender
# scenario, rather than always grabbing the latest, avoids installing
# straight into a known-bad combination on a fresh machine. Bump this
# deliberately (and re-test in Blender) rather than letting it float -
# has_pyside() compares the installed version against this constant and
# triggers a clean reinstall on any mismatch, so bumping it here is
# enough to roll it out to everyone on next launch.
PYSIDE6_VERSION = "6.8.3"


# ----------------------------------------------------------
# Utilities
# ----------------------------------------------------------

def blender_python():

    return Path(sys.executable)


def pip_install(*packages, target=None):
    """ensurepip + pip self-upgrade, then `pip install packages` -
    optionally into `target` (a --target dir) rather than the default
    site-packages. The one place in this module that actually shells
    out to pip; install_pyside() below calls this instead of repeating
    the same three subprocess.check_call()s inline."""

    python = blender_python()

    subprocess.check_call([
        str(python),
        "-m",
        "ensurepip",
        "--upgrade",
    ])

    subprocess.check_call([
        str(python),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
    ])

    install_cmd = [
        str(python),
        "-m",
        "pip",
        "install",
        *packages,
    ]

    if target is not None:
        install_cmd += ["--target", str(target)]

    subprocess.check_call(install_cmd)


# ----------------------------------------------------------
# PySide6
# ----------------------------------------------------------

def has_pyside():
    """True only if PySide6 is installed AND its version matches
    PYSIDE6_VERSION.

    A version mismatch - e.g. this addon gets updated later to pin a
    newer PySide6, but the user's machine still has an older one from
    a previous install - is treated the same as "not installed" so
    ensure_qt_dependencies() re-runs install_pyside(). That function
    already clears out its own files for a clean reinstall (see the
    comment in install_pyside() about mismatched native Qt/shiboken
    binaries) - previously that wipe-and-reinstall logic only ever ran
    on a genuinely first install, since this function returned True for
    ANY installed version forever after, with no way to ever pick up a
    future pin bump.
    """

    add_python_path()

    if importlib.util.find_spec("PySide6") is None:
        return False

    try:
        installed_version = importlib.metadata.version("PySide6")
    except importlib.metadata.PackageNotFoundError:
        # find_spec() resolved a module but there's no distribution
        # metadata for it - a broken/partial install (interrupted
        # extraction, hand-deleted dist-info, etc). Treat it as missing
        # rather than silently trusting an unknown version.
        return False

    return installed_version == PYSIDE6_VERSION





def install_pyside():

    target = python_folder()

    # Always install into a clean folder, not on top of whatever's
    # already there. `pip install --upgrade --target` layers new files
    # over old ones rather than removing files an older version had
    # that the new one doesn't - across several upgrades this can leave
    # DLLs/pyd files from different PySide6 releases coexisting in the
    # same folder. Mismatched native Qt/shiboken binaries loaded
    # together are a well-known cause of an access violation crash the
    # instant QtCore is first imported (Blender crashing on startup with
    # a fault inside shiboken6.abi3.dll/QtCore.pyd, before any of the
    # addon's own UI code runs, is the signature of exactly this).
    #
    # "Clean" means clean of PySide6's own files specifically, not the
    # whole folder: BQT lives in this same shared directory (see
    # bqt_folder() = python_folder() / "bqt", and add_python_path() -
    # sys.path only ever gets THIS one folder added to it, so every
    # package has to be a direct child of it rather than its own
    # separate location). Wiping target outright would also delete an
    # already-installed, perfectly good BQT any time PySide6 alone
    # needs a reinstall (e.g. a PYSIDE6_VERSION bump), for no reason
    # other than the two happening to share a folder.
    if target.exists():
        for entry in target.iterdir():
            if entry.name == "bqt":
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    target.mkdir(parents=True, exist_ok=True)

    # Pinned, not "--upgrade" to whatever is newest on PyPI - see the
    # PYSIDE6_VERSION module-level comment above for why.
    pip_install(
        f"PySide6=={PYSIDE6_VERSION}",
        f"shiboken6=={PYSIDE6_VERSION}",
        "blender-qt-stylesheet",
        "qtpy",
        target=target,
    )

    # The files on disk just changed out from under any earlier
    # find_spec()/metadata lookups (this matters most for the
    # version-mismatch reinstall path added to has_pyside() - without
    # this, a stale finder cache could keep reporting the OLD version
    # immediately after we just installed the new one).
    importlib.invalidate_caches()

    add_python_path()


# ----------------------------------------------------------
# BQT
# ----------------------------------------------------------

def has_bqt():

    add_python_path()

    return (
        importlib.util.find_spec("bqt")
        is not None
    )


# ----------------------------------------------------------
# Initialization
# ----------------------------------------------------------

_BQT_INITIALIZED = False


def _patch_bqt_quit_save_dialogue():
    """Fixes a bug in vendored BQT (techartorg/bqt) where clicking "Save"
    in its own close-confirmation popup saves the file but never actually
    quits Blender - the window just sits there until the close button is
    clicked a second time.

    bqt.ui.quit_dialogue.shutdown_blender() is registered as a
    bpy.app.handlers.save_post callback the moment "Save" is clicked:

        bpy.app.handlers.save_post.append(shutdown_blender)

    Blender calls every save_post handler with positional arguments (the
    scene, and on newer Blender versions a depsgraph too), and
    shutdown_blender blindly forwards whatever it was called with
    straight into:

        bpy.ops.wm.quit_blender(*args, **kwargs)

    wm.quit_blender doesn't accept positional arguments like a Scene, so
    that call raises - inside the bpy.app.timers one-shot callback
    quit_blender_from_main_thread() schedules to make it, so the
    exception never surfaces anywhere but the console, and the quit
    silently never happens. The save itself already completed by then,
    so the popup closes looking like everything worked. Clicking close
    again then works fine, because with the file now clean,
    shutdown_blender() is instead called directly with no arguments at
    all (see BlenderClosingDialog.execute()'s not-dirty branch) - which
    is exactly why only the *second* click actually quits.

    Patched here to never forward save_post's own arguments into
    wm.quit_blender, rather than editing the vendored copy in place -
    this way the fix also applies to any BQT folder a user already has
    extracted on disk from before this fix existed (install_bqt() only
    ever re-extracts vendor/bqt.zip if that folder is missing).
    """
    from bqt.ui import quit_dialogue

    if getattr(quit_dialogue.shutdown_blender, "_rig_picker_patched", False):
        return

    def _shutdown_blender(*_args, **_kwargs):
        if bpy.context.preferences.use_preferences_save:
            bpy.ops.wm.save_userpref()
        quit_dialogue.quit_blender_from_main_thread()

    _shutdown_blender._rig_picker_patched = True
    quit_dialogue.shutdown_blender = _shutdown_blender


def initialize_bqt():
    global _BQT_INITIALIZED

    add_python_path()

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()

    if app is not None and _BQT_INITIALIZED:
        return app

    if app is None and _BQT_INITIALIZED:
        # BQT was registered earlier this session, but the QApplication
        # it created is gone (destroyed/lost since). Previously this
        # fell through both branches below - `not _BQT_INITIALIZED` was
        # False, so nothing re-ran - and hit the "Unable to initialize
        # BQT" error on every later call, permanently, until Blender was
        # restarted. Clear the flag so the registration path below runs
        # again and recreates the app (including the blender_widget
        # attribute launcher.py relies on, which a bare QApplication
        # wouldn't have).
        _BQT_INITIALIZED = False

    if not _BQT_INITIALIZED:
        import bqt
        try:
            bqt.register()
        except Exception as e:
            raise RuntimeError(
                f"Unable to initialize BQT: {e}. If this happened after "
                f"the Qt application was lost mid-session, restart "
                f"Blender."
            ) from e
        try:
            _patch_bqt_quit_save_dialogue()
        except Exception:
            # Best-effort - BQT's internal module layout could change in
            # a future vendored version. Worse case here is falling back
            # to the original bug (a second click needed to quit), not a
            # broken picker, so this must never block initialization.
            pass
        _BQT_INITIALIZED = True

    app = QApplication.instance()

    if app is None:
        raise RuntimeError("Unable to initialize BQT.")

    return app


# ----------------------------------------------------------
# Public API
# ----------------------------------------------------------

_INSTALL_LOCK_TIMEOUT = 600  # seconds - a first-time install can be slow.


@contextlib.contextmanager
def _cross_process_install_lock(timeout=_INSTALL_LOCK_TIMEOUT):
    """Cross-process lock serializing PySide6/BQT installation, via a
    dedicated lock file next to the addon (NOT inside python_folder() -
    install_pyside() below clears that folder out on a reinstall, and
    unlinking/rewriting a lock file out from under its own open handle
    is asking for trouble, especially on Windows).

    Without this, two separate Blender processes both doing first-time
    setup at once (or one needing a PySide6 version-bump reinstall while
    another is mid-install) would race pip/zipfile writes into the same
    target folder - genuinely corruptible (interleaved writes into the
    same files, not just "whichever finishes last wins"), the same class
    of bug json_manager.py's own cross-process lock guards against for
    rig_picker_data.json.

    Unlike that lock, this one does NOT give up and proceed unguarded on
    timeout: an install is not a single atomic operation the way a JSON
    write is, so racing it is exactly the failure mode this exists to
    prevent. It raises instead, so the caller (ensure_qt_dependencies(),
    called from operators.py's background thread) reports a clear error
    rather than silently risking a half-overwritten install.
    """
    lock_path = addon_root() / ".install.lock"
    try:
        f = open(lock_path, "a+b")
    except OSError:
        # Can't even create the lock file (e.g. read-only install
        # location) - proceed unguarded rather than blocking every
        # install attempt outright.
        yield
        return

    try:
        if not (msvcrt or fcntl):
            yield
            return

        if msvcrt is not None and lock_path.stat().st_size == 0:
            # msvcrt.locking() locks a byte range that must already
            # exist in the file.
            f.write(b"\0")
            f.flush()

        acquired = False
        deadline = time.time() + timeout
        while not acquired:
            try:
                if msvcrt is not None:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                if time.time() >= deadline:
                    raise RuntimeError(
                        "Timed out waiting for another Blender instance "
                        "to finish installing Rig Picker's Qt "
                        "dependencies. If no other instance is actually "
                        "installing right now, delete the stale lock "
                        f"file at {lock_path} and try again."
                    )
                time.sleep(0.2)

        try:
            yield
        finally:
            try:
                if msvcrt is not None:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        f.close()


def ensure_qt_dependencies():
    """Installs PySide6/shiboken6 and BQT via pip if either is missing.

    Deliberately split out from ensure_qt() below: this half only shells
    out to pip (subprocess.check_call + zip extraction), never imports
    PySide6 and never touches QApplication - both of which are only
    legal on Blender's main thread. That makes this function safe to run
    on a background thread, which is exactly what operators.py does for
    it, since a first-time install downloads/unpacks tens of MB and
    would otherwise freeze Blender's UI with no feedback.

    The has_pyside()/has_bqt() checks and their installs run under
    _cross_process_install_lock() as one critical section, not just the
    installs alone - otherwise two processes could both pass the "not
    installed yet" check before either started installing.
    """

    with _cross_process_install_lock():

        if not has_pyside():

            install_pyside()

        if not has_bqt():

            install_bqt()


def ensure_qt():
    """Ensures PySide6/BQT are installed (fast no-op if they already
    are) and returns the initialized QApplication.

    Must be called on the main thread - unlike ensure_qt_dependencies()
    above, this imports PySide6 and creates/registers the QApplication.
    """

    ensure_qt_dependencies()

    return initialize_bqt()

def addon_root():

    return Path(__file__).parent


def vendor_folder():

    return addon_root() / "vendor"


def bqt_zip():

    return vendor_folder() / "bqt.zip"


def bqt_folder():

    return python_folder() / "bqt"


def install_bqt():

    target = bqt_folder()

    # A specific expected file, not just the folder existing. If
    # Blender crashes or is force-closed mid zipfile.extractall() below,
    # target already exists (extraction creates directory entries before
    # the files inside them) but is incomplete - checking only
    # target.exists() would make every future launch skip re-extracting
    # forever, leaving BQT permanently broken with no way to recover
    # short of the user manually deleting the folder.
    marker = target / "__init__.py"

    if target.exists() and marker.exists():
        return

    if target.exists():
        shutil.rmtree(target)

    with zipfile.ZipFile(
        bqt_zip(),
        "r",
    ) as z:

        z.extractall(
            python_folder()
        )

    importlib.invalidate_caches()


def python_folder():

    folder = addon_root() / "python"

    folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    return folder


def add_python_path():

    folder = str(python_folder())

    if folder not in sys.path:

        sys.path.insert(
            0,
            folder,
        )

