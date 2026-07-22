bl_info = {
    "name": "Rig Picker",
    "author": "Susmitha",
    "version": (1, 0, 0),
    "blender": (4, 3, 0),
    "location": "View3D",
    "description": "Floating Rig Picker",
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
    RP_OT_RemoveByName,
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
    RP_OT_RemoveByName,
    RP_OT_CaptureView,
    RP_PT_MainPanel,
)


from .dependency import ensure_qt
from .backend import update_armature_cache


def register():

    for cls in classes:
        bpy.utils.register_class(cls)

    ensure_qt()

    # ----------------------------------------------------
    # Register Armature Cache Handler
    # ----------------------------------------------------
    if update_armature_cache not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(update_armature_cache)

    bpy.types.Scene.rp_items = bpy.props.CollectionProperty(
        type=RP_Item
    )

    bpy.types.Scene.rp_search = bpy.props.StringProperty()

    bpy.types.Scene.rp_background_image = bpy.props.StringProperty()

    bpy.types.Scene.rp_symmetry = bpy.props.BoolProperty(
        name="Symmetry",
        default=False
    )

    bpy.types.Scene.rp_symmetry_x = bpy.props.FloatProperty(
        name="Symmetry X",
        default=-1.0
    )


def unregister():

    # ----------------------------------------------------
    # Unregister Armature Cache Handler
    # ----------------------------------------------------
    if update_armature_cache in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(update_armature_cache)

    del bpy.types.Scene.rp_items
    del bpy.types.Scene.rp_search
    del bpy.types.Scene.rp_background_image
    del bpy.types.Scene.rp_symmetry
    del bpy.types.Scene.rp_symmetry_x

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)