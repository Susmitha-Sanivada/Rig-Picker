import os
import tempfile
import bpy
from bpy.app.handlers import persistent

# Global cache for the active armature reference
_CACHED_ARM: bpy.types.Object | None = None

# Name of the armature _CACHED_ARM refers to. Change detection is done by
# name, not object identity - Blender can hand back a fresh Python
# wrapper/evaluated-copy instance for the logically same armature between
# checks, and comparing by identity would treat that as a "different"
# armature and trigger a needless reload.
_CACHED_ARM_NAME: str | None = None

_ACTIVE_CONTROLLER = None
_ACTIVE_WINDOW = None

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

def arm() -> bpy.types.Object | None:
    """Returns the cached armature reference without re-checking mode continuously."""
    global _CACHED_ARM, _CACHED_ARM_NAME

    if _CACHED_ARM is None:
        obj = bpy.context.active_object
        if obj and obj.type == 'ARMATURE':
            _CACHED_ARM = obj
            _CACHED_ARM_NAME = obj.name

    return _CACHED_ARM


def poll_active_armature():
    """Runs every _POLL_INTERVAL seconds via bpy.app.timers.

    Detects when a different armature has become active (in either Object
    Mode or Pose Mode) and reloads the picker for it. Registered with
    persistent=True in register(), so it keeps running across File > Open
    without needing to be re-registered.

    Unlike depsgraph_update_post, a timer callback isn't a restricted
    context, so the reload can happen directly here - no need to defer it
    another tick.
    """
    global _CACHED_ARM, _CACHED_ARM_NAME

    try:
        obj = getattr(bpy.context, "active_object", None)
        if not obj and hasattr(bpy.context, "view_layer"):
            obj = bpy.context.view_layer.objects.active

        if obj and obj.type == 'ARMATURE':
            if obj.name != _CACHED_ARM_NAME:
                _CACHED_ARM = obj
                _CACHED_ARM_NAME = obj.name

                if _ACTIVE_CONTROLLER is not None:
                    try:
                        _ACTIVE_CONTROLLER.load_armature(obj)
                    except Exception:
                        import traceback
                        traceback.print_exc()
            else:
                # Same armature logically; keep the reference fresh.
                _CACHED_ARM = obj

    except Exception:
        import traceback
        traceback.print_exc()

    return _POLL_INTERVAL  # returning a number reschedules the timer


@persistent
def on_frame_change(scene, depsgraph=None):
    """Registered as a frame_change_post handler.

    Runs whenever the current frame changes - scrubbing the timeline,
    stepping through keyframes, or playback - so the IK/FK toggle label
    stays in sync with whatever the animated IK_FK custom property
    evaluates to at that frame, not just whatever it was when a control
    was last clicked.

    frame_change_post (rather than _pre) is used deliberately: it fires
    after Blender has evaluated the new frame's animation, so pose bone
    property values (like IK_FK) are already up to date when this runs.

    @persistent is required here: without it, Blender silently drops
    every handler in bpy.app.handlers.* the moment a .blend file is
    loaded (New or Open) - even mid-session, after register() already
    ran once. poll_active_armature doesn't need this because it's a
    bpy.app.timers callback, not an application handler; timers have
    their own separate persistent=True flag passed at registration.

    Only reads property values and updates a Qt label - no bpy.ops calls -
    so it's safe to run from this handler.
    """
    if _ACTIVE_CONTROLLER is None:
        return

    try:
        _ACTIVE_CONTROLLER.refresh_ikfk_label()
    except Exception:
        import traceback
        traceback.print_exc()


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
    if rig and rig.mode != 'POSE':
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


def mirror_name(name):
    if name.endswith(".L"):
        return name[:-2] + ".R"
    if name.endswith(".R"):
        return name[:-2] + ".L"
    return None


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
                selected_bones = [pb.name for pb in rig.pose.bones if pb.select]

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

            # Hide unselected bones
            if selected_bones:
                bpy.ops.pose.hide(unselected=True)
            else:
                bpy.ops.pose.select_all(action='SELECT')
                bpy.ops.pose.hide(unselected=False)

        refresh_3d_view(context)
        return {'FINISHED'}


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
# CALCULATE MOTION PATH
# ---------------------------------------------------------

class RP_OT_CalculatePath(bpy.types.Operator):
    bl_idname = "rp.calculate_path"
    bl_label = "Calculate Motion Path"

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

            selected = [pb for pb in rig.pose.bones if pb.select]
            if not selected:
                return {'CANCELLED'}

            # Uses Blender's own "Calculate Motion Paths" behavior, which
            # operates on whichever pose bones are currently selected -
            # the picker keeps that selection in sync with the controls
            # selected in the picker UI, so this just piggybacks on it.
            bpy.ops.pose.paths_calculate()

        refresh_3d_view(context)
        return {'FINISHED'}


class RP_OT_ClearPath(bpy.types.Operator):
    bl_idname = "rp.clear_path"
    bl_label = "Clear Motion Path"

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

            selected = [pb for pb in rig.pose.bones if pb.select]
            if not selected:
                return {'CANCELLED'}

            # Only clear paths for the bone(s) behind the currently
            # selected control(s) - not every calculated path on the rig.
            bpy.ops.pose.paths_clear(only_selected=True)

        refresh_3d_view(context)
        return {'FINISHED'}


# ---------------------------------------------------------
# CAPTURE VIEW
# ---------------------------------------------------------

class RP_OT_CaptureView(bpy.types.Operator):
    bl_idname = "rp.capture_view"
    bl_label = "Capture View"

    def execute(self, context):
        image_path = os.path.join(
            tempfile.gettempdir(),
            "rig_picker_capture.png"
        )

        scene = context.scene
        old_filepath = scene.render.filepath
        scene.render.filepath = image_path

        success = False

        for window in bpy.context.window_manager.windows:
            screen = window.screen
            for area in screen.areas:
                if area.type != 'VIEW_3D':
                    continue

                for region in area.regions:
                    if region.type != 'WINDOW':
                        continue

                    with bpy.context.temp_override(
                        window=window,
                        area=area,
                        region=region,
                    ):
                        bpy.ops.render.opengl(write_still=True)

                    success = True
                    break

                if success:
                    break

            if success:
                break

        scene.render.filepath = old_filepath

        if not success:
            self.report({'ERROR'}, "Could not capture viewport.")
            return {'CANCELLED'}

        self.report({'INFO'}, "Viewport captured.")

        return {'FINISHED'}