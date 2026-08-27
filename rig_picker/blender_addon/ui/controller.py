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
from collections import deque

import bpy

from PySide6.QtCore import QPoint

from .. import json_manager
from .. import backend

# How many operations back, per rig, Ctrl+Z can step through with no
# other operation in between. See Controller.undo_snapshots.
UNDO_STACK_DEPTH = 18

IK_FK_GROUPS = {

    "upper_arm_parent.L": {
        "output_bones": [
            "upper_arm_fk.L",
            "forearm_fk.L",
            "hand_fk.L",
        ],
        "input_bones": [
            "upper_arm_ik.L",
            "MCH-forearm_ik.L",
            "MCH-upper_arm_ik_target.L",
        ],
        "ctrl_bones": [
            "upper_arm_ik.L",
            "upper_arm_ik_target.L",
            "hand_ik.L",
        ],
    },

    "upper_arm_parent.R": {
        "output_bones": [
            "upper_arm_fk.R",
            "forearm_fk.R",
            "hand_fk.R",
        ],
        "input_bones": [
            "upper_arm_ik.R",
            "MCH-forearm_ik.R",
            "MCH-upper_arm_ik_target.R",
        ],
        "ctrl_bones": [
            "upper_arm_ik.R",
            "upper_arm_ik_target.R",
            "hand_ik.R",
        ],
    },
    "thigh_parent.L": {
        "output_bones": [
            "thigh_fk.L",
            "shin_fk.L",
            "foot_fk.L",
            "toe_fk.L",
        ],
        "input_bones": [
            "thigh_ik.L",
            "MCH-shin_ik.L",
            "MCH-thigh_ik_target.L",
        ],
        "ctrl_bones": [
            "thigh_ik.L",
            "thigh_ik_target.L",
            "foot_ik.L",
        ],

        "tail_bones": [
            "toe_ik.L",
        ],

        "extra_ctrls": [
            "foot_heel_ik.L",
            "foot_spin_ik.L",
        ],

        "heel_control": "foot_heel_ik.L",
    },
    "thigh_parent.R": {
        "output_bones": [
            "thigh_fk.R",
            "shin_fk.R",
            "foot_fk.R",
            "toe_fk.R",
        ],

        "input_bones": [
            "thigh_ik.R",
            "MCH-shin_ik.R",
            "MCH-thigh_ik_target.R",
        ],

        "ctrl_bones": [
            "thigh_ik.R",
            "thigh_ik_target.R",
            "foot_ik.R",
        ],

        "tail_bones": [
            "toe_ik.R",
        ],

        "extra_ctrls": [
            "foot_heel_ik.R",
            "foot_spin_ik.R",
        ],

        "heel_control": "foot_heel_ik.R",
    },
}


class Controller:

    def __init__(self):
        self.window = None
        self.selected_bones = set()
        self.active = False

        # Name of the armature the currently-loaded data belongs to.
        self.armature_name = None

        # Appearance a brand-new control gets (Add Selected) and what
        # the pickers show/enable whenever nothing is selected. Starts
        # at the historical hardcoded values (see json_manager.py's
        # docstring example) but is live-editable via the Size/Shape/
        # Color combos while nothing is selected - see
        # _set_selected_appearance() below.
        self.default_control_size = 18
        self.default_control_shape = "CIRCLE"
        self.default_control_color = "GREEN"

        # In-memory picker data for the current armature:
        # {"background": str, "symmetry": bool, "symmetry_x": float, "items": [...]}
        self.data = json_manager.new_armature_data()

        # Undo: up to UNDO_STACK_DEPTH snapshots per rig, keyed by
        # armature name, each taken right before an operation (drag,
        # toggle, selection change, appearance change, IK/FK switch flip,
        # etc.) on THAT rig changed something. Switching to a different
        # armature doesn't touch this - each rig keeps its own stack, so
        # switching away and back and pressing Ctrl+Z behaves exactly as
        # if no switch had happened in between. Each rig's stack is a
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
        # self.undo_snapshots, keyed by armature name - undo() pushes
        # onto this every time it pops something, and redo() pushes
        # onto self.undo_snapshots every time IT pops something, so
        # walking back and forth between the two behaves like a normal
        # two-directional undo history. Any genuine new operation
        # clears the current rig's redo stack entirely (see
        # _record_undo_snapshot()) - same as every other undo/redo
        # system, redoing only makes sense along the exact timeline you
        # just stepped back from.
        self.redo_snapshots = {}

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

    # ---------------------------------------------------------

    def set_window(self, window):
        self.window = window

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
    def _capture_pose_transforms(bone_names):
        """Snapshots location/rotation/scale for each named pose bone,
        for use as _record_undo_snapshot()'s pose_restore argument.
        Skips bones that don't exist on the current rig."""
        rig = backend.arm()
        if rig is None:
            return {}

        captured = {}
        for name in bone_names:
            pb = rig.pose.bones.get(name)
            if pb is None:
                continue

            captured[name] = {
                "rotation_mode": pb.rotation_mode,
                "location": tuple(pb.location),
                "rotation_quaternion": tuple(pb.rotation_quaternion),
                "rotation_euler": tuple(pb.rotation_euler),
                "scale": tuple(pb.scale),
            }

        return captured

    @staticmethod
    def _show_snapshot_in_viewport(changed):
        """Displays what the last undo restored in the 3D viewport's
        header, purely for visibility into the operation - does not
        affect what undo() restores or how."""
        text = f"RigPicker Undo: {', '.join(changed)}"

        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    area.header_text_set(text)

    def _record_undo_snapshot(self, ikfk_restore=None, pose_restore=None, clear_motion_path=False, recalculate_motion_path=False, selection_only=False):
        """Stores everything undo() needs to revert the picker UI to
        exactly how it looked right before the operation that's about
        to happen.

        Call this once, right before a user-initiated operation starts
        changing anything - e.g. at the start of a drag (not on every
        mouseMoveEvent), before a toggle/appearance/add/delete/selection
        change is applied, before the IK/FK switch flips a bone's
        IK_FK value, or before an FK<->IK snap moves bones. Pushes onto
        this rig's own undo stack (see self.undo_snapshots); once that
        stack holds UNDO_STACK_DEPTH entries, each further push quietly
        drops the oldest one.

        ikfk_restore: optional (parent_bone_name, previous_IK_FK_value)
        tuple - pass this when the operation is about to change a pose
        bone's "IK_FK" custom property (the IK/FK switch), so undo() can
        set it back.

        pose_restore: optional {bone_name: {"location": ..., "rotation_quaternion":
        ..., "rotation_euler": ..., "rotation_mode": ..., "scale": ...}}
        dict - pass this when the operation is about to move pose bones
        directly (e.g. an FK<->IK snap), so undo() can put those bones'
        transforms back. Unlike ikfk_restore (a single custom property),
        this restores the actual bone pose.

        clear_motion_path: pass True when the operation about to happen
        is calculating a motion path (rp.calculate_path). Blender draws
        motion paths from its own per-bone cache, which the picker's
        data dict knows nothing about, so reverting self.data alone
        can't make the drawn path disappear - undo() needs to be told
        to explicitly clear it too (the same as pressing the "x"
        button).

        recalculate_motion_path: the mirror of clear_motion_path - pass
        True when the operation about to happen is clearing a motion
        path (rp.clear_path), so undo() knows to recalculate it rather
        than leave it cleared. Redo also builds this pairing
        automatically on whichever mirror entry it constructs for the
        other stack (see _capture_reverse_entry()), so a redo of a
        calculate re-calculates it and an undo of a redo-of-a-clear
        clears it again - passing it explicitly here is only needed for
        the original, forward push of a genuine clear.

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
        """
        stack = self.undo_snapshots.setdefault(
            self.armature_name, deque(maxlen=UNDO_STACK_DEPTH)
        )

        # If the user clicked truly-empty canvas space right before this
        # operation, with nothing else in between, restore selection all
        # the way back to what it was before that deselect - not to the
        # momentarily-empty state it left behind. The deselect itself was
        # never treated as an operation, so undo shouldn't land on it.
        if self._pending_deselect_baseline is not None:
            selection_for_snapshot = self._pending_deselect_baseline
            self._pending_deselect_baseline = None
        else:
            selection_for_snapshot = self.selected_bones

        # Nothing was selected right before this pick, so this isn't a
        # switch between two selections - it's the start of one. Skip
        # pushing anything and let undo fall straight through to
        # whatever real state (or real operation) preceded this click.
        if selection_only and not selection_for_snapshot:
            return

        # A real operation immediately following a plain selection change
        # doesn't need that selection change to remain its own separate
        # undo step - the operation's own snapshot (pushed below) already
        # captures the live, just-made selection, so the trailing
        # selection_only entry would only ever be an intermediate, no-op
        # detour between two states undo already visits.
        if stack and stack[-1].get("selection_only") and not selection_only:
            stale = stack.pop()
            self._settle_snapshot_pending_image(stale, self.armature_name)

        # deque(maxlen=...) auto-evicts the oldest entry once full, but
        # silently - it doesn't hand the evicted item back. Pop it
        # ourselves first when the stack is already full, so its own
        # staged background-image delete/backup (if any) can be finalized
        # now that it's genuinely unreachable, rather than leaking a
        # file on disk that nothing will ever clean up.
        if len(stack) == stack.maxlen:
            evicted = stack.popleft()
            self._settle_snapshot_pending_image(evicted, self.armature_name)

        # A real, new operation happening now means whatever was
        # previously undone on this rig is no longer reachable by
        # redoing forward into it - this is the one point every genuine
        # push (selection switch or real edit alike) passes through, so
        # it's the right place to invalidate the old branch. Any
        # snapshots still on the redo stack that never got applied are
        # settled here (their staged background-image work, if any, is
        # now permanently moot).
        stale_redo_stack = self.redo_snapshots.pop(self.armature_name, None)
        if stale_redo_stack:
            for stale_redo_entry in stale_redo_stack:
                self._settle_snapshot_pending_image(stale_redo_entry, self.armature_name)

        stack.append({
            "data": copy.deepcopy(self.data),
            "selected_bones": set(selection_for_snapshot),
            "ikfk_restore": ikfk_restore,
            "pose_restore": pose_restore,
            "clear_motion_path": clear_motion_path,
            "recalculate_motion_path": recalculate_motion_path,
            "selection_only": selection_only,
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
        stack = self.undo_snapshots.get(self.armature_name)
        if not stack:
            return None
        return stack[-1]

    @staticmethod
    def _redraw_all_areas():
        """Tags every area in every window for a redraw. Pulled out as a
        helper because several undo-adjacent operations (an IK/FK flip,
        an FK<->IK snap's pose restore, a plain refresh()) each need one
        of these after changing pose data, and previously each copied
        the same loop rather than sharing it."""
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()

    def _settle_snapshot_pending_image(self, snapshot, armature_name=None):
        """Finalizes whatever background-image delete/backup a snapshot
        was carrying, once that snapshot itself is no longer reachable
        by undo (evicted from a full stack, or the rig it belongs to was
        deleted from the scene entirely). "Finalizing" a delete means
        actually performing it; "finalizing" a backup means discarding
        the backup copy, since the capture that overwrote it is now
        permanent and can never be undone back to it.

        armature_name: which rig this snapshot belongs to (pass
        self.armature_name from callers operating on the currently
        loaded rig's own stack, or the relevant key when iterating
        self.undo_snapshots/self.redo_snapshots directly). Used to guard
        against deleting a file that's back in active use - capture_view()
        always writes to the SAME fixed per-armature path
        (json_manager.get_image_path()), so "Clear All" (stages the old
        path for deletion once unreachable) followed by a fresh capture
        BEFORE that old snapshot ages off the stack reuses that exact
        path for the new, live image. Without this check, whichever of
        eviction/rig-deletion/window-close finalized that old snapshot
        first would delete the file the rig's CURRENT background is
        actively pointing at - invisible for the rest of the session
        (Qt already has the pixels loaded in memory), but gone the next
        time this rig's data is loaded from disk. If armature_name isn't
        given, this check is skipped (safe for backup paths, which are
        always uniquely suffixed per-operation and never collide with a
        live path anyway).
        """
        if snapshot is None:
            return

        image_path = snapshot.get("pending_image_delete")
        if image_path:
            still_live = False
            if armature_name:
                current = json_manager.get_armature_data(armature_name)
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
        for armature_name, stack in self.undo_snapshots.items():
            for snapshot in stack:
                self._settle_snapshot_pending_image(snapshot, armature_name)
        self.undo_snapshots.clear()

        for armature_name, stack in self.redo_snapshots.items():
            for snapshot in stack:
                self._settle_snapshot_pending_image(snapshot, armature_name)
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
            "ikfk_restore": None,
            "pose_restore": None,
            "clear_motion_path": False,
            "recalculate_motion_path": False,
            "selection_only": snapshot.get("selection_only", False),
            "pending_image_delete": None,
            "pending_image_backup": None,
        }

        # ikfk_restore mirror: capture the CURRENT IK_FK value for the
        # same bone before snapshot's own value overwrites it below, so
        # reversing this step later can put it back.
        if snapshot.get("ikfk_restore") is not None:
            parent_bone, _ = snapshot["ikfk_restore"]
            rig = backend.arm()
            if rig is not None:
                pb = rig.pose.bones.get(parent_bone)
                if pb is not None and "IK_FK" in pb:
                    reverse_entry["ikfk_restore"] = (parent_bone, float(pb["IK_FK"]))

        # pose_restore mirror: same idea, for the same set of bones.
        if snapshot.get("pose_restore"):
            reverse_entry["pose_restore"] = self._capture_pose_transforms(
                list(snapshot["pose_restore"].keys())
            )

        # Motion path: whichever direction snapshot is about to go
        # (clear or recalculate), reversing it later needs to do the
        # opposite.
        if snapshot.get("clear_motion_path"):
            reverse_entry["recalculate_motion_path"] = True
        if snapshot.get("recalculate_motion_path"):
            reverse_entry["clear_motion_path"] = True

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

    def _push_reverse_entry(self, stacks_dict, entry):
        """Pushes `entry` onto this rig's stack within `stacks_dict`
        (either self.undo_snapshots or self.redo_snapshots), applying
        the exact same maxlen-eviction handling _record_undo_snapshot()
        does for a normal push - keeps undo() and redo()'s own pushes
        consistent with it instead of duplicating the eviction logic a
        third time."""
        stack = stacks_dict.setdefault(
            self.armature_name, deque(maxlen=UNDO_STACK_DEPTH)
        )
        if len(stack) == stack.maxlen:
            evicted = stack.popleft()
            self._settle_snapshot_pending_image(evicted, self.armature_name)
        stack.append(entry)

    def undo(self):
        """Reverts the current rig's picker UI to exactly how it looked
        right before the most recent still-remembered operation on THIS
        rig: picker data (control positions, appearance, symmetry,
        background, checkboxes, ...), the current control selection/
        highlighting, and - if that operation was flipping the IK/FK
        switch - the affected bone's IK_FK value.

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
        """
        stack = self.undo_snapshots.get(self.armature_name)
        if not stack:
            return

        snapshot = stack.pop()

        # Capture the mirror-image redo entry from the CURRENT live
        # state before anything below changes it.
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
        self.save()
        self.refresh()

        self._show_snapshot_in_viewport(changed)

        ikfk_restore = snapshot["ikfk_restore"]
        if ikfk_restore is not None:
            parent_bone, prev_value = ikfk_restore

            rig = backend.arm()
            if rig is not None:
                pb = rig.pose.bones.get(parent_bone)

                if pb is not None and "IK_FK" in pb:
                    pb["IK_FK"] = prev_value

                    pb.keyframe_insert(
                        data_path='["IK_FK"]',
                        frame=bpy.context.scene.frame_current
                    )

                    rig.update_tag(refresh={'DATA'})
                    bpy.context.view_layer.update()

                    self._redraw_all_areas()

        pose_restore = snapshot.get("pose_restore")
        if pose_restore:
            rig = backend.arm()
            if rig is not None:
                for bone_name, xform in pose_restore.items():
                    pb = rig.pose.bones.get(bone_name)
                    if pb is None:
                        continue

                    pb.rotation_mode = xform["rotation_mode"]
                    pb.location = xform["location"]
                    pb.rotation_quaternion = xform["rotation_quaternion"]
                    pb.rotation_euler = xform["rotation_euler"]
                    pb.scale = xform["scale"]

                    frame = bpy.context.scene.frame_current
                    pb.keyframe_insert(data_path="location", frame=frame)
                    pb.keyframe_insert(data_path="rotation_quaternion", frame=frame)
                    pb.keyframe_insert(data_path="rotation_euler", frame=frame)
                    pb.keyframe_insert(data_path="scale", frame=frame)

                rig.update_tag(refresh={'DATA'})
                bpy.context.view_layer.update()

                self._redraw_all_areas()

        # Restore which controls were selected/highlighted. Drop any
        # bone that no longer exists as a control post-restore (e.g. an
        # add/delete was what's being undone).
        current_bone_names = {item["bone_name"] for item in self.data["items"]}
        self.selected_bones = snapshot["selected_bones"] & current_bone_names

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

        # If what's being undone was a "Calculate" motion-path press,
        # also do what the "x" (clear) button does - Blender's drawn
        # motion path lives in its own per-bone cache, separate from
        # the picker data restored above, so it won't disappear from
        # the viewport unless we clear it explicitly here too.
        if snapshot.get("clear_motion_path"):
            try:
                bpy.ops.rp.clear_path()
            except Exception:
                import traceback
                traceback.print_exc()

        # The mirror-image case: this snapshot came from redo()'s own
        # push (see _capture_reverse_entry()) because what's being
        # undone right now was itself a redo of a "Calculate" press -
        # recalculate the path rather than leaving it cleared.
        if snapshot.get("recalculate_motion_path"):
            try:
                bpy.ops.rp.calculate_path()
            except Exception:
                import traceback
                traceback.print_exc()

        self.refresh_ikfk_label()

    def redo(self):
        """The Ctrl+Shift+Z counterpart to undo() - re-applies whatever
        was last undone on the currently loaded rig, provided nothing
        new has happened on it since (see _record_undo_snapshot(),
        which clears this rig's redo stack the instant a fresh
        operation is recorded - same as every other undo/redo system,
        redoing only makes sense along the exact timeline just stepped
        back from).

        Mirrors undo() in every respect - same per-rig stack depth,
        same ikfk_restore/pose_restore/motion-path/background-image
        handling, same selection restore - just walking forward instead
        of back. Every step taken forward is pushed onto this rig's
        undo stack first, so undo() can immediately reverse it again.
        """
        stack = self.redo_snapshots.get(self.armature_name)
        if not stack:
            return

        snapshot = stack.pop()

        self._push_reverse_entry(
            self.undo_snapshots, self._capture_reverse_entry(snapshot)
        )

        snapshot["pending_image_delete"] = None
        self._restore_snapshot_pending_image_backup(snapshot)

        changed = self._diff_items(self.data, snapshot["data"])

        self.data = snapshot["data"]
        self.save()
        self.refresh()

        self._show_snapshot_in_viewport(changed)

        ikfk_restore = snapshot.get("ikfk_restore")
        if ikfk_restore is not None:
            parent_bone, value = ikfk_restore

            rig = backend.arm()
            if rig is not None:
                pb = rig.pose.bones.get(parent_bone)

                if pb is not None and "IK_FK" in pb:
                    pb["IK_FK"] = value

                    pb.keyframe_insert(
                        data_path='["IK_FK"]',
                        frame=bpy.context.scene.frame_current
                    )

                    rig.update_tag(refresh={'DATA'})
                    bpy.context.view_layer.update()

                    self._redraw_all_areas()

        pose_restore = snapshot.get("pose_restore")
        if pose_restore:
            rig = backend.arm()
            if rig is not None:
                for bone_name, xform in pose_restore.items():
                    pb = rig.pose.bones.get(bone_name)
                    if pb is None:
                        continue

                    pb.rotation_mode = xform["rotation_mode"]
                    pb.location = xform["location"]
                    pb.rotation_quaternion = xform["rotation_quaternion"]
                    pb.rotation_euler = xform["rotation_euler"]
                    pb.scale = xform["scale"]

                    frame = bpy.context.scene.frame_current
                    pb.keyframe_insert(data_path="location", frame=frame)
                    pb.keyframe_insert(data_path="rotation_quaternion", frame=frame)
                    pb.keyframe_insert(data_path="rotation_euler", frame=frame)
                    pb.keyframe_insert(data_path="scale", frame=frame)

                rig.update_tag(refresh={'DATA'})
                bpy.context.view_layer.update()

                self._redraw_all_areas()

        current_bone_names = {item["bone_name"] for item in self.data["items"]}
        self.selected_bones = snapshot["selected_bones"] & current_bone_names

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

        if snapshot.get("clear_motion_path"):
            try:
                bpy.ops.rp.clear_path()
            except Exception:
                import traceback
                traceback.print_exc()

        if snapshot.get("recalculate_motion_path"):
            try:
                bpy.ops.rp.calculate_path()
            except Exception:
                import traceback
                traceback.print_exc()

        self.refresh_ikfk_label()

    def _sync_viewport_selection(self):
        """Pushes self.selected_bones onto the actual viewport selection
        by reusing the same operator select_control() relies on (rp.select)
        - rather than re-implementing bone-selection logic here."""
        try:
            selected = list(self.selected_bones)

            if not selected:
                # Just deselect - this used to call bpy.ops.rp.hide_all(),
                # which doesn't just clear the selection, it hides every
                # pose bone in the viewport. Hiding bones should only ever
                # happen from the explicit "H" hotkey/Hide All action, not
                # as a side effect of an undo landing on an empty
                # selection.
                from ..backend import arm, get_3d_override, ensure_pose_mode

                rig = arm()
                if rig and rig.type == "ARMATURE":
                    override = get_3d_override(bpy.context, rig)
                    if override:
                        with bpy.context.temp_override(**override):
                            # rp.select (used by the non-empty branch
                            # below) always sets this explicitly before
                            # calling ensure_pose_mode() - the
                            # "active_object" key in the override dict
                            # alone isn't enough for mode_set()/
                            # select_all()'s polls to reliably pick up
                            # the rig as active. Without it, both calls
                            # can silently no-op (CANCELLED, no
                            # exception) if the rig wasn't already the
                            # "real" active object at this exact moment -
                            # leaving the previous selection stuck in the
                            # viewport even though the picker UI already
                            # shows nothing selected.
                            bpy.context.view_layer.objects.active = rig
                            ensure_pose_mode(bpy.context, rig)

                            # Whenever a control WAS selected, rp.select
                            # (below) hides every other bone via
                            # pose.hide(unselected=True) to "solo" it in
                            # the viewport. Landing back on an empty
                            # selection needs to undo that explicitly -
                            # without this reveal, any bone that a prior
                            # selection hid stayed hidden forever, even
                            # though the picker UI now shows nothing
                            # selected (same "show all" behavior
                            # deselect_all()/_show_all_and_deselect()
                            # already give via rp.show_all()).
                            bpy.ops.pose.reveal(select=False)
                            bpy.ops.pose.select_all(action="DESELECT")
                        rig.data.bones.active = None

                backend._LAST_SELECTED_BONES = frozenset()
                return

            # First bone resets selection to just itself (shift=False);
            # each subsequent bone is added on top (shift=True) - the
            # same sequence a user gets from clicking one control, then
            # shift-clicking the rest.
            for i, name in enumerate(selected):
                bpy.ops.rp.select(bone_name=name, shift=(i > 0))

            backend._LAST_SELECTED_BONES = frozenset(self.selected_bones)

        except Exception:
            import traceback
            traceback.print_exc()

    # ---------------------------------------------------------
    # ARMATURE SWITCHING
    # ---------------------------------------------------------

    def _armature_is_hidden(self, obj):
        """True if this armature is currently hidden in the viewport
        (the eye-icon / H-key hide toggle - bpy's hide_get()/hide_set()).
        Used to grey out hidden rigs in the dropdown rather than remove
        them outright, since a hidden rig's picker data (controls,
        background, etc.) is still perfectly valid - it just can't be
        made active/selected in the viewport while hidden, which is what
        change_armature() needs to do.
        """
        try:
            return obj.hide_get()
        except (AttributeError, RuntimeError):
            # hide_get() can raise if the object isn't in the current
            # view layer at all - treat that as "not selectable" too,
            # though _sync_armature_combo_items() already filters those
            # out of the list entirely before this ever gets called.
            return False

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
        scene_armatures = set(armature_objects)

        if current_items == scene_armatures:
            # Nothing added/removed/renamed, but a rig's hide state may
            # have changed - that doesn't touch the name set at all, so
            # it wouldn't be caught by anything below. Keep the enabled/
            # disabled state of each entry in sync regardless of whether
            # a full rebuild happens this call.
            self._sync_armature_combo_enabled_state(combo, armature_objects)
            return

        # Any name that was selectable a moment ago but is no longer an
        # armature in the scene was deleted - not just renamed (a rename
        # would still show up as a "new" name in scene_armatures, so it
        # isn't caught here, only genuine removals are). The picker's own
        # data lives entirely in rig_picker_data.json, keyed by armature
        # name, and nothing else ever prunes that file - the dropdown
        # simply "forgetting" the name left its JSON entry (controls,
        # background image path, everything) as permanent orphaned data
        # with no UI path to reach or clear it. Delete it here, at the
        # same point the deletion is actually detected, the same way
        # clear_all() would for the currently-loaded armature.
        deleted_armatures = current_items - scene_armatures
        for name in deleted_armatures:
            json_manager.delete_armature_data(name)
            # A deleted rig can never be switched back to, so its undo
            # AND redo history (and any pending image delete/backup
            # riding along with each of their snapshots) has nowhere
            # left to ever be resolved - discard both stacks outright
            # rather than leaving them in the dicts forever. Only undo
            # was being popped here before - the redo stack for a
            # deleted rig (e.g. one that still had an un-redone undo
            # sitting on it) was left behind permanently instead.
            stale_stack = self.undo_snapshots.pop(name, None)
            if stale_stack:
                for stale_snapshot in stale_stack:
                    self._settle_snapshot_pending_image(stale_snapshot, name)

            stale_redo_stack = self.redo_snapshots.pop(name, None)
            if stale_redo_stack:
                for stale_redo_entry in stale_redo_stack:
                    self._settle_snapshot_pending_image(stale_redo_entry, name)

        combo.blockSignals(True)
        combo.clear()
        for name in sorted(scene_armatures):
            combo.addItem(name)
        combo.blockSignals(False)

        self._sync_armature_combo_enabled_state(combo, armature_objects)

        # The armature currently loaded into the picker can itself be
        # one of the deleted ones (e.g. the user deletes the rig that's
        # actively showing in the picker, and nothing else becomes the
        # active object afterwards). Its data was just wiped from JSON
        # above, but self.armature_name/self.data still reference it in
        # memory - left alone, the next self.save() (fired by all sorts
        # of routine picker actions) would simply write self.data back
        # out under that name, silently undoing the delete. Clear the
        # in-memory state directly instead of going through
        # load_armature(None), which would call self.save() FIRST and
        # resurrect the very entry this method just removed.
        if self.armature_name in deleted_armatures:
            self.armature_name = None
            self.data = json_manager.new_armature_data()
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
            self.refresh()

            self.window.set_no_selection_defaults()

        # Qt auto-selects the first item the moment a combo goes from
        # empty to non-empty, which happens on every clear()+addItem()
        # rebuild above - regardless of which rig is actually still
        # loaded. So even when the *deleted* armature wasn't the one
        # loaded in the picker (some other, unrelated rig was removed),
        # the dropdown could end up visibly showing the wrong name while
        # self.armature_name still correctly points at the real one.
        # Re-apply the displayed selection from self.armature_name every
        # time the list is rebuilt, the same way load_armature() does.
        combo.blockSignals(True)
        target_text = self.armature_name or ""
        if target_text:
            index = combo.findText(target_text)
            combo.setCurrentIndex(index)
        else:
            combo.setCurrentIndex(-1)
        combo.blockSignals(False)

    def load_armature(self, rig):
        """Saves the current picker to JSON, then loads the given armature's
        picker (or a blank one) from JSON and refreshes the UI."""

        # Persist whatever is currently loaded before switching away from it.
        self.save()

        self.selected_bones = set()

        # Nothing to do here for undo anymore - each rig's stack lives
        # in self.undo_snapshots[rig_name], including any pending
        # background-image delete/backup each of its snapshots is
        # carrying (see _record_undo_snapshot(), _stage_image_backup()),
        # so it's already exactly where it needs to be regardless of
        # which rig is currently loaded. Switching rigs doesn't touch
        # any of it.

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

        if rig is None:
            self.armature_name = None
            self.data = json_manager.new_armature_data()
        else:
            self.armature_name = rig.name
            stored = json_manager.get_armature_data(rig.name)
            self.data = stored if stored is not None else json_manager.new_armature_data()

        self.refresh()

        if self.window is not None:
            self.window.set_no_selection_defaults()

            self._sync_armature_combo_items()

            # Keep the dropdown's displayed text in sync when the active
            # armature changes from outside the combo itself (e.g. clicking
            # a different rig in the 3D viewport, detected by
            # backend.poll_active_armature). Block signals while setting it
            # so this doesn't re-trigger change_armature() and recurse back
            # into load_armature().
            combo = self.window.armature_combo
            target_text = self.armature_name or ""
            if combo.currentText() != target_text:
                combo.blockSignals(True)
                index = combo.findText(target_text)
                if index >= 0:
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
        if armature_name not in view_layer.objects:
            self._sync_armature_combo_items()
            return

        backend._CACHED_ARM = rig
        backend._CACHED_ARM_NAME = rig.name

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

    def save(self):
        """Writes the current in-memory data to rig_picker_data.json."""
        if self.armature_name:
            json_manager.save_armature_data(self.armature_name, self.data)

    def find_item(self, bone_name):
        return next(
            (item for item in self.data["items"] if item["bone_name"] == bone_name),
            None,
        )

    def _resolve_ikfk_group(self):
        """Resolves the single IK/FK group that ALL of self.selected_bones
        belong to.

        Shift-selecting several controls that are all part of the same
        limb (e.g. upper_arm_fk.L + forearm_fk.L, both under the
        "upper_arm_parent.L" group) should still be treated as one valid
        selection for IK/FK purposes - the old code only ever looked at
        the selection when it contained exactly one bone, so snapping
        silently did nothing the moment a second control was shift-added.

        Returns (parent_bone_name, group_dict) if every selected bone
        resolves to the same group, otherwise (None, None) - e.g. when
        nothing is selected, a selected bone isn't part of any IK/FK
        group, or the selection spans two different groups (which has no
        single well-defined snap target).
        """
        if not self.selected_bones:
            return None, None

        resolved_parents = set()

        for bone in self.selected_bones:
            match = None

            for parent, group in IK_FK_GROUPS.items():
                if (
                    bone == parent
                    or bone in group["output_bones"]
                    or bone in group["input_bones"]
                    or bone in group["ctrl_bones"]
                ):
                    match = parent
                    break

            if match is None:
                return None, None

            resolved_parents.add(match)

        if len(resolved_parents) != 1:
            return None, None

        parent = next(iter(resolved_parents))
        return parent, IK_FK_GROUPS[parent]

    def refresh_ikfk_label(self):
        """Updates the IK/FK toggle button to show the state it will
        switch TO if clicked (e.g. "->IK" while currently in FK), based
        on the currently selected control(s)' live IK_FK property. Shows a
        neutral label when the current selection doesn't resolve to a
        single IK/FK group.
        """
        if self.window is None:
            return

        is_fk = None

        parent_bone, group = self._resolve_ikfk_group()

        if parent_bone is not None:
            rig = backend.arm()

            if rig is not None:
                pb = rig.pose.bones.get(parent_bone)

                if pb is not None and "IK_FK" in pb:
                    # Rigify convention: 0.0 = IK, 1.0 = FK
                    is_fk = float(pb["IK_FK"]) >= 0.5

        self.window.control_list.container.update_ikfk_toggle(is_fk)

    # ---------------------------------------------------------

    def _update_action_controls_enabled(self):
        """Add Selected / Clear All / Delete, and the Symmetry / IK-FK /
        Motion Paths checkboxes, all operate on controls placed relative
        to a background image - so keep them disabled until this rig
        actually has one, rather than letting someone add/delete
        controls or enable symmetry against a canvas with nothing to
        position against. Capture View itself is deliberately left
        alone: it's what produces the background in the first place, so
        gating it the same way would make it impossible to ever get
        past this state.

        Delete additionally requires a non-empty self.selected_bones -
        it's a no-op with nothing selected (see delete_selected()'s own
        early "if not self.selected_bones: return"), so leaving it
        clickable in that state invited a confusing no-op click. Call
        this after every place that changes self.selected_bones, not
        just after background/armature changes.
        """
        if self.window is None:
            return

        # Gate on whether an image is actually loaded onto the canvas
        # right now, not on self.data["background"] being a non-empty
        # path string. A stored path can point at a file that no longer
        # loads (missing, moved .blend, corrupted) - set_background()
        # falls back to a clean canvas in that case, and this must agree
        # with what's on screen rather than what's merely on record, or
        # these stay enabled against an image the user can't see.
        has_background = self.window.control_list.container.background is not None

        self.window.add_button.setEnabled(has_background)
        self.window.clear_button.setEnabled(has_background)
        self.window.delete_button.setEnabled(
            has_background and bool(self.selected_bones)
        )

        self.window.symmetry_checkbox.setEnabled(has_background)
        self.window.ik_fk_checkbox.setEnabled(has_background)
        self.window.motion_paths_checkbox.setEnabled(has_background)

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

        # Sync the checkbox to match the loaded armature's stored value.
        # blockSignals() prevents this from re-triggering toggle_symmetry(),
        # which would otherwise immediately re-save the value we just loaded
        # (harmless, but pointless) and could clobber symmetry_x if the
        # background isn't set up yet.
        self.window.symmetry_checkbox.blockSignals(True)
        self.window.symmetry_checkbox.setChecked(
            self.data.get("symmetry", False)
        )
        self.window.symmetry_checkbox.blockSignals(False)

        self.window.ik_fk_checkbox.blockSignals(True)
        self.window.ik_fk_checkbox.setChecked(
            self.data.get("ik_fk_enabled", False)
        )
        self.window.ik_fk_checkbox.blockSignals(False)

        canvas.ikfk_controls_enabled = self.data.get("ik_fk_enabled", False)

        self.window.motion_paths_checkbox.blockSignals(True)
        self.window.motion_paths_checkbox.setChecked(
            self.data.get("motion_paths_enabled", False)
        )
        self.window.motion_paths_checkbox.blockSignals(False)

        canvas.motion_paths_controls_enabled = self.data.get("motion_paths_enabled", False)
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
            # add_control()'s own fallback grid, based purely on where it
            # falls in this list AT THIS MOMENT (its index among however
            # many controls exist right now). Never persisting that
            # computed position back into self.data meant every single
            # refresh() recomputed it from scratch - so deleting an
            # earlier control (or adding a new one anywhere before it)
            # shifted every OTHER still-unpositioned control's grid index,
            # making them silently jump to new spots even though nothing
            # about them was touched. Saving it back the very first time
            # locks that control's position in for good from then on -
            # exactly as if the user had dragged it there themselves -
            # so later deletions/additions elsewhere in the list can no
            # longer move it.
            if had_no_position and control is not None:
                item["x"] = control.image_position.x()
                item["y"] = control.image_position.y()
                positions_pinned = True

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

        self.refresh_ikfk_label()

    # ---------------------------------------------------------

    def add_selected(self):
        from ..backend import arm, mirror_name, control_dimensions

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
            # flag set. Checking collection visibility directly fixes
            # both cases. A bone with no collections at all is never
            # hidden by this check, since collections only ever restrict
            # visibility for bones actually assigned to one.
            collections = list(pb.bone.collections)
            if collections and not any(c.is_visible for c in collections):
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
            }

            mirror = mirror_name(pb.name)

            if self.data.get("symmetry") and mirror is not None:
                mirror_item = self.find_item(mirror)

                # Same sentinel gap as the resize/reshape fix above: a
                # mirror control that was itself just added and never
                # dragged still has mirror_item["x"]/["y"] stuck at the
                # -1.0 "unset" sentinel, even though it already has a
                # real position via its widget's image_position. Gating
                # on the data sentinel alone meant adding a new control
                # whose mirror was freshly auto-placed (not yet dragged)
                # silently fell back to the default grid position instead
                # of being placed symmetrically opposite it. Prefer the
                # live widget's position when one exists, and only fall
                # back to the stored data position otherwise.
                mirror_widget = self.window.control_list.controls.get(mirror)

                if mirror_widget is not None and hasattr(
                    mirror_widget, "image_position"
                ):
                    mirror_x = mirror_widget.image_position.x()
                    mirror_y = mirror_widget.image_position.y()
                    mirror_has_position = True
                elif mirror_item and mirror_item["x"] >= 0 and mirror_item["y"] >= 0:
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

        self.refresh_ikfk_label()

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

        self.refresh_ikfk_label()

    def set_selected_size(self, size):
        self._set_selected_appearance(size=size)

    def set_selected_shape(self, shape):
        self._set_selected_appearance(shape=shape)

    def set_selected_color(self, color):
        self._set_selected_appearance(color=color)

    def _set_selected_appearance(self, size=None, shape=None, color=None):
        if not self.selected_bones:
            # Nothing selected - the combos are showing/editing the
            # *default* appearance (see set_no_selection_defaults()),
            # not any actual control, so update that default instead
            # of touching picker data. Not undoable and not saved to
            # disk: it's a live default for controls not created yet,
            # not a change to anything that already exists.
            if size is not None:
                self.default_control_size = size

            if shape is not None:
                self.default_control_shape = shape

            if color is not None:
                self.default_control_color = color

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

    def calculate_motion_path(self):
        if not self.selected_bones:
            return

        self._record_undo_snapshot(clear_motion_path=True)
        bpy.ops.rp.calculate_path()

    def clear_motion_path(self):
        if not self.selected_bones:
            return

        # Missing an undo snapshot here meant clicking "Clear" (the "x"
        # button) wasn't undoable at all - Ctrl+Z right after clicking it
        # would skip straight past this action and revert whatever REAL
        # operation happened before it instead, which is both surprising
        # and inconsistent with calculate_motion_path() (the "Calculate"
        # button), which already records one. recalculate_motion_path=True
        # tells undo() to re-run rp.calculate_path() when this snapshot
        # is stepped back through, bringing the just-cleared path back -
        # the exact mirror of what clear_motion_path=True already does
        # for the Calculate button.
        self._record_undo_snapshot(recalculate_motion_path=True)
        bpy.ops.rp.clear_path()

    def show_all(self):
        self._record_undo_snapshot()
        bpy.ops.rp.show_all()

        for item in self.data["items"]:
            item["hidden"] = False
        self.save()

    # ---------------------------------------------------------

    def hide_all(self):
        self._record_undo_snapshot()
        bpy.ops.rp.hide_all()

        for item in self.data["items"]:
            item["hidden"] = True
        self.save()

    def capture_view(self):
        if not self.armature_name:
            return

        bpy.ops.rp.capture_view()

        # RP_OT_CaptureView always renders to this fixed, shared temp path.
        temp_path = os.path.join(
            tempfile.gettempdir(),
            "rig_picker_capture.png"
        )

        if not os.path.exists(temp_path):
            return

        # Copy it into this armature's own dedicated image file so it can
        # never be overwritten by capturing a different armature's view.
        dest_path = json_manager.get_image_path(self.armature_name)

        self._record_undo_snapshot()

        # This file is always the same fixed path for this armature, so
        # simply overwriting it would permanently lose the previous
        # image's pixels - even though undo() below restores
        # self.data["background"] to that very same, unchanged path.
        # Back the existing file up first so undo can bring the actual
        # old image back, not just an unchanged path string now pointing
        # at the new one.
        self._stage_image_backup(dest_path)

        shutil.copyfile(temp_path, dest_path)

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
        self._record_undo_snapshot()

        self.data["items"] = []

        # Clear the captured background too, not just the controls - but
        # don't delete the actual image file from disk yet. This clear
        # is still undoable (up to UNDO_STACK_DEPTH operations deep),
        # and undo() restores self.data["background"] to point back at
        # this same path, so deleting the file now would make undo bring
        # back a reference to a file that no longer exists. Instead,
        # stage it for deletion on the snapshot _record_undo_snapshot()
        # just pushed above: _settle_snapshot_pending_image() removes it
        # for real once that snapshot is no longer reachable - either it
        # falls off the back of this rig's undo stack, this rig gets
        # deleted from the scene entirely, or the picker window closes.
        image_path = self.data.get("background")

        self.data["background"] = ""
        self.data["image_offset_x"] = 0.0
        self.data["image_offset_y"] = 0.0

        # Reset the Symmetry / IK-FK / Motion Paths checkboxes and the
        # symmetry guide's saved position along with everything else -
        # "Clear All" previously only cleared controls and the
        # background, silently leaving these checked/positioned from
        # before. A cleared rig with no background/controls has nothing
        # for symmetry to mirror across or IK/FK-snap between anyway, so
        # leaving them on was more confusing than useful. This also
        # means a fresh symmetry_x will get computed cleanly (in the
        # current image-space, not a stale one) if the user re-enables
        # Symmetry after loading a new background.
        self.data["symmetry"] = False
        self.data["symmetry_x"] = -1.0
        self.data["ik_fk_enabled"] = False
        self.data["motion_paths_enabled"] = False

        # Same reset load_armature()/change_armature() already do when
        # switching rigs (see their matching comments) - the size/shape/
        # color defaults are live-editable per set_no_selection_defaults()
        # while nothing is selected, so without this, a default tweaked
        # before clearing would silently keep applying to new controls
        # added after the clear, on what's now effectively a blank slate.
        self.default_control_size = 18
        self.default_control_shape = "CIRCLE"
        self.default_control_color = "GREEN"

        self.save()

        snapshot = self._current_snapshot()
        if snapshot is not None:
            snapshot["pending_image_delete"] = image_path or None

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

    def delete_selected(self):
        if not self.selected_bones:
            return

        self._record_undo_snapshot()

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

        self.data["items"] = [
            item for item in self.data["items"]
            if item["bone_name"] not in selected
        ]
        self.save()

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

    def select_all(self):
        # Same reasoning as select_control(): a plain selection change
        # gets its own selection_only snapshot, so it stays individually
        # undoable unless a real operation immediately follows, in which
        # case that operation's own push discards this one instead of
        # stacking on top of it. Skip the push entirely if every control
        # is already selected - nothing would actually change, so it'd
        # just be a no-op step eating a Ctrl+Z press.
        all_bones = set(self.window.control_list.controls.keys())
        if all_bones != self.selected_bones:
            self._record_undo_snapshot(selection_only=True)
        self.selected_bones = all_bones

        # Update UI widgets
        for name, widget in self.window.control_list.controls.items():
            widget.active = True
            widget.update()

        from ..backend import arm
        rig = arm()

        from ..backend import (
            get_3d_override,
            ensure_pose_mode,
        )

        if rig and rig.type == "ARMATURE":
            override = get_3d_override(bpy.context, rig)

            if override:
                with bpy.context.temp_override(**override):
                    bpy.context.view_layer.objects.active = rig
                    ensure_pose_mode(bpy.context, rig)

                    bpy.ops.pose.reveal(select=False)
                    bpy.ops.pose.select_all(action="SELECT")

        # Same leak select_control()/deselect_all()/_show_all_and_deselect()
        # had: this pushes the viewport selection directly (a bulk
        # pose.select_all rather than per-bone rp.select calls, since
        # that's far cheaper for a rig with many controls) but must still
        # update backend._LAST_SELECTED_BONES itself - nothing else here
        # does. Left stale, the next poll tick would see the viewport's
        # now-fully-selected bones as a "change" it needs to sync back in,
        # which happens to be harmless *if* everything above genuinely
        # succeeded (it'd just resync to the same set) - but if the reveal/
        # select_all ever silently failed (get_3d_override() returning
        # None is already guarded above, but the operators themselves can
        # still no-op), a stale _LAST_SELECTED_BONES is exactly what lets
        # a since-invalidated viewport selection get synced back into
        # self.selected_bones unnoticed later.
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

        self._update_action_controls_enabled()

        self.refresh_ikfk_label()

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

        self.refresh_ikfk_label()

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

    def toggle_ik_fk_setting(self, enabled):
        """Persists the IK-FK checkbox state and shows/hides the IK/FK
        switch plus its two snapping buttons (FK->IK, IK->FK) on the
        canvas accordingly."""
        self._record_undo_snapshot()

        self.data["ik_fk_enabled"] = enabled

        canvas = self.window.control_list.container
        canvas.ikfk_controls_enabled = enabled
        canvas.update_overlay_buttons()

        # Was missing entirely - unlike its two sibling toggles
        # (toggle_symmetry(), toggle_motion_paths_setting()), this never
        # wrote the checkbox state to disk. It looked correct for the
        # rest of the session (self.data and the canvas were both
        # updated), but switching to a different rig and back, or
        # closing and reopening Blender, silently reverted it to
        # whatever was last actually saved.
        self.save()

    def toggle_motion_paths_setting(self, enabled):
        """Persists the Motion Paths checkbox state and shows/hides the
        Calculate/Clear motion-path buttons on the canvas accordingly."""
        self._record_undo_snapshot()

        self.data["motion_paths_enabled"] = enabled

        canvas = self.window.control_list.container
        canvas.motion_paths_controls_enabled = enabled
        canvas.update_overlay_buttons()

        self.save()
    def toggle_ik_fk(self):

        parent_bone, group = self._resolve_ikfk_group()

        if parent_bone is None:
            return

        rig = backend.arm()
        if rig is None:
            return

        pb = rig.pose.bones.get(parent_bone)
        if pb is None or "IK_FK" not in pb:
            return

        current = float(pb["IK_FK"])

        self._record_undo_snapshot(ikfk_restore=(parent_bone, current))

        pb["IK_FK"] = 0.0 if current >= 0.5 else 1.0

        pb.keyframe_insert(
            data_path='["IK_FK"]',
            frame=bpy.context.scene.frame_current
        )

        rig.update_tag(refresh={'DATA'})
        bpy.context.view_layer.update()

        self._redraw_all_areas()

        self.refresh_ikfk_label()
    

    def fk_to_ik(self):

        parent_bone, group = self._resolve_ikfk_group()

        if group is None:
            return

        all_group_bones = (
            group["output_bones"]
            + group["input_bones"]
            + group["ctrl_bones"]
            + group.get("tail_bones", [])
            + group.get("extra_ctrls", [])
        )
        pose_restore = self._capture_pose_transforms(all_group_bones)
        self._record_undo_snapshot(pose_restore=pose_restore)

        rig = backend.arm()

        bpy.context.view_layer.objects.active = rig
        rig.select_set(True)

        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != "VIEW_3D":
                    continue

                for region in area.regions:
                    if region.type != "WINDOW":
                        continue

                    with bpy.context.temp_override(
                        window=window,
                        area=area,
                        region=region,
                        active_object=rig,
                        object=rig,
                    ):

                        bpy.ops.pose.rigify_generic_snap_oeujyfjmc8c1f286(
                            output_bones=str(group["output_bones"]).replace("'", '"'),
                            input_bones=str(group["input_bones"]).replace("'", '"'),
                            ctrl_bones=str(group["ctrl_bones"]).replace("'", '"'),
                            tooltip="FK to IK",
                            locks=(False, False, False),
                        )

                        bpy.context.view_layer.update()

                        self._redraw_all_areas()

                        self.refresh_ikfk_label()
                        return


    def ik_to_fk(self):

        parent_bone, group = self._resolve_ikfk_group()

        if group is None:
            return

        all_group_bones = (
            group["output_bones"]
            + group["input_bones"]
            + group["ctrl_bones"]
            + group.get("tail_bones", [])
            + group.get("extra_ctrls", [])
        )
        pose_restore = self._capture_pose_transforms(all_group_bones)
        self._record_undo_snapshot(pose_restore=pose_restore)

        rig = backend.arm()

        bpy.context.view_layer.objects.active = rig
        rig.select_set(True)

        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type != "VIEW_3D":
                    continue

                for region in area.regions:
                    if region.type != "WINDOW":
                        continue

                    with bpy.context.temp_override(
                        window=window,
                        area=area,
                        region=region,
                        active_object=rig,
                        object=rig,
                    ):

                        bpy.ops.pose.rigify_limb_ik2fk_oeujyfjmc8c1f286(
                            prop_bone=parent_bone,
                            pole_prop="pole_vector",

                            fk_bones=str(group["output_bones"]).replace("'", '"'),
                            ik_bones=str(group["input_bones"]).replace("'", '"'),
                            ctrl_bones=str(group["ctrl_bones"]).replace("'", '"'),

                            tail_bones=str(group.get("tail_bones", [])).replace("'", '"'),
                            extra_ctrls=str(group.get("extra_ctrls", [])).replace("'", '"'),
                        )

                        bpy.context.view_layer.update()

                        self._redraw_all_areas()

                        self.refresh_ikfk_label()
                        return