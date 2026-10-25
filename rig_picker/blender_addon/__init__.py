bl_info = {
    "name": "Rig Picker",
    "author": "Susmitha",
    "version": (1, 0, 0),
    "blender": (5, 0, 0),
    "location": "View3D",
    "description": "Visual picker for selecting rig controls and running quick actions",
    "category": "Animation",
}

# Re-enabling this addon (uninstall -> install, disable -> enable, "module
# changed on disk") re-executes only THIS file; Python keeps serving the
# submodules (backend, json_manager, ui.*, ...) from sys.modules, i.e. the
# OLD code, until Blender is restarted - which shows up as new code "not
# taking effect" or as ImportError for names added to a submodule. On a
# reload (bpy is already a global here), forget the cached submodules so the
# imports below load them fresh from disk.
if "bpy" in globals():
    import sys as _sys
    for _name in [m for m in _sys.modules if m.startswith(__name__ + ".")]:
        del _sys.modules[_name]

import bpy

from .operators import (
    RP_OT_OpenPicker,
    RP_PT_MainPanel,
)

from . import backend
from . import json_manager
from . import custom_actions_quick
from .backend import (
    RP_OT_Select,
    RP_OT_ShowAll,
    RP_OT_HideAll,
    RP_OT_CaptureView,
    RP_OT_TrackViewport,
    RP_OT_PickerUndoGuard,
)

classes = (
    RP_OT_OpenPicker,

    RP_OT_Select,
    RP_OT_ShowAll,
    RP_OT_HideAll,
    RP_OT_CaptureView,
    RP_OT_TrackViewport,
    RP_OT_PickerUndoGuard,

    RP_PT_MainPanel,
)


from .backend import poll_active_armature, _POLL_INTERVAL


# ---------------------------------------------------------
# LIFECYCLE DISPATCHER
# ---------------------------------------------------------
# The ONLY place load_pre / load_post / save_post are registered. The
# existing handlers (backend's, the Quick panel's) are plain functions
# that this dispatcher calls in a fixed order, instead of the result
# depending on which module happened to register first. Order matters
# now that every .blend has its own data store (see json_manager):
#
#   load_pre   - while the OUTGOING file is still open and its store is
#                still the current one: settle pending items
#                (1) the controller's, then (2) the Quick panel's;
#                (3) write any pending N-panel change (auto-save);
#                (4) flush the picker window's pending geometry and stop
#                its timers; (5) detach, so nothing can be written until
#                the new file's store is bound. A one-shot timer lifts the
#                detach again if the load FAILED and load_post never came.
#   load_post  - (1) swap to the new file's store (and re-point the open
#                window at it), (2) backend / controller UI reset,
#                (3) the Quick panel's handler (which also restores the
#                N-panel buttons stored for that file), (4) apply the new
#                file's window geometry, (5) register the new file's
#                picker folder if it has one.
#   save_pre   - the N-panel buttons are taken out of the scene (after
#                confirming the .rigpicker folder holds them), so the .blend
#                doesn't carry them; save_post / save_post_fail put them back
#                FIRST, before the steps below.
#   save_post  - (1) a first save moves the data folder next to the file,
#                a Save As copies it to the new name; (2) re-point the open
#                window at the saved file; (3) re-point in-memory image
#                paths (controller, Quick panel); (4) tell the N-panel
#                auto-save its data now lives in the new location; (5) the
#                Quick panel's handler.
#
# Each step is guarded on its own so one failing step can't stop the
# rest, or block the file from loading.

def _step(label, fn, *args):
    try:
        fn(*args)
    except Exception:
        import traceback
        print(f"Rig Picker: lifecycle step '{label}' failed")
        traceback.print_exc()


def _lift_detach_if_load_failed():
    """One-shot timer started at load_pre. After a load that worked,
    load_post has already swapped the store and lifted the detach, so this
    finds nothing to do. After one that FAILED (missing or corrupt file)
    load_post never fires and the old file is still open: lift the detach
    here, or nothing would be saved for the rest of the session."""
    if json_manager.is_detached():
        json_manager.swap_store()
    return None


@bpy.app.handlers.persistent
def _on_load_pre(*args):
    # A save that never finished may have left the buttons out of the scene;
    # put them back while the old file is still open, before anything reads it.
    _step("put N-panel buttons back", custom_actions_quick.restore_after_save)
    _step("controller settle", backend.on_load_pre)
    _step("quick panel settle", custom_actions_quick.settle_before_file_change)
    _step("flush N-panel auto-save", custom_actions_quick.flush_autosave)
    _step("flush window geometry", backend.flush_window_geometry)
    _step("detach", json_manager.detach)
    if not bpy.app.timers.is_registered(_lift_detach_if_load_failed):
        # persistent: a plain timer is cleared when a file loads.
        bpy.app.timers.register(
            _lift_detach_if_load_failed, first_interval=1.0, persistent=True
        )


@bpy.app.handlers.persistent
def _on_load_post(*args):
    _step("store swap", json_manager.swap_store)
    _step("backend/controller reset", backend.on_load_post)
    _step("quick panel load", custom_actions_quick._on_load_post)
    _step("apply window geometry", backend.apply_window_geometry)
    _step("register picker folder", json_manager.register_current_folder_if_present)


@bpy.app.handlers.persistent
def _on_save_pre(*args):
    _step("take N-panel buttons out of the .blend", custom_actions_quick.strip_before_save)


@bpy.app.handlers.persistent
def _on_save_post_fail(*args):
    _step("put N-panel buttons back", custom_actions_quick.restore_after_save)


@bpy.app.handlers.persistent
def _on_save_post(*args):
    # First: put the buttons back, before anything looks at the scene.
    _step("put N-panel buttons back", custom_actions_quick.restore_after_save)
    moved = None
    try:
        moved = json_manager.handle_file_saved()
    except Exception:
        import traceback
        print("Rig Picker: lifecycle step 'move data folder' failed")
        traceback.print_exc()
    _step("rebind window", json_manager.rebind_window)
    if moved:
        _step("re-point controller paths", backend.repoint_paths, *moved)
        _step("re-point quick panel paths", custom_actions_quick.repoint_paths, *moved)
    _step("N-panel auto-save location", custom_actions_quick.on_file_saved)
    _step("quick panel save", custom_actions_quick._on_save_post)


_LIFECYCLE_HANDLERS = (
    (bpy.app.handlers.load_pre, _on_load_pre),
    (bpy.app.handlers.load_post, _on_load_post),
    (bpy.app.handlers.save_pre, _on_save_pre),
    (bpy.app.handlers.save_post, _on_save_post),
) + (
    ((bpy.app.handlers.save_post_fail, _on_save_post_fail),)
    if hasattr(bpy.app.handlers, "save_post_fail") else ()
)


def _register_lifecycle_handlers():
    for handler_list, fn in _LIFECYCLE_HANDLERS:
        if fn not in handler_list:
            handler_list.append(fn)


def _unregister_lifecycle_handlers():
    for handler_list, fn in _LIFECYCLE_HANDLERS:
        if fn in handler_list:
            handler_list.remove(fn)
    if bpy.app.timers.is_registered(_lift_detach_if_load_failed):
        bpy.app.timers.unregister(_lift_detach_if_load_failed)


_MIN_BLENDER = (5, 0, 0)


def register():

    # The picker selects pose bones through PoseBone.select, which only
    # exists from Blender 5.0 (see backend.poll_active_armature()). On an
    # older Blender that would raise AttributeError the first time a bone
    # is selected, so refuse to enable with a clear message instead.
    if bpy.app.version < _MIN_BLENDER:
        raise RuntimeError(
            "Rig Picker requires Blender "
            + ".".join(str(v) for v in _MIN_BLENDER)
            + " or newer (this is Blender "
            + ".".join(str(v) for v in bpy.app.version)
            + ")."
        )

    for cls in classes:
        bpy.utils.register_class(cls)


    # Initialize backend UI/armature references. These are module-level
    # Python globals that survive across File > Open / File > Revert
    # within the same Blender process, so without resetting them here
    # they could keep pointing at objects/controllers from a previous
    # file - reset on every register() so we always start clean.
    backend._ACTIVE_CONTROLLER = None
    backend._ACTIVE_WINDOW = None
    backend._LAST_VIEW3D = None
    backend._CACHED_ARM = None
    backend._CACHED_ARM_NAME = None
    backend._CACHED_ARM_MODE = None
    backend._MODE_TRACKED_ARM_NAME = None
    backend._LAST_SELECTED_BONES = frozenset()

    # RP_OT_OpenPicker is a plain Python class, not re-created by
    # register_class - if the addon was previously disabled (or
    # scripts reloaded) while its background dependency-install thread
    # was still running, this class-level guard could otherwise be
    # left stuck True forever, permanently blocking every future
    # "Open Rig Picker" click with a false "already installing"
    # warning. Reset it on every register(), the same as the backend
    # globals above - UNLESS that background thread is still actually
    # alive (e.g. the addon was disabled and immediately re-enabled
    # while a first-time pip install was still mid-flight): daemon
    # threads aren't stopped by unregister() and keep installing
    # regardless, so resetting the guard in that case would let a
    # second "Open Rig Picker" click start a second, concurrent install
    # into the same target folder. _active_thread is a class attribute,
    # so it survives the disable/re-enable cycle right alongside
    # _installing itself.
    _stale_install_thread = RP_OT_OpenPicker._active_thread
    if _stale_install_thread is None or not _stale_install_thread.is_alive():
        RP_OT_OpenPicker._installing = False
        RP_OT_OpenPicker._active_thread = None

    # Forget the bound store so the next access re-resolves the open file
    # and re-reads its data.json from disk (Reload Scripts etc.).
    json_manager.reload()

    # ----------------------------------------------------
    # Watch for the active armature changing.
    #
    # depsgraph_update_post is NOT used for this: it's meant to fire on
    # actual data changes (transforms, mode switches, etc.) and is not a
    # reliable signal for a pure active-object/selection change - which is
    # exactly the case of clicking a different armature in the viewport.
    # A periodic timer sidesteps that unreliability entirely.
    #
    # persistent=True keeps this timer running across File > Open without
    # needing to be re-registered. Picker data lives entirely in
    # rig_picker_data.json via the JSON Manager - there are no Scene
    # properties to register here.
    # ----------------------------------------------------
    _register_lifecycle_handlers()

    if not bpy.app.timers.is_registered(poll_active_armature):
        bpy.app.timers.register(
            poll_active_armature,
            first_interval=_POLL_INTERVAL,
            persistent=True,
        )

    # ----------------------------------------------------
    # Quick Actions (custom Button/Toggle commands) - registers its own
    # classes and the Scene.rp_quick_action_settings property; see
    # custom_actions_quick.py. It draws as a sibling panel in the same
    # "Rig Picker" N-panel tab as RP_PT_MainPanel (below the "Open Rig
    # Picker" button, sharing its bl_category), not as its own separate
    # tab.
    # ----------------------------------------------------
    custom_actions_quick.register()

    backend.register_picker_undo_keymap()


def unregister():

    # Decided once, up front, while Blender's Remove/Uninstall operator is
    # still visible on the call stack. Plain disable and Reload Scripts
    # never delete anything.
    uninstalling = custom_actions_quick._is_uninstalling()

    # The lifecycle handlers go first: the uninstall cleanup below saves
    # the open file, and that save must not run them.
    _unregister_lifecycle_handlers()
    backend.unregister_picker_undo_keymap()

    # ----------------------------------------------------
    # Quick Actions panel - unregister before anything else below.
    # ----------------------------------------------------
    custom_actions_quick.unregister()

    # ----------------------------------------------------
    # Close the picker window if it's open, before the operator classes
    # it depends on are unregistered below - see close_picker()'s own
    # docstring.
    # ----------------------------------------------------
    try:
        from .ui.launcher import close_picker
        close_picker()
    except Exception:
        import traceback
        traceback.print_exc()

    # ----------------------------------------------------
    # Stop watching for armature changes
    # ----------------------------------------------------
    if bpy.app.timers.is_registered(poll_active_armature):
        bpy.app.timers.unregister(poll_active_armature)

    backend._ACTIVE_CONTROLLER = None
    backend._ACTIVE_WINDOW = None

    # ----------------------------------------------------
    # Uninstall only: wipe every picker data folder and the registry file.
    # Last of all the data-touching steps - the picker window has been
    # closed above (closing writes its geometry), the handlers and the
    # poll timer are gone, and the Quick panel has already cleaned the
    # button data from the open file and from the other .blend files it
    # knows about (custom_actions_quick.unregister()).
    # ----------------------------------------------------
    if uninstalling:
        try:
            removed = json_manager.wipe_all_data()
            print(f"Rig Picker: uninstall removed {len(removed)} data item(s).")
        except Exception:
            import traceback
            traceback.print_exc()

    # Same is-it-actually-still-running check as register() above -
    # the operator classes are about to be unregistered below regardless
    # (so nothing can click "Open Rig Picker" again until re-enabled),
    # but leaving _installing/_active_thread accurate here rather than
    # unconditionally clearing means register()'s own check on a
    # following re-enable still has the real thread reference to test,
    # instead of a state that already claims "not installing" no matter
    # what the daemon thread is actually doing in the background.
    _stale_install_thread = RP_OT_OpenPicker._active_thread
    if _stale_install_thread is None or not _stale_install_thread.is_alive():
        RP_OT_OpenPicker._installing = False
        RP_OT_OpenPicker._active_thread = None

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)