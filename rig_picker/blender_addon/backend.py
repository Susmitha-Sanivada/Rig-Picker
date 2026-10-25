import os
import re
import tempfile
import bpy

# Global cache for the active armature reference
_CACHED_ARM: bpy.types.Object | None = None

# Name of the armature _CACHED_ARM refers to. Change detection is done by
# name, not object identity - Blender can hand back a fresh Python
# wrapper/evaluated-copy instance for the logically same armature between
# checks, and comparing by identity would treat that as a "different"
# armature and trigger a needless reload.
_CACHED_ARM_NAME: str | None = None

# Name of the armature last actually handed to Controller.load_armature()
# - i.e. what the Qt picker's own UI currently reflects. Change detection
# for driving that reload (see poll_active_armature() below) is done
# against THIS, never against _CACHED_ARM_NAME above, even though the two
# often agree: arm() (below) updates _CACHED_ARM_NAME purely as its own
# memoization, independent of whether the Qt UI has been told about that
# rig yet - and arm() gets called from places that run far more often
# than this module's own poll (N-panel draw() methods, in
# custom_actions_quick.py, redraw on nearly every viewport interaction,
# well under poll_active_armature()'s 0.15s interval). If
# poll_active_armature() compared against _CACHED_ARM_NAME instead, one
# of those other callers could - and in practice reliably did - race it:
# arm() quietly adopts the newly-active rig into _CACHED_ARM_NAME the
# moment some N-panel redraws (often before the next poll tick even
# runs), so by the time poll_active_armature() checked, _CACHED_ARM_NAME
# already matched the new rig and "is_new_armature" came back False -
# the Qt picker's Rig dropdown, canvas and everything else just silently
# never updated, even though the rig genuinely had changed. Keeping a
# separate variable that only this function's own logic ever writes
# closes that race: it changes if and only if
# Controller.load_armature() has actually been called for that rig.
_QT_SYNCED_ARM_NAME: str | None = None

# Mode the cached armature was in as of the last poll. Used to detect an
# Object Mode -> Pose Mode transition (however it happened - the mode
# dropdown, Ctrl+Tab, tabbing in the viewport, not just via this addon's
# own buttons) so leftover pb.select flags from rig generation/editing
# can be cleared exactly once on entry, instead of silently feeding into
# Add Selected as if the user had actually selected those bones.
_CACHED_ARM_MODE: str | None = None

# Name of the armature _CACHED_ARM_MODE / _LAST_SELECTED_BONES currently
# describe. Written ONLY by poll_active_armature(): _CACHED_ARM_NAME above
# is also written by arm() and select_and_activate_armature(), which can
# adopt a newly-active rig before the next poll tick runs - comparing
# against it made the poll think it had already processed that rig and
# skip the one-time mode/selection reset for it.
_MODE_TRACKED_ARM_NAME: str | None = None

# Set by ensure_pose_mode() right before it actually switches a rig from
# Object Mode into Pose Mode on this addon's own behalf (e.g. clicking a
# control on a rig that was still in Object Mode). Tells the very next
# poll_active_armature() tick's "Object Mode -> Pose Mode: reset
# selection once" logic below that this particular transition was caused
# by a picker click that ALREADY made its own deliberate selection right
# after switching modes - not a bare mode switch (Tab in the viewport,
# Rigify generation, etc.) potentially carrying over stale pb.select
# leftovers - so that selection must be left alone rather than wiped a
# moment later.
_SUPPRESS_NEXT_POSE_ENTRY_SELECTION_RESET = False

_ACTIVE_CONTROLLER = None
_ACTIVE_WINDOW = None

# (window pointer, area pointer) of the 3D viewport the user last interacted
# with (clicked, orbited, panned, zoomed). Recorded by RP_OT_TrackViewport,
# which sits on the 3D View keymap and passes every event straight through.
# Capture View uses it to pick WHICH viewport to photograph. Pointers, not
# bpy objects: an Area wrapper can dangle after the layout changes, whereas
# a pointer is just compared against the areas that exist right now.
_LAST_VIEW3D = None

# Names of the pose bones that were selected the last time we checked.
# Used to detect selections made directly in the 3D viewport (rather than
# by clicking a control in the picker UI) so the UI can be kept in sync.
_LAST_SELECTED_BONES: frozenset[str] = frozenset()

# How often (seconds) to check whether a different armature has become
# active. Selecting an object in the viewport doesn't always run
# depsgraph_update_post - that handler is meant for actual data changes
# (transforms, mode switches, etc.) and isn't a reliable signal for a pure
# active-object/selection change. A lightweight poll sidesteps that
# entirely and is imperceptible to the user at this interval.
_POLL_INTERVAL = 0.15


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def is_rig_visible(obj):
    """True if `obj` is a selectable, visible armature in the current view
    layer - i.e. NOT hidden via the eye icon / H key (hide_get()), the
    monitor icon (hide_viewport), or a hidden parent collection
    (visible_get()). Hidden rigs are left out of every rig dropdown (the Qt
    picker's and the N-panel's) and can't be the picker's current rig.
    """
    try:
        if obj is None or obj.type != 'ARMATURE':
            return False
        if obj.name not in bpy.context.view_layer.objects:
            return False
        return not (obj.hide_get() or obj.hide_viewport or not obj.visible_get())
    except Exception:
        return False


def first_visible_rig():
    """The first (name-sorted, matching the Rig dropdown's order) visible
    armature in the current view layer, or None."""
    rigs = sorted(
        (o for o in bpy.context.view_layer.objects if is_rig_visible(o)),
        key=lambda o: o.name,
    )
    return rigs[0] if rigs else None


def run_native_undo_redo(redo=False):
    """Runs Blender's own Ctrl+Z / Ctrl+Shift+Z, safely, on behalf of the Qt
    picker.

    NOT called directly from the Qt key handler: undo/redo frees and
    rebuilds datablocks and fires undo_post/redo_post handlers that rebuild
    picker widgets - doing that while the Qt event that triggered it is
    still on the stack is a native-crash risk. It is deferred to a one-shot
    bpy.app.timers callback (runs outside any Qt event) and given a real
    window/area/region context, since ed.undo polls against one and the
    floating picker has none of its own.
    """
    def _contexts():
        """Every window/screen/area/region combination worth trying, the
        3D viewport of each window first. ed.undo's poll only passes with a
        usable window context, and which window that is isn't something to
        guess (the first one can be the wrong one)."""
        wm = bpy.context.window_manager
        for window in wm.windows:
            screen = window.screen
            areas = sorted(screen.areas, key=lambda a: a.type != 'VIEW_3D')
            for area in areas:
                region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                ov = {"window": window, "screen": screen, "area": area}
                if region is not None:
                    ov["region"] = region
                yield ov
            yield {"window": window, "screen": screen}

    def _do():
        last_error = None
        try:
            for override in _contexts():
                try:
                    with bpy.context.temp_override(**override):
                        if redo:
                            bpy.ops.ed.redo()
                        else:
                            bpy.ops.ed.undo()
                    return None  # one-shot; done
                except RuntimeError as e:
                    last_error = e  # poll failed for this context: try next
        except Exception:
            import traceback
            traceback.print_exc()
        if last_error is not None:
            print(f"Rig Picker: Blender {'redo' if redo else 'undo'} "
                  f"was not possible in any window ({last_error}).")
        return None  # one-shot

    bpy.app.timers.register(_do, first_interval=0.0)


def arm() -> bpy.types.Object | None:
    """Returns the current armature.

    The cached bpy Object is NEVER trusted as-is: undo/redo (and file
    reload) free and rebuild datablocks, and a Python reference held across
    that can dangle - touching it can crash Blender outright rather than
    raise ReferenceError. So the cache is re-resolved by NAME from
    bpy.data.objects on every call (a hash lookup - cheap) and replaced
    with the live object.
    """
    global _CACHED_ARM, _CACHED_ARM_NAME

    if _CACHED_ARM_NAME:
        live = bpy.data.objects.get(_CACHED_ARM_NAME)
        if live is not None and live.type == 'ARMATURE':
            _CACHED_ARM = live
        else:
            _CACHED_ARM = None
            _CACHED_ARM_NAME = None
    else:
        _CACHED_ARM = None

    if _CACHED_ARM is None:
        obj = bpy.context.active_object
        if obj and obj.type == 'ARMATURE':
            _CACHED_ARM = obj
            _CACHED_ARM_NAME = obj.name

    return _CACHED_ARM


def visible_arm():
    """arm(), unless that rig is hidden in the viewport - hidden rigs are
    never offered in the picker, so open/reload on the first visible rig
    instead (or None if there isn't one)."""
    rig = arm()
    if rig is not None and not is_rig_visible(rig):
        return first_visible_rig()
    return rig


def select_and_activate_armature(rig):
    """Makes `rig` the sole selected object and the active object, the
    same object-level side effect Controller.change_armature() applies
    before it goes on to do its Qt-side reload. Used when the Quick
    Actions panel's own rig dropdown (RP_QuickActionSettings.active_rig,
    see custom_actions_quick.py) is changed while the Qt picker window
    isn't open - there's no controller/picker state to reload in that
    case, but the active armature should still actually switch, exactly
    like it would if the Qt combo had done it.
    """
    view_layer = bpy.context.view_layer
    if rig.name not in view_layer.objects:
        return

    for obj in view_layer.objects:
        if obj != rig and obj.select_get():
            obj.select_set(False)

    view_layer.objects.active = rig
    rig.select_set(True)

    global _CACHED_ARM, _CACHED_ARM_NAME
    _CACHED_ARM = rig
    _CACHED_ARM_NAME = rig.name


def on_load_pre(dummy=None):
    """Settle half of File > Open / New / Revert, called by the lifecycle
    dispatcher (see __init__.py) while the OUTGOING file is still open:
    lets the open picker's controller settle its pending undo-snapshot
    images and armature deletions against the outgoing file's store. See
    Controller.settle_before_file_change()."""
    if _ACTIVE_CONTROLLER is not None:
        try:
            _ACTIVE_CONTROLLER.settle_before_file_change()
        except Exception:
            import traceback
            traceback.print_exc()


def _live_window():
    """The open picker window if it still exists, else None."""
    if _ACTIVE_WINDOW is None:
        return None
    try:
        import shiboken6
        return _ACTIVE_WINDOW if shiboken6.isValid(_ACTIVE_WINDOW) else None
    except Exception:
        return None


def flush_window_geometry():
    """load_pre: writes the picker window's size/position to the OUTGOING
    file's store and stops its debounce timers. See
    RigPickerWindow.flush_geometry()."""
    window = _live_window()
    if window is not None:
        window.flush_geometry()


def repoint_paths(old_folder, new_folder):
    """save_post: the data folder moved/was copied - re-point the open
    picker's in-memory image paths. See Controller.repoint_paths()."""
    if _ACTIVE_CONTROLLER is not None:
        _ACTIVE_CONTROLLER.repoint_paths(old_folder, new_folder)


def apply_window_geometry():
    """load_post: applies the NEW file's saved geometry to the open picker
    window (a closed window does this itself when it is shown again). See
    RigPickerWindow.apply_saved_geometry()."""
    window = _live_window()
    if window is None:
        return
    from . import json_manager
    if json_manager.is_window_bound():
        window.apply_saved_geometry()


def on_load_post(dummy=None):
    """File > Open / New / Revert. Drops every cached reference into the
    file that was just replaced (they'd otherwise be stale or freed) and
    tells the open picker, if any, to reset WITHOUT deleting anything from
    any store - see Controller.on_file_loaded(). Called by the lifecycle
    dispatcher (see __init__.py), after the store has been swapped to the
    new file."""
    global _CACHED_ARM, _CACHED_ARM_NAME, _LAST_SELECTED_BONES
    global _CACHED_ARM_MODE, _MODE_TRACKED_ARM_NAME, _QT_SYNCED_ARM_NAME
    global _SUPPRESS_NEXT_POSE_ENTRY_SELECTION_RESET, _LAST_VIEW3D

    _LAST_VIEW3D = None
    _CACHED_ARM = None
    _CACHED_ARM_NAME = None
    _LAST_SELECTED_BONES = frozenset()
    _CACHED_ARM_MODE = None
    _MODE_TRACKED_ARM_NAME = None
    _QT_SYNCED_ARM_NAME = None
    _SUPPRESS_NEXT_POSE_ENTRY_SELECTION_RESET = False

    if _ACTIVE_CONTROLLER is not None:
        try:
            _ACTIVE_CONTROLLER.on_file_loaded()
        except Exception:
            import traceback
            traceback.print_exc()


_POLL_ERROR_LAST_PRINTED = {}
POLL_ERROR_REPEAT_SECONDS = 30.0


def _poll_step(label, fn, *args):
    """Runs one step of the poll tick, swallowing (but reporting) any
    exception. The identical error is printed at most once per
    POLL_ERROR_REPEAT_SECONDS so a persistent failure doesn't flood the
    console every 0.15s tick."""
    import time
    import traceback
    try:
        fn(*args)
    except Exception:
        text = traceback.format_exc()
        now = time.monotonic()
        last = _POLL_ERROR_LAST_PRINTED.get((label, text))
        if last is None or now - last >= POLL_ERROR_REPEAT_SECONDS:
            _POLL_ERROR_LAST_PRINTED[(label, text)] = now
            print(f"Rig Picker: poll step '{label}' failed:\n{text}")


def poll_active_armature():
    """Runs every _POLL_INTERVAL seconds via bpy.app.timers.

    Detects when a different armature has become active (in either Object
    Mode or Pose Mode) and reloads the picker for it. Also detects when the
    set of *selected pose bones* changes - e.g. the user clicking a control
    directly in the 3D viewport instead of in the picker UI - and pushes
    that selection into the Qt UI so the two stay in sync.

    Registered with persistent=True in register(), so it keeps running
    across File > Open without needing to be re-registered.

    Unlike depsgraph_update_post, a timer callback isn't a restricted
    context, so the reload can happen directly here - no need to defer it
    another tick.
    """
    global _CACHED_ARM, _CACHED_ARM_NAME, _LAST_SELECTED_BONES, _CACHED_ARM_MODE
    global _SUPPRESS_NEXT_POSE_ENTRY_SELECTION_RESET, _QT_SYNCED_ARM_NAME
    global _MODE_TRACKED_ARM_NAME

    try:
        # Run every tick, independent of the active-object branches below.
        # Those only ever call _sync_armature_combo_items() indirectly via
        # load_armature(), and only when a *different* armature becomes the
        # newly active object - so a rig getting deleted was missed
        # whenever nothing else became active afterward (active_object is
        # None or a non-armature), and also when some other, non-active
        # armature was the one deleted while the current one stayed active.
        # Calling it unconditionally here catches both; it's cheap when
        # nothing changed (one set comparison, early return).
        if _ACTIVE_CONTROLLER is not None:
            try:
                _ACTIVE_CONTROLLER._sync_armature_combo_items()
            except Exception:
                import traceback
                traceback.print_exc()

        # Same "run every tick, cheap when nothing changed" reasoning as
        # the Rig-combo sync just above - keeps the Qt picker's Panel
        # dropdown in step with Scene.rp_quick_action_settings.panels
        # (the Manage Panels N-panel section's add/rename/remove list)
        # without needing the picker window to be reopened.
        #
        # Deliberately called further down this function, AFTER
        # _ensure_default_panel_for_rig() runs below - not here - so a
        # rig with no panels yet (freshly active, or its last panel
        # just got deleted) gets its default "Panel 1" created and
        # synced to the combo within the SAME tick. Calling it here
        # would sync against the old, still-empty panel list first,
        # flashing "No Panel" in the Qt combo for one full poll
        # interval until the following tick finally picked up the
        # panel _ensure_default_panel_for_rig() had, by then, already
        # created.

        # Also run every tick, unconditionally: toggling a bone
        # collection's visibility (the eye icon in the Armature
        # Properties panel) doesn't fire any signal or operator this
        # addon's other hooks would ever see - polling for it here is
        # the only way a control whose bone just got hidden/shown
        # actually gets disabled/re-enabled in step with it, instead of
        # staying clickable (or stuck disabled) until some unrelated
        # action happens to call refresh().
        if _ACTIVE_CONTROLLER is not None:
            try:
                _ACTIVE_CONTROLLER.refresh_bone_collection_visibility_states()
            except Exception:
                import traceback
                traceback.print_exc()

        # Same every-tick reasoning: renaming/deleting a bone fires nothing
        # this addon sees, yet it can strand a Multi button's Control shape
        # bone names. Cheap (a set comparison) unless the names changed.
        if _ACTIVE_CONTROLLER is not None:
            try:
                _ACTIVE_CONTROLLER.refresh_quick_multi_action_bone_validity()
            except Exception:
                import traceback
                traceback.print_exc()

        obj = getattr(bpy.context, "active_object", None)
        if not obj and hasattr(bpy.context, "view_layer"):
            obj = bpy.context.view_layer.objects.active

        # A hidden rig is not offered anywhere in the UI, so it must not
        # stay the picker's "current" rig either. Blender keeps a hidden
        # object as view_layer.objects.active, which would otherwise make
        # this tick reload it straight back into the picker. Follow the
        # first visible rig instead - the same one the Controller falls
        # back to when it drops the hidden rig from its dropdown - or treat
        # it as "no active rig" if none is visible. Deliberately READ-ONLY
        # with respect to Blender: changing the active object / selection /
        # mode from a timer callback can crash Blender natively, so only
        # the picker's own state follows the fallback.
        if obj and obj.type == 'ARMATURE' and not is_rig_visible(obj):
            obj = first_visible_rig()

        if obj and obj.type == 'ARMATURE':
            # Whether THIS armature is one we haven't seen as active
            # before, judged purely by identity (this poll tick's obj
            # against what _CACHED_ARM_NAME was left holding at the END
            # of the previous tick) - independent of whether the Qt
            # picker has been told about it yet. This is what drives the
            # one-time mode/selection reset further below. Deliberately
            # a SEPARATE check from is_new_to_qt just underneath: see
            # that variable's comment for why conflating the two used to
            # wipe pose-bone selection continuously while the picker was
            # closed.
            is_newly_seen_armature = obj.name != _MODE_TRACKED_ARM_NAME
            _MODE_TRACKED_ARM_NAME = obj.name

            # Compared against _QT_SYNCED_ARM_NAME, not _CACHED_ARM_NAME -
            # see that variable's own comment above for why: arm() (called
            # from N-panel draw() methods elsewhere) can silently update
            # _CACHED_ARM_NAME to this same rig well before this poll tick
            # ever runs, which would make this check come back False and
            # skip load_armature() entirely even though the Qt UI was
            # never actually told about the change.
            #
            # This ONLY gates whether the Qt picker gets reloaded below -
            # it must NOT also gate the mode/selection reset further down
            # (is_newly_seen_armature, above, does that instead). While
            # the picker window is closed, _ACTIVE_CONTROLLER is None, so
            # _QT_SYNCED_ARM_NAME is never updated (see the inner
            # "if _ACTIVE_CONTROLLER is not None" block below) - is_new_to_qt
            # therefore comes back True on EVERY tick for as long as the
            # picker stays closed, by design, so the picker still gets a
            # real load_armature() the moment it's reopened. This used to
            # ALSO be the variable gating _CACHED_ARM_MODE's reset to
            # None below, which - being "always True while closed" -
            # made the Object -> Pose Mode check further down fire on
            # every single tick instead of once: any armature left in
            # Pose Mode with the picker closed had its entire bone
            # selection wiped roughly every _POLL_INTERVAL seconds,
            # making ordinary viewport posing unusable unless the picker
            # happened to be open.
            is_new_to_qt = obj.name != _QT_SYNCED_ARM_NAME

            _CACHED_ARM = obj
            _CACHED_ARM_NAME = obj.name

            if is_newly_seen_armature:
                # New armature - forget the previous armature's selection
                # so a stale comparison doesn't suppress the first sync.
                _LAST_SELECTED_BONES = frozenset()

                # Also forget the previous armature's MODE. Without this,
                # a brand-new armature first seen already in Pose Mode
                # (e.g. mid rig-generation, or the poll catching it right
                # after Tab) would inherit the old armature's leftover
                # _CACHED_ARM_MODE - if that happened to already be
                # 'POSE', the Object -> Pose check below would see
                # 'POSE' -> 'POSE' and skip the one-time selection reset
                # entirely, even though this armature has never been
                # checked before.
                _CACHED_ARM_MODE = None

            if is_new_to_qt and _ACTIVE_CONTROLLER is not None:
                try:
                    _ACTIVE_CONTROLLER.load_armature(obj)
                    # Only mark this rig as synced to the Qt UI once
                    # load_armature() has actually been called for it -
                    # if _ACTIVE_CONTROLLER is None (picker window
                    # closed) there's nothing to be "in sync", and
                    # leaving _QT_SYNCED_ARM_NAME unset means the
                    # picker still gets this rig loaded for real the
                    # moment it's opened (see main_window.py's own
                    # load_armature(backend.arm()) call on open) rather
                    # than this poll having already (wrongly) marked
                    # it as handled.
                    _QT_SYNCED_ARM_NAME = obj.name
                except Exception:
                    import traceback
                    traceback.print_exc()

            # ----------------------------------------------------
            # Object Mode -> Pose Mode: reset selection once.
            #
            # Freshly generated/edited rigs commonly carry over
            # pb.select = True on bones the user never actually clicked
            # (e.g. Rigify leaves internal bones selected as a side
            # effect of generation). That stale selection is invisible
            # in Object Mode, then silently feeds Add Selected the
            # moment Pose Mode is entered. Clearing it exactly on the
            # Object -> Pose transition (not on every poll, and not on
            # Pose -> Pose) means it only fires once per switch, and
            # leaves any selection the user makes afterwards untouched.
            # A brand-new armature (is_newly_seen_armature) is treated the
            # same way as an Object -> Pose transition below, since
            # _CACHED_ARM_MODE is None for it and obj.mode could
            # already be 'POSE' the first time this armature is seen.
            # ----------------------------------------------------
            entered_pose_mode = (
                obj.mode == 'POSE'
                and _CACHED_ARM_MODE in ('OBJECT', None)
            )

            if entered_pose_mode and obj.pose is not None:
                if _SUPPRESS_NEXT_POSE_ENTRY_SELECTION_RESET:
                    # This Object -> Pose transition was caused by this
                    # addon's own ensure_pose_mode() - e.g. clicking a
                    # control on a rig that was still in Object Mode,
                    # which switches it to Pose Mode and selects that
                    # control's bone in the same click. That selection
                    # is exactly what the user just asked for, not
                    # Rigify/generation leftovers, so skip the wipe this
                    # one time. Without this, the very next poll tick
                    # after such a click cleared pb.select right back
                    # off again a moment later - the control would flash
                    # selected and then immediately deselect.
                    pass
                else:
                    for pb in obj.pose.bones:
                        # Blender 5.0 moved pose-bone selection from
                        # Bone.select (bone.select, now removed) to
                        # PoseBone.select directly.
                        pb.select = False
                    obj.data.bones.active = None

            _SUPPRESS_NEXT_POSE_ENTRY_SELECTION_RESET = False
            _CACHED_ARM_MODE = obj.mode

            # ----------------------------------------------------
            # Detect pose-bone selection made in the 3D viewport and
            # reflect it in the picker UI.
            #
            # Deliberately NOT "pb.select for pb in obj.pose.bones":
            # toggling a bone collection's visibility off/on doesn't
            # clear the raw pb.select flag on the bones inside it, so
            # a bone left selected before its collection was hidden
            # stays "selected" by that raw flag even while hidden -
            # and a scan over every pose bone would then pick it back
            # up (alongside whatever was actually just selected, e.g.
            # via Select All) the moment its collection is unhidden
            # again, syncing the picker to a mix of real and stale
            # selection. context.selected_pose_bones is the same
            # visibility-aware list Blender's own operators (Select
            # All included) read and write, so it only ever reflects
            # bones that are genuinely selected AND visible right now.
            # ----------------------------------------------------
            if obj.pose is not None:
                current_selected = frozenset(
                    pb.name
                    for pb in (bpy.context.selected_pose_bones or [])
                    if pb.id_data == obj and is_bone_collection_visible(pb)
                )

                if current_selected != _LAST_SELECTED_BONES:
                    _LAST_SELECTED_BONES = current_selected

                    if _ACTIVE_CONTROLLER is not None:
                        try:
                            _ACTIVE_CONTROLLER.sync_selection_from_blender(
                                current_selected
                            )
                        except Exception:
                            import traceback
                            traceback.print_exc()
        else:
            # No active armature - nothing to compare selection against.
            _LAST_SELECTED_BONES = frozenset()
            _CACHED_ARM_MODE = None
            _SUPPRESS_NEXT_POSE_ENTRY_SELECTION_RESET = False
            _MODE_TRACKED_ARM_NAME = None

            # _CACHED_ARM is documented (see arm()'s own comment) as
            # "only ever reassigned when the active object changes" -
            # but this branch never actually did that: it left the
            # previous armature cached here even after the real active
            # object became something else entirely (a camera, a mesh,
            # nothing). arm() would then keep handing out that stale
            # rig to everything downstream (the Qt picker's initial
            # load, Manage Panels, anywhere else that calls it) as if
            # it were still active - the three-way desync where the
            # viewport has a camera selected, the N-panel's Rig
            # dropdown correctly goes blank (it reads `obj` directly,
            # not this cache), but the Qt picker keeps showing the old
            # rig regardless.
            if _CACHED_ARM is not None:
                _CACHED_ARM = None
                _CACHED_ARM_NAME = None

            # Same reset, for the same reason, as the _CACHED_ARM_NAME
            # clear just above - nothing is active for the Qt UI to be
            # "in sync" with anymore, so forget what it was last synced
            # to as well. Unconditional (not gated on _CACHED_ARM having
            # been set): _QT_SYNCED_ARM_NAME and _CACHED_ARM_NAME can
            # legitimately disagree (that's the whole point of keeping
            # them separate - see _QT_SYNCED_ARM_NAME's own comment), so
            # this can't just piggyback on the `if _CACHED_ARM is not
            # None` check above.
            _QT_SYNCED_ARM_NAME = None

            # Same three-way sync as the armature branch above, for the
            # opposite transition: clear the Qt picker's own loaded rig
            # (and, in turn, its combo's displayed selection - see
            # Controller._sync_armature_combo_selection(), called every
            # tick by _sync_armature_combo_items() near the top of this
            # function) so it doesn't keep showing whatever rig was
            # active a moment ago once the real active object isn't an
            # armature anymore.
            if _ACTIVE_CONTROLLER is not None and _ACTIVE_CONTROLLER.armature_name is not None:
                try:
                    _ACTIVE_CONTROLLER.load_armature(None)

                    # load_armature() -> refresh() only *schedules* the
                    # now-empty canvas's repaint via canvas.update() -
                    # same "sits pending until some unrelated Qt event
                    # forces it through" issue PickerComboBox._apply_
                    # style()'s comment (main_window.py) describes.
                    # _sync_armature_combo_items() above already flushes
                    # its OWN combo changes with a processEvents() call,
                    # but that ran before this load_armature(None) ever
                    # scheduled the canvas clear - flush again here so
                    # the picker visibly goes blank immediately, not up
                    # to one ~0.15s poll tick later.
                    # Direct repaint of the picker window only - NOT
                    # QApplication.processEvents(), which dispatches every
                    # queued Qt event from inside this timer and crashed
                    # Blender natively (see Controller.
                    # _repaint_armature_combo()).
                    if _ACTIVE_WINDOW is not None:
                        _ACTIVE_WINDOW.repaint()
                except Exception:
                    import traceback
                    traceback.print_exc()

        # Keep the Quick Actions panel's own rig dropdown
        # (Scene.rp_quick_action_settings.active_rig - see
        # custom_actions_quick.py) in step with whatever the active
        # armature actually is now, the same way the top of this
        # function keeps the Qt picker's own combo in sync - covers a
        # switch made from the Qt combo itself, a viewport/Outliner
        # pick, or that dropdown's own update callback
        # (_on_quick_active_rig_changed()) actually switching rigs.
        # Runs every tick regardless of whether the armature branch
        # above matched, so switching to "no armature active" clears
        # the dropdown too, not just leaves it pointing at whatever rig
        # was last active. Guarded via custom_actions_quick.
        # _SYNCING_QUICK_RIG_DROPDOWN so setting it here doesn't
        # re-trigger that update callback and try to switch the active
        # rig a second time - see that guard's own comment.
        try:
            from . import custom_actions_quick
            scene = bpy.context.scene
            quick_settings = getattr(scene, "rp_quick_action_settings", None) if scene else None
            if quick_settings is not None:
                current_rig = obj if (obj and obj.type == 'ARMATURE') else None
                if quick_settings.active_rig != current_rig:
                    custom_actions_quick._SYNCING_QUICK_RIG_DROPDOWN = True
                    try:
                        quick_settings.active_rig = current_rig
                    finally:
                        custom_actions_quick._SYNCING_QUICK_RIG_DROPDOWN = False
        except Exception:
            import traceback
            traceback.print_exc()

        # Same "cheap, safe to write to scene data from a timer tick"
        # reasoning as the Rig-dropdown sync just above - makes sure
        # whichever rig is currently active always has at least one
        # panel of its own in the Manage Panels N-panel section's list
        # (and, in turn, the Qt picker's own Panel dropdown, via
        # sync_panel_combo_items()): a rig that's never had one added,
        # or whose last one just got deleted, gets a default one added
        # back automatically, scoped to IT specifically - each rig
        # keeps its own independent set of panels (Face, Body, etc.),
        # the same way multi-command buttons are already scoped per
        # rig. Also corrects panels_index back onto this rig's own
        # panels if it's still pointing at whatever rig was active
        # before. Deliberately NOT done from that panel's own draw() or
        # from register()/load_post - both of those can run in a
        # restricted context that disallows writing to ID data
        # (collection.add()), where a timer tick like this one does
        # not. No-ops entirely when no rig is active - an empty panel
        # list is the correct state then, not something to paper over.
        # Each step is guarded on its own: one failing step (a persistent
        # error would otherwise repeat every tick) must not also stop the
        # rest - in particular rename tracking and default-panel creation -
        # from running, and its traceback is printed once, not 6x a second.
        from . import custom_actions_quick
        scene = bpy.context.scene
        quick_settings = getattr(scene, "rp_quick_action_settings", None) if scene else None
        if quick_settings is not None:
            _poll_step("purge expired panel deletions",
                       custom_actions_quick._purge_expired_panel_deletions, quick_settings)
            _poll_step("track rig renames", custom_actions_quick.track_rig_renames)

            def _ensure_default_for_active_rig():
                rig_for_panels = custom_actions_quick._quick_active_rig(quick_settings)
                if rig_for_panels is not None:
                    custom_actions_quick._ensure_default_panel_for_rig(
                        quick_settings, rig_for_panels.name
                    )

            _poll_step("ensure default panel", _ensure_default_for_active_rig)

        # Now that this tick's _ensure_default_panel_for_rig() call
        # above has already created the current rig's default panel if
        # it needed one, sync the Qt combo against that up-to-date
        # panel list - see the comment where this call used to sit,
        # near the top of this function, for why the ordering matters.
        if _ACTIVE_CONTROLLER is not None:
            try:
                _ACTIVE_CONTROLLER.sync_panel_combo_items()
            except Exception:
                import traceback
                traceback.print_exc()

    except Exception:
        import traceback
        traceback.print_exc()

    return _POLL_INTERVAL  # returning a number reschedules the timer


def ensure_pose(context, rig):
    """Utility to make rig active safely."""
    try:
        if rig and context.view_layer.objects.active != rig:
            context.view_layer.objects.active = rig
    except Exception:
        pass


def ensure_pose_mode(context, rig):
    """Switches the rig into Pose Mode if it isn't already.

    pose.* operators (reveal, hide, select_all...) poll() against
    context.active_object.mode == 'POSE'. Overriding "active_object" in
    temp_override does not change the object's actual mode, so without this
    the operators fail with "context is incorrect" whenever the picker is
    used while the rig is in Object Mode.
    """
    global _SUPPRESS_NEXT_POSE_ENTRY_SELECTION_RESET

    if rig and rig.mode != 'POSE':
        # Tells the next poll_active_armature() tick that whatever
        # selection ends up in place right after this mode switch (e.g.
        # RP_OT_Select setting pb.select on the clicked bone, a couple
        # of lines below this same call) was made deliberately by this
        # addon, not carried over from Object Mode - see
        # _SUPPRESS_NEXT_POSE_ENTRY_SELECTION_RESET's own comment.
        _SUPPRESS_NEXT_POSE_ENTRY_SELECTION_RESET = True
        bpy.ops.object.mode_set(mode='POSE')


def refresh_3d_view(context):
    """Triggers an instant GPU redraw of all 3D Viewport areas to fix click latency."""
    context.view_layer.update()
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def get_3d_override(context, rig):
    """Generates a context override for running pose operators safely."""
    if not rig:
        return None

    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        return {
                            "window": window,
                            "screen": window.screen,
                            "area": area,
                            "region": region,
                            "scene": context.scene,
                            "view_layer": context.view_layer,
                            "active_object": rig,
                            "object": rig,
                        }
    return None


_MIRROR_RE = re.compile(r"^(.*)(left|right|l|r)(\.\d+)?$", re.IGNORECASE)


def mirror_name(name):
    """Returns the mirrored bone name for names ending in a left/right
    side token - l, L, left, Left, LEFT, r, R, right, Right, RIGHT -
    including Blender's numbered-duplicate suffix (e.g.
    "forearm_tweak.L.001" -> "forearm_tweak.R.001",
    "hand.Left" -> "hand.Right"). Returns None if the name has no
    left/right side at all.

    Only the side token itself is matched/stripped - whatever comes
    right before it (a ".", a "_", nothing at all) is left untouched
    as part of the base and carried straight through into the
    mirrored name, rather than assuming a "." separator like the
    older version of this function did. That's deliberate: it's what
    makes "hand.L"/"hand.R", "hand_L"/"hand_R" and even delimiter-less
    "HandLeft"/"HandRight" all resolve to the same shared base ("hand.",
    "hand_", "Hand") and mirror correctly, instead of only the
    dot-suffix convention. The trade-off is that an ordinary word that
    happens to end in "l" or "r" (e.g. "curl", "hair") also matches
    and gets "mirrored" (to "curr", "hail") - there's no delimiter left
    to tell a real side token apart from a word's own last letter.

    Matching itself is case-insensitive (re.IGNORECASE on _MIRROR_RE),
    so "hand.l" and "hand.L" both match the same way - but the
    mirrored name this returns keeps the SAME case style as the side
    token it's replacing, rather than always normalizing to one
    canonical style: a single-letter token stays a single letter
    (l<->r, L<->R) and a word token stays a word (left<->right,
    Left<->Right, LEFT<->RIGHT). That's what lets a rig's own naming
    convention - whichever of the two forms it actually uses - round-
    trip unchanged instead of every mirrored name being silently
    rewritten into a different style than the bone it was derived
    from.
    """
    match = _MIRROR_RE.match(name)
    if not match:
        return None

    base, side, suffix = match.groups()
    is_single_letter = len(side) == 1
    is_left = side[0].lower() == "l"

    if is_single_letter:
        mirrored = "r" if is_left else "l"
    else:
        mirrored = "right" if is_left else "left"

    if side.isupper():
        mirrored = mirrored.upper()
    elif side[0].isupper():
        mirrored = mirrored.capitalize()
    # else: side is lowercase (or an odd mixed case we can't infer a
    # style from) - mirrored is already lowercase by default above.

    return f"{base}{mirrored}{suffix or ''}"


def is_bone_collection_visible(pb):
    """True if pb's bone collection(s) are visible right now - the one
    thing bpy determines a bone's real on-screen visibility by that a
    raw pb.select/pb.bone.hide check alone can't see.

    Bone Collections (Blender 4.0+) replaced the old armature layers
    system, and collection visibility (bone.collections[i].is_visible)
    is a completely separate flag from the individual bone.hide toggle -
    Blender does not sync them. So a bone sitting in a hidden collection
    can still carry pb.select = True (left over from before the
    collection was hidden, or from a rig-generation tool like Rigify
    that leaves internal bones selected as a side effect) and pb.bone.
    hide = False, and yet not actually be visible or selectable in the
    viewport at all. Every place in this addon that treats "selected"
    or "should this bone be acted on" as meaning something in the
    viewport has to check this, not just the raw flags - this is the
    shared version of the check add_selected() established first.

    A bone with no collections at all is never hidden by this check,
    since collections only ever restrict visibility for bones actually
    assigned to one.
    """
    collections = list(pb.bone.collections)
    return not collections or any(c.is_visible for c in collections)


def control_dimensions(size, shape):
    """Returns the (width, height) bounding box of a control in image
    space for the given size/shape. Mirrors CircleControl's own sizing
    logic (see circle_control.py: set_display_scale), so callers that
    need to reason about a control's footprint - e.g. to keep its center
    fixed when size/shape change - stay in sync with what actually gets
    drawn."""
    height = size
    if shape == "RECTANGLE":
        width = max(14, round(size * 1.6))
    else:
        width = size
    return width, height


# ---------------------------------------------------------
# SELECT
# ---------------------------------------------------------

class RP_OT_Select(bpy.types.Operator):
    bl_idname = "rp.select"
    bl_label = "Select Control"

    bone_name: bpy.props.StringProperty()
    shift: bpy.props.BoolProperty(default=False)

    def execute(self, context):
        rig = arm()
        if not rig:
            return {'CANCELLED'}

        override = get_3d_override(context, rig)
        if not override:
            return {'CANCELLED'}

        with context.temp_override(**override):
            context.view_layer.objects.active = rig
            ensure_pose_mode(context, rig)

            target_pb = rig.pose.bones.get(self.bone_name)
            if not target_pb:
                return {'CANCELLED'}

            # Determine which bones should be selected
            if self.shift:
                # Not a raw "if pb.select" scan: that would also pull
                # in any bone left with a stale pb.select = True from
                # before its collection was hidden (see
                # is_bone_collection_visible()'s docstring) - shift-
                # clicking a control would then silently add that
                # invisible bone into the restored selection alongside
                # the one actually clicked.
                selected_bones = [
                    pb.name for pb in rig.pose.bones
                    if pb.select and is_bone_collection_visible(pb)
                ]

                if self.bone_name in selected_bones:
                    selected_bones.remove(self.bone_name)
                else:
                    selected_bones.append(self.bone_name)
            else:
                selected_bones = [self.bone_name]

            # Reveal everything first
            bpy.ops.pose.reveal(select=False)

            # Clear selection
            bpy.ops.pose.select_all(action='DESELECT')

            # Restore desired selection
            for name in selected_bones:
                rig.pose.bones[name].select = True

            # Active bone
            if selected_bones:
                active_name = (
                    self.bone_name
                    if self.bone_name in selected_bones
                    else selected_bones[-1]
                )
                rig.data.bones.active = rig.pose.bones[active_name].bone
            else:
                rig.data.bones.active = None

            # Hide unselected bones - if nothing is selected there's
            # nothing to isolate, so leave everything revealed (from
            # pose.reveal(select=False) above) instead of selecting
            # every bone and hiding it, which hid the whole rig.
            if selected_bones:
                bpy.ops.pose.hide(unselected=True)

        refresh_3d_view(context)
        return {'FINISHED'}


# ---------------------------------------------------------
# PICKER UNDO/REDO GUARD
# ---------------------------------------------------------
# Ctrl+Z / Ctrl+Shift+Z while the mouse is over the picker must only ever
# act on the picker's own undo history. The picker window's own key handler
# does that when it has keyboard focus, but hovering does not always move OS
# keyboard focus off Blender's main window - then Blender itself receives the
# key and runs its global undo. This operator sits on those keys in Blender's
# keymap: if the cursor is over the picker it performs the picker undo/redo
# (and swallows the key); otherwise it passes the key straight through to
# Blender's normal undo.

def _cursor_over_picker():
    window = _live_window()
    if window is None:
        return False
    try:
        from PySide6.QtGui import QCursor
        return bool(window.isVisible()
                    and window.frameGeometry().contains(QCursor.pos()))
    except Exception:
        return False


class RP_OT_PickerUndoGuard(bpy.types.Operator):
    bl_idname = "rp.picker_undo_guard"
    bl_label = "Picker Undo/Redo"
    bl_options = {'INTERNAL'}

    redo: bpy.props.BoolProperty(default=False, options={'SKIP_SAVE'})

    def invoke(self, context, event):
        if not _cursor_over_picker():
            return {'PASS_THROUGH'}
        controller = _ACTIVE_CONTROLLER
        redo = self.redo

        def _do():
            try:
                if controller is not None:
                    controller.redo() if redo else controller.undo()
            except Exception:
                import traceback
                traceback.print_exc()
            return None

        # Deferred, like the other picker-driven rebuilds: not inside the
        # operator/event that triggered it.
        bpy.app.timers.register(_do, first_interval=0.0)
        return {'FINISHED'}


_PICKER_KEYMAPS = []


def register_picker_undo_keymap():
    unregister_picker_undo_keymap()
    kc = bpy.context.window_manager.keyconfigs.addon
    if kc is None:  # background mode
        return
    km = kc.keymaps.new(name="Screen", space_type='EMPTY')
    for redo, shift in ((False, False), (True, True)):
        kmi = km.keymap_items.new(
            "rp.picker_undo_guard", 'Z', 'PRESS', ctrl=True, shift=shift)
        kmi.properties.redo = redo
        _PICKER_KEYMAPS.append((km, kmi))

    # Remember which 3D viewport the user last touched (see
    # RP_OT_TrackViewport). Mouse buttons and wheel count with any
    # modifier held (Alt+MMB orbit, Ctrl+wheel pan, ...); trackpad
    # pan/zoom has no press, so it is matched on any value.
    km3d = kc.keymaps.new(name="3D View", space_type='VIEW_3D')
    for event_type, value in (
        ('LEFTMOUSE', 'PRESS'),
        ('RIGHTMOUSE', 'PRESS'),
        ('MIDDLEMOUSE', 'PRESS'),
        ('WHEELUPMOUSE', 'PRESS'),
        ('WHEELDOWNMOUSE', 'PRESS'),
        ('TRACKPADPAN', 'ANY'),
        ('TRACKPADZOOM', 'ANY'),
    ):
        kmi = km3d.keymap_items.new(
            "rp.track_viewport", event_type, value, any=True)
        _PICKER_KEYMAPS.append((km3d, kmi))


def unregister_picker_undo_keymap():
    for km, kmi in _PICKER_KEYMAPS:
        try:
            km.keymap_items.remove(kmi)
        except Exception:
            pass
    _PICKER_KEYMAPS.clear()


# ---------------------------------------------------------
# SHOW ALL
# ---------------------------------------------------------

class RP_OT_ShowAll(bpy.types.Operator):
    bl_idname = "rp.show_all"
    bl_label = "Show All"

    def execute(self, context):
        rig = arm()
        if not rig:
            return {'CANCELLED'}

        override = get_3d_override(context, rig)
        if not override:
            return {'CANCELLED'}

        with context.temp_override(**override):
            context.view_layer.objects.active = rig
            ensure_pose_mode(context, rig)
            bpy.ops.pose.reveal(select=False)

        refresh_3d_view(context)
        return {'FINISHED'}


# ---------------------------------------------------------
# HIDE ALL
# ---------------------------------------------------------

class RP_OT_HideAll(bpy.types.Operator):
    bl_idname = "rp.hide_all"
    bl_label = "Hide All"

    def execute(self, context):
        rig = arm()
        if not rig:
            return {'CANCELLED'}

        override = get_3d_override(context, rig)
        if not override:
            return {'CANCELLED'}

        with context.temp_override(**override):
            context.view_layer.objects.active = rig
            ensure_pose_mode(context, rig)

            bpy.ops.pose.reveal(select=False)
            bpy.ops.pose.select_all(action='DESELECT')
            bpy.ops.pose.hide(unselected=True)

        refresh_3d_view(context)
        return {'FINISHED'}


# ---------------------------------------------------------
# CAPTURE VIEW
# ---------------------------------------------------------

class RP_OT_TrackViewport(bpy.types.Operator):
    """Records which 3D viewport the user last interacted with. Bound (see
    register_picker_undo_keymap) to mouse buttons / wheel / trackpad in the
    3D View keymap; always passes the event through untouched, so viewport
    behaviour is unchanged."""
    bl_idname = "rp.track_viewport"
    bl_label = "Track Viewport"
    bl_options = {'INTERNAL'}

    def invoke(self, context, event):
        global _LAST_VIEW3D
        try:
            window, area = context.window, context.area
            if window is not None and area is not None \
                    and area.type == 'VIEW_3D':
                _LAST_VIEW3D = (window.as_pointer(), area.as_pointer())
        except Exception:
            pass
        return {'PASS_THROUGH'}


def find_capture_viewport(window_manager):
    """(window, area, region) of the 3D viewport Capture View should
    photograph: the one the user last interacted with, if it still exists
    and is still a 3D viewport; otherwise the first 3D viewport found (the
    old behaviour). None if there is no 3D viewport at all."""
    first = None
    for window in window_manager.windows:
        for area in window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            region = next(
                (r for r in area.regions if r.type == 'WINDOW'), None)
            if region is None:
                continue
            if first is None:
                first = (window, area, region)
            if _LAST_VIEW3D == (window.as_pointer(), area.as_pointer()):
                return window, area, region
    return first


class RP_OT_CaptureView(bpy.types.Operator):
    bl_idname = "rp.capture_view"
    bl_label = "Capture View"

    def execute(self, context):
        # PID-scoped, not a fixed shared name: two separate Blender
        # processes on the same machine capturing around the same
        # moment would otherwise race on this same temp file - one
        # process's Controller.capture_view() could copy the OTHER
        # process's screenshot into its own rig/panel's saved
        # background. os.getpid() is stable for this whole process's
        # lifetime, so Controller.capture_view() below (which computes
        # this same path independently, in a different module) always
        # agrees with it without needing to plumb the path through the
        # operator's return value.
        image_path = os.path.join(
            tempfile.gettempdir(),
            f"rig_picker_capture_{os.getpid()}.png"
        )

        scene = context.scene
        old_filepath = scene.render.filepath
        scene.render.filepath = image_path

        success = False

        try:
            target = find_capture_viewport(bpy.context.window_manager)
            if target is not None:
                window, area, region = target
                with bpy.context.temp_override(
                    window=window,
                    area=area,
                    region=region,
                ):
                    bpy.ops.render.opengl(write_still=True)
                success = True
        finally:
            # Always restore the scene's real render output path, even if
            # render.opengl() above raised (a headless/minimized viewport,
            # an invalid image_path, etc.) - left pointed at
            # rig_picker_capture.png, every NORMAL render (F12, batch
            # render, etc.) afterward would silently overwrite that same
            # temp file instead of the user's actual configured output.
            scene.render.filepath = old_filepath

        if not success:
            self.report({'ERROR'}, "Could not capture viewport.")
            return {'CANCELLED'}

        self.report({'INFO'}, "Viewport captured.")


        return {'FINISHED'}