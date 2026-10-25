import ast
import math
import bpy
from collections import namedtuple
import bpy.utils.previews
import os
import uuid
from bpy.app.handlers import persistent
from bpy.types import Panel, Operator, PropertyGroup, UIList
from bpy.props import (
    StringProperty, CollectionProperty, IntProperty, BoolProperty,
    EnumProperty, PointerProperty,
)


# Custom icon (loaded on register, cleared on unregister)
_icon_collections = {}


def _edit_icon_id():
    pcoll = _icon_collections.get("main")
    if pcoll and "edit_icon" in pcoll:
        return pcoll["edit_icon"].icon_id
    return 0


class RP_ControlBoneItem(PropertyGroup):
    name: StringProperty(
        name="Bone Name",
        description="Name of a control shape bone that should trigger this command",
        default="",
    )


class RP_UL_control_bones(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # Plain label, not an editable field - a control shape bone
        # name only ever means anything here if it matches an actual
        # picker control (see RP_OT_quick_add_control_bone below, which
        # only ever fills this list off a real selection/picker
        # control). Letting it be typed/edited in place let the rigger
        # create entries that don't correspond to anything on the
        # picker at all - silently unreachable slots with no warning,
        # since nothing here validated free-typed text the way add does.
        settings = getattr(context.scene, "rp_quick_action_settings", None)
        rig = _quick_active_rig(settings) if settings is not None else None
        if _quick_rig_has_bone(rig, item.name):
            layout.label(text=item.name, icon='BONE_DATA')
        else:
            # Renamed or deleted on the rig since it was added - this
            # entry can never match a selection until it's removed or
            # replaced, so say so instead of showing it like a good one.
            layout.alert = True
            layout.label(text=f"{item.name}  (missing on rig)", icon='ERROR')


def _sync_quick_picker_ui():
    """Prototype counterpart to custom_actions._sync_picker_ui() - pushes
    this scene's Quick action buttons into the floating Qt Rig Picker
    window (see main_window.py's new sync_quick_actions() method), if
    that window is currently open, so adding/removing/renaming a Quick
    button shows up there immediately instead of only on the window's
    next rig switch.
    """
    from . import backend

    window = getattr(backend, "_ACTIVE_WINDOW", None)
    if window is None:
        return

    try:
        window.sync_quick_actions()
    except Exception:
        import traceback
        traceback.print_exc()


# uid -> (rig_name, panel_name) as this session last SAW each panel. Lives
# in plain Python memory, NOT in the scene, so a native Blender undo/redo
# (which rewinds every scene property, including a panel's name) can't
# rewind it too - that difference is exactly what lets
# _reconcile_panel_names_after_undo() notice "the name was changed behind
# my back" and move the panel's saved layout to match.
#
# LIMITS: this is session-only memory. (1) After Blender is restarted or a
# .blend is reopened, nothing is remembered, so an undo/redo of a rename
# can only be detected for panels this session has already seen (they're
# registered on the first poll tick after load, see
# _stamp_panel_prev_names()). (2) It only tracks panel NAME changes; it
# doesn't try to reconcile a panel that undo/redo removed or re-created
# beyond the deferred-deletion handling further below.
_PANEL_NAME_REGISTRY = {}


def _panel_uid(panel):
    """Stable per-panel id, stored as a hidden ID property (survives file
    save/load and follows the panel through undo). Created on demand."""
    uid = panel.get("_uid")
    if not uid:
        uid = uuid.uuid4().hex
        panel["_uid"] = uid
    return uid


# (rig_name, panel_name) -> time.monotonic() deadline. A panel removed from
# Manage Panels keeps its saved layout on disk until this grace period
# runs out, so Ctrl+Z (which brings the row and its buttons back, being
# scene data) also finds its layout again - same idea as
# Controller's ARMATURE_DELETE_GRACE_SECONDS for deleted rigs.
_PENDING_PANEL_DELETIONS = {}

# Subset of _PENDING_PANEL_DELETIONS keys whose removal has been SAVED to a
# .blend (see _on_save_post()). Only used when another file is opened: a
# saved removal is permanent in the file it was made in, so its layout can
# be deleted at that point; an unsaved one must be kept, because the
# previous file - reopened from disk - still has that panel.
_SAVED_PANEL_DELETIONS = set()
#
# LIMITS: (1) The pending list is plain memory. If Blender is quit (or
# crashes) inside the grace period, the removed panel's layout is never
# deleted and stays in rig_picker_data.json as an orphan - harmless, and
# it's discarded if a panel later takes that exact name (see
# _finalize_pending_panel_deletion()). Opening ANOTHER file inside the
# grace period is handled (see _resolve_pending_panel_deletions_on_file_load()):
# a removal that was saved is completed, one that wasn't is kept.
# (2) Purging only runs on poll ticks, so it needs the addon's timer
# running; it's a few ticks late, never early.
# (3) Undo restores the row and its buttons (scene data) only while the
# layout is still on disk - after the grace period it's gone for good.
PANEL_DELETE_GRACE_SECONDS = 60


def _schedule_panel_deletion(rig_name, panel_name):
    import time
    _PENDING_PANEL_DELETIONS.setdefault(
        (rig_name, panel_name), time.monotonic() + PANEL_DELETE_GRACE_SECONDS
    )


# (rig_name, panel_name) -> {"undo": stack|None, "redo": stack|None}: the
# picker's own undo/redo history of a panel removed from Manage Panels,
# parked for as long as the removal can be undone (the same window as
# _PENDING_PANEL_DELETIONS). Blender's own undo brings the row back; this is
# what lets picker Ctrl+Z keep going through what was done on that panel
# before it was removed. Restored when the panel is back in the scene,
# settled (staged image work finalized) when it can never come back.
_PENDING_PANEL_HISTORY = {}


def _park_panel_history(controller, rig_name, panel_name):
    key = (rig_name, panel_name)
    if key in _PENDING_PANEL_HISTORY:
        _settle_panel_history(key)        # never leak an earlier parked copy
    parked = controller.stash_panel_history(key)
    if parked["undo"] or parked["redo"]:
        _PENDING_PANEL_HISTORY[key] = parked


def _settle_panel_history(key):
    """The panel can never come back: finalize its parked history."""
    from . import backend
    parked = _PENDING_PANEL_HISTORY.pop(key, None)
    controller = backend._ACTIVE_CONTROLLER
    if parked and controller is not None:
        controller.settle_panel_history(key, parked)


def _restore_panel_history(key):
    """The panel is back in the scene (its removal was undone)."""
    from . import backend
    parked = _PENDING_PANEL_HISTORY.pop(key, None)
    controller = backend._ACTIVE_CONTROLLER
    if parked and controller is not None:
        controller.restore_panel_history(key, parked)


def _finalize_pending_panel_deletion(rig_name, panel_name):
    """Deletes a pending panel's saved layout NOW. Called before a NEW
    panel takes over a name that still has a layout waiting out its grace
    period, so the new panel starts blank instead of inheriting it."""
    from . import json_manager
    _SAVED_PANEL_DELETIONS.discard((rig_name, panel_name))
    if _PENDING_PANEL_DELETIONS.pop((rig_name, panel_name), None) is not None:
        _settle_panel_history((rig_name, panel_name))
        json_manager.delete_panel_data(rig_name, panel_name)
        _forget_registry_entries(rig_name, panel_name)


def _forget_registry_entries(rig_name, panel_name):
    for uid in [u for u, v in _PANEL_NAME_REGISTRY.items() if v == (rig_name, panel_name)]:
        _PANEL_NAME_REGISTRY.pop(uid, None)


def purge_rig_quick_data(rig_name):
    """Removes every trace of a rig that is gone for good from the Quick
    panel's scene data: its multi-command buttons and its Manage Panels
    rows, in EVERY scene of the file (the scene the user happens to have
    open isn't special - a rig that no longer exists anywhere in the file
    is gone from all of them), plus the session memory that mentions it.
    Called by Controller._purge_expired_armature_deletions() only once the
    rig is confirmed absent from bpy.data entirely, so a rig that still
    lives in another scene is never purged by this."""
    global _SYNCING_QUICK_PANEL_DROPDOWN

    for scene in bpy.data.scenes:
        qs = getattr(scene, "rp_quick_action_settings", None)
        if qs is None:
            continue

        # Buttons are removed with the rig their owning PANEL belongs to
        # (before the panels themselves are removed below, while that
        # link can still be resolved); the stored armature_name only
        # decides for a button with no resolvable panel (legacy/orphan).
        for i in range(len(qs.multi_actions) - 1, -1, -1):
            action = qs.multi_actions[i]
            owner_rig = quick_action_rig_name(qs, action) or action.armature_name
            if owner_rig == rig_name:
                qs.multi_actions.remove(i)

        # Removing rows shifts panels_index: remember which panel was
        # selected and re-point the index at it afterwards (guarded so the
        # re-point doesn't trigger a panel switch).
        selected = None
        if 0 <= qs.panels_index < len(qs.panels):
            sel = qs.panels[qs.panels_index]
            if sel.armature_name != rig_name:
                selected = (sel.armature_name, sel.name)

        removed_any = False
        for i in range(len(qs.panels) - 1, -1, -1):
            if qs.panels[i].armature_name == rig_name:
                qs.panels.remove(i)
                removed_any = True

        if removed_any:
            new_index = next(
                (
                    i for i, p in enumerate(qs.panels)
                    if selected and (p.armature_name, p.name) == selected
                ),
                max(0, min(qs.panels_index, len(qs.panels) - 1)),
            )
            _SYNCING_QUICK_PANEL_DROPDOWN = True
            try:
                qs.panels_index = new_index
            finally:
                _SYNCING_QUICK_PANEL_DROPDOWN = False

    from . import json_manager
    for entry in [e for e in _PENDING_RIG_CLEARS if e["rig"] == rig_name]:
        if entry["state"] == "cleared":
            json_manager.discard_armature_backup(entry["token"])
        _discard_entry_history(entry)
        _PENDING_RIG_CLEARS.remove(entry)

    _LAST_PANEL_BY_RIG.pop(rig_name, None)
    for uid in [u for u, v in _PANEL_NAME_REGISTRY.items() if v[0] == rig_name]:
        _PANEL_NAME_REGISTRY.pop(uid, None)
    for key in [k for k in _PENDING_PANEL_DELETIONS if k[0] == rig_name]:
        _PENDING_PANEL_DELETIONS.pop(key, None)
        _SAVED_PANEL_DELETIONS.discard(key)
    for key in [k for k in _PENDING_PANEL_HISTORY if k[0] == rig_name]:
        _settle_panel_history(key)


# Clear All (Controller.clear_all) removes every panel row of a rig AND its
# saved layouts. The rows/buttons are scene data, so Ctrl+Z brings them
# back by itself - but the layouts live in rig_picker_data.json, which undo
# doesn't touch. Each Clear All therefore keeps a reversible backup of the
# rig's layouts here for a while, and _update_rig_clears_after_undo()
# restores / re-applies it as undo/redo moves the scene back and forth.
#
# entry = {
#   "rig", "token" (json_manager.backup_armature_data(), None once
#   restored), "uids" (the removed rows' panel uids), "default_uid" (the
#   uid of the fresh "Panel 1" row Clear All created), "state" ("cleared"
#   | "undone"), "deadline", "saved" (a save happened while cleared)
# }
#
# LIMITS: plain memory, same as the pending panel deletions - quitting
# Blender inside the window leaves the backed-up images as <name>.png.
# clearbak files next to the layout images (harmless; never auto-cleaned).
# A redo can only re-apply the clear while the "undone" watch below is
# alive.
_PENDING_RIG_CLEARS = []
RIG_CLEAR_REDO_WATCH_SECONDS = 600
# How long a Clear All can still be undone (its layouts/images are kept aside).
RIG_CLEAR_UNDO_SECONDS = 600


def schedule_rig_clear(rig_name, token, original_uids):
    """Registers a Clear All's backup. Returns the entry (or None when
    nothing was stored, so there is nothing to protect)."""
    import time
    if token is None:
        return None
    entry = {
        "rig": rig_name,
        "token": token,
        "uids": {u for u in original_uids if u},
        "default_uid": None,
        "state": "cleared",
        "deadline": time.monotonic() + RIG_CLEAR_UNDO_SECONDS,
        "saved": False,
    }
    _PENDING_RIG_CLEARS.append(entry)
    return entry


def note_rig_clear_default(entry, settings, rig_name):
    """Records the uid of the fresh default panel row Clear All just made.
    It is what tells a REDO of the clear (this row coming back while the
    original rows stay gone) apart from any other panel removal."""
    if entry is None:
        return
    for panel in settings.panels:
        if panel.armature_name == rig_name:
            entry["default_uid"] = _panel_uid(panel)
            return


def _reset_controller_after_clear_change(rig_name):
    """The picker's loaded panel is stale after the layouts under it were
    swapped by an undo/redo. Drop its reference (WITHOUT saving - saving
    would write the stale in-memory data straight over the swap) so the
    next poll tick re-loads whatever panel the scene now shows."""
    from . import backend, json_manager
    controller = backend._ACTIVE_CONTROLLER
    if controller is not None and controller.armature_name == rig_name:
        controller.panel_name = None
        controller.data = json_manager.new_panel_data()
        controller._quick_synced_key = None


def _restore_history_after_clear_undone(entry):
    """The Clear All was undone: bring back the picker undo/redo history it
    parked (Controller.clear_all()), replacing the fresh panel's."""
    from . import backend
    controller = backend._ACTIVE_CONTROLLER
    history = entry.pop("history", None)
    if controller is not None:
        controller.restore_rig_history(entry["rig"], history)


def _stash_history_after_clear_redone(entry):
    """The Clear All was redone: park the picker history again."""
    from . import backend
    controller = backend._ACTIVE_CONTROLLER
    if controller is not None:
        entry["history"] = controller.stash_rig_history(entry["rig"])


def _discard_entry_history(entry):
    """The entry is going away for good: settle whatever history it parked."""
    from . import backend
    history = entry.pop("history", None)
    controller = backend._ACTIVE_CONTROLLER
    if history and controller is not None:
        controller.settle_rig_history(history)


def _update_rig_clears_after_undo():
    """After an undo/redo: an UNDO of a Clear All shows up as the removed
    rows coming back while the fresh default row disappears; a REDO as the
    reverse. Restore / re-apply the layouts to match."""
    if not _PENDING_RIG_CLEARS:
        return
    import time
    from . import json_manager

    # discard(None): a panel the poll tick hasn't stamped yet has no uid,
    # and a None in this set would make an entry whose default_uid was
    # never recorded (also None) look like its default row is "back".
    present = {p.get("_uid") for p in _all_scene_panels()}
    present.discard(None)
    for entry in _PENDING_RIG_CLEARS:
        originals_back = bool(entry["uids"] & present)
        default_back = entry["default_uid"] is not None and entry["default_uid"] in present

        if entry["state"] == "cleared" and originals_back and not default_back:
            json_manager.restore_armature_backup(entry["token"])
            entry["token"] = None
            entry["state"] = "undone"
            entry["deadline"] = time.monotonic() + RIG_CLEAR_REDO_WATCH_SECONDS
            _restore_history_after_clear_undone(entry)
            _reset_controller_after_clear_change(entry["rig"])

        elif entry["state"] == "undone" and default_back and not originals_back:
            entry["token"] = json_manager.backup_armature_data(entry["rig"])
            entry["state"] = "cleared"
            entry["deadline"] = time.monotonic() + PANEL_DELETE_GRACE_SECONDS
            _stash_history_after_clear_redone(entry)
            _reset_controller_after_clear_change(entry["rig"])


# ---------------------------------------------------------------------
# Picker-driven undo/redo of Clear All
# ---------------------------------------------------------------------
# Clear All removes scene data (the rig's Manage Panels rows and its
# multi-command buttons) next to the layouts in rig_picker_data.json.
# Blender's own undo restores the scene data on a viewport Ctrl+Z (see
# _update_rig_clears_after_undo() above), but it can't be driven from the
# picker window: bpy.ops.ed.undo refuses to run from there. So Clear All
# also keeps its own copy of the scene data and the picker undoes/redoes it
# itself. Values are copied as raw ID properties (to_dict()/item[key]=...),
# which does not fire the properties' update callbacks - re-assigning a
# panel's name, say, would otherwise try to rename layouts on disk.

def _props_to_dict(item):
    """Raw ID-property contents of a PropertyGroup item as plain Python.
    (A registered PropertyGroup exposes them through id_properties_ensure();
    to_dict() is only tried as a fallback.)"""
    try:
        return item.id_properties_ensure().to_dict()
    except Exception:
        return item.to_dict()


def snapshot_rig_scene_data(settings, rig_name):
    return {
        "panels": [_props_to_dict(p) for p in settings.panels
                   if p.armature_name == rig_name],
        "multi_actions": [_props_to_dict(m) for m in settings.multi_actions
                          if m.armature_name == rig_name],
    }


def _remove_rig_scene_rows(settings, rig_name):
    for collection in (settings.panels, settings.multi_actions):
        for i in range(len(collection) - 1, -1, -1):
            if collection[i].armature_name == rig_name:
                collection.remove(i)


def _restore_rig_scene_rows(settings, rig_name, data):
    _remove_rig_scene_rows(settings, rig_name)
    for collection, key in ((settings.panels, "panels"),
                            (settings.multi_actions, "multi_actions")):
        for values in data.get(key, ()):
            item = collection.add()
            for k, v in values.items():
                item[k] = v
    matching = [i for i, p in enumerate(settings.panels)
                if p.armature_name == rig_name]
    if matching:
        settings.panels_index = matching[0]


def keep_clear_scene_snapshot(entry, settings, rig_name):
    """Clear All calls this BEFORE it removes the rows."""
    if entry is not None:
        try:
            entry["scene"] = snapshot_rig_scene_data(settings, rig_name)
        except Exception:
            # Never let this break Clear All itself; without the copy the
            # clear simply can't be undone from the picker.
            import traceback
            traceback.print_exc()
            print("Rig Picker: could not keep a copy of the panels for undoing Clear All.")


def undo_rig_clear(entry):
    """Brings a Clear All back. True if it did."""
    import time
    from . import json_manager
    if (entry is None or entry not in _PENDING_RIG_CLEARS
            or entry["state"] != "cleared" or "scene" not in entry):
        print("Rig Picker: Clear All can't be undone (no saved copy of it).")
        return False
    settings = bpy.context.scene.rp_quick_action_settings
    try:
        _restore_rig_scene_rows(settings, entry["rig"], entry["scene"])
    except Exception:
        import traceback
        traceback.print_exc()
        print("Rig Picker: restoring the panels for Clear All undo failed.")
        return False
    json_manager.restore_armature_backup(entry["token"])
    entry["token"] = None
    entry["state"] = "undone"
    entry["deadline"] = time.monotonic() + RIG_CLEAR_REDO_WATCH_SECONDS
    _restore_history_after_clear_undone(entry)
    _reset_controller_after_clear_change(entry["rig"])
    _sync_quick_picker_ui()
    return True


def redo_rig_clear(entry):
    """Clears the rig again after undo_rig_clear(). True if it did."""
    import time
    from . import json_manager
    if (entry is None or entry not in _PENDING_RIG_CLEARS
            or entry["state"] != "undone"):
        return False
    rig = entry["rig"]
    settings = bpy.context.scene.rp_quick_action_settings
    entry["scene"] = snapshot_rig_scene_data(settings, rig)
    entry["token"] = json_manager.backup_armature_data(rig)
    _remove_rig_scene_rows(settings, rig)
    _ensure_default_panel_for_rig(settings, rig)
    note_rig_clear_default(entry, settings, rig)
    entry["state"] = "cleared"
    entry["deadline"] = time.monotonic() + RIG_CLEAR_UNDO_SECONDS
    _stash_history_after_clear_redone(entry)
    _reset_controller_after_clear_change(rig)
    _sync_quick_picker_ui()
    return True


def _purge_expired_rig_clears():
    """Poll tick: a Clear All whose grace period ran out without an undo
    gets its set-aside images deleted for good; an expired redo watch is
    simply dropped."""
    if not _PENDING_RIG_CLEARS:
        return
    import time
    from . import json_manager

    now = time.monotonic()
    for entry in list(_PENDING_RIG_CLEARS):
        if now < entry["deadline"]:
            continue
        if entry["state"] == "cleared":
            json_manager.discard_armature_backup(entry["token"])
        _discard_entry_history(entry)
        _PENDING_RIG_CLEARS.remove(entry)


def _all_scene_panels():
    """Every RP_QuickPanel in EVERY scene of the open file. Panel layouts
    live in rig_picker_data.json keyed by rig/panel NAME, independent of
    which scene a panel row belongs to, so "is this panel gone?" has to
    be answered against all scenes - checking only the current scene would
    make a panel that merely lives in another scene look removed and get
    its layout deleted."""
    panels = []
    for scene in bpy.data.scenes:
        scene_settings = getattr(scene, "rp_quick_action_settings", None)
        if scene_settings is not None:
            panels.extend(scene_settings.panels)
    return panels


def _schedule_missing_panel_deletions(settings):
    """After an undo/redo: any panel this session saw that is no longer in
    the scene (a REDO of a removal) gets its layout scheduled for deletion
    again, since the removal operator's own scheduling already ran once
    and was cancelled when undo brought the panel back."""
    all_panels = _all_scene_panels()
    present_uids = {p.get("_uid") for p in all_panels}
    present_keys = {(p.armature_name, p.name) for p in all_panels}
    for uid, key in list(_PANEL_NAME_REGISTRY.items()):
        if uid not in present_uids and key not in present_keys:
            _schedule_panel_deletion(*key)


def _purge_expired_panel_deletions(settings):
    """Called every poll tick. A pending panel that is back in the scene
    (undo) is simply un-scheduled; one still gone after the grace period
    has its layout (and captured image) deleted for good."""
    _purge_expired_rig_clears()
    if not _PENDING_PANEL_DELETIONS:
        return
    import time
    from . import json_manager

    live = {(p.armature_name, p.name) for p in _all_scene_panels()}
    now = time.monotonic()
    for key, deadline in list(_PENDING_PANEL_DELETIONS.items()):
        if key in live:
            del _PENDING_PANEL_DELETIONS[key]
            _SAVED_PANEL_DELETIONS.discard(key)
            _restore_panel_history(key)
        elif now >= deadline:
            del _PENDING_PANEL_DELETIONS[key]
            _SAVED_PANEL_DELETIONS.discard(key)
            _settle_panel_history(key)
            json_manager.delete_panel_data(*key)
            _forget_registry_entries(*key)


def _reconcile_panel_names_after_undo(settings):
    """Native undo/redo of a panel rename reverts the panel's NAME (scene
    data) but not rig_picker_data.json, so the saved layout stayed under
    the un-undone name and the panel came up empty. Compare each panel's
    current name with what this session last saw for it and migrate the
    saved layout (and the live Controller, if that panel is loaded) to the
    name undo/redo just restored. multi_actions' panel_name is scene data
    and is rewound by the same undo step, so it needs no help here."""
    from . import json_manager, backend

    # READ-ONLY with respect to scene data on purpose: this runs inside an
    # undo_post/redo_post handler, a context where Blender may refuse
    # writes to ID data. A panel with no "_uid" yet is simply skipped here;
    # the poll tick (_stamp_panel_prev_names(), a timer, where writing is
    # allowed) stamps it and registers its name.
    for panel in settings.panels:
        uid = panel.get("_uid")
        if not uid:
            continue
        seen = _PANEL_NAME_REGISTRY.get(uid)
        if seen is None:
            _PANEL_NAME_REGISTRY[uid] = (panel.armature_name, panel.name)
            continue

        seen_rig, seen_name = seen
        if seen_rig == panel.armature_name and seen_name != panel.name:
            json_manager.rename_panel_data(panel.armature_name, seen_name, panel.name)

            controller = backend._ACTIVE_CONTROLLER
            if (
                controller is not None
                and controller.armature_name == panel.armature_name
                and controller.panel_name == seen_name
            ):
                controller.handle_panel_renamed(seen_name, panel.name)

        # (No write to the panel's "_prev_name" here: the undo step that
        # restored the name restored that hidden property along with it,
        # so the two already agree.)
        _PANEL_NAME_REGISTRY[uid] = (panel.armature_name, panel.name)


@persistent
def _on_undo_redo(dummy=None):
    """Prototype "Quick" panel counterpart to custom_actions._on_undo_redo()
    - keeps an open Rig Picker window's Quick action buttons in sync
    after a native Blender undo/redo (Ctrl+Z in the viewport) of one of
    this module's own operators (Add/Remove/Rename, or a Run click's
    RNA changes) - see custom_actions._sync_picker_ui()'s docstring for
    why this can't rely on the operators' own execute() being called
    again. Also re-aligns saved panel layouts with any panel name the
    undo/redo just changed (see _reconcile_panel_names_after_undo()).
    """
    try:
        scene = bpy.context.scene
        settings = getattr(scene, "rp_quick_action_settings", None) if scene else None
        if settings is not None:
            _reconcile_panel_names_after_undo(settings)
            _update_rig_clears_after_undo()
            _update_control_deletes_after_undo()
            _schedule_missing_panel_deletions(settings)
    except Exception:
        import traceback
        traceback.print_exc()

    _sync_quick_picker_ui()


@persistent
def _on_frame_change_update_quick_toggles(scene, depsgraph=None):
    """Registered as a frame_change_post handler - Quick-panel
    counterpart to custom_actions._on_frame_change_update_toggles().
    Thin wrapper around sync_quick_toggle_states() below.
    """
    sync_quick_toggle_states(scene)


@persistent
def _on_save_post(*args):
    """Marks every pending panel removal that the file just saved no longer
    contains as SAVED - i.e. permanent in that .blend. See
    _SAVED_PANEL_DELETIONS. Also records whether the file just written
    contains button data (see _update_blend_tracking())."""
    _update_blend_tracking()
    try:
        if _PENDING_RIG_CLEARS:
            present = {p.get("_uid") for p in _all_scene_panels()}
            present.discard(None)
            for entry in _PENDING_RIG_CLEARS:
                if entry["state"] == "cleared" and not (entry["uids"] & present):
                    entry["saved"] = True
        if not _PENDING_PANEL_DELETIONS:
            return
        live = {(p.armature_name, p.name) for p in _all_scene_panels()}
        for key in _PENDING_PANEL_DELETIONS:
            if key not in live:
                _SAVED_PANEL_DELETIONS.add(key)
    except Exception:
        import traceback
        traceback.print_exc()


def _resolve_pending_panel_deletions_on_file_load():
    """Opening a file wipes undo history, so the grace period that pending
    removals were waiting out can no longer be used by anyone - settle
    them now instead of letting them carry over into the new file.

    Runs at load_pre (see settle_before_file_change()), while the
    OUTGOING file is still open and its store is still the current one:
      * removal was SAVED in the outgoing file -> delete the layout now
        (nothing can bring it back);
      * removal was never saved -> keep the layout: reopening the
        outgoing file from disk brings that panel back.
    Each file has its own store, so there is no longer a "the new file
    has a live panel of that name" case to guard against.
    """
    from . import json_manager

    # A newly opened file starts with an empty undo history, so nothing
    # remembered about picker deletes can be undone/redone any more.
    _PENDING_CONTROL_DELETES.clear()
    json_manager._REMOVED_OVERLAY_POSITIONS.clear()

    # Pending Clear Alls settle the same way: saved -> permanent (drop the
    # backup); never saved -> the outgoing file still has those panels on
    # disk, so put back every layout that doesn't collide with something
    # already stored under the same rig name.
    for entry in _PENDING_RIG_CLEARS:
        if entry["state"] != "cleared":
            continue
        if entry["saved"]:
            json_manager.discard_armature_backup(entry["token"])
        else:
            json_manager.restore_armature_backup(entry["token"], only_missing=True)
    for entry in _PENDING_RIG_CLEARS:
        _discard_entry_history(entry)
    _PENDING_RIG_CLEARS.clear()

    if not _PENDING_PANEL_DELETIONS:
        _SAVED_PANEL_DELETIONS.clear()
        return

    for key in list(_PENDING_PANEL_HISTORY):
        _settle_panel_history(key)

    live = {(p.armature_name, p.name) for p in _all_scene_panels()}
    for key in list(_PENDING_PANEL_DELETIONS):
        if key in _SAVED_PANEL_DELETIONS and key not in live:
            json_manager.delete_panel_data(*key)
    _PENDING_PANEL_DELETIONS.clear()
    _SAVED_PANEL_DELETIONS.clear()


def repoint_paths(old_folder, new_folder):
    """After a first save or Save As moved/copied the data folder:
    re-points the image paths held by pending Clear All backups."""
    from . import json_manager
    tr = json_manager.translate_path
    try:
        for entry in _PENDING_RIG_CLEARS:
            token = entry.get("token")
            if not token:
                continue
            token["images"] = {
                tr(path, old_folder, new_folder): tr(backup, old_folder, new_folder)
                for path, backup in token["images"].items()
            }
            for panel_data in token["data"]["panels"].values():
                if panel_data.get("background"):
                    panel_data["background"] = tr(
                        panel_data["background"], old_folder, new_folder)
    except Exception:
        import traceback
        traceback.print_exc()


def settle_before_file_change():
    """Settle half of a file switch for the Quick panel: called by the
    lifecycle dispatcher at load_pre, against the outgoing file's store.
    Never raises - a failure here must not block the file from loading."""
    try:
        _resolve_pending_panel_deletions_on_file_load()
    except Exception:
        import traceback
        traceback.print_exc()


def _on_load_post(dummy=None):
    """Registered as a load_post handler - runs
    _migrate_legacy_multi_action_panels() against the newly-opened
    file's scenes, so a .blend saved before Section 1 buttons became
    panel-scoped gets migrated the moment it's opened, not only in
    whichever file happened to be open when this addon version was
    first enabled (register() below covers that one case directly,
    since load_post doesn't fire for a file already open).
    """
    # The session-memory dicts above describe the PREVIOUS file's panels.
    # Carrying them over could make _schedule_missing_panel_deletions()
    # treat the old file's panels as "removed" and, a minute later, delete
    # layouts from the NEW file's store. Start clean; the first poll tick
    # re-registers the new file's panels. (Pending removals of the old
    # file were already settled at load_pre.)
    global _RIG_NAMES_DIRTY
    _PANEL_NAME_REGISTRY.clear()
    _LAST_PANEL_BY_RIG.clear()
    _KNOWN_RIG_NAMES_BY_UID.clear()
    _RIG_RENAME_MEMO.clear()
    _RIG_NAMES_DIRTY = True
    subscribe_rig_name_messages()
    # Put back the N-panel buttons stored for this file (they may be newer
    # than the ones in the .blend), THEN migrate old-format buttons.
    _restore_and_arm()
    _migrate_legacy_multi_action_panels()
    _update_blend_tracking()


def _on_rig_renamed(old_name, new_name):
    """Moves this module's session-memory records (keyed by rig name) over
    to a rig's new name. Called from Controller._handle_armature_renamed()
    right after it migrates the scene data and the JSON entry, so the
    per-rig remembered panel, pending panel deletions and the rename
    registry keep pointing at the same rig instead of at a dead name."""
    if old_name == new_name:
        return
    for entry in _PENDING_RIG_CLEARS:
        if entry["rig"] == old_name:
            entry["rig"] = new_name
            if entry["token"] is not None:
                entry["token"]["armature"] = new_name
    if old_name in _LAST_PANEL_BY_RIG:
        _LAST_PANEL_BY_RIG[new_name] = _LAST_PANEL_BY_RIG.pop(old_name)
    for key in [k for k in _PENDING_PANEL_HISTORY if k[0] == old_name]:
        _PENDING_PANEL_HISTORY[(new_name, key[1])] = _PENDING_PANEL_HISTORY.pop(key)
    for key in [k for k in _PENDING_PANEL_DELETIONS if k[0] == old_name]:
        _PENDING_PANEL_DELETIONS[(new_name, key[1])] = _PENDING_PANEL_DELETIONS.pop(key)
        if key in _SAVED_PANEL_DELETIONS:
            _SAVED_PANEL_DELETIONS.discard(key)
            _SAVED_PANEL_DELETIONS.add((new_name, key[1]))
    for uid, (rig, name) in list(_PANEL_NAME_REGISTRY.items()):
        if rig == old_name:
            _PANEL_NAME_REGISTRY[uid] = (new_name, name)


def rename_rig_scene_data(old_name, new_name):
    """Rewrites the rig name stored on every multi-command button and every
    Manage Panels row, in EVERY scene of the file (not just the open one -
    a scene that isn't active would otherwise keep pointing at a name that
    no longer exists and lose all its buttons/panels), then moves the
    session-memory records over too. Idempotent: running it again for the
    same rename finds nothing left under old_name and changes nothing."""
    if not old_name or not new_name or old_name == new_name:
        return
    for scene in bpy.data.scenes:
        qs = getattr(scene, "rp_quick_action_settings", None)
        if qs is None:
            continue
        for action in qs.multi_actions:
            if action.armature_name == old_name:
                action.armature_name = new_name
        for panel in qs.panels:
            if panel.armature_name == old_name:
                panel.armature_name = new_name
    _on_rig_renamed(old_name, new_name)


# session_uid -> (old_name, new_name, time) of that rig object's most recent
# rename. BOTH rename detectors (this module's track_rig_renames() and
# Controller._sync_armature_combo_items()) see the same rename and both
# call migrate_rig_renames(); this lets the second caller recognise that
# the first one already moved the data for THIS rig's THIS rename, so it
# isn't moved twice (harmless for a single rename, but wrong for a swap,
# where a second pass would shuffle the two rigs' data back). Keyed by the
# object, not the name pair, so repeating an identical rename later (or
# swapping the same two rigs twice) is still treated as a new event.
_RIG_RENAME_MEMO = {}
RIG_RENAME_MEMO_SECONDS = 30.0


def _apply_rig_rename(old_name, new_name):
    from . import json_manager, backend
    json_manager.rename_armature_data(old_name, new_name)
    rename_rig_scene_data(old_name, new_name)
    if backend._CACHED_ARM_NAME == old_name:
        backend._CACHED_ARM_NAME = new_name


def rig_renames_conflict(pairs):
    """True if any rename's new name is still another rename's OLD name - a
    swap (A<->B) or longer cycle - which can't be applied one at a time
    without the first rename landing on top of the second rig. Each item
    is (old, new) or (old, new, uid)."""
    olds = {item[0] for item in pairs}
    return any(item[1] in olds for item in pairs)


def migrate_rig_renames(events):
    """Moves every piece of rig-name-keyed data - the rig's layouts in
    rig_picker_data.json, the buttons and panel rows in every scene, and
    this module's session memory - from each event's old rig name to its
    new one. events: list of (old_name, new_name, session_uid). A
    conflicting set (swapped names) is applied in two phases through
    unique temporary names so no rig ever lands on a name another is still
    using. Events this session already migrated (see _RIG_RENAME_MEMO) are
    skipped."""
    import time

    now = time.monotonic()
    todo = []
    for old, new, uid in events:
        if old == new:
            continue
        seen = _RIG_RENAME_MEMO.get(uid)
        if seen is not None and seen[:2] == (old, new) and now - seen[2] <= RIG_RENAME_MEMO_SECONDS:
            continue
        todo.append((old, new, uid))
    if not todo:
        return

    if rig_renames_conflict(todo):
        staged = []
        for old, new, _uid in todo:
            temp = f"__rig_rename_{uuid.uuid4().hex}__"
            _apply_rig_rename(old, temp)
            staged.append((temp, new))
        for temp, new in staged:
            _apply_rig_rename(temp, new)
    else:
        for old, new, _uid in todo:
            _apply_rig_rename(old, new)

    for old, new, uid in todo:
        _RIG_RENAME_MEMO[uid] = (old, new, now)


# session_uid -> rig object name as of the previous scan. session_uid is
# stable across a rename (the name isn't), which is what makes "same rig,
# new name" distinguishable from "one rig deleted, another added".
_KNOWN_RIG_NAMES_BY_UID = {}

# Scanning every object in the file on every 0.15s poll tick is wasted
# work almost every tick - rig names rarely change. Instead a msgbus
# subscription on Object.name flips this flag whenever ANY object is
# renamed, and the scan only runs when it's set. A slow periodic scan stays
# as a safety net (msgbus delivery isn't guaranteed for every kind of
# rename, e.g. one made deep inside another operator).
_RIG_NAMES_DIRTY = True
_LAST_RIG_SCAN_TIME = 0.0
RIG_SCAN_SAFETY_INTERVAL_SECONDS = 2.0
_MSGBUS_OWNER = object()


def _on_object_name_message(*args):
    global _RIG_NAMES_DIRTY
    _RIG_NAMES_DIRTY = True


def subscribe_rig_name_messages():
    """(Re)subscribes to Object.name changes. msgbus subscriptions are
    dropped whenever a file is loaded, so this runs from register() and
    from the load_post handler. Failure is non-fatal: the periodic safety
    scan in track_rig_renames() still catches every rename, just up to
    RIG_SCAN_SAFETY_INTERVAL_SECONDS later."""
    try:
        bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
        bpy.msgbus.subscribe_rna(
            key=(bpy.types.Object, "name"),
            owner=_MSGBUS_OWNER,
            args=(),
            notify=_on_object_name_message,
        )
    except Exception:
        import traceback
        traceback.print_exc()


def unsubscribe_rig_name_messages():
    try:
        bpy.msgbus.clear_by_owner(_MSGBUS_OWNER)
    except Exception:
        pass


def track_rig_renames():
    """Poll-tick rename detection that does NOT depend on the Qt picker.

    Controller._sync_armature_combo_items() detects renames too, but it
    only exists while the picker window is open (its tracker lives on the
    Controller). The N-panel's buttons and panels are scoped by rig NAME
    and work with the picker closed, so a rig renamed in the Outliner/F2
    meanwhile would silently lose all of them - and once the picker opened
    later there would be no record of the old name left to recover from.
    This runs from backend.poll_active_armature() every tick, but only
    actually scans when an object rename was signalled (or the safety
    interval elapsed). Both detectors go through migrate_rig_renames(), so
    whichever sees a rename first moves the data and the other skips it."""
    import time
    global _RIG_NAMES_DIRTY, _LAST_RIG_SCAN_TIME

    now = time.monotonic()
    if (
        _KNOWN_RIG_NAMES_BY_UID
        and not _RIG_NAMES_DIRTY
        and now - _LAST_RIG_SCAN_TIME < RIG_SCAN_SAFETY_INTERVAL_SECONDS
    ):
        return
    _RIG_NAMES_DIRTY = False
    _LAST_RIG_SCAN_TIME = now

    current = {
        ob.session_uid: ob.name for ob in bpy.data.objects if ob.type == 'ARMATURE'
    }
    renamed = [
        (_KNOWN_RIG_NAMES_BY_UID[uid], name, uid)
        for uid, name in current.items()
        if uid in _KNOWN_RIG_NAMES_BY_UID and _KNOWN_RIG_NAMES_BY_UID[uid] != name
    ]
    _KNOWN_RIG_NAMES_BY_UID.clear()
    _KNOWN_RIG_NAMES_BY_UID.update(current)

    migrate_rig_renames(renamed)


# --- Compiled-command caches, keyed by the exact command string -----------
# -----------------------------------------------------------------------------
# Safety net for Quick Action commands
# -----------------------------------------------------------------------------
# Button/Toggle commands are full Python, run when the button is CLICKED.
# Most of what a command can do is fine, but a few calls are known to crash
# or wedge Blender when run from inside a running operator / Qt click
# handler: undo/redo (rewinds the very data the operator is editing),
# loading/reverting/saving/quitting a file (replaces everything, including
# this addon's own state), starting a blocking render, and unloading an
# addon (including this one) while its code is mid-execution. These are
# refused with a normal error message instead.
#
# LIMIT: this is a static check of the command text (calls written as
# bpy.ops.<module>.<op>(...) etc.). It catches accidents, not a command
# that deliberately hides the call (aliasing `ops = bpy.ops`, getattr
# tricks, exec of a built string, ...) - it is not a sandbox.
_BLOCKED_OPERATORS = frozenset({
    "ed.undo", "ed.redo", "ed.undo_history",
    "wm.open_mainfile", "wm.revert_mainfile", "wm.read_homefile",
    "wm.read_factory_settings", "wm.recover_last_session",
    "wm.recover_auto_save", "wm.save_mainfile", "wm.save_as_mainfile",
    "wm.save_homefile", "wm.quit_blender", "wm.window_close",
    "render.render",
    "script.reload",
    "preferences.addon_disable", "preferences.addon_remove",
    "preferences.addon_install", "preferences.addon_refresh",
})
_BLOCKED_CALL_NAMES = frozenset({
    "exit", "quit", "sys.exit", "os._exit", "addon_utils.disable",
    "bpy.utils.unregister_class", "bpy.utils.unregister_module",
})


def _dotted_call_name(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def command_blocked_reason(command):
    """Returns a human-readable reason if `command` calls something on the
    blocklist above, else None. Commands that don't even parse return None
    so the normal SyntaxError reporting still happens at exec time."""
    try:
        tree = ast.parse(command, mode='exec')
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _dotted_call_name(node.func)
        if not name:
            continue
        for prefix in ("bpy.ops.", "C.ops.", "context.ops."):
            if name.startswith(prefix) and name[len(prefix):] in _BLOCKED_OPERATORS:
                return (f"'{name}' isn't allowed in a button command - it can "
                        f"crash Blender when run from the picker.")
        if name in _BLOCKED_CALL_NAMES:
            return (f"'{name}' isn't allowed in a button command - it can "
                    f"crash or close Blender.")
    return None


# Commands are arbitrary Python run with exec(), so they are NOT
# transactional: if one changes several properties and then raises, the
# changes made before the exception stay (the operator returns CANCELLED,
# which pushes no undo step, and nothing here records what the command
# touched). Full rollback of arbitrary code is not feasible, so the failure
# report says so instead of implying the scene is untouched. See
# AUDIT_NOTES.md, "Commands are not transactional".
COMMAND_NOT_TRANSACTIONAL_NOTE = (
    "Changes made before the error may remain - check the rig, "
    "and use Undo if something was left half-applied."
)


def command_failure_message(exc, started):
    """One-line ERROR text for a command that raised.

    `started` is True once exec() has been entered. A command refused by
    _assert_command_allowed() never ran, so nothing can have been
    half-applied and the note would be wrong.
    """
    message = f"Error running command: {exc}"
    if started:
        message += f". {COMMAND_NOT_TRANSACTIONAL_NOTE}"
    return message


def _assert_command_allowed(command):
    reason = command_blocked_reason(command)
    if reason:
        raise ValueError(reason)


# Every one of a Toggle's ast.parse()/compile() steps below asks the SAME
# question - "what does this command string structurally look like" - over
# and over for the exact same text: _eval_toggle_raw_value() runs on every
# frame-change tick and every selection change (see sync_quick_toggle_states
# below), not just on click, and re-parsing/re-compiling unchanged command
# text on every one of those ticks is pure waste once it's already been done
# once. A command's STRUCTURE (is it assignable, is its target a pure
# reference chain, what's the literal key/attribute) depends only on the
# command's text, never on which rig it's currently being evaluated against
# - so these cache the *parse/compile* step, not the result of actually
# running the command (that part - the live property value, or whichever
# rig it resolves against right now - is still computed fresh every call,
# same as before).
#
# Keyed by the command string itself rather than needing explicit
# invalidation: editing a command in the UI produces a different string,
# which simply starts a fresh cache entry - the old string's entry is just
# never looked up again. Left unbounded (a plain dict) since the number of
# DISTINCT command strings in use is bounded by how many Toggle buttons
# exist across all rigs in the file - realistically dozens, not the kind of
# unbounded growth that would ever need an eviction policy.
_toggle_assignment_cache = {}   # command -> compiled code object (write side)
_toggle_read_cache = {}         # command -> (is_pure: bool, code: code|None) (read side)
_toggle_keyframe_shape_cache = {}  # command -> (owner_code, data_path, index, reason) (Auto-Key target)


def _toggle_assignment_code(command):
    """Compiles `command` into an actual `<command> = value` ASSIGNMENT
    STATEMENT - structurally, via the `ast` module - rather than by
    string-concatenating "= value" onto the end of the command text
    (the previous approach here).

    That string version had a real, silent bug: Python comments run to
    the end of the physical line, so a command with a trailing
    "# some note" swallowed the appended "= value" into the comment -
    `'rig["Prop"]  # note' + " = value"` became
    `'rig["Prop"]  # note = value'`, which `exec()` runs as a no-op
    expression statement with no error at all. The click would report
    success and the switch's own sync (a plain eval() of the command,
    unaffected by a trailing comment) would keep showing the
    "correct" value, while the underlying property silently never
    changed. Building a real ast.Assign node sidesteps this rather
    than working around it with more text-handling (stripping
    comments as a separate string pass would still be fragile - e.g.
    a "#" that's part of an actual string literal in the command
    rather than a comment).

    This still makes NO assumption about what `command` looks like
    (no bone/property shape, nothing hardcoded) - it works for any
    expression that's a legal Python assignment target (a name, an
    attribute, or a subscript, however deeply chained -
    `rig.pose.bones["Bone"]["Prop"]`, `rig["Prop"]`, `rig.some_attr`,
    ...), which is exactly the same requirement `{command} = value`
    always had (you can only assign to something assignable) - just
    enforced structurally instead of left to slip through as a syntax
    error, or in this one case, silently not error at all.

    Compiled once per distinct command string and cached
    (_toggle_assignment_cache) - every later call with the SAME
    command text gets the same code object back with no re-parsing.
    A command that fails to compile/isn't assignable is NOT cached
    (raises fresh each time) - that path isn't the hot one this cache
    exists for (a click, not a per-frame tick), and re-raising a
    stored exception instance repeatedly would carry a stale traceback
    from whichever call first hit it.

    Returns a compiled code object ready for `exec(code, globals_dict)`
    (with `value` provided in that dict, same as before). Raises
    SyntaxError (invalid Python) or ValueError (valid Python, but not
    something assignable - e.g. a function call or a binary
    expression) - both already handled by the existing try/except
    around every exec() call site, same as a malformed command always
    was.
    """
    cached = _toggle_assignment_cache.get(command)
    if cached is not None:
        return cached

    tree = ast.parse(command, mode='eval')
    target = tree.body
    if not isinstance(target, (ast.Name, ast.Attribute, ast.Subscript)):
        raise ValueError(f"Command isn't something that can be assigned to: {command!r}")
    target.ctx = ast.Store()
    module = ast.Module(
        body=[ast.Assign(targets=[target], value=ast.Name(id='value', ctx=ast.Load()))],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    code = compile(module, '<quick_toggle_command>', 'exec')
    _toggle_assignment_cache[command] = code
    return code


def _is_pure_reference_expression(node):
    """True if `node` is ONLY ever reading something - some chain of
    name/attribute/subscript lookups on constant keys (`rig`,
    `rig.pose`, `rig.pose.bones["Bone"]["IK_FK"]`, ...) - with no
    function/method call, operator, comparison, conditional, lambda,
    comprehension, or anything else that could DO something rather
    than just look something up.

    This is what _eval_toggle_raw_value() below checks a command
    against before it will ever eval() it. That matters because
    reading a Toggle's value isn't a rare, deliberate action the way
    clicking the button is - _eval_toggle_raw_value() gets called on
    every frame change and every selection change (see
    sync_quick_toggle_states() below) to keep the switch's on-screen
    position honest, often many times a second during playback/
    scrubbing. plain eval() doesn't distinguish "an expression that
    only reads" from "an expression that happens to call something" -
    `eval('rig.pose.bones["Bone"]["IK_FK"] if not
    bpy.ops.some.operator() else 0', ...)` runs that operator exactly
    as much as exec() would. A command with a call embedded in it (by
    design or by accident) would otherwise fire that call at sync
    frequency instead of only when the button is actually clicked.
    Restricting eval() to expressions this function accepts closes
    that off entirely, structurally - not by guessing at which
    functions are "probably fine" to call, but by never calling
    anything at all here.

    This only gates the READ side. Actually running a Toggle's command
    (a real click, via _toggle_assignment_code() + exec() in
    RP_OT_quick_run_multi_action) is unaffected and still runs whatever
    the command says, full Python, exactly as before - that's a
    deliberate, once-per-click user action, not a background tick, and
    nothing here restricts it.
    """
    if isinstance(node, ast.Expression):
        return _is_pure_reference_expression(node.body)
    if isinstance(node, ast.Name):
        return isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Attribute):
        return isinstance(node.ctx, ast.Load) and _is_pure_reference_expression(node.value)
    if isinstance(node, ast.Subscript):
        if not isinstance(node.ctx, ast.Load):
            return False
        slice_node = node.slice
        # Python < 3.9 wraps a constant subscript key in ast.Index;
        # unwrap it so the check below still reaches the real Constant
        # underneath (ast.Index doesn't exist at all on 3.12+, hence
        # the hasattr guard).
        if hasattr(ast, 'Index') and isinstance(slice_node, ast.Index):
            slice_node = slice_node.value
        return (
            _is_pure_reference_expression(node.value)
            and _is_pure_reference_expression(slice_node)
        )
    # Anything else - Call, BinOp, BoolOp, UnaryOp, Compare, IfExp,
    # Lambda, comprehensions, f-strings, ... - is rejected outright.
    return False


# ---------------------------------------------------------------------------
# Auto-Key: which Toggle targets can be keyframed, and how
#
# When the timeline's Auto-Key is on, a Toggle click (and its undo/redo)
# keyframes the property the command assigns to - not whatever else happens
# to change. What counts as a keyable target is explicit, not "whatever a
# scan of the rig turns up":
#
#   1. a custom property on any struct      X["Prop"]
#        e.g. rig.pose.bones["B"]["IK_FK"], rig["Mode"],
#             rig.pose.bones["B"].constraints["C"]["Blend"]
#   2. an RNA property                      X.attr
#        e.g. rig.pose.bones["B"].constraints["IK"].influence, .mute
#   3. ONE ELEMENT of an array property     X.attr[i]   or   X["Arr"][i]
#        e.g. rig.pose.bones["B"].location[0], rig.pose.bones["B"]["Arr"][1]
#
# where X is a plain reference chain (names, attributes, constant keys - no
# calls, see _is_pure_reference_expression) that resolves to a struct that
# can be keyframed. Each is keyed with exactly one
# `X.keyframe_insert(data_path, index)`, so a Toggle on a constraint's
# influence or an array element works the same as one on a custom property.
#
# Anything else - a vector COMPONENT (.location.x - use location[0]), an
# element of a nested array, a non-constant key, a container computed by a
# call (rig.pose.bones.get("B")[...], which a click refuses anyway because
# its value can't be read back to verify) - can't be resolved without
# running the command's own code again. For those the only thing that can
# still be done is the older fallback: diff the numeric custom properties on
# the rig and its pose bones and key the ones that changed.
# That fallback CANNOT see RNA properties (a constraint's influence, a bone
# transform...), so when it finds nothing the user is told the target isn't
# supported and why, instead of the click silently not being keyed.
# ---------------------------------------------------------------------------

AUTO_KEY_SUPPORT_NOTE = (
    'Auto-Key keys a Toggle whose target is a custom property (X["Prop"]), '
    "an RNA property (X.influence) or one element of an array property "
    '(X.location[0], X["Arr"][1]), with X a plain reference chain. For any '
    "other target only numeric custom properties of the rig and its pose "
    "bones that the command changed can be keyed."
)


def _toggle_keyframe_shape(command):
    """Works out, from the command TEXT alone (cached per command - no rig,
    no evaluation), which struct/data_path/index a Toggle's assignment
    target would be keyframed through.

    Returns `(owner_code, data_path, index, reason)`:
      * supported -> `owner_code` is the compiled code for the sub-
        expression that resolves to the struct owning the property,
        `data_path` is what `owner.keyframe_insert()` takes (`influence`,
        `["Prop"]`, `location`, `["Arr"]`), `index` is the array element
        (-1 = the whole property, i.e. a scalar), and `reason` is None;
      * unsupported -> `(None, None, None, reason)` with a one-line reason.
    """
    cached = _toggle_keyframe_shape_cache.get(command)
    if cached is not None:
        return cached

    def unsupported(reason):
        return (None, None, None, reason)

    result = None
    try:
        tree = ast.parse((command or "").strip(), mode='eval')
    except SyntaxError:
        result = unsupported("the command isn't valid Python")
    else:
        target = tree.body
        owner_node = data_path = None
        index = -1

        def const_key(node):
            if hasattr(ast, 'Index') and isinstance(node, ast.Index):
                node = node.value
            return node.value if isinstance(node, ast.Constant) else None

        if isinstance(target, ast.Attribute):
            owner_node, data_path = target.value, target.attr
        elif isinstance(target, ast.Subscript):
            key = const_key(target.slice)
            if isinstance(key, str):
                owner_node, data_path = target.value, f'["{key}"]'
            elif isinstance(key, int) and not isinstance(key, bool) and key >= 0:
                inner = target.value
                if isinstance(inner, ast.Attribute):
                    owner_node, data_path, index = inner.value, inner.attr, key
                elif isinstance(inner, ast.Subscript) and isinstance(const_key(inner.slice), str):
                    owner_node, data_path, index = inner.value, f'["{const_key(inner.slice)}"]', key
                else:
                    result = unsupported(
                        "the array element isn't an element of a named property"
                    )
            else:
                result = unsupported(
                    "the subscript key isn't a constant string or a "
                    "non-negative integer"
                )
        else:
            result = unsupported(
                "the target isn't a property, a custom property or an "
                "array element"
            )

        if result is None:
            if not _is_pure_reference_expression(owner_node):
                result = unsupported(
                    "the target's container is computed by a call, so it "
                    "can't be found without running the command's code again"
                )
            else:
                expression = ast.Expression(body=owner_node)
                ast.fix_missing_locations(expression)
                result = (
                    compile(expression, '<quick_toggle_target>', 'eval'),
                    data_path, index, None,
                )

    _toggle_keyframe_shape_cache[command] = result
    return result


def _resolve_toggle_keyframe_target(command, rig, context=None):
    """Resolves a Toggle command's assignment target to what Auto-Key can
    key: `(owner, data_path, index, None)`, or `(None, None, None, reason)`.

    Only the owner sub-expression is evaluated, and only because
    _toggle_keyframe_shape() proved it is a pure reference chain - the
    command's own code is never run a second time. The resolved owner must
    actually be keyframeable (a struct with keyframe_insert): a vector or
    color component (`.location.x`) resolves to a plain math value that
    isn't, and is reported rather than guessed at.
    """
    owner_code, data_path, index, reason = _toggle_keyframe_shape(command)
    if owner_code is None:
        return None, None, None, reason

    ctx = context if context is not None else bpy.context
    eval_globals = {"bpy": bpy, "context": ctx, "C": ctx, "D": bpy.data, "rig": rig}
    try:
        owner = eval(owner_code, eval_globals)
    except Exception:
        return None, None, None, "the target doesn't resolve on this rig"
    if not callable(getattr(owner, "keyframe_insert", None)):
        return None, None, None, (
            f"a {type(owner).__name__} can't be keyframed directly (to key a "
            "vector or color component use its index, e.g. location[0])"
        )
    return owner, data_path, index, None


def _auto_key_toggle(rig, command, frame, before_snapshot, context=None):
    """Keyframes what a Toggle's command just changed, for the timeline's
    Auto-Key. Used by the click (RP_OT_quick_run_multi_action). The
    picker's own Ctrl+Z does not re-run Toggles - those are Blender
    changes, undone with Blender's own undo.

    Keys the command's own assignment target when it is one of the
    supported kinds (see the block comment above) - exactly that one
    property, never some other property that moved as a side effect (a
    driver reacting to the assignment, a command touching several things).
    Only when the target can't be resolved does it fall back to diffing the
    numeric custom properties of the rig and its pose bones against
    `before_snapshot` (_snapshot_numeric_custom_props(), taken before the
    command ran).

    Returns `(keyed, failed, unsupported)`:
      keyed        how many properties were keyed;
      failed       [(scope, name, prop)] that existed but couldn't be keyed
                   ('target' scope = the command's own target);
      unsupported  None, or why NOTHING could be keyed because the target
                   isn't a supported kind (see _auto_key_report()).
    """
    owner, data_path, index, reason = _resolve_toggle_keyframe_target(
        command, rig, context
    )
    if owner is not None:
        try:
            inserted = bool(owner.keyframe_insert(
                data_path=data_path, index=index, frame=frame
            ))
        except Exception:
            inserted = False
        if inserted:
            id_data = getattr(owner, "id_data", None) or owner
            id_data.update_tag(refresh={'DATA'})
            return 1, [], None
        return 0, [('target', command, '')], None

    changed = _changed_numeric_custom_props(
        before_snapshot or {}, _snapshot_numeric_custom_props(rig)
    )
    keyed, failed = _keyframe_custom_prop_changes(rig, changed, frame)
    unsupported = reason if (not keyed and not failed) else None
    return keyed, failed, unsupported


def _auto_key_report(subject, command, keyed, failed, unsupported):
    """The (level, message) to show for an _auto_key_toggle() outcome, or
    None when everything was keyed. `subject` is "Toggle ran" / "Toggle
    restored". Names the exact target that failed, or why the target isn't
    supported - never a silent no-op."""
    if failed:
        described = ", ".join(
            command if scope == 'target'
            else f'bone "{name}"["{prop_name}"]' if scope == 'bone'
            else f'rig["{prop_name}"]'
            for scope, name, prop_name in failed
        )
        return 'WARNING', f"{subject}, but auto-key failed for: {described}"
    if not keyed:
        why = unsupported or "nothing the command changed could be keyed"
        return 'WARNING', (
            f"{subject}, but couldn't auto-key: {why}. {AUTO_KEY_SUPPORT_NOTE}"
        )
    return None


class _ToggleUnreadable:
    """Type of _TOGGLE_UNREADABLE (a unique object, so `is` is the test)."""
    __slots__ = ()

    def __repr__(self):
        return "<toggle target unreadable>"


# "The command could not be read at all" (empty command, not a pure
# reference, evaluation raised, property missing), as opposed to "it was
# read and the property holds None". None is a legitimate VALUE - an unset
# pointer property (rig.parent, a bone's custom_shape), an entry in a plain
# Python dict - so it can't also mean "unreadable". Callers that must tell
# the two apart pass `unreadable=_TOGGLE_UNREADABLE` to
# _eval_toggle_original() / _read_quick_toggle_original() and test with
# `is`; everyone else keeps the default (None for both).
_TOGGLE_UNREADABLE = _ToggleUnreadable()


def _eval_toggle_original(rig, command, context=None, unreadable=None):
    """Evaluates a Quick Toggle's command AS WRITTEN (a bare expression,
    no parsing) and returns the value EXACTLY as the property holds it -
    its original Python object and type (bool, int, float, but also
    whatever else it might be: str, a tuple...) - or None if the command
    is empty, isn't a pure read (see _is_pure_reference_expression above)
    or fails to evaluate.

    That "or None" is ambiguous when the property itself holds None. Pass
    `unreadable=_TOGGLE_UNREADABLE` to get that sentinel back instead of
    None for the can't-read cases, so a read None stays a value. The
    default (None) is kept for callers that don't care (display sync,
    numeric view).

    Not converted to float here on purpose: a Toggle's undo/redo has to
    put back what was there, TYPE included. Assigning a float to an int or
    boolean custom property (or an int to a float one) makes Blender
    replace the property with one of the new type, so an undo that
    restored `1.0` over an originally boolean `True` quietly changed the
    property's data type. Use toggle_value_supported() before relying on
    the result, and _eval_toggle_raw_value() when only a number is needed.

    This is the one shared place that runs a Toggle's command to find
    out its live value (_eval_toggle_raw_value() below is the numeric
    view of it) - reused for keeping the switch in sync
    (_read_quick_toggle_state below) and for reading the pre-click value
    in RP_OT_quick_run_*_action, so both agree on exactly the same
    command, evaluated the same way, instead of one of them reading a
    regex-parsed bone/property and one of them reading the command
    itself.

    `rig`/`context` are exposed to the evaluated expression as
    `rig`/`context` (`context` falls back to bpy.context when not
    passed in, e.g. from the frame-change handler), same globals the
    run operators already expose, so a command written generically
    (e.g. `rig.pose.bones["Bone"]["IK_FK"]`) resolves against whichever
    armature/context it's evaluated with.

    The parse + purity check (_is_pure_reference_expression) + compile
    only happen once per distinct command string, cached in
    _toggle_read_cache, same reasoning as _toggle_assignment_code's own
    cache on the write side - this is the READ side's hot path: it
    runs on every frame change and every selection change (see
    sync_quick_toggle_states() below), not just once per click, so
    re-parsing/re-compiling the same command text on every single tick
    is pure waste for however many Toggle buttons a rig has. A
    command that isn't a pure read is cached too (as `(False, None)`)
    so a Toggle with a non-eligible command doesn't pay the parse cost
    again on the next tick either - unlike _toggle_assignment_code's
    cache, there's no stale-exception concern here since this path
    never raises, it only ever returns None.
    """
    command = (command or "").strip()
    if not command:
        return unreadable

    cached = _toggle_read_cache.get(command)
    if cached is None:
        try:
            tree = ast.parse(command, mode='eval')
        except SyntaxError:
            cached = (False, None)
        else:
            if _is_pure_reference_expression(tree):
                cached = (True, compile(tree, '<quick_toggle_read>', 'eval'))
            else:
                cached = (False, None)
        _toggle_read_cache[command] = cached

    is_pure, code = cached
    if not is_pure:
        return unreadable

    ctx = context if context is not None else bpy.context
    eval_globals = {
        "bpy": bpy,
        "context": ctx,
        "C": ctx,
        "D": bpy.data,
        "rig": rig,
    }
    try:
        raw_value = eval(code, eval_globals)
    except Exception:
        return unreadable

    return raw_value


# The only property types a Toggle can switch AND faithfully restore: a
# boolean, an integer or a float (what a 0/1 switch is stored as). Anything
# else - a string, an array, a tuple, a mathutils value - has no meaningful
# 0/1 state, and writing 0/1 over it would change its type, so it is
# neither toggled nor restored.
def toggle_value_supported(value):
    """True when `value` is a bool, int or float (see above)."""
    return isinstance(value, (bool, int, float))


def _toggle_held_description(value):
    """Wording for a value a Toggle can't switch: "None" or "a <type>"."""
    if value is None:
        return "None"
    return f"a {type(value).__name__}"


def _eval_toggle_raw_value(rig, command, context=None):
    """The numeric (float) view of a Quick Toggle's current value, for
    deciding the switch's state and verifying a write: the float of
    _eval_toggle_original()'s result, or None if it can't be evaluated or
    isn't a supported type (bool/int/float - so e.g. the string "1" no
    longer reads as ON). Never used to WRITE a value back: that must keep
    the original type, which a float does not."""
    original = _eval_toggle_original(rig, command, context)
    if not toggle_value_supported(original):
        return None
    return float(original)


def _toggle_value_for_state(state, original):
    """The value to WRITE for a Toggle going to `state` (True = ON), of the
    same type the property already holds: True/False for a boolean, 1/0 for
    an integer, 1.0/0.0 for a float. With no original value to match (the
    property doesn't exist yet - its first run creates it) a plain 1/0."""
    if isinstance(original, bool):
        return bool(state)
    if isinstance(original, float):
        return 1.0 if state else 0.0
    return 1 if state else 0


def _toggle_type_change_note(expected_value, rig, command, context=None):
    """None when the property still holds the type `expected_value` has;
    otherwise a one-line description of the change. Run after a Toggle
    write/restore as a safety net: the write is typed, so a mismatch means
    something coerced it (an RNA-defined property of another type, a
    setter, a driver)."""
    after = _eval_toggle_original(
        rig, command, context, unreadable=_TOGGLE_UNREADABLE
    )
    if after is _TOGGLE_UNREADABLE or type(after) is type(expected_value):
        return None
    return (
        f"the property is now {type(after).__name__} "
        f"(it was {type(expected_value).__name__})"
    )


def _toggle_restore_blocker(rig, command, value):
    """Why a Toggle undo/redo must NOT write `value` back, or None if it is
    safe. Two checks, both about not damaging the property's type:
      * the saved value itself must be a supported type (bool/int/float);
      * the property must not now hold something else (a string, an
        array...) that a 0/1 write would overwrite and retype."""
    if not toggle_value_supported(value):
        return (
            f"the saved value {value!r} is a {type(value).__name__}, "
            "not a number or boolean"
        )
    # A property that now holds None is a value a 0/1 write would overwrite
    # and retype, not a missing one (which undo is allowed to re-create).
    current = _eval_toggle_original(
        rig, command, unreadable=_TOGGLE_UNREADABLE
    )
    if current is not _TOGGLE_UNREADABLE and not toggle_value_supported(current):
        return (
            f"the property now holds {_toggle_held_description(current)}, "
            "not a number or boolean"
        )
    return None


# ---------------------------------------------------------------------------
# What a Toggle's property VALUE means - ONE rule, used by everything.
#
# A Toggle drives a property that is meant to hold 0 (OFF) or 1 (ON). The
# switch's on-screen state (sync_quick_toggle_states(), the post-click
# re-sync, undo/redo) and the click handler (what to write next) must read
# that value the same way, or a click can contradict the switch it was
# made on: with the property at 0.5 the display said OFF while the click
# handler's `not bool(0.5)` also said "currently ON, so write 0" - the
# switch the user had just flipped to ON snapped straight back to OFF.
#
#   value within tolerance of 0  -> OFF
#   value within tolerance of 1  -> ON
#   anything else (0.5, 2, -1, nan, inf) is NOT a valid switch state. It is
#       DISPLAYED as OFF, so a click moves it to ON (the way the switch
#       looks like it should go), and the click tells the user the value was
#       not 0/1. Nothing is lost: the exact previous value is kept in the
#       undo snapshot, so undo puts 0.5 back.
#   unreadable (no value at all: missing property, evaluation error) has no
#       value to interpret, so the click falls back to inverting the
#       switch's last known state.
#
# Tolerance, not exact equality: 0.9999999 from float noise or an
# interpolated/animated value is ON, not "invalid". Same tolerance the
# write-verification below uses (_TOGGLE_VERIFY_TOLERANCE), so a value that
# verifies as "took effect" is always one the display reads as binary.
# ---------------------------------------------------------------------------
_TOGGLE_STATE_TOLERANCE = 1e-6


def _toggle_state_from_value(raw_value):
    """True (ON), False (OFF), or None when `raw_value` is not a valid
    switch state - not 0 or 1 within tolerance, not a number, or missing."""
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    if abs(value) <= _TOGGLE_STATE_TOLERANCE:
        return False
    if abs(value - 1.0) <= _TOGGLE_STATE_TOLERANCE:
        return True
    return None


def _toggle_click_target_state(raw_before, cached_state):
    """The state a click on a Toggle should move it to, given the value the
    property holds right now (`raw_before`, None if unreadable) and the
    switch's last known state. Reads the value with the SAME rule as the
    display (see above): a non-binary value counts as OFF, so the click
    turns it ON."""
    if raw_before is None:
        return not cached_state
    return not bool(_toggle_state_from_value(raw_before))


def _format_toggle_value(raw_value):
    try:
        return f"{float(raw_value):g}"
    except (TypeError, ValueError):
        return repr(raw_value)


def _read_quick_toggle_state(rig, command):
    """Reads a Quick Toggle's CURRENT state by evaluating its command
    itself (via _eval_toggle_raw_value above) - not by regex-parsing
    the command text for one hardcoded `.pose.bones["Bone"]["Prop"]`
    shape. A Toggle's command can be any Python expression that
    resolves to a number (a pose bone custom property, an object
    property, a nested attribute, whatever the rigger writes) - it
    only ever gets treated as one specific literal shape at RUN time
    (RP_OT_quick_run_*_action appends `= value` to it), never here.
    Evaluating it directly means sync works for whatever the command
    actually is, instead of only the one pattern a parser happens to
    recognize.

    Returns True/False for a value that is a valid switch state (see
    _toggle_state_from_value()), or None for anything else (empty
    command, evaluation error, property missing, a non-binary value like
    0.5, a result that isn't numeric). Callers display None as OFF.
    """
    return _toggle_state_from_value(_eval_toggle_raw_value(rig, command))


# How far a Toggle's read-back value may differ from the value just written
# and still count as "took effect" (plain float noise only - a property
# that is clamped, driven or locked ends up far outside this). Deliberately
# the same number the display uses to decide a value is 0 or 1.
_TOGGLE_VERIFY_TOLERANCE = _TOGGLE_STATE_TOLERANCE


def _toggle_values_match(actual, expected):
    """Whether a Toggle's read-back `actual` counts as the `expected` value
    that was just written (or restored by undo/redo).

    Finite numbers: within _TOGGLE_VERIFY_TOLERANCE (float noise only).
    Non-finite numbers are compared explicitly, because the plain
    `abs(actual - expected) <= tolerance` is False for them even when the
    value is exactly what was written: nan - nan is nan, and inf - inf is
    nan. A float nan/inf is a supported Toggle value (see
    toggle_value_supported()) that undo saves and writes back as-is, so
    without this a correct restore was reported as not having taken effect.

      * nan matches only nan (any payload/sign - they are all "not a
        number"); it never matches a finite number or an infinity.
      * an infinity matches only the infinity of the same sign.
    """
    actual = float(actual)
    expected = float(expected)
    if math.isnan(actual) or math.isnan(expected):
        return math.isnan(actual) and math.isnan(expected)
    if math.isinf(actual) or math.isinf(expected):
        return actual == expected
    return abs(actual - expected) <= _TOGGLE_VERIFY_TOLERANCE


def _toggle_assignment_took_effect(rig, command, expected_value, context=None):
    """Reads a Quick Toggle's command back right after its assignment ran
    and checks the property now actually holds `expected_value`.

    An assignment that doesn't raise only proves Python accepted it - a
    driver, a constraint, a min/max limit, an override-locked property or
    a setter that ignores the write can all leave a different value behind.
    Committing the switch's look (action.toggle_state) on that alone
    would show a state the rig is not in.

    Returns (took_effect, actual_value). `actual_value` is None when the
    command couldn't be read back at all (not a pure reference
    expression, evaluation error, non-numeric result), in which case
    nothing can be confirmed and took_effect is False.
    """
    actual_value = _eval_toggle_raw_value(rig, command, context)
    if actual_value is None:
        return False, None
    return _toggle_values_match(actual_value, expected_value), actual_value


def _sync_toggle_state_from_property(action, rig, command):
    """Sets action.toggle_state to what the Toggle's property ACTUALLY
    holds right now: 1 is ON, 0 is OFF (both within tolerance), and
    anything else (unreadable, non-binary) is OFF - the same rule
    (_toggle_state_from_value()) sync_quick_toggle_states() applies on
    every frame/selection tick and the click handler uses to decide what
    to write, so a corrected switch is not flipped again by the next tick.
    Returns the new state."""
    state = bool(_read_quick_toggle_state(rig, command))
    if action.toggle_state != state:
        action.toggle_state = state
    return state


def _toggle_verify_failure_message(command, expected_value, actual_value, shown_state):
    """One-line report for a Toggle whose assignment didn't take effect.
    `shown_state` is what the switch was re-synchronized to."""
    command = " ".join(command.split())
    expected = f"{expected_value:g}"
    shows = "On" if shown_state else "Off"
    if actual_value is None:
        return (
            f'Toggle ran, but "{command}" could not be read back to confirm it '
            f"is now {expected}. The switch now shows {shows}."
        )
    return (
        f'Toggle failed: "{command}" reads {actual_value:g} after being set to '
        f"{expected}. A driver, constraint, limit or locked property may be "
        f"overriding it. The switch now shows the actual value ({shows})."
    )


def sync_quick_toggle_states(scene=None, selected_names=None):
    """Quick-panel counterpart to custom_actions.sync_custom_toggle_states()
    - UI-state only (see this module's top-of-file scope note): keeps
    action.toggle_state, and via _sync_quick_picker_ui() the picker
    canvas's ToggleSwitch, in step with whatever each Toggle's command
    property currently holds. Does not auto-keyframe anything - that's
    a separate concern this module doesn't implement yet.

    Multi-command Toggles are rig-scoped (only checked against the
    currently active rig, matching which ones are even visible - see
    RP_QuickMultiAction.armature_name) and, since a Toggle can have
    several rules (e.g. a mirrored Left/Right pair), the one actually
    read is whichever the current selection matches via
    _quick_find_matching_rule() - the exact same resolution a click
    itself uses. With no match, the toggle is left exactly where it
    was, same as a plain click-only toggle never having run.

    Single-command buttons (Section 2) have no Toggle type at all -
    they're always plain buttons - so there's nothing of theirs for
    this function to sync.

    Called from the frame_change_post handler above (so a Toggle stays
    in sync while scrubbing/playing) and from
    Controller.refresh_custom_action_enabled_states() in
    ui/controller.py (so it also stays in sync the moment the
    selection changes) - same two call sites as the real
    sync_custom_toggle_states(), and same reasoning for accepting
    scene/selected_names as optional overrides instead of always
    re-querying bpy.context.

    Only reads scene/action/bone data - no bpy.ops calls - so it's
    safe to run from the frame-change handler.

    Returns True if any switch changed, else False.

    LIMIT: this only runs on a frame change and on a selection change (and
    after the picker's own click/undo/redo). A script that writes a Toggle's
    target property without changing either is not noticed until the next
    of those; Blender sends no notification for that, and nothing here polls
    it. refresh_quick_toggle_states() / the "Refresh Toggles" operator
    (rp.quick_refresh_toggles) is the explicit way to catch up. Like every
    other sync, it only updates a Toggle whose rule matches the current
    selection - one whose rule doesn't is left alone by design.
    """
    scene = scene if scene is not None else bpy.context.scene
    settings = getattr(scene, "rp_quick_action_settings", None)
    if settings is None:
        return False

    from . import backend
    rig = backend.arm()
    if rig is None:
        return False

    if selected_names is None:
        selected_names = {b.name for b in (getattr(bpy.context, "selected_pose_bones", None) or [])}
    else:
        selected_names = set(selected_names)

    changed = False

    for action in settings.multi_actions:
        if action.action_type != 'TOGGLE':
            continue
        if quick_action_rig_name(settings, action) != rig.name:
            continue
        matched_rule, _warning = _quick_find_matching_rule(selected_names, action.rules)
        if matched_rule is None:
            continue
        new_state = _read_quick_toggle_state(rig, matched_rule.command)
        if new_state is not None:
            if new_state != action.toggle_state:
                action.toggle_state = new_state
                changed = True
        elif action.toggle_state:
            # The command exists and matched, but its target can't be
            # read at all right now (deleted bone, removed property, a
            # broken command, a non-numeric result) - _read_quick_toggle_state()
            # returning None means exactly that, not "leave it alone"
            # (that's the `matched_rule is None` branch above, a
            # completely different case: nothing wrong with the
            # target, the current selection just isn't the one this
            # button cares about). Whatever ON/OFF the switch was
            # showing here is left over from the last time the read
            # DID succeed - it's not describing anything real anymore,
            # so force it back to the neutral OFF default rather than
            # let a stale ON keep sitting there looking like a live
            # reading it no longer is. (No tri-state "unknown" look
            # exists for the switch - this is deliberately the more
            # conservative of the two states it can still show, never
            # a false ON.)
            action.toggle_state = False
            changed = True

    if not changed:
        return False

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()

    _sync_quick_picker_ui()
    return True

def refresh_quick_toggle_states(scene=None):
    """Re-reads every Quick Toggle's target and updates its switch NOW, for
    use after a script (or any tool) changed a target property without
    changing the frame or the selection - the two events that otherwise
    trigger a sync. Safe to call from scripts:

        from <addon package> import custom_actions_quick
        custom_actions_quick.refresh_quick_toggle_states()

    or bpy.ops.rp.quick_refresh_toggles(). Uses the picker's own tracked
    selection when the picker is open (the same source a click and the
    selection-change sync use), else the viewport's. Returns True if any
    switch changed.
    """
    from . import backend

    controller = getattr(backend, "_ACTIVE_CONTROLLER", None)
    selected = getattr(controller, "selected_bones", None) if controller is not None else None
    return sync_quick_toggle_states(scene, selected)


# -----------------------------------------------------------------------------
# SCRATCH / PROTOTYPE MODULE - fully additive, nothing in custom_actions.py
# is touched (only imported from, to reuse its RP_ControlBoneItem type, its
# RP_UL_control_bones list widget, and its edit-icon helper, so this section
# looks pixel-identical to the real one). Delete this file plus the two
# register()/unregister() hook lines in blender_addon/__init__.py to remove
# this feature entirely.
#
# Add/Remove/Rename/expand all work, and both sections' Run buttons are
# wired up: Section 1 (multi-command) matches the selection against each
# command's Control shape bones (see _quick_find_matching_rule()) before
# running the matched one; Section 2 (single-command) just runs its one
# command whenever something is selected.
#
# Adds one new panel below the existing "Custom Actions" panel:
#
# Section 1 - "Multi-Command Buttons": Button/Toggle type dropdown + Add.
# Every button here looks/behaves exactly like the existing "Multiple"
# command-mode buttons - any number of commands, each with its own
# Control shape bones list.
#
# Section 2 - "Single-Command Buttons": + Add Button.
# Every button here looks like the existing "Single" command-mode buttons -
# exactly one command box - but with the Control shape bones list removed
# entirely, since these aren't matched against a bone selection. Always a
# plain button - no Toggle type here (that's Section 1's Multi-Command
# Buttons only).
# -----------------------------------------------------------------------------

QUICK_ACTION_TYPES = [
    ('BUTTON', "Button", "A normal button that runs its command(s) once per click"),
    ('TOGGLE', "Toggle", "A toggle button that flips a 1/0 'value' variable each click"),
]


# -----------------------------------------------------------------------------
# Section 1 - Multi-command buttons (rig-scoped, with Control shape bones)
# -----------------------------------------------------------------------------

def _on_quick_rule_command_changed(self, context):
    """Typing/clearing a command changes whether a button can run: a rule
    with an empty command never enables its button (see
    _quick_find_matching_rule()), and for a Single button a second
    non-blank command makes it malformed. Without this the picker canvas
    only re-evaluated enabled/greyed state on the next selection change -
    so a button whose Control shape bone and selection were already right
    stayed greyed out after its command was typed, until the picker
    selection was cleared and re-made.
    """
    _sync_quick_picker_ui()


class RP_QuickMultiRule(PropertyGroup):
    command: StringProperty(
        name="Command", default="", update=_on_quick_rule_command_changed
    )
    bones: CollectionProperty(type=RP_ControlBoneItem)
    bones_index: IntProperty()
    expanded: BoolProperty(default=True)


class RP_QuickMultiAction(PropertyGroup):
    uid: StringProperty(
        name="UID",
        description="Stable per-action identifier - lets the picker window's "
                    "canvas button (see PickerCanvas.sync_quick_actions() in "
                    "ui/control_list.py) key off something that doesn't shift "
                    "when an earlier action is removed/reordered.",
        default="",
    )
    name: StringProperty(name="Label", default="New Button")
    action_type: EnumProperty(name="Type", items=QUICK_ACTION_TYPES, default='BUTTON')
    rules: CollectionProperty(type=RP_QuickMultiRule)
    rules_index: IntProperty()
    expanded: BoolProperty(default=True)
    toggle_state: BoolProperty(default=False)
    armature_name: StringProperty(
        name="Armature",
        description="Name of the rig that was active when this button was added",
        default="",
    )
    # NOTE: since panel_uid (below) was added this - and armature_name
    # above - is a DISPLAY MIRROR of the owning panel's current names,
    # kept in step by _stamp_multi_action_panel_uids() and the rename
    # callbacks. Ownership is decided by panel_uid; these two strings are
    # only consulted for a legacy button that has no panel_uid yet.
    #
    # Scopes this button to a single PANEL of the rig above, the same
    # way RP_QuickPanel.armature_name scopes a panel to a single rig -
    # a button added while "Face" is the active panel only shows (and
    # only runs from) "Face"; switching to "Body" shows that panel's
    # own separate set instead. Set once, at creation time (from
    # whichever panel is active then, via _quick_active_panel_name()),
    # and kept following along on an in-place panel rename the same way
    # armature_name above follows a rig rename - see
    # _on_quick_panel_name_changed()'s migration of both json_manager's
    # data AND this field.
    #
    # A blank value here means this button predates panel-scoping (it
    # was added back when Section 1 was scoped to a rig only) - see
    # _migrate_legacy_multi_action_panels() for the one-time migration
    # that assigns it to that rig's first panel so it doesn't just stop
    # showing up anywhere the moment this version of the addon loads.
    panel_name: StringProperty(
        name="Panel",
        description="Name of the panel that was active when this button was added",
        default="",
    )
    # THE ownership link: the stable id (the panel's hidden "_uid", see
    # _panel_uid()) of the panel that owns this button. Ownership is
    # decided by this and NOT by panel_name/armature_name, which are
    # only display mirrors kept in step for readability and for files
    # opened by an older version. A name can be reused (a panel deleted
    # and a new one created under the same name), left stale by a
    # rename path that skipped a migration, or edited behind the addon's
    # back; the id cannot be - a recreated panel gets a NEW id, so the
    # old panel's buttons never silently move onto it.
    #
    # Blank = a button saved before this existed: it is adopted by name
    # ONCE (see _stamp_multi_action_panel_uids()) and from then on
    # follows the id. A NON-blank id whose panel is gone is an orphan:
    # it is shown nowhere and cannot run, and is never re-matched by name.
    panel_uid: StringProperty(
        name="Panel ID",
        description="Stable id of the panel that owns this button",
        default="",
    )


# -----------------------------------------------------------------------------
# Section 2 - Single-command buttons (rig-independent, commands only)
# -----------------------------------------------------------------------------

class RP_QuickSingleRule(PropertyGroup):
    command: StringProperty(
        name="Command", default="", update=_on_quick_rule_command_changed
    )
    expanded: BoolProperty(default=True)


class RP_QuickSingleAction(PropertyGroup):
    uid: StringProperty(
        name="UID",
        description="Stable per-action identifier - same purpose as "
                    "RP_QuickMultiAction.uid above.",
        default="",
    )
    name: StringProperty(name="Label", default="New Button")
    # INVARIANT: exactly one rule. A collection can't enforce that itself and
    # the UI never offers "add command" for these, but migrated / restored /
    # scripted / hand-edited data can break it, so it is enforced in three
    # places rather than assumed: normalize_single_action() repairs what can
    # be repaired without losing anything (on load and every poll tick),
    # single_action_rule() refuses to guess when two or more COMMANDS exist,
    # and the Quick panel / picker canvas flag such a button and let the
    # user keep one (RP_OT_quick_keep_single_rule).
    rules: CollectionProperty(type=RP_QuickSingleRule)
    expanded: BoolProperty(default=True)


# -----------------------------------------------------------------------------
# Scene settings
# -----------------------------------------------------------------------------

def _poll_armature_object(self, obj):
    # Hidden rigs are left out of the Rig dropdown entirely.
    from . import backend
    return backend.is_rig_visible(obj)


# Guards against _on_quick_active_rig_changed() treating a programmatic
# sync (backend.poll_active_armature() setting active_rig to reflect a
# switch made elsewhere - the Qt picker's combo, a viewport/Outliner
# click) as a user-initiated pick from this dropdown itself. Without
# this, syncing the dropdown TO the current rig would immediately
# trigger the update callback below and try to switch the rig again (a
# no-op, but redundant, and re-enters the same code path
# poll_active_armature() is already in the middle of).
_SYNCING_QUICK_RIG_DROPDOWN = False


# Same purpose as _SYNCING_QUICK_RIG_DROPDOWN above, but for the Manage
# Panels section's panel list (RP_QuickActionSettings.panels /
# panels_index) and its Qt-picker counterpart (main_window.py's
# stale_combo) - see Controller.change_panel() and
# _on_quick_panels_index_changed() below.
_SYNCING_QUICK_PANEL_DROPDOWN = False


def _warn_panel_name_rejected(context, message):
    """Popup explaining why an edited panel name was reverted (see
    _warn_duplicate_panel_name() for why this is a native popup)."""

    def draw(menu, _context):
        menu.layout.label(text=message)

    try:
        context.window_manager.popup_menu(draw, title="Rig Picker", icon='ERROR')
    except Exception:
        pass


def _warn_duplicate_panel_name(context, name):
    """Shows a small popup telling the user why their typed panel name
    was rejected. Used from a PropertyGroup update callback (no
    Operator around to call self.report() on), so this uses Blender's
    own window_manager.popup_menu() rather than a Qt QMessageBox - the
    Manage Panels list lives in the native N-panel and can be edited
    whether or not the Qt picker window is even open."""

    def draw(menu, _context):
        menu.layout.label(
            text=f'A panel named "{name}" already exists on this rig.'
        )

    try:
        context.window_manager.popup_menu(
            draw, title="Rig Picker", icon='ERROR'
        )
    except Exception:
        # Best-effort - context can be restricted in some callback
        # timings. Worse case is just no popup; the name is still
        # reverted below either way.
        pass


def _on_quick_panel_name_changed(self, context):
    """Fired whenever RP_QuickPanel.name is set - both the very first
    time (RP_OT_quick_add_panel/_ensure_default_panel_for_rig assigning
    its initial name) and every later in-place rename from the Manage
    Panels list (double-click a row).

    Migrates this panel's stored picker data (items, background
    capture, symmetry - see json_manager's per-panel storage) from its
    old name to the new one, and keeps the live Controller in sync if
    this happens to be the panel currently loaded there - the panel-
    level equivalent of Controller._handle_armature_renamed().

    self["_prev_name"] is a hidden shadow of the last name this panel
    was actually stored under - a PropertyGroup's update callback only
    ever sees the NEW value via self.name, so without tracking the old
    one separately there'd be nothing to migrate FROM. Comparing
    self.name to it also distinguishes a genuine rename (fires this
    with a real old name already tracked) from the very first
    assignment (old name not tracked yet, defaults to the new one via
    self.get(), so the two compare equal and this is correctly treated
    as a no-op rather than an attempted rename of nothing).

    Whether to also update the live Controller is deliberately decided
    by IDENTITY (is this RNA item literally the one at panels_index?),
    not by comparing controller.panel_name == old_name. Two panels on
    the same rig can legitimately share a name for a moment mid-edit,
    and old_name is, by definition, a name that's just about to stop
    being unique - comparing strings here is exactly the fragile thing
    being renamed. If that string comparison ever misses (a rename
    landing on a name variant, a poll tick sneaking in mid-edit, etc.),
    the Controller silently keeps believing it's still showing
    old_name; the next poll-tick safety net then treats the rename as
    "switched to a different panel" and calls save() BEFORE switching -
    which writes the still-live data straight back into
    rig_picker_data.json under old_name, resurrecting the exact entry
    rename_panel_data() just removed. Matching by position sidesteps
    that failure mode entirely instead of trying to make the string
    comparison more careful.
    """
    old_name = self.get("_prev_name", self.name)
    new_name = self.name

    if old_name != new_name:
        settings = context.scene.rp_quick_action_settings

        # An empty (or whitespace-only) name can't identify a panel: every
        # lookup in this module treats a blank panel name as "none /
        # unmigrated legacy button", rename_panel_data() refuses to move a
        # layout onto a blank name (leaving the layout stranded under the
        # old one), and the buttons would be re-pointed at "" too. Revert
        # before anything is touched.
        if not new_name.strip():
            self["_prev_name"] = old_name
            self.name = old_name
            _warn_panel_name_rejected(context, "A panel name can't be empty.")
            return

        # Reject a typed/pasted name that collides with another panel
        # already on the SAME rig (a different rig's panel sharing a
        # name is fine - json_manager scopes storage by (rig, panel),
        # so only same-rig collisions are actually ambiguous). Without
        # this, two panels ending up with the same name would make
        # json_manager's per-(rig, panel) storage, this list's own
        # filter_items() rig-scoping, and every panel_name lookup above
        # unable to tell which panel is meant - e.g.
        # rename_panel_data() below would silently clobber whichever
        # panel's data happened to already be stored under the
        # colliding name (see its own "don't clobber" comment).
        conflict = any(
            p != self
            and p.armature_name == self.armature_name
            and p.name == new_name
            for p in settings.panels
        )
        if conflict:
            # Revert in place. This re-enters this same callback (name
            # is being set again), but old_name == old_name there so
            # the `old_name != new_name` check is False and it's a
            # harmless no-op re-entry, not a loop.
            self["_prev_name"] = old_name
            self.name = old_name
            _warn_duplicate_panel_name(context, new_name)
            return

        from . import json_manager
        json_manager.rename_panel_data(self.armature_name, old_name, new_name)
        # A layout still waiting out its removal grace period under
        # new_name was just overwritten by the renamed panel's own data.
        _PENDING_PANEL_DELETIONS.pop((self.armature_name, new_name), None)
        _SAVED_PANEL_DELETIONS.discard((self.armature_name, new_name))
        _settle_panel_history((self.armature_name, new_name))
        if _LAST_PANEL_BY_RIG.get(self.armature_name) == old_name:
            _LAST_PANEL_BY_RIG[self.armature_name] = new_name

        # Section 1's multi-command buttons carry their own snapshot of
        # this panel's name (RP_QuickMultiAction.panel_name) rather than
        # a live reference - follow the rename here too, the same way
        # armature_name is migrated on a rig rename
        # (Controller._handle_armature_renamed()), so a button added on
        # "Face" doesn't silently stop matching (and vanish from both
        # the N-panel list and the Qt canvas) the moment "Face" becomes
        # "Head".
        # Ownership follows the panel's id, so the rename itself needs no
        # migration; the name on each button is only a display mirror,
        # refreshed here. A legacy button (no panel_uid yet) is adopted
        # by THIS panel now, via the OLD name, while that still
        # identifies it - after the rename it no longer would.
        renamed_uid = _panel_uid(self)
        for action in settings.multi_actions:
            if action.panel_uid:
                if action.panel_uid == renamed_uid:
                    action.panel_name = new_name
            elif action.armature_name == self.armature_name and action.panel_name == old_name:
                action.panel_uid = renamed_uid
                action.panel_name = new_name

        from . import backend
        controller = backend._ACTIVE_CONTROLLER
        is_active_panel = (
            0 <= settings.panels_index < len(settings.panels)
            and settings.panels[settings.panels_index] == self
        )
        if (
            controller is not None
            and controller.armature_name == self.armature_name
            and is_active_panel
        ):
            controller.handle_panel_renamed(old_name, new_name)

    self["_prev_name"] = new_name
    _PANEL_NAME_REGISTRY[_panel_uid(self)] = (self.armature_name, new_name)


class RP_QuickPanel(PropertyGroup):
    name: StringProperty(name="Name", default="Panel", update=_on_quick_panel_name_changed)
    # Scopes this panel to a single rig, the same way RP_QuickMultiAction.
    # armature_name scopes a multi-command button - a plain name snapshot
    # rather than a live object reference, since a PropertyGroup can't
    # hold a PointerProperty back to a specific Object cleanly across
    # file save/load the way this addon uses it elsewhere. Set once, at
    # creation time (RP_OT_quick_add_panel), from whichever rig is
    # active at that moment; never changed afterward.
    armature_name: StringProperty(default="")


class RP_UL_quick_panels(UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        # Editable in place (unlike RP_UL_control_bones above) - this is
        # exactly what "edit" in "add edit and delete the panel list"
        # means: double-click a row to rename it.
        layout.prop(item, "name", text="", emboss=False)

    def filter_items(self, context, data, propname):
        """Makes the panel list rig-independent: each rig gets its own set
        of panels (Face, Body, etc.), so only show the rows belonging to
        whichever rig is currently active here - the same scoping
        RP_PT_quick_multi_actions_panel already applies to Section 1's
        buttons via armature_name, just expressed as a UIList filter
        instead of a manual draw-time loop, since this list is drawn
        via template_list(). panels_index itself is left untouched -
        it's always a real index into the *full* underlying collection,
        which is exactly what template_list expects regardless of which
        rows filter_items() hides.
        """
        panels = getattr(data, propname)
        rig = _quick_active_rig(data)
        current_rig_name = rig.name if rig is not None else ""

        flags = [
            self.bitflag_filter_item if item.armature_name == current_rig_name else 0
            for item in panels
        ]
        return flags, []


# LIMIT: the stamp is applied by the poll tick (and on file load), never
# by a panel's own draw()/update callback - those can run in a restricted
# context that disallows writing ID data. So a legacy panel that is
# renamed before the very first poll tick after the file loads (picker
# never opened, timer not yet running) can still be renamed without its
# layout following. Normal use is unaffected: the Manage Panels list is
# only visible once the addon's timer is running.
def _dedupe_panel_uids():
    """Duplicating a scene (Scene > New > Full/Linked Copy) copies every
    panel row INCLUDING its hidden "_uid", so two scenes would share uids.
    _PANEL_NAME_REGISTRY is keyed by uid, so a rename in one scene would
    then look like an undo of a rename to the other scene's identical
    panel, and its saved layout would be migrated to the wrong name. Give
    every repeat of a uid a fresh one (the first scene keeps the original).

    The copied scene's multi-command buttons carry the OLD id in their
    panel_uid, so they are re-pointed at the fresh one in the same
    scene - otherwise every button in the copy would become an orphan.
    A uid repeated inside ONE scene (not a scene copy) just gets a fresh
    id for the repeat; its buttons stay with the first."""
    seen = set()
    for scene in bpy.data.scenes:
        scene_settings = getattr(scene, "rp_quick_action_settings", None)
        if scene_settings is None:
            continue
        seen_here = set()
        remap = {}
        for panel in scene_settings.panels:
            uid = panel.get("_uid")
            if not uid:
                continue
            if uid in seen:
                fresh = uuid.uuid4().hex
                remap[uid] = fresh
                panel["_uid"] = fresh
            elif uid in seen_here:
                panel["_uid"] = uuid.uuid4().hex
            else:
                seen_here.add(uid)
        seen |= seen_here
        if remap:
            for action in scene_settings.multi_actions:
                if action.panel_uid in remap:
                    action.panel_uid = remap[action.panel_uid]


def _stamp_panel_prev_names(settings):
    """Makes sure every RP_QuickPanel carries the hidden "_prev_name"
    shadow _on_quick_panel_name_changed() relies on to know what a panel
    was called BEFORE a rename. Panels created by an older version of
    this addon (or restored from a .blend saved before that shadow
    existed) don't have it, and without it the callback falls back to
    "old name == new name" and treats the rename as a no-op: nothing is
    migrated, so the panel's saved layout stays on disk under the old
    name and the renamed panel comes up empty, with its buttons gone
    too. Idempotent and cheap - only writes where it's missing.
    """
    _dedupe_panel_uids()
    for panel in settings.panels:
        if panel.get("_prev_name") is None:
            panel["_prev_name"] = panel.name
        # Also record what this session last saw for the panel, so an
        # undo/redo of a rename can be detected (see _PANEL_NAME_REGISTRY).
        _PANEL_NAME_REGISTRY.setdefault(
            _panel_uid(panel), (panel.armature_name, panel.name)
        )
    # Every panel has an id now, so the buttons can be bound to them.
    _stamp_multi_action_panel_uids(settings)
    # Same hook (file load + every poll tick, where writing is allowed):
    # keep Single buttons at their one-rule invariant where that is lossless.
    _normalize_single_actions(settings)


def _ensure_default_panel_for_rig(settings, armature_name):
    """If the given rig has no panels of its own yet - a rig nobody's ever
    added one for, or the last one just got deleted - add one default
    entry scoped to it and select it, rather than leaving Manage Panels
    (and the Qt picker's own Panel dropdown) sitting empty until the
    user thinks to click the + themselves. Also corrects panels_index
    if it's currently pointing at some OTHER rig's panel (e.g. right
    after switching rigs) - never leaves the "active" panel selection
    sitting on a row that filter_items() above would hide.
    """
    # Runs every poll tick (backend.poll_active_armature()), so any panel
    # missing its rename shadow gets stamped long before anyone can
    # double-click it.
    _stamp_panel_prev_names(settings)

    if not armature_name:
        return

    matching = [i for i, p in enumerate(settings.panels) if p.armature_name == armature_name]

    if not matching:
        item = settings.panels.add()
        item.armature_name = armature_name
        item.name = "Panel 1"
        _panel_uid(item)  # its stable id exists from creation
        _finalize_pending_panel_deletion(armature_name, "Panel 1")
        settings.panels_index = len(settings.panels) - 1
    elif settings.panels_index not in matching:
        settings.panels_index = _preferred_panel_index(settings, armature_name)


def _on_quick_panels_index_changed(self, context):
    """Fired when RP_QuickActionSettings.panels_index changes - either the
    user selected a different row in the Manage Panels list, or
    Controller.change_panel() just set it to mirror a pick made in the
    Qt picker's own Panel dropdown (guarded via
    _SYNCING_QUICK_PANEL_DROPDOWN so that push doesn't re-enter this
    callback and bounce the value back a second time). Only the former
    should push the new selection out to the Qt combo.
    """
    if 0 <= self.panels_index < len(self.panels):
        selected = self.panels[self.panels_index]
        _LAST_PANEL_BY_RIG[selected.armature_name] = selected.name

    if _SYNCING_QUICK_PANEL_DROPDOWN:
        return

    if not (0 <= self.panels_index < len(self.panels)):
        return

    from . import backend
    if backend._ACTIVE_CONTROLLER is not None:
        backend._ACTIVE_CONTROLLER.change_panel(self.panels[self.panels_index].name)



def _on_quick_active_rig_changed(self, context):
    """Fired when RP_QuickActionSettings.active_rig changes - either the
    user picked a different rig from this dropdown, or
    backend.poll_active_armature() just set it to reflect a switch made
    elsewhere (see the guard above). Only the former should actually
    switch the active armature.
    """
    if _SYNCING_QUICK_RIG_DROPDOWN:
        return

    rig = self.active_rig

    if rig is None:
        # The user cleared this field with the pointer property's own
        # X button, rather than picking a different rig - propagate
        # that "nothing selected" state to the viewport (and, in turn,
        # the Qt picker, via backend.poll_active_armature()'s own
        # armature-cleared handling on its next tick) the same way a
        # real pick below gets routed everywhere, rather than leaving
        # both still pointing at whatever rig was active a moment ago.
        view_layer = bpy.context.view_layer
        active = view_layer.objects.active
        if active is not None and active.type == 'ARMATURE':
            view_layer.objects.active = None
        return

    if rig.type != 'ARMATURE':
        return

    from . import backend
    # Route through the Qt controller's own change_armature() when the
    # picker window is open, so a dropdown-driven switch goes through
    # the exact same save-current/load-new/deselect/object-mode logic
    # as picking a rig from the Qt combo - not a second, divergent path.
    if backend._ACTIVE_CONTROLLER is not None:
        backend._ACTIVE_CONTROLLER.change_armature(rig.name)
    else:
        backend.select_and_activate_armature(rig)


class RP_QuickActionSettings(PropertyGroup):
    # This panel's own rig picker: kept in sync with the actual active
    # rig from anywhere it can change (viewport/Outliner selection, the
    # Qt picker's own combo) by backend.poll_active_armature() every
    # ~0.15s, AND picking a different rig here switches to it too (via
    # _on_quick_active_rig_changed() above) - same two-way behavior the
    # old Custom Actions panel's own rig dropdown had. See
    # _quick_active_rig() below for how it's resolved into an actual
    # rig to use.
    active_rig: PointerProperty(
        type=bpy.types.Object,
        name="Rig",
        description="The rig this panel's Multi-Command Buttons belong to. "
                    "Follows the active armature automatically (shown "
                    "read-only in the panel header)",
        poll=_poll_armature_object,
        update=_on_quick_active_rig_changed,
    )

    new_multi_type: EnumProperty(name="", description="Button type", items=QUICK_ACTION_TYPES, default='BUTTON')

    # Manage Panels section's panel list - kept in two-way sync with
    # the Qt picker's own Panel dropdown (stale_combo in
    # ui/main_window.py) by Controller.change_panel() and
    # Controller.sync_panel_combo_items(), the same way active_rig
    # above is synced with the Qt picker's Rig dropdown. Add/rename/
    # remove rows here (RP_OT_quick_add_panel, inline rename via the
    # RP_UL_quick_panels list, RP_OT_quick_remove_panel) and the Qt
    # combo's own item list follows along every poll tick.
    panels: CollectionProperty(type=RP_QuickPanel)
    panels_index: IntProperty(default=0, update=_on_quick_panels_index_changed)

    multi_actions: CollectionProperty(type=RP_QuickMultiAction)
    multi_actions_index: IntProperty()

    single_actions: CollectionProperty(type=RP_QuickSingleAction)
    single_actions_index: IntProperty()


def _quick_active_rig(settings):
    """Resolves this panel's own rig dropdown (RP_QuickActionSettings.
    active_rig) into an actual rig to scope Add/the multi-command list
    against. Normally just mirrors it directly - see the poll-driven
    sync in backend.poll_active_armature() that keeps it following the
    real active rig - but still falls back to backend.arm() directly
    for the brief window before that first poll tick has run (e.g.
    right after opening a file), or if the picked rig was ever somehow
    deleted out from under it.
    """
    from . import backend
    rig = settings.active_rig
    if rig is not None and rig.type == 'ARMATURE' and rig.name in bpy.context.view_layer.objects:
        return rig
    return backend.arm()


# Stable identity for a rig (RigRef). Currently NOT used by the picker's own
# undo: picker Ctrl+Z only reverts picker layout data and no longer carries a
# Toggle restore (see Controller.undo()); kept, with its tests, for a pinned
# rig reference. A rig's NAME
# is not stable: it can be renamed between a Toggle click and the undo/redo
# that reverts it (Outliner, F2, Properties panel), after which the old name
# resolves to nothing - or, worse, to a DIFFERENT rig that now carries it
# (two rigs swapping names). `session_uid` is assigned by Blender once per
# datablock and survives renames and (memfile) undo for the whole session,
# the same property Controller._sync_armature_combo_items() already relies
# on to tell a rename apart from a delete+add. `name` is kept for
# diagnostics only and is never used to look the rig up.
RigRef = namedtuple("RigRef", ("name", "session_uid"))


def make_rig_ref(rig):
    """Pins `rig` by its stable identity. None in, None out."""
    if rig is None:
        return None
    return RigRef(rig.name, rig.session_uid)


def resolve_rig_ref(ref):
    """Returns the armature object `ref` was made from, or None if it no
    longer exists (deleted, or the file was reloaded and uids reassigned).

    Resolved by session_uid, so a rename in between is followed. There is
    deliberately NO fall-back to the stored name when the uid isn't found:
    a deleted rig whose name has since been reused by an unrelated object
    must be skipped, not restored onto the wrong rig.

    A bare string (a snapshot from before RigRef existed) is still looked
    up by name - such snapshots only ever live in memory for one session,
    so this only matters for hand-built/scripted callers.
    """
    if ref is None:
        return None
    if isinstance(ref, str):
        obj = bpy.data.objects.get(ref)
    else:
        obj = next(
            (o for o in bpy.data.objects if o.session_uid == ref.session_uid),
            None,
        )
    if obj is None or obj.type != 'ARMATURE':
        return None
    return obj


# Ownership of a Multi button = the id of the PANEL that owns it
# (RP_QuickMultiAction.panel_uid <-> the panel's hidden "_uid"). The rig
# is whichever rig that panel belongs to (RP_QuickPanel.armature_name),
# so it isn't a second, separately-migrated copy that can drift. The
# name strings stored on the button (armature_name / panel_name) are
# display mirrors, refreshed by _stamp_multi_action_panel_uids() and the
# rename callbacks, and are only ever CONSULTED for a legacy button that
# has no panel_uid yet.
#
# LIMITS: (1) What the user has SELECTED is still a (rig name, panel
# name) pair - the picker's combos and Manage Panels list pick rows by
# name - so each use resolves that pair to the panel ROW first
# (_quick_panel_row) and compares rows, never names, against the owner.
# (2) The rig itself is still identified by name (panel.armature_name),
# kept current by rename_rig_scene_data(); a rig has no persistent id of
# its own across a save/reopen, and the saved layouts in
# rig_picker_data.json are keyed by rig/panel name as well.
def _quick_panel_by_uid(settings, uid):
    """The panel row whose stable id is `uid`, or None (no such panel)."""
    if not uid:
        return None
    return next((p for p in settings.panels if p.get("_uid") == uid), None)


def _quick_panel_row(settings, rig_name, panel_name):
    """Resolves a (rig name, panel name) UI selection - how the picker and
    Manage Panels identify the panel being shown - to its row, or None."""
    if not rig_name or panel_name is None:
        return None
    return next(
        (
            p for p in settings.panels
            if p.armature_name == rig_name and p.name == panel_name
        ),
        None,
    )


def quick_action_owner_panel(settings, action, default_rig_name=None):
    """The panel row that owns `action`, or None.

    By panel_uid when the button has one: names are not consulted at all,
    so a deleted-and-recreated panel of the same name is a stranger and a
    button whose panel is gone resolves to None (orphan).

    Only a legacy button WITHOUT a panel_uid falls back to its stored
    names (blank panel_name = the rig's first panel). `default_rig_name`
    lets a legacy button that predates rig-scoping resolve against the
    active rig; it is never used for a button that has a panel_uid.
    """
    uid = action.panel_uid
    if uid:
        return _quick_panel_by_uid(settings, uid)
    rig_name = action.armature_name or default_rig_name
    if not rig_name:
        return None
    if action.panel_name:
        return _quick_panel_row(settings, rig_name, action.panel_name)
    return next((p for p in settings.panels if p.armature_name == rig_name), None)


def quick_action_rig_name(settings, action):
    """The name of the rig `action` belongs to: its owning panel's rig when
    the panel is known (the single source of truth - the button's own
    armature_name is only a mirror of it), "" for an orphan, and only for a
    legacy button with no panel_uid its stored armature_name."""
    owner = quick_action_owner_panel(settings, action)
    if owner is not None:
        return owner.armature_name
    return "" if action.panel_uid else action.armature_name


def _quick_multi_action_visible(settings, action, rig_name, panel_name):
    """Single source of truth for "does this multi-command button belong
    to the (rig, panel) being shown?". Used by the N-panel list, the Qt
    canvas and the enabled-state refresh. (rig_name, panel_name) is the
    UI's current selection; it is resolved to a panel row and compared
    with the button's OWNING panel by identity - see
    quick_action_owner_panel().
    """
    shown = _quick_panel_row(settings, rig_name, panel_name)
    if shown is None:
        return False
    owner = quick_action_owner_panel(settings, action)
    return owner is not None and owner == shown


# rig_name -> name of the panel that was last selected on it. Lets a rig
# switch made ANYWHERE - the Blender viewport/Outliner, the picker's Rig
# combo - land back on the panel you were last using on that rig instead
# of always snapping to its first one. Session-only (plain memory): a
# stale/missing entry just falls back to the rig's first panel.
#
# LIMITS: (1) plain memory only - after restarting Blender each rig opens
# on its FIRST panel until you pick another. (2) The viewport itself has no
# notion of a panel, so the Panel label can't be driven by a viewport
# selection; it follows the rig chosen in the viewport/Outliner/picker and
# then resolves to the panel remembered for that rig. (3) Because updates
# arrive via the addon's poll timer, a change made in the viewport shows
# up in the picker window / N-panel within one poll interval, not
# instantly.
_LAST_PANEL_BY_RIG = {}


def _preferred_panel_index(settings, armature_name):
    """Index into settings.panels of the panel the given rig should be
    showing: the one last selected on it if it still exists, else its
    first. None if the rig has no panels."""
    matching = [
        i for i, p in enumerate(settings.panels) if p.armature_name == armature_name
    ]
    if not matching:
        return None
    remembered = _LAST_PANEL_BY_RIG.get(armature_name)
    for i in matching:
        if settings.panels[i].name == remembered:
            return i
    return matching[0]


def _quick_active_panel(settings, armature_name):
    """Resolves Manage Panels' current selection (settings.panels_index)
    into a panel ROW, scoped to armature_name - the panel-level
    counterpart to _quick_active_rig() above, used to scope Add/the
    multi-command list to a single (rig, panel) pair instead of just a
    rig.

    Returns None if armature_name is blank, or if panels_index isn't
    currently pointing at one of ITS panels (no rig active, or this
    rig's _ensure_default_panel_for_rig() hasn't run yet this tick) -
    callers treat that the same as "nothing to scope to yet" rather
    than guessing at some other panel.
    """
    if not armature_name:
        return None
    if 0 <= settings.panels_index < len(settings.panels):
        panel = settings.panels[settings.panels_index]
        if panel.armature_name == armature_name:
            return panel
    # panels_index still points at the PREVIOUS rig's panel (a rig switch
    # from the viewport whose poll tick hasn't corrected it yet). Resolve
    # to the very panel that correction is about to select, so the
    # N-panel's Panel label and button list are right on the first
    # redraw after the switch instead of flashing "None".
    index = _preferred_panel_index(settings, armature_name)
    return settings.panels[index] if index is not None else None


def _quick_active_panel_name(settings, armature_name):
    """_quick_active_panel()'s name - for display and for the name-keyed
    saved layouts (rig_picker_data.json). Ownership uses the row."""
    panel = _quick_active_panel(settings, armature_name)
    return panel.name if panel is not None else None


def _stamp_multi_action_panel_uids(settings):
    """Keeps every multi-command button's owner link current. Runs from
    _stamp_panel_prev_names() - i.e. on file load and every poll tick, a
    context where writing scene data is allowed (never from draw()).

      * A legacy button with no panel_uid is adopted by its stored names
        ONE time - here - and from then on follows the panel's id.
      * A button that has one is left bound to that panel; its display
        mirrors (panel_name, armature_name) are refreshed from the panel,
        so a rename/migration that was skipped or partial can't leave a
        stale name behind for anything else to trip over.
      * An orphan (panel_uid set, panel gone) is left exactly as it is:
        inert, never adopted by a new panel of the same name.
    """
    for action in settings.multi_actions:
        owner = quick_action_owner_panel(settings, action)
        if owner is None:
            continue
        uid = _panel_uid(owner)
        if action.panel_uid != uid:
            action.panel_uid = uid
        if action.panel_name != owner.name:
            action.panel_name = owner.name
        if action.armature_name != owner.armature_name:
            action.armature_name = owner.armature_name


def _migrate_legacy_multi_action_panels(scene=None):
    """One-time migration for RP_QuickMultiAction rows saved before
    Section 1 (multi-command buttons) became scoped to a single (rig,
    panel) pair instead of just a rig.

    A row with panel_name still blank predates panel_name existing at
    all - assign it to that rig's first tracked panel (the same
    "Panel 1" a rig's very first panel is always named) so it keeps
    showing up somewhere instead of silently vanishing everywhere the
    moment this version's per-panel filtering takes effect. Idempotent:
    already-migrated rows (non-blank panel_name) are left untouched, so
    it's safe to call on every file load rather than only once ever.
    """
    scenes = [scene] if scene is not None else bpy.data.scenes
    for scn in scenes:
        settings = getattr(scn, "rp_quick_action_settings", None)
        if settings is None:
            continue
        _stamp_panel_prev_names(settings)
        for action in settings.multi_actions:
            if action.panel_name or not action.armature_name:
                continue
            first_panel = next(
                (p.name for p in settings.panels if p.armature_name == action.armature_name),
                None,
            )
            action.panel_name = first_panel or "Panel 1"


# -----------------------------------------------------------------------------
# Operators - Manage Panels
# -----------------------------------------------------------------------------

def _next_unique_panel_name(settings, rig_name):
    """Returns the lowest-numbered "Panel N" that isn't already used by
    any panel on rig_name.

    Picks the first FREE number counting up from 1 instead of guessing
    from how many panels the rig already has. A count-based guess
    (count + 1) drifts as soon as the count and the names disagree - a
    panel renamed away from its "Panel N" name, a stray extra row left
    over from an earlier session, etc. - and produced "Panel 3" for what
    was visibly only the rig's second panel right after Clear All. With
    "first free number", a rig holding just "Panel 1" always gets
    "Panel 2" next, and a renamed-away number is naturally reused.
    """
    existing = {p.name for p in settings.panels if p.armature_name == rig_name}
    n = 1
    while f"Panel {n}" in existing:
        n += 1
    return f"Panel {n}"


def show_unsaved_file_popup(context):
    """Tells the user the file needs saving first. Rig Picker keeps its data
    in a folder next to the .blend, which a file without a name can't have."""
    def draw(menu, _context):
        col = menu.layout.column(align=True)
        col.label(text="This .blend file hasn't been saved yet.")
        col.label(text="Rig Picker keeps its data in a folder next to")
        col.label(text="your .blend file, so please save the file first,")
        col.label(text="then try again.")
        col.separator()
        col.operator("wm.save_as_mainfile", text="Save File As...", icon='FILE_TICK')

    context.window_manager.popup_menu(
        draw, title="Save your file first", icon='ERROR'
    )


def _file_is_unsaved():
    try:
        from . import json_manager
        return not json_manager._blend_filepath()
    except Exception:
        return False


class RP_OT_quick_add_panel(Operator):
    bl_idname = "rp.quick_add_panel"
    bl_label = "Add Panel"
    bl_description = "Add a new panel to the currently active rig's list"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _quick_active_rig(context.scene.rp_quick_action_settings) is not None

    def execute(self, context):
        if _file_is_unsaved():
            show_unsaved_file_popup(context)
            return {'CANCELLED'}
        settings = context.scene.rp_quick_action_settings
        rig = _quick_active_rig(settings)
        if rig is None:
            return {'CANCELLED'}

        item = settings.panels.add()
        item.armature_name = rig.name
        item.name = _next_unique_panel_name(settings, rig.name)
        _panel_uid(item)  # its stable id exists from creation
        _finalize_pending_panel_deletion(rig.name, item.name)
        # Assigning panels_index here fires _on_quick_panels_index_changed,
        # which pushes this new panel into the Qt combo as the current
        # selection - same "select what you just added" behavior
        # RP_OT_quick_add_multi_action/RP_OT_quick_add_single_action
        # already have via their own expanded=True default.
        settings.panels_index = len(settings.panels) - 1
        _update_blend_tracking()
        return {'FINISHED'}


class RP_OT_quick_remove_panel(Operator):
    bl_idname = "rp.quick_remove_panel"
    bl_label = "Remove Panel"
    bl_description = "Remove the selected panel from the list"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        settings = context.scene.rp_quick_action_settings
        if not (0 <= settings.panels_index < len(settings.panels)):
            return False
        # Keep at least one panel per rig - Manage Panels (like the Qt
        # picker's own Panel dropdown) has nothing sensible to show
        # once a rig's last panel is gone, and _ensure_default_panel_for_rig()
        # would just silently recreate a blank "Panel 1" on the very
        # next poll tick anyway, so disable rather than let the click
        # through to that surprise.
        rig_name = settings.panels[settings.panels_index].armature_name
        panels_for_rig = sum(1 for p in settings.panels if p.armature_name == rig_name)
        return panels_for_rig > 1

    def execute(self, context):
        settings = context.scene.rp_quick_action_settings
        index = settings.panels_index
        if not (0 <= index < len(settings.panels)):
            return {'CANCELLED'}

        rig_name = settings.panels[index].armature_name
        panel_name = settings.panels[index].name
        # Read BEFORE the row is removed - afterwards there is nothing
        # left to ask for its id.
        removed_uid = _panel_uid(settings.panels[index])
        settings.panels.remove(index)

        # This panel can never be switched back to - remove its own
        # stored picker data (items, background capture, symmetry - see
        # json_manager's per-panel storage) and captured-view image the
        # same way a deleted RIG's data is cleaned up
        # (Controller._sync_armature_combo_items()'s deleted_armatures
        # handling), rather than leaving it as permanent orphaned data
        # under a name Manage Panels no longer shows.
        from . import json_manager
        # Deferred (see _PENDING_PANEL_DELETIONS): undo can bring this
        # panel back within the grace period and its layout must still
        # be there. Nothing resurrects it meanwhile - the Controller's
        # reference to it is cleared below before any save() can run.
        _schedule_panel_deletion(rig_name, panel_name)

        # This panel's own multi-command buttons (Section 1) can never
        # be shown or run again either - the N-panel list and the Qt
        # canvas both filter strictly on (armature_name, panel_name),
        # so leaving them in settings.multi_actions would just strand
        # them there permanently with no UI path to ever reach or
        # remove them, the same orphaned-data problem the json/undo
        # cleanup just above solves for this panel's picker layout and
        # history. Iterate back-to-front so removing by index is safe.
        for i in range(len(settings.multi_actions) - 1, -1, -1):
            action = settings.multi_actions[i]
            # Owned by the removed panel's id; names are only consulted
            # for a legacy button that never got one.
            if action.panel_uid:
                owned = action.panel_uid == removed_uid
            else:
                owned = action.armature_name == rig_name and action.panel_name == panel_name
            if owned:
                settings.multi_actions.remove(i)

        # This panel's own picker undo/redo history is taken out of the
        # live stacks but NOT thrown away: Blender's undo can bring the
        # panel back within the grace period, and picker Ctrl+Z should
        # then still walk back through what was done on it. It is parked
        # (_PENDING_PANEL_HISTORY) and restored if the panel comes back, or
        # settled when it can't (grace period over, rig purged, file
        # change).
        from . import backend
        controller = backend._ACTIVE_CONTROLLER
        if controller is not None:
            _park_panel_history(controller, rig_name, panel_name)

            # If this panel is the one currently loaded, clear the
            # in-memory reference to it DIRECTLY rather than letting the
            # panels_index reassignment below reach change_panel() ->
            # _switch_to_panel() the normal way - that path's first step
            # is to save() whatever was loaded before switching away
            # from it, which would simply write self.data straight back
            # out under (rig_name, panel_name), silently resurrecting
            # the very entry delete_panel_data() just removed. Same
            # failure mode, and same fix, as the deleted-RIG case in
            # Controller._sync_armature_combo_items().
            if controller.armature_name == rig_name and controller.panel_name == panel_name:
                controller.panel_name = None
                controller.data = json_manager.new_panel_data()

        # Land back on another panel belonging to the SAME rig if one's
        # left, rather than whatever happens to now sit at this index
        # (which, post-removal, could easily belong to a different rig
        # entirely) - _ensure_default_panel_for_rig() on the very next
        # poll tick would otherwise catch this anyway, but doing it here
        # too means the Qt combo and this list agree immediately rather
        # than for one tick showing a mismatched selection.
        remaining_for_rig = [i for i, p in enumerate(settings.panels) if p.armature_name == rig_name]
        if remaining_for_rig:
            settings.panels_index = remaining_for_rig[0]
        else:
            settings.panels_index = max(0, min(index, len(settings.panels) - 1))
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Operators - Section 1 (multi-command buttons)
# -----------------------------------------------------------------------------

def _panel_has_image(rig_name, panel_name):
    """True if the given (rig, panel) pair currently has a background image
    the picker can actually show - which is what a multi-command button
    needs in order to appear anywhere (buttons are only drawn on top of
    that image, so adding one to an image-less panel changes nothing
    visible).

    Mirrors the picker's own gating for its Add Selected button
    (Controller._update_action_controls_enabled): if the picker is open
    on this very (rig, panel), trust what's really loaded on its canvas -
    a stored path can point at a file that no longer loads. Otherwise
    fall back to the saved panel data plus a check that the file exists.
    Fails OPEN (True) on any unexpected error, so a bug here can never
    lock the Add button for good.
    """
    if not rig_name or not panel_name:
        return False
    try:
        from . import backend
        controller = backend._ACTIVE_CONTROLLER
        window = getattr(controller, "window", None)
        if (
            controller is not None
            and window is not None
            and controller.armature_name == rig_name
            and controller.panel_name == panel_name
        ):
            return window.control_list.container.background is not None

        from . import json_manager
        data = json_manager.get_panel_data(rig_name, panel_name) or {}
        path = data.get("background") or ""
        if not path:
            path = json_manager.get_image_path(rig_name, panel_name)
        return bool(path) and os.path.isfile(path)
    except Exception:
        return True


def _picker_bone_names(rig_name, panel_name):
    """Names of the bones that already have a control on the picker for the
    given (rig, panel) pair, or None if that can't be determined.

    Same sourcing as _panel_has_image(): if the picker is open on this very
    (rig, panel), the live controller.data is trusted (it can be ahead of
    what is saved); otherwise the saved panel data is used. A blank
    panel_name (legacy button) resolves to the rig's first panel, matching
    _quick_multi_action_visible().

    Returns None (callers then do NOT restrict) on any unexpected error, so
    a bug here can never lock the Add Control Bone button for good.
    """
    if not rig_name:
        return None
    try:
        from . import backend, json_manager

        if not panel_name:
            scene = bpy.context.scene
            settings = getattr(scene, "rp_quick_action_settings", None)
            panel_name = next(
                (p.name for p in settings.panels if p.armature_name == rig_name),
                None,
            ) if settings is not None else None
            if not panel_name:
                return None

        controller = backend._ACTIVE_CONTROLLER
        window = getattr(controller, "window", None)
        if (
            controller is not None
            and window is not None
            and controller.armature_name == rig_name
            and controller.panel_name == panel_name
        ):
            items = controller.data.get("items", [])
        else:
            data = json_manager.get_panel_data(rig_name, panel_name) or {}
            items = data.get("items", [])
        return {i["bone_name"] for i in items if i.get("bone_name")}
    except Exception:
        return None


def _iter_panel_multi_actions(settings, rig_name, panel_name):
    """Multi-command buttons belonging to the (rig, panel) pair - the same
    test the N-panel and the picker canvas use (_quick_multi_action_visible)."""
    for action in settings.multi_actions:
        if _quick_multi_action_visible(settings, action, rig_name, panel_name):
            yield action


def find_control_bone_references(rig_name, panel_name, bone_names, settings=None):
    """Where the given bones are used as Control shape bones on this
    (rig, panel)'s multi-command buttons.

    Returns [(action, rule_index, [bone names])], one entry per command
    that lists at least one of them. Empty if there are none (or no rig).
    Used by the picker's Delete to decide whether to ask before deleting.
    """
    if not rig_name:
        return []
    if settings is None:
        settings = getattr(bpy.context.scene, "rp_quick_action_settings", None)
    if settings is None:
        return []
    wanted = {n.strip() for n in bone_names}
    found = []
    for action in _iter_panel_multi_actions(settings, rig_name, panel_name):
        for rule_index, rule in enumerate(action.rules):
            hits = [item.name for item in rule.bones if item.name.strip() in wanted]
            if hits:
                found.append((action, rule_index, hits))
    return found


def remove_control_bone_references(rig_name, panel_name, bone_names, settings=None):
    """Removes the given bones from the Control shape bones of this
    (rig, panel)'s multi-command buttons. A command left with no bones
    simply stops matching anything (see _quick_find_matching_rule) - the
    command itself is kept.

    Returns what was removed, in the form apply_control_bone_presence()
    takes, so the removal can be undone/redone later:
        [{"uid": action uid, "rule_index": int,
          "bones": [(original position, bone name), ...]}]
    """
    if not rig_name:
        return []
    if settings is None:
        settings = getattr(bpy.context.scene, "rp_quick_action_settings", None)
    if settings is None:
        return []
    wanted = {n.strip() for n in bone_names}
    removed = []
    for action in _iter_panel_multi_actions(settings, rig_name, panel_name):
        for rule_index, rule in enumerate(action.rules):
            hits = [
                (i, item.name) for i, item in enumerate(rule.bones)
                if item.name.strip() in wanted
            ]
            if not hits:
                continue
            for i, _name in reversed(hits):
                rule.bones.remove(i)
            rule.bones_index = max(0, min(rule.bones_index, len(rule.bones) - 1))
            removed.append({"uid": action.uid, "rule_index": rule_index, "bones": hits})
    return removed


def apply_control_bone_presence(entries, present, settings=None):
    """Puts the bones in `entries` (as returned by
    remove_control_bone_references) back into their commands'
    Control shape bones (present=True), or takes them out again
    (present=False). This is what picker undo/redo of a control delete
    calls, so it is deliberately surgical: only those exact bones are
    touched, so anything else edited on the same lists since is kept.

    Skips, instead of failing, anything that no longer exists (a removed
    button or command) or is already in the requested state. A restored
    bone is also skipped if another command on the same button has taken
    it meanwhile (a bone can only be in one command per button).
    Returns how many bone entries were changed.
    """
    if settings is None:
        settings = getattr(bpy.context.scene, "rp_quick_action_settings", None)
    if settings is None or not entries:
        return 0
    changed = 0
    for entry in entries:
        action = next((a for a in settings.multi_actions if a.uid == entry["uid"]), None)
        if action is None or not (0 <= entry["rule_index"] < len(action.rules)):
            continue
        rule = action.rules[entry["rule_index"]]
        if present:
            for position, name in sorted(entry["bones"]):
                taken = any(
                    item.name.strip() == name.strip()
                    for r in action.rules for item in r.bones
                )
                if taken:
                    continue
                new_item = rule.bones.add()
                new_item.name = name
                rule.bones.move(len(rule.bones) - 1, min(position, len(rule.bones) - 1))
                changed += 1
        else:
            names = {name.strip() for _position, name in entry["bones"]}
            for i in range(len(rule.bones) - 1, -1, -1):
                if rule.bones[i].name.strip() in names:
                    rule.bones.remove(i)
                    changed += 1
            rule.bones_index = max(0, min(rule.bones_index, len(rule.bones) - 1))
    return changed


# A picker control delete that also removed bones from Multi Command Buttons'
# Control shape bone lists (Controller.delete_selected) touches two stores
# that Blender's own undo treats differently: the button lists are scene
# data (native Ctrl+Z / Ctrl+Shift+Z in the viewport rewinds them), the
# picker's controls live in rig_picker_data.json (native undo never touches
# it). Left alone, a native undo would put the bones back into the buttons
# while the control stayed deleted - a bone in a list that isn't on the
# picker. Each such delete is therefore remembered here, and
# _update_control_deletes_after_undo() (run after every native undo/redo)
# makes the picker's controls follow whatever the button lists now say:
# bones back -> control restored, bones gone -> control deleted again.
# Same approach as _PENDING_RIG_CLEARS above.
#
# entry = {
#   "rig", "panel" (names when deleted - the CURRENT ones are re-read from
#   the buttons themselves, which the rename callbacks keep up to date),
#   "items" ([(position, control item dict)] as deleted),
#   "entries" (what remove_control_bone_references() returned),
#   "witness" (True = the bones are in the buttons, False = removed)
# }
#
# LIMITS: plain memory (cleared when a file is opened, since that also wipes
# Blender's undo history), the last _MAX_PENDING_CONTROL_DELETES deletes
# only, and only while the buttons involved still exist: a bone list that
# can't be read (button removed) is left alone rather than guessed at.
_PENDING_CONTROL_DELETES = []
_MAX_PENDING_CONTROL_DELETES = 50


def register_control_delete(rig_name, panel_name, items, entries):
    """Remembers one picker delete (see above). `items` are the deleted
    controls as [(position, item dict)], `entries` the matching result of
    remove_control_bone_references(). Returns the registry entry."""
    import copy
    entry = {
        "rig": rig_name,
        "panel": panel_name,
        "items": [(index, copy.deepcopy(item)) for index, item in items],
        "entries": entries,
        "witness": False,
    }
    _PENDING_CONTROL_DELETES.append(entry)
    del _PENDING_CONTROL_DELETES[:-_MAX_PENDING_CONTROL_DELETES]
    return entry


def note_control_bones_applied(entries, present):
    """The picker's own undo/redo just put the bones in `entries` back
    (present=True) or took them out (False) directly. Records that, so the
    next native undo/redo only reacts to a real change after it."""
    for entry in _PENDING_CONTROL_DELETES:
        if entry["entries"] is entries:
            entry["witness"] = present


def push_native_undo_step(message):
    """Adds a step to Blender's own undo stack, so the scene edit a picker
    delete just made (the button lists) is something viewport Ctrl+Z can
    revert - and _update_control_deletes_after_undo() can then react to."""
    try:
        from . import backend
        rig = backend.arm()
        override = backend.get_3d_override(bpy.context, rig) if rig else None
        if override:
            with bpy.context.temp_override(**override):
                bpy.ops.ed.undo_push(message=message)
        else:
            bpy.ops.ed.undo_push(message=message)
    except Exception:
        import traceback
        traceback.print_exc()


def _control_bones_witness(entries, settings):
    """True if the bones in `entries` are currently in their commands'
    Control shape bone lists, False if all are gone, None if that can't be
    told (a button/command is missing, or some are in and some out)."""
    states = []
    for entry in entries:
        action = next((a for a in settings.multi_actions if a.uid == entry["uid"]), None)
        if action is None or not (0 <= entry["rule_index"] < len(action.rules)):
            continue
        in_rule = {item.name.strip() for item in action.rules[entry["rule_index"]].bones}
        for _position, name in entry["bones"]:
            states.append(name.strip() in in_rule)
    if not states:
        return None
    if all(states):
        return True
    if not any(states):
        return False
    return None


def _entry_location(entry, settings):
    """The (rig, panel) this delete belongs to RIGHT NOW. Read from the
    first button it touched, whose names follow rig/panel renames; falls
    back to the names recorded at delete time."""
    for item in entry["entries"]:
        action = next((a for a in settings.multi_actions if a.uid == item["uid"]), None)
        if action is None:
            continue
        owner = quick_action_owner_panel(settings, action)
        if owner is not None:
            return owner.armature_name, owner.name
        if action.panel_uid:
            continue  # orphan: its panel is gone, don't guess by name
        rig_name = action.armature_name or entry["rig"]
        panel_name = action.panel_name or next(
            (p.name for p in settings.panels if p.armature_name == rig_name), None
        ) or entry["panel"]
        return rig_name, panel_name
    return entry["rig"], entry["panel"]


def _set_picker_controls_present(entry, rig_name, panel_name, present):
    """Puts the deleted controls back on the picker panel (present=True,
    at their original positions) or deletes them again (False). Only the
    recorded controls are touched, and one already in the wanted state is
    skipped - so a control the user re-added meanwhile is never duplicated
    or removed. Returns True if anything changed."""
    import copy
    from . import backend, json_manager

    controller = getattr(backend, "_ACTIVE_CONTROLLER", None)
    live = (
        controller is not None
        and controller.armature_name == rig_name
        and controller.panel_name == panel_name
    )
    data = controller.data if live else json_manager.get_panel_data(rig_name, panel_name)
    if not isinstance(data, dict):
        return False
    items = data.setdefault("items", [])

    changed = False
    if present:
        have = {item["bone_name"] for item in items}
        for position, item in sorted(entry["items"], key=lambda pair: pair[0]):
            if item["bone_name"] in have:
                continue
            items.insert(min(position, len(items)), copy.deepcopy(item))
            have.add(item["bone_name"])
            changed = True
    else:
        names = {item["bone_name"] for _position, item in entry["items"]}
        kept = [item for item in items if item["bone_name"] not in names]
        if len(kept) != len(items):
            items[:] = kept
            changed = True
            if live:
                controller.selected_bones -= names

    if not changed:
        return False
    if live:
        controller.save()
        controller.refresh()
    else:
        json_manager.save_panel_data(rig_name, panel_name, data)
    return True


def _update_control_deletes_after_undo():
    """After a native undo/redo: if it changed whether the bones of a
    remembered picker delete are in the buttons, make the picker's
    controls match (see _PENDING_CONTROL_DELETES)."""
    if not _PENDING_CONTROL_DELETES:
        return
    settings = getattr(bpy.context.scene, "rp_quick_action_settings", None)
    if settings is None:
        return
    for entry in list(_PENDING_CONTROL_DELETES):
        state = _control_bones_witness(entry["entries"], settings)
        if state is None or state == entry["witness"]:
            continue
        entry["witness"] = state
        rig_name, panel_name = _entry_location(entry, settings)
        _set_picker_controls_present(entry, rig_name, panel_name, state)


class RP_OT_quick_add_multi_action(Operator):
    bl_idname = "rp.quick_add_multi_action"
    bl_label = "Add"
    bl_description = "Add a new button that can hold multiple commands, each with its own Control shape bones"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _file_is_unsaved():
            show_unsaved_file_popup(context)
            return {'CANCELLED'}
        settings = context.scene.rp_quick_action_settings

        rig = _quick_active_rig(settings)
        rig_name = rig.name if rig is not None else ""
        panel_name = _quick_active_panel_name(settings, rig_name)
        if panel_name is None and rig_name:
            # panels_index isn't on one of this rig's panels yet (e.g.
            # right after a rig switch, before the poll tick corrected
            # it). Bind to this rig's first panel explicitly instead of
            # storing a blank name, so the button always has a definite
            # (rig, panel) owner.
            panel_name = next(
                (p.name for p in settings.panels if p.armature_name == rig_name),
                None,
            )

        if not _panel_has_image(rig_name, panel_name):
            self.report(
                {'WARNING'},
                "Add an image to this panel first (Capture View in the "
                "picker) - buttons are drawn on top of it.",
            )
            return {'CANCELLED'}

        type_label = "Toggle" if settings.new_multi_type == 'TOGGLE' else "Button"
        # Scoped to (rig, panel), the same way Manage Panels' own
        # "Panel N" default name is scoped to a rig - counting across
        # every OTHER panel's buttons too would produce misleading
        # numbers ("Button 3" as the very first button on a fresh
        # panel) the moment more than one panel has buttons of the
        # same type.
        count = sum(
            1 for a in settings.multi_actions
            if a.action_type == settings.new_multi_type
            and _quick_multi_action_visible(settings, a, rig_name, panel_name)
        ) + 1
        item = settings.multi_actions.add()
        item.uid = uuid.uuid4().hex
        item.name = f"{type_label} {count}"
        item.action_type = settings.new_multi_type
        item.armature_name = rig_name
        item.panel_name = panel_name or ""
        # Bound to the panel by its stable id from the start.
        owner_row = _quick_panel_row(settings, rig_name, panel_name)
        item.panel_uid = _panel_uid(owner_row) if owner_row is not None else ""
        item.rules.add()  # start with one command slot, same as the real Add
        settings.multi_actions_index = len(settings.multi_actions) - 1
        _sync_quick_picker_ui()
        _update_blend_tracking()
        return {'FINISHED'}


class RP_OT_quick_remove_multi_action(Operator):
    bl_idname = "rp.quick_remove_multi_action"
    bl_label = "Remove Button"
    bl_description = "Remove this button and all of its commands"
    bl_options = {'REGISTER', 'UNDO'}

    action_index: IntProperty()

    def execute(self, context):
        settings = context.scene.rp_quick_action_settings
        if 0 <= self.action_index < len(settings.multi_actions):
            settings.multi_actions.remove(self.action_index)
        _sync_quick_picker_ui()
        return {'FINISHED'}


class RP_OT_quick_rename_multi_action(Operator):
    bl_idname = "rp.quick_rename_multi_action"
    bl_label = "Rename"
    bl_description = "Rename the button"
    bl_options = {'REGISTER', 'UNDO'}

    action_index: IntProperty()
    new_name: StringProperty(name="Name")

    def invoke(self, context, event):
        settings = context.scene.rp_quick_action_settings
        if 0 <= self.action_index < len(settings.multi_actions):
            self.new_name = settings.multi_actions[self.action_index].name
        return context.window_manager.invoke_props_dialog(self, width=200)

    def draw(self, context):
        split = self.layout.split(factor=0.28)
        split.label(text="Name")
        split.prop(self, "new_name", text="")

    def execute(self, context):
        settings = context.scene.rp_quick_action_settings
        if 0 <= self.action_index < len(settings.multi_actions):
            settings.multi_actions[self.action_index].name = self.new_name
        for area in context.screen.areas:
            area.tag_redraw()
        _sync_quick_picker_ui()
        return {'FINISHED'}


class RP_OT_quick_add_rule(Operator):
    bl_idname = "rp.quick_add_rule"
    bl_label = "Add a Command"
    bl_description = "Add another command slot to this button"
    bl_options = {'REGISTER', 'UNDO'}

    action_index: IntProperty()

    def execute(self, context):
        if _file_is_unsaved():
            show_unsaved_file_popup(context)
            return {'CANCELLED'}
        settings = context.scene.rp_quick_action_settings
        if 0 <= self.action_index < len(settings.multi_actions):
            settings.multi_actions[self.action_index].rules.add()
        # A new/removed command can change which rule the current
        # selection matches, which changes this button's enabled/
        # greyed look on the picker canvas - resync it here the same
        # way add/remove/rename of the whole button already does,
        # rather than leaving the canvas stale until some unrelated
        # selection change happens to trigger the next sync.
        _sync_quick_picker_ui()
        return {'FINISHED'}


class RP_OT_quick_remove_rule(Operator):
    bl_idname = "rp.quick_remove_rule"
    bl_label = "Remove Command"
    bl_description = "Remove this command slot"
    bl_options = {'REGISTER', 'UNDO'}

    action_index: IntProperty()
    rule_index: IntProperty()

    def execute(self, context):
        settings = context.scene.rp_quick_action_settings
        if 0 <= self.action_index < len(settings.multi_actions):
            action = settings.multi_actions[self.action_index]
            if 0 <= self.rule_index < len(action.rules):
                action.rules.remove(self.rule_index)
        # See RP_OT_quick_add_rule's own comment above - same reasoning.
        _sync_quick_picker_ui()
        return {'FINISHED'}


class RP_OT_quick_add_control_bone(Operator):
    bl_idname = "rp.quick_add_control_bone"
    bl_label = "Add Control Bone"
    bl_description = "Add a control shape bone slot for each selected bone (does nothing if none selected)"
    bl_options = {'REGISTER', 'UNDO'}

    action_index: IntProperty()
    rule_index: IntProperty()

    def execute(self, context):
        if _file_is_unsaved():
            show_unsaved_file_popup(context)
            return {'CANCELLED'}
        settings = context.scene.rp_quick_action_settings
        if not (0 <= self.action_index < len(settings.multi_actions)):
            return {'CANCELLED'}
        action = settings.multi_actions[self.action_index]
        if not (0 <= self.rule_index < len(action.rules)):
            return {'CANCELLED'}
        rule = action.rules[self.rule_index]

        selected_bones = getattr(context, "selected_pose_bones", None) or []
        bone_names = [b.name for b in selected_bones]
        if not bone_names:
            active_bone = getattr(context, "active_pose_bone", None)
            if active_bone is not None:
                bone_names = [active_bone.name]
        if not bone_names:
            return {'CANCELLED'}

        # A control bone must already exist on the picker for THIS button's
        # (rig, panel) - the picker canvas is what a button is matched
        # against and drawn on, so a bone that isn't there can never be
        # part of a working selection. Refuse it and say what to do.
        # (None = couldn't be determined: don't restrict, see
        # _picker_bone_names().)
        # The picker layout is keyed by (rig, panel) NAME, so use the
        # names the owning panel has RIGHT NOW. A button whose panel is
        # gone (orphan) must not fall back to its stored names: that
        # could read the layout of a different panel recreated under the
        # same name.
        owner = quick_action_owner_panel(settings, action)
        if owner is None and action.panel_uid:
            self.report({'ERROR'}, f'"{action.name}" belongs to a panel that no longer exists.')
            return {'CANCELLED'}
        picker_bones = _picker_bone_names(
            owner.armature_name if owner is not None else action.armature_name,
            owner.name if owner is not None else action.panel_name,
        )
        if picker_bones is not None:
            not_in_picker = [n for n in bone_names if n not in picker_bones]
            if not_in_picker:
                if len(not_in_picker) == 1:
                    message = (
                        f'Control shape bone not added: "{not_in_picker[0]}" is '
                        "not on the picker. Add it to the picker first, then "
                        "add it here."
                    )
                else:
                    message = (
                        "Control shape bones not added - these bones are not on "
                        f"the picker: {', '.join(not_in_picker)}. Add them to "
                        "the picker first, then add them here."
                    )
                self.report({'WARNING'}, message)
                bone_names = [n for n in bone_names if n in picker_bones]
                if not bone_names:
                    return {'CANCELLED'}

        existing = {item.name for item in rule.bones}
        used_by_others = {}
        for other_index, other_rule in enumerate(action.rules):
            if other_index == self.rule_index:
                continue
            for item in other_rule.bones:
                used_by_others.setdefault(item.name, other_index)

        skipped_taken = []
        skipped_duplicate = []
        for bone_name in bone_names:
            if bone_name in existing:
                skipped_duplicate.append(bone_name)
                continue
            if bone_name in used_by_others:
                skipped_taken.append(bone_name)
                continue
            new_item = rule.bones.add()
            new_item.name = bone_name
            existing.add(bone_name)

        if skipped_taken:
            self.report(
                {'WARNING'},
                f"Already used by another command on this button: {', '.join(skipped_taken)}",
            )
        if skipped_duplicate:
            self.report(
                {'WARNING'},
                f"Already added to this command: {', '.join(skipped_duplicate)}",
            )

        # See RP_OT_quick_add_rule's own comment for why - a changed
        # Control shape bones list can change which rule the current
        # selection matches, which the picker canvas's enabled/greyed
        # state needs to reflect.
        _sync_quick_picker_ui()
        return {'FINISHED'}


class RP_OT_quick_remove_control_bone(Operator):
    bl_idname = "rp.quick_remove_control_bone"
    bl_label = "Remove Control Bone"
    bl_description = "Remove the selected control shape bone slot"
    bl_options = {'REGISTER', 'UNDO'}

    action_index: IntProperty()
    rule_index: IntProperty()

    def execute(self, context):
        settings = context.scene.rp_quick_action_settings
        if not (0 <= self.action_index < len(settings.multi_actions)):
            return {'CANCELLED'}
        action = settings.multi_actions[self.action_index]
        if not (0 <= self.rule_index < len(action.rules)):
            return {'CANCELLED'}
        rule = action.rules[self.rule_index]
        if 0 <= rule.bones_index < len(rule.bones):
            rule.bones.remove(rule.bones_index)
            rule.bones_index = max(0, rule.bones_index - 1)
        # See RP_OT_quick_add_rule's own comment above - same reasoning.
        _sync_quick_picker_ui()
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Control-bone reference validation
#
# A Multi rule stores its Control shape bones as plain NAME strings. Blender
# has no hook for "a bone was renamed/deleted", so a stale name just stays in
# the rule: the command is still displayed, but no selection can ever match it
# and the button never enables, with nothing saying why. These helpers find
# the stale names (against the rig's live pose bones) so every place that
# shows or runs a Multi action can say so:
#   * the N-panel marks the missing bones in the list and on the command/
#     button headers (RP_UL_control_bones, _draw_rule_blocks, _draw_multi_action)
#   * the picker canvas button gets a tooltip + warning look
#     (Controller.refresh_quick_multi_action_enabled_states)
#   * Controller.refresh_quick_multi_action_bone_validity() re-validates every
#     poll tick in which the rig's bone names changed (rename, delete, undo
#     restoring a bone, ...) and whenever a rig is (re)loaded
#   * RP_OT_quick_run_multi_action validates again right before it runs
#
# Deliberately NOT done: guessing that a vanished name + a new name is a
# rename and rewriting the rule. A bone has no stable identity (unlike an
# Object's session_uid), so that would be a heuristic that can silently
# retarget a command at the wrong bone. The rule is left exactly as the user
# configured it and the stale name is flagged; the existing remove/add
# control-bone buttons fix it.
# ---------------------------------------------------------------------------

def _rig_bone_names(rig):
    """Set of every pose bone name currently on `rig`, or None when it can't
    be known (no rig) - callers must treat None as "can't validate", never
    as "every bone is missing"."""
    if rig is None or getattr(rig, "pose", None) is None:
        return None
    return set(rig.pose.bones.keys())


def quick_rule_missing_bones(rule, bone_names):
    """Names (stripped, de-duplicated, in list order) in `rule`'s Control
    shape bones that aren't in `bone_names`. Same strip() the matcher uses."""
    if bone_names is None:
        return []
    missing = []
    for item in rule.bones:
        name = item.name.strip()
        if name and name not in bone_names and name not in missing:
            missing.append(name)
    return missing


def quick_action_missing_bones(action, rig):
    """Validates the ENTIRE action: {rule_index (0-based): [missing names]}
    for every rule that references a bone `rig` doesn't have. Empty when
    everything resolves - or when there's no rig to check against."""
    names = _rig_bone_names(rig)
    if names is None:
        return {}
    result = {}
    for index, rule in enumerate(action.rules):
        missing = quick_rule_missing_bones(rule, names)
        if missing:
            result[index] = missing
    return result


def quick_missing_bones_text(missing, separator="\n"):
    """Human-readable form of quick_action_missing_bones()'s result."""
    return separator.join(
        f"Command {index + 1}: {', '.join(names)}"
        for index, names in sorted(missing.items())
    )


def _quick_rig_has_bone(rig, name):
    """Cheap single-name check for list drawing. True (= don't flag) when
    there's no rig to check against."""
    name = name.strip()
    if rig is None or not name or getattr(rig, "pose", None) is None:
        return True
    return name in rig.pose.bones


AMBIGUOUS_RULES_PREFIX = "Ambiguous configuration"


def _quick_find_matching_rule(selected_names, rules):
    """Simplified counterpart to custom_actions.find_matching_rule() for
    Quick multi-command buttons: a rule only matches when the WHOLE
    current selection is a subset of that rule's own Control shape
    bones. Unlike custom_actions.find_matching_rule(), a rule with an
    empty Control shape bones list is never treated as a wildcard - it
    simply can't match anything until bones are added to it. Also
    skips custom_actions.py's rig-scoping, since a Quick multi button
    is already scoped to one rig via RP_QuickMultiAction.armature_name
    (the panel only ever draws the buttons whose armature_name matches
    the current Rig dropdown), so a second per-rule rig check isn't
    needed here.

    EVERY rule on the action is checked, not just up to the first hit.
    If more than one rule matches the selection (overlapping Control
    shape bones), the configuration is ambiguous and NOTHING is
    returned - silently running whichever rule happens to be listed
    first could execute the wrong command. Rules with an empty command
    count towards this too: a placeholder that overlaps a real rule is
    still an unresolved configuration. Because the button's enabled
    state, Toggle state sync and the run operator all resolve through
    this one function, an ambiguous action is greyed out and refuses
    to run everywhere at once.

    Returns (rule, warning) - rule is None (with a warning string) if
    nothing matched or the match was ambiguous (that warning starts
    with AMBIGUOUS_RULES_PREFIX).
    """
    selected_names = set(selected_names or ())

    if not rules:
        return None, "No commands added yet"
    if not selected_names:
        return None, "No bone selected in the viewport"

    matches = []
    for index, rule in enumerate(rules, start=1):
        rule_bone_names = {item.name.strip() for item in rule.bones if item.name.strip()}
        if rule_bone_names and selected_names <= rule_bone_names:
            matches.append((index, rule))

    if len(matches) > 1:
        which = ", ".join(f"#{index}" for index, _ in matches)
        return None, (
            f"{AMBIGUOUS_RULES_PREFIX}: commands {which} all match this "
            "selection. Remove the overlapping Control shape bones so "
            "only one command matches. Not run."
        )

    if matches:
        _, rule = matches[0]
        if not rule.command.strip():
            return None, "Matched command is empty"
        return rule, None

    return None, "Selected bone doesn't match any command's control bone list"


def _read_quick_toggle_original(rig, command, unreadable=None):
    """Returns a Quick Toggle's command's CURRENT value exactly as the
    property holds it - original object and type, see
    _eval_toggle_original() - or None if there's no rig yet or the command
    can't be evaluated (e.g. the property doesn't exist yet: first-ever run).

    Used by the run operator, right before flipping a Toggle, to read the
    pre-click value: it decides the next state, and the value written
    keeps the property's own TYPE, so a click can't turn a boolean/int
    property into a float. (Reverting the click is Blender's own undo.)
    """
    if rig is None:
        return unreadable
    return _eval_toggle_original(rig, command, unreadable=unreadable)


def _snapshot_numeric_custom_props(rig):
    """Snapshots every custom property currently on `rig` and its pose
    bones that holds a plain number, keyed by where it lives:
    `('object', None, prop_name)` for a property on the rig object
    itself, `('bone', bone_name, prop_name)` for one on a pose bone.

    This is the "before" half of a before/after diff (see
    _changed_numeric_custom_props below), used by the Auto-Key step to
    find out WHAT a Toggle's command actually changed by comparing
    state rather than reading the command's text - so auto-keying
    works for any command, not just the one recognized
    `.pose.bones["Bone"]["Prop"]` shape a parser could match.

    Only numeric properties are tracked (bool excluded even though
    Python treats it as an int subclass, since bpy custom properties
    are never actually bools) - a Toggle's own value is always
    numeric (0/1), so strings, arrays and nested ID-property groups
    are irrelevant here and skipped.
    """
    snapshot = {}
    if rig is None:
        return snapshot
    for prop_name in rig.keys():
        value = rig.get(prop_name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            snapshot[('object', None, prop_name)] = float(value)
    for pb in rig.pose.bones:
        for prop_name in pb.keys():
            value = pb.get(prop_name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                snapshot[('bone', pb.name, prop_name)] = float(value)
    return snapshot


def _changed_numeric_custom_props(before, after):
    """Returns the `('object'|'bone', name, prop_name)` keys whose
    value differs between two _snapshot_numeric_custom_props() results
    - including a property that exists in `after` but not `before`
    (created by the command that just ran) - i.e. whatever a just-run
    command actually touched. Order isn't meaningful; a command that
    happens to touch several properties at once gets all of them
    keyed, not just one.
    """
    changed = []
    for key, before_value in before.items():
        after_value = after.get(key)
        if after_value is None or after_value != before_value:
            changed.append(key)
    for key in after.keys() - before.keys():
        changed.append(key)
    return changed


def _keyframe_custom_prop_changes(rig, changed_keys, frame):
    """Keyframes every `('object'|'bone', name, prop_name)` entry in
    `changed_keys` on `rig` at `frame` - the properties
    _changed_numeric_custom_props() found had actually changed, not a
    single property resolved by parsing the command that changed them.

    Each property is keyframed independently, inside its own try/except
    - `keyframe_insert()` can both raise (e.g. the rig is linked/a
    library override, the action is otherwise locked, or the call
    happens somewhere Blender doesn't allow inserting right now) and
    simply return False on failure without raising, so both are treated
    as the same kind of failure here rather than letting one kind slip
    through unnoticed or crash the whole batch and abandon whatever
    hadn't been keyed yet.

    Returns `(keyed, failed)`: `keyed` is how many properties were
    actually keyed (0 with an empty `failed` means none of
    `changed_keys` still exists to key, or `changed_keys` was empty -
    "nothing to auto-key" rather than a failure); `failed` is a list of
    the `('object'|'bone', name, prop_name)` entries that did exist but
    couldn't be keyed, for the caller to report.
    """
    keyed = 0
    failed = []
    for scope, name, prop_name in changed_keys:
        if scope == 'bone':
            pb = rig.pose.bones.get(name)
            target = pb if (pb is not None and prop_name in pb) else None
        else:
            target = rig if prop_name in rig else None
        if target is None:
            # Property no longer there to key (removed by the same
            # command run, or the bone's gone) - not a keying failure,
            # just nothing left to do for this entry.
            continue

        try:
            inserted = target.keyframe_insert(data_path=f'["{prop_name}"]', frame=frame)
        except Exception:
            inserted = False

        if inserted:
            keyed += 1
        else:
            failed.append((scope, name, prop_name))

    if keyed:
        rig.update_tag(refresh={'DATA'})
    return keyed, failed



def _multi_action_ownership_error(settings, action, rig):
    """Validates BOTH ownership dimensions of a multi-command button
    against the current state, returning a user-facing error string, or
    None when it's safe to run:

      1. The action belongs to the active rig.
      2. The action belongs to the active panel.

    Both are decided by the button's OWNING PANEL, found by its stable id
    (see quick_action_owner_panel()): the rig is that panel's rig and
    the panel is compared as a row with the one active now, so no
    name string stored on the button can be stale, half-migrated or
    reused by a later panel and still pass. A button whose panel no
    longer exists is refused outright.

    Run immediately before a button's commands are exec()'d, so a
    scripted/stale call or an index that outlived a rig OR panel switch
    can't run against the wrong rig's bones or another panel's button.
    A legacy button (no panel_uid yet) is resolved by its stored names,
    and a blank armature_name there still resolves against the active rig.
    """
    owner = quick_action_owner_panel(
        settings, action,
        default_rig_name=rig.name if rig is not None else None,
    )
    if action.panel_uid and owner is None:
        return (
            f'"{action.name}" belongs to a panel that no longer exists '
            f'(it was deleted or replaced). Not run.'
        )

    # Dimension 1: rig.
    owner_rig = owner.armature_name if owner is not None else action.armature_name
    if owner_rig and (rig is None or rig.name != owner_rig):
        active_label = rig.name if rig is not None else "none"
        return (
            f'"{action.name}" belongs to rig "{owner_rig}", '
            f'but the active rig is "{active_label}". Not run.'
        )

    # Dimension 2: panel.
    if rig is None:
        # Only reachable for a legacy blank-armature button with no rig
        # at all; there are no panels to be scoped to.
        return None
    if owner is None:
        return (
            f'"{action.name}" is not assigned to any panel of rig '
            f'"{rig.name}". Not run.'
        )
    active_panel = _quick_active_panel(settings, rig.name)
    if active_panel is None or owner != active_panel:
        active_label = active_panel.name if active_panel is not None else "none"
        return (
            f'"{action.name}" belongs to panel "{owner.name}", '
            f'but the active panel is "{active_label}". Not run.'
        )
    return None


class RP_OT_quick_run_multi_action(Operator):
    bl_idname = "rp.quick_run_multi_action"
    bl_label = "Run"
    bl_description = (
        "Checks the selected bone(s) against each command's Control shape "
        "bones and runs whichever command matches"
    )
    bl_options = {'REGISTER', 'UNDO'}

    action_index: IntProperty()

    def execute(self, context):
        settings = context.scene.rp_quick_action_settings
        if not (0 <= self.action_index < len(settings.multi_actions)):
            return {'CANCELLED'}
        action = settings.multi_actions[self.action_index]

        from . import backend
        rig = backend.arm()

        # Resolve selection from the SAME source that decided this
        # button's enabled state, not an independent re-read. The
        # canvas gates "enabled" on Controller.selected_bones (see
        # Controller.refresh_quick_multi_action_enabled_states()) -
        # it's the addon's own kept-in-sync record of the current
        # selection, updated as part of the same refresh() pass that
        # also drives undo/toggle-state sync, and it's *not* the same
        # read as a fresh context.selected_pose_bones taken here,
        # inside an operator invoked from a Qt button callback outside
        # Blender's normal event loop. If those two ever disagree for
        # even one poll tick, a canvas click could execute against a
        # different selection than the one that made the button look
        # clickable - matching the wrong rule, or reporting "no match"
        # on a button that visibly looked enabled. Falls back to a
        # live context read only when there's no active picker
        # controller at all (pure N-panel use, or a stale/scripted
        # call), which is what decided that button's own enabled state
        # in the first place (see draw_multi_action()).
        controller = getattr(backend, "_ACTIVE_CONTROLLER", None)
        if controller is not None:
            selected_names = set(controller.selected_bones)
        else:
            selected_bones = getattr(context, "selected_pose_bones", None) or []
            selected_names = {b.name for b in selected_bones}

        rule, warning = _quick_find_matching_rule(selected_names, action.rules)
        if rule is None:
            # An ambiguous action is a configuration error the rigger has
            # to fix, not just "nothing selected" - report it as one.
            ambiguous = bool(warning) and warning.startswith(AMBIGUOUS_RULES_PREFIX)
            message = (
                f'"{action.name}": {warning}' if ambiguous
                else (warning or "No matching command")
            )
            # "Doesn't match" is often really "a rule's bone was renamed or
            # deleted" - say so rather than leave the user guessing. (Only
            # when `rig` is the rig this button belongs to; a stale call
            # against another rig gets the ownership error below instead.)
            if (
                not ambiguous and rig is not None
                and _multi_action_ownership_error(settings, action, rig) is None
            ):
                stale = quick_action_missing_bones(action, rig)
                if stale:
                    message += (
                        " Control bones missing on the rig (renamed or "
                        "deleted?) - " + quick_missing_bones_text(stale, "; ") + "."
                    )
            self.report({'ERROR' if ambiguous else 'WARNING'}, message)
            return {'CANCELLED'}

        # Ownership check, deliberately made immediately before anything
        # executes (not earlier): the action must belong to BOTH the
        # active rig and the active panel. A click on a visible button
        # always passes; this exists for scripted/stale calls and
        # indices that outlived a rig or panel switch, which would
        # otherwise exec() this button's commands in the wrong context.
        # Either failure cancels the run.
        ownership_error = _multi_action_ownership_error(settings, action, rig)
        if ownership_error is not None:
            self.report({'ERROR'}, ownership_error)
            return {'CANCELLED'}

        # Bone-reference validation of the whole action, also immediately
        # before anything executes:
        #   * The selection the match above used comes from the
        #     controller's poll-synced record, which can lag a rename/
        #     delete by up to one tick. A selected name the rig no longer
        #     has means the match was made against stale data - refuse
        #     rather than run a command for a selection that isn't real.
        #   * Rules that reference bones the rig doesn't have are not a
        #     reason to refuse a rule that DID match (the selection is
        #     real and a subset of it), but they're gathered now - before
        #     the command can change the rig - and reported after the run.
        bone_names = _rig_bone_names(rig)
        if bone_names is not None:
            stale_selection = sorted(selected_names - bone_names)
            if stale_selection:
                self.report(
                    {'ERROR'},
                    f'"{action.name}": selected bone(s) '
                    f'{", ".join(stale_selection)} no longer exist on rig '
                    f'"{rig.name}" (renamed or deleted). Not run.',
                )
                return {'CANCELLED'}
        missing_references = quick_action_missing_bones(action, rig)

        command = rule.command.strip()
        exec_globals = {"bpy": bpy, "context": context, "C": context, "D": bpy.data}
        # See RP_OT_quick_run_single_action's own comment for why `rig`
        # is exposed here - lets a command reference the current
        # armature generically (rig.pose.bones[...]) instead of
        # hardcoding a specific object name.
        exec_globals["rig"] = rig

        if action.action_type == 'TOGGLE':
            # Same convention as custom_actions.py's real Toggle
            # actions: the command is just the bone property reference
            # (no trailing "= value") - _toggle_assignment_code() below
            # builds that assignment structurally (see its own
            # docstring for why not string concatenation).

            # Derive the next state from the target's OWN current
            # value, not action.toggle_state's cached value - so a
            # click always reflects reality even if the target drifted
            # since the button was last synced (frame scrubbing, a
            # script, another button touching the same property,
            # etc.), rather than blindly inverting whatever the button
            # last happened to show. `raw_before` is read via
            # _read_quick_toggle_value() -> _eval_toggle_raw_value(),
            # which runs the command itself as a plain expression (the
            # same path sync_quick_toggle_states() already uses to
            # keep the switch's on-screen state honest) - never a
            # regex-parsed bone/property - so this works for whatever
            # expression the command happens to be, not just one
            # recognized `.pose.bones["Bone"]["Prop"]` shape.
            #
            # Falls back to the old blind flip (invert the cached
            # toggle_state) only when the target can't be read at all
            # yet - e.g. the property doesn't exist until this very
            # first run - since there's nothing real to derive from in
            # that case.
            #
            # `unreadable` is a unique sentinel, not None: None is also a
            # value a property can hold (an unset pointer, a dict entry),
            # and "holds None" must be refused below, not mistaken for
            # "doesn't exist yet" and overwritten.
            original_before = _read_quick_toggle_original(
                rig, command, unreadable=_TOGGLE_UNREADABLE
            )
            if (
                original_before is not _TOGGLE_UNREADABLE
                and not toggle_value_supported(original_before)
            ):
                # The property exists but holds None / a string / array /
                # ...: a Toggle can only switch a boolean, integer or float,
                # and writing 0/1 over anything else would retype it (and
                # its undo could not put it back). Refuse, and put the
                # switch the click already flipped back, as every failed
                # click does.
                self.report(
                    {'ERROR'},
                    f'"{action.name}": its property holds '
                    f'{_toggle_held_description(original_before)}, not a '
                    "number or boolean - a Toggle can't switch it without "
                    "changing its type. Not run.",
                )
                _sync_toggle_state_from_property(action, rig, command)
                _sync_quick_picker_ui()
                return {'CANCELLED'}
            if original_before is _TOGGLE_UNREADABLE:
                # Can't be read (e.g. the property doesn't exist until this
                # very first run). From here on None means exactly that:
                # a value that was read is a supported number/bool and is
                # never None.
                original_before = None
            # The numeric view, for deciding the next state. The ORIGINAL
            # (typed) value is what the new value's type is taken from.
            raw_before = float(original_before) if original_before is not None else None

            # Compute the prospective post-click value WITHOUT
            # committing it to action.toggle_state yet - only commit
            # (and keyframe) once the
            # command has actually run successfully, same reasoning as
            # the real operator.
            #
            # The next state comes from _toggle_click_target_state(): the
            # SAME reading of the value the switch's display uses, so a
            # click can never contradict what the switch shows (a
            # non-binary 0.5 is displayed OFF, so the click turns it ON -
            # it used to compute `not bool(0.5)` and write 0).
            prospective_toggle_state = _toggle_click_target_state(
                raw_before, action.toggle_state
            )
            # Remembered for the report after the click: the value the
            # property held wasn't a valid switch state (not 0/1).
            non_binary_before = (
                raw_before is not None
                and _toggle_state_from_value(raw_before) is None
            )
            # Written in the property's own type (bool/int/float), not a
            # bare 1/0: an int written over a float property (or a float
            # over an int/bool one) makes Blender retype the property.
            new_value = _toggle_value_for_state(prospective_toggle_state, original_before)
            exec_globals["value"] = new_value
            exec_globals["state"] = new_value

        # Only relevant when the timeline's own Auto-Key is on (see the
        # comment on the keyframe block below for why this check
        # exists at all) - captured BEFORE exec() so the block below
        # can diff against it to find out what the command actually
        # changed, without needing to parse the command text for a
        # target first.
        auto_key = context.scene.tool_settings.use_keyframe_insert_auto
        before_snapshot = (
            _snapshot_numeric_custom_props(rig)
            if action.action_type == 'TOGGLE' and auto_key and rig is not None
            else None
        )

        command_started = False
        try:
            _assert_command_allowed(command)
            command_started = True
            if action.action_type == 'TOGGLE':
                # Built structurally (see _toggle_assignment_code's own
                # docstring for why, not string-concatenated onto
                # `command`) - same try/except as any other command
                # error catches a command that isn't something
                # assignable too, not just a runtime failure.
                exec(_toggle_assignment_code(command), exec_globals)
            else:
                exec(command, exec_globals)
        except Exception as e:
            self.report({'ERROR'}, command_failure_message(e, command_started))
            if action.action_type == 'TOGGLE':
                # The click itself just confirmed this command is
                # broken (bad target, deleted bone/property, whatever
                # raised above). The switch on the picker canvas flips
                # itself the instant it is clicked, before this operator
                # runs, so it must always be put back - whatever the
                # property actually holds (OFF if it can't even be read,
                # same as sync_quick_toggle_states()'s own reset) - not
                # only when it was previously ON.
                _sync_toggle_state_from_property(action, rig, command)
                _sync_quick_picker_ui()
            return {'CANCELLED'}

        if action.action_type == 'TOGGLE':
            # Verify before committing anything: the assignment above not
            # raising only means Python accepted it. Let the dependency
            # graph settle first (a driver or constraint that pins the
            # property writes its result back during this update), then
            # read the command again and require the property to hold the
            # value that was just written. Only a confirmed match commits
            # the switch's look (action.toggle_state), the undo entry and
            # the auto-key below.
            context.view_layer.update()
            took_effect, actual_value = _toggle_assignment_took_effect(
                rig, command, new_value, context
            )
            if not took_effect:
                # No match: report it, and re-synchronize the switch from
                # the property's actual value. No undo entry and no
                # keyframe are made for a toggle that didn't happen.
                shown_state = _sync_toggle_state_from_property(action, rig, command)
                self.report(
                    {'ERROR' if actual_value is not None else 'WARNING'},
                    _toggle_verify_failure_message(
                        command, new_value, actual_value, shown_state
                    ),
                )
                for window in bpy.context.window_manager.windows:
                    for area in window.screen.areas:
                        area.tag_redraw()
                _sync_quick_picker_ui()
                return {'CANCELLED'}

            action.toggle_state = prospective_toggle_state
            # No picker undo entry for a toggle click: it changes Blender
            # data, which picker Ctrl+Z must not touch.
            type_note = (
                _toggle_type_change_note(new_value, rig, command, context)
                if original_before is not None else None
            )
            if type_note:
                self.report(
                    {'WARNING'},
                    f'"{action.name}": toggled, but {type_note} - something '
                    "(an RNA-defined property, a driver) coerced the write.",
                )
            if non_binary_before:
                # Not an error - the toggle did what the switch showed - but
                # the user should know the property wasn't 0/1 and was
                # normalized, rather than wonder why 0.5 became 1.
                shown = _format_toggle_value(raw_before)
                self.report(
                    {'WARNING'},
                    f'"{action.name}": its property was {shown}, not 0 or 1 - '
                    f"treated as Off, so it was turned On. Undo restores {shown}.",
                )

            # Only keyframes whatever this Toggle's command just
            # changed when the timeline's own Auto-Key is on - a
            # scripted property assignment (this exec() call) never
            # goes through either of the two places Blender itself
            # checks that setting (a UI widget's own edit-commit, or a
            # transform operator calling the hook explicitly), so
            # without this check here a Toggle click would leave a
            # keyframe regardless of Auto-Key, which doesn't match
            # what every other property edit in the file is doing
            # around it.
            if before_snapshot is not None:
                # One shared helper (also used by undo/redo): keys the
                # command's own target when it is a supported kind -
                # custom property, RNA property or array element, see the
                # Auto-Key block comment - else falls back to the numeric
                # custom-property diff, and says so when neither can key.
                keyed, failed, unsupported = _auto_key_toggle(
                    rig, command, context.scene.frame_current,
                    before_snapshot, context,
                )
                outcome = _auto_key_report("Toggle ran", command, keyed, failed, unsupported)
                if outcome is not None:
                    self.report({outcome[0]}, outcome[1])

        context.view_layer.update()
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        _sync_quick_picker_ui()

        if missing_references:
            self.report(
                {'WARNING'},
                f'"{action.name}" ran, but control bones are missing on the '
                "rig (renamed or deleted?), so those commands can't be "
                "triggered: " + quick_missing_bones_text(missing_references, "; ")
                + ". Remove or re-add them.",
            )
        self.report({'INFO'}, "Ran command")
        return {'FINISHED'}


# -----------------------------------------------------------------------------
# Operators - Section 2 (single-command buttons)
# -----------------------------------------------------------------------------

MALFORMED_SINGLE_PREFIX = "Malformed button"


def single_action_rule(action):
    """The ONE command a Single button runs, validated - replaces blindly
    taking action.rules[0], which silently ignored every other rule.

    Returns (rule, warning); rule is None (with a warning) when there is
    nothing to run or the button is malformed. Blank rules are ignored, so
    [blank, "cmd"] runs "cmd" (rules[0] would have run the blank and
    reported "No command set" while a real command sat unused). Two or
    more NON-blank commands are a conflict only the user can resolve: it is
    never resolved by picking the first. That warning starts with
    MALFORMED_SINGLE_PREFIX.
    """
    filled = [r for r in action.rules if r.command.strip()]
    if len(filled) > 1:
        return None, (
            f'{MALFORMED_SINGLE_PREFIX}: "{action.name}" holds {len(filled)} '
            "commands, but a Single button holds exactly one - only one "
            "could run and the rest would be silently ignored. Keep the "
            "one you want in the Quick panel. Not run."
        )
    if filled:
        return filled[0], None
    return None, "No command set on this button"


def single_action_problem(action):
    """Short UI text when `action` is malformed (2+ non-blank commands),
    else None. Same test single_action_rule() makes, so what the UI flags
    and what the operator refuses can't drift apart."""
    _rule, warning = single_action_rule(action)
    if warning and warning.startswith(MALFORMED_SINGLE_PREFIX):
        filled = sum(1 for r in action.rules if r.command.strip())
        return (
            f"Holds {filled} commands; a Single button holds exactly one. "
            "Open the Quick panel and keep the one you want."
        )
    return None


def normalize_single_action(action):
    """Lossless repair toward the one-rule invariant; True if it changed
    anything. Adds the missing slot to a button with none, and drops BLANK
    extra slots. It never deletes a command: with two or more non-blank
    commands it leaves the button alone (see single_action_rule(), which
    then refuses to run it)."""
    rules = action.rules
    if len(rules) == 0:
        rules.add()
        return True
    if len(rules) == 1:
        return False
    filled = [i for i, r in enumerate(rules) if r.command.strip()]
    if len(filled) > 1:
        return False
    keep = filled[0] if filled else 0
    for i in range(len(rules) - 1, -1, -1):
        if i != keep:
            rules.remove(i)
    return True


def _normalize_single_actions(settings):
    for action in settings.single_actions:
        normalize_single_action(action)


class RP_OT_quick_keep_single_rule(Operator):
    bl_idname = "rp.quick_keep_single_rule"
    bl_label = "Keep This Command"
    bl_description = (
        "A Single button holds exactly one command. Keep this one and "
        "remove the others"
    )
    bl_options = {'REGISTER', 'UNDO'}

    action_index: IntProperty()
    rule_index: IntProperty()

    def execute(self, context):
        settings = context.scene.rp_quick_action_settings
        if not (0 <= self.action_index < len(settings.single_actions)):
            return {'CANCELLED'}
        action = settings.single_actions[self.action_index]
        if not (0 <= self.rule_index < len(action.rules)):
            return {'CANCELLED'}
        for i in range(len(action.rules) - 1, -1, -1):
            if i != self.rule_index:
                action.rules.remove(i)
        _sync_quick_picker_ui()
        _update_blend_tracking()
        return {'FINISHED'}


class RP_OT_quick_add_single_action(Operator):
    bl_idname = "rp.quick_add_single_action"
    bl_label = "Add Button"
    bl_description = "Add a new button that holds exactly one command"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _file_is_unsaved():
            show_unsaved_file_popup(context)
            return {'CANCELLED'}
        settings = context.scene.rp_quick_action_settings
        count = len(settings.single_actions) + 1
        item = settings.single_actions.add()
        item.uid = uuid.uuid4().hex
        item.name = f"Button {count}"
        item.rules.add()  # the one and only command slot, same as the real Single mode
        settings.single_actions_index = len(settings.single_actions) - 1
        _sync_quick_picker_ui()
        _update_blend_tracking()
        return {'FINISHED'}


class RP_OT_quick_remove_single_action(Operator):
    bl_idname = "rp.quick_remove_single_action"
    bl_label = "Remove Button"
    bl_description = "Remove this button"
    bl_options = {'REGISTER', 'UNDO'}

    action_index: IntProperty()

    def execute(self, context):
        settings = context.scene.rp_quick_action_settings
        if 0 <= self.action_index < len(settings.single_actions):
            settings.single_actions.remove(self.action_index)
        _sync_quick_picker_ui()
        return {'FINISHED'}


class RP_OT_quick_rename_single_action(Operator):
    bl_idname = "rp.quick_rename_single_action"
    bl_label = "Rename"
    bl_description = "Rename the button"
    bl_options = {'REGISTER', 'UNDO'}

    action_index: IntProperty()
    new_name: StringProperty(name="Name")

    def invoke(self, context, event):
        settings = context.scene.rp_quick_action_settings
        if 0 <= self.action_index < len(settings.single_actions):
            self.new_name = settings.single_actions[self.action_index].name
        return context.window_manager.invoke_props_dialog(self, width=200)

    def draw(self, context):
        split = self.layout.split(factor=0.28)
        split.label(text="Name")
        split.prop(self, "new_name", text="")

    def execute(self, context):
        settings = context.scene.rp_quick_action_settings
        if 0 <= self.action_index < len(settings.single_actions):
            settings.single_actions[self.action_index].name = self.new_name
        for area in context.screen.areas:
            area.tag_redraw()
        _sync_quick_picker_ui()
        return {'FINISHED'}


class RP_OT_quick_run_single_action(Operator):
    bl_idname = "rp.quick_run_single_action"
    bl_label = "Run"
    bl_description = "Runs this button's one command"
    bl_options = {'REGISTER', 'UNDO'}

    action_index: IntProperty()

    def execute(self, context):
        settings = context.scene.rp_quick_action_settings
        if not (0 <= self.action_index < len(settings.single_actions)):
            return {'CANCELLED'}
        action = settings.single_actions[self.action_index]

        # Single buttons are always enabled and run with or without a
        # selection - nothing here (or in the N panel / picker) greys them
        # out. Their one command only ever sees `rig`/`context`, never a
        # selection, so there is nothing to wait for.
        from . import backend

        # Validated before anything runs: exactly one command or nothing.
        # (Used to be action.rules[0] with no check, silently ignoring any
        # other rule a migrated/restored/scripted button carried.)
        rule, warning = single_action_rule(action)
        if rule is None:
            malformed = bool(warning) and warning.startswith(MALFORMED_SINGLE_PREFIX)
            self.report({'ERROR' if malformed else 'WARNING'}, warning)
            return {'CANCELLED'}
        command = rule.command.strip()

        exec_globals = {"bpy": bpy, "context": context, "C": context, "D": bpy.data}

        rig = backend.arm()
        # Exposed so commands can be written generically against
        # whatever rig is currently active (e.g.
        # rig.pose.bones["Bone"]["Prop"]) instead of every command
        # having to hardcode a specific object name
        # (bpy.data.objects["SomeRigName"]...) - the latter breaks the
        # moment the same button is used on a differently-named rig,
        # which defeats the whole point of these buttons being
        # armature-independent. May be None if nothing's active; a
        # command that then references `rig` fails with a normal
        # Python error, reported below same as any other exec failure.
        exec_globals["rig"] = rig

        command_started = False
        try:
            _assert_command_allowed(command)
            command_started = True
            exec(command, exec_globals)
        except Exception as e:
            self.report({'ERROR'}, command_failure_message(e, command_started))
            return {'CANCELLED'}

        context.view_layer.update()
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()
        _sync_quick_picker_ui()

        self.report({'INFO'}, "Ran command")
        return {'FINISHED'}


def report_quick_invocation_failure(button_name, exc):
    """Surfaces an exception raised AROUND invoking a Quick button's
    operator from the picker window (ui/control_list.py) - the operator
    itself reports a failing COMMAND via self.report(), but nothing reports
    the things around it: the operator's poll failing, bpy.ops raising a
    RuntimeError, the 3D-view context override or the rig lookup raising.
    Those used to be swallowed by `except Exception: pass`, so the click
    just appeared to do nothing.

    Two channels, so a failure is neither lost nor shouted over the user's
    work: the FULL traceback goes to the system console (where Blender
    prints Python errors), and a one-line ERROR goes through
    rp.quick_report - the same Info-editor/status-bar channel the operators
    use - naming the button and the error. Never raises itself.
    Returns the one-line message.
    """
    import sys
    import traceback

    try:
        traceback.print_exception(type(exc), exc, exc.__traceback__)
    except Exception:
        pass

    # bpy.ops wraps a failure in a RuntimeError whose text can be a whole
    # traceback; the banner wants the line that says what actually went wrong.
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    detail = lines[-1] if lines else "no details"
    message = (
        f'Quick button "{button_name or "?"}" could not be run: '
        f"{type(exc).__name__}: {detail} (see the system console)"
    )
    try:
        bpy.ops.rp.quick_report(message=message, level='ERROR')
    except Exception:
        # Even the report channel is unavailable: the console is all that's
        # left, and the traceback above is already there.
        print(message, file=sys.stderr)
    return message


class RP_OT_quick_refresh_toggles(Operator):
    bl_idname = "rp.quick_refresh_toggles"
    bl_label = "Refresh Toggles"
    bl_description = (
        "Re-reads each Toggle's property and updates its switch. Use after a "
        "script or another tool changed a property without changing the "
        "selection or the frame (those are what normally refresh them)"
    )
    # No UNDO: it only brings the switches' display state in line with the
    # properties, it changes no rig data.
    bl_options = {'REGISTER'}

    def execute(self, context):
        try:
            changed = refresh_quick_toggle_states(context.scene)
        except Exception as e:
            self.report({'ERROR'}, f"Couldn't refresh the Toggles: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, "Toggles updated" if changed else "Toggles already up to date")
        return {'FINISHED'}


class RP_OT_quick_report(Operator):
    """Shows a message via self.report() - used by code that isn't
    itself inside an operator (e.g. controller code) so it can still show
    the same warning banner every other report in this file uses.
    """
    bl_idname = "rp.quick_report"
    bl_label = "Rig Picker Report"
    bl_options = {'INTERNAL'}

    message: StringProperty()
    level: EnumProperty(
        items=[
            ('INFO', "Info", ""),
            ('WARNING', "Warning", ""),
            ('ERROR', "Error", ""),
        ],
        default='WARNING',
    )

    def execute(self, context):
        self.report({self.level}, self.message)
        return {'CANCELLED'}


# -----------------------------------------------------------------------------
# Shared drawing helpers (used by both panels below)
# -----------------------------------------------------------------------------

# ---- shared rule-block drawing, styled exactly like the real panel's
# draw_rule_blocks() in custom_actions.py ----
def _draw_rule_blocks(layout, action_index, rules, op_prefix, with_bones=True, missing_by_rule=None, keep_rule_op=None):
    if not rules:
        layout.label(text="No commands are added yet", icon='INFO')
        return
    for i, rule in enumerate(rules):
        margin_row = layout.row()
        margin_row.separator(factor=0.3)
        rule_col = margin_row.column()
        rule_box = rule_col.box()
        margin_row.separator(factor=0.3)

        header = rule_box.row()
        label_text = "Command" if (not with_bones and len(rules) == 1) else f"Command {i + 1}"
        header.label(text=label_text, icon='SCRIPT')
        rule_missing = (missing_by_rule or {}).get(i)
        if rule_missing:
            # Visible even while the rule is collapsed.
            warn = header.row()
            warn.alert = True
            warn.label(text=f"{len(rule_missing)} missing", icon='ERROR')

        controls = header.row(align=True)
        if keep_rule_op:
            keep = controls.operator(keep_rule_op, text="Keep this")
            keep.action_index = action_index
            keep.rule_index = i
        if with_bones:
            remove_op = controls.operator("rp.quick_remove_rule", text="", icon='X', emboss=False)
            remove_op.action_index = action_index
            remove_op.rule_index = i
        arrow_icon = 'TRIA_DOWN' if rule.expanded else 'TRIA_RIGHT'
        controls.prop(rule, "expanded", text="", icon=arrow_icon, emboss=False)

        if not rule.expanded:
            continue

        rule_box.prop(rule, "command", text="")

        if with_bones:
            rule_box.label(text="Control shape bones:")
            row = rule_box.row()
            row.template_list(
                "RP_UL_control_bones", f"quick_action_{action_index}_rule_{i}_bones",
                rule, "bones",
                rule, "bones_index",
                rows=3,
            )
            side = row.column(align=True)
            add_op = side.operator("rp.quick_add_control_bone", icon='ADD', text="")
            add_op.action_index = action_index
            add_op.rule_index = i
            rem_op = side.operator("rp.quick_remove_control_bone", icon='REMOVE', text="")
            rem_op.action_index = action_index
            rem_op.rule_index = i


def _draw_multi_action(layout, action_index, action, selected_names):
    box = layout.box()
    header = box.row()

    left_section = header.row()
    left_section.alignment = 'LEFT'
    run_row = left_section.row()
    run_row.alignment = 'LEFT'
    # Grey the run button out (not the rename icon next to it) when
    # the current selection doesn't match any of this action's
    # rules - a visual cue before the click, using the exact same
    # _quick_find_matching_rule() the run operator itself checks,
    # so "looks clickable" and "is clickable" can't drift apart.
    run_row.enabled = _quick_find_matching_rule(selected_names, action.rules)[0] is not None
    if action.action_type == 'TOGGLE':
        run_op = run_row.operator(
            "rp.quick_run_multi_action", icon='NONE', text=action.name or "Toggle",
            depress=action.toggle_state,
        )
    else:
        run_op = run_row.operator("rp.quick_run_multi_action", icon='NONE', text=action.name or "Button")
    run_op.action_index = action_index

    rename_slot = left_section.row(align=True)
    rename_op = rename_slot.operator(
        "rp.quick_rename_multi_action", text="", icon_value=_edit_icon_id(), emboss=False
    )
    rename_op.action_index = action_index

    right_section = header.row(align=True)
    right_section.alignment = 'RIGHT'
    add_rule_op = right_section.operator("rp.quick_add_rule", text="", icon='ADD', emboss=False)
    add_rule_op.action_index = action_index
    remove_op = right_section.operator("rp.quick_remove_multi_action", text="", icon='X', emboss=False)
    remove_op.action_index = action_index
    arrow_icon = 'TRIA_DOWN' if action.expanded else 'TRIA_RIGHT'
    right_section.prop(action, "expanded", text="", icon=arrow_icon, emboss=False)

    # Validated against the live rig on every redraw (a name lookup per
    # listed bone - cheap), so a rename/delete is flagged without the user
    # having to guess why the button never enables. Shown even while the
    # button is collapsed.
    settings = getattr(bpy.context.scene, "rp_quick_action_settings", None)
    rig = _quick_active_rig(settings) if settings is not None else None
    missing = quick_action_missing_bones(action, rig)
    if missing:
        alert = box.column(align=True)
        alert.alert = True
        alert.label(text="Control bones missing on the rig:", icon='ERROR')
        for index, names in sorted(missing.items()):
            alert.label(text=f"Command {index + 1}: {', '.join(names)}")
        alert.label(text="Renamed or deleted? Remove or re-add them below.")

    if not action.expanded:
        return

    _draw_rule_blocks(
        box, action_index, action.rules, "quick_multi", with_bones=True,
        missing_by_rule=missing,
    )


def _draw_single_action(layout, action_index, action):
    box = layout.box()
    header = box.row()

    left_section = header.row()
    left_section.alignment = 'LEFT'
    run_row = left_section.row()
    run_row.alignment = 'LEFT'
    # A malformed button (2+ commands) is greyed out - the operator refuses
    # it too, via the same single_action_rule() - so "looks clickable" and
    # "is clickable" can't drift apart.
    problem = single_action_problem(action)
    run_row.enabled = problem is None
    run_op = run_row.operator("rp.quick_run_single_action", icon='NONE', text=action.name or "Button")
    run_op.action_index = action_index

    rename_slot = left_section.row(align=True)
    rename_op = rename_slot.operator(
        "rp.quick_rename_single_action", text="", icon_value=_edit_icon_id(), emboss=False
    )
    rename_op.action_index = action_index

    right_section = header.row(align=True)
    right_section.alignment = 'RIGHT'
    # No Add-command icon here - Single buttons only ever hold one
    # command slot, same as the real panel's SINGLE command_mode.
    remove_op = right_section.operator("rp.quick_remove_single_action", text="", icon='X', emboss=False)
    remove_op.action_index = action_index
    arrow_icon = 'TRIA_DOWN' if action.expanded else 'TRIA_RIGHT'
    right_section.prop(action, "expanded", text="", icon=arrow_icon, emboss=False)

    if problem:
        alert = box.column(align=True)
        alert.alert = True
        alert.label(text=problem, icon='ERROR')
        alert.label(text="Choose which command to keep:")
        # Shown even while the button is collapsed, with the choices
        # below it - the button can't run until one is picked.

    if not action.expanded and not problem:
        return

    _draw_rule_blocks(
        box, action_index, action.rules, "quick_single", with_bones=False,
        keep_rule_op="rp.quick_keep_single_rule" if problem else None,
    )


# -----------------------------------------------------------------------------
# Panels
# -----------------------------------------------------------------------------

class RP_PT_quick_manage_panels_panel(Panel):
    bl_label = "Manage Panels"
    bl_idname = "RP_PT_quick_manage_panels_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Rig Picker"
    bl_order = 1

    @classmethod
    def poll(cls, context):
        # Only makes sense while the picker window is actually open -
        # hidden again the moment it's closed (see is_picker_open()).
        from .ui.launcher import is_picker_open
        return is_picker_open()

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.rp_quick_action_settings

        if _quick_active_rig(settings) is None:
            layout.label(text="No active rig", icon='INFO')
            return

        row = layout.row()
        row.template_list(
            "RP_UL_quick_panels", "quick_panels_list",
            settings, "panels",
            settings, "panels_index",
            rows=3,
        )
        side = row.column(align=True)
        side.operator("rp.quick_add_panel", icon='ADD', text="")
        side.operator("rp.quick_remove_panel", icon='REMOVE', text="")


class RP_PT_quick_multi_actions_panel(Panel):
    bl_label = "Multi Command Buttons"
    bl_idname = "RP_PT_quick_multi_actions_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Rig Picker"
    bl_order = 2

    @classmethod
    def poll(cls, context):
        from .ui.launcher import is_picker_open
        return is_picker_open()

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.rp_quick_action_settings

        # Read-only Rig / Panel header. No dropdown here any more: the rig
        # follows the Blender viewport's active armature (and the Qt
        # picker's Rig combo), the panel follows Manage Panels' selection
        # (and the Qt picker's Panel combo) - all kept in step by
        # backend.poll_active_armature() / Controller.change_panel() - so
        # this just DISPLAYS the current pair. The buttons listed below
        # are exactly the ones belonging to it.
        # LIMIT: the Rig row is display-only on purpose - there's no
        # dropdown to pick a rig from this panel any more. Change rigs in
        # the viewport/Outliner or the picker's Rig combo; this label
        # follows within one poll interval (see _LAST_PANEL_BY_RIG's note).
        header_rig = _quick_active_rig(settings)
        header_rig_name = header_rig.name if header_rig is not None else None
        header_panel_name = _quick_active_panel_name(settings, header_rig_name)

        header = layout.column(align=True)
        rig_line = header.split(factor=0.25, align=True)
        rig_line.label(text="Rig")
        rig_line.label(text=header_rig_name or "None", icon='ARMATURE_DATA')
        panel_line = header.split(factor=0.25, align=True)
        panel_line.label(text="Panel")
        panel_line.label(text=header_panel_name or "None", icon='WINDOW')
        layout.separator()

        # A single selected bone or several (multi-select) both count
        # as "something is selected" - used below for per-rule bone
        # matching (see _draw_multi_action's run-button graying).
        selected_bones = getattr(context, "selected_pose_bones", None) or []
        selected_names = {b.name for b in selected_bones}

        add_row = layout.row()
        add_row.alignment = 'LEFT'
        add_row.prop(settings, "new_multi_type", text="")
        # Buttons are drawn on top of the panel's image, so with no image
        # there is nothing for a new one to appear on - Add stays disabled
        # until the panel has one (Capture View in the picker).
        panel_has_image = _panel_has_image(header_rig_name, header_panel_name)
        add_button_row = add_row.row()
        add_button_row.enabled = panel_has_image
        add_button_row.operator("rp.quick_add_multi_action", icon='ADD', text="Add")
        # Toggles follow the frame and the selection; this catches up after
        # a script changed a Toggle's property without either.
        add_row.operator("rp.quick_refresh_toggles", icon='FILE_REFRESH', text="")
        if not panel_has_image and header_rig_name and header_panel_name:
            hint = layout.row()
            hint.alignment = 'LEFT'
            hint.label(text="Add an image to this panel to enable Add", icon='INFO')

        # Multi-command buttons are scoped to whichever (rig, panel)
        # pair was active when they were added - only show the ones
        # that match both the dropdown's current rig AND Manage
        # Panels' current panel. A blank panel_name means this button
        # predates panel-scoping and hasn't been migrated for some
        # reason (shouldn't happen once _migrate_legacy_multi_action_panels()
        # has run, but treated as "this rig's first panel" rather than
        # silently hidden everywhere, matching that migration's own
        # fallback).
        rig = _quick_active_rig(settings)
        current_rig_name = rig.name if rig is not None else None
        current_panel_name = _quick_active_panel_name(settings, current_rig_name)

        visible_multi = [
            (i, action) for i, action in enumerate(settings.multi_actions)
            if _quick_multi_action_visible(
                settings, action, current_rig_name, current_panel_name
            )
        ]

        if not visible_multi:
            if current_rig_name is None:
                layout.label(text="No active rig", icon='INFO')
            else:
                layout.label(
                    text=f"No added buttons yet for {current_panel_name or 'this panel'}",
                    icon='INFO',
                )
        else:
            for i, action in visible_multi:
                _draw_multi_action(layout, i, action, selected_names)


class RP_PT_quick_single_actions_panel(Panel):
    bl_label = "Single Command Buttons"
    bl_idname = "RP_PT_quick_single_actions_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Rig Picker"
    bl_order = 3

    @classmethod
    def poll(cls, context):
        from .ui.launcher import is_picker_open
        return is_picker_open()

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        settings = scene.rp_quick_action_settings

        add_row2 = layout.row()
        add_row2.alignment = 'LEFT'
        add_row2.operator("rp.quick_add_single_action", icon='ADD', text="Add Button")

        if not settings.single_actions:
            layout.label(text="No added buttons yet", icon='INFO')
        else:
            for i, action in enumerate(settings.single_actions):
                _draw_single_action(layout, i, action)


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------

classes = (
    RP_ControlBoneItem,
    RP_UL_control_bones,
    RP_QuickPanel,
    RP_UL_quick_panels,
    RP_QuickMultiRule,
    RP_QuickMultiAction,
    RP_QuickSingleRule,
    RP_QuickSingleAction,
    RP_QuickActionSettings,
    RP_OT_quick_add_panel,
    RP_OT_quick_remove_panel,
    RP_OT_quick_add_multi_action,
    RP_OT_quick_remove_multi_action,
    RP_OT_quick_rename_multi_action,
    RP_OT_quick_add_rule,
    RP_OT_quick_remove_rule,
    RP_OT_quick_add_control_bone,
    RP_OT_quick_remove_control_bone,
    RP_OT_quick_run_multi_action,
    RP_OT_quick_add_single_action,
    RP_OT_quick_keep_single_rule,
    RP_OT_quick_remove_single_action,
    RP_OT_quick_rename_single_action,
    RP_OT_quick_run_single_action,
    RP_OT_quick_refresh_toggles,
    RP_OT_quick_report,
    RP_PT_quick_manage_panels_panel,
    RP_PT_quick_multi_actions_panel,
    RP_PT_quick_single_actions_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.rp_quick_action_settings = PointerProperty(type=RP_QuickActionSettings)

    pcoll = bpy.utils.previews.new()
    icons_dir = os.path.join(os.path.dirname(__file__), "icons")
    try:
        pcoll.load("edit_icon", os.path.join(icons_dir, "edit_icon.png"), 'IMAGE')
    except Exception:
        pass
    _icon_collections["main"] = pcoll

    # Keeps the open Rig Picker window's Quick action buttons in sync
    # after a native Blender undo/redo.
    subscribe_rig_name_messages()

    if _on_undo_redo not in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.append(_on_undo_redo)
    if _on_undo_redo not in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.append(_on_undo_redo)

    # Keeps any Quick TOGGLE action in sync with whatever custom
    # property its command references on every frame change - UI-state
    # only.
    if _on_frame_change_update_quick_toggles not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_on_frame_change_update_quick_toggles)

    # One-time-per-file migration for Section 1 buttons saved before
    # they became scoped to a (rig, panel) pair instead of just a rig -
    # see _migrate_legacy_multi_action_panels()'s docstring. Registered
    # for every FUTURE file open via load_post; for whichever file is
    # already open right now (enabling the addon, or a fresh Blender
    # session with it already enabled - load_post never fires for that
    # one), it has to run through a timer rather than being called
    # directly here: register() runs with bpy.data still a restricted
    # placeholder (Blender deliberately disallows touching real
    # datablocks - scenes included - from inside register()/
    # unregister() itself), so bpy.data.scenes below would raise
    # "'_RestrictData' object has no attribute 'scenes'" the moment
    # this addon is installed/enabled, before ever reaching a real
    # file. A one-shot timer with first_interval=0.0 runs on the very
    # next event loop tick instead, by which point that restriction has
    # already been lifted.
    # _on_load_post / _on_save_post are NOT registered here: the addon's
    # single lifecycle dispatcher (see __init__.py) calls them, in a fixed
    # order relative to the backend's handlers and the store swap.
    # Auto-save of the N-panel data: the startup restore handles the file
    # that is already open now; the tick (persistent, so it keeps running
    # across File > Open) stays idle until a restore has armed it.
    if not bpy.app.timers.is_registered(_startup_restore_once):
        bpy.app.timers.register(_startup_restore_once, first_interval=0.1)
    if not bpy.app.timers.is_registered(_quick_autosave_tick):
        bpy.app.timers.register(
            _quick_autosave_tick, first_interval=1.0, persistent=True
        )
    if not bpy.app.timers.is_registered(_track_current_file_once):
        bpy.app.timers.register(_track_current_file_once, first_interval=0.5)
    if not bpy.app.timers.is_registered(_migrate_legacy_multi_action_panels):
        bpy.app.timers.register(_migrate_legacy_multi_action_panels, first_interval=0.0)


_SCENE_DATA_KEY = "rp_quick_action_settings"


def _current_file_has_button_data():
    for scene in bpy.data.scenes:
        st = getattr(scene, _SCENE_DATA_KEY, None)
        if st is not None and (
            len(st.multi_actions) or len(st.single_actions) or len(st.panels)
        ):
            return True
    return False


def _update_blend_tracking(*args):
    """Remembers (in rig_picker_data.json) which .blend files on disk
    contain button data, so uninstalling the addon can clean them too -
    an addon can't scan the whole computer for .blend files, it can only
    clean the ones it knows about. Called after a save and after a file
    is opened. Does nothing for a file that was never saved."""
    try:
        path = bpy.data.filepath
        if not path:
            return
        from . import json_manager
        if _current_file_has_button_data():
            json_manager.track_blend_file(path)
        else:
            json_manager.untrack_blend_file(path)
    except Exception:
        import traceback
        traceback.print_exc()


def _track_current_file_once():
    """One-shot timer: load_post never fires for the file that is already
    open when the addon is enabled, so track it on the next tick (bpy.data
    is still restricted inside register())."""
    _update_blend_tracking()
    return None


# -----------------------------------------------------------------------------
# Auto-save: N-panel data -> the open file's .rigpicker/data.json
# -----------------------------------------------------------------------------
# The N-panel's panel list and Multi/Single command buttons live in the
# scene (Scene.rp_quick_action_settings), so on their own they only reach
# disk when the .blend itself is saved. This mirrors them into the open
# file's data.json (under "quick_actions") a moment after every change, and
# puts them back from there when the file is opened - so nothing the user
# adds, renames, edits or deletes in the N-panel waits for a manual save.
#
# How it works:
#   * A timer (_quick_autosave_tick) serializes the scene data every
#     _AUTOSAVE_INTERVAL seconds and compares a signature of it with what
#     was last stored. A change is written once it has stayed the same for
#     one more check (so a burst of edits is one write), or after
#     _AUTOSAVE_MAX_WAIT seconds at the latest. Native undo/redo and the
#     picker window's own actions are picked up the same way, because the
#     check looks at the data, not at who changed it.
#   * _restore_and_arm() applies the stored data to the scene when a file
#     is opened (load_post) or the addon is enabled with a file already
#     open, then starts watching.
#   * flush_autosave() writes any pending change immediately; the
#     lifecycle dispatcher calls it at load_pre, just before the open file
#     is replaced.
#
# Safety rules: nothing is written for a file whose button data was never
# restored (the stored identity must match the open file), a failed restore
# stops all writing so a half-applied scene can never overwrite the stored
# data, and a file that never had any buttons never gets a data folder
# created for it.
#
# LIMITS: (1) toggle_state follows the scene (see sync_quick_toggle_states)
# and is therefore not treated as a change by itself - it is saved along
# with the next real change. (2) Scenes are matched by name; a scene
# renamed without a save loses its stored data unless it is the only scene.
# (3) Blender's own undo history is unaffected: restoring happens at load,
# which clears it anyway.

_AUTOSAVE_INTERVAL = 0.4         # seconds between checks
_AUTOSAVE_MAX_WAIT = 3.0         # longest a change waits while edits keep arriving
_AUTOSAVE_RETRY_SECONDS = 30.0   # after a failed write, before trying again
_AUTOSAVE_PAYLOAD_VERSION = 1

# Follow the scene (a Toggle's value is read back from a custom property),
# so on their own they are not a change worth a write.
_AUTOSAVE_VOLATILE = ("toggle_state",)

# Hidden ID properties (not RNA properties, so introspection can't see
# them), per PropertyGroup type.
_AUTOSAVE_HIDDEN_KEYS = {"RP_QuickPanel": ("_uid", "_prev_name")}

# Properties that must be applied before the rest, per type. A panel's
# rig is set before its name so the name's update callback registers the
# panel under the right rig.
_AUTOSAVE_SET_FIRST = {"RP_QuickPanel": ("armature_name",)}

_autosave = {
    "enabled": False,    # off until the open file's stored data was restored
    "identity": None,    # (blend_key, folder) the scene data belongs to
    "saved_sig": None,   # signature of what the store holds
    "pending_sig": None, # signature seen at the previous check, if it was a change
    "dirty_since": 0.0,  # when the current change was first seen
    "retry_after": 0.0,  # no write attempts before this (after a failure)
}


def _serialize_group(obj):
    """A PropertyGroup as plain JSON-able data, read through RNA so a
    property added to these classes later is saved without touching this
    code. Pointers (active_rig follows the active armature) are skipped."""
    out = {}
    for prop in obj.bl_rna.properties:
        pid = prop.identifier
        if pid == "rna_type" or prop.type == 'POINTER':
            continue
        if prop.type == 'COLLECTION':
            out[pid] = [_serialize_group(item) for item in getattr(obj, pid)]
            continue
        value = getattr(obj, pid)
        if prop.type == 'ENUM' and getattr(prop, "is_enum_flag", False):
            value = sorted(value)
        out[pid] = value

    hidden = {}
    for key in _AUTOSAVE_HIDDEN_KEYS.get(obj.bl_rna.identifier, ()):
        value = obj.get(key)
        if value is not None:
            hidden[key] = value
    if hidden:
        out["_hidden"] = hidden
    return out


def _apply_group(obj, data):
    """Writes `data` (from _serialize_group) back into a PropertyGroup,
    replacing its collections. Properties missing from `data` keep their
    defaults, so data from an older or newer version still applies."""
    ident = obj.bl_rna.identifier

    for key, value in (data.get("_hidden") or {}).items():
        obj[key] = value
    if ident == "RP_QuickPanel":
        # A panel's name update callback treats "name differs from the
        # hidden _prev_name" as a rename and migrates stored layouts. The
        # name being restored is not a rename, so make the two agree first.
        obj["_prev_name"] = data.get("name", "")

    props = {p.identifier: p for p in obj.bl_rna.properties}
    first = [k for k in _AUTOSAVE_SET_FIRST.get(ident, ()) if k in props]
    order = first + [k for k in props if k not in first]

    for pid in order:
        prop = props[pid]
        if pid == "rna_type" or prop.type == 'POINTER':
            continue
        if prop.type == 'COLLECTION':
            collection = getattr(obj, pid)
            collection.clear()
            for item_data in data.get(pid) or []:
                _apply_group(collection.add(), item_data)
            continue
        if prop.is_readonly or pid not in data:
            continue
        setattr(obj, pid, data[pid])


def _autosave_scenes():
    """The scenes whose button data is stored: local ones (a linked
    library's scenes can't be edited) that carry the settings."""
    return [
        scene for scene in bpy.data.scenes
        if getattr(scene, "library", None) is None
        and getattr(scene, _SCENE_DATA_KEY, None) is not None
    ]


def _collect_payload():
    return {
        "version": _AUTOSAVE_PAYLOAD_VERSION,
        "scenes": {
            scene.name: _serialize_group(getattr(scene, _SCENE_DATA_KEY))
            for scene in _autosave_scenes()
        },
    }


def _strip_volatile(obj):
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in _AUTOSAVE_VOLATILE}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    return obj


def _signature(payload):
    import json
    return json.dumps(_strip_volatile(payload), sort_keys=True, default=str)


def _payload_is_empty(payload):
    return not any(
        entry.get("panels") or entry.get("multi_actions") or entry.get("single_actions")
        for entry in payload.get("scenes", {}).values()
    )


def _apply_payload(stored):
    """Applies stored N-panel data to the open file's scenes. Returns True
    if any scene was changed. Raises if applying fails midway - the caller
    must then stop auto-saving (see _restore_and_arm())."""
    global _SYNCING_QUICK_PANEL_DROPDOWN

    stored_scenes = stored.get("scenes") if isinstance(stored, dict) else None
    if not isinstance(stored_scenes, dict) or not stored_scenes:
        return False

    scenes = _autosave_scenes()
    applied = False
    for scene in scenes:
        entry = stored_scenes.get(scene.name)
        if entry is None and len(scenes) == 1 and len(stored_scenes) == 1:
            entry = next(iter(stored_scenes.values()))   # the only scene, renamed
        if not isinstance(entry, dict):
            continue

        settings = getattr(scene, _SCENE_DATA_KEY)
        if _signature({"s": _serialize_group(settings)}) == _signature({"s": entry}):
            continue                                      # already the same

        # panels_index's update callback would switch the picker's panel;
        # this is a restore, not a user pick.
        _SYNCING_QUICK_PANEL_DROPDOWN = True
        try:
            _apply_group(settings, entry)
            for name in ("panels", "multi_actions", "single_actions"):
                last = max(len(getattr(settings, name)) - 1, 0)
                if getattr(settings, name + "_index") > last:
                    setattr(settings, name + "_index", last)
        finally:
            _SYNCING_QUICK_PANEL_DROPDOWN = False
        applied = True
    return applied


def _restore_and_arm():
    """Brings the open file's scenes in line with its stored N-panel data,
    then starts watching for changes. Called at load_post and, for the file
    already open when the addon is enabled, from a startup timer."""
    from . import json_manager
    state = _autosave
    state["enabled"] = False
    forget_stripped()

    try:
        stored = json_manager.get_quick_actions()
    except Exception:
        stored = None
        import traceback
        traceback.print_exc()

    if stored is not None:
        try:
            if _apply_payload(stored):
                _PANEL_NAME_REGISTRY.clear()
                for panel in _all_scene_panels():
                    _PANEL_NAME_REGISTRY[_panel_uid(panel)] = (panel.armature_name, panel.name)
                _sync_quick_picker_ui()
        except Exception:
            # A half-applied scene must never be written over the stored
            # data, so stay switched off until the next file load.
            json_manager._report_error(
                "Rig Picker: couldn't restore the saved N-panel buttons "
                "for this file. They are untouched on disk, but changes "
                "to the N-panel will not be auto-saved until the file is "
                "opened again."
            )
            return

    state["identity"] = json_manager.current_identity()
    state["saved_sig"] = _signature(stored) if stored is not None else None
    state["pending_sig"] = None
    state["dirty_since"] = 0.0
    state["retry_after"] = 0.0
    state["enabled"] = True


def _write_payload(payload, sig):
    """Stores `payload`; True if what is on disk now matches it."""
    import time
    from . import json_manager
    state = _autosave

    if _payload_is_empty(payload) and json_manager.get_quick_actions() is None:
        # Nothing to store and nothing stored: don't create a data folder
        # for a file that never had any buttons.
        state["saved_sig"] = sig
        state["pending_sig"] = None
        return True

    if json_manager.save_quick_actions(payload):
        state["saved_sig"] = sig
        state["pending_sig"] = None
        return True

    # Refused or failed (already reported once by json_manager). Don't
    # retry on every check - that would repeat the message.
    state["retry_after"] = time.monotonic() + _AUTOSAVE_RETRY_SECONDS
    return False


def _autosave_ready():
    """True if it is safe to read the scene and store it: armed, bpy.data
    usable, and the open file is the one the scene data was restored for."""
    state = _autosave
    if not state["enabled"]:
        return False
    if _stripped["payload"] is not None:
        return False            # buttons are out of the scene for a save: it is NOT the real state
    try:
        bpy.data.scenes
    except AttributeError:
        return False            # restricted context while Blender starts up
    from . import json_manager
    return json_manager.current_identity() == state["identity"]


def _quick_autosave_tick():
    """Timer: saves N-panel changes shortly after they happen."""
    import time
    state = _autosave
    try:
        if not _autosave_ready() or _file_is_unsaved():
            return _AUTOSAVE_INTERVAL

        started = time.perf_counter()
        payload = _collect_payload()
        sig = _signature(payload)
        now = time.monotonic()

        if sig == state["saved_sig"]:
            state["pending_sig"] = None
        else:
            if state["pending_sig"] is None:
                state["dirty_since"] = now
            stable = sig == state["pending_sig"]
            state["pending_sig"] = sig
            if (stable or now - state["dirty_since"] >= _AUTOSAVE_MAX_WAIT) \
                    and now >= state["retry_after"]:
                _write_payload(payload, sig)

        # Back off if the data is big enough for the check itself to cost.
        return max(_AUTOSAVE_INTERVAL, (time.perf_counter() - started) * 20)
    except Exception:
        import traceback
        traceback.print_exc()
        return _AUTOSAVE_INTERVAL


def flush_autosave():
    """Writes a pending N-panel change right now. Called at load_pre (just
    before the open file is replaced) and when the addon is disabled."""
    try:
        if not _autosave_ready() or _file_is_unsaved():
            return
        payload = _collect_payload()
        sig = _signature(payload)
        if sig != _autosave["saved_sig"]:
            _write_payload(payload, sig)
    except Exception:
        import traceback
        traceback.print_exc()


def on_file_saved():
    """save_post: a first save or Save As put the data in a new folder (see
    json_manager.handle_file_saved()). The scene data now belongs to that
    location; re-read what it holds so the next check compares against it."""
    state = _autosave
    if not state["enabled"]:
        return
    try:
        from . import json_manager
        identity = json_manager.current_identity()
        if identity == state["identity"]:
            return
        stored = json_manager.get_quick_actions()
        state["identity"] = identity
        state["saved_sig"] = _signature(stored) if stored is not None else None
        state["pending_sig"] = None
        state["retry_after"] = 0.0
    except Exception:
        import traceback
        traceback.print_exc()


# ---------------------------------------------------------
# KEEPING THE BUTTONS OUT OF THE .BLEND
# ---------------------------------------------------------
# The N-panel buttons live in the file's .rigpicker folder (auto-saved
# above). So that the .blend itself doesn't ALSO carry them, they are taken
# out of the scene for the moment of writing the file and put back right
# after:
#
#   save_pre         - make sure the folder holds exactly what the scene
#                      holds (verified by reading it back); only then clear
#                      the scene's lists. If that can't be confirmed, the
#                      scene is left alone and the .blend keeps its copy -
#                      never lose data to make the file tidier.
#   save_post / _fail - put them back from the copy kept in memory (before
#                      anything else runs, so path re-pointing after a first
#                      save or Save As sees the real data).
#   timer            - the same restore a second later, in case neither
#                      handler ran (the save was interrupted).

_STRIP_RESTORE_SECONDS = 1.0
_stripped = {"payload": None, "identity": None}


def strip_before_save():
    """save_pre. See the section comment above."""
    _stripped["payload"] = None
    try:
        if not _autosave_ready():
            return
        from . import json_manager
        if json_manager.is_detached():
            return
        payload = _collect_payload()
        if _payload_is_empty(payload):
            return
        sig = _signature(payload)
        if sig != _autosave["saved_sig"] and not _write_payload(payload, sig):
            return
        if _signature(json_manager.get_quick_actions() or {}) != sig:
            _autosave["saved_sig"] = None      # the folder is behind: write again
            return

        global _SYNCING_QUICK_PANEL_DROPDOWN
        _stripped["payload"] = payload
        _stripped["identity"] = json_manager.current_identity()
        _SYNCING_QUICK_PANEL_DROPDOWN = True
        try:
            for scene in _autosave_scenes():
                settings = getattr(scene, _SCENE_DATA_KEY)
                for name in ("panels", "multi_actions", "single_actions"):
                    getattr(settings, name).clear()
        finally:
            _SYNCING_QUICK_PANEL_DROPDOWN = False

        if not bpy.app.timers.is_registered(_restore_after_save_timer):
            bpy.app.timers.register(
                _restore_after_save_timer,
                first_interval=_STRIP_RESTORE_SECONDS, persistent=True,
            )
    except Exception:
        import traceback
        traceback.print_exc()
        restore_after_save()      # never leave a half-cleared scene behind


def restore_after_save():
    """save_post / save_post_fail / timer: puts back what strip_before_save()
    took out. Does nothing if nothing was taken out."""
    payload = _stripped["payload"]
    _stripped["payload"] = None
    if payload is None:
        return
    try:
        _apply_payload(payload)
        _PANEL_NAME_REGISTRY.clear()
        for panel in _all_scene_panels():
            _PANEL_NAME_REGISTRY[_panel_uid(panel)] = (panel.armature_name, panel.name)
        _sync_quick_picker_ui()
    except Exception:
        import traceback
        traceback.print_exc()
        from . import json_manager
        json_manager._report_error(
            "Rig Picker: couldn't put the N-panel buttons back after saving. "
            "They are safe in this file's .rigpicker folder; reopen the file "
            "to get them back."
        )
        _autosave["enabled"] = False      # don't overwrite the folder with a partial scene


def _restore_after_save_timer():
    restore_after_save()
    return None


def forget_stripped():
    """A different file is being loaded: whatever was taken out belonged to
    the old one and is safe in its folder."""
    _stripped["payload"] = None


def _startup_restore_once():
    """One-shot timer: load_post never fires for the file that is already
    open when the addon is enabled, so restore its stored buttons on the
    next tick (bpy.data is restricted inside register())."""
    _restore_and_arm()
    _migrate_legacy_multi_action_panels()
    return None


# Runs in a separate, background Blender process (no UI, no addons) so
# other .blend files can be cleaned without opening them in this session.
_CLEANUP_SCRIPT = r'''
import bpy, json, os, shutil, sys, tempfile, traceback

KEY = "rp_quick_action_settings"
listing = sys.argv[sys.argv.index("--") + 1]
with open(listing, encoding="utf8") as f:
    paths = json.load(f)

log = []
try:
    bpy.context.preferences.filepaths.save_version = 0  # no backup of old data
except Exception:
    pass

for path in paths:
    try:
        bpy.ops.wm.open_mainfile(filepath=path, load_ui=False)
        hit = False
        for scene in bpy.data.scenes:
            if scene.library is not None:
                continue
            getter = getattr(scene, "bl_system_properties_get", None)
            if getter is not None:
                store = getter()
                if store is not None and KEY in store:
                    del store[KEY]
                    hit = True
            if KEY in scene.keys():
                del scene[KEY]
                hit = True
        if hit:
            bpy.ops.wm.save_mainfile()
            log.append("cleaned: " + path)
        else:
            log.append("nothing to clean: " + path)
    except Exception:
        log.append("FAILED: " + path + "\n" + traceback.format_exc())

try:
    with open(os.path.join(tempfile.gettempdir(), "rig_picker_uninstall_cleanup.log"),
              "w", encoding="utf8") as f:
        f.write("\n".join(log))
except Exception:
    pass
shutil.rmtree(os.path.dirname(listing), ignore_errors=True)
'''

_LAST_CLEANUP_PROCESS = None


def _write_cleanup_files(paths):
    import json
    import tempfile
    folder = tempfile.mkdtemp(prefix="rig_picker_cleanup_")
    script = os.path.join(folder, "cleanup.py")
    listing = os.path.join(folder, "paths.json")
    with open(script, "w", encoding="utf8") as f:
        f.write(_CLEANUP_SCRIPT)
    with open(listing, "w", encoding="utf8") as f:
        json.dump(list(paths), f)
    return script, listing


def _cleanup_command(script, listing):
    """Command line for the background cleanup process, or None if this
    Blender has no executable to start."""
    exe = bpy.app.binary_path
    if not exe or not os.path.exists(exe):
        return None
    return [exe, "-b", "--factory-startup", "--python", script, "--", listing]


def _clean_other_files():
    """Removes the button data from every OTHER .blend file this addon
    knows contains some (see _update_blend_tracking()), by running a
    background Blender over them. The currently open file was already
    handled in memory. Detached, so Blender's UI doesn't wait for it.

    LIMITS: only files the addon recorded are cleaned - a file that has
    button data but was never opened/saved with the addon enabled isn't
    known. Missing/moved files are skipped. A file that is open in another
    Blender window can be overwritten by that window's next save. Each
    cleaned file is re-saved by THIS Blender version.
    """
    global _LAST_CLEANUP_PROCESS
    import subprocess
    from . import json_manager

    try:
        current = bpy.data.filepath
        current_key = os.path.normcase(os.path.abspath(current)) if current else None

        paths = []
        for path in json_manager.get_tracked_blend_files():
            if not os.path.isfile(path):
                continue
            if current_key and os.path.normcase(os.path.abspath(path)) == current_key:
                continue
            paths.append(path)

        json_manager.clear_tracked_blend_files()

        if not paths:
            return

        script, listing = _write_cleanup_files(paths)
        command = _cleanup_command(script, listing)
        if command is None:
            return
        kwargs = {}
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
        else:
            kwargs["start_new_session"] = True
        _LAST_CLEANUP_PROCESS = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **kwargs,
        )
    except Exception:
        import traceback
        traceback.print_exc()


def _save_cleaned_file():
    """Saves the open .blend right after the uninstall purge, so the
    removal reaches the file on disk without the user having to press
    Ctrl+S. This is a normal save of the whole file (anything else that
    was unsaved in it is saved too).

    LIMITS: a file that has never been saved (no path yet) can't be
    saved from here - the data is only removed in memory for it. Other
    .blend files aren't touched. Failure here is non-fatal: the data is
    still removed in memory and a normal save will persist it.
    """
    prefs = None
    old_versions = None
    try:
        if bpy.data.filepath:
            # Blender's "Save Versions" would otherwise rename the previous
            # on-disk file (still containing the data) to <name>.blend1.
            try:
                prefs = bpy.context.preferences.filepaths
                old_versions = prefs.save_version
                prefs.save_version = 0
            except Exception:
                prefs = None
            bpy.ops.wm.save_mainfile()
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        if prefs is not None and old_versions is not None:
            try:
                prefs.save_version = old_versions
            except Exception:
                pass


def _remove_scene_property(scene, key):
    """Deletes a stored property from a scene. Blender 5 keeps properties
    created with bpy.props in a separate "system properties" store that
    scene.keys() and del scene[key] don't see, so both stores are tried
    (older Blender versions only have the plain one)."""
    removed = False
    getter = getattr(scene, "bl_system_properties_get", None)
    if getter is not None:
        try:
            store = getter()
            if store is not None and key in store:
                del store[key]
                removed = True
        except Exception:
            pass
    try:
        if key in scene.keys():
            del scene[key]
            removed = True
    except Exception:
        pass
    return removed


def _purge_scene_data():
    """Removes every scene's Quick-action data from the CURRENTLY OPEN
    .blend (the Multi/Single command buttons and the panel list).

    LIMITS: (1) Only the file that is open right now is cleaned - other
    .blend files still carry their own copy until they're opened with
    this addon removed and re-saved. (2) The change is in memory only:
    the .blend must be saved afterwards for the data to disappear from
    disk. (3) Only called from unregister() when _is_uninstalling() is true. (4) bpy.data can be
    a restricted placeholder while Blender is starting up; in that case
    this does nothing rather than raising. rig_picker_data.json (rig
    picker layouts) is intentionally NOT touched here.
    """
    try:
        scenes = bpy.data.scenes
    except AttributeError:
        return

    for scene in scenes:
        # Clear the collections through the live registered property
        # first. Do NOT test `key in scene.keys()` - registered
        # properties don't show up in keys(), so that check was always
        # False and silently skipped the wipe.
        try:
            settings = getattr(scene, _SCENE_DATA_KEY, None)
            if settings is not None:
                for name in ("multi_actions", "single_actions", "panels"):
                    getattr(settings, name).clear()
        except Exception:
            import traceback
            traceback.print_exc()

        # Then drop the stored property itself, so nothing is left behind
        # as an orphaned property once it is unregistered.
        try:
            _remove_scene_property(scene, _SCENE_DATA_KEY)
        except Exception:
            import traceback
            traceback.print_exc()

    _PANEL_NAME_REGISTRY.clear()
    _PENDING_PANEL_DELETIONS.clear()
    _PENDING_PANEL_HISTORY.clear()
    _SAVED_PANEL_DELETIONS.clear()


# Blender operators that delete an addon from disk. Each one disables the
# addon first (which is what calls unregister() below), so while one of
# them is executing it is still on the Python call stack.
_UNINSTALL_OPERATOR_IDNAMES = {
    "preferences.addon_remove",
    "PREFERENCES_OT_addon_remove",
    "extensions.package_uninstall",
    "extensions.package_uninstall_marked",
    "extensions.package_uninstall_system",
    "EXTENSIONS_OT_package_uninstall",
    "EXTENSIONS_OT_package_uninstall_marked",
    "EXTENSIONS_OT_package_uninstall_system",
}


def _is_uninstalling():
    """True only while Blender's own Remove/Uninstall operator is running
    (that operator disables the addon, which calls unregister()). Plain
    disable, Reload Scripts and quitting Blender call unregister() too,
    but without one of these operators on the stack, so they return False.

    LIMIT: this works by looking at the Python call stack, so it depends
    on Blender's operators still being structured that way. If a future
    Blender version changes it, this fails SAFE: it returns False and no
    data is deleted (rather than deleting data on a plain disable).
    """
    import sys
    try:
        frame = sys._getframe(1)
        depth = 0
        while frame is not None and depth < 60:
            owner = frame.f_locals.get("self")
            if owner is not None:
                # A running operator reports its CLASS name as bl_idname
                # (e.g. "PREFERENCES_OT_addon_remove"), not the
                # "preferences.addon_remove" form - so check both.
                names = (
                    getattr(owner, "bl_idname", None),
                    type(owner).__name__,
                )
                if any(n in _UNINSTALL_OPERATOR_IDNAMES for n in names):
                    return True
            frame = frame.f_back
            depth += 1
    except Exception:
        pass
    return False


def unregister():
    unsubscribe_rig_name_messages()

    # One-shot startup timers: if the addon is removed before they have
    # fired, they must not run afterwards (_track_current_file_once would
    # write the registry file again, right after an uninstall deleted it).
    for timer_fn in (
        _track_current_file_once,
        _migrate_legacy_multi_action_panels,
        _startup_restore_once,
    ):
        if bpy.app.timers.is_registered(timer_fn):
            bpy.app.timers.unregister(timer_fn)

    # Auto-save of the N-panel data stops with the addon. A pending change
    # is written first - except when the addon is being uninstalled, where
    # the data is about to be deleted anyway and must not be re-created.
    if not _is_uninstalling():
        flush_autosave()
    restore_after_save()                  # never leave the scene cleared
    if bpy.app.timers.is_registered(_restore_after_save_timer):
        bpy.app.timers.unregister(_restore_after_save_timer)
    _autosave["enabled"] = False
    if bpy.app.timers.is_registered(_quick_autosave_tick):
        bpy.app.timers.unregister(_quick_autosave_tick)

    if _on_frame_change_update_quick_toggles in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.remove(_on_frame_change_update_quick_toggles)

    if _on_undo_redo in bpy.app.handlers.undo_post:
        bpy.app.handlers.undo_post.remove(_on_undo_redo)
    if _on_undo_redo in bpy.app.handlers.redo_post:
        bpy.app.handlers.redo_post.remove(_on_undo_redo)

    for pcoll in _icon_collections.values():
        bpy.utils.previews.remove(pcoll)
    _icon_collections.clear()

    # Wipe this addon's Quick-action data (Multi/Single buttons, their
    # rules, the Manage Panels list) out of every scene in the open .blend
    # BEFORE the property is unregistered. Once it's unregistered, Blender
    # keeps the leftover data in the scene as an orphaned raw ID property
    # that is saved into the .blend forever. All handlers/timers that
    # could touch (and silently re-create) the settings were removed
    # above, and nothing runs between this call and the del below.
    # Only when the user is actually UNINSTALLING the addon (Preferences >
    # Add-ons > Remove). Merely disabling it, Reload Scripts, or quitting
    # Blender leaves the scene data alone.
    if _is_uninstalling():
        _purge_scene_data()
        _save_cleaned_file()
        _clean_other_files()

    if hasattr(bpy.types.Scene, "rp_quick_action_settings"):
        del bpy.types.Scene.rp_quick_action_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
