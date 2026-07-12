import bpy
import sys

print("=" * 60)
print(sys.executable)
print()
print("Python search paths:")
for p in sys.path:
    print(p)
print("=" * 60)

class RP_OT_OpenPicker(bpy.types.Operator):
    bl_idname = "rigpicker.open"
    bl_label = "Open Rig Picker"

    def execute(self, context):

        try:
            # Import only when button is clicked
            from .ui.launcher import show_picker

            print("=" * 50)
            print("Opening Rig Picker...")
            print("=" * 50)

            show_picker()

            self.report({'INFO'}, "Rig Picker Opened")

        except Exception as e:

            import traceback
            traceback.print_exc()

            self.report({'ERROR'}, str(e))

            return {'CANCELLED'}

        return {'FINISHED'}


class RP_PT_MainPanel(bpy.types.Panel):

    bl_label = "Rig Picker"
    bl_idname = "RP_PT_MAIN"

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Rig Picker"

    def draw(self, context):

        self.layout.operator(
            "rigpicker.open",
            icon='WINDOW'
        )