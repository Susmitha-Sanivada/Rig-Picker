bl_info = {
    "name": "Rig Picker",
    "author": "Susmitha",
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

from .backend import (
    RP_Item,
    RP_OT_Add,
    RP_OT_ClearAll,
    RP_OT_Select,
    RP_OT_ShowAll,
    RP_OT_HideAll,
    RP_OT_Remove,
    RP_OT_Rename,
    RP_OT_CaptureView
)

classes = (
    RP_Item,

    RP_OT_OpenPicker,

    RP_OT_Add,
    RP_OT_ClearAll,
    RP_OT_Select,
    RP_OT_ShowAll,
    RP_OT_HideAll,
    RP_OT_Remove,
    RP_OT_Rename,
    RP_OT_CaptureView,
    RP_PT_MainPanel,
)


def register():

    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Scene.rp_items = bpy.props.CollectionProperty(
        type=RP_Item
    )

    bpy.types.Scene.rp_search = bpy.props.StringProperty()

    bpy.types.Scene.rp_background_image = bpy.props.StringProperty()


def unregister():

    del bpy.types.Scene.rp_items
    del bpy.types.Scene.rp_search
    del bpy.types.Scene.rp_background_image

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)