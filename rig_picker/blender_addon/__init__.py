bl_info = {
    "name": "Rig Picker",
    "author": "Susmitha",
    "version": (2, 0, 0),
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

from . import backend
from . import json_manager
from .backend import (
    RP_OT_Select,
    RP_OT_ShowAll,
    RP_OT_HideAll,
    RP_OT_CaptureView,
)

classes = (
    RP_OT_OpenPicker,

    RP_OT_Select,
    RP_OT_ShowAll,
    RP_OT_HideAll,
    RP_OT_CaptureView,

    RP_PT_MainPanel,
)


from .dependency import ensure_qt
from .backend import poll_active_armature, _POLL_INTERVAL


def register():

    for cls in classes:
        bpy.utils.register_class(cls)


    # Initialize backend UI/armature references. These are module-level
    # Python globals that survive across File > Open / File > Revert
    # within the same Blender process, so without resetting them here
    # they could keep pointing at objects/controllers from a previous
    # file - reset on every register() so we always start clean.
    backend._ACTIVE_CONTROLLER = None
    backend._ACTIVE_WINDOW = None
    backend._CACHED_ARM = None
    backend._CACHED_ARM_NAME = None

    # Force a fresh read of rig_picker_data.json from disk rather than
    # trusting an in-memory cache that may be stale after a file reload.
    json_manager.reload()

    # ----------------------------------------------------
    # Watch for the active armature changing.
    #
    # depsgraph_update_post is NOT used for this: it's meant to fire on
    # actual data changes (transforms, mode switches, etc.) and is not a
    # reliable signal for a pure active-object/selection change - which is
    # exactly the case of clicking a different armature in the viewport.
    # A periodic timer sidesteps that unreliability entirely.
    #
    # persistent=True keeps this timer running across File > Open without
    # needing to be re-registered. Picker data lives entirely in
    # rig_picker_data.json via the JSON Manager - there are no Scene
    # properties to register here.
    # ----------------------------------------------------
    if not bpy.app.timers.is_registered(poll_active_armature):
        bpy.app.timers.register(
            poll_active_armature,
            first_interval=_POLL_INTERVAL,
            persistent=True,
        )


def unregister():

    # ----------------------------------------------------
    # Stop watching for armature changes
    # ----------------------------------------------------
    if bpy.app.timers.is_registered(poll_active_armature):
        bpy.app.timers.unregister(poll_active_armature)

    backend._ACTIVE_CONTROLLER = None
    backend._ACTIVE_WINDOW = None

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
