import os
import tempfile
import bpy

# Global cache for the active armature reference
_CACHED_ARM: bpy.types.Object | None = None

# Per-armature picker cache
_PICKER_CACHE = {}

_ACTIVE_CONTROLLER = None
_ACTIVE_WINDOW = None


# ---------------------------------------------------------
# DATA
# ---------------------------------------------------------

class RP_Item(bpy.types.PropertyGroup):
    bone_name: bpy.props.StringProperty()
    label: bpy.props.StringProperty()
    x: bpy.props.FloatProperty(default=-1.0)
    y: bpy.props.FloatProperty(default=-1.0)
    control_size: bpy.props.IntProperty(default=36)
    control_shape: bpy.props.StringProperty(default="CIRCLE")
    control_color: bpy.props.StringProperty(default="GREEN")


# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------

def arm() -> bpy.types.Object | None:
    """Returns the cached armature reference without re-checking mode continuously."""
    global _CACHED_ARM

    if _CACHED_ARM is None:
        obj = bpy.context.active_object
        if obj and obj.type == 'ARMATURE':
            _CACHED_ARM = obj

    return _CACHED_ARM


def update_armature_cache(scene=None, depsgraph=None):
    """Callback function: updates _CACHED_ARM safely during depsgraph updates."""
    global _CACHED_ARM

    # Safely get active object without triggering AttributeError on context
    obj = getattr(bpy.context, "active_object", None)
    if not obj and hasattr(bpy.context, "view_layer"):
        obj = bpy.context.view_layer.objects.active

    # Only update reference if we are actively in POSE mode on an Armature
    if obj and obj.type == 'ARMATURE' and obj.mode == 'POSE':
        if _CACHED_ARM != obj:
            save_picker_state(_CACHED_ARM)
            _CACHED_ARM = obj
            load_picker_state(_CACHED_ARM)

            # Refresh the picker UI if it is open
            if _ACTIVE_CONTROLLER is not None:
                try:
                    _ACTIVE_CONTROLLER.refresh()
                except Exception:
                    pass




def save_picker_state(rig):
    """Save scene picker state for the given armature."""
    if rig is None:
        return
    scene = bpy.context.scene
    _PICKER_CACHE[rig.name] = {
        "background": scene.rp_background_image,
        "symmetry": scene.rp_symmetry,
        "symmetry_x": scene.rp_symmetry_x,
        "items": [
            {
                "bone_name": i.bone_name,
                "label": i.label,
                "x": i.x,
                "y": i.y,
                "control_size": i.control_size,
                "control_shape": i.control_shape,
                "control_color": i.control_color,
            }
            for i in scene.rp_items
        ],
    }


def load_picker_state(rig):
    """Restore scene picker state for the given armature."""
    if rig is None:
        return
    scene = bpy.context.scene
    state = _PICKER_CACHE.get(rig.name)
    scene.rp_items.clear()
    if not state:
        scene.rp_background_image = ""
        scene.rp_symmetry = False
        scene.rp_symmetry_x = -1.0
        return
    scene.rp_background_image = state["background"]
    scene.rp_symmetry = state["symmetry"]
    scene.rp_symmetry_x = state["symmetry_x"]
    for d in state["items"]:
        item = scene.rp_items.add()
        for k,v in d.items():
            setattr(item,k,v)


def ensure_pose(context, rig):
    """Utility to make rig active safely."""
    try:
        if rig and context.view_layer.objects.active != rig:
            context.view_layer.objects.active = rig
    except Exception:
        pass


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
# ADD SELECTED
# ---------------------------------------------------------

class RP_OT_Add(bpy.types.Operator):
    bl_idname = "rp.add_selected"
    bl_label = "Add Selected Controls"

    def execute(self, context):
        rig = arm()
        if not rig:
            return {'CANCELLED'}

        existing = {item.bone_name for item in context.scene.rp_items}

        for pb in rig.pose.bones:
            if not pb.select or pb.name in existing:
                continue

            item = context.scene.rp_items.add()
            item.bone_name = pb.name
            item.label = pb.name

            scene = context.scene
            mirror = mirror_name(pb.name)

            if scene.rp_symmetry and mirror is not None:
                mirror_item = next((i for i in scene.rp_items if i.bone_name == mirror), None)

                if mirror_item and mirror_item.x >= 0 and mirror_item.y >= 0:

                    CONTROL_SIZE = 36.0

                    mirror_center = mirror_item.x + CONTROL_SIZE / 2
                    new_center = 2 * scene.rp_symmetry_x - mirror_center

                    item.x = new_center - CONTROL_SIZE / 2
                    item.y = mirror_item.y

                else:
                    item.x = -1.0
                    item.y = -1.0
            else:
                item.x = -1.0
                item.y = -1.0

        return {'FINISHED'}


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

            bpy.ops.pose.reveal(select=False)
            bpy.ops.pose.select_all(action='DESELECT')
            bpy.ops.pose.hide(unselected=True)

        refresh_3d_view(context)
        return {'FINISHED'}


# ---------------------------------------------------------
# REMOVE
# ---------------------------------------------------------

class RP_OT_Remove(bpy.types.Operator):
    bl_idname = "rp.remove"
    bl_label = "Remove Control"

    index: bpy.props.IntProperty()

    def execute(self, context):
        items = context.scene.rp_items

        if 0 <= self.index < len(items):
            items.remove(self.index)

        bpy.ops.rp.hide_all()
        return {'FINISHED'}


class RP_OT_RemoveByName(bpy.types.Operator):
    bl_idname = "rp.remove_by_name"
    bl_label = "Remove Control"

    bone_name: bpy.props.StringProperty()

    def execute(self, context):
        items = context.scene.rp_items

        for i in reversed(range(len(items))):
            if items[i].bone_name == self.bone_name:
                items.remove(i)
                break

        bpy.ops.rp.hide_all()
        return {'FINISHED'}


# ---------------------------------------------------------
# CLEAR ALL
# ---------------------------------------------------------

class RP_OT_ClearAll(bpy.types.Operator):
    bl_idname = "rp.clear_all"
    bl_label = "Clear All Controls"

    def execute(self, context):
        context.scene.rp_items.clear()
        bpy.ops.rp.hide_all()
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

        scene.rp_background_image = image_path
        self.report({'INFO'}, "Viewport captured.")

        return {'FINISHED'}