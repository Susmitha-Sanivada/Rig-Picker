"""controller.py

Connects the PySide UI with Blender.

The Controller is the single owner of the current picker's data. It holds
that data (for whichever armature is active) in memory as plain Python
dicts/lists, and is responsible for saving/loading it through the JSON
Manager. Blender's Scene is never used for storage.
"""

import copy
import os
import shutil
import tempfile
import time
from collections import deque

import bpy

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from .. import json_manager
from .. import backend

# How many operations back, per rig, Ctrl+Z can step through with no
# other operation in between. See Controller.undo_snapshots.
UNDO_STACK_DEPTH = 18

# How long the "RigPicker Undo: ..." text stays in the 3D viewport header
# after an undo/redo before the normal header is put back.
UNDO_HEADER_SECONDS = 4.0

# Bumped by every undo/redo so only the LATEST one's timer clears the header
# (an earlier timer firing mid-way through a newer message would wipe it).
_undo_header_generation = 0

# How long a rig has to stay genuinely gone from the file (not merely out
# of the current view layer) before its saved picker data is purged. Gives
# Ctrl+Z on an accidental delete a window to bring the rig back with its
# layout intact.
# LIMITS: the pending-deletion list (self._pending_armature_deletions) is
# plain memory. Quitting/crashing Blender inside this window leaves the
# rig's saved layout, panel rows and multi-command buttons behind (as
# orphans keyed by the old rig name); a NEW rig that later gets exactly
# that name would inherit them. A rename is told apart from a deletion via
# the object's session_uid (see _sync_armature_combo_items()), so a rig
# that is deleted and then re-created - even under the same name - is a
# NEW object as far as this code is concerned.
ARMATURE_DELETE_GRACE_SECONDS = 60


class Controller:

    def __init__(self):
        self.window = None
        self.selected_bones = set()
        self.active = False

        # Name of the armature the currently-loaded data belongs to.
        self.armature_name = None

        # Name of the panel (Manage Panels N-panel section /
        # self.window.stale_combo) the currently-loaded data belongs
        # to. Each panel on a rig keeps its own fully independent
        # picker data - items, background capture, symmetry, undo/redo
        # - the same way two different rigs already did before panels
        # existed; this is the other half of that (rig, panel) key. See
        # _switch_to_panel() and _snapshot_key().
        self.panel_name = None

        # The (rig, panel) pair the picker canvas's Quick multi-command
        # buttons were last filtered for. sync_panel_combo_items() compares
        # it every poll tick and re-syncs on a mismatch, so the canvas can
        # never keep showing another panel's buttons no matter which code
        # path changed self.armature_name/self.panel_name.
        self._quick_synced_key = None

        # Appearance a brand-new control gets (Add Selected) and what
        # the pickers show/enable whenever nothing is selected. Starts
        # at the historical hardcoded values (see json_manager.py's
        # docstring example) but is live-editable via the Size/Shape/
        # Color combos while nothing is selected - see
        # _set_selected_appearance() below.
        self.default_control_size = 18
        self.default_control_shape = "CIRCLE"
        self.default_control_color = "GREEN"

        # In-memory picker data for the current (rig, panel) pair:
        # {"background": str, "symmetry": bool, "symmetry_x": float, "items": [...]}
        self.data = json_manager.new_panel_data()

        # Undo: up to UNDO_STACK_DEPTH snapshots per (rig, panel) pair,
        # keyed by _snapshot_key(), each taken right before an operation
        # (drag, toggle, selection change, appearance change, etc.) on
        # THAT panel changed something. Switching to a different
        # armature OR a different panel on the same rig doesn't touch
        # this - each (rig, panel) pair keeps its own stack, so
        # switching away and back and pressing Ctrl+Z behaves exactly as
        # if no switch had happened in between. Each pair's stack is a
        # deque with maxlen=UNDO_STACK_DEPTH: appending past that limit
        # silently drops the oldest entry, so pressing undo repeatedly
        # with no new operations in between walks back up to
        # UNDO_STACK_DEPTH steps, then stops.
        #
        # A background image delete/backup staged by clear_all()/
        # capture_view() is stored INSIDE the relevant snapshot itself
        # (its "pending_image_delete"/"pending_image_backup" fields, see
        # _record_undo_snapshot(), _current_snapshot(), clear_all(), and
        # capture_view()) rather than as a separate piece of live state -
        # that's what lets each of the last few operations independently
        # restore its own file-level change on undo, instead of only the
        # single most recent one.
        self.undo_snapshots = {}

        # Redo: the exact same structure and depth limit as
        # self.undo_snapshots, keyed the same way - undo() pushes
        # onto this every time it pops something, and redo() pushes
        # onto self.undo_snapshots every time IT pops something, so
        # walking back and forth between the two behaves like a normal
        # two-directional undo history. Any genuine new operation
        # clears the current rig's redo stack entirely (see
        # _record_undo_snapshot()) - same as every other undo/redo
        # system, redoing only makes sense along the exact timeline you
        # just stepped back from.
        self.redo_snapshots = {}

        # Maps each known armature's session_uid (a Blender-assigned
        # identifier for a datablock that stays the same for its whole
        # lifetime in this session, unlike its name) to the name it had
        # as of the last _sync_armature_combo_items() call. Used to tell
        # "this armature was renamed" apart from "this armature was
        # deleted and an unrelated one was added" - see
        # _sync_armature_combo_items()/_handle_armature_renamed().
        self._known_armature_names_by_uid = {}

        # Selection captured by deselect_all() right before it clears
        # self.selected_bones, kept only until the *next* snapshot is
        # taken. Clicking truly-empty canvas space isn't an operation in
        # its own right, so deselect_all() never snapshots - but without
        # this, the very next real operation's snapshot would capture
        # "nothing selected" (the live state left behind by that
        # deselect) instead of whatever was actually selected before the
        # user clicked empty space. See deselect_all() and
        # _record_undo_snapshot() below.
        self._pending_deselect_baseline = None

        # Monotonically increasing, used only to keep each staged
        # background-image backup file's name unique - see
        # _stage_image_backup(). Without this, two capture_view() calls
        # in a row (now both still undoable, up to UNDO_STACK_DEPTH
        # entries deep, instead of only the most recent one) would both
        # write to the same fixed "<path>.undo_backup" filename, and the
        # second would silently clobber the first snapshot's backup
        # before it was ever used.
        self._undo_backup_counter = 0

        # {armature_name: time.monotonic() deadline} for rigs that vanished
        # from the scene and are no longer anywhere in bpy.data.objects.
        # Their saved data is only purged once the deadline passes without
        # the rig coming back - see _sync_armature_combo_items().
        self._pending_armature_deletions = {}

        # (rig name, rig session_uid, frozenset of its pose bone names) as of
        # the last refresh_quick_multi_action_bone_validity() pass - lets
        # that per-tick check do nothing unless the rig's bones changed.
        self._quick_bone_fingerprint = None

    # ---------------------------------------------------------

    def set_window(self, window):
        self.window = window
        # A new/reopened window has no canvas buttons yet - force the next
        # poll tick to re-filter for the currently loaded (rig, panel).
        self._quick_synced_key = None

    # ---------------------------------------------------------
    # UNDO
    # ---------------------------------------------------------

    @staticmethod
    def _diff_items(old_data, new_data):
        """Compact list of what differs between two picker data dicts -
        only changed fields per bone, plus added/removed bones. Used
        purely for undo debug logging."""
        if old_data is None:
            return ["no previous snapshot"]

        old_items = {i["bone_name"]: i for i in old_data.get("items", [])}
        new_items = {i["bone_name"]: i for i in new_data.get("items", [])}

        changes = []
        for name, new_item in new_items.items():
            old_item = old_items.get(name)
            if old_item is None:
                changes.append(f"{name}: added")
                continue
            for field in ("x", "y", "control_size", "control_shape", "control_color", "hidden"):
                if old_item.get(field) != new_item.get(field):
                    changes.append(
                        f"{name}.{field}: {old_item.get(field)} -> {new_item.get(field)}"
                    )

        for name in old_items:
            if name not in new_items:
                changes.append(f"{name}: removed")

        return changes if changes else ["no field changes"]

    @staticmethod
    def _prune_dead_custom_action_overlay_positions(data):
        """Drops any `overlay_positions` entry belonging to a Quick
        Action button that no longer exists in
        Scene.rp_quick_action_settings.

        Removing a Quick Action button goes through Blender's own undo
        system, not this picker's own undo stack (see
        custom_actions_quick.py's RP_OT_quick_remove_*_action). That
        removal's own cleanup deletes the button's saved position from
        self.data/disk at the time it happens, but a LATER picker-level
        undo()/redo() can restore an OLDER self.data snapshot - one
        recorded before that removal - which still has the now-dead
        button's position entry in it. Left alone, that resurrected
        entry would get written straight back to disk by the save()
        undo()/redo() call right after restoring self.data. Called on
        `data` (either self.data restored by undo()/redo(), or any
        dict about to become self.data) before that save().
        """
        overlay_positions = data.get("overlay_positions")
        if not overlay_positions:
            return

        # EVERY scene's buttons count as alive: panel layouts live in the
        # shared JSON keyed by rig/panel name, so a button that belongs to
        # a scene other than the open one still owns its saved position.
        # Checking only bpy.context.scene deleted those positions.
        live_keys = set()
        for scene in bpy.data.scenes:
            quick_settings = getattr(scene, "rp_quick_action_settings", None)
            if quick_settings is None:
                continue
            live_keys |= {
                f"custom_action_quick_multi_{action.uid}"
                for action in quick_settings.multi_actions
            }
            live_keys |= {
                f"custom_action_quick_single_{action.uid}"
                for action in quick_settings.single_actions
            }

        for key in list(overlay_positions.keys()):
            if key.startswith("custom_action_") and key not in live_keys:
                del overlay_positions[key]

    @staticmethod
    def _set_viewport_header_text(text):
        """Sets (or, with None, clears) the text in every 3D viewport's
        header. Best effort: a window/area can be gone by the time the
        clear timer fires (file switch, closed window)."""
        try:
            for window in bpy.context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == "VIEW_3D":
                        area.header_text_set(text)
        except Exception:
            pass

    @staticmethod
    def _show_snapshot_in_viewport(changed):
        """Displays what the last undo restored in the 3D viewport's
        header, purely for visibility into the operation - does not
        affect what undo() restores or how. The text is shown for
        UNDO_HEADER_SECONDS and then removed again: header_text_set()
        replaces the header until it is explicitly reset, so without the
        timer the message would stay there until Blender restarts."""
        global _undo_header_generation

        Controller._set_viewport_header_text(
            f"RigPicker Undo: {', '.join(changed)}"
        )

        _undo_header_generation += 1
        generation = _undo_header_generation

        def _clear():
            if generation == _undo_header_generation:
                Controller._set_viewport_header_text(None)
            return None

        bpy.app.timers.register(_clear, first_interval=UNDO_HEADER_SECONDS)

    def _snapshot_key(self, armature_name=None, panel_name=None):
        """The (rig, panel) key undo/redo stacks are keyed by. Defaults
        to whichever pair is currently loaded; pass explicit values when
        computing the key for a pair other than the live one (e.g. one
        being renamed or deleted elsewhere in the scene)."""
        armature_name = self.armature_name if armature_name is None else armature_name
        panel_name = self.panel_name if panel_name is None else panel_name
        return (armature_name or "", panel_name or "")

    def _switch_to_panel(self, panel_name):
        """Saves whichever panel's data is currently loaded (if any) and
        loads panel_name's own data - on the CURRENT rig (self.armature_name)
        - in its place. The panel-level equivalent of load_armature()'s
        rig-level save-current/load-new swap: a different panel is a
        completely independent picker layout (its own items, background
        capture, symmetry - see json_manager's per-panel storage), so
        nothing carries over across the switch, the same way nothing
        carries over switching rigs.

        No-ops if panel_name already matches what's loaded, so this is
        safe to call speculatively (change_panel() calls it immediately
        for a snappy switch; sync_panel_combo_items() also calls it
        every poll tick as a safety net) without ever double-loading.
        Pass None to represent "no panel loaded" (no rig active, or the
        active rig has no panel selected yet).
        """
        if panel_name == self.panel_name:
            return

        if self.armature_name and self.panel_name:
            self.save()

        self.panel_name = panel_name

        if self.armature_name and panel_name:
            stored = json_manager.get_panel_data(self.armature_name, panel_name)
            self.data = stored if stored is not None else json_manager.new_panel_data()
        else:
            self.data = json_manager.new_panel_data()

        # A different panel's items are a wholly separate picker layout -
        # nothing about the OLD panel's live state belongs here: reset
        # selection and the "what a new control looks like" defaults
        # exactly the way load_armature() already does for a rig switch.
        self.selected_bones = set()
        self._pending_deselect_baseline = None
        self._load_panel_defaults()

        if self.window is not None:
            # Quick multi-command buttons are scoped to (rig, panel), and
            # PickerCanvas.sync_quick_actions() is the ONLY place that
            # re-filters the canvas widgets against self.panel_name.
            # Without this call a panel switch left the previous panel's
            # buttons on screen (and, right after a rig switch - where
            # panel_name is still None when that sync runs - left this
            # panel's own buttons missing). Must run BEFORE refresh() so
            # refresh() only paints the final, correct set.
            self.window.sync_quick_actions()
            self.refresh()
            self.window.set_no_selection_defaults()
            self.refresh_custom_action_enabled_states()

    def _record_undo_snapshot(self, selection_only=False, defaults_only=False):
        """Stores everything undo() needs to revert the picker UI to
        exactly how it looked right before the operation that's about
        to happen.

        Call this once, right before a user-initiated operation starts
        changing anything - e.g. at the start of a drag (not on every
        mouseMoveEvent), before a toggle/appearance/add/delete/selection
        change is applied. Pushes onto this rig's own undo stack (see
        self.undo_snapshots); once that stack holds UNDO_STACK_DEPTH
        entries, each further push quietly drops the oldest one.

        selection_only: pass True when this snapshot is for a plain
        selection change (select_control()/select_all()) rather than a
        real, data-changing operation. A selection_only push only
        actually lands on the stack when it's a genuine *switch* - from
        one selected control (or set of controls) to a different one -
        so undo can walk back through "selected A, then selected B" one
        click at a time. Going from nothing selected to something
        doesn't push anything at all, since there's no earlier selection
        state worth stopping at - without this, undoing back past a
        first-ever pick would land on a redundant "nothing was selected"
        frame wedged between the switch and whatever real operation came
        before it. If a REAL operation (add/delete/move/etc) comes right
        after a selection change with nothing else in between, its own
        push below discards any trailing selection_only entry instead
        of stacking on top of it - otherwise undoing straight back past
        a delete would land on a redundant, do-nothing "reselect
        whatever was selected before you selected the thing you just
        deleted" step instead of the actual prior state.

        defaults_only: pass True when the operation about to happen is
        _set_selected_appearance()'s "nothing selected" branch - editing
        the *default* size/shape/color new controls will get, which by
        definition only happens while there's no selection at all. That
        makes it a different case from the "toggling something"/
        "dragging"/"picking a new control" examples above: those
        continue an interaction with a specific control, so if the
        picker's current selection happens to be empty only because of
        an incidental deselect right before them, resurrecting the
        selection from before that deselect (see selection_for_snapshot
        below) is the right call. Editing the *default* appearance isn't
        continuing anything - it's an operation that's only reachable
        because nothing is selected - so its own undo step should
        restore selection to genuinely empty (what the canvas actually
        looked like right before the dropdown was touched), not resurrect
        whatever was selected before that. Doesn't affect whether a
        pending deselect still protects the previous entry from being
        discarded below - that part is unrelated to which selection this
        particular snapshot restores to.
        """
        stack = self.undo_snapshots.setdefault(
            self._snapshot_key(), deque(maxlen=UNDO_STACK_DEPTH)
        )

        # If the user clicked truly-empty canvas space right before this
        # operation, with nothing else in between, restore selection all
        # the way back to what it was before that deselect - not to the
        # momentarily-empty state it left behind - UNLESS this operation
        # is itself only possible because nothing is selected
        # (defaults_only), in which case genuinely-empty IS the correct
        # state to restore to. The deselect itself was never treated as
        # an operation, so undo shouldn't land on it either way.
        #
        # deselect_was_pending tracks whether a deselect happened here at
        # all (regardless of defaults_only) - see the stale-entry check
        # below, which needs to know this too: a deselect having happened
        # is what makes the previous selection_only entry a genuinely
        # earlier, distinct state, whether or not THIS particular
        # snapshot goes on to resurrect it.
        deselect_was_pending = self._pending_deselect_baseline is not None

        if deselect_was_pending and not defaults_only:
            selection_for_snapshot = self._pending_deselect_baseline
        else:
            selection_for_snapshot = self.selected_bones

        if deselect_was_pending:
            self._pending_deselect_baseline = None

        # Nothing was selected right before this pick, so this isn't a
        # switch between two selections - it's the start of one. Skip
        # pushing anything and let undo fall straight through to
        # whatever real state (or real operation) preceded this click.
        if selection_only and not selection_for_snapshot:
            return

        # A real operation immediately following a plain selection change,
        # with NOTHING ELSE in between, doesn't need that selection change
        # to remain its own separate undo step - the operation's own
        # snapshot (pushed below) already captures the live, just-made
        # selection, so the trailing selection_only entry would only ever
        # be an intermediate, no-op detour between two states undo already
        # visits.
        #
        # deselect_was_pending being True means that assumption doesn't
        # hold: a deselect DID happen in between (select control, click
        # empty canvas, then e.g. change the default shape/size/color
        # with nothing selected) - that deselect was real, even though it
        # never pushed its own snapshot, so the selection_only entry still
        # sitting on top of the stack represents a genuinely earlier,
        # distinct state, not a redundant detour. Leave it be so undo
        # steps through it instead of skipping straight past it.
        if (
            stack
            and stack[-1].get("selection_only")
            and not selection_only
            and not deselect_was_pending
        ):
            stale = stack.pop()
            self._settle_snapshot_pending_image(stale, self._snapshot_key())

        # deque(maxlen=...) auto-evicts the oldest entry once full, but
        # silently - it doesn't hand the evicted item back. Pop it
        # ourselves first when the stack is already full, so its own
        # staged background-image delete/backup (if any) can be finalized
        # now that it's genuinely unreachable, rather than leaking a
        # file on disk that nothing will ever clean up.
        if len(stack) == stack.maxlen:
            evicted = stack.popleft()
            self._settle_snapshot_pending_image(evicted, self._snapshot_key())

        # A real, new operation happening now means whatever was
        # previously undone on this rig is no longer reachable by
        # redoing forward into it - this is the one point every genuine
        # push (selection switch or real edit alike) passes through, so
        # it's the right place to invalidate the old branch. Any
        # snapshots still on the redo stack that never got applied are
        # settled here (their staged background-image work, if any, is
        # now permanently moot).
        stale_redo_stack = self.redo_snapshots.pop(self._snapshot_key(), None)
        if stale_redo_stack:
            for stale_redo_entry in stale_redo_stack:
                self._settle_snapshot_pending_image(stale_redo_entry, self._snapshot_key())

        stack.append({
            "data": copy.deepcopy(self.data),
            "selected_bones": set(selection_for_snapshot),
            "selection_only": selection_only,
            # The "what will a newly-added control look like" default -
            # live Controller state, not part of self.data, so it needs
            # its own explicit capture here. Captured unconditionally
            # (like selected_bones above) since it's cheap and any
            # operation could in principle be the one that changed it.
            "default_control_size": self.default_control_size,
            "default_control_shape": self.default_control_shape,
            "default_control_color": self.default_control_color,
            # Filled in afterward, directly by clear_all()/capture_view()
            # via _current_snapshot(), if this particular operation turns
            # out to be one of those - see that method's docstring.
            "pending_image_delete": None,
            "pending_image_backup": None,
        })

    def _current_snapshot(self):
        """Returns the most recently pushed snapshot for the current rig
        (the one representing "state right before the operation that's
        happening right now"), or None if none exists yet. clear_all()
        and capture_view() use this right after _record_undo_snapshot()
        to attach their own staged background-image delete/backup to the
        snapshot for the operation that staged it, so undoing exactly
        that operation later (however many other operations happen
        first, up to UNDO_STACK_DEPTH) reverts its file-level change
        too - not just whichever operation happens to be most recent at
        the time undo() is actually pressed.
        """
        stack = self.undo_snapshots.get(self._snapshot_key())
        if not stack:
            return None
        return stack[-1]

    @staticmethod
    def _redraw_all_areas():
        """Tags every area in every window for a redraw. Pulled out as a
        helper since more than one undo-adjacent operation (a Custom
        Actions Toggle restore, a plain refresh()) needs one of these
        after changing pose data, rather than each copying the same
        loop."""
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()

    def _settle_snapshot_pending_image(self, snapshot, key=None):
        """Finalizes whatever background-image delete/backup a snapshot
        was carrying, once that snapshot itself is no longer reachable
        by undo (evicted from a full stack, or the (rig, panel) pair it
        belongs to was deleted from the scene entirely). "Finalizing" a
        delete means actually performing it; "finalizing" a backup means
        discarding the backup copy, since the capture that overwrote it
        is now permanent and can never be undone back to it.

        key: the (armature_name, panel_name) pair this snapshot belongs
        to (pass self._snapshot_key() from callers operating on the
        currently loaded pair's own stack, or the relevant key when
        iterating self.undo_snapshots/self.redo_snapshots directly).
        Used to guard against deleting a file that's back in active use -
        capture_view() always writes to the SAME fixed per-(rig, panel)
        path (json_manager.get_image_path()), so "Clear All" (stages the
        old path for deletion once unreachable) followed by a fresh
        capture BEFORE that old snapshot ages off the stack reuses that
        exact path for the new, live image. Without this check, whichever
        of eviction/deletion/window-close finalized that old snapshot
        first would delete the file the panel's CURRENT background is
        actively pointing at - invisible for the rest of the session
        (Qt already has the pixels loaded in memory), but gone the next
        time this panel's data is loaded from disk. If key isn't given,
        this check is skipped (safe for backup paths, which are always
        uniquely suffixed per-operation and never collide with a live
        path anyway).
        """
        if snapshot is None:
            return

        image_path = snapshot.get("pending_image_delete")
        if image_path:
            still_live = False
            if key and key[0] and key[1]:
                current = json_manager.get_panel_data(key[0], key[1])
                if current and current.get("background") == image_path:
                    still_live = True

            if not still_live:
                try:
                    if os.path.exists(image_path):
                        os.remove(image_path)
                except OSError:
                    import traceback
                    traceback.print_exc()

        pending_backup = snapshot.get("pending_image_backup")
        if pending_backup:
            _, backup_path = pending_backup
            if backup_path:
                try:
                    if os.path.exists(backup_path):
                        os.remove(backup_path)
                except OSError:
                    import traceback
                    traceback.print_exc()

    def settle_all_pending_images(self):
        """Finalizes every still-pending background-image delete/backup
        across EVERY rig's undo AND redo stacks, not just the currently
        loaded one - called when the picker window closes, since undo/
        redo history is in-memory only (see self.undo_snapshots and
        self.redo_snapshots) and none of it, for any rig, survives that
        anyway. Without this, closing the picker right after a
        clear_all() or capture_view() (on any rig, not necessarily the
        one currently showing, and whether or not it had since been
        undone) would leave an orphaned "<path>.undo_backup_N" file on
        disk forever, or fail to actually remove a background clear_all()
        had staged for deletion."""
        for key, stack in self.undo_snapshots.items():
            for snapshot in stack:
                self._settle_snapshot_pending_image(snapshot, key)
        self.undo_snapshots.clear()

        for key, stack in self.redo_snapshots.items():
            for snapshot in stack:
                self._settle_snapshot_pending_image(snapshot, key)
        self.redo_snapshots.clear()

    def _stage_image_backup(self, dest_path):
        """Called right before capture_view() overwrites dest_path,
        backing up whatever is currently there (if anything) so
        undo() can bring it back if the capture gets undone. If
        dest_path doesn't exist yet (first-ever capture for this
        armature), remembers that instead, so undo can delete the
        newly-written file rather than restore nothing.

        Must be called AFTER _record_undo_snapshot() for this same
        operation - it attaches the backup to that snapshot (via
        _current_snapshot()) so undoing exactly this capture, later,
        restores exactly this backup, regardless of how many further
        operations happen first."""
        snapshot = self._current_snapshot()
        if snapshot is None:
            return

        if dest_path and os.path.exists(dest_path):
            self._undo_backup_counter += 1
            backup_path = f"{dest_path}.undo_backup_{self._undo_backup_counter}"
            try:
                shutil.copyfile(dest_path, backup_path)
                snapshot["pending_image_backup"] = (dest_path, backup_path)
            except OSError:
                import traceback
                traceback.print_exc()
                snapshot["pending_image_backup"] = None
        else:
            snapshot["pending_image_backup"] = (dest_path, None)

    def _restore_snapshot_pending_image_backup(self, snapshot):
        """Undoes what _stage_image_backup() staged for this specific
        snapshot, restoring the previous background image's actual
        pixels (or removing the file entirely if there wasn't one
        before). Called from undo() for the snapshot being popped."""
        if snapshot is None:
            return

        pending = snapshot.get("pending_image_backup")
        if not pending:
            return

        dest_path, backup_path = pending

        try:
            if backup_path:
                if os.path.exists(backup_path):
                    shutil.copyfile(backup_path, dest_path)
                    os.remove(backup_path)
            else:
                if dest_path and os.path.exists(dest_path):
                    os.remove(dest_path)
        except OSError:
            import traceback
            traceback.print_exc()

    def _capture_reverse_entry(self, snapshot):
        """Builds the mirror-image entry for whatever `snapshot` is
        about to apply, captured from the CURRENT live state right
        before anything changes - so the opposite action (redo right
        after this undo, or undo right after this redo) can bring it
        straight back. Shared by undo() and redo(), since applying a
        snapshot in either direction needs the exact same "remember
        what we're about to overwrite" step, just walking the timeline
        in opposite directions.
        """
        reverse_entry = {
            "data": copy.deepcopy(self.data),
            "selected_bones": set(self.selected_bones),
            "selection_only": snapshot.get("selection_only", False),
            "pending_image_delete": None,
            "pending_image_backup": None,
            # Mirror of default_control_size/shape/color below - capture
            # the CURRENT live values before snapshot's own values
            # overwrite them, so reversing this step later restores them.
            "default_control_size": self.default_control_size,
            "default_control_shape": self.default_control_shape,
            "default_control_color": self.default_control_color,
        }

        # Background image pixel-content mirror (capture_view() case:
        # same file path, different bytes on disk) - back up whatever's
        # currently there before snapshot's own pending_image_backup
        # overwrites it below.
        pending_backup = snapshot.get("pending_image_backup")
        if pending_backup:
            dest_path, _ = pending_backup
            if dest_path and os.path.exists(dest_path):
                self._undo_backup_counter += 1
                mirror_path = f"{dest_path}.undo_backup_{self._undo_backup_counter}"
                try:
                    shutil.copyfile(dest_path, mirror_path)
                    reverse_entry["pending_image_backup"] = (dest_path, mirror_path)
                except OSError:
                    import traceback
                    traceback.print_exc()
            else:
                reverse_entry["pending_image_backup"] = (dest_path, None)

        # Background image existence mirror (clear_all() case: the
        # reference is dropped entirely, not overwritten) - re-derived
        # by comparing the state being left to the state being entered,
        # rather than trusting a hardcoded "only clear_all sets this"
        # rule, so it self-corrects no matter which direction or how
        # many undo/redo cycles this entry has been through.
        left_background = reverse_entry["data"].get("background")
        entering_background = snapshot["data"].get("background")
        if left_background and left_background != entering_background:
            reverse_entry["pending_image_delete"] = left_background

        return reverse_entry

    def stash_rig_history(self, rig_name):
        """Removes every (rig, panel) undo/redo stack belonging to
        `rig_name` from the live dicts and returns them as
        {"undo": {key: stack}, "redo": {key: stack}} WITHOUT settling their
        staged image work (the caller either parks them, to be restored
        later, or hands them to settle_rig_history())."""
        history = {"undo": {}, "redo": {}}
        for which, stacks_dict in (("undo", self.undo_snapshots),
                                   ("redo", self.redo_snapshots)):
            for key in [k for k in stacks_dict if k[0] == rig_name]:
                history[which][key] = stacks_dict.pop(key)
        return history

    def settle_rig_history(self, history):
        """Finalizes the staged image work of a stashed history that will
        never be restored (see stash_rig_history())."""
        if not history:
            return
        for stacks in history.values():
            for key, stack in stacks.items():
                for snapshot in stack:
                    self._settle_snapshot_pending_image(snapshot, key)

    def restore_rig_history(self, rig_name, history):
        """Puts a stashed history back. Whatever stacks `rig_name` has
        accumulated in the meantime (the fresh default panel's, while the
        rig was cleared) belong to a state that is going away, so they are
        settled and replaced - the pre-clear timeline wins."""
        self.settle_rig_history(self.stash_rig_history(rig_name))
        if not history:
            return
        self.undo_snapshots.update(history.get("undo", {}))
        self.redo_snapshots.update(history.get("redo", {}))

    def stash_panel_history(self, key):
        """Removes ONE (rig, panel) pair's undo/redo stacks from the live
        dicts and returns {"undo": stack|None, "redo": stack|None},
        without settling their staged image work. Used while a removed
        panel's removal can still be undone (see
        custom_actions_quick._PENDING_PANEL_HISTORY)."""
        return {
            "undo": self.undo_snapshots.pop(key, None),
            "redo": self.redo_snapshots.pop(key, None),
        }

    def settle_panel_history(self, key, parked):
        """Finalizes the staged image work of a stashed panel history that
        will never be restored."""
        for stack in (parked or {}).values():
            for snapshot in stack or ():
                self._settle_snapshot_pending_image(snapshot, key)

    def restore_panel_history(self, key, parked):
        """Puts a stashed panel history back. Anything the pair has
        accumulated since (a panel of the same name created meanwhile) is
        settled and replaced - the original timeline wins."""
        self.settle_panel_history(key, self.stash_panel_history(key))
        if not parked:
            return
        if parked.get("undo"):
            self.undo_snapshots[key] = parked["undo"]
        if parked.get("redo"):
            self.redo_snapshots[key] = parked["redo"]

    def _push_reverse_entry(self, stacks_dict, entry):
        """Pushes `entry` onto this rig's stack within `stacks_dict`
        (either self.undo_snapshots or self.redo_snapshots), applying
        the exact same maxlen-eviction handling _record_undo_snapshot()
        does for a normal push - keeps undo() and redo()'s own pushes
        consistent with it instead of duplicating the eviction logic a
        third time."""
        stack = stacks_dict.setdefault(
            self._snapshot_key(), deque(maxlen=UNDO_STACK_DEPTH)
        )
        if len(stack) == stack.maxlen:
            evicted = stack.popleft()
            self._settle_snapshot_pending_image(evicted, self._snapshot_key())
        stack.append(entry)

    def _visible_selected_bones(self, names):
        """Filters a candidate set of bone names down to ones whose
        bone collection is actually visible right now (see backend.
        is_bone_collection_visible()). Used wherever a selection is
        being *restored* from stored state (undo/redo snapshots)
        rather than coming fresh from a live click or the viewport -
        an undo landing on a snapshot that selected a bone whose
        collection has since been hidden would otherwise mark that
        control active in the picker immediately, only to have the
        next poll tick silently shrink it back down once it notices
        Blender's own selection doesn't actually include it.

        A bone that no longer exists on the current rig at all (not a
        visibility question) is left untouched here - current_bone_names
        intersection at each call site already handles that.
        """
        from .. import backend as _backend

        rig = _backend.arm()
        if not rig or rig.type != "ARMATURE":
            return set(names)

        return {
            name for name in names
            if name not in rig.pose.bones
            or _backend.is_bone_collection_visible(rig.pose.bones[name])
        }

    def _interrupted_by_drag(self):
        """True when an undo/redo request was consumed by a drag or G grab
        that is in progress, so the caller must do nothing further. Every
        entry point (the picker window's Ctrl+Z / Ctrl+Shift+Z, and the
        Blender keymap guard while the cursor is over the picker) comes
        through undo()/redo(), so this is the one place that decides.

        Ctrl+Z mid-grab CANCELS the grab (what Esc does) and is not an
        undo step; mid-mouse-drag it is ignored. See
        PickerCanvas.interrupt_for_undo()."""
        canvas = getattr(getattr(self.window, "control_list", None), "container", None)
        return bool(canvas is not None and canvas.interrupt_for_undo())

    def undo(self, discard_redo=False):
        """Reverts the current rig's picker UI to exactly how it looked
        right before the most recent still-remembered operation on THIS
        rig: picker data (control positions, appearance, symmetry,
        background, checkboxes, ...) and the current control selection/
        highlighting.

        Each rig keeps its own undo stack, up to UNDO_STACK_DEPTH entries
        deep (see self.undo_snapshots) - switching to a different
        armature and back doesn't clear it, so this only ever reverts
        whatever was last done on the rig that's currently loaded,
        regardless of how many other rigs were visited in between.
        Pressing Ctrl+Z repeatedly with no new operations in between
        walks back up to UNDO_STACK_DEPTH steps on this rig, then does
        nothing once the stack is empty.

        Every step taken back is pushed onto this rig's redo stack (see
        self.redo_snapshots) before anything is restored, so redo()
        (Ctrl+Shift+Z) can bring it right back - unless a genuine new
        operation happens first, which clears that redo history (see
        _record_undo_snapshot()).

        discard_redo: pass True when this undo is a CANCEL of an operation
        that was never committed (Esc / right-click during a G grab), not
        a step the user took back. Nothing is pushed onto the redo stack
        then, so Ctrl+Shift+Z can't re-apply a move that was cancelled.

        A real undo requested while a drag/grab is in progress is not an
        undo at all: see _interrupted_by_drag().
        """
        if not discard_redo and self._interrupted_by_drag():
            return
        stack = self.undo_snapshots.get(self._snapshot_key())
        if not stack and discard_redo:
            # A cancel only ever reverts the snapshot its own first
            # movement recorded; with none left there is nothing to cancel,
            # and it must never fall through to undoing a Clear All.
            return
        if not stack:
            # Nothing left on the picker's own undo stack. The one thing
            # that can still be undone from here is this rig's Clear All
            # (its panel rows are scene data; see clear_all()). Anything
            # else: do nothing - Ctrl+Z over the picker must never be
            # forwarded to Blender's global undo for other edits.
            entry = self._clear_all_entry("cleared")
            if entry is not None:
                from .. import custom_actions_quick
                custom_actions_quick.undo_rig_clear(entry)
            else:
                print("Rig Picker: nothing to undo in the picker.")
            return

        snapshot = stack.pop()

        # Capture the mirror-image redo entry from the CURRENT live
        # state before anything below changes it - unless this undo is
        # only cancelling an uncommitted operation (see discard_redo).
        if not discard_redo:
            self._push_reverse_entry(
                self.redo_snapshots, self._capture_reverse_entry(snapshot)
            )

        # Whatever's being undone is no longer eligible for its own
        # background image to be deleted (if it was clear_all(), the
        # image is about to be back in active use once self.data is
        # restored below) - drop its pending deletion rather than act on
        # it, since deleting here would remove the very file undo is
        # about to reference again.
        snapshot["pending_image_delete"] = None

        # If what's being undone was a capture_view() press, bring back
        # the background image it overwrote (or remove the file it
        # wrote if there was nothing there before) - self.data below
        # restores the *path* to what it was, but capture_view() always
        # writes to that same fixed path, so the path alone doesn't get
        # the old pixels back.
        self._restore_snapshot_pending_image_backup(snapshot)

        changed = self._diff_items(self.data, snapshot["data"])

        self.data = snapshot["data"]
        self._prune_dead_custom_action_overlay_positions(self.data)
        self.save()
        self.refresh()

        self._show_snapshot_in_viewport(changed)

        # Picker undo/redo only touches the picker's own layout data. It
        # deliberately does NOT write anything back into Blender (Quick
        # Action toggle values, N-panel buttons): those are Blender changes
        # and are undone with Blender's own undo.

        # Restore the "what will a newly-added control look like"
        # default too - live Controller state, not part of self.data,
        # so self.data above doesn't already cover it. Set before the
        # set_no_selection_defaults() call below, which reads these
        # three attributes live.
        self.default_control_size = snapshot["default_control_size"]
        self.default_control_shape = snapshot["default_control_shape"]
        self.default_control_color = snapshot["default_control_color"]
        self._save_panel_defaults()

        # Restore which controls were selected/highlighted. Drop any
        # bone that no longer exists as a control post-restore (e.g. an
        # add/delete was what's being undone).
        current_bone_names = {item["bone_name"] for item in self.data["items"]}
        self.selected_bones = self._visible_selected_bones(
            snapshot["selected_bones"] & current_bone_names
        )

        if self.window is not None:
            for name, widget in self.window.control_list.controls.items():
                widget.active = (name in self.selected_bones)
                widget.update()

            if self.selected_bones:
                first = next(iter(self.selected_bones))
                item = self.find_item(first)

                if item:
                    self.window.set_selected_control(
                        item["control_size"],
                        item["control_shape"],
                        item["control_color"],
                    )
                    # set_selected_control() above already enables/
                    # disables size_combo/shape_combo/color_combo based
                    # on whether this rig currently has a background -
                    # no need to force them on separately here (doing so
                    # used to bypass that gating after an undo).
            else:
                self.window.set_no_selection_defaults()

            self._update_action_controls_enabled()

        self._sync_viewport_selection()

        self.refresh_custom_action_enabled_states()

    def redo(self):
        """The Ctrl+Shift+Z counterpart to undo() - re-applies whatever
        was last undone on the currently loaded rig, provided nothing
        new has happened on it since (see _record_undo_snapshot(),
        which clears this rig's redo stack the instant a fresh
        operation is recorded - same as every other undo/redo system,
        redoing only makes sense along the exact timeline just stepped
        back from).

        Mirrors undo() in every respect - same per-rig stack depth,
        same background-image handling, same selection restore - just
        walking forward instead of back. Every step taken forward is
        pushed onto this rig's undo stack first, so undo() can
        immediately reverse it again.

        Like undo(), does nothing while a drag/grab is in progress (see
        _interrupted_by_drag()).
        """
        if self._interrupted_by_drag():
            return
        stack = self.redo_snapshots.get(self._snapshot_key())
        if not stack:
            # Nothing left on the picker's own redo stack: only a Clear All
            # that was just undone can be redone (see undo()).
            entry = self._clear_all_entry("undone")
            if entry is not None:
                from .. import custom_actions_quick
                custom_actions_quick.redo_rig_clear(entry)
            return

        snapshot = stack.pop()

        self._push_reverse_entry(
            self.undo_snapshots, self._capture_reverse_entry(snapshot)
        )

        snapshot["pending_image_delete"] = None
        self._restore_snapshot_pending_image_backup(snapshot)

        changed = self._diff_items(self.data, snapshot["data"])

        self.data = snapshot["data"]
        self._prune_dead_custom_action_overlay_positions(self.data)
        self.save()
        self.refresh()

        self._show_snapshot_in_viewport(changed)

        # Picker undo/redo only touches the picker's own layout data. It
        # deliberately does NOT write anything back into Blender (Quick
        # Action toggle values, N-panel buttons): those are Blender changes
        # and are undone with Blender's own undo.

        self.default_control_size = snapshot["default_control_size"]
        self.default_control_shape = snapshot["default_control_shape"]
        self.default_control_color = snapshot["default_control_color"]
        self._save_panel_defaults()

        current_bone_names = {item["bone_name"] for item in self.data["items"]}
        self.selected_bones = self._visible_selected_bones(
            snapshot["selected_bones"] & current_bone_names
        )

        if self.window is not None:
            for name, widget in self.window.control_list.controls.items():
                widget.active = (name in self.selected_bones)
                widget.update()

            if self.selected_bones:
                first = next(iter(self.selected_bones))
                item = self.find_item(first)

                if item:
                    self.window.set_selected_control(
                        item["control_size"],
                        item["control_shape"],
                        item["control_color"],
                    )
            else:
                self.window.set_no_selection_defaults()

            self._update_action_controls_enabled()

        self._sync_viewport_selection()

        self.refresh_custom_action_enabled_states()

    def _sync_viewport_selection(self):
        """Pushes self.selected_bones onto the actual viewport selection.

        Does exactly ONE reveal/deselect/hide/redraw pass for the whole
        selection - setting every target bone's pb.select directly -
        instead of calling bpy.ops.rp.select() once per bone. Each of
        those per-bone operator calls redid that entire reveal/
        deselect/hide/redraw cycle on its own (plus its own undo push,
        depsgraph update and viewport redraw), which is fine for a
        single click but scales linearly with the number of selected
        controls - so a box select or an undo/redo landing on a multi-
        control selection got visibly slower the more controls were
        involved, even though the end result is exactly the same set
        of bones selected either way.
        """
        try:
            from ..backend import (
                arm,
                get_3d_override,
                ensure_pose_mode,
                is_bone_collection_visible,
                refresh_3d_view,
            )

            rig = arm()
            if not rig or rig.type != "ARMATURE":
                backend._LAST_SELECTED_BONES = frozenset()
                return

            override = get_3d_override(bpy.context, rig)
            if not override:
                return

            # Same visibility guard RP_OT_Select's own shift-branch
            # applied per bone (see backend.py) - a bone sitting in a
            # currently-hidden collection can't really be selected in
            # the viewport, so it's dropped here rather than fed into
            # pb.select below. Checked once for the whole batch instead
            # of once per operator call.
            valid_selected = [
                name for name in self.selected_bones
                if name in rig.pose.bones
                and is_bone_collection_visible(rig.pose.bones[name])
            ]

            with bpy.context.temp_override(**override):
                # rp.select always sets this explicitly before calling
                # ensure_pose_mode() - the "active_object" key in the
                # override dict alone isn't enough for mode_set()/
                # select_all()'s polls to reliably pick up the rig as
                # active. Without it, both calls can silently no-op
                # (CANCELLED, no exception) if the rig wasn't already
                # the "real" active object at this exact moment.
                bpy.context.view_layer.objects.active = rig
                ensure_pose_mode(bpy.context, rig)

                # Reveal everything, then clear the selection ONCE for
                # the whole batch (not once per bone) before applying
                # the actual target selection below.
                bpy.ops.pose.reveal(select=False)
                bpy.ops.pose.select_all(action="DESELECT")

                if valid_selected:
                    for name in valid_selected:
                        rig.pose.bones[name].select = True

                    # Arbitrary but stable pick for "active" (order
                    # isn't otherwise meaningful in a set) - matches
                    # what the old per-bone loop left active, since its
                    # final call's own bone_name always ended up being
                    # the one still in selected_bones at that point.
                    active_name = valid_selected[-1]
                    rig.data.bones.active = rig.pose.bones[active_name].bone

                    bpy.ops.pose.hide(unselected=True)
                else:
                    # Nothing selected means nothing to isolate - the
                    # pose.reveal(select=False) above already left every
                    # bone visible, so just leave it that way. (This used
                    # to select every bone and then hide(unselected=False),
                    # which hides whatever's selected - i.e. it hid every
                    # bone in the rig, making them look deleted in the
                    # viewport.)
                    rig.data.bones.active = None

            refresh_3d_view(bpy.context)

            backend._LAST_SELECTED_BONES = frozenset(valid_selected)

        except Exception:
            import traceback
            traceback.print_exc()

    # ---------------------------------------------------------
    # ARMATURE SWITCHING
    # ---------------------------------------------------------

    def _armature_is_hidden(self, obj):
        """True if this armature is currently hidden in the viewport
        (the eye-icon / H-key hide toggle - bpy's hide_get()/hide_set()).
        Hidden rigs are left out of the Rig dropdown entirely (their saved
        picker data is untouched - they simply reappear in the list when
        shown again).
        """
        return not backend.is_rig_visible(obj)

    def _set_combo_item_enabled(self, combo, index, enabled):
        """Greys out (but doesn't remove) a QComboBox entry. addItem()
        backs the combo with a QStandardItemModel by default, so
        model().item(index) is always available here without needing to
        construct one explicitly.
        """
        model = combo.model()
        item = model.item(index)
        if item is not None:
            item.setEnabled(enabled)

    def _sync_armature_combo_enabled_state(self, combo, armature_objects):
        """Greys out any rig currently hidden in the viewport, for every
        entry the combo currently has - called both when the combo's
        list of names was just rebuilt and when it wasn't (a hide
        toggle alone never changes which names exist, only whether
        they're selectable), so hidden rigs stay visibly disabled
        immediately regardless of which path triggered this sync.
        """
        for i in range(combo.count()):
            obj = armature_objects.get(combo.itemText(i))
            hidden = obj is not None and self._armature_is_hidden(obj)
            self._set_combo_item_enabled(combo, i, not hidden)

    def _handle_armature_renamed(self, old_name, new_name):
        """Migrates a tracked armature's saved state from old_name to
        new_name after Blender itself renamed the underlying object
        (Outliner, F2, the Properties panel, etc. - anything other than
        this addon, which never renames an armature on its own).

        Called from _sync_armature_combo_items() once it's confirmed
        (via session_uid) that this is the same rig as before, just
        under a new name - so this Controller's in-memory undo/redo
        history and the combo entry itself need to follow the rename
        rather than the old name being treated as deleted. (The saved
        layouts and scene data are migrated before this, see below.)
        """
        # The rig's saved layouts (rig_picker_data.json), the Quick
        # multi-command buttons and Manage Panels rows (every scene) and
        # custom_actions_quick's session memory have ALREADY been moved by
        # custom_actions_quick.migrate_rig_renames(), which
        # _sync_armature_combo_items() runs for the whole batch of renames
        # before calling this once per pair - doing it there, not here, is
        # what lets two rigs swapping names be migrated safely. What's left
        # is this Controller's own state.
        if self.armature_name == old_name:
            self.armature_name = new_name
            # Keeps backend.arm()/poll_active_armature() from seeing a
            # name mismatch on the very next check and mistaking this
            # rename for the active object switching to a different
            # armature entirely (which would reload a blank picker for
            # "new_name" instead of recognizing it as the rig already
            # loaded).
            if backend._CACHED_ARM_NAME == old_name:
                backend._CACHED_ARM_NAME = new_name

        # Each (rig, panel) pair's undo/redo stack is keyed by
        # _snapshot_key() (see self.undo_snapshots' docstring) - move
        # every one of this rig's own pairs over (whichever panel each
        # key's second element names) so Ctrl+Z still finds them under
        # the rig's new name instead of becoming unreachable dead weight
        # in the dict forever.
        for stacks_dict in (self.undo_snapshots, self.redo_snapshots):
            for key in [k for k in stacks_dict if k[0] == old_name]:
                stacks_dict[(new_name, key[1])] = stacks_dict.pop(key)

        # Rename the existing combo entry in place rather than letting
        # the caller's normal add/remove diffing handle it - that would
        # drop the item and re-add it at a new sorted position, which is
        # harmless here but also unnecessary now that the data itself
        # has already safely followed the rename.
        if self.window is not None:
            combo = self.window.armature_combo
            for i in range(combo.count()):
                if combo.itemText(i) == old_name:
                    combo.blockSignals(True)
                    combo.setItemText(i, new_name)
                    combo.blockSignals(False)
                    break

    def handle_panel_renamed(self, old_name, new_name):
        """The panel-level counterpart to _handle_armature_renamed()
        above - called (from RP_QuickPanel's own name update callback
        in custom_actions_quick.py) when the CURRENTLY LOADED panel is
        renamed in place from the Manage Panels list, so this
        Controller's own live state follows the rename instead of
        pointing at a name that no longer matches anything.

        json_manager's on-disk migration and the Qt combo's displayed
        text are handled by the caller/sync_panel_combo_items()
        respectively - this only needs to move this rig's undo/redo
        stack entries for the old panel name over to the new one, and
        update self.panel_name itself so the next save()/_switch_to_panel()
        comparison keys against the right name.
        """
        for stacks_dict in (self.undo_snapshots, self.redo_snapshots):
            key = (self.armature_name, old_name)
            if key in stacks_dict:
                stacks_dict[(self.armature_name, new_name)] = stacks_dict.pop(key)

        self.panel_name = new_name

    def _sync_armature_combo_selection(self, combo):
        """Keeps the Rig dropdown's *displayed selection* - not just its
        item list - matching self.armature_name, so the three-way Rig
        state (Blender viewport active object / this combo / the Quick
        Actions N-panel's own Rig dropdown) never shows a rig here
        while the N-panel shows none, or the other way around. Qt also
        auto-selects the first item the moment a combo goes from empty
        to non-empty on any clear()+addItem() rebuild, regardless of
        which rig (if any) is actually loaded - this corrects that
        too. Cheap no-op when already correct, so safe to call on
        every poll tick regardless of whether anything else changed
        this time.
        """
        target_text = self.armature_name or ""
        if combo.currentText() == target_text:
            return

        combo.blockSignals(True)
        try:
            if target_text:
                index = combo.findText(target_text)
                combo.setCurrentIndex(index)
            else:
                combo.setCurrentIndex(-1)
        finally:
            combo.blockSignals(False)

    def _purge_expired_armature_deletions(self, scene_armatures):
        """Permanently removes saved data for rigs that have been gone
        from the file for ARMATURE_DELETE_GRACE_SECONDS. A rig that came
        back (undo, re-link, re-import) before its deadline is simply
        dropped from the pending list and keeps everything."""
        if not self._pending_armature_deletions:
            return

        now = time.monotonic()
        for name, deadline in list(self._pending_armature_deletions.items()):
            existing = bpy.data.objects.get(name)
            if name in scene_armatures or (
                existing is not None and existing.type == "ARMATURE"
            ):
                del self._pending_armature_deletions[name]
                continue

            if now < deadline:
                continue

            del self._pending_armature_deletions[name]
            json_manager.delete_armature_data(name)

            # A purged rig can never be switched back to, so every one of
            # its (rig, panel) undo/redo stacks (and any pending image
            # delete/backup riding along) has nowhere left to resolve.
            for stacks_dict in (self.undo_snapshots, self.redo_snapshots):
                for key in [k for k in stacks_dict if k[0] == name]:
                    stale_stack = stacks_dict.pop(key)
                    for stale_snapshot in stale_stack:
                        self._settle_snapshot_pending_image(stale_snapshot, key)

            # Quick multi-command buttons and Manage Panels rows are scoped
            # to a rig by a plain name snapshot (armature_name); with the
            # rig gone for good they'd sit in the Scene collections
            # forever. Cleaned in EVERY scene - this branch is only
            # reached once the rig is absent from bpy.data entirely.
            from .. import custom_actions_quick
            custom_actions_quick.purge_rig_quick_data(name)

    def settle_before_file_change(self):
        """Settle half of a file switch. Called from the lifecycle
        dispatcher at load_pre (File > Open / New / Revert), while the
        OUTGOING file's store is still the current one.

        Pending undo-snapshot images (staged deletes/backups) and the
        pending armature deletions all consult json_manager, which now
        resolves to the file that is open - so they must be settled here,
        before the switch, or they would be evaluated against the NEW
        file's store. Armature deletions still in their grace period are
        dropped without deleting anything (nothing says the removal was
        saved; reopening the old file brings the rig back).
        """
        self._pending_armature_deletions.clear()

        for stacks_dict in (self.undo_snapshots, self.redo_snapshots):
            for key, stack in list(stacks_dict.items()):
                for snapshot in stack:
                    self._settle_snapshot_pending_image(snapshot, key)
            stacks_dict.clear()

    def repoint_paths(self, old_folder, new_folder):
        """After a first save or Save As moved/copied the data folder:
        re-points every absolute image path this controller holds (the
        loaded layout, every undo/redo snapshot and their staged image
        work) from old_folder to new_folder. Without it a later delete or
        undo would act on the OLD file's images."""
        tr = json_manager.translate_path

        def fix_data(data):
            if data and data.get("background"):
                data["background"] = tr(data["background"], old_folder, new_folder)

        def fix_stack(stack):
            for snapshot in stack or ():
                fix_data(snapshot.get("data"))
                if snapshot.get("pending_image_delete"):
                    snapshot["pending_image_delete"] = tr(
                        snapshot["pending_image_delete"], old_folder, new_folder)
                pending = snapshot.get("pending_image_backup")
                if pending:
                    snapshot["pending_image_backup"] = tuple(
                        tr(p, old_folder, new_folder) for p in pending)

        fix_data(self.data)
        for stacks_dict in (self.undo_snapshots, self.redo_snapshots):
            for stack in stacks_dict.values():
                fix_stack(stack)

        # History parked while a Clear All / panel removal can still be
        # undone belongs to the same file, so it moves too - otherwise
        # undoing that Clear All after a Save As would bring back snapshots
        # that point into the old folder.
        from .. import custom_actions_quick
        parked = [entry.get("history") for entry in custom_actions_quick._PENDING_RIG_CLEARS]
        parked += list(custom_actions_quick._PENDING_PANEL_HISTORY.values())
        for history in parked:
            for stacks in (history or {}).values():
                if isinstance(stacks, dict):          # Clear All: {key: stack}
                    for stack in stacks.values():
                        fix_stack(stack)
                else:                                 # panel removal: a stack
                    fix_stack(stacks)

    def on_file_loaded(self):
        """UI-reset half of a file switch. Called from backend's
        load_post handler (File > Open / New / Revert), after the store
        has been swapped to the new file. Everything this controller
        holds in memory belongs to the file that was just replaced. The
        old file's rigs are simply not in the new file's scene, which is
        not the same as deleted, so NOTHING is deleted from any store
        here. Settling of pending items already happened at load_pre -
        see settle_before_file_change()."""
        self._pending_armature_deletions.clear()
        self._known_armature_names_by_uid = {}

        # Normally already empty (settled at load_pre). Cleared without
        # settling on purpose: settling now would consult the NEW file's
        # store.
        self.undo_snapshots.clear()
        self.redo_snapshots.clear()

        self.armature_name = None
        self.panel_name = None
        self.data = json_manager.new_panel_data()
        self.selected_bones = set()
        self._pending_deselect_baseline = None
        self.default_control_size = 18
        self.default_control_shape = "CIRCLE"
        self.default_control_color = "GREEN"

        if self.window is not None:
            combo = self.window.armature_combo
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
            self.refresh()
            self.window.set_no_selection_defaults()

    def _repaint_armature_combo(self, combo):
        """Repaint just the Rig dropdown right now, after its items changed
        from a bpy.app.timers callback (where Qt isn't pumped).

        Deliberately NOT QApplication.processEvents(): that dispatches
        EVERY queued Qt event - deferred deletes of the canvas controls
        just rebuilt by load_armature(), enter/leave events, nested
        processEvents() from PickerComboBox._apply_style(), etc. - from
        inside Blender's timer, and a Python override raising during that
        crashed Blender natively (shiboken6 access violation in
        sendPostedEvents). A direct repaint() only paints this one widget.
        """
        try:
            combo.update()
            combo.repaint()
        except Exception:
            import traceback
            traceback.print_exc()

    def _fallback_if_current_rig_hidden(self, armature_objects):
        """If the rig currently loaded in the picker has been hidden in the
        viewport, switch the picker to the first visible rig (name-sorted -
        the same choice the deleted-rig fallback in
        _sync_armature_combo_items() makes), or to "no rig" if none is
        visible.
        Read-only with respect to Blender's own active object/selection -
        only the picker's state moves (changing the active object from a
        timer callback is unsafe). The hidden rig's data is saved first by
        load_armature().
        """
        # load_armature() below calls back into _sync_armature_combo_items(),
        # which would land here again - never nest, so a load that fails to
        # move off the hidden rig can't recurse.
        if getattr(self, "_hidden_fallback_active", False):
            return False

        name = self.armature_name
        if not name:
            return False
        obj = armature_objects.get(name)
        if obj is None or backend.is_rig_visible(obj):
            return False

        self._hidden_fallback_active = True
        try:
            return self._switch_off_hidden_rig(name, armature_objects)
        finally:
            self._hidden_fallback_active = False

    def _switch_off_hidden_rig(self, name, armature_objects):

        visible = sorted(
            n for n, o in armature_objects.items() if backend.is_rig_visible(o)
        )
        if visible:
            new_rig = armature_objects[visible[0]]
            backend._CACHED_ARM = new_rig
            backend._CACHED_ARM_NAME = new_rig.name
            backend._QT_SYNCED_ARM_NAME = new_rig.name
            self.load_armature(new_rig)
        else:
            if backend._CACHED_ARM_NAME == name:
                backend._CACHED_ARM = None
                backend._CACHED_ARM_NAME = None
            backend._QT_SYNCED_ARM_NAME = None
            self.load_armature(None)
        return True

    def _sync_armature_combo_items(self):
        """Keeps the dropdown's list of choices in sync with the actual
        armatures in the scene.

        The combo is only populated once at window-creation time
        (main_window.py), so an armature added to the scene afterwards -
        or renamed/deleted - was never reflected as a selectable entry,
        even though everything else in the picker (controls, data, etc.)
        updates fine. Blocks signals while rebuilding so this never
        re-triggers change_armature().
        """
        if self.window is None:
            return

        combo = self.window.armature_combo

        current_items = {combo.itemText(i) for i in range(combo.count())}
        # Use the current VIEW LAYER's objects, not bpy.data.objects - the
        # latter is every object datablock in the whole .blend file,
        # including ones that were deleted from this scene but are still
        # kept alive as orphan data (0-user datablocks aren't always
        # purged immediately), or that simply live in a different scene
        # entirely. Either of those showing up here meant the dropdown
        # could offer a rig that view_layer.objects.active = rig would
        # then crash on with "ViewLayer does not contain object", since
        # it was never actually selectable in this scene to begin with.
        armature_objects = {
            obj.name: obj
            for obj in bpy.context.view_layer.objects
            if obj.type == "ARMATURE"
        }
        # The combo's item list always contains EVERY armature in the view
        # layer (same as before hiding was handled at all). Hidden rigs are
        # only disabled here and skipped by PickerComboBox.showPopup(), so
        # they never appear in the opened list - the items themselves are
        # NOT removed/re-added on every hide/unhide, since that rebuild
        # (clear()+addItem() on a styled combo) crashed Blender.
        scene_armatures = set(armature_objects)

        # Detect armatures that are still the exact same underlying
        # datablock as last time (session_uid is stable across a rename,
        # unlike name) but now report a different name - i.e. renamed
        # directly in Blender (Outliner double-click, F2, the Object/
        # Armature Properties panel, etc.) rather than through this
        # addon. Without this, a rename is indistinguishable below from
        # "the old name was deleted and an unrelated new one was added",
        # which either orphaned or (via the deleted-armature cleanup
        # further down) outright deleted the rig's saved picker data
        # just because Blender renamed the underlying object.
        current_by_uid = {
            obj.session_uid: name for name, obj in armature_objects.items()
        }
        prev_by_uid = self._known_armature_names_by_uid
        renamed_pairs = [
            (self._known_armature_names_by_uid[uid], new_name)
            for uid, new_name in current_by_uid.items()
            if uid in self._known_armature_names_by_uid
            and self._known_armature_names_by_uid[uid] != new_name
        ]
        self._known_armature_names_by_uid = current_by_uid

        if renamed_pairs:
            from .. import custom_actions_quick
            # Data first (layouts, scene buttons/panels, session memory),
            # for the whole batch at once so a swap is handled safely.
            custom_actions_quick.migrate_rig_renames(
                [
                    (old_name, new_name, uid)
                    for uid, new_name in current_by_uid.items()
                    for old_name in [prev_by_uid.get(uid)]
                    if old_name is not None and old_name != new_name
                ]
            )

            if custom_actions_quick.rig_renames_conflict(renamed_pairs):
                # Swapped/cycled names: take each rig through a unique
                # temporary name so this Controller's own per-name state
                # (undo stacks, combo entries, armature_name) never has two
                # rigs sharing a name mid-way.
                import uuid
                staged = []
                for old_name, new_name in renamed_pairs:
                    temp = f"__rig_rename_{uuid.uuid4().hex}__"
                    self._handle_armature_renamed(old_name, temp)
                    staged.append((temp, new_name))
                for temp, new_name in staged:
                    self._handle_armature_renamed(temp, new_name)
            else:
                for old_name, new_name in renamed_pairs:
                    self._handle_armature_renamed(old_name, new_name)

        if renamed_pairs:
            # _handle_armature_renamed() updates the combo entry's text
            # in place - refresh current_items so the rest of this
            # method (and the deleted-armature diff below in particular)
            # sees the rig under its new name instead of treating the
            # name it no longer has as a removal.
            current_items = {combo.itemText(i) for i in range(combo.count())}

        self._purge_expired_armature_deletions(scene_armatures)

        if current_items == scene_armatures:
            # Nothing added/removed/renamed, but a rig's hide state may
            # have changed - that doesn't touch the name set at all, so
            # it wouldn't be caught by anything below. Keep the enabled/
            # disabled state of each entry in sync regardless of whether
            # a full rebuild happens this call.
            self._sync_armature_combo_enabled_state(combo, armature_objects)
            self._fallback_if_current_rig_hidden(armature_objects)

            # Also reconcile the *displayed selection* every tick, not
            # just on a full item-list rebuild - the scene's set of
            # armatures doesn't change just because the active object
            # switched from one of them to something else entirely (a
            # camera, say), but self.armature_name does, and this combo
            # needs to follow it every time either way. See
            # _sync_armature_combo_selection()'s own comment for the
            # three-way desync this fixes.
            self._sync_armature_combo_selection(combo)

            # See the matching comment near the end of this method - this
            # call also runs from a bpy.app.timers callback, so any Qt
            # follow-up to the enabled-state change above needs an
            # explicit flush rather than waiting on a real input event.
            self._repaint_armature_combo(combo)
            return

        # Any name that was selectable a moment ago but is no longer an
        # armature in this view layer. That is NOT the same as deleted:
        # the rig can also be excluded via its collection, live in another
        # scene/view layer, or belong to the file that was just replaced.
        # Only a rig that no longer exists as an armature object anywhere
        # in bpy.data is a candidate for having its saved data purged, and
        # even then only after ARMATURE_DELETE_GRACE_SECONDS (see
        # _purge_expired_armature_deletions()) so Ctrl+Z can bring it back
        # with its layout intact. Everything else just becomes
        # unselectable here while its saved data stays untouched.
        deleted_armatures = current_items - scene_armatures
        for name in deleted_armatures:
            existing = bpy.data.objects.get(name)
            if existing is None or existing.type != "ARMATURE":
                self._pending_armature_deletions.setdefault(
                    name, time.monotonic() + ARMATURE_DELETE_GRACE_SECONDS
                )

        combo.blockSignals(True)
        combo.clear()
        for name in sorted(scene_armatures):
            combo.addItem(name)
        combo.blockSignals(False)

        self._sync_armature_combo_enabled_state(combo, armature_objects)
        self._fallback_if_current_rig_hidden(armature_objects)

        # The armature currently loaded into the picker can itself be
        # one of the deleted ones (e.g. the user deletes the rig that's
        # actively showing in the picker, and nothing else becomes the
        # active object afterwards) or simply left the view layer. Its
        # saved data is NOT deleted here (see the grace-period purge
        # above), but self.armature_name/self.data still reference a rig
        # the picker can no longer act on - drop the in-memory state
        # rather than keep editing/saving against a rig that isn't
        # selectable. Clear it directly instead of going through
        # load_armature(None), which would call self.save() FIRST and
        # resurrect the very entry this method just removed.
        if self.armature_name in deleted_armatures:
            self.armature_name = None
            self.panel_name = None
            self.data = json_manager.new_panel_data()
            self.selected_bones = set()
            # This rig's entire undo stack (and any pending image
            # delete/backup riding along with each of its snapshots) was
            # already popped and resolved by the loop above - pending
            # image state now lives directly inside each snapshot (see
            # _stage_image_backup()), not as separate live controller
            # state, so there's nothing further to settle here.
            # Same reasoning as load_armature(): a pending
            # deselect-baseline or a tweaked appearance default from the
            # now-deleted armature must not leak into whatever rig gets
            # loaded next.
            self._pending_deselect_baseline = None
            self.default_control_size = 18
            self.default_control_shape = "CIRCLE"
            self.default_control_color = "GREEN"

            # Don't leave the picker sitting on an empty selection just
            # because the rig that happened to be loaded was the one
            # deleted - if any armatures are still left in the scene,
            # fall onto the first one in the (now-rebuilt, sorted)
            # dropdown, the same as if the user had picked it themselves.
            # self.armature_name is already None at this point, so
            # load_armature()'s save() call can't resurrect the deleted
            # rig's JSON entry.
            #
            # Deliberately call load_armature() directly here rather than
            # change_armature() - change_armature() also reassigns
            # view_layer.objects.active, touches object selection, and
            # can run bpy.ops.object.mode_set() through a context
            # override. This method runs from backend.poll_active_armature
            # (a bpy.app.timers callback), and poll_active_armature's own
            # existing armature-switch handling already calls
            # load_armature() directly from that same context for exactly
            # this reason - mutating scene selection/mode from here (as
            # opposed to just the Qt-side picker state) was what left Qt
            # without a real UI event to process afterwards, so nothing
            # visibly updated until an actual input event (e.g. the mouse
            # moving over the window) pumped the event loop.
            remaining = sorted(scene_armatures)
            if remaining:
                new_rig = armature_objects[remaining[0]]

                # refresh() (called by load_armature() below) syncs bone
                # visibility through backend.arm(), which returns
                # whatever armature is currently *cached* - not
                # necessarily this newly-picked one, since deliberately
                # not touching view_layer.objects.active above means
                # Blender's own active object may still be the deleted
                # rig (now stale) or nothing at all. Point the cache at
                # the rig we're actually loading so that sync lands on
                # the right object instead of a freed one or none.
                backend._CACHED_ARM = new_rig
                backend._CACHED_ARM_NAME = new_rig.name
                # Already loaded into the picker right here - stops the
                # next poll tick from reloading this same rig a second
                # time (see backend._QT_SYNCED_ARM_NAME).
                backend._QT_SYNCED_ARM_NAME = new_rig.name

                self.load_armature(new_rig)
            else:
                # No armatures left in the scene at all - the cached
                # active-armature reference (if it still points at the
                # one just deleted) is now stale. Clear it rather than
                # leaving a freed object behind for the next arm() call
                # to trip over.
                if backend._CACHED_ARM_NAME in deleted_armatures:
                    backend._CACHED_ARM = None
                    backend._CACHED_ARM_NAME = None

                self.refresh()
                self.window.set_no_selection_defaults()

        # Qt auto-selects the first item the moment a combo goes from
        # empty to non-empty, which happens on every clear()+addItem()
        # rebuild above - regardless of which rig is actually still
        # loaded. So even when the *deleted* armature wasn't the one
        # loaded in the picker (some other, unrelated rig was removed),
        # the dropdown could end up visibly showing the wrong name while
        # self.armature_name still correctly points at the real one.
        self._sync_armature_combo_selection(combo)

        # This whole rebuild ran from a bpy.app.timers callback, not a
        # real Qt-driven tick - on Windows, bqt shares Blender's native
        # message queue instead of running its own event loop, so
        # anything Qt needs to do as a follow-up to the mutations above
        # (repainting, delivering the internal itemChanged signal each
        # addItem()/setEnabled() emits on the combo's model, etc.) was
        # simply left queued until the next real OS input message (e.g.
        # a mouse move over the window) happened to pump it - which is
        # why the picker visibly looked unchanged right after a rig was
        # deleted until the mouse was moved over the UI. Flush explicitly
        # here instead of waiting for that to happen by chance.
        self._repaint_armature_combo(combo)

    def load_armature(self, rig):
        """Saves the current picker to JSON, then switches to the given
        armature (or none) and refreshes the UI.

        This only switches which RIG is active - self.armature_name and
        rig-level state (Quick Action buttons, the Rig dropdown). Which
        PANEL of that rig is loaded (self.panel_name/self.data) is left
        unresolved here (reset to a blank placeholder) and picked up a
        few lines later in the same poll tick by
        _ensure_default_panel_for_rig()/sync_panel_combo_items() calling
        _switch_to_panel() - at the point THIS method runs, the new
        rig's active panel isn't known yet (it may not even exist yet,
        for a rig that's never had one), so there's nothing real to load
        for it here.
        """

        # Persist whatever is currently loaded before switching away from it.
        self.save()

        self.selected_bones = set()

        # Nothing to do here for undo anymore - each (rig, panel) pair's
        # stack lives in self.undo_snapshots[self._snapshot_key(...)],
        # including any pending background-image delete/backup each of
        # its snapshots is carrying (see _record_undo_snapshot(),
        # _stage_image_backup()), so it's already exactly where it needs
        # to be regardless of which rig is currently loaded. Switching
        # rigs doesn't touch any of it.

        # A pending deselect-baseline (see deselect_all()) holds bone
        # names from whatever armature was loaded a moment ago. Left in
        # place, the *next* operation on the newly-loaded armature would
        # pick it up as this rig's "selection before the last click" -
        # wrong armature entirely, and if the two rigs happen to share a
        # bone name (e.g. two Rigify rigs both have "hand.L"), undo would
        # silently restore a selection that has nothing to do with this
        # rig's actual history.
        self._pending_deselect_baseline = None

        # The size/shape/color defaults are live-editable per
        # set_no_selection_defaults()/_set_selected_appearance() while
        # nothing is selected. Reset them to the addon's stock defaults
        # on every armature switch rather than carrying over whatever
        # the previous rig was left showing - otherwise a default tweaked
        # while working on one rig would silently apply to new controls
        # added on a completely different rig.
        self.default_control_size = 18
        self.default_control_shape = "CIRCLE"
        self.default_control_color = "GREEN"

        self.armature_name = None if rig is None else rig.name
        # Panel not resolved yet - see docstring. A blank placeholder
        # here is the correct thing to show for the brief moment before
        # _ensure_default_panel_for_rig()/sync_panel_combo_items()
        # resolve and load the real one later this same tick.
        self.panel_name = None
        self.data = json_manager.new_panel_data()

        # Rebuild Quick Action buttons for the NEW rig before refresh()
        # below - refresh() restores overlay_positions/calls
        # update_overlay_buttons() against whatever's currently
        # registered in canvas.OVERLAY_DRAG_GROUPS, and the buttons may
        # have changed (added/removed/renamed) since this rig was last
        # loaded (see PickerCanvas.sync_quick_actions()). Doing this
        # AFTER refresh() instead (as change_armature() used to) meant
        # refresh() painted once with the PREVIOUS rig's leftover
        # buttons still registered, then this call tore them down and
        # rebuilt the correct set a moment later - visible as one
        # button appearing, then the rest popping in a beat after.
        # Syncing first means refresh() only ever paints the final,
        # correct set - a single pass. self.armature_name was already
        # updated to the NEW rig earlier in this method, so this
        # correctly filters the Quick multi-command group to it.
        if self.window is not None:
            self.window.sync_quick_actions()

        self.refresh()

        if self.window is not None:
            self.window.set_no_selection_defaults()

            self._sync_armature_combo_items()

            # Keep the dropdown's displayed text in sync when the active
            # armature changes from outside the combo itself (e.g. clicking
            # a different rig - or something that isn't a rig at all - in
            # the 3D viewport, detected by backend.poll_active_armature).
            # Block signals while setting it so this doesn't re-trigger
            # change_armature() and recurse back into load_armature().
            #
            # target_text is "" whenever self.armature_name is None (no
            # rig active) - findText("") never matches a real item, so it
            # returns -1 exactly like "no selection" already does. That
            # used to be treated as "nothing to set", skipping
            # setCurrentIndex() entirely and leaving the combo still
            # showing whatever rig was selected before - which is why
            # selecting something other than a rig in the viewport looked
            # like it had no effect on the picker at all. -1 IS the
            # correct index for "no selection" (it's what shows the
            # "Select Rig" placeholder - see PickerComboBox.
            # setPlaceholderText() in main_window.py), so it has to be
            # passed through to setCurrentIndex() too, not just a
            # genuine match.
            combo = self.window.armature_combo
            target_text = self.armature_name or ""
            if combo.currentText() != target_text:
                combo.blockSignals(True)
                index = combo.findText(target_text) if target_text else -1
                combo.setCurrentIndex(index)
                combo.blockSignals(False)

    def change_armature(self, armature_name):
        rig = bpy.data.objects.get(armature_name)
        if rig is None or rig.type != "ARMATURE":
            return

        # rig can exist in bpy.data.objects (the whole .blend file) while
        # not actually being in the current scene's view layer - orphan
        # data, a different scene, etc. view_layer.objects.active = rig
        # below raises RuntimeError for exactly that case, so bail out
        # safely instead of crashing, and let the combo/JSON cleanup
        # that already runs on the poll timer catch up and drop this
        # stale entry from the dropdown.
        view_layer = bpy.context.view_layer
        if armature_name not in view_layer.objects or not backend.is_rig_visible(rig):
            # Not in this view layer, or hidden - neither can be made
            # active. Re-sync so the dropdown drops the stale entry.
            self._sync_armature_combo_items()
            return

        backend._CACHED_ARM = rig
        backend._CACHED_ARM_NAME = rig.name
        # Also mark this rig as already synced to the Qt UI (see
        # backend._QT_SYNCED_ARM_NAME's own comment) - self.load_armature()
        # a few lines down already does the actual sync, right now, from
        # this same call. Without this, poll_active_armature()'s next
        # tick would see this rig's name still didn't match
        # _QT_SYNCED_ARM_NAME, treat that as "the Qt UI hasn't heard about
        # this rig yet", and reload it a second time right behind this
        # one - redundant, and a visible flicker (canvas repaint, combo
        # reselect, etc.) for no reason.
        backend._QT_SYNCED_ARM_NAME = rig.name

        for obj in view_layer.objects:
            if obj != rig and obj.select_get():
                obj.select_set(False)

        view_layer.objects.active = rig
        rig.select_set(True)

        self.load_armature(rig)

        # Switching rigs shouldn't carry over the previous rig's hidden/
        # selected bones - reveal and deselect on the new armature too.
        self._show_all_and_deselect()

        # _show_all_and_deselect() runs pose.reveal/pose.select_all under
        # the hood, and those operators only poll() in Pose Mode - so it
        # calls ensure_pose_mode() to switch into Pose Mode first, but
        # never switches back out afterward. That left the rig sitting in
        # Pose Mode after every armature switch that happened to have a
        # 3D-viewport override available (hence only "sometimes" - it
        # depended on whether that override could be built). Switching
        # rigs from the picker should always land back in Object Mode.
        if rig.mode != 'OBJECT':
            override = backend.get_3d_override(bpy.context, rig)
            if override:
                with bpy.context.temp_override(**override):
                    bpy.ops.object.mode_set(mode='OBJECT')

    def change_panel(self, panel_name):
        """Keeps the Qt picker's own Panel dropdown (stale_combo) and the
        Manage Panels N-panel section's selected row
        (Scene.rp_quick_action_settings.panels/panels_index) in
        two-way sync - the same guarded-flag pattern change_armature()'s
        counterpart (_on_quick_active_rig_changed() in
        custom_actions_quick.py) uses for the Rig dropdown. Called both
        when the user picks a different item in the Qt combo itself
        (wired up in main_window.py) and, via panels_index's own update
        callback, when the user selects a different row in the N-panel
        list instead - either direction pushes the new selection into
        the other side. The Qt combo's actual item list (add/rename/
        remove made from the N-panel) is kept current separately by
        sync_panel_combo_items(), called every poll tick.
        """
        from .. import custom_actions_quick

        # Load this panel's own data (and save whatever was loaded
        # before it) right away, rather than waiting for the next poll
        # tick's sync_panel_combo_items() to notice - this is the direct,
        # user-driven pick (Qt combo or N-panel list), so it should feel
        # instant. No-ops if this panel is already the one loaded (e.g.
        # called from the settings.panels_index branch below when
        # THIS call is itself what triggered that index change).
        self._switch_to_panel(panel_name)

        if self.window is not None and self.window.stale_combo.currentText() != panel_name:
            self.window.stale_combo.blockSignals(True)
            try:
                self.window.stale_combo.setCurrentText(panel_name)
            finally:
                self.window.stale_combo.blockSignals(False)

        scene = bpy.context.scene
        settings = getattr(scene, "rp_quick_action_settings", None) if scene else None
        if settings is None:
            return

        # Panels are rig-independent (each rig keeps its own set - Face,
        # Body, etc.), so different rigs can easily have same-named
        # panels ("Panel 1" on both). Matching by name alone here could
        # jump to a completely different rig's panel that just happens
        # to share this one's name - scope the lookup to panels
        # belonging to whichever rig is actually loaded right now.
        current_rig_name = self.armature_name or ""
        index = next(
            (
                i for i, p in enumerate(settings.panels)
                if p.armature_name == current_rig_name and p.name == panel_name
            ),
            -1,
        )
        if index == -1 or settings.panels_index == index:
            return

        custom_actions_quick._SYNCING_QUICK_PANEL_DROPDOWN = True
        try:
            settings.panels_index = index
        finally:
            custom_actions_quick._SYNCING_QUICK_PANEL_DROPDOWN = False

    def sync_panel_combo_items(self):
        """Keeps the Qt picker's own Panel dropdown (stale_combo) in step
        with whichever rig is currently loaded's own panels -
        Scene.rp_quick_action_settings.panels filtered to
        armature_name == self.armature_name, the Manage Panels
        N-panel section's add/rename/remove list for THIS rig
        specifically (each rig keeps its own independent set, the same
        way Section 1's multi-command buttons are scoped per rig) - the
        same way _sync_armature_combo_items() keeps the Rig dropdown in
        step with the scene's actual armatures. Called every poll tick
        (see backend.poll_active_armature()), so an add/rename/remove,
        or switching to a different rig entirely, shows up here without
        needing the window to be reopened.
        """
        if self.window is None:
            return

        scene = bpy.context.scene
        settings = getattr(scene, "rp_quick_action_settings", None) if scene else None
        if settings is None:
            return

        current_rig_name = self.armature_name or ""
        combo = self.window.stale_combo

        # No rig loaded -> nothing to pick a panel for (panels are
        # scoped per rig). Disable the combo and show "Select" rather
        # than "No Panel", which would wrongly imply a rig is loaded
        # and just happens to have no panels of its own. Cheap no-op
        # once already correct, so safe every poll tick.
        should_be_enabled = bool(current_rig_name)
        if combo.isEnabled() != should_be_enabled:
            combo.setEnabled(should_be_enabled)
            combo.setPlaceholderText("No Panel" if should_be_enabled else "Select")

        names = [p.name for p in settings.panels if p.armature_name == current_rig_name]
        current_items = [combo.itemText(i) for i in range(combo.count())]

        if current_items != names:
            combo.blockSignals(True)
            try:
                combo.clear()
                combo.addItems(names)
            finally:
                combo.blockSignals(False)

        resolved_panel_name = None
        if 0 <= settings.panels_index < len(settings.panels):
            selected_panel = settings.panels[settings.panels_index]
            if selected_panel.armature_name == current_rig_name:
                resolved_panel_name = selected_panel.name

        # Load/save this (rig, panel) pair's own data - the safety net
        # for switches that don't go through change_panel() directly:
        # a rig switch landing on a different panel (load_armature()
        # itself runs before the new rig's active panel is even known),
        # or _ensure_default_panel_for_rig() having just created a rig's
        # very first panel this same tick, right before this method
        # runs. No-ops if this pair is already what's loaded.
        self._switch_to_panel(resolved_panel_name)

        # Safety net for the Quick multi-command buttons: re-filter the
        # canvas whenever the loaded (rig, panel) pair differs from the
        # one it was last filtered for (covers switches that set
        # self.panel_name directly, or that skipped _switch_to_panel()'s
        # own sync because the name already matched).
        # LIMIT: this compares only the (rig, panel) NAMES, and runs once
        # per poll tick. It re-filters which buttons are shown; it doesn't
        # detect a button's own fields (label, toggle state) changing -
        # those are refreshed by the operators and the frame-change
        # handler that change them.
        quick_key = (self.armature_name, self.panel_name)
        if quick_key != self._quick_synced_key:
            self._quick_synced_key = quick_key
            self.window.sync_quick_actions()
            # Rig / Panel labels + button list in the N-panel are drawn
            # from scene data; a change made from the Qt picker or the
            # viewport doesn't redraw that region by itself.
            self._redraw_all_areas()

        if resolved_panel_name is not None and combo.currentText() != resolved_panel_name:
            combo.blockSignals(True)
            try:
                combo.setCurrentText(resolved_panel_name)
            finally:
                combo.blockSignals(False)
        elif resolved_panel_name is None and combo.currentIndex() != -1:
            # panels_index is still pointing at a different rig's
            # panel (e.g. this tick's _ensure_default_panel_for_rig()
            # hasn't run yet, or no rig is active at all right now) -
            # nothing of this rig's own to show as selected.
            combo.blockSignals(True)
            try:
                combo.setCurrentIndex(-1)
            finally:
                combo.blockSignals(False)

    def _load_panel_defaults(self):
        """Sets the "what a new control looks like" defaults from the loaded
        panel's own saved values (stock 18 / Circle / Green if it has none).
        """
        saved = self.data.get("defaults") if isinstance(self.data, dict) else None
        if not isinstance(saved, dict):
            saved = {}
        size = saved.get("size")
        self.default_control_size = size if isinstance(size, int) and not isinstance(size, bool) else 18
        self.default_control_shape = saved.get("shape") if isinstance(saved.get("shape"), str) else "CIRCLE"
        self.default_control_color = saved.get("color") if isinstance(saved.get("color"), str) else "GREEN"

    def _save_panel_defaults(self):
        """Stores the current new-control defaults with the loaded panel
        and writes them to disk right away (auto-save)."""
        if not (self.armature_name and self.panel_name):
            return
        wanted = {
            "size": self.default_control_size,
            "shape": self.default_control_shape,
            "color": self.default_control_color,
        }
        stock = {"size": 18, "shape": "CIRCLE", "color": "GREEN"}
        if self.data.get("defaults") == wanted or (
            "defaults" not in self.data and wanted == stock
        ):
            return
        self.data["defaults"] = wanted
        self.save()

    def save(self):
        """Writes the current in-memory data to rig_picker_data.json,
        under the currently loaded (rig, panel) pair."""
        if self.armature_name and self.panel_name:
            json_manager.save_panel_data(self.armature_name, self.panel_name, self.data)

    def find_item(self, bone_name):
        return next(
            (item for item in self.data["items"] if item["bone_name"] == bone_name),
            None,
        )

    def refresh_custom_action_enabled_states(self):
        """Grays out a Quick Action's picker-canvas button (or, for a
        Toggle, its switch + flanking name label) whenever the current
        selection wouldn't match any of that action's rules - i.e.
        clicking it right now would just no-op with a
        self.report({'WARNING'}, ...) that's easy to miss, and outright
        invisible from this floating window.

        Uses self.selected_bones (this controller's own tracked
        selection) rather than re-querying bpy.context directly, so
        every overlay control stays in agreement about what's currently
        selected.

        Called from here (every place self.selected_bones changes) and
        again from PickerCanvas.sync_quick_actions() itself, so a
        freshly added/rebuilt button starts in the correct state too,
        instead of defaulting to enabled until the next selection
        change.
        """
        if self.window is None:
            return

        # Snap each Quick Toggle's on/off position to whatever its
        # now-relevant rule's bone/property actually holds, not just its
        # enabled/disabled look - selecting a different control (e.g.
        # switching from a Left to a Right bone under the same action)
        # can change which rule applies, and therefore which bone's value
        # the toggle should be showing, same as changing frames does.
        # Passing self.selected_bones explicitly (rather than letting it
        # fall back to bpy.context.selected_pose_bones) matters here:
        # this runs as part of handling the selection change itself, so
        # the live viewport selection can still be a step behind this
        # controller's own already-updated selected_bones at this exact
        # point - reading bpy.context here risked matching against the
        # selection this change is replacing, not the new one.
        from .. import custom_actions_quick
        custom_actions_quick.sync_quick_toggle_states(bpy.context.scene, self.selected_bones)

        # Quick single-command buttons - always enabled (selection plays
        # no part), re-asserted here so none can ever be left disabled.
        self.refresh_quick_single_action_enabled_states()

        # Same for the Quick multi-command buttons.
        self.refresh_quick_multi_action_enabled_states()

    def refresh_quick_single_action_enabled_states(self):
        """Keeps every Quick single-command button in
        custom_actions_quick.py enabled. They never grey out for
        selection reasons (not with nothing selected, not for any other
        reason - the multi-command buttons are the ones that depend on the
        selection, see refresh_quick_multi_action_enabled_states() below).
        The one exception is a MALFORMED button - 2+ commands on something
        that must hold exactly one (see
        custom_actions_quick.single_action_rule()) - which can't run, so it
        is greyed out with a tooltip saying why, rather than looking
        clickable and then refusing.

        Still called from refresh_custom_action_enabled_states() above
        and from PickerCanvas.sync_quick_actions(), so a button that was
        just created or rebuilt is explicitly set enabled too.
        """
        if self.window is None:
            return

        settings = getattr(bpy.context.scene, "rp_quick_action_settings", None)
        actions = settings.single_actions if settings is not None else []

        from .. import custom_actions_quick

        problems_by_uid = {}
        for action in actions:
            problem = custom_actions_quick.single_action_problem(action)
            if problem:
                problems_by_uid[action.uid] = problem
        enabled_by_uid = {
            action.uid: action.uid not in problems_by_uid for action in actions
        }

        self.window.control_list.container.update_quick_single_actions_enabled(
            enabled_by_uid, problems_by_uid
        )

    def refresh_quick_multi_action_enabled_states(self):
        """Prototype counterpart to refresh_custom_action_enabled_states()
        above, for the Quick multi-command buttons in
        custom_actions_quick.py - grays out a button's canvas widget
        whenever the current selection doesn't match any of that
        button's rules, using custom_actions_quick._quick_find_matching_rule()
        (the exact same check the N-panel's draw_multi_action() and
        RP_OT_quick_run_multi_action itself use), so "looks clickable"
        and "is clickable" can't drift apart on the picker canvas
        either.

        Only actions scoped to this controller's own (rig, panel) pair
        are considered - the canvas only ever shows those anyway (see
        PickerCanvas.sync_quick_actions()).

        Called from refresh_custom_action_enabled_states() above (so
        every place that already refreshes the real buttons' enabled
        state refreshes these too) and again from PickerCanvas.
        sync_quick_actions() itself, so a freshly added/rebuilt or
        just-switched-back-into-view button starts in the correct
        state too.
        """
        if self.window is None:
            return

        from .. import custom_actions_quick

        settings = getattr(bpy.context.scene, "rp_quick_action_settings", None)
        actions = [
            action for action in (settings.multi_actions if settings is not None else [])
            if custom_actions_quick._quick_multi_action_visible(
                settings, action, self.armature_name, self.panel_name
            )
        ]

        enabled_by_uid = {
            action.uid: custom_actions_quick._quick_find_matching_rule(
                self.selected_bones, action.rules
            )[0] is not None
            for action in actions
        }

        # Validate every button's Control shape bones against the live rig,
        # so a button that can't enable because a rule's bone was renamed/
        # deleted says why (tooltip + warning look on the canvas) instead of
        # just sitting there disabled. Skipped (nothing flagged) unless the
        # cached rig really is the one this controller has loaded.
        rig = backend.arm()
        if rig is None or rig.type != "ARMATURE" or rig.name != self.armature_name:
            rig = None
        problems_by_uid = {}
        for action in actions:
            missing = custom_actions_quick.quick_action_missing_bones(action, rig)
            if missing:
                problems_by_uid[action.uid] = (
                    "Control bones missing on the rig (renamed or deleted?):\n"
                    + custom_actions_quick.quick_missing_bones_text(missing)
                )

        self.window.control_list.container.update_quick_multi_actions_enabled(
            enabled_by_uid, problems_by_uid
        )

    def refresh_quick_multi_action_bone_validity(self):
        """Re-validates the Quick multi-command buttons' Control shape bones
        when the active rig's bone NAMES change - a rename, a delete, an undo
        that brings a bone back, a script adding one. None of those fire a
        signal this addon sees, so this runs every poll tick (see
        backend.poll_active_armature()) but is a no-op unless the set of
        names actually differs from the last pass (one set comparison).

        A rig switch is already covered: load_armature() -> refresh() ends in
        refresh_quick_multi_action_enabled_states(), which validates against
        whichever rig is loaded. This only adds the "same rig, its bones
        changed underneath us" case, and re-validates the N-panel too (which
        validates live on every redraw, but needs one to be requested).
        """
        rig = backend.arm()
        if rig is None or rig.type != "ARMATURE" or rig.name != self.armature_name:
            fingerprint = None
        else:
            fingerprint = (rig.name, rig.session_uid, frozenset(rig.pose.bones.keys()))

        if fingerprint == self._quick_bone_fingerprint:
            return
        previous = self._quick_bone_fingerprint
        self._quick_bone_fingerprint = fingerprint
        if previous is None or fingerprint is None:
            # First look at a rig (or none at all): load/refresh already
            # validated it; nothing changed underneath us.
            return

        self.refresh_quick_multi_action_enabled_states()
        self._redraw_all_areas()

    def _update_action_controls_enabled(self):
        """Add Selected / Clear All / Delete, and the Symmetry
        checkbox, all operate on controls placed relative
        to a background image - so keep them disabled until this rig
        actually has one, rather than letting someone add/delete
        controls or enable symmetry against a canvas with nothing to
        position against. Capture View is gated on a coarser condition
        (having a rig loaded AT ALL, regardless of whether it has a
        background yet) rather than has_background itself: it's what
        produces the background in the first place, so gating it the
        same way would make it impossible to ever get past that state
        for a rig that's actually loaded - but with no rig loaded at
        all (nothing armature-shaped active in the viewport, or an
        object of some other type selected instead) there's no rig for
        it to capture a background FOR, so it stays disabled along
        with everything else until a rig is picked.

        The picker canvas itself (self.window.control_list) is gated
        the same coarse way, purely so the whole panel visibly reads
        as "off" the moment no rig is active - not because there's
        anything left to interact with there anyway, since refresh()
        already empties it in that state.

        Delete additionally requires a non-empty self.selected_bones -
        it's a no-op with nothing selected (see delete_selected()'s own
        early "if not self.selected_bones: return"), so leaving it
        clickable in that state invited a confusing no-op click. Call
        this after every place that changes self.selected_bones, not
        just after background/armature changes.
        """
        if self.window is None:
            return

        has_rig = bool(self.armature_name)

        # Gate on whether an image is actually loaded onto the canvas
        # right now, not on self.data["background"] being a non-empty
        # path string. A stored path can point at a file that no longer
        # loads (missing, moved .blend, corrupted) - set_background()
        # falls back to a clean canvas in that case, and this must agree
        # with what's on screen rather than what's merely on record, or
        # these stay enabled against an image the user can't see.
        has_background = self.window.control_list.container.background is not None

        # capture_view() silently does nothing without a panel loaded (the
        # background is stored per panel), so a rig with no panel yet must
        # not leave the button looking clickable.
        self.window.capture_button.setEnabled(has_rig and bool(self.panel_name))
        self.window.control_list.setEnabled(has_rig)

        self.window.add_button.setEnabled(has_background)
        self.window.clear_button.setEnabled(has_background)
        self.window.delete_button.setEnabled(
            has_background and bool(self.selected_bones)
        )

        self.window.symmetry_icon.setEnabled(has_background)
        self.window.symmetry_toggle.setEnabled(has_background)

        # The N-panel's multi-command Add button is gated on the same
        # "panel has an image" state (custom_actions_quick.
        # _panel_has_image) - ask Blender to redraw its UI so it
        # enables/disables right away instead of on the next mouse-over.
        # Only when that state actually flipped (this method runs on every
        # selection change), and only the areas that can show the panel.
        if has_background != getattr(self, "_last_has_background", None):
            self._last_has_background = has_background
            try:
                for blender_window in bpy.context.window_manager.windows:
                    for area in blender_window.screen.areas:
                        if area.type == 'VIEW_3D':
                            area.tag_redraw()
            except Exception:
                pass


    def refresh(self):
        if self.window is None:
            return


        self.window.control_list.clear_controls()

        canvas = self.window.control_list.container

        if self.data.get("background"):
            # Deliberately NOT clearing/persisting self.data["background"]
            # here if this fails to load. The very first refresh() of a
            # session runs from RigPickerWindow.__init__() before the
            # window has ever been shown (see _needs_post_show_relayout),
            # which is an unreliable moment for a QImage load - and a
            # second, more reliable refresh() is already scheduled right
            # after the window's first real showEvent() to correct
            # exactly this kind of early hiccup. Wiping the stored path
            # to disk the moment this first attempt fails would destroy
            # the correct reference before that second, working attempt
            # ever got a chance to use it - permanently losing the
            # background on every close/reopen even though the file was
            # fine the whole time. A load that fails here just leaves the
            # canvas empty for now; _update_action_controls_enabled()
            # already reads the canvas's actual state (not this string),
            # so the UI still won't lie about having an image loaded.
            self.window.control_list.set_background(
                self.data["background"],
                self.data.get("image_offset_x", 0.0),
                self.data.get("image_offset_y", 0.0),
            )
        else:
            self.window.control_list.clear_background()

        canvas.symmetry_enabled = self.data.get("symmetry", False)
        canvas.symmetry_x = self.data.get("symmetry_x", -1.0)

        # Covers the same gap as capture_view() - this rig may have had
        # Symmetry enabled before it ever had a background (or before
        # that background's data made it into a save), so re-check
        # whether a default symmetry_x can be filled in now that the
        # background above has just been (re)loaded.
        self._ensure_symmetry_default()

        # Sync the toggle button to match the loaded armature's stored
        # value. blockSignals() prevents this from re-triggering
        # toggle_symmetry(), which would otherwise immediately re-save
        # the value we just loaded (harmless, but pointless) and could
        # clobber symmetry_x if the background isn't set up yet.
        self.window.symmetry_toggle.blockSignals(True)
        self.window.symmetry_toggle.setChecked(
            self.data.get("symmetry", False)
        )
        self.window.symmetry_toggle.blockSignals(False)

        # Restore each draggable overlay widget's (currently just Quick
        # Action buttons/toggles - see sync_quick_actions()) saved
        # custom position (if it's ever been dragged) - same idea as
        # symmetry_x above, just per-widget. A key that's missing (or
        # whose stored value is None/empty) leaves that widget on the
        # default stacked layout, matching a rig that's never had this
        # widget moved.
        saved_overlay_positions = self.data.get("overlay_positions") or {}
        canvas.overlay_positions = {
            key: (
                QPoint(saved_overlay_positions[key][0], saved_overlay_positions[key][1])
                if saved_overlay_positions.get(key)
                else None
            )
            for key in canvas.OVERLAY_DRAG_GROUPS
        }

        canvas.update_overlay_buttons()

        self._update_action_controls_enabled()

        positions_pinned = False

        for item in self.data["items"]:
            had_no_position = item["x"] < 0 or item["y"] < 0

            x = None if item["x"] < 0 else item["x"]
            y = None if item["y"] < 0 else item["y"]

            control = self.window.control_list.add_control(
                item["bone_name"],
                x,
                y,
                item.get("control_size", 18),
                item.get("control_shape", "CIRCLE"),
                item.get("control_color", "GREEN"),
            )

            # A control with no saved position yet gets auto-placed by
            # add_control()'s own fallback grid: the first default slot
            # that isn't already occupied by another control's actual
            # position (see PickerCanvas._next_grid_position()). Never
            # persisting that computed position back into self.data
            # meant every single refresh() recomputed it from scratch -
            # so deleting an earlier control (or adding a new one
            # anywhere before it) could shift which slot a still-
            # unpositioned control landed on, making it silently jump
            # to a new spot even though nothing about it was touched.
            # Saving it back the very first time locks that control's
            # position in for good from then on - exactly as if the
            # user had dragged it there themselves - so later
            # deletions/additions elsewhere in the list can no longer
            # move it.
            if had_no_position and control is not None:
                item["x"] = control.image_position.x()
                item["y"] = control.image_position.y()
                positions_pinned = True

            # Restore whether this position came from an actual drag (or
            # the symmetric reflection of one) vs. still just sitting on
            # its auto-placed grid default - add_selected()'s mirroring
            # check relies on this to tell the two apart.
            if control is not None:
                control.manually_positioned = item.get("manually_positioned", False)

            widget = self.window.control_list.controls[item["bone_name"]]
            self.window.connect_item(widget)

            # refresh() rebuilds every control widget from scratch, so a
            # freshly-created widget always starts with active=False
            # regardless of self.selected_bones. Most callers (undo(),
            # load_armature(), clear_all(), delete_selected()) already
            # clear or explicitly re-apply selected_bones right after
            # calling refresh(), so this was masked there - but
            # add_selected() calls refresh() without touching
            # selected_bones at all, so any control that was selected
            # right before Add Selected silently lost its highlight in
            # the UI (while staying selected in self.selected_bones and
            # in the viewport). Re-apply it here so refresh() never
            # drops selection state on its own.
            widget.active = item["bone_name"] in self.selected_bones

        self.window.control_list.container.layout_controls()
        self.window.control_list.container.update()

        if positions_pinned:
            self.save()

        # Apply each item's stored visibility to the actual pose bone in
        # the viewport. Visibility now lives in self.data like any other
        # picker field, so it's captured by _record_undo_snapshot() and
        # restored automatically here whenever refresh() runs (including
        # from undo()) - no change needed to undo()'s own logic.
        rig = backend.arm()
        if rig is not None:
            for item in self.data["items"]:
                pb = rig.pose.bones.get(item["bone_name"])
                if pb is not None:
                    pb.bone.hide = item.get("hidden", False)

            self._redraw_all_areas()

        # Every widget refresh() just (re)built starts fully enabled
        # (CircleControl's own default) regardless of whether its
        # bone's collection happens to be hidden right now - set that
        # correctly immediately rather than waiting for the next poll
        # tick (see refresh_bone_collection_visibility_states(), which
        # also runs on every poll tick to catch a collection's
        # visibility being toggled directly in the Armature Properties
        # panel, something no picker action would otherwise notice).
        self.refresh_bone_collection_visibility_states(rig=rig)

        self.refresh_custom_action_enabled_states()

    def refresh_bone_collection_visibility_states(self, rig=None):
        """Keeps every control widget's enabled/disabled state in sync
        with whether its bone's collection is currently visible in
        Blender (see CircleControl.set_bone_collection_visible()) - a
        hidden-collection bone can't actually be selected in the
        viewport at all, so its control shouldn't be clickable either.

        Called from refresh() (so a freshly (re)built canvas starts
        correct immediately) and unconditionally from
        backend.poll_active_armature() every tick, since toggling a
        collection's visibility in the Armature Properties panel
        doesn't go through any picker action at all - nothing else
        would ever notice that change without polling for it.
        """
        if self.window is None:
            return

        if rig is None:
            rig = backend.arm()

        if not rig or rig.type != "ARMATURE" or rig.name != self.armature_name:
            return

        for name, widget in self.window.control_list.controls.items():
            pb = rig.pose.bones.get(name)
            visible = pb is None or backend.is_bone_collection_visible(pb)
            widget.set_bone_collection_visible(visible)

    # ---------------------------------------------------------

    def add_selected(self):
        from ..backend import arm, mirror_name, control_dimensions, is_bone_collection_visible

        rig = arm()
        if not rig:
            return

        existing = {item["bone_name"] for item in self.data["items"]}

        # Diagnostics only - so a click that adds nothing isn't a silent
        # no-op. Selecting a bone in the viewport doesn't guarantee
        # add_selected() will pick it up: it's skipped if it's already
        # in the picker (skipped_existing) or if its collection is
        # hidden (skipped_hidden, see the comment below). Reported to
        # the user below when nothing ends up getting added, so "I
        # selected it and nothing happened" is diagnosable without
        # digging into the console.
        selected_count = 0
        skipped_existing = []
        skipped_hidden = []
        snapshotted = False

        for pb in rig.pose.bones:
            if not pb.select:
                continue

            selected_count += 1

            # Rig-generation tools (Rigify and similar) commonly leave
            # internal DEF/MCH/ORG bones flagged pb.select = True behind
            # the scenes, even though they sit in hidden bone
            # collections the user never sees or clicks. Without a check
            # here, those invisible "selected" bones get swept into Add
            # Selected too - hence dozens of controls appearing after a
            # fresh rig generation with nothing visibly selected.
            #
            # Bone Collections (Blender 4.0+) replaced the old armature
            # layers system, and collection visibility (bone.collections[
            # i].is_visible) is a completely separate flag from the
            # individual bone.hide toggle - Blender does not sync them.
            # Checking pb.bone.hide here (as a stand-in for "is this
            # bone's collection hidden") is therefore wrong: it misses
            # bones that really are only hidden via a collection, and it
            # wrongly blocks any bone that merely has a stray bone.hide
            # flag set. Checking collection visibility directly (via the
            # shared is_bone_collection_visible()) fixes both cases. A
            # bone with no collections at all is never hidden by this
            # check, since collections only ever restrict visibility for
            # bones actually assigned to one.
            if not is_bone_collection_visible(pb):
                skipped_hidden.append(pb.name)
                continue

            if pb.name in existing:
                skipped_existing.append(pb.name)
                continue

            new_item = {
                "bone_name": pb.name,
                "label": pb.name,
                "x": -1.0,
                "y": -1.0,
                "control_size": self.default_control_size,
                "control_shape": self.default_control_shape,
                "control_color": self.default_control_color,
                "hidden": False,
                "manually_positioned": False,
            }

            mirror = mirror_name(pb.name)

            if self.data.get("symmetry") and mirror is not None:
                mirror_item = self.find_item(mirror)

                # Only mirror off of a position the user actually put
                # there on purpose (a real drag, or the symmetric
                # reflection of one) - not wherever the mirror control
                # happens to be currently sitting from its own auto-
                # placed grid default. Otherwise a fresh, never-dragged
                # mirror's incidental grid slot would get treated as a
                # deliberate symmetry anchor, pulling this new control
                # off to mirror a coincidence instead of a real position.
                # A control not yet flushed to self.data (mirror_item
                # missing/stale) is checked on its live widget first,
                # since that's the more current source during this same
                # add_selected() call - see the sentinel-gap note this
                # replaced, now gated on manually_positioned as well.
                mirror_widget = self.window.control_list.controls.get(mirror)

                if mirror_widget is not None and getattr(
                    mirror_widget, "manually_positioned", False
                ):
                    mirror_x = mirror_widget.image_position.x()
                    mirror_y = mirror_widget.image_position.y()
                    mirror_has_position = True
                elif mirror_item and mirror_item.get("manually_positioned"):
                    mirror_x = mirror_item["x"]
                    mirror_y = mirror_item["y"]
                    mirror_has_position = True
                else:
                    mirror_x = mirror_y = None
                    mirror_has_position = False

                if mirror_item and mirror_has_position:

                    # Copy appearance first
                    new_item["control_size"] = mirror_item["control_size"]
                    new_item["control_shape"] = mirror_item["control_shape"]
                    new_item["control_color"] = mirror_item["control_color"]

                    width, _height = control_dimensions(
                        new_item["control_size"], new_item["control_shape"]
                    )

                    mirror_center = mirror_x + width / 2.0
                    new_center = 2.0 * self.data["symmetry_x"] - mirror_center

                    new_item["x"] = new_center - width / 2.0
                    new_item["y"] = mirror_y
                    new_item["manually_positioned"] = True

            # Snapshot lazily, right before the first control this call
            # actually adds - not up front, before we even know whether
            # this bone will get skipped (already on the picker, hidden
            # collection) or nothing was selected at all. Snapshotting
            # unconditionally meant every "Add Selected" click pushed an
            # undo entry even on a click that added nothing, wasting a
            # Ctrl+Z press on a no-op step.
            if not snapshotted:
                self._record_undo_snapshot()
                snapshotted = True

            self.data["items"].append(new_item)

        self.save()
        self.refresh()

        added_count = selected_count - len(skipped_existing) - len(skipped_hidden)
        if added_count == 0:
            from PySide6.QtWidgets import QMessageBox

            if selected_count == 0:
                message = "No bones are selected in the viewport."
            else:
                reasons = []
                if skipped_existing:
                    reasons.append(
                        "already on the picker: " + ", ".join(skipped_existing)
                    )
                if skipped_hidden:
                    reasons.append(
                        "in a hidden bone collection: " + ", ".join(skipped_hidden)
                    )
                message = (
                    "Nothing was added - every selected bone is "
                    + "; and ".join(reasons)
                    + "."
                )
                if skipped_hidden:
                    message += (
                        " Unhide its collection in the Armature's Bone "
                        "Collections/Layers to add it."
                    )

            QMessageBox.information(self.window, "Add Selected", message)
    # ---------------------------------------------------------

    def select_control(self, bone_name, shift=False, was_drag=False):
        # Plain selection clicks push their own selection_only snapshot,
        # so consecutive selects each stay individually undoable (see
        # _record_undo_snapshot()'s docstring) - but if a real
        # data-changing operation comes right after, that operation's
        # own push discards this one rather than stacking on top of it.
        #
        # was_drag is True when this call is arriving via CircleControl's
        # `clicked` signal right after a real drag - in that case
        # CircleControl's own mouseMoveEvent already took a snapshot the
        # moment movement crossed the drag threshold, capturing state
        # *before* the drag started (that one IS a real, undoable data
        # change - the control's position moved), so don't push a second,
        # selection_only one on top of it here.
        if shift:
            if bone_name in self.selected_bones:
                new_selection = self.selected_bones - {bone_name}
            else:
                new_selection = self.selected_bones | {bone_name}
        else:
            new_selection = {bone_name}

        # Re-clicking a control that's already the sole selection (or
        # shift-clicking one that's already part of it, in a way that
        # doesn't add/remove anything) doesn't actually change anything -
        # pushing a snapshot here would just be a no-op undo step that
        # silently eats one Ctrl+Z press for nothing.
        if not was_drag and new_selection != self.selected_bones:
            self._record_undo_snapshot(selection_only=True)

        self.selected_bones = new_selection

        for name, widget in self.window.control_list.controls.items():
            widget.active = (name in self.selected_bones)
            widget.update()

        # Update UI comboboxes to match selected control appearance
        item = self.find_item(bone_name)

        if item:
            self.window.set_selected_control(
                item["control_size"],
                item["control_shape"],
                item["control_color"],
            )

        self._update_action_controls_enabled()

        # Push the new selection through the same path undo()/redo() use
        # (_sync_viewport_selection()), not a standalone bpy.ops.rp.select
        # call - that direct call never updated backend._LAST_SELECTED_BONES,
        # the value poll_active_armature() compares the viewport against
        # every ~0.15s to detect an out-of-picker selection change. Left
        # stale after every single picker click, it meant: if rp.select
        # ever silently failed (its own get_3d_override() can return None
        # and the operator just returns CANCELLED, no exception, no
        # visible error) so the real viewport selection never actually
        # became what was just clicked, nothing corrected
        # _LAST_SELECTED_BONES either - leaving the OLD, still-actually-
        # selected bones in the viewport free to get silently synced back
        # into self.selected_bones on the very next poll tick, overwriting
        # this click before the user's next action (e.g. Delete) ever saw
        # it. Going through _sync_viewport_selection() keeps
        # _LAST_SELECTED_BONES correctly in lockstep with every picker-
        # side selection change, the same way undo()/redo() already do.
        self._sync_viewport_selection()

        self.refresh_custom_action_enabled_states()

    def box_select(self, bone_names):
        """Adds every control whose on-canvas geometry intersected a
        Shift+drag rubber-band box (see PickerCanvas.mousePressEvent/
        mouseMoveEvent/mouseReleaseEvent) to the current selection.

        Always additive - union with whatever was already selected, the
        same way a single Shift+click adds to the selection in
        select_control() above. Box select is only ever started with
        Shift held in the first place (a plain drag on empty canvas
        does the usual background-image drag instead), so there's no
        non-additive box-select gesture to support here - this never
        replaces the existing selection the way a shift-less click
        does in select_control().
        """
        new_selection = self.selected_bones | set(bone_names)

        # Every hit control was already selected - nothing actually
        # changes, so don't push a no-op undo step.
        if new_selection == self.selected_bones:
            return

        self._record_undo_snapshot(selection_only=True)

        self.selected_bones = new_selection

        for name, widget in self.window.control_list.controls.items():
            widget.active = (name in self.selected_bones)
            widget.update()

        # Match select_control()'s combobox behavior: with multiple
        # controls now selected, show whichever one was box-selected
        # last (arbitrary but stable) so the size/shape/color fields
        # reflect something concrete instead of being left showing
        # whatever single control was selected before this box select.
        item = self.find_item(bone_names[-1])

        if item:
            self.window.set_selected_control(
                item["control_size"],
                item["control_shape"],
                item["control_color"],
            )

        self._update_action_controls_enabled()

        # Everything above only touches Qt widgets (control highlights
        # via widget.update(), the delete/symmetry/etc buttons via
        # setEnabled(), the combos via set_selected_control()) - cheap,
        # and all that's actually visible to the user. Flush it right
        # here, before the slower bpy.ops work below, for the same
        # reason as PickerAreaButton._apply_style() in main_window.py:
        # widget.update()/setEnabled() only *schedule* a repaint, which
        # can sit pending until some unrelated event forces it through
        # inside Blender's own event loop (it doesn't pump Qt
        # continuously). Flushing AFTER _sync_viewport_selection()
        # instead (as this used to) meant every bit of visible feedback
        # had to wait on that loop of bpy.ops.rp.select() calls first -
        # one Blender operator call per selected bone - which is what
        # made the whole thing feel delayed again, this time end to
        # end. Flushing here instead means the highlights and buttons
        # appear immediately, and the viewport sync (which has nothing
        # to do with what's on screen in this Qt window) can take
        # however long it needs afterward without blocking that.
        # Direct synchronous repaint of the picker window, NOT a nested
        # QApplication.processEvents(): this runs inside a mouse-click
        # handler, and pumping the whole event queue from there re-enters
        # event dispatch (further clicks/hover events/Python overrides)
        # mid-handler - a native-crash risk.
        if self.window is not None:
            self.window.repaint()

        # Same reasoning as select_control()'s own call: keep
        # backend._LAST_SELECTED_BONES in lockstep with the viewport so
        # a silently-failed rp.select operator can't leave stale bones
        # free to get synced back into self.selected_bones later.
        self._sync_viewport_selection()

        self.refresh_custom_action_enabled_states()

    def sync_selection_from_blender(self, selected_bone_names):
        """Called from backend.poll_active_armature when the set of
        selected pose bones changes in the 3D viewport (i.e. the user
        selected a control directly in Blender, not by clicking it in the
        picker UI).

        Only bones that are actually registered as controls in the current
        picker are reflected in the picker's own selection state:
        - If the viewport selection is empty (user deselected everything,
          e.g. clicking empty space or Alt+A), the picker selection is
          cleared to match.
        - If the viewport selection is non-empty but none of the selected
          bones are picker controls (e.g. a mesh or a non-control bone was
          selected), the picker selection is also cleared - whatever
          control was highlighted before is no longer actually selected
          in Blender, so leaving it highlighted would be showing stale
          state. The picker's control widgets themselves are untouched
          (they stay where they are); only which one is marked
          active/selected changes.

        This never calls back into bpy.ops.rp.select: the bones are
        already selected in Blender (that's what triggered this in the
        first place), so this only needs to update the Qt widgets/state.
        """
        if self.window is None:
            return

        added_bones = {item["bone_name"] for item in self.data["items"]}

        new_selection = set(selected_bone_names) & added_bones

        if new_selection == self.selected_bones:
            return

        self.selected_bones = new_selection

        for name, widget in self.window.control_list.controls.items():
            widget.active = (name in self.selected_bones)
            widget.update()

        if self.selected_bones:
            first = next(iter(self.selected_bones))
            item = self.find_item(first)

            if item:
                self.window.set_selected_control(
                    item["control_size"],
                    item["control_shape"],
                    item["control_color"],
                )
                # set_selected_control() already enables/disables these
                # combos based on background presence - see the matching
                # fix in undo() above.
        else:
            self.window.set_no_selection_defaults()

        self._update_action_controls_enabled()

        self.refresh_custom_action_enabled_states()

    def set_selected_size(self, size):
        self._set_selected_appearance(size=size)

    def set_selected_shape(self, shape):
        self._set_selected_appearance(shape=shape)

    def set_selected_color(self, color):
        self._set_selected_appearance(color=color)

    def sync_mirror_color(self, color):
        """Runs whenever the color dropdown is ACTIVATED - i.e. the user
        clicked an entry, including the color it already shows (the
        dropdown's currentIndexChanged doesn't fire for that, so
        set_selected_color() never runs for a re-pick).

        If Symmetry is off, or nothing is selected, or no selected control
        has a mirror partner: nothing to do. Otherwise each partner that
        doesn't already have `color` is recolored; partners that already
        match are left alone, and when every one matches no snapshot, save
        or redraw happens at all. (After a normal color change the partner
        was already synced by _set_selected_appearance(), so this is then a
        pure no-op check.)
        """
        if color is None or not self.data.get("symmetry") or not self.selected_bones:
            return

        from ..backend import mirror_name

        items = {item["bone_name"]: item for item in self.data["items"]}

        stale = []
        for bone_name in self.selected_bones:
            item = items.get(bone_name)
            mirror_bone = mirror_name(bone_name)
            mirror_item = items.get(mirror_bone) if mirror_bone else None
            if item is None or mirror_item is None or mirror_item is item:
                continue
            if mirror_item.get("control_color") != color and mirror_item not in stale:
                stale.append(mirror_item)

        if not stale:
            return

        self._record_undo_snapshot()
        for mirror_item in stale:
            mirror_item["control_color"] = color
            widget = self.window.control_list.controls.get(mirror_item["bone_name"])
            if widget:
                widget.set_appearance(color=color)
        self.save()

    def _set_selected_appearance(self, size=None, shape=None, color=None):
        if not self.selected_bones:
            # Nothing selected - the combos are showing/editing the
            # *default* appearance (see set_no_selection_defaults()),
            # not any actual control. Still an operation the rigger
            # made though, so it's undoable/redoable like anything
            # else (see default_control_size/shape/color in
            # _record_undo_snapshot()) - just not saved to disk on its
            # own, since self.data itself never changes here: it's a
            # live default for controls not created yet, not a change
            # to anything that already exists.
            #
            # defaults_only=True: this only runs because nothing is
            # selected, so its own undo step should restore selection to
            # genuinely empty rather than resurrecting whatever was
            # selected before an incidental deselect - see
            # _record_undo_snapshot()'s defaults_only docstring.
            self._record_undo_snapshot(defaults_only=True)

            if size is not None:
                self.default_control_size = size

            if shape is not None:
                self.default_control_shape = shape

            if color is not None:
                self.default_control_color = color

            # Auto-save: the defaults live with the loaded panel.
            self._save_panel_defaults()
            return

        self._record_undo_snapshot()

        from ..backend import mirror_name, control_dimensions

        # Build a lookup once instead of searching every time
        items = {item["bone_name"]: item for item in self.data["items"]}

        for item in self.data["items"]:

            if item["bone_name"] not in self.selected_bones:
                continue

            # -----------------------------
            # Update selected control data
            # -----------------------------
            # Capture the pre-change footprint so we can re-center the
            # control below - "x"/"y" store the top-left corner, and
            # resizing/reshaping a widget grows it from that corner, not
            # its center, so without this the control visibly drifts.
            old_width, old_height = control_dimensions(
                item.get("control_size", 18), item.get("control_shape", "CIRCLE")
            )

            if size is not None:
                item["control_size"] = size

            if shape is not None:
                item["control_shape"] = shape

            if color is not None:
                item["control_color"] = color

            # -----------------------------
            # Update selected widget
            # -----------------------------
            widget = self.window.control_list.controls.get(item["bone_name"])

            if widget:
                widget.set_appearance(
                    size=size,
                    shape=shape,
                    color=color
                )

            if size is not None or shape is not None:
                new_width, new_height = control_dimensions(
                    item["control_size"], item["control_shape"]
                )
                dx = (new_width - old_width) / 2.0
                dy = (new_height - old_height) / 2.0

                # Shift the WIDGET's own image_position - what
                # layout_controls() actually renders from - rather than
                # gating this on item["x"]/["y"]. A freshly-added,
                # never-dragged control is auto-arranged straight into
                # widget.image_position by add_control(), but its
                # item["x"]/["y"] stay at their -1.0 "unset" sentinel
                # until the first manual drag (see move_control_from_
                # canvas()) - so gating the shift on that sentinel, as
                # this used to, silently skipped re-centering for
                # exactly the controls a user is most likely to resize
                # right after adding them: it grew from the auto-placed
                # top-left corner instead of staying centered. Reading
                # off the widget's real position and writing the result
                # back into item["x"]/["y"] keeps both in sync and
                # naturally resolves the sentinel the first time a
                # control's appearance changes, same as a drag would.
                if widget is not None and hasattr(widget, "image_position"):
                    widget.image_position.setX(
                        round(widget.image_position.x() - dx)
                    )
                    widget.image_position.setY(
                        round(widget.image_position.y() - dy)
                    )
                    item["x"] = widget.image_position.x()
                    item["y"] = widget.image_position.y()
                else:
                    # No live widget to read a real position from
                    # (shouldn't normally happen) - fall back to
                    # shifting the stored data position directly, only
                    # when it's already a real (non-sentinel) value.
                    if item["x"] >= 0:
                        item["x"] -= dx
                    if item["y"] >= 0:
                        item["y"] -= dy

            # -----------------------------
            # Update mirrored control
            # -----------------------------
            # Only propagate to the mirror while Symmetry is actually on
            # - this used to run unconditionally whenever a mirror bone
            # happened to exist as a control, regardless of the checkbox,
            # so resizing/reshaping/recoloring one control silently
            # changed its mirror too even with symmetry off. Dragging
            # (move_control_from_canvas() in control_list.py), adding
            # (add_bones() above), and deleting (delete_selected() below)
            # all already gate their own mirror-sync on this same check -
            # this was the one appearance-editing path that didn't.
            mirror_bone = mirror_name(item["bone_name"]) if self.data.get("symmetry") else None

            if mirror_bone:

                mirror_item = items.get(mirror_bone)

                if mirror_item:

                    mirror_old_width, mirror_old_height = control_dimensions(
                        mirror_item.get("control_size", 18),
                        mirror_item.get("control_shape", "CIRCLE"),
                    )

                    if size is not None:
                        mirror_item["control_size"] = size

                    if shape is not None:
                        mirror_item["control_shape"] = shape

                    if color is not None:
                        mirror_item["control_color"] = color

                    mirror_widget = self.window.control_list.controls.get(
                        mirror_bone
                    )

                    if mirror_widget:
                        mirror_widget.set_appearance(
                            size=size,
                            shape=shape,
                            color=color
                        )

                    if size is not None or shape is not None:
                        mirror_new_width, mirror_new_height = control_dimensions(
                            mirror_item["control_size"], mirror_item["control_shape"]
                        )
                        mirror_dx = (mirror_new_width - mirror_old_width) / 2.0
                        mirror_dy = (mirror_new_height - mirror_old_height) / 2.0

                        # Same widget-position-based fix as the primary
                        # control above, for the same reason.
                        if mirror_widget is not None and hasattr(
                            mirror_widget, "image_position"
                        ):
                            mirror_widget.image_position.setX(
                                round(mirror_widget.image_position.x() - mirror_dx)
                            )
                            mirror_widget.image_position.setY(
                                round(mirror_widget.image_position.y() - mirror_dy)
                            )
                            mirror_item["x"] = mirror_widget.image_position.x()
                            mirror_item["y"] = mirror_widget.image_position.y()
                        else:
                            if mirror_item["x"] >= 0:
                                mirror_item["x"] -= mirror_dx
                            if mirror_item["y"] >= 0:
                                mirror_item["y"] -= mirror_dy

        # Update control positions once in case size changed
        self.window.control_list.container.layout_controls()
        self.save()

    def show_all(self):
        # Only a step of its own when it actually changes picker data: a
        # Show All with nothing hidden would otherwise cost a Ctrl+Z
        # press that does nothing (same reasoning as add_selected()).
        if any(item.get("hidden", False) for item in self.data["items"]):
            self._record_undo_snapshot()
        bpy.ops.rp.show_all()

        for item in self.data["items"]:
            item["hidden"] = False
        self.save()

    # ---------------------------------------------------------

    def hide_all(self):
        # See show_all(): no snapshot when every control is already hidden.
        if any(not item.get("hidden", False) for item in self.data["items"]):
            self._record_undo_snapshot()
        bpy.ops.rp.hide_all()

        for item in self.data["items"]:
            item["hidden"] = True
        self.save()

    def capture_view(self):
        if not self.armature_name or not self.panel_name:
            return

        result = bpy.ops.rp.capture_view()

        # RP_OT_CaptureView writes to this same (per-process) temp path
        # on every successful capture, so it's never cleaned up between
        # calls - a failed capture (e.g. no VIEW_3D area visible right
        # now) would otherwise leave whatever an EARLIER successful
        # capture wrote still sitting there. Checking the operator's own
        # result (rather than just os.path.exists(temp_path), which
        # would be true from that stale leftover) is what tells "this
        # capture actually just ran" apart from "some previous capture
        # ran, this one didn't" - without it, a failed capture would
        # silently overwrite this rig's background with a stale,
        # outdated screenshot instead of leaving it untouched.
        if 'FINISHED' not in result:
            return

        # Must match RP_OT_CaptureView.execute()'s own path exactly
        # (see backend.py) - PID-scoped rather than a fixed shared name
        # so two separate Blender processes capturing around the same
        # moment can never interleave on the same temp file.
        temp_path = os.path.join(
            tempfile.gettempdir(),
            f"rig_picker_capture_{os.getpid()}.png"
        )

        if not os.path.exists(temp_path):
            return

        # Copy it into this (rig, panel) pair's own dedicated image file
        # so it can never be overwritten by capturing a different
        # panel's - on this rig or any other - view.
        dest_path = json_manager.get_image_path(
            self.armature_name, self.panel_name, ensure_folder=True
        )

        self._record_undo_snapshot()

        # This file is always the same fixed path for this armature, so
        # simply overwriting it would permanently lose the previous
        # image's pixels - even though undo() below restores
        # self.data["background"] to that very same, unchanged path.
        # Back the existing file up first so undo can bring the actual
        # old image back, not just an unchanged path string now pointing
        # at the new one.
        self._stage_image_backup(dest_path)

        try:
            shutil.copyfile(temp_path, dest_path)
        except OSError as e:
            # Full disk, read-only/synced folder, antivirus lock... Left
            # unhandled this escaped the Qt slot as a bare traceback with
            # no feedback, after the undo snapshot above had already been
            # recorded. Nothing in self.data has changed yet, so the
            # leftover snapshot is a harmless no-op undo step.
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self.window,
                "Capture View",
                f"Couldn't save the captured image to:\n{dest_path}\n\n{e}",
            )
            return
        finally:
            # The per-process temp capture has been consumed (or failed);
            # don't leave a PNG behind in the temp folder for every
            # session. Whether a capture just ran is decided by the
            # operator's result above, never by this file existing.
            try:
                os.remove(temp_path)
            except OSError:
                pass

        self.data["background"] = dest_path
        self.data["image_offset_x"] = 0.0
        self.data["image_offset_y"] = 0.0

        self.window.control_list.set_background(dest_path, 0.0, 0.0)

        # If Symmetry was already enabled on this rig before it ever had
        # a background (see _ensure_symmetry_default()'s docstring),
        # this is the moment a default symmetry_x can finally be
        # computed - do it now rather than leaving the guide line
        # invisible until the checkbox happens to get toggled again.
        self._ensure_symmetry_default()

        # capture_view() is the one place a background can newly appear
        # without going through refresh() (which already calls this) -
        # without this, the toolbar would stay showing its "no
        # background yet" disabled state even after one now exists,
        # until the next armature switch happened to trigger a refresh().
        self._update_action_controls_enabled()

        self.save()

    def _show_all_and_deselect(self):
        """Reveals every bone (same as Blender's Alt+H / pose.reveal) and
        clears the viewport pose-bone selection - shared by clear_all()
        and change_armature() so both leave the rig in the same neutral
        state."""
        bpy.ops.rp.show_all()

        # Clear the picker's own selection state first, then push that
        # (now-empty) selection through _sync_viewport_selection() - the
        # same reveal+deselect this used to do manually here (via its own
        # get_3d_override()/temp_override block), but that inline copy
        # never updated backend._LAST_SELECTED_BONES the way
        # _sync_viewport_selection() does. Left stale, that's the same
        # leak select_control() and deselect_all() had (see their
        # comments): if the deselect ever silently failed, nothing would
        # correct _LAST_SELECTED_BONES, letting the still-actually-
        # selected bones get synced back into self.selected_bones on the
        # next poll tick.
        self.selected_bones.clear()
        self._sync_viewport_selection()

    def clear_all(self):
        """Wipes EVERY panel this rig has - not just the one currently
        showing - and leaves the rig with a single fresh default panel,
        the same empty state a brand new rig starts with.

        "Clear All" used to be scoped to just self.data (the currently
        loaded panel), a leftover from before per-panel scoping existed -
        back when a rig had exactly one implicit panel, clearing "the
        current panel" and clearing "the whole rig" were the same thing.
        Once a rig could have several panels (Face, Body, ...), that old
        scoping quietly became "clear only whichever panel happens to be
        open", leaving every other panel's controls/background untouched
        despite the button still saying "Clear All". This restores the
        original whole-rig meaning: every panel's stored data (and its
        captured-view image) is removed, then exactly one blank "Panel 1"
        is created and loaded, mirroring what already happens when a
        rig's last remaining panel is deleted via Manage Panels.
        """
        armature_name = self.armature_name
        if not armature_name:
            return

        quick_settings = getattr(bpy.context.scene, "rp_quick_action_settings", None)

        # Every (rig, panel) pair's undo/redo history for THIS rig is about
        # to point at panels that no longer exist, so it is taken OUT of the
        # live dicts - but NOT thrown away: it is parked on the Clear All's
        # own entry (below) and put back when that Clear All is undone, so
        # Ctrl+Z can keep walking back through everything done before it.
        # Only when there is no entry to park it on (nothing was stored, so
        # the Clear All can't be undone anyway) is it settled and dropped.
        rig_history = self.stash_rig_history(armature_name)

        # Take every panel's data - items, background capture image,
        # symmetry, everything - for this rig out of rig_picker_data.json.
        # Done as a REVERSIBLE backup instead of an outright delete: the
        # rows/buttons removed below are scene data, so Ctrl+Z restores
        # them by itself, and without the layouts coming back too they'd
        # all be empty. custom_actions_quick keeps the backup for a grace
        # period and restores it when it sees the rows come back (see
        # _update_rig_clears_after_undo()).
        from .. import custom_actions_quick
        original_uids = []
        if quick_settings is not None:
            original_uids = [
                custom_actions_quick._panel_uid(p)
                for p in quick_settings.panels
                if p.armature_name == armature_name
            ]
        clear_entry = custom_actions_quick.schedule_rig_clear(
            armature_name,
            json_manager.backup_armature_data(armature_name),
            original_uids,
        )
        # Remembered, with a copy of the scene rows about to be removed, so
        # picker Ctrl+Z / Ctrl+Shift+Z can undo/redo this Clear All itself
        # (see undo()/redo()).
        if not hasattr(self, "_clear_entries"):
            self._clear_entries = {}
        self._clear_entries[armature_name] = clear_entry
        if clear_entry is not None:
            clear_entry["history"] = rig_history
        else:
            self.settle_rig_history(rig_history)
        if quick_settings is not None:
            custom_actions_quick.keep_clear_scene_snapshot(
                clear_entry, quick_settings, armature_name
            )

        # self.panel_name still names a panel whose data was just
        # deleted above - clear the in-memory reference DIRECTLY rather
        # than routing through change_panel()/_switch_to_panel(), whose
        # first step is to save() whatever was loaded before switching,
        # which would simply write self.data straight back out under
        # that now-deleted name, silently resurrecting it.
        self.panel_name = None
        self.data = json_manager.new_panel_data()

        # Remove every RP_QuickPanel row belonging to this rig from the
        # Manage Panels N-panel list too - not just the JSON data - so
        # the list doesn't keep showing panels whose data no longer
        # exists. Iterate back-to-front so removing by index is safe.
        if quick_settings is not None:
            for i in range(len(quick_settings.panels) - 1, -1, -1):
                if quick_settings.panels[i].armature_name == armature_name:
                    quick_settings.panels.remove(i)

            # Every panel this rig had just got wiped above, and Section
            # 1's multi-command buttons are scoped to (rig, panel) - none
            # of them have a panel left to belong to. Remove every one of
            # this rig's, the same orphaned-data cleanup
            # RP_OT_quick_remove_panel does for a single deleted panel,
            # just scoped to "every panel of this rig" here since Clear
            # All deletes them all at once.
            for i in range(len(quick_settings.multi_actions) - 1, -1, -1):
                if quick_settings.multi_actions[i].armature_name == armature_name:
                    quick_settings.multi_actions.remove(i)

            # Manage Panels (like the Qt picker's own Panel dropdown) has
            # nothing sensible to show with this rig now having zero
            # panels - recreate its default "Panel 1" immediately rather
            # than waiting for the next poll tick's
            # _ensure_default_panel_for_rig() to notice, so the picker
            # and the N-panel list both already show it the moment this
            # returns.
            custom_actions_quick._ensure_default_panel_for_rig(quick_settings, armature_name)
            custom_actions_quick.note_rig_clear_default(
                clear_entry, quick_settings, armature_name
            )

        self.panel_name = "Panel 1"
        self.data = json_manager.new_panel_data()

        # Same reset load_armature()/change_armature() already do when
        # switching rigs (see their matching comments) - the size/shape/
        # color defaults are live-editable per set_no_selection_defaults()
        # while nothing is selected, so without this, a default tweaked
        # before clearing would silently keep applying to new controls
        # added after the clear, on what's now effectively a blank slate.
        self.default_control_size = 18
        self.default_control_shape = "CIRCLE"
        self.default_control_color = "GREEN"

        # Persist the fresh "Panel 1" entry right away, rather than
        # waiting for some later action to trigger the first save() -
        # a rig that's just been cleared should already show up in
        # rig_picker_data.json with its new blank default panel, not
        # only once something happens to touch it again.
        self.save()

        # Clear All just deleted this rig's multi-command buttons from the
        # scene. The (rig, panel) pair can end up identical to what was
        # loaded before ("Panel 1" -> "Panel 1"), so the poll tick's key
        # comparison wouldn't notice - drop their canvas widgets now.
        if self.window is not None:
            self.window.sync_quick_actions()

        # Clearing the picker's controls shouldn't leave bones hidden or
        # selected in the viewport.
        self._show_all_and_deselect()
        self.refresh()

        # refresh() doesn't touch the size/shape/color combos itself -
        # push the just-reset defaults into them explicitly, the same
        # way delete_selected()/change_armature() do, so the toolbar
        # reflects the reset immediately instead of still showing
        # whatever was selected/tweaked right before the clear.
        self.window.set_no_selection_defaults()

        # No extra Blender undo step is pushed here: _show_all_and_deselect()
        # above already runs bpy.ops.rp.show_all() (pose.reveal), which
        # pushes the one step that undo() below reverses. A second step
        # would leave undo() stopping halfway, on a state where the rows
        # are still gone.

    def _clear_all_entry(self, wanted_state):
        """This rig's Clear All entry if it is still being tracked and in
        `wanted_state` ("cleared" = can be undone, "undone" = can be redone)."""
        from .. import custom_actions_quick
        entry = getattr(self, "_clear_entries", {}).get(self.armature_name)
        if (entry is not None
                and entry in custom_actions_quick._PENDING_RIG_CLEARS
                and entry["state"] == wanted_state):
            return entry
        return None

    def _confirm_delete_used_controls(self, quick_refs):
        """Yes/No prompt shown by delete_selected() when the controls being
        deleted are used as Control shape bones on multi-command buttons.
        Returns True only on Yes; No (or closing the box) cancels."""
        from PySide6.QtWidgets import QMessageBox

        bone_count = len({name for _a, _i, names in quick_refs for name in names})
        plural = bone_count != 1

        lines = [
            f'\u2022 {action.name or "Unnamed button"} \u2013 '
            f'Command {rule_index + 1}: {", ".join(bone_names)}'
            for action, rule_index, bone_names in quick_refs
        ]
        max_lines = 10
        if len(lines) > max_lines:
            extra = len(lines) - max_lines
            lines = lines[:max_lines] + [f"\u2026and {extra} more"]

        if plural:
            headline = "Delete these controls?"
            intro = (
                "These controls are used as Control shape bones in "
                "Multi Command Buttons:"
            )
            yes_text = (
                "Yes: the controls are deleted from the picker and removed "
                "from these buttons."
            )
        else:
            headline = "Delete this control?"
            intro = (
                "This control is used as a Control shape bone in a "
                "Multi Command Button:"
            )
            yes_text = (
                "Yes: the control is deleted from the picker and removed "
                "from this button."
            )

        box = QMessageBox(
            QMessageBox.Warning,
            "Delete Control",
            headline,
            QMessageBox.Yes | QMessageBox.No,
            self.window,
        )
        box.setInformativeText(
            intro + "\n\n" + "\n".join(lines) + "\n\n"
            + yes_text + "\nNo: the operation is cancelled and nothing is changed."
        )
        box.setDefaultButton(QMessageBox.No)
        box.setEscapeButton(QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    def delete_selected(self):
        if not self.selected_bones:
            return

        selected = set(self.selected_bones)

        # When symmetry is on, deleting a control should take its mirror
        # with it - otherwise you're left with a single one-sided
        # control and have to hunt down and delete the mirror
        # separately, which defeats the point of working symmetrically
        # in the first place. Only expand to mirrors that actually exist
        # as controls (mirror_name() can return a name with nothing
        # behind it, e.g. a control added on an asymmetric bone with no
        # real ".L"/".R" counterpart).
        if self.data.get("symmetry"):
            from ..backend import mirror_name

            existing_bones = {item["bone_name"] for item in self.data["items"]}

            for bone_name in list(selected):
                mirror_bone = mirror_name(bone_name)
                if mirror_bone and mirror_bone in existing_bones:
                    selected.add(mirror_bone)

        # A control whose bone is listed as a Control shape bone on one of
        # this panel's multi-command buttons can't just disappear: ask first.
        # No -> nothing is changed at all. Yes -> the control is deleted AND
        # the bone is removed from those buttons' Control shape bones.
        from .. import custom_actions_quick
        quick_refs = custom_actions_quick.find_control_bone_references(
            self.armature_name, self.panel_name, selected
        )
        if quick_refs and not self._confirm_delete_used_controls(quick_refs):
            return

        # The controls about to be deleted, with their positions, kept so a
        # native Blender undo of this delete can put them back (see
        # custom_actions_quick.register_control_delete()).
        deleted_items = [
            (index, item) for index, item in enumerate(self.data["items"])
            if item["bone_name"] in selected
        ] if quick_refs else []

        self._record_undo_snapshot()

        self.data["items"] = [
            item for item in self.data["items"]
            if item["bone_name"] not in selected
        ]
        self.save()

        if quick_refs:
            removed = custom_actions_quick.remove_control_bone_references(
                self.armature_name, self.panel_name, selected
            )
            # The picker's own Ctrl+Z only reverts picker layout data - it
            # never writes into Blender scene data (see undo()), and the
            # Multi Command Buttons' Control shape bone lists are scene
            # data. So this removal is undone by Blender's own undo only,
            # via the native step registered below.
            if removed:
                # Also make this a step on Blender's own undo stack, and
                # remember it, so viewport Ctrl+Z / Ctrl+Shift+Z keep the
                # buttons and the picker's controls in step too.
                custom_actions_quick.register_control_delete(
                    self.armature_name, self.panel_name, deleted_items, removed
                )
                custom_actions_quick.push_native_undo_step("Delete Picker Control")
            self._redraw_all_areas()
            custom_actions_quick._sync_quick_picker_ui()

        self.selected_bones.clear()

        self.window.set_no_selection_defaults()

        # Previously called bpy.ops.rp.hide_all() here ("Force hiding
        # remaining controls"), which hides every pose bone in the
        # viewport - hiding should only ever happen from the explicit
        # "H" hotkey/Hide All action. Reveal instead, the same way
        # show_all() does: clear each remaining item's own "hidden" flag
        # too, not just call the reveal operator - refresh() below
        # re-applies item["hidden"] onto every pose bone every time it
        # runs, so a plain viewport reveal here would get silently
        # undone the moment refresh() executes for any control that was
        # still marked hidden in the data.
        for item in self.data["items"]:
            item["hidden"] = False
        self.save()

        self._show_all_and_deselect()
        self.refresh()

    def _selectable_control_names(self, rig):
        """Names of the picker controls that can be selected right now.

        A control that is greyed out in the picker (setEnabled(False), see
        CircleControl.set_bone_collection_visible()) is never selectable:
        the widget's own state is what the user sees, and it is also how
        box select decides. On top of that, with a rig, a bone whose
        collection is hidden can't really be selected in the viewport
        either (_sync_viewport_selection() drops it), so it is left out
        here too - marking it active in the picker would only get silently
        corrected on the next ~0.15s poll tick."""
        from ..backend import is_bone_collection_visible

        controls = {
            name for name, widget in self.window.control_list.controls.items()
            if widget.isEnabled()
        }
        if rig and rig.type == "ARMATURE":
            return {
                name for name in controls
                if name in rig.pose.bones
                and is_bone_collection_visible(rig.pose.bones[name])
            }
        return controls

    def _set_picker_selection(self, new_selection):
        """Makes `new_selection` (a set of control/bone names) the picker's
        selection and pushes it to the viewport. Shared by select_all() and
        invert_selection()."""
        # Same reasoning as select_control(): a plain selection change
        # gets its own selection_only snapshot, so it stays individually
        # undoable unless a real operation immediately follows, in which
        # case that operation's own push discards this one instead of
        # stacking on top of it. Skip the push entirely if nothing would
        # change - it'd just be a no-op step eating a Ctrl+Z press.
        if new_selection != self.selected_bones:
            self._record_undo_snapshot(selection_only=True)
        self.selected_bones = new_selection

        # Update UI widgets - only the bones in new_selection end up marked
        # active, so a control that can't be selected stays visibly
        # unselected instead of briefly flashing active until the next
        # poll corrects it.
        for name, widget in self.window.control_list.controls.items():
            widget.active = (name in new_selection)
            widget.update()

        # Push exactly this selection onto the viewport (select those bones,
        # hide everything else) in one pass.
        self._sync_viewport_selection()

        # Same leak select_control()/deselect_all()/_show_all_and_deselect()
        # had: _sync_viewport_selection() returns early without touching
        # backend._LAST_SELECTED_BONES when there is no usable 3D view, so
        # keep it in lockstep with the picker's selection here as well.
        # Left stale, a viewport selection that never actually changed could
        # later be synced back into self.selected_bones by the poll tick,
        # resurrecting a selection the picker UI already moved on from.
        backend._LAST_SELECTED_BONES = frozenset(self.selected_bones)

        # Update appearance control dropdowns
        if self.selected_bones:
            first = next(iter(self.selected_bones))
            item = self.find_item(first)

            if item:
                self.window.set_selected_control(
                    item["control_size"],
                    item["control_shape"],
                    item["control_color"],
                )
        else:
            self.window.set_no_selection_defaults()

        self._update_action_controls_enabled()

        self.refresh_custom_action_enabled_states()

    def select_all(self):
        """Selects every picker control - and ONLY the picker controls.

        The viewport selection is pushed through _sync_viewport_selection(),
        the same path a box select or any other multi-control selection
        uses: it selects exactly those bones and isolates them (hides the
        rest), like selecting a single control does. It used to reveal every
        bone and run pose.select_all(SELECT), which selected every visible
        bone in the rig - controls or not - and left them all showing, i.e.
        the same viewport result as clicking empty space (Show All)."""
        from ..backend import arm

        if not self.window.control_list.controls:
            # No picker controls: there is nothing to select, and the
            # viewport selection is none of the picker's business.
            return

        self._set_picker_selection(self._selectable_control_names(arm()))

    def invert_selection(self):
        """Selects every selectable picker control that is NOT selected now,
        and deselects the ones that were (greyed-out controls are never
        selected either way)."""
        from ..backend import arm

        if not self.window.control_list.controls:
            return

        selectable = self._selectable_control_names(arm())
        self._set_picker_selection(selectable - set(self.selected_bones))

    def deselect_all(self):
        # No snapshot here: deselect_all() is only ever called when the
        # user clicks empty canvas space to clear selection, which isn't
        # an undoable edit in its own right. Instead, remember what was
        # selected right before clearing it - if the very next thing
        # that happens is a real operation (picking a new control,
        # dragging, toggling something), that operation's own
        # _record_undo_snapshot() call picks this up as the selection to
        # restore, so undo lands on "before this deselect" rather than
        # on the deselect itself.
        if self.selected_bones:
            self._pending_deselect_baseline = set(self.selected_bones)

        self.selected_bones.clear()

        for widget in self.window.control_list.controls.values():
            widget.active = False
            widget.update()

        self.window.set_no_selection_defaults()

        # Reveal + deselect in the viewport, and critically, keep
        # backend._LAST_SELECTED_BONES in lockstep with it - this used to
        # be a separate, inline copy of _sync_viewport_selection()'s own
        # empty-selection branch that never updated
        # backend._LAST_SELECTED_BONES. Left stale, that's the exact same
        # leak select_control() had (see its comment): if the deselect
        # operator ever silently failed (get_3d_override() returning None,
        # no exception, no visible error) so the real viewport selection
        # never actually cleared, nothing corrected _LAST_SELECTED_BONES
        # either - leaving the OLD, still-actually-selected bones free to
        # get silently synced back into self.selected_bones on the very
        # next poll tick, resurrecting a selection the picker UI (and the
        # user) already believed was cleared.
        self._sync_viewport_selection()

        self._update_action_controls_enabled()

        self.refresh_custom_action_enabled_states()

    def _ensure_symmetry_default(self):
        """Computes symmetry_x (the image-space X the dashed guide line
        sits at), but only if symmetry is enabled AND the current
        symmetry_x is missing OR stale.

        IMPORTANT: this must be computed in the same "image space" that
        control.image_position and canvas_to_image_position() use - which,
        since image_scale() was reworked to be relative to a load-time
        canvas-size baseline (see reset_scale_reference()), is no longer
        the background pixmap's raw native pixel size. Right after a
        background loads, image_scale() == 1.0 relative to that baseline,
        so "image space" coordinates are actually canvas-display pixels
        at that moment - typically much smaller than
        canvas.background.width() (the actual image file's native pixel
        width).

        Rigs saved before this rework can have a symmetry_x that LOOKS
        valid (>= 0) but was computed under the old raw-pixel space
        (canvas.background.width() / 2) - e.g. 378.0 for an image whose
        current image-space width is only ~200. That stale value used to
        make this method return early and never get corrected, which is
        why re-toggling symmetry or reloading a background never fixed
        rigs that already had a saved symmetry_x, even though it fixed
        rigs starting from scratch. So instead of just checking
        "is it set", check "is it within this image's current
        image-space bounds" - anything negative or larger than the
        image's own current width is treated as stale and recomputed,
        the same as if it had never been set.
        """
        if not self.data.get("symmetry"):
            return

        canvas = self.window.control_list.container
        if not canvas.background:
            return

        pixmap = canvas.scaled_background()
        if pixmap is None or pixmap.width() <= 0:
            return

        # The image's own width, converted into the current image-space
        # coordinate system - the valid upper bound for any symmetry_x
        # in that same space.
        image_space_width = canvas.canvas_to_image_position(
            QPoint(canvas.image_x + pixmap.width(), 0)
        ).x()

        current = self.data.get("symmetry_x", -1.0)

        if 0 <= current <= image_space_width:
            # Already valid in the CURRENT coordinate space - a value a
            # user deliberately dragged should be left alone, not reset
            # back to center every time this runs.
            return

        canvas_center_x = canvas.image_x + pixmap.width() / 2.0
        image_pos = canvas.canvas_to_image_position(
            QPoint(round(canvas_center_x), 0)
        )

        self.data["symmetry_x"] = image_pos.x()
        canvas.symmetry_x = self.data["symmetry_x"]
        canvas.update()


    def toggle_symmetry(self, enabled):
        self._record_undo_snapshot()

        self.data["symmetry"] = enabled

        canvas = self.window.control_list.container
        canvas.symmetry_enabled = enabled
        canvas.symmetry_x = self.data.get("symmetry_x", -1.0)

        self._ensure_symmetry_default()

        canvas.update()

        # Previously missing entirely: without this, checking/unchecking
        # Symmetry never made it to disk, so the setting (and any
        # symmetry_x default just computed above) could be silently lost
        # the next time this armature's data was reloaded - e.g. on
        # switching armatures and back, or reopening Blender - even
        # though the checkbox and guide line looked correct in the
        # current session.
        self.save()