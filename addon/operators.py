import bpy

from ui.launcher import show_picker


class RP_OT_OpenPicker(bpy.types.Operator):

    bl_idname = "rigpicker.open"

    bl_label = "Open Rig Picker"

    def execute(self, context):

        show_picker()

        return {'FINISHED'}


class RP_PT_MainPanel(bpy.types.Panel):

    bl_label = "Rig Picker"

    bl_idname = "RP_PT_MAIN"

    bl_space_type = "VIEW_3D"

    bl_region_type = "UI"

    bl_category = "Rig Picker"

    def draw(self, context):

        layout = self.layout

        layout.operator(
            "rigpicker.open",
            icon="WINDOW"
        )