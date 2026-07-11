bl_info = {
    "name": "Rig Picker",
    "author": "You",
    "version": (1, 0, 0),
    "blender": (4, 3, 0),
    "location": "View3D > Sidebar > Rig Picker",
    "category": "Animation",
}

import bpy

from .operators import (
    RP_OT_OpenPicker,
    RP_PT_MainPanel,
)

classes = (
    RP_OT_OpenPicker,
    RP_PT_MainPanel,
)


def register():

    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()