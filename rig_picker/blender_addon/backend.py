import bpy
import tempfile
import os

ARMATURE_NAME = "Sky_Rig"


# ---------------------------------------------------------
# DATA
# ---------------------------------------------------------

class RP_Item(bpy.types.PropertyGroup):

    bone_name: bpy.props.StringProperty()

    label: bpy.props.StringProperty()

    x: bpy.props.FloatProperty(
        default=-1.0,
    )

    y: bpy.props.FloatProperty(
        default=-1.0,
    )

    control_size: bpy.props.IntProperty(default=36)
    control_shape: bpy.props.StringProperty(default="CIRCLE")
    control_color: bpy.props.StringProperty(default="GREEN")


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def arm():
    return bpy.data.objects.get(ARMATURE_NAME)


def ensure_pose(context, rig):
    """
    Make the rig active if possible.
    Do NOT call bpy.ops.object.mode_set() from the Qt window.
    """

    try:
        context.view_layer.objects.active = rig
    except Exception:
        pass


# ---------------------------------------------------------
# ADD SELECTED
# ---------------------------------------------------------

class RP_OT_Add(bpy.types.Operator):

    bl_idname = "rp.add_selected"

    bl_label = "Add Selected Controls"

    def execute(self, context):

        rig = arm()

        if not rig:
            return {'CANCELLED'}

        existing = {
            item.bone_name
            for item in context.scene.rp_items
        }

        # Read selection directly from the rig
        for pb in rig.pose.bones:

            if not pb.bone.select:
                continue

            if pb.name in existing:
                continue

            item = context.scene.rp_items.add()

            item.bone_name = pb.name
            item.label = pb.name

            item.x = -1

            item.y = -1
        

        return {'FINISHED'}


# ---------------------------------------------------------
# SELECT
# ---------------------------------------------------------

class RP_OT_Select(bpy.types.Operator):

    bl_idname = "rp.select"

    bl_label = "Select Control"

    bone_name: bpy.props.StringProperty()

    shift: bpy.props.BoolProperty(
        default=False
    )

    def execute(self, context):

        rig = arm()

        if not rig:
            return {'CANCELLED'}

        ensure_pose(context, rig)

        if not self.shift:
            for bone in rig.data.bones:
                bone.hide = True
                bone.select = False

        bone = rig.data.bones.get(self.bone_name)

        if bone:

            bone.hide = False

            if self.shift:
                # Toggle selection
                bone.select = not bone.select
            else:
                bone.select = True

            rig.data.bones.active = bone

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

        ensure_pose(context, rig)

        for bone in rig.data.bones:
            bone.hide = False

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

        ensure_pose(context, rig)

        for bone in rig.data.bones:
            bone.hide = True

        return {'FINISHED'}


# ---------------------------------------------------------
# REMOVE
# ---------------------------------------------------------

class RP_OT_Remove(bpy.types.Operator):

    bl_idname = "rp.remove"

    bl_label = "Remove"

    index: bpy.props.IntProperty()

    def execute(self, context):

        context.scene.rp_items.remove(self.index)

        return {'FINISHED'}


# ---------------------------------------------------------
# RENAME
# ---------------------------------------------------------

class RP_OT_Rename(bpy.types.Operator):

    bl_idname = "rp.rename"

    bl_label = "Rename"

    index: bpy.props.IntProperty()

    new_name: bpy.props.StringProperty(name="Label")

    def invoke(self, context, event):

        self.new_name = context.scene.rp_items[self.index].label

        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):

        self.layout.prop(self, "new_name")

    def execute(self, context):

        context.scene.rp_items[self.index].label = self.new_name

        return {'FINISHED'}




class RP_OT_CaptureView(bpy.types.Operator):

    bl_idname = "rp.capture_view"
    bl_label = "Capture View"

    def execute(self, context):

        import os
        import tempfile
        import bpy

        image_path = os.path.join(
            tempfile.gettempdir(),
            "rig_picker_capture.png"
        )

        scene = context.scene

        old_filepath = scene.render.filepath
        scene.render.filepath = image_path

        success = False

        # Find a VIEW_3D and capture it
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

            self.report(
                {'ERROR'},
                "Could not capture viewport."
            )

            return {'CANCELLED'}

        # Store the image path
        scene.rp_background_image = image_path

        self.report(
            {'INFO'},
            "Viewport captured."
        )

        return {'FINISHED'}

class RP_OT_ClearAll(bpy.types.Operator):

    bl_idname = "rp.clear_all"
    bl_label = "Clear All Controls"

    def execute(self, context):

        context.scene.rp_items.clear()

        return {'FINISHED'}
