"""
json_manager.py

Single source of truth for reading/writing Rig Picker data on disk.

Storage layout (<file>.rigpicker/data.json):
{
    "settings": {
        "window": { "width": ..., "height": ..., "x": ..., "y": ... }
    },
    "armatures": {
        "ArmatureName": {
            "panels": {
                "Panel 1": {
                    "background": "C:/.../capture.png",
                    "symmetry": false,
                    "symmetry_x": -1.0,
                    "items": [
                        {
                            "bone_name": "hand.L",
                            "label": "hand.L",
                            "x": 120.0,
                            "y": 84.0,
                            "control_size": 18,
                            "control_shape": "CIRCLE",
                            "control_color": "GREEN"
                        },
                        ...
                    ]
                },
                "Face": { ... },
                ...
            }
        },
        ...
    }
}

Picker data is scoped per (rig, panel) pair, not just per rig - each panel
on a rig (Manage Panels in the N-panel: Face, Body, ...) keeps its own
independent set of controls, background capture and symmetry setting, the
same way two different rigs already kept independent data before panels
existed. A rig's entry is now a small wrapper around a "panels" dict rather
than the picker fields living directly on it.

Global (non-per-rig) settings live under their own top-level "settings"
key, in a namespace completely separate from "armatures" - NOT as a
same-level sibling to armature entries the way an earlier version of this
file did (a reserved "_settings" key living directly alongside
"ArmatureName" keys). That older layout meant an armature a user happened
to name "_settings" would collide with - and get silently merged into -
the real settings entry the moment its picker data was touched, corrupting
the saved window size/position and raising a KeyError on the next save.
Nesting every armature strictly under "armatures" makes that collision
structurally impossible: no armature name, however chosen, can ever land
on the same key as "settings". _migrate_top_level() below upgrades an
old-format file (or the corrupted state such a collision could have left
behind) to this shape on first load.

Old per-armature files (from before panels existed, or before this
per-panel scoping was added) store a rig's picker fields directly on its
entry instead of nested under "panels" - _migrate_armature_entry() below
detects that shape on first load and wraps it into a single "Panel 1"
entry, so nothing is lost.

No Blender Scene properties, no .blend storage - picker data lives on
disk, one folder PER .blend file, next to that file:

    P2M_REGULAR.blend
    P2M_REGULAR.rigpicker/
        data.json        (this layout, plus a top-level "version" field)
        data.json.bak / data.json.lock
        images/          (captured-view PNGs)

A rig named "Hero" in two different .blend files therefore has two
completely independent layouts. Each Blender process works on ONE
"store" (one data.json + folder) at a time, always resolved through
current_store(), which follows bpy.data.filepath on its own - see the
STORES section below. The addon-folder rig_picker_data.json is no longer
a data file: it is only a registry of .blend files and their picker
folders (see TRACKED .BLEND FILES).

On disk, a panel's "background" is stored relative to the store folder
("images/<name>.png") so a .blend and its .rigpicker folder can be moved
together; in memory it is always an absolute path (callers compare it as
a raw string), converted on load and on write.
"""

import os
import re
import sys
import time
import json
import hashlib
import copy
import contextlib
import threading
import traceback

try:
    import fcntl  # POSIX (Linux/macOS)
except ImportError:
    fcntl = None

try:
    import msvcrt  # Windows
except ImportError:
    msvcrt = None

# Registry file (in the addon's parent folder): only lists .blend files and
# their picker folders, never picker data. See TRACKED .BLEND FILES.
FILE_NAME = "rig_picker_data.json"

# Per-file store layout (see STORES).
STORE_FOLDER_SUFFIX = ".rigpicker"
STORE_FILE_NAME = "data.json"
IMAGES_FOLDER_NAME = "images"

# Schema version written into every data.json. A file written by a newer
# addon (higher number) is still read, but the user is warned once.
DATA_VERSION = 1

# The one store this Blender process is working on (see current_store()).
_store = None
_warned_newer_version = set()

# Optional callback(message: str), registered by the UI layer once a Qt
# window exists (see ui/launcher.py), invoked whenever a load or save
# silently fails so the user actually finds out - instead of a disk-full/
# locked-file/permissions error just vanishing. This module stays
# Qt-agnostic (it's the storage layer, used before Qt is even
# initialized), so it never imports Qt itself; it only calls this hook.
_error_handler = None


def set_error_handler(callback):
    """Registers `callback(message)` to be called whenever a read/write to
    rig_picker_data.json fails. Safe to call more than once - the latest
    registration wins. Entirely optional: failures are always printed to
    the console (with a full traceback when one exists) regardless of
    whether a handler is registered."""
    global _error_handler
    _error_handler = callback


def _report_error(message):
    """Reports a storage FAILURE: prints the current exception's traceback
    (so nothing fails with zero trace, unlike before) and best-effort
    forwards `message` to whatever the UI layer registered via
    set_error_handler().

    Meant to be called from an `except` block. If it is called with no
    exception being handled, there is no traceback to print
    (traceback.print_exc() would emit a bare "NoneType: None"), so the
    message itself goes to the console instead. Conditions that are
    expected rather than failures - merge conflicts, a file deleted from
    outside, a refused write location - should call _notify_user()
    directly rather than relying on that fallback.
    """
    if sys.exc_info()[0] is not None:
        traceback.print_exc()
    else:
        print(message)
    if _error_handler is not None:
        try:
            _error_handler(message)
        except Exception:
            traceback.print_exc()


def _notify_user(message):
    """Best-effort heads-up through the UI layer's error handler, for
    situations that aren't failures (no traceback to print) but that the
    user should know about."""
    print(message)
    if _error_handler is not None:
        try:
            _error_handler(message)
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------
# STORAGE LOCATION
# ---------------------------------------------------------

def get_storage_path():
    """Returns the full path to the registry file rig_picker_data.json
    (the list of .blend files; NOT picker data - that lives in each
    file's own .rigpicker folder, see current_store()).

    Requested location:
        %APPDATA%\\Blender Foundation\\Blender\\5.0\\scripts\\addons\\rig_picker_data.json

    Falls back to Blender's own user scripts/addons folder on platforms
    where APPDATA isn't defined (macOS/Linux), so the addon still works
    there instead of crashing.
    """

    appdata = os.getenv("APPDATA")

    if appdata:
        folder = os.path.join(
            appdata,
            "Blender Foundation",
            "Blender",
            "5.0",
            "scripts",
            "addons",
        )
    else:
        import bpy
        folder = bpy.utils.user_resource('SCRIPTS', path="addons")

    os.makedirs(folder, exist_ok=True)

    return os.path.join(folder, FILE_NAME)


# ---------------------------------------------------------
# STORES (one data folder per .blend file)
# ---------------------------------------------------------

class _Store:
    """One data.json plus its folder, loaded in memory. `blend_key` is the
    normalized path of the .blend it belongs to ("" for an unsaved file);
    `blend_path` is its absolute path ("" if unsaved)."""

    def __init__(self, blend_key, folder, blend_path=""):
        self.blend_key = blend_key
        self.blend_path = blend_path
        # True once this store's folder is listed in the registry.
        self.registered = False
        self.folder = folder
        self.path = os.path.join(folder, STORE_FILE_NAME)
        self.images_folder = os.path.join(folder, IMAGES_FOLDER_NAME)
        self.data = None
        # Content fingerprint (SHA-256 of the bytes) of data.json when this
        # process last read/wrote it (None if it didn't exist or couldn't
        # be read) - see _flush(). A hash, not an mtime: timestamps can
        # collide or be coarse, so different contents may share one.
        self.loaded_hash = None
        # Deep copy of `data` as of the last read/write of data.json: the
        # common ancestor for the three-way merge in _flush().
        self.base = None


def _blend_filepath():
    """Path of the .blend open in this Blender process, or "" if unsaved
    (or if bpy.data can't be read right now, e.g. in a restricted
    context). Isolated in one function so tests can replace it."""
    try:
        import bpy
        return bpy.data.filepath or ""
    except Exception:
        return ""


def _temp_roots():
    import tempfile
    roots = [tempfile.gettempdir()]
    try:
        import bpy
        if bpy.app.tempdir:
            roots.append(bpy.app.tempdir)
    except Exception:
        pass
    return roots


def _is_in_temp(path):
    """True if `path` lies inside the OS or Blender temp folder (e.g. a
    file recovered from an autosave) - such a file is treated as unsaved,
    so no data folder is ever created in the temp directory."""
    key = _same_path_key(path)
    for root in _temp_roots():
        root_key = _same_path_key(root)
        if key == root_key or key.startswith(root_key + os.sep):
            return True
    return False


def _resolve_location():
    """Returns (blend_key, folder, blend_path) for the file open right now
    (blend_path is "" for an unsaved file).

    Saved file:  <blend folder>/<blend name>.rigpicker
    Unsaved file (or one inside a temp folder): a temp folder with the
    process id in its name, so two unsaved Blender instances never share
    one.
    """
    import tempfile
    path = _blend_filepath()
    if path and not _is_in_temp(path):
        full = os.path.abspath(path)
        stem = os.path.splitext(os.path.basename(full))[0]
        folder = os.path.normpath(
            os.path.join(os.path.dirname(full), stem + STORE_FOLDER_SUFFIX)
        )
        return _same_path_key(full), folder, full

    folder = os.path.normpath(os.path.join(
        tempfile.gettempdir(),
        f"rig_picker_unsaved_{os.getpid()}{STORE_FOLDER_SUFFIX}",
    ))
    return "", folder, ""


def _is_picker_folder_path(folder):
    """True if `folder` is named like a picker data folder (*.rigpicker).
    data.json, its .bak and its .lock may only ever be written inside such
    a folder - never next to the .blend or anywhere else."""
    return os.path.basename(os.path.normpath(folder)).lower().endswith(
        STORE_FOLDER_SUFFIX)


def current_store():
    """The store for the file this Blender process has open.

    Self-correcting: every call compares the bound store with the file
    that is open right now (bpy.data.filepath) and rebinds if they differ.
    So it stays right even when no handler ran - the addon enabled while a
    file is already open, Reload Scripts, a Save As, handler order.
    Rebinding is cheap (data loads lazily on first use), and nothing is
    lost by it: every change is written to disk immediately.
    """
    global _store
    key, folder, blend_path = _resolve_location()
    if _store is None or _store.blend_key != key or _store.folder != folder:
        _store = _Store(key, folder, blend_path)
    return _store


# ---------------------------------------------------------
# WINDOW BINDING + WRITE INVARIANT
# ---------------------------------------------------------
# While a picker window is open it is BOUND to the file it was opened
# from (bind_window()). Every write to a store first checks that the file
# Blender has open right now is still that file; if not, nothing is
# written and the user is told once. So a late timer, or a handler that
# didn't run, can never put one file's layout into another file's folder.
#
# During a file switch the dispatcher detaches (load_pre) and swaps the
# store (load_post); writes are refused in between.

_bound_identity = None   # (blend_key, folder) the open window belongs to
_detached = False
_last_refusal = None


def _identity():
    key, folder, _ = _resolve_location()
    return (key, folder)


def bind_window():
    """Binds the picker window to the file open right now."""
    global _bound_identity, _last_refusal
    _bound_identity = _identity()
    _last_refusal = None


def unbind_window():
    """The picker window was closed: nothing is bound any more, so writes
    from the N-panel (no window) are never refused."""
    global _bound_identity
    _bound_identity = None


def rebind_window():
    """Re-points an open window at the file open right now. For the
    moments the file really did change under it (a load, or a save that
    gave the file a new name). Does nothing if no window is bound."""
    global _bound_identity, _last_refusal
    if _bound_identity is not None:
        _bound_identity = _identity()
        _last_refusal = None


def is_window_bound():
    return _bound_identity is not None


def detach():
    """Refuse every write until the next swap_store(). Called at load_pre,
    after the outgoing file's pending work has been settled and flushed."""
    global _detached
    _detached = True


def is_detached():
    """True between detach() and the next swap_store()/reload()."""
    return _detached


def swap_store():
    """load_post step: forget the old file's store (the next access
    resolves the newly opened one), lift the detach, and re-point the open
    window, if any, at the new file."""
    global _detached
    reload()
    _detached = False
    rebind_window()


def get_bound_file_label():
    """Short name of the file the current store belongs to, for the
    window title ('unsaved' if it has no file yet)."""
    store = current_store()
    if store.blend_path:
        return os.path.basename(store.blend_path)
    return "unsaved"


def _write_allowed(report=True):
    """The write invariant. False means: do not write."""
    global _last_refusal
    if _detached:
        print("Rig Picker: write skipped (no store is active right now).")
        return False
    if _bound_identity is None:
        return True
    current = _identity()
    if current == _bound_identity:
        return True
    if report and _last_refusal != (_bound_identity, current):
        _last_refusal = (_bound_identity, current)
        _notify_user(
            "Rig Picker: the picker window was opened for a different "
            "file than the one Blender has open now, so nothing was "
            "saved. Close and reopen the picker to continue with this "
            "file."
        )
    return False


# ---------------------------------------------------------
# FIRST SAVE / SAVE AS
# ---------------------------------------------------------
# A file's data folder is named after the .blend, so a first save (the file
# had no name yet) or a Save As (new name) changes which folder the data
# belongs to. handle_file_saved() runs from save_post:
#   * first save   - the unsaved file's temp folder is MOVED next to the
#                    .blend;
#   * Save As      - the old file's folder is COPIED to the new name (the
#                    old file keeps its own);
#   * Save / Save Copy - the file's path doesn't change, nothing to do.
# The destination is replaced if it already exists: the .blend itself is
# being replaced, so a folder left over from the previous file of that
# name is stale. Callers then re-point their in-memory absolute paths with
# translate_path().

def translate_path(path, old_folder, new_folder):
    """`path` moved from old_folder to new_folder if it lies inside
    old_folder, else unchanged. Same string form get_image_path() builds,
    so the controller's raw-string comparisons keep working."""
    if path and isinstance(path, str) and os.path.isabs(path) \
            and _is_inside(path, old_folder):
        return os.path.join(new_folder, os.path.relpath(path, old_folder))
    return path


def _replace_folder(src, dest, move):
    """Puts `src`'s contents at `dest` (moving or copying), replacing an
    existing `dest`. The old destination is set aside first and restored if
    anything fails, so a failed replace never leaves it half-deleted."""
    import shutil
    aside = None
    if os.path.exists(dest):
        aside = f"{dest}.replaced.{os.getpid()}"
        shutil.rmtree(aside, ignore_errors=True)
        os.replace(dest, aside)
    try:
        if move:
            shutil.move(src, dest)
        else:
            shutil.copytree(
                src, dest,
                ignore=shutil.ignore_patterns("*.lock", "*.tmp"),
            )
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        if aside:
            os.replace(aside, dest)
        raise
    if aside:
        shutil.rmtree(aside, ignore_errors=True)
    # A moved folder can carry the old lock/temp files along.
    for name in os.listdir(dest) if os.path.isdir(dest) else ():
        if name.endswith(".lock") or name.endswith(".tmp"):
            try:
                os.remove(os.path.join(dest, name))
            except OSError:
                pass


def handle_file_saved():
    """save_post step 1. Moves (first save) or copies (Save As) the data
    folder to match the file's new name. Returns (old_folder, new_folder)
    if the data now lives in a different folder, so in-memory absolute
    paths can be re-pointed with translate_path(); otherwise None. Never
    raises: a failure is reported through the error handler and the
    session carries on in memory (the next write retries)."""
    global _store
    old = _store
    if old is None:
        return None
    key, folder, blend_path = _resolve_location()
    if old.blend_key == key and old.folder == folder:
        return None                                    # plain Save, Save Copy

    new_store = _Store(key, folder, blend_path)
    result = None
    try:
        if not blend_path:
            # Saved file turned into an unsaved one (not a Blender flow
            # today): nothing sensible to move.
            pass
        elif os.path.isdir(old.folder):
            _replace_folder(old.folder, folder, move=(old.blend_key == ""))
            result = (old.folder, folder)
        else:
            # Nothing was ever stored for the old file. If the new name has
            # a stale folder of an earlier file, it is replaced as well.
            if os.path.isdir(folder):
                import shutil
                shutil.rmtree(folder)
    except Exception as e:
        _report_error(
            f"Rig Picker: couldn't move the picker data to {folder} ({e}). "
            f"This session continues in memory; the data will be written "
            f"there on the next change if the location becomes writable."
        )
        if old.data is not None:
            # Keep working with what is in memory; _flush() retries.
            new_store.data = old.data
            new_store.base = old.base
        result = None

    _store = new_store
    if result is not None and blend_path:
        register_ui_data_folder(blend_path, folder)
        new_store.registered = True
    return result


def _is_inside(path, folder):
    return _same_path_key(path).startswith(_same_path_key(folder) + os.sep)


def _background_to_disk(path, folder):
    """In-memory absolute path -> what is written: relative to the store
    folder (forward slashes) if it lies inside it, otherwise unchanged."""
    if path and os.path.isabs(path) and _is_inside(path, folder):
        return os.path.relpath(path, folder).replace(os.sep, "/")
    return path


def _background_from_disk(path, folder):
    """What is on disk -> absolute path for memory. Absolute paths pass
    through unchanged."""
    if path and not os.path.isabs(path):
        return os.path.normpath(os.path.join(folder, *path.split("/")))
    return path


def _each_panel(data):
    for armature in data.get("armatures", {}).values():
        if not isinstance(armature, dict):
            continue
        for panel in armature.get("panels", {}).values():
            if isinstance(panel, dict):
                yield panel


def _to_disk(store):
    """Deep copy of the store's data in its on-disk form (relative
    backgrounds, version field first). The in-memory data is untouched."""
    import copy
    body = copy.deepcopy(store.data)
    version = body.pop("version", DATA_VERSION)
    out = {"version": version}
    out.update(body)
    for panel in _each_panel(out):
        if panel.get("background"):
            panel["background"] = _background_to_disk(
                panel["background"], store.folder)
    return out


def _from_disk(data, folder):
    """Converts a freshly loaded dict to its in-memory form, in place."""
    for panel in _each_panel(data):
        if panel.get("background"):
            panel["background"] = _background_from_disk(
                panel["background"], folder)
    return data


# ---------------------------------------------------------
# PER-PANEL IMAGE STORAGE
# ---------------------------------------------------------

def get_images_folder(create=False):
    """The images/ folder of the current store - one persistent
    captured-view image per (rig, panel) pair, inside the file's own
    .rigpicker folder (not the OS temp folder, which can be cleared at
    any time). Only created on disk when `create` is True (and the write
    invariant allows it), so merely asking for a path never leaves a
    folder behind."""
    folder = current_store().images_folder
    if create and _write_allowed(report=False):
        os.makedirs(folder, exist_ok=True)
    return folder


# Cap on the human-readable prefix _sanitize() keeps, in characters.
# get_image_path() combines two sanitized names (rig + panel) into one
# filename: "<rig>__<panel>.png". With no cap, a very long rig or panel
# name could push that filename - appended to an already-long nested
# AppData path - past Windows' 260-char MAX_PATH, causing capture saves
# to silently fail with an OSError. 40 chars per side keeps the whole
# filename comfortably bounded (2 * (40 + 1 + 8) + len("__.png") <= ~104
# chars) while still leaving the name recognizable.
_SANITIZE_MAX_PREFIX_LEN = 40


def _sanitize(name):
    """Filesystem-safe version of `name`, truncated to a bounded length,
    with a short hash of the ORIGINAL (untruncated, unsanitized) name
    appended.

    The replace-everything-with-`_` step alone isn't collision-safe: two
    differently-named rigs or panels that differ only in punctuation
    (e.g. "Face!" and "Face?", or "A/B" and "A B") sanitize to the exact
    same string and would land on the same image file - whichever one
    saves second silently overwrites the first's captured background.
    Appending a hash of the original name keeps that from happening even
    when the sanitized prefix collides, while keeping the filename
    still mostly human-readable.

    The prefix is also capped at _SANITIZE_MAX_PREFIX_LEN characters
    before the hash is appended, so an unusually long rig/panel name
    can't blow the resulting path past filesystem limits (Windows'
    260-char MAX_PATH in particular, when combined with a deeply nested
    AppData path). The hash is taken from the full original name either
    way, so a long name is still truncated safely rather than colliding
    with another long name that merely shares the same first 40 chars.
    """
    safe = re.sub(r'[^A-Za-z0-9_.-]', '_', name)[:_SANITIZE_MAX_PREFIX_LEN]
    suffix = hashlib.sha1(name.encode("utf8")).hexdigest()[:8]
    return f"{safe}_{suffix}"


def get_image_path(armature_name, panel_name, ensure_folder=False):
    """Returns the dedicated image path for one rig's one panel's
    captured view.

    Each (rig, panel) pair gets its own file (named after both,
    sanitized for filesystem safety) so capturing a view for one panel
    can never overwrite another panel's - on this rig or any other -
    saved background. Pass ensure_folder=True when about to WRITE the
    file (creates the images folder); plain lookups never create anything.
    """
    safe_rig = _sanitize(armature_name)
    safe_panel = _sanitize(panel_name)
    return os.path.join(
        get_images_folder(create=ensure_folder),
        f"{safe_rig}__{safe_panel}.png",
    )


# ---------------------------------------------------------
# LOAD / SAVE
# ---------------------------------------------------------

class LockError(Exception):
    """A lock taken with required=True could not be established. Raised
    BEFORE the guarded body runs, so the write does not happen."""


class LockTimeoutError(LockError):
    """The cross-process lock on rig_picker_data.json could not be acquired
    within the timeout (another live process holds it)."""


class LockUnavailableError(LockError):
    """No lock could be set up at all: the `.lock` file can't be opened, or
    the platform has neither msvcrt nor fcntl."""


def _lock_failure_reason(path, e):
    """Wording for a failed required lock, for the user-facing message."""
    if isinstance(e, LockTimeoutError):
        return f"{path} is locked by another Blender instance ({e})"
    return f"{path} could not be locked ({e})"


# Lock paths THIS thread currently holds (acquired for real, not a read that
# gave up waiting) -> nesting depth. flock()/msvcrt locks taken through two
# different file handles exclude each other even inside one process, so a
# nested _cross_process_lock() on a path the thread already holds would
# deadlock until its timeout. Per THREAD, not per process: the install thread
# must still be excluded by a lock the main thread holds, and vice versa.
_HELD_LOCKS = threading.local()


@contextlib.contextmanager
def _cross_process_lock(path, timeout=10.0, required=False):
    """Cross-process advisory lock on rig_picker_data.json, via a
    dedicated `<path>.lock` file.

    Only single-process concurrency (main thread vs. the install
    thread) was previously guarded against. If a second Blender process
    (a genuinely separate instance, not just another .blend open in the
    same process) is pointed at the same user profile, nothing stopped
    both processes' _flush() calls from writing their own ".tmp" file
    and os.replace()-ing it at effectively the same moment - not just
    a clash between the two processes' *data* (now handled separately: see
    the three-way merge in _flush()), but a real race on the *files*
    involved, which could leave rig_picker_data.json.bak clobbered
    mid-copy or momentarily point at whichever replace() landed last in
    an undefined way.

    This lock is held for the whole read-or-write critical section in
    _load() / _flush(), so the two processes' file operations are
    strictly serialized and never interleave.

    Uses msvcrt on Windows and flock on POSIX (Linux/macOS). If no lock
    can be established at all (lock file can't be opened, or neither module
    exists) a required=True lock raises LockUnavailableError; a
    required=False (read) lock proceeds unlocked.

    The wait is capped at `timeout` seconds so Blender's UI thread is never
    frozen forever. What happens at the cap depends on `required`:

      * required=False (reads): proceeds without the lock. A read can't
        corrupt anything, and every write is an atomic os.replace(), so a
        reader never sees a half-written file.
      * required=True (every write): raises LockTimeoutError BEFORE the
        body runs, so the write does not happen. os.replace() only protects
        against a partial file; it does nothing to stop two processes
        overwriting each other's changes, so a write must not proceed
        unprotected. Callers report it and keep their changes in memory.

    (A lock held by a process that crashed is not a reason for a timeout:
    flock/msvcrt locks are released by the OS when the holder exits. A
    timeout means another live process is holding it, or is hung.)

    If the lock can't be set up at all (lock file can't be opened, or the
    platform has neither msvcrt nor fcntl), required=True raises
    LockUnavailableError BEFORE the body runs - a write never proceeds
    without cross-process protection. Reads (required=False) proceed
    unlocked, which is safe for the reason above.
    """
    lock_path = path + ".lock"

    # Re-entrant for the thread that already holds it: lets a caller keep
    # ONE required lock across a whole read-merge-decide-write sequence even
    # though the steps inside (_load(), _flush(), ...) take the lock again.
    held = getattr(_HELD_LOCKS, "paths", None)
    if held is None:
        held = _HELD_LOCKS.paths = {}
    if held.get(lock_path, 0) > 0:
        held[lock_path] += 1
        try:
            yield
        finally:
            held[lock_path] -= 1
        return

    try:
        f = open(lock_path, "a+b")
    except OSError as e:
        # Can't even create the lock file (e.g. read-only folder).
        if required:
            raise LockUnavailableError(
                f"cannot open lock file {lock_path}: {e}") from e
        yield
        return

    try:
        if not (msvcrt or fcntl):
            if required:
                raise LockUnavailableError(
                    "no file-locking support (neither msvcrt nor fcntl) "
                    "on this platform")
            yield
            return

        if msvcrt is not None and os.path.getsize(lock_path) == 0:
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
                    break
                time.sleep(0.05)

        if not acquired and required:
            raise LockTimeoutError(
                f"could not lock {lock_path} within {timeout:g}s - another "
                f"Blender instance is using it"
            )

        if acquired:
            held[lock_path] = 1
        try:
            yield
        finally:
            if acquired:
                held.pop(lock_path, None)
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


def _load():
    """Loads the current store's data.json into memory (once per store)
    and returns the data dict."""
    store = current_store()

    if store.data is not None:
        return store.data

    if not _is_picker_folder_path(store.folder):
        # Same rule as _flush(): never touch (or create a lock file in) a
        # folder that isn't a .rigpicker data folder.
        store.data, store.loaded_hash = {"settings": {"window": {}}, "armatures": {}}, None
        store.base = copy.deepcopy(store.data)
        return store.data

    with _cross_process_lock(store.path):
        store.data, store.loaded_hash = _load_locked(store.path, store.folder)
    store.base = copy.deepcopy(store.data)

    return store.data


def _file_fingerprint(path):
    """SHA-256 hex digest of the file's bytes, or None if it doesn't exist
    or can't be read."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _load_locked(path, folder):
    """The actual disk-read behind _load(), run while
    _cross_process_lock(path) is held so it can never interleave with
    another process's _flush(). `folder` is the store folder, used to turn
    relative background paths into absolute ones. Returns
    (data, content_hash_of_the_bytes_read_or_None)."""
    data = None
    file_hash = None

    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                raw = f.read()
            # Fingerprint the exact bytes parsed (even if they turn out to
            # be corrupt), so "unchanged since load" is judged on content.
            file_hash = hashlib.sha256(raw).hexdigest()
            data = json.loads(raw.decode("utf8"))
        except (OSError, ValueError) as e:

            # Fall back to the last known-good backup instead of silently
            # wiping every armature's saved picker data.
            backup_path = path + ".bak"
            if os.path.exists(backup_path):
                try:
                    with open(backup_path, "r", encoding="utf8") as f:
                        data = json.load(f)
                    _report_error(
                        f"Rig Picker: {path} could not be read ({e}). "
                        f"Recovered your picker data from the backup file "
                        f"instead - it may be missing your most recent "
                        f"changes."
                    )
                except (OSError, json.JSONDecodeError) as e2:
                    data = {}
                    _report_error(
                        f"Rig Picker: {path} could not be read ({e}), and "
                        f"the backup {backup_path} could not be read "
                        f"either ({e2}). All saved picker data has been "
                        f"reset - see the system console for details."
                    )
            else:
                data = {}
                _report_error(
                    f"Rig Picker: {path} could not be read ({e}), and no "
                    f"backup file was found. All saved picker data has "
                    f"been reset - see the system console for details."
                )
    else:
        data = {}

    data = _migrate_top_level(data)

    # --------------------------------------------------
    # Ensure global settings always exist
    # --------------------------------------------------

    data.setdefault("settings", {})
    # Deliberately an EMPTY dict - no default width/height stored here.
    # get_window_size() falls back to the caller's screen-scaled default
    # (the window's minimum size) only while nothing has been saved yet;
    # seeding a fixed size here would count as "saved" and override that.
    data["settings"].setdefault("window", {})
    data.setdefault("armatures", {})

    # N-panel (Quick action) data - see get_quick_actions(). Anything that
    # isn't an object is dropped rather than trusted.
    if "quick_actions" in data and not isinstance(data["quick_actions"], dict):
        del data["quick_actions"]

    # Schema version. A missing one means a file this addon is creating
    # (or a hand-made one) - treat it as the current version. A HIGHER one
    # was written by a newer addon: still read (best effort), but warn
    # once per file, and keep its number when writing back.
    version = data.setdefault("version", DATA_VERSION)
    if isinstance(version, int) and version > DATA_VERSION \
            and path not in _warned_newer_version:
        _warned_newer_version.add(path)
        _notify_user(
            f"Rig Picker: {path} was written by a newer version of the "
            f"addon (data version {version}, this addon understands "
            f"{DATA_VERSION}). Some data may not show up correctly."
        )

    _from_disk(data, folder)

    return data, file_hash


def _migrate_top_level(data):
    """Upgrades a raw dict loaded from disk to the current
    {"settings": {...}, "armatures": {...}} top-level shape, in place,
    and returns it.

    An older version of this file kept "_settings" as a same-level
    sibling of every armature entry (top-level keys were "_settings"
    plus one per armature name). That let an armature literally named
    "_settings" collide with the real settings entry - see the module
    docstring. Detected here by the absence of an "armatures" key (the
    new layout always has one, even on an otherwise-empty file) and
    migrated by lifting the old "_settings" value into the new
    "settings" key and moving every other top-level key under
    "armatures".

    Idempotent: a file already in the new shape (has "armatures") is
    returned unchanged, so this is safe to call on every load.
    """
    if "armatures" in data:
        return data

    old_settings = data.pop("_settings", {})
    # Whatever is left is every pre-migration armature entry, keyed
    # directly by armature name (including, potentially, one literally
    # named "_settings" from before this file's collision was fixed -
    # that entry is now just an ordinary armature again, nested safely
    # under "armatures" where its name can never collide with anything).
    armatures = data

    return {
        "settings": old_settings,
        "armatures": armatures,
    }


_MISSING = object()

# Collections in the N-panel payload (custom_actions_quick.py) are lists of
# dicts. Their items carry a stable id - "uid" on Multi/Single buttons, a
# hidden "_uid" on panels (serialized as item["_hidden"]["_uid"]) - which
# _merge3() uses to merge such a list item by item instead of as one value.
# Lists without ids (rules, bones, anything else) stay single values.
_ITEM_ID_KEYS = ("uid",)
_HIDDEN_ID_KEYS = ("_uid",)

# UI state that differs between two open instances all the time (which row
# is selected, which row is expanded). The merge still resolves them (ours
# wins) but a difference is not a conflict worth telling the user about.
_QUIET_KEYS = ("expanded", "toggle_state")
_QUIET_SUFFIXES = ("_index",)


def _item_id(item):
    if not isinstance(item, dict):
        return None
    for k in _ITEM_ID_KEYS:
        v = item.get(k)
        if isinstance(v, str) and v:
            return v
    hidden = item.get("_hidden")
    if isinstance(hidden, dict):
        for k in _HIDDEN_ID_KEYS:
            v = hidden.get(k)
            if isinstance(v, str) and v:
                return v
    return None


def _id_index(items):
    """{id: item} if `items` is a list whose every item has a distinct id
    (an empty list qualifies), else None."""
    if not isinstance(items, list):
        return None
    out = {}
    for item in items:
        ident = _item_id(item)
        if ident is None or ident in out:
            return None
        out[ident] = item
    return out


def _is_quiet(key):
    return isinstance(key, str) and (
        key in _QUIET_KEYS or key.endswith(_QUIET_SUFFIXES))


def _merge_by_id(base, mine, theirs, path, conflicts):
    """Merges two lists of id'd items against their common ancestor, item
    by item: an item changed (or added, or deleted) on one side only is
    taken from that side; an item both sides changed is merged field by
    field with _merge3(), so renaming Button 1 here and Button 2 there both
    survive. Same-field edits, and delete-vs-edit of one item, are
    conflicts (ours wins). Returns the merged list, or None if the lists
    can't be merged by id."""
    b = _id_index(base) if isinstance(base, list) else (
        {} if base is _MISSING or base is None else None)
    m = _id_index(mine)
    t = _id_index(theirs)
    if b is None or m is None or t is None:
        return None

    def label(ident):
        for src in (b, m, t):          # the name both sides started from first
            item = src.get(ident)
            if isinstance(item, dict) and item.get("name"):
                return f"{item['name']}"
        return ident

    merged = {}
    for ident in list(m) + [i for i in t if i not in m] + [i for i in b if i not in m and i not in t]:
        v = _merge3(b.get(ident, _MISSING), m.get(ident, _MISSING),
                    t.get(ident, _MISSING), path + (label(ident),), conflicts)
        if v is not _MISSING:
            merged[ident] = v

    # Order: follow whichever side reordered the items both started with
    # (ours if both did); otherwise the other side's order.
    base_order = [i for i in b]
    mine_common = [i for i in m if i in b]
    theirs_common = [i for i in t if i in b]
    base_in_mine = [i for i in base_order if i in m]
    if mine_common != base_in_mine:
        primary, secondary = list(m), list(t)
    elif theirs_common != [i for i in base_order if i in t]:
        primary, secondary = list(t), list(m)
    else:
        primary, secondary = list(m), list(t)
        # Nobody reordered: keep the shared order and put new items from
        # either side after it.
        shared = [i for i in base_order if i in merged]
        primary = shared + [i for i in list(m) + list(t) if i not in b]
    order = []
    for i in primary + secondary + list(merged):
        if i in merged and i not in order:
            order.append(i)
    return [merged[i] for i in order]


def _merge3(base, mine, theirs, path=(), conflicts=None):
    """Three-way merge of JSON-like values. `base` is the common ancestor,
    `mine` this instance's copy, `theirs` what another instance saved.
    Dicts are merged key by key, recursively. Lists of dicts that all carry
    a stable id (see _item_id()) are merged item by item by that id, not by
    position. Any other value, including a list without ids, is a leaf. For
    a leaf, whichever side changed it wins; if both changed it differently,
    `mine` wins and the path is appended to `conflicts` (unless it is UI
    state, see _QUIET_KEYS). A key or item missing on one side counts as
    deleted there. Returns the merged value (_MISSING means "absent")."""
    if mine == theirs:
        return mine
    if mine == base:
        return theirs
    if theirs == base:
        return mine
    if (isinstance(base, dict) or base is _MISSING) \
            and isinstance(mine, dict) and isinstance(theirs, dict):
        b = base if isinstance(base, dict) else {}
        out = {}
        for k in list(mine) + [k for k in theirs if k not in mine]:
            v = _merge3(b.get(k, _MISSING), mine.get(k, _MISSING),
                        theirs.get(k, _MISSING), path + (k,), conflicts)
            if v is not _MISSING:
                out[k] = v
        return out
    if isinstance(mine, list) and isinstance(theirs, list):
        merged = _merge_by_id(base, mine, theirs, path, conflicts)
        if merged is not None:
            return merged
    if conflicts is not None and not _is_quiet(path[-1] if path else None):
        conflicts.append("/".join(str(x) for x in path) or "(root)")
    return mine


def _report_merge_conflicts(path, conflicts):
    if not conflicts:
        return
    shown = ", ".join(conflicts[:5])
    more = "" if len(conflicts) <= 5 else f" (+{len(conflicts) - 5} more)"
    _notify_user(
        f"Rig Picker: {path} was changed by another "
        f"Blender instance. Its changes were merged "
        f"with yours, but both of you edited: {shown}"
        f"{more}. Your version of those was kept."
    )


def _merge_external_changes(store, path):
    """Brings changes another Blender instance saved to data.json into
    `store.data`. MUST be called with the lock on `path` held.

    The file's content hash is compared with `store.loaded_hash` (not mtime:
    timestamps can collide or be coarse). If it differs, another instance
    saved in between. The lock only serializes writers; it doesn't combine
    their edits, so writing our whole in-memory copy would erase theirs.
    Instead three-way merge: base (what we last read/wrote), ours, and what
    is on disk now. Edits to different keys all survive; only if both sides
    changed the same value differently does ours win, and the user is told.

    On success the store's base and hash describe the disk state just
    merged, so a repeat call is a no-op until the file changes again.
    A file that existed at our last read/write but is now missing is reported
    and `store.loaded_hash` reset to None (data stays in memory to be
    rewritten); a file that never existed is not an error.
    Returns True if the store is in sync with the file (nothing to merge, no
    file, or merged); False if the file changed but can't be read (a
    failure already reported) - `store.data` is then untouched."""
    if not os.path.exists(path):
        if store.loaded_hash is not None:
            # The file existed when we last read/wrote it and is gone now:
            # deleted from outside. Our in-memory copy is the only one left,
            # so it is restored by the write that follows (a caller must not
            # treat "already saved" as true). Forget the old fingerprint so
            # this is reported once, and so callers see "no file on disk".
            store.loaded_hash = None
            _notify_user(
                f"Rig Picker: {path} was deleted while the file was open. "
                f"Your picker data is still in memory and is being written "
                f"back; if you meant to remove it, close this file without "
                f"making further changes."
            )
        return True
    try:
        with open(path, "rb") as f:
            raw = f.read()
        current_hash = hashlib.sha256(raw).hexdigest()
    except OSError:
        raw = None
        current_hash = None
    if current_hash is not None and current_hash == store.loaded_hash:
        return True

    try:
        if raw is None:
            raise OSError("file could not be read")
        theirs = json.loads(raw.decode("utf8"))
        if not isinstance(theirs, dict):
            raise ValueError("not a JSON object")
        theirs = _from_disk(_migrate_top_level(theirs), store.folder)
    except (OSError, ValueError) as e:
        _report_error(
            f"Rig Picker: {path} was changed by another "
            f"Blender instance and can't be read to merge "
            f"({e}). Your most recent changes were NOT "
            f"written; they are kept in this session and "
            f"will be merged on the next change."
        )
        return False

    conflicts = []
    merged = _merge3(
        store.base if store.base is not None else _MISSING,
        store.data, theirs, conflicts=conflicts)
    # Update the root dict IN PLACE: callers hold a reference to it
    # (save_quick_actions() and others), and a replaced dict would leave
    # them editing a copy the store no longer uses.
    store.data.clear()
    store.data.update(merged)
    store.base = copy.deepcopy(theirs)
    store.loaded_hash = current_hash
    _report_merge_conflicts(path, conflicts)
    return True


def _flush():
    """Writes the in-memory cache to disk.

    Writes are atomic (write to a temp file, then os.replace() it into
    place) so a crash, force-quit, or antivirus/sync lock mid-write can
    never leave data.json truncated or corrupted - a corrupted
    file would otherwise be indistinguishable from "no data" on next load,
    silently wiping every armature's saved state.

    A .bak copy of the previous good version is also kept as a fallback
    in case the file is ever found unreadable on load anyway.

    The whole thing runs under _cross_process_lock() so a second Blender
    instance pointed at the same user profile can never have its own
    _flush() interleave with this one - each write's temp-file-then-
    backup-then-replace sequence completes in full before the other
    process's can start, instead of the two racing on the same files.
    tmp_path additionally includes this process's pid, so even in the
    rare case the lock gives up after timing out (see
    _cross_process_lock()) two processes still can't stomp on the same
    temp file.

    If another instance changed data.json since this one last read it, the
    two versions are three-way merged (see _merge3()) rather than one
    overwriting the other.

    If the lock can't be acquired within its timeout the write is ABORTED
    and reported, never done unlocked: the in-memory data is kept, so the
    next _flush() retries.

    Returns True if data.json was written, False if nothing was (the
    write was refused, failed or timed out - already reported to the user).
    """
    store = current_store()
    if store.data is None:
        return False

    if not _is_picker_folder_path(store.folder):
        # data.json, its .bak, .lock and .tmp are only ever written inside
        # a <name>.rigpicker folder - never next to the .blend or anywhere
        # else. (Always true for folders _resolve_location() produces; this
        # makes it a hard rule rather than a side effect.)
        _notify_user(
            f"Rig Picker: refusing to write picker data to {store.folder} "
            f"- it is not a .rigpicker folder."
        )
        store.data = None
        return False

    if not _write_allowed():
        # Drop the in-memory copy: the change that led here was applied
        # to it, and it must not be written later. Everything else was
        # already flushed, so reloading from disk loses nothing.
        store.data = None
        return False

    path = store.path
    tmp_path = f"{path}.{os.getpid()}.tmp"
    backup_path = path + ".bak"

    # The folder is created on the first write, never on a read.
    try:
        os.makedirs(store.folder, exist_ok=True)
    except OSError as e:
        _report_error(
            f"Rig Picker: couldn't create the data folder {store.folder} "
            f"({e}). Your most recent changes were NOT written to disk; "
            f"the session continues in memory."
        )
        return False

    wrote = False

    try:
        with _cross_process_lock(path, required=True):
            # Another instance may have saved since we last read/wrote:
            # merge its changes in first (never overwrite them). If the
            # on-disk file can't be read, nothing is written.
            if not _merge_external_changes(store, path):
                return False

            try:
                with open(tmp_path, "w", encoding="utf8") as f:
                    json.dump(_to_disk(store), f, indent=2)

                if os.path.exists(path):
                    try:
                        import shutil
                        shutil.copyfile(path, backup_path)
                    except OSError:
                        pass

                os.replace(tmp_path, path)
                store.loaded_hash = _file_fingerprint(path)
                store.base = copy.deepcopy(store.data)
                wrote = True

            except OSError as e:
                _report_error(
                    f"Rig Picker: failed to save {path} ({e}). Your most "
                    f"recent changes were NOT written to disk - see the "
                    f"system console for details, and try again once "
                    f"whatever's holding the file (full disk, antivirus/"
                    f"sync lock, permissions) is resolved."
                )
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
    except LockError as e:
        _report_error(
            f"Rig Picker: {_lock_failure_reason(path, e)}. Your most recent changes were NOT written to disk; "
            f"they are kept in this session and will be saved by the next "
            f"change once the other instance has finished."
        )

    # First successful write for a saved file: list its folder in the
    # registry (outside the data lock - the registry has its own).
    if wrote and store.blend_path and not store.registered:
        register_ui_data_folder(store.blend_path, store.folder)
        store.registered = True

    return wrote


def reload():
    """Forgets the bound store, so the next access re-resolves the open
    file and re-reads its data.json from disk."""
    global _store, _detached
    _store = None
    _detached = False


# ---------------------------------------------------------
# N-PANEL (QUICK ACTION) DATA
# ---------------------------------------------------------
# The N-panel's buttons and panel list (custom_actions_quick.py) are
# mirrored into the open file's data.json under "quick_actions", so every
# change is on disk right away instead of waiting for the .blend to be
# saved. This module treats the payload as an opaque dict; the Quick panel
# module builds and applies it.

QUICK_ACTIONS_KEY = "quick_actions"


def current_identity():
    """(blend_key, folder) of the file open right now - what a store is
    bound to. Lets a caller notice that the open file changed under data
    it read earlier."""
    return _identity()


def get_quick_actions():
    """A copy of the N-panel data stored for the open file, or None if
    nothing was ever stored (a file that never had any, or one from before
    this existed)."""
    import copy
    stored = _load().get(QUICK_ACTIONS_KEY)
    return copy.deepcopy(stored) if isinstance(stored, dict) else None


def save_quick_actions(payload):
    """Stores `payload` as the open file's N-panel data and writes
    data.json right away. Returns True once what is on disk holds
    `payload`'s changes (including when it already did), False if the write
    was refused or failed.

    `payload` is a snapshot taken from this instance's scene, so it may be
    stale if another Blender instance saved in the meantime. Before judging
    anything, the file is checked (by content hash) under the lock and the
    other instance's changes are merged into memory. Then `payload` is
    three-way merged with the disk state, using the N-panel data this
    instance last saw as the base: only what THIS instance changed is
    applied, so another instance's edits are never overwritten just to push
    a stale snapshot. If this instance changed nothing relative to its base,
    nothing is written and True is returned even though the disk may differ
    from `payload` (it holds the other instance's newer data, which
    get_quick_actions() now returns)."""
    store = current_store()
    if not _is_picker_folder_path(store.folder):
        return _save_quick_actions_locked(payload)

    # The .lock file lives inside the data folder, and only _flush() creates
    # that folder. After an uninstall wipe (or the folder being deleted by
    # hand) it no longer exists, so opening the lock file fails with
    # ENOENT. Recreate it first - but only when a write is actually allowed,
    # so a detached/wiped store never leaves an empty folder behind.
    if _write_allowed(report=False):
        try:
            os.makedirs(store.folder, exist_ok=True)
        except OSError:
            pass  # the lock attempt below reports the real problem

    # ONE required lock around the whole read -> merge -> decide -> write
    # sequence (the nested locks _load()/_flush() take are re-entrant). If it
    # can't be had, nothing is judged or written from an unlocked read.
    try:
        with _cross_process_lock(store.path, required=True):
            return _save_quick_actions_locked(payload)
    except LockError as e:
        _report_error(
            f"Rig Picker: {_lock_failure_reason(store.path, e)}. Your most "
            f"recent changes were NOT written to disk; they are kept in this "
            f"session and will be saved by the next change once the other "
            f"instance has finished."
        )
        return False


def _save_quick_actions_locked(payload):
    """The body of save_quick_actions(). For a real picker folder it runs
    with the required cross-process lock already held."""
    data = _load()
    store = current_store()
    if store.data is data and _is_picker_folder_path(store.folder):
        old_base = (store.base or {}).get(QUICK_ACTIONS_KEY, _MISSING)
        in_sync = _merge_external_changes(store, store.path)
        # loaded_hash None here means there is no file on disk (never
        # written, or deleted - reported above): never "already saved".
        if in_sync and store.loaded_hash is not None:
            disk_qa = (store.base or {}).get(QUICK_ACTIONS_KEY, _MISSING)
            conflicts = []
            effective = _merge3(old_base, payload, disk_qa,
                                (QUICK_ACTIONS_KEY,), conflicts)
            _report_merge_conflicts(store.path, conflicts)
            if effective == disk_qa:
                return True
            payload = effective
        # else: the file couldn't be verified. Fall through: _flush()
        # refuses to write over a file it can't read and reports it.
    elif data.get(QUICK_ACTIONS_KEY) == payload:
        return True

    missing = QUICK_ACTIONS_KEY not in data
    previous = data.get(QUICK_ACTIONS_KEY)
    data[QUICK_ACTIONS_KEY] = payload
    ok = _flush()
    if not ok:
        # Roll back, or a retry would see "already equal" and report
        # success although nothing reached the disk. Work on the store's
        # CURRENT data, not the `data` read above: _flush() may have merged
        # another instance's edits into it (or dropped it entirely).
        # Reverting is itself a three-way merge - base = what we set,
        # mine = what it was before, theirs = what is there now - so only
        # our own change is undone and anything merged in from the other
        # instance (in N-panel data or elsewhere) is kept.
        current = current_store().data
        if current is not None:
            reverted = _merge3(
                payload,
                _MISSING if missing else previous,
                current.get(QUICK_ACTIONS_KEY, _MISSING),
            )
            if reverted is _MISSING:
                current.pop(QUICK_ACTIONS_KEY, None)
            else:
                current[QUICK_ACTIONS_KEY] = reverted
    return ok


# ---------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------

def new_panel_data():
    """Returns an empty picker data structure for a (rig, panel) pair
    with no saved state yet."""
    return {
        "background": "",
        "symmetry": False,
        "symmetry_x": -1.0,
        "image_offset_x": 0.0,
        "image_offset_y": 0.0,
        "items": [],
        # Custom canvas positions for the draggable overlay widgets
        # (Quick Action buttons/toggles - see custom_actions_quick.py) - see
        # PickerCanvas.OVERLAY_DRAG_GROUPS. Keyed the same as
        # OVERLAY_DRAG_GROUPS; a key that's absent (or whose value is
        # None) means that widget hasn't been dragged yet and still
        # uses the default stacked layout.
        "overlay_positions": {},
    }


# Kept as an alias so anything still calling the pre-panel name gets the
# exact same empty-picker structure - it's now the shape of one PANEL's
# data rather than a whole armature's, but the fields are identical.
new_armature_data = new_panel_data


def _migrate_armature_entry(armature_data):
    """Normalizes one armature's raw dict from disk into the current
    {"panels": {panel_name: {...}}} shape, in place, and returns it.

    A file written before per-panel scoping existed has the picker
    fields (background/items/...) directly on the armature entry itself,
    with no "panels" wrapper at all - wrap that straight into a single
    "Panel 1" entry (the same name _ensure_default_panel_for_rig() in
    custom_actions_quick.py gives a rig's first-ever panel) so old data
    keeps showing up right where a freshly panel-aware rig would expect
    to find it, instead of looking like it vanished.
    """
    if "panels" in armature_data:
        return armature_data

    old_fields = {
        k: v for k, v in armature_data.items()
        if k in ("background", "symmetry", "symmetry_x", "image_offset_x",
                  "image_offset_y", "items", "overlay_positions")
    }
    armature_data.clear()
    armature_data["panels"] = {"Panel 1": old_fields or new_panel_data()}
    return armature_data


def _get_armature_entry(armature_name, create=False):
    """Returns the (migrated) entry for one armature, or None if it
    doesn't exist and create is False."""
    armatures = _load()["armatures"]
    if armature_name not in armatures:
        if not create:
            return None
        armatures[armature_name] = {"panels": {}}
    return _migrate_armature_entry(armatures[armature_name])


def get_panel_data(armature_name, panel_name):
    """Returns the stored data for one (rig, panel) pair, or None if
    nothing is stored for it yet."""
    if not armature_name or not panel_name:
        return None
    entry = _get_armature_entry(armature_name)
    if entry is None:
        return None
    return entry["panels"].get(panel_name)


def save_panel_data(armature_name, panel_name, panel_data):
    """Persists the given data for one (rig, panel) pair and writes it
    to disk immediately."""
    if not armature_name or not panel_name:
        return

    entry = _get_armature_entry(armature_name, create=True)
    entry["panels"][panel_name] = panel_data
    _flush()


def rename_armature_data(old_name, new_name):
    """Migrates a stored armature's data - every one of its panels, and
    each panel's own captured background image, if it has one - from
    old_name to new_name.

    Called when an armature already tracked by the picker is renamed
    inside Blender itself (Outliner, F2, Properties panel, etc.) rather
    than through this addon, so the rig's saved layout follows it under
    its new name instead of being left behind as orphaned data under a
    name that no longer refers to anything.
    """
    if not old_name or not new_name or old_name == new_name:
        return

    armatures = _load()["armatures"]

    if old_name not in armatures:
        return

    armature_data = _migrate_armature_entry(armatures.pop(old_name))

    if new_name in armatures:
        # Something is already stored under new_name - typically leftover
        # data from a rig of that name in ANOTHER .blend (this file is
        # shared by every .blend), or an orphan from a deleted rig. Keep
        # that entry where it is, as before - but DON'T throw the renamed
        # rig's own data away (this used to drop it outright): park it
        # under a recovery name instead. Its captured-view images are left
        # exactly where they are (each panel's stored "background" path
        # still points at them), so nothing on disk is touched or
        # overwritten and the data can be moved back by renaming the entry.
        recovery_name = f"{old_name} (not merged - {new_name} already had data)"
        suffix = 2
        while recovery_name in armatures:
            recovery_name = f"{old_name} (not merged - {new_name} already had data) {suffix}"
            suffix += 1
        armatures[recovery_name] = armature_data
        _flush()
        _notify_user(
            f'Rig Picker: "{new_name}" already had saved picker data, so the '
            f'layouts of the rig renamed from "{old_name}" were kept under '
            f'"{recovery_name}" instead of being merged or deleted.'
        )
        return

    armatures[new_name] = armature_data

    for panel_name, panel_data in armature_data["panels"].items():
        old_image = panel_data.get("background") or get_image_path(old_name, panel_name)
        new_image = get_image_path(new_name, panel_name)
        if old_image and os.path.exists(old_image) and not os.path.exists(new_image):
            try:
                os.replace(old_image, new_image)
                if panel_data.get("background") == old_image:
                    panel_data["background"] = new_image
            except OSError:
                pass

    _flush()


def rename_panel_data(armature_name, old_panel_name, new_panel_name):
    """Migrates one rig's single panel's data - and its captured
    background image, if it has one - from old_panel_name to
    new_panel_name.

    Called when a panel is renamed in place from the Manage Panels
    N-panel list (double-click a row), so its saved layout follows it
    under its new name instead of being orphaned under a name Manage
    Panels no longer shows.
    """
    if not armature_name or not old_panel_name or not new_panel_name:
        return
    if old_panel_name == new_panel_name:
        return

    entry = _get_armature_entry(armature_name)
    if entry is None or old_panel_name not in entry["panels"]:
        return

    # LIMIT: a panel's captured background image is moved along with the
    # data, but only if the file still exists on disk; a missing image is
    # not recreated (the panel just comes up without a background).
    panel_data = entry["panels"].pop(old_panel_name)

    # The UI already rejects a rename onto another LIVE panel of the same
    # rig (see _on_quick_panel_name_changed), so anything still stored
    # under new_panel_name here is an orphan - left behind by a deleted
    # panel, or resurrected by a late save(). The panel being renamed is
    # the live one, so ITS data must win. (This used to bail out here
    # AFTER already popping old_panel_name above, which silently threw
    # the renamed panel's data away.)
    orphan = entry["panels"].pop(new_panel_name, None)
    new_image = get_image_path(armature_name, new_panel_name)
    if orphan is not None:
        orphan_image = orphan.get("background")
        if (
            orphan_image and orphan_image != panel_data.get("background")
            and os.path.exists(orphan_image)
        ):
            try:
                os.remove(orphan_image)
            except OSError:
                pass

    entry["panels"][new_panel_name] = panel_data

    old_image = panel_data.get("background") or get_image_path(armature_name, old_panel_name)
    if old_image and os.path.exists(old_image) and old_image != new_image:
        try:
            os.replace(old_image, new_image)
            if panel_data.get("background") == old_image:
                panel_data["background"] = new_image
        except OSError:
            pass

    _flush()


# Suffix for a captured-view image that Clear All has set aside (rather than
# deleted) so an undo can bring it back - see backup_armature_data().
_CLEAR_BACKUP_SUFFIX = ".clearbak"


def backup_armature_data(armature_name):
    """Reversible counterpart to delete_armature_data(): takes every panel
    of a rig out of rig_picker_data.json exactly like it, but instead of
    deleting each panel's captured-view image, renames it aside
    (<path>.clearbak). Returns a token for restore_armature_backup() /
    discard_armature_backup(), or None if nothing was stored.

    Used by Controller.clear_all() so that Ctrl+Z - which restores the
    Manage Panels rows and buttons (scene data) - can also restore the
    layouts those rows pointed at. The renamed images also can't collide
    with a new "Panel 1" capturing its own image at the same path.
    """
    armatures = _load()["armatures"]
    if armature_name not in armatures:
        return None

    armature_data = _migrate_armature_entry(armatures[armature_name])
    del armatures[armature_name]
    _flush()

    images = {}
    for panel_name, panel_data in armature_data["panels"].items():
        for path in {panel_data.get("background"), get_image_path(armature_name, panel_name)}:
            if path and path not in images and os.path.exists(path):
                backup_path = path + _CLEAR_BACKUP_SUFFIX
                try:
                    os.replace(path, backup_path)
                    images[path] = backup_path
                except OSError:
                    pass

    return {"armature": armature_name, "data": armature_data, "images": images}


def restore_armature_backup(token, only_missing=False):
    """Puts a backup_armature_data() token back. By default the rig's
    stored entry is REPLACED by the backed-up one (an undo: whatever the
    cleared rig has stored now is the blank state being undone). With
    only_missing=True nothing that is currently stored is overwritten -
    only panels whose names are absent come back (used when another file
    is opened, where the rig name may already be in use by that file)."""
    if not token:
        return
    name = token["armature"]
    armatures = _load()["armatures"]

    if only_missing and name in armatures:
        current = _migrate_armature_entry(armatures[name])["panels"]
        restored = {
            panel_name: panel_data
            for panel_name, panel_data in token["data"]["panels"].items()
            if panel_name not in current
        }
        current.update(restored)
        keep_images = {
            data.get("background") for data in restored.values()
        } | {get_image_path(name, panel_name) for panel_name in restored}
    else:
        armatures[name] = token["data"]
        keep_images = None  # every image comes back

    for path, backup_path in token["images"].items():
        if keep_images is not None and path not in keep_images:
            discard = True
        else:
            discard = False
        if not os.path.exists(backup_path):
            continue
        try:
            if discard:
                os.remove(backup_path)
            else:
                os.replace(backup_path, path)
        except OSError:
            pass

    _flush()


def discard_armature_backup(token):
    """Deletes the set-aside images of a backup_armature_data() token for
    good - the backup can no longer be restored once this runs."""
    if not token:
        return
    for backup_path in token["images"].values():
        if os.path.exists(backup_path):
            try:
                os.remove(backup_path)
            except OSError:
                pass


def delete_panel_data(armature_name, panel_name):
    """Removes stored data for one (rig, panel) pair (e.g. its last
    remaining row was deleted from Manage Panels), including its
    captured background image file on disk.

    Mirrors delete_armature_data() below, just scoped to a single panel
    instead of the whole rig - without also removing the image, a
    deleted panel would leave its captured-view PNG behind permanently.
    """
    if not armature_name or not panel_name:
        return

    entry = _get_armature_entry(armature_name)
    if entry is None or panel_name not in entry["panels"]:
        return

    panel_data = entry["panels"].pop(panel_name)
    background = panel_data.get("background")
    _flush()

    for path in {background, get_image_path(armature_name, panel_name)}:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass


# Saved canvas positions of Quick Action buttons that were removed THIS
# session: key -> {(armature_name, panel_name): [x, y]}. A button removed
# from the N panel (or taken with its panel) has its saved position erased
# for good - from the loaded controller, from every stored panel and from
# disk (see remove_overlay_position()). Blender's undo brings the button
# itself back, but it has no way to bring that position back, so without
# this the button would reappear at a fresh default spot (possibly right
# on top of another button) instead of where it was. The canvas hands the
# position back out via take_removed_overlay_positions() the moment the
# same button reappears (PickerCanvas._assign_default_custom_action_position).
#
# LIMITS: plain memory - gone when Blender restarts or a file is opened,
# and only the last _MAX_REMOVED_OVERLAY_POSITIONS removed buttons are kept.
_REMOVED_OVERLAY_POSITIONS = {}
_MAX_REMOVED_OVERLAY_POSITIONS = 100


def remember_removed_overlay_position(key, armature_name, panel_name, position):
    """Keeps `position` ([x, y]) of the removed button `key` on the given
    (armature, panel) so it can be handed back if the button returns."""
    if not position:
        return
    _REMOVED_OVERLAY_POSITIONS.setdefault(key, {})[(armature_name, panel_name)] = [
        position[0],
        position[1],
    ]
    while len(_REMOVED_OVERLAY_POSITIONS) > _MAX_REMOVED_OVERLAY_POSITIONS:
        _REMOVED_OVERLAY_POSITIONS.pop(next(iter(_REMOVED_OVERLAY_POSITIONS)))


def take_removed_overlay_positions(key):
    """Returns (and forgets) everything remembered for button `key`:
    {(armature_name, panel_name): [x, y]}, empty if nothing was."""
    return _REMOVED_OVERLAY_POSITIONS.pop(key, None) or {}


def restore_overlay_positions(key, positions):
    """Writes remembered positions ({(armature_name, panel_name): [x, y]})
    back into the stored panel data and to disk. Only panels that still
    exist are touched, and a panel that already has a position for `key`
    keeps it. The panel the open Rig Picker has loaded is NOT handled here
    - the caller puts that one into its in-memory data itself."""
    if not positions:
        return

    armatures = _load()["armatures"]
    changed = False

    for (armature_name, panel_name), position in positions.items():
        armature_data = armatures.get(armature_name)
        if armature_data is None:
            continue
        armature_data = _migrate_armature_entry(armature_data)
        panel_data = armature_data["panels"].get(panel_name)
        if panel_data is None:
            continue
        overlay_positions = panel_data.setdefault("overlay_positions", {})
        if overlay_positions.get(key) is None:
            overlay_positions[key] = [position[0], position[1]]
            changed = True

    if changed:
        _flush()


def remove_overlay_position(key):
    """Removes one `overlay_positions` key from EVERY stored (rig,
    panel) pair's data, and writes the result to disk.

    Used when a draggable overlay widget is gone for good (e.g. a Quick
    Action button whose action was deleted, not just hidden) - its saved
    canvas position may have been independently dragged under more than
    one rig or panel over time, so a single one's data isn't enough to
    clean up. Safe to call even for armatures/panels that aren't
    currently loaded into the open Rig Picker window; it edits the
    on-disk store directly.

    Every position removed here is first remembered (see
    _REMOVED_OVERLAY_POSITIONS) so an undo of the removal can put the
    button back where it was.

    The caller is still responsible for popping the same key out of any
    ALREADY-loaded in-memory controller.data/canvas.overlay_positions
    (see PickerCanvas.sync_quick_actions()) - otherwise a later save()
    from that stale in-memory copy would silently write the key right
    back in on top of this cleanup.
    """
    armatures = _load()["armatures"]
    changed = False

    for armature_name, armature_data in armatures.items():
        armature_data = _migrate_armature_entry(armature_data)
        for panel_name, panel_data in armature_data["panels"].items():
            overlay_positions = panel_data.get("overlay_positions")
            if overlay_positions and key in overlay_positions:
                remember_removed_overlay_position(
                    key, armature_name, panel_name, overlay_positions[key]
                )
                del overlay_positions[key]
                changed = True

    if changed:
        _flush()


def delete_armature_data(armature_name):
    """Removes stored data for an armature (e.g. if the user deletes the
    rig) - every one of its panels - including each panel's captured
    background image file on disk.

    Without also removing the images, every deleted armature left its
    panels' captured-view PNGs behind permanently in IMAGES_FOLDER_NAME -
    the JSON entry pointing to them was gone, so nothing would ever
    reference or clean them up again. Over a long project this silently
    accumulates orphaned images for every rig ever deleted.
    """
    armatures = _load()["armatures"]

    if armature_name in armatures:
        armature_data = _migrate_armature_entry(armatures[armature_name])
        panels = armature_data["panels"]

        del armatures[armature_name]
        _flush()

        for panel_name, panel_data in panels.items():
            background = panel_data.get("background")
            # Removing by whatever path was actually stored (not just
            # get_image_path(armature_name, panel_name)) covers the file
            # that's really referenced even in the unlikely case the two
            # ever diverged.
            for path in {background, get_image_path(armature_name, panel_name)}:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

# ---------------------------------------------------------
# TRACKED .BLEND FILES + PICKER FOLDERS (global registry)
# ---------------------------------------------------------
# The registry file (rig_picker_data.json in the addon's parent folder) is
# the only file shared between .blend files and Blender instances. It
# holds two things, used only by uninstall and pruning:
#   "panel data files": .blend files whose scene holds Quick-action button
#                       data (see custom_actions_quick._clean_other_files())
#   "ui data folders":  {.blend path: its .rigpicker folder}
#
# Every change is a locked read-modify-write straight against the file on
# disk (merge-on-write): a stale in-memory copy is never written, so one
# Blender instance can't erase another's entry.

PANEL_DATA_FILES_KEY = "panel data files"
UI_DATA_FOLDERS_KEY = "ui data folders"


def _same_path_key(path):
    return os.path.normcase(os.path.abspath(path))


def _registry_read_locked(path):
    """Registry dict from disk (empty lists if missing or unreadable).
    Any other keys in the file (e.g. the old pre-per-file layout data,
    which the user deletes by hand) are carried along untouched, so
    this code never destroys data it doesn't own."""
    raw = {}
    for candidate in (path, path + ".bak"):
        if os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf8") as f:
                    raw = json.load(f)
                break
            except (OSError, json.JSONDecodeError):
                continue
    if not isinstance(raw, dict):
        raw = {}
    files = raw.get(PANEL_DATA_FILES_KEY)
    folders = raw.get(UI_DATA_FOLDERS_KEY)
    raw[PANEL_DATA_FILES_KEY] = list(files) if isinstance(files, list) else []
    raw[UI_DATA_FOLDERS_KEY] = dict(folders) if isinstance(folders, dict) else {}
    return raw


def _registry_edit(mutate):
    """Takes the registry lock, re-reads the file from disk, calls
    `mutate(registry_dict)` (which returns True if it changed anything),
    and writes the result atomically if so. Returns the registry dict."""
    path = get_storage_path()
    if _detached:
        # No store is active (a file switch in progress, or the uninstall
        # wipe has run): nothing may be written - after the wipe this is
        # what stops a late call from re-creating the deleted registry.
        return _registry_read_locked(path)
    try:
        with _cross_process_lock(path, required=True):
            registry = _registry_read_locked(path)
            if not mutate(registry):
                return registry

            tmp_path = f"{path}.{os.getpid()}.tmp"
            backup_path = path + ".bak"
            try:
                with open(tmp_path, "w", encoding="utf8") as f:
                    json.dump(registry, f, indent=2)
                if os.path.exists(path):
                    try:
                        import shutil
                        shutil.copyfile(path, backup_path)
                    except OSError:
                        pass
                os.replace(tmp_path, path)
            except OSError as e:
                _report_error(
                    f"Rig Picker: failed to save {path} ({e}). The list of "
                    f".blend files could not be updated."
                )
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass
            return registry
    except LockError as e:
        _report_error(
            f"Rig Picker: {_lock_failure_reason(path, e)}. The list of .blend files was not updated."
        )
        return _registry_read_locked(path)


def get_tracked_blend_files():
    path = get_storage_path()
    with _cross_process_lock(path):
        return list(_registry_read_locked(path)[PANEL_DATA_FILES_KEY])


def track_blend_file(path):
    key = _same_path_key(path)

    def mutate(registry):
        files = registry[PANEL_DATA_FILES_KEY]
        if any(_same_path_key(p) == key for p in files):
            return False
        files.append(os.path.abspath(path))
        return True

    _registry_edit(mutate)


def untrack_blend_file(path):
    key = _same_path_key(path)

    def mutate(registry):
        files = registry[PANEL_DATA_FILES_KEY]
        kept = [p for p in files if _same_path_key(p) != key]
        if len(kept) == len(files):
            return False
        registry[PANEL_DATA_FILES_KEY] = kept
        return True

    _registry_edit(mutate)


def clear_tracked_blend_files():
    def mutate(registry):
        if not registry[PANEL_DATA_FILES_KEY]:
            return False
        registry[PANEL_DATA_FILES_KEY] = []
        return True

    _registry_edit(mutate)


def get_ui_data_folders():
    """{blend path: picker folder} for every registered file."""
    path = get_storage_path()
    with _cross_process_lock(path):
        return dict(_registry_read_locked(path)[UI_DATA_FOLDERS_KEY])


def register_ui_data_folder(blend_path, folder):
    """Lists `folder` as the picker folder of `blend_path`. Paths are
    compared in normalized form, so a different spelling of the same
    path replaces the entry instead of duplicating it."""
    key = _same_path_key(blend_path)

    def mutate(registry):
        folders = registry[UI_DATA_FOLDERS_KEY]
        same = [p for p in folders if _same_path_key(p) == key]
        if len(same) == 1 and _same_path_key(folders[same[0]]) == _same_path_key(folder):
            return False
        for p in same:
            del folders[p]
        folders[os.path.abspath(blend_path)] = os.path.abspath(folder)
        return True

    _registry_edit(mutate)


def unregister_ui_data_folder(blend_path):
    key = _same_path_key(blend_path)

    def mutate(registry):
        folders = registry[UI_DATA_FOLDERS_KEY]
        same = [p for p in folders if _same_path_key(p) == key]
        for p in same:
            del folders[p]
        return bool(same)

    _registry_edit(mutate)


def prune_ui_data_folders():
    """Drops registry entries whose folder no longer exists on disk."""
    def mutate(registry):
        folders = registry[UI_DATA_FOLDERS_KEY]
        stale = [p for p, f in folders.items() if not os.path.isdir(f)]
        for p in stale:
            del folders[p]
        return bool(stale)

    _registry_edit(mutate)


def register_current_folder_if_present():
    """For a saved file whose .rigpicker folder already exists (e.g. the
    .blend and folder were copied or moved together) but isn't registered
    yet: registers it, and prunes stale entries. Does nothing for an
    unsaved file or when no folder exists (the first write registers)."""
    store = current_store()
    if not store.blend_path:
        return
    if os.path.isfile(store.path):
        register_ui_data_folder(store.blend_path, store.folder)
        store.registered = True
    prune_ui_data_folders()


# ---------------------------------------------------------
# UNINSTALL
# ---------------------------------------------------------

def _remove_data_folder(folder):
    """Deletes one picker data folder, but only if it really is one: not a
    symlink, the name ends in .rigpicker, and it contains data.json. Those
    checks mean a wrong or tampered registry entry can never delete an
    arbitrary folder. Returns True if it was deleted."""
    import shutil
    try:
        if not folder or os.path.islink(folder) or not os.path.isdir(folder):
            return False
        name = os.path.basename(os.path.normpath(folder))
        if not name.lower().endswith(STORE_FOLDER_SUFFIX):
            return False
        if not os.path.isfile(os.path.join(folder, STORE_FILE_NAME)):
            return False
        shutil.rmtree(folder)
        return True
    except OSError:
        traceback.print_exc()
        return False


def wipe_all_data():
    """Uninstall: removes everything this addon stored on disk.

    1. Every folder listed under "ui data folders" in the registry, plus
       the open file's own data folder (or the unsaved file's temp folder)
       if it is not listed - each only if it passes _remove_data_folder()'s
       checks.
    2. The registry file itself with its .bak, .lock and leftover .tmp
       files. They live next to the addon folder, not inside it, so
       Blender's Remove would otherwise leave them behind.

    Only the caller's uninstall gate decides when this runs; it must never
    run on a plain disable or Reload Scripts. Run it AFTER the picker
    window is closed (closing writes the window geometry), and it refuses
    all further writes by detaching, so nothing can recreate what it just
    deleted. register() starts clean again via reload(). Returns the list
    of paths deleted."""
    import glob
    removed = []

    registry_path = get_storage_path()
    with _cross_process_lock(registry_path):
        registry = _registry_read_locked(registry_path)

    folders = []
    seen = set()

    def add(folder):
        if folder and _same_path_key(folder) not in seen:
            seen.add(_same_path_key(folder))
            folders.append(folder)

    for folder in registry[UI_DATA_FOLDERS_KEY].values():
        if isinstance(folder, str):
            add(folder)
    try:
        add(_resolve_location()[1])
    except Exception:
        traceback.print_exc()
    if _store is not None:
        add(_store.folder)

    for folder in folders:
        if _remove_data_folder(folder):
            removed.append(folder)

    candidates = [registry_path, registry_path + ".bak", registry_path + ".lock"]
    candidates += glob.glob(glob.escape(registry_path) + ".*.tmp")
    for path in candidates:
        try:
            if os.path.isfile(path):
                os.remove(path)
                removed.append(path)
        except OSError:
            traceback.print_exc()

    detach()
    return removed


DEFAULT_WINDOW_WIDTH = 330
DEFAULT_WINDOW_HEIGHT = 550


def get_window_size(default_width=None, default_height=None):

    data = _load()

    settings = data["settings"]
    window = settings.get("window", {})

    return (
        window.get("width", default_width if default_width is not None else DEFAULT_WINDOW_WIDTH),
        window.get("height", default_height if default_height is not None else DEFAULT_WINDOW_HEIGHT),
    )


def save_window_size(width, height):

    data = _load()

    data["settings"]["window"]["width"] = int(width)
    data["settings"]["window"]["height"] = int(height)

    _flush()


def get_window_position():
    """Returns the saved (x, y) window position, or None if nothing has
    been saved yet (e.g. first-ever launch) - callers should fall back to
    their own default placement logic (e.g. relative to the Blender
    window) in that case."""

    data = _load()

    settings = data["settings"]
    window = settings.get("window", {})

    x = window.get("x")
    y = window.get("y")

    if x is None or y is None:
        return None

    return (x, y)


def save_window_position(x, y):

    data = _load()

    data["settings"]["window"]["x"] = int(x)
    data["settings"]["window"]["y"] = int(y)

    _flush()