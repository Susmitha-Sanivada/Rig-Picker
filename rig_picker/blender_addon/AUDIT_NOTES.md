# Audit notes

## `_settings` reserved-key collision (fixed)

`json_manager.py` used to store per-armature picker data and the
global window-settings entry as siblings at the same dict level,
keyed by armature name and the reserved string `"_settings"`
respectively. An armature literally named `_settings` collided with
that reserved key: touching its picker data ran it through
`_migrate_armature_entry()`, which cleared and overwrote the real
settings entry in place, and the next window resize/move then raised
`KeyError: 'window'`.

Fixed by moving to a genuinely separate namespace instead of a
reserved name sharing the armature-name namespace:

```json
{
    "settings": { "window": { ... } },
    "armatures": { "ArmatureName": { ... }, ... }
}
```

No armature name can collide with `"settings"` anymore, because
armature names are never top-level keys - they only ever live inside
`"armatures"`. `_migrate_top_level()` upgrades an existing file (old
flat layout, or a file already corrupted by the old bug) to this shape
on first load; nothing else needed to change on disk or in the rest of
the addon, since every other module only ever went through
`json_manager`'s public functions.

## Line endings (fixed)

`backend.py`, `dependency.py`, `operators.py`, `ui/circle_control.py`,
`ui/controller.py`, `ui/flow_layout.py`, `ui/launcher.py`,
`ui/main_window.py`, `ui/toggle_switch.py`, and `ui/themes/blender_dark.qss`
previously had CRLF line endings (several of them a mix of CRLF *and*
LF within the same file). All files in the addon are now plain LF.

`.gitattributes` now forces `eol=lf` for source/text files, so this
shouldn't drift back regardless of which OS or editor makes the next
edit.

## Lint false positives on `custom_actions_quick.py` (mitigated)

Blender's `PropertyGroup` field syntax:

```python
class RP_ControlBoneItem(PropertyGroup):
    name: StringProperty(
        name="Bone Name",
        description="...",
    )
```

is valid, idiomatic `bpy` code, but a vanilla `pyflakes`/`flake8` run
(without Blender's own `bpy` module available) misreads
`name: StringProperty(...)` as a PEP 526 variable annotation and tries
to parse the string arguments inside it as forward references. That
produces a wall of `F722`/`F821` findings that aren't real bugs.

`.flake8` now scopes an ignore for those two codes to just
`custom_actions_quick.py` (the only file that declares `bpy.props`
fields at module scope), so the rest of the addon still lints
normally.

If you set up `mypy` in CI as well, the same lines will show as
"Name 'StringProperty' is not defined" etc. unless a Blender stub
package is installed. Add
[`fake-bpy-module`](https://github.com/nutti/fake-bpy-module) as a
dev/type-checking dependency rather than blanket-ignoring the file -
it gives mypy real signatures for `bpy`/`bpy.props`/`bpy.types` and
resolves this at the source instead of suppressing it.

## Multi-button execution validates rig AND panel (fixed)

`RP_OT_quick_run_multi_action` used to check only the rig. It now calls
`_multi_action_ownership_error()` immediately before the command is
`exec()`'d, and cancels unless the action belongs to both the active rig
(`armature_name`) and the active panel (`panel_name`, a blank one counting
as the rig's first panel).

## Toggle undo/redo survives a rig rename (fixed)

> **Superseded:** the picker's own Ctrl+Z no longer restores Toggles at all
> (`Controller._restore_quick_toggle` and the snapshot's `quick_toggle_restore`
> were never wired up and were removed; see "Picker undo is picker-only"
> below). Toggle clicks are undone with Blender's own undo. The text below
> describes the intended design at the time and is kept as history.

Quick Toggle undo snapshots pinned the rig by NAME and
`_restore_quick_toggle()` / `_capture_reverse_entry()` looked it up with
`bpy.data.objects.get(name)`. Renaming the rig between the click and the
undo made that lookup miss, so the restore was silently skipped (or, if
another rig had taken the old name, hit the wrong rig).

Snapshots now pin a `custom_actions_quick.RigRef(name, session_uid)` and
resolve it with `resolve_rig_ref()`, which matches on `session_uid` only.
`session_uid` is stable for the datablock's lifetime in a session (the same
property `_sync_armature_combo_items()` already uses to tell a rename from a
delete+add), so no pending snapshot needs migrating when a rig is renamed,
and the result does not depend on the poll timer having run yet. There is
deliberately no fall-back to the stored name: a deleted rig whose name was
reused by something else is skipped rather than restored onto it.

Known limit: `session_uid` is not persistent across Blender sessions or a
file reload, so snapshots from before a reload are skipped, never
redirected.

Tests: `tests/test_toggle_undo_rig_identity.py` (stub `bpy`, runs outside
Blender) covers the resolver, `make_rig_ref()` / `resolve_rig_ref()`: rename,
rename with no poll in between, name reuse by another armature, name swap,
delete, rename + delete, a deleted rig's name reused (skipped, not
redirected), uids reassigned after a reload, non-armatures, and the legacy
bare-string lookup. It does not cover the Controller's undo/redo stack
(`Controller._restore_quick_toggle`, multi-step undo/redo), which needs
Blender. (An earlier version of these notes cited a file of this name that
was not in the archive; it is now, with the scope above.)

## Overlapping rules no longer pick a command silently (fixed)

`_quick_find_matching_rule()` returned the first rule whose Control shape
bones contained the selection, so two overlapping rules ran whichever was
listed first. It now checks every rule on the action; if more than one
matches (empty-command placeholders included) it returns no rule and an
"Ambiguous configuration" warning naming the matching commands. The button
enabled state, Toggle state sync and the run operator all resolve through
this function, so an ambiguous action is greyed out and the operator
cancels with an ERROR report before any validation or `exec()`.

Behaviour change to be aware of: an action that only worked because an
earlier rule shadowed a later overlapping one will now be refused until its
Control shape bones are made disjoint for that selection.

## Renamed/deleted control bones are flagged instead of silently stranding a rule (fixed)

Multi rules store Control shape bones as name strings, and Blender has no
rename/delete hook, so a stale name stayed in the rule: the command was still
displayed but could never match, and the button never enabled with no hint why.

Validation (`quick_action_missing_bones()` and friends) now runs against the
rig's live pose bones:

- **Rig change:** `Controller.refresh_quick_multi_action_bone_validity()` runs
  every poll tick but only acts when the active rig's bone-name set changed
  (rename, delete, undo restoring a bone, a script...). Loading/switching a rig
  is validated by `refresh_quick_multi_action_enabled_states()`.
- **N-panel:** a missing bone is shown in red with "(missing on rig)" in the
  bone list, each affected command header shows "N missing" (even collapsed),
  and the button's box lists the missing bones per command.
- **Picker canvas:** an affected button/toggle gets an amber look and a
  tooltip naming the missing bones (shown even while it is disabled).
- **Execution:** `RP_OT_quick_run_multi_action` validates the whole action
  immediately before running. A selection naming a bone the rig no longer has
  (the controller's record lags by up to one tick) is refused. A "no match"
  report now names the missing bones. A rule that matched still runs - the
  selection is real - but a WARNING lists every stale reference.

Not done on purpose: auto-migrating a vanished name to a new one. Bones have
no stable identity (unlike an Object's `session_uid`), so pairing "one name
gone + one name new" is a guess that could retarget a command at the wrong
bone. Stale entries are flagged and fixed with the existing remove/add
control-bone buttons.

## Panel ownership uses a stable panel id, not names (fixed)

A Multi button's owner was the pair of strings `armature_name` / `panel_name`.
That broke on an incomplete rename migration, stale data, or a panel deleted
and recreated under the same name (the new panel silently adopted the old
panel's buttons).

- Every panel already carried a persistent hidden `_uid`. A button now stores
  that id in the new `RP_QuickMultiAction.panel_uid` and
  `quick_action_owner_panel()` resolves ownership by it. Names are never
  consulted for a button that has one.
- The rig is the owning panel's rig, so a button's own `armature_name` can no
  longer drift out of step with it and strand the button.
- `panel_name` / `armature_name` remain as display mirrors, refreshed by
  `_stamp_multi_action_panel_uids()` (file load + every poll tick) and the
  rename callbacks.
- A button whose panel is gone is an orphan: shown nowhere, cannot run
  ("belongs to a panel that no longer exists"), never re-adopted by name.
- Legacy buttons with no `panel_uid` are adopted by name exactly once, then
  follow the id. Scene copies re-point their buttons at the copied panels'
  fresh ids (`_dedupe_panel_uids()`); removing a panel removes the buttons
  it owns by id; the deleted-rig purge follows the owning panel's rig.

Remaining limits: the *selected* panel is still chosen by name in the UI
(resolved to its row on each use), the rig itself is still identified by name
(no persistent rig id across save/reopen), and saved layouts in
rig_picker_data.json are keyed by rig/panel name. Orphaned buttons are inert
but are not deleted automatically.

## Single buttons enforce their one-command invariant (fixed)

`RP_OT_quick_run_single_action` ran `action.rules[0]` and nothing checked
that a Single button had exactly one rule. Migrated, autosave-restored,
scripted or hand-edited data with extra rules had all but the first silently
ignored - and `[blank, "cmd"]` reported "No command set" while the real
command sat unused. The UI never offers "add command" for these, so the
invariant was a convention only.

Enforced in three layers (the invariant is documented on
`RP_QuickSingleAction.rules`):

- **Before execution:** `single_action_rule()` returns the one non-blank
  command. Blank slots are ignored (so `[blank, "cmd"]` now runs "cmd"); two
  or more non-blank commands are refused with a "Malformed button" ERROR
  instead of picking the first.
- **On load and every poll tick:** `normalize_single_action()` repairs only
  what is lossless: it adds the missing slot to a button with none and drops
  BLANK extra slots. It never deletes a command, so a genuine conflict is
  left for the user to resolve (deleting would turn a silent ignore into
  silent data loss).
- **In the UI:** a conflicting button is greyed out in the Quick panel with
  an alert listing the problem and a "Keep this" button per command
  (`RP_OT_quick_keep_single_rule`), and on the picker canvas it is disabled
  with an amber look and a tooltip. The canvas look is now the shared
  `actionProblem` property (previously `bonesMissing`, Multi only).

Not done: there is no `save_pre` hook in this module (handlers are registered
by the addon's `__init__`), so "when saving" is covered by the per-tick
normaliser plus the execution check rather than a save-time pass.

## Toggle display and click read a property value the same way (fixed)

The switch's display (`_read_quick_toggle_state`) accepted only exactly 0.0 or
1.0 and showed everything else as OFF, while the click handler computed
`not bool(raw_before)`. With the property at 0.5 the display said OFF but the
click saw "truthy, so currently ON" and wrote 0: the switch the user had just
flipped to ON snapped back to OFF. (Any non-zero noise such as 1e-9 hit the
same path.) The UI emitting a binary state was never the problem.

One rule, `_toggle_state_from_value()`, now serves the display, the post-click
re-sync, the frame/selection sync and the click handler
(`_toggle_click_target_state()`):

- within 1e-6 of 0 -> OFF; within 1e-6 of 1 -> ON (same tolerance the write
  verification already used, so float noise and interpolated values are
  binary, not invalid);
- anything else (0.5, 2, -1, nan, inf) is not a valid switch state: it is
  displayed as OFF, so a click turns it ON, and the click reports a WARNING
  naming the old value. The old value is in the undo snapshot, so undo
  restores it exactly and redo turns it on again;
- an unreadable value (missing property, evaluation error) still falls back
  to inverting the switch's last known state.

Chosen over refusing the click: a mid-way value is often legitimate (an
IK/FK blend someone dragged), and a toggle that does nothing is the same
confusing click in a different form. Scope: Quick Toggle buttons only.

## Toggle undo/redo preserve the property's value AND type (fixed)

> **Superseded:** the picker's own Ctrl+Z no longer restores Toggles at all
> (`Controller._restore_quick_toggle` and the snapshot's `quick_toggle_restore`
> were never wired up and were removed; see "Picker undo is picker-only"
> below). Toggle clicks are undone with Blender's own undo. The text below
> describes the intended design at the time and is kept as history.

`_eval_toggle_raw_value()` returns `float(value)` and the click stored that in
the undo snapshot, so undo wrote `0.0`/`1.0` over a property that was a
boolean or integer, and redo (captured through the same function) did too.
Blender retypes a custom property when a value of another type is assigned.
The click itself had the same flaw: it wrote a bare `1`/`0` whatever the
property held, so a float property became an int on the click.

- `_eval_toggle_original()` returns the value exactly as the property holds it
  (original object and type). `_eval_toggle_raw_value()` is now only its
  numeric view, used to decide the switch state and verify a write.
- The undo snapshot (`quick_toggle_restore`) and the redo reverse entry
  (`Controller._capture_reverse_entry`) store that original value, so a bool
  restores as a bool, an int as an int, a float as a float.
- The click writes the property's own type (`_toggle_value_for_state()`:
  True/False, 1/0 or 1.0/0.0); a property that doesn't exist yet still gets a
  plain int on its first run, as before.
- Supported types are bool, int and float (`toggle_value_supported()`).
  Before restoring, `_toggle_restore_blocker()` refuses if the saved value is
  not one of them or if the property now holds something else (a string, an
  array...) that a 0/1 write would overwrite and retype; nothing is written,
  the switch is left alone and a WARNING says why. A click on such a property
  is refused with an ERROR, for the same reason, and records no undo entry.
- After a write or restore, `_toggle_type_change_note()` checks the property
  still has the expected type and warns if something coerced it (an
  RNA-defined property of another type, a setter, a driver).

Behaviour change: a numeric STRING property ("1") used to read as ON because
it went through float(); it is now an unsupported type (not a switch state).
Legacy float-only snapshots still restore.

## Auto-Key supports documented target kinds and reports the rest (fixed)

With Auto-Key on, a Toggle click keyed the command's own target only when it
was `X["Prop"]` or `X.attr`. Anything else (an element of an array property
such as `bone.location[0]` or `bone["Arr"][1]`, and any shape the resolver
couldn't read) fell back to diffing the numeric custom properties of the rig
and its pose bones. That fallback cannot see RNA properties, so those clicks
were silently not keyed, and the message when nothing was found ("no numeric
custom property on the rig changed") was misleading. The click and the undo
restore also each carried their own copy of this logic.

- **Supported targets (documented in `AUTO_KEY_SUPPORT_NOTE`):** a custom
  property `X["Prop"]`, an RNA property `X.attr` (a constraint's `influence`,
  `mute`, ...), or ONE ELEMENT of an array property `X.attr[i]` /
  `X["Arr"][i]` (keyed with its index, not the whole array), where X is a
  plain reference chain resolving to a keyframeable struct.
- `_toggle_keyframe_shape()` (cached, text only) and
  `_resolve_toggle_keyframe_target()` work out the owner/data_path/index;
  `_auto_key_toggle()` is the single implementation used by both the click
  (`RP_OT_quick_run_multi_action`) and undo/redo
  (`Controller._restore_quick_toggle`), so they support the same targets.
- **Everything else** (a vector component `.location.x`, a nested array
  element, a non-constant key) uses the numeric custom-property diff only
  where it can work, and otherwise `_auto_key_report()` says why nothing was
  keyed and lists the supported kinds - a WARNING, never a silent no-op. The
  same report is now produced on undo/redo.

Note: a command with a call inside it (`rig.pose.bones.get("B")[...]`) is
refused by the Toggle's read-back verification before Auto-Key is reached, so
those shapes only matter to the resolver as a unit.

Replaces `_keyframe_toggle_command_target()`, `_toggle_target_location()` and
`_toggle_target_shape()`.

## Picker click callbacks no longer swallow invocation errors (fixed)

`_run_quick_multi_action()` and `_run_quick_single_action()` in
`ui/control_list.py` wrapped the operator call in `except Exception: pass`.
The operators report a failing COMMAND themselves, but nothing reported the
things around the invocation - the operator's poll failing, `bpy.ops`
raising a RuntimeError, the 3D-view context override or the rig lookup
raising - so the click simply appeared to do nothing.

- The two callbacks now share `_run_quick_action()`. The rig lookup, the
  override and the call are all inside one guarded block.
- On failure, `custom_actions_quick.report_quick_invocation_failure()`
  prints the full traceback to the system console and raises a one-line ERROR
  through `rp.quick_report` (the same Info-editor/status-bar channel the
  operators use) naming the button, the exception type and the line that says
  what went wrong. It never raises itself; if even the report channel is down
  the message goes to stderr.
- A stale button (its action was removed between draw and click) runs nothing,
  is not an error, and triggers a resync so it stops being offered.

Not changed: the unrelated `except Exception: pass` in the canvas's
image-scale helper (it reads image dimensions, not an operator invocation).
Reports reach Blender's Info editor/status bar, not the floating picker
window itself, the same as every operator report; the console traceback is
the full record.

## Commands are not transactional (documented limitation)

Single and Multi button commands are arbitrary Python run with `exec()`. If a
command changes several properties and then raises, the changes made before
the exception remain. The operator reports the error and returns
`CANCELLED`, which pushes no undo step, and nothing records what the command
touched, so nothing is rolled back.

Not fixed on purpose: full rollback of arbitrary Python is not generally
feasible (it can touch any datablock, call `bpy.ops`, change selection or
mode, or have side effects outside Blender's data). A partial rollback for a
"supported subset" would be a snapshot/diff of custom properties, which the
addon already takes for Toggle undo, but it would silently not cover
everything else a command can do, and a rollback that only sometimes works is
easier to trust wrongly than none.

What changed instead:

- `command_failure_message()` appends a note to the ERROR report when the
  failure happened during `exec()`: changes made before the error may remain,
  check the rig and use Undo if something was left half-applied. A command
  refused by `_assert_command_allowed()` never ran, so it gets no note.
- Toggle buttons are the one partly covered case: a failed toggle re-syncs the
  switch from the property's real value and records no undo entry, but that
  only concerns the toggle's own target, not other side effects of its command.

Guidance for command authors: keep a command to one logical change, or order
it so the step most likely to fail runs first.


## A lock timeout no longer lets a write proceed unprotected (fixed)

`_cross_process_lock()` waited up to 10 s and then yielded anyway, so
`_flush()` and `_registry_edit()` wrote with no lock held. The atomic
`os.replace()` only stops a half-written file; it does not stop two Blender
instances overwriting each other's changes.

- `_cross_process_lock(path, timeout, required=False)`: with `required=True`
  a timeout raises `LockTimeoutError` before the body runs. Reads keep the old
  behaviour (proceed after the timeout; harmless, since every write is atomic).
- `_flush()` and `_registry_edit()` take the lock with `required=True`. On a
  timeout nothing is written, the user gets an error report, and the
  in-memory data is kept, so the next `_flush()` retries. `_flush()` returns
  `False`, so `save_quick_actions()` rolls back and reports failure as it
  already did for any other failed write. `_registry_edit()` returns the
  registry as read from disk.
- A crashed holder is not a cause of timeouts: flock/msvcrt locks are released
  by the OS when the process exits. A timeout means another live instance is
  holding the lock (or is hung).
- If no lock can be set up at all (the `.lock` file can't be opened, or the
  platform has neither `msvcrt` nor `fcntl`), `required=True` raises
  `LockUnavailableError` before the body runs, so a write never proceeds
  unprotected. Reads proceed unlocked. `LockTimeoutError` and
  `LockUnavailableError` share the base class `LockError`, which `_flush()`
  and `_registry_edit()` catch.

Test: `tests/test_json_lock_timeout.py` (stub `bpy`, POSIX; a second process
holds a real flock). Covers: required lock raises and skips the body, a read
still proceeds, a free lock acquires, a required lock raises when the lock
file can't be opened or locking isn't supported, `_flush()` aborts and keeps
memory then succeeds on retry, `_registry_edit()` aborts.
`tests/test_json_lock_windows.py` is the same idea for `msvcrt`; it runs only
on Windows and has not been run yet.


## Concurrent edits are merged, not overwritten (fixed)

The lock only serializes writers; it does not combine their edits. `_flush()`
used to write this instance's whole in-memory copy and only warn when the file
had changed ("last writer wins"), so instance A's button edit could be erased
by instance B's save. That is no longer the behaviour.

- **Detection is by content hash**, not mtime. `store.loaded_hash` is the
  SHA-256 of `data.json`'s bytes as last read (`_load_locked()`) or written
  (`_flush()`). Under the lock, `_flush()` hashes the file again; a different
  hash means another instance saved in between. Timestamps can collide or be
  coarse, so they are not used.
- **Three-way merge.** `store.base` is a deep copy of the data as last
  read/written. On a changed file `_flush()` reads the file from disk and
  calls `_merge3(base, ours, disk)`. Dicts merge key by key, recursively.
  Lists of dicts whose items all carry a distinct stable id are merged **item
  by item by that id**, not by position: `uid` on `multi_actions` /
  `single_actions` buttons, `_hidden._uid` on `panels` (the shapes
  `_serialize_group()` produces). So renaming Button 1 in one instance and
  Button 2 in another keeps both; additions and deletions from both sides
  survive; edits to different fields of the same button are combined; order
  follows whichever side reordered. Anything else is a single value: the side
  that changed it wins, and that includes lists without ids (a button's
  `rules`, `bones`), so two instances editing the *same button's* rule list
  conflict as a whole. If both sides changed the same value differently,
  **this instance's value wins** and the user is told which path conflicted
  (buttons and panels are named by label, e.g. `.../multi_actions/Button 1/name`).
  Delete-versus-edit of the same button is such a conflict (deletion wins,
  being ours when we deleted). A key or item missing on one side counts as
  deleted there. Selection and expansion state (`*_index`, `expanded`,
  `toggle_state`) is resolved the same way but never reported: it differs
  between open instances all the time. If ids are missing or duplicated
  (older data, hand-edited files) the list falls back to a single value.
- **Unreadable disk file:** if the changed file can't be read or parsed, nothing
  is written; the in-memory data is kept and merged on the next change.
- **`store.data` is updated in place** after a merge (root dict cleared and
  refilled), so references callers took earlier stay valid. References to
  nested dicts taken before the merge (for example `armatures = _load()["armatures"]`)
  can still point at replaced dictionaries; today none of those callers roll
  back, so it is harmless, but a new rollback there would hit it.
- **`save_quick_actions()` checks the disk before calling anything "already
  saved".** It used to return `True` as soon as the payload equalled the
  in-memory value, which could be stale if another instance had saved. Now it
  first calls `_merge_external_changes()` under the lock (hash check, merge
  into memory, no write), then three-way merges the payload with the disk
  state using the N-panel data this instance last saw as the base. Only what
  this instance changed is applied, so a stale snapshot never overwrites the
  other instance's edit. If this instance changed nothing, nothing is written
  and `True` is returned even though the disk differs from the payload; the
  newer data is then what `get_quick_actions()` returns. If the file changed
  and can't be read, it does not report success. Known gap: the autosave tick
  in `custom_actions_quick.py` compares the stored data with its scene
  signature afterwards and will keep retrying until the scene is reloaded
  from the merged data; that reload is not implemented.
- **A deleted `data.json` is restored, never "already saved".** If the file
  existed at our last read/write (`store.loaded_hash` set) and is gone,
  `_merge_external_changes()` reports it once and resets `loaded_hash` to
  `None`; the in-memory copy is the only one left, so the write that follows
  recreates the file. `save_quick_actions()` treats `loaded_hash is None` as
  "no file on disk" and writes instead of returning `True`. A file that never
  existed is not reported. If the user deleted it on purpose, the data comes
  back; the message says so.
- **Rollback after a failed write:** `save_quick_actions()` reverts on the
  store's current data with `_merge3(payload, previous, current)`, so only its
  own change is undone and the other instance's merged edits stay. The saved
  `base` is untouched, so a retry re-merges and writes.

Tests: `tests/test_quick_payload_merge.py` merges the real serialized N-panel
payload shape (renames of different buttons, single actions and panels, field-level
merge in one button, additions, deletions, reorder, conflicts, quiet UI state, id
fallbacks, and two instances end to end through `save_quick_actions()`).
`tests/test_flush_merge.py` (different keys both survive, added and
deleted keys, same-key conflict keeps ours and reports, unreadable file
aborts, same-length same-mtime change still detected, unchanged file merges
nothing, failed write after a merge rolls back correctly and the retry
saves, unchanged/changed stale payload vs an external edit, unreadable file
not reported as saved, deleted file restored (and a failed restore not reported as saved), `_merge3` unit).


## Toggle read-back verification handles NaN and infinity (fixed)

`_toggle_assignment_took_effect()` checked a read-back with
`abs(actual - float(expected)) <= tolerance`. For non-finite values that is
False even when the property holds exactly what was written: `nan - nan` and
`inf - inf` are both `nan`. A float `nan`/`inf` is a supported Toggle value
(`toggle_value_supported()` accepts any float, and undo saves and writes the
previous value back as-is), so undo/redo of such a Toggle wrote the right
value and then reported that it had not taken effect: the switch was left
alone and nothing was re-keyed.

The comparison now lives in `_toggle_values_match(actual, expected)`:

- finite vs finite: unchanged, within `_TOGGLE_VERIFY_TOLERANCE`;
- NaN matches only NaN (sign/payload ignored), never a finite number or an
  infinity;
- an infinity matches only the infinity of the same sign.

The click path is unaffected in practice: it writes 0/1, which are finite,
so a driver that leaves NaN behind still fails verification as before.

Test: `tests/test_toggle_nonfinite_verify.py` (real module, stub `bpy`).
Covers the finite/NaN/inf matrix, float noise vs a clamped value, the old
expression failing on NaN and both infinities, and
`_toggle_assignment_took_effect()` end to end with the read-back stubbed. Not run inside Blender: the undo path itself
(`Controller._restore_quick_toggle`) was not exercised.


## `None` is no longer both a value and the "unreadable" marker (fixed)

`_eval_toggle_original()` returned `None` for "the command can't be read"
(empty, not a pure reference, evaluation raised, property missing) and also
returned `None` when the property genuinely held `None`. The click handler
tested `original_before is not None` to decide whether the target was
readable, so a target holding `None` was treated as "doesn't exist yet" and
the unsupported-type refusal was skipped.

Reachable because a "pure reference" command is any chain of
name/attribute/constant-subscript lookups, so it can resolve to an unset
pointer property (`rig.parent`, a bone's `custom_shape`) or an entry in a plain
Python dict (`bpy.app.driver_namespace["x"]`). On an RNA pointer the 0/1 write
then raised `TypeError` (a misleading "Error running command"); on a plain
dict it silently overwrote the `None`, and no undo entry was recorded because
`original_before` was `None`.

- New unique sentinel `_TOGGLE_UNREADABLE`. `_eval_toggle_original()` and
  `_read_quick_toggle_original()` take `unreadable=` (default `None`, so the
  display sync and the numeric view are unchanged) and return that for the
  can't-read cases; a value that was read, `None` included, comes back as
  itself.
- The click handler passes the sentinel and tests with `is`. A held `None` is
  now refused with the existing unsupported-type error ("its property holds
  None, not a number or boolean ... Not run.") before any assignment, and the
  switch is re-synced. A really missing property is still treated as a first
  run and created, as before. After that check the sentinel is normalised back
  to `None`, which from there only ever means "unreadable" because a value that
  was read and accepted is never `None`.
- Same ambiguity fixed where it also mattered: `_toggle_restore_blocker()`
  (undo/redo no longer writes 0/1 over a property that now holds `None`, while
  still allowing it to re-create a deleted one) and
  `_toggle_type_change_note()` (a write that left `None` behind is reported).
- Not changed: the display sync and `_eval_toggle_raw_value()` still map
  `None` to "no usable value" (shown OFF); `None` has no 0/1 meaning, so that
  was already the right answer there.

Test: `tests/test_toggle_none_value.py` imports the real module with a stub
`bpy` and covers held None vs missing/unreadable reads, default behaviour for
other callers, the handler's decision (refuse / first run / run), the undo
blocker and the type-change note. The operator's `execute()` itself was not
run (it needs Blender).


## Toggle switches can be refreshed on demand (limit documented, manual refresh added)

`sync_quick_toggle_states()` runs from the `frame_change_post` handler and
from the controller's selection refresh (plus the picker's own click, undo and
redo). A script that changes a Toggle's target without changing the frame or
the selection is therefore not reflected until one of those fires.

No automatic fix was added, deliberately:

- A Toggle target is any attribute/subscript chain (a custom property, an RNA
  property, an entry in a plain Python dict...). There is no single property
  whose update callback could be hooked, and Blender does not notify an addon
  when a script writes an arbitrary property.
- `depsgraph_update_post` is the nearest event, but it fires on every
  depsgraph change (every transform, every frame of playback), so it would run
  the sync constantly, and a plain Python object would never trigger it at all.
  I could not verify in this environment which kinds of write do produce a
  depsgraph update, so coverage would be uncertain: a refresh that works for
  some writes and not others is worse than one that is explicitly manual.
- A timer that re-reads every Toggle is the "aggressive" option the finding
  rules out; the existing 0.15 s poll is left alone for the same reason.

Added instead:

- `custom_actions_quick.refresh_quick_toggle_states(scene=None)`: re-reads
  every Toggle and returns True if a switch changed. Uses the picker's own
  tracked selection when it is open (the same source a click uses), else the
  viewport's. Scripts call it after changing a property.
- `rp.quick_refresh_toggles` ("Refresh Toggles"): the operator behind it, for
  `bpy.ops` and for a button. No UNDO flag: it changes no rig data.
- A refresh icon button next to Add in the N-panel's Multi Command Buttons.
- `sync_quick_toggle_states()` now returns whether anything changed (existing
  callers ignore the value), and its docstring states the limit.

Unchanged by design: a Toggle whose rule does not match the current selection
is still left alone, refresh or not (the "no match" case in the sync). The
Qt picker window has no refresh button; the N-panel button, the operator and
the function cover it.

Test: `tests/test_toggle_manual_refresh.py` (real module, stub `bpy`): a
script-changed property leaves the switch stale until refreshed, the refresh
updates it and reports a change, a second refresh is a no-op, non-matching
rules are untouched, the picker's selection is preferred over the viewport's,
an unreadable target resets a stale ON, and the operator/button are
registered. The operator's `execute()` and the panel drawing were not run
(they need Blender).


## Tests

The regression tests live in `tests/` and are plain `unittest` cases (no
Blender, no third-party packages). Run all of them with:

    python -m unittest discover -s tests -v

from the addon folder (`blender_addon/`), or run one file directly, e.g.
`python tests/test_toggle_none_value.py`. They also pass when started from a
different working directory. pytest should collect them too, but that was not
tried here (it was not installed).

- `tests/_stubs.py` is the shared loader: it installs a stub `bpy`, loads the
  addon module under test, and restores `sys.modules` after every test so
  nothing leaks between tests. It is not named `test*.py`, so discovery does
  not treat it as a test.
- There is deliberately no `tests/__init__.py`: the addon root is itself a
  package that imports `bpy`, and making `tests/` a package would let runners
  try to import it. Run discovery with `-s tests` as above, not from the
  addon root.
- `test_json_lock_timeout.py` needs POSIX `flock` and is skipped elsewhere.
  `test_json_lock_windows.py` covers the `msvcrt` path but runs only on
  Windows and has not been run yet.
- What is not covered: anything that needs Blender itself (operator
  `execute()` methods, panel drawing, the Qt picker, the Controller's undo/redo
  stack). The stubbed `bpy` turns the addon's Blender classes into mocks, so
  only its plain functions are exercised.

| File | Covers |
|---|---|
| `test_json_lock_timeout.py` | write lock timeout / unavailable lock aborts instead of writing unlocked (POSIX) |
| `test_json_lock_windows.py` | same, via `msvcrt` (Windows only; not yet run) |
| `test_overlay_button_cursor.py` | hand cursor on Quick Action buttons/toggles and the symmetry handle (logic) |
| `test_disabled_hover_cursor_qt.py` | hand over disabled buttons/labels/toggles, real Qt (needs PySide6) |
| `test_no_h_hotkey.py` | no H / Alt+H handling; Ctrl+I wired to invert |
| `test_select_all_controls.py` | `A` and Ctrl+I: picker controls only, greyed-out skipped |
| `test_quick_payload_merge.py` | merge by uid on the real N-panel payload structure |
| `test_flush_merge.py` | content-hash conflict detection, three-way merge, rollback after a failed write |
| `test_toggle_nonfinite_verify.py` | NaN / infinity read-back verification |
| `test_toggle_none_value.py` | `None` as a value vs the "unreadable" sentinel |
| `test_toggle_manual_refresh.py` | manual Toggle refresh, stale-until-refreshed limit |
| `test_toggle_undo_rig_identity.py` | rig pinned by `session_uid` through renames/deletes |


## The picker's `A` hotkey selects the picker controls only (changed)

`Controller.select_all()` (`ui/main_window.py` maps `A` to it) used to run
`pose.reveal` and `pose.select_all(SELECT)` in the viewport. That selected every
visible bone in the rig, controls or not, and left them all showing - the same
viewport result as clicking empty space (Show All).

It now sets `selected_bones` to the picker's controls (still dropping controls
whose bone collection is hidden) and pushes them through
`_sync_viewport_selection()`, the path box select and undo/redo already use:
exactly those bones are selected and the rest are hidden, like selecting one
control does. Unchanged: the selection-only undo snapshot (skipped when nothing
changes), the picker widgets' active state, the appearance dropdowns, and the
`backend._LAST_SELECTED_BONES` update. Also, a control that is greyed out in
the picker (`isEnabled()` false: its bone collection is hidden) is never
selected, judged by the widget's own state as well as the live collection
check - the same rule box select uses. New: with no picker controls at all, `A`
does nothing (it used to select every bone).

Test: `tests/test_select_all_controls.py` runs the method's source against
fakes (`controller.py` needs PySide6 and Blender). Not covered: the real
viewport result - try `A` in Blender 5.0.


## The H and Alt+H hotkeys are removed (changed)

`PickerWindow.keyPressEvent()` (`ui/main_window.py`) no longer handles `H`
(hide all) or `Alt+H` (show all); those keys now fall through to Qt's default
handling. `Controller.hide_all()` / `show_all()` and the `rp.hide_all` /
`rp.show_all` operators are left in place: `show_all` is still used by
`_show_all_and_deselect()`, and nothing else was touched. `A`, `Ctrl+Z` and
`Ctrl+Shift+Z` are unchanged. Test: `tests/test_no_h_hotkey.py`.


## Ctrl+I inverts the picker selection (new)

`PickerWindow.keyPressEvent()` maps `Ctrl+I` (not Alt+I, not with Shift) to `Controller.invert_selection()`: every selectable picker control
that is not selected becomes selected, and the selected ones are deselected.
Greyed-out controls (disabled widget or hidden bone collection) are never
selected. Inverting with nothing selected selects all selectable controls;
inverting with everything selected clears the selection and resets the
appearance dropdowns to their no-selection defaults.

`select_all()` and `invert_selection()` now share two helpers:
`_selectable_control_names()` (the greyed-out / hidden-collection rule) and
`_set_picker_selection()` (selection-only undo snapshot when something changes,
widget highlight, one `_sync_viewport_selection()` push, `_LAST_SELECTED_BONES`,
dropdowns, enabled states). One small side effect on `A`: if no control is
selectable it now also resets the dropdowns to the no-selection defaults, as
deselecting does. Tests: `tests/test_select_all_controls.py`,
`tests/test_no_h_hotkey.py` (hotkey wiring). Not covered: the real viewport
result and Qt delivering the key - try it in Blender 5.0.


## Hand cursor on Quick Action buttons, toggles and the symmetry handle (fixed)

`CircleControl` and `ToggleSwitch` call `setCursor(Qt.PointingHandCursor)`; the
Quick Action buttons (plain `QPushButton`s created in
`PickerCanvas._sync_quick_action_group()`) never did, and the symmetry line's
grab handle had no hover cursor at all.

- Buttons get the hand cursor when created, and `_set_overlay_button_enabled()`
  (used by `update_quick_single_actions_enabled()` /
  `update_quick_multi_actions_enabled()`) re-applies it on every update. **But
  Qt ignores a disabled widget's own cursor and shows its parent's** (verified
  against real Qt), so setting it on a greyed-out button, toggle switch or
  toggle label does nothing. Instead `PickerCanvas.eventFilter()` (already on
  every overlay widget) watches Enter/HoverEnter/HoverMove/Leave/HoverLeave
  (these still reach a disabled widget's filter), and
  `_update_disabled_hover_cursor()` sets the hand on the canvas while the
  pointer is over a disabled one (`_refresh_canvas_cursor()` combines this with
  the symmetry-handle cursor). Safety nets so the hand can't stick: a canvas
  `mouseMoveEvent` (pointer not over a child) clears the state, a widget that
  becomes enabled while hovered stops counting, and unregistering a group
  clears it. A toggle's name label gets the hand too. The picker's greyed-out
  *controls* are unchanged: they show the arrow on purpose.
- The symmetry handle (the top grab area of the line, `symmetry_handle_rect()`,
  not the dashed line itself, which is not draggable) shows the hand while
  symmetry is on, through a drag started on it, and reverts afterwards
  (`_update_symmetry_cursor()`, called from `mouseMoveEvent` / `mouseReleaseEvent`).
- `PickerCanvas` now calls `setMouseTracking(True)`. Qt only sends hover moves
  to `mouseMoveEvent` with tracking on; the handle's existing highlight
  (`symmetry_handle_hover`) relied on that too. Every other branch of
  `mouseMoveEvent` only acts during a pending/active drag, so plain hover moves
  do nothing else.
- Not changed: the toolbar buttons in `ui/main_window.py` (Capture View, Add
  Selected, Clear All, Delete, X) still use the default arrow.

Tests: `tests/test_overlay_button_cursor.py` (source checks, logic against
fakes) and `tests/test_disabled_hover_cursor_qt.py` (real Qt, offscreen
platform, skipped without PySide6: the canvas methods run against actual
disabled buttons, labels and widgets and the platform window's cursor is read
back). Not covered: the live Blender window - hover a disabled button there.


## Select All is the plain `A` key only (changed)

`MainWindow.keyPressEvent()` handled `Qt.Key_A` whatever modifiers were held,
so Ctrl+A, Shift+A and Alt+A also ran `Controller.select_all()`. It now acts
only when none of Ctrl, Shift, Alt or Meta is down (Meta because Qt reports the
physical Ctrl key as `MetaModifier` on macOS), the same rule the `G` hotkey
already uses. A modified `A` is left unaccepted. Ctrl+A is deliberately not
supported as Select All: in Blender, Shift+A is "Add" and Alt+A is "deselect
all", so reusing them here would be misleading. Unchanged: Ctrl+I (invert),
Ctrl+Z / Ctrl+Shift+Z (undo/redo), `G`.

Test: `tests/test_a_hotkey_modifiers_qt.py` (real Qt, offscreen, skipped without
PySide6). Not covered: pressing the keys in Blender.


## Dragging a button/toggle in a canvas smaller than it (policy documented)

`_drag_overlay_group()` clamps the group's whole bounding box inside the canvas.
When the canvas is narrower or shorter than the group there is no valid spot:
the old clamp pinned the group's left/top edge to 0 and let the rest hang
outside. Policy now, per axis: a group larger than the canvas along an axis is
**locked on that axis** (it keeps its position there); the other axis still
drags and clamps normally if the group fits it; if it fits neither, the drag
does nothing and nothing is persisted. Nothing is resized or moved
automatically, the stored position is untouched, and the group drags normally
again as soon as the canvas is large enough. Controls (`CircleControl`) are not
affected: they are small and `resolve_control_collision()` already pins them to
0 when the canvas is smaller than one.

Test: `tests/test_overlay_small_canvas_qt.py` (real Qt, offscreen, skipped
without PySide6). Not covered: resizing the live Blender window.


## Dragging a button/toggle off an existing overlap (fixed)

`_resolve_overlay_collision()` treated every obstacle as blocking, so a Quick
Action button/toggle that already overlapped a control (an old layout) was
shoved around on the first drag step instead of following the pointer. A first
fix ignored such obstacles for the whole drag, which let the widget be dragged
*deeper* into the control. Now an existing overlap may shrink or stay the same
but never grow:

- Per widget and obstacle it already touches, the overlap's width and height at
  the start of the call are the limit. If the widget would overlap more on an
  axis, that axis of the delta is pinned back to the starting depth; the other
  axis still moves, so the widget slides along the obstacle's edge.
- A widget that spans or sits inside the obstacle on an axis is already at the
  maximum overlap there, so that axis is unrestricted.
- The baseline is the widget's live position at each call, i.e. each mouse
  event, so many small drags can't creep deeper either. Backing out part way
  lowers the limit: it can't return to the old depth.
- Once completely clear, the obstacle blocks like any other (no re-entering).
- Obstacles the widget isn't touching, including the same obstacle for another
  widget of a group, block as before.

Test: `tests/test_overlay_collision_qt.py` (toward/away in 1px steps, one big
jump, back-out then return, slide along the edge, corner overlap, a group
member, and a 200-run randomized check that no drag grows an overlap or creates
a new one). Skipped without PySide6. Not covered: dragging in the live Blender
window.


## Clear All no longer erases the picker's undo history (fixed)

`Controller.clear_all()` threw away every undo/redo stack of the rig. Undoing
the Clear All (picker Ctrl+Z, or Blender's undo) brought the layouts and panel
rows back, but nothing before the Clear All could be undone any more: the first
Ctrl+Z undid the Clear All, and further Ctrl+Z did nothing.

- `clear_all()` now parks the rig's stacks on the Clear All's own entry
  (`stash_rig_history()`), and undoing that Clear All puts them back
  (`restore_rig_history()`), replacing whatever the blank "Panel 1" collected
  meanwhile. Redoing the Clear All parks them again.
- Parked history is settled (staged image files finalized) when the entry
  expires, the rig is purged, or the file changes.
- If no entry could be created (nothing stored, so the Clear All can't be
  undone), the history is settled and dropped as before.

Test: `tests/test_clear_all_keeps_undo_history.py` (real Controller undo/redo/
clear_all and real Clear All helpers against stub `bpy`/Qt). Not run inside
Blender.


## Picker undo is picker-only (changed)

The picker's Ctrl+Z / Ctrl+Shift+Z only revert picker layout data (see the
note in `Controller.undo()`); they never write into Blender scene data. Two
leftovers contradicted that and were removed:

- `quick_control_bones` (set by `delete_selected()` on the undo snapshot, never
  applied by `undo()`/`redo()`). Deleting a control that a Multi Command Button
  uses as a Control shape bone still removes it from the button; picker Ctrl+Z
  brings the control back but not the button reference. Blender's own undo
  restores both (native step via `register_control_delete()`).
- `quick_toggle_restore` (the `_record_undo_snapshot()` argument, the reverse
  entry's mirror of it, `_restore_quick_toggle()`), and the click operator's
  unused `pending_undo_snapshot`. No caller ever passed it.

`custom_actions_quick.make_rig_ref()` / `resolve_rig_ref()` are now unused by
the picker; they were kept (with `tests/test_toggle_undo_rig_identity.py`).


## Smaller undo gaps (fixed)

- A cancelled G grab (Esc / right-click) is undone with `undo(discard_redo=True)`,
  so it no longer leaves a redo entry that Ctrl+Shift+Z could re-apply. With
  nothing to cancel it returns instead of falling through to undoing a Clear All.
- Show All / Hide All only take an undo snapshot when some control's `hidden`
  flag would change, so a no-op press no longer costs a Ctrl+Z.
- The "RigPicker Undo: ..." text in the 3D viewport header is cleared after
  `UNDO_HEADER_SECONDS` (only the latest undo's timer clears it). Before, it
  stayed until Blender restarted.

- Ctrl+Z / Ctrl+Shift+Z while a drag or G grab is in progress is no longer an
  undo/redo. `Controller.undo()`/`redo()` first ask the canvas
  (`PickerCanvas.interrupt_for_undo()`), which covers every entry point (the
  picker window's keys, the G-grab key filter, and the Blender keymap guard
  while the cursor is over the picker):
  - **G grab:** cancelled exactly like Esc (the move is taken back via the
    grab's own snapshot, nothing becomes redoable); earlier steps are untouched.
    The grab's key filter also handles Ctrl+Z itself (it used to swallow it
    silently).
  - **Mouse drag with a button held:** the request is ignored and the drag
    carries on and commits on release as usual. Not cancelled: each mouse drag
    (control, Quick Action button, symmetry line, background image, box
    select) commits in its own release handler. The "button held" check keeps a
    stale drag flag from ever disabling undo.

Not changed: a first-ever selection after an undo does not clear the redo
stack (a selection is not a real operation); `capture_view()` can still leave a
no-op step if the copy fails, because that snapshot's backup is what repairs a
half-written image.

Tests: `tests/test_undo_gaps.py`; `tests/test_g_grab_qt.py` (real Qt, offscreen)
covers the grab cancel, Ctrl+Z / Ctrl+Shift+Z mid-grab and `interrupt_for_undo()`.


## Removed panels keep their picker undo history while the removal can be undone (fixed)

Removing a panel from Manage Panels is undoable by Blender's own undo for
`PANEL_DELETE_GRACE_SECONDS`, but the removal operator discarded the panel's
picker undo/redo stacks outright ("nowhere left to ever be resolved"). After
undoing the removal the panel and its layout came back, but picker Ctrl+Z could
not go back through anything done on it before - the same symptom as Clear All.

- The stacks are now parked in `custom_actions_quick._PENDING_PANEL_HISTORY`
  (`_park_panel_history()`), restored when `_purge_expired_panel_deletions()`
  sees the panel back in the scene, and settled (staged image work finalized,
  before the layout is deleted) when the grace period ends, a new panel takes
  the name (`_finalize_pending_panel_deletion()`), the rig is purged, or the
  file changes. A rig rename re-keys it. `Controller` gained
  `stash_panel_history()`, `settle_panel_history()`, `restore_panel_history()`.
- `Controller.repoint_paths()` (Save As) now also re-points history parked on
  Clear All entries and removed panels; before, undoing a Clear All after a
  Save As brought back snapshots whose image paths pointed into the old folder.

Tests: `PanelRemovalHistoryTests` in `tests/test_undo_gaps.py` (real helpers;
the operator's own call into `_park_panel_history()` needs Blender and is not
covered).

Known, not changed: only the most recent Clear All of a rig can be undone from
the picker. `Controller._clear_entries` holds one entry per rig, so a second
Clear All overwrites the first; after undoing the second, picker Ctrl+Z reports
nothing to undo (the first entry's backup and parked history just wait out
their window and are then settled). Chaining them means re-checking the scene
row restore (`_restore_rig_scene_rows`) in Blender, so it was left alone.

