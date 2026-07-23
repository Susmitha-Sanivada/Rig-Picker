"""controller.py

Connects the PySide UI with Blender.
"""

import bpy


def refresh_3d_view(context):
    """Triggers an instant GPU redraw of all 3D Viewport areas."""
    context.view_layer.update()
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


class Controller:

    def __init__(self):
        self.window = None
        self.selected_bones = set()
        self.active = False

    # ---------------------------------------------------------

    def set_window(self, window):
        self.window = window


    # ---------------------------------------------------------

    def refresh(self):
        if self.window is None:
            return

        scene = bpy.context.scene

        self.window.control_list.clear_controls()

        if scene.rp_background_image:
            self.window.control_list.set_background(
                scene.rp_background_image
            )
            canvas = self.window.control_list.container
            canvas.symmetry_enabled = scene.rp_symmetry
            canvas.symmetry_x = scene.rp_symmetry_x

        for item in scene.rp_items:
            x = None if item.x < 0 else item.x
            y = None if item.y < 0 else item.y

            self.window.control_list.add_control(
                item.bone_name,
                x,
                y,
                item.control_size,
                item.control_shape,
                item.control_color,
            )

            widget = self.window.control_list.controls[item.bone_name]
            self.window.connect_item(widget)

        self.window.control_list.container.layout_controls()
        self.window.control_list.container.update()

    # ---------------------------------------------------------

    def add_selected(self):
        bpy.ops.rp.add_selected()
        self.refresh()

    # ---------------------------------------------------------

    def select_control(self, bone_name, shift=False):
        if shift:
            if bone_name in self.selected_bones:
                self.selected_bones.remove(bone_name)
            else:
                self.selected_bones.add(bone_name)
        else:
            self.selected_bones = {bone_name}

        for name, widget in self.window.control_list.controls.items():
            widget.active = (name in self.selected_bones)
            widget.update()

        # Update UI comboboxes to match selected control appearance
        item = next(
            (item for item in bpy.context.scene.rp_items if item.bone_name == bone_name),
            None
        )

        if item:
            self.window.set_selected_control(
                item.control_size,
                item.control_shape,
                item.control_color,
            )

        bpy.ops.rp.select(
            bone_name=bone_name,
            shift=shift
        )

    def set_selected_size(self, size):
        self._set_selected_appearance(size=size)

    def set_selected_shape(self, shape):
        self._set_selected_appearance(shape=shape)

    def set_selected_color(self, color):
        self._set_selected_appearance(color=color)

    def _set_selected_appearance(self, size=None, shape=None, color=None):
        if not self.selected_bones:
            return

        from ..backend import mirror_name

        # Build a lookup once instead of searching every time
        items = {
            item.bone_name: item
            for item in bpy.context.scene.rp_items
        }

        for item in bpy.context.scene.rp_items:

            if item.bone_name not in self.selected_bones:
                continue

            # -----------------------------
            # Update selected control data
            # -----------------------------
            if size is not None:
                item.control_size = size

            if shape is not None:
                item.control_shape = shape

            if color is not None:
                item.control_color = color

            # -----------------------------
            # Update selected widget
            # -----------------------------
            widget = self.window.control_list.controls.get(item.bone_name)

            if widget:
                widget.set_appearance(
                    size=size,
                    shape=shape,
                    color=color
                )

            # -----------------------------
            # Update mirrored control
            # -----------------------------
            mirror_bone = mirror_name(item.bone_name)

            if mirror_bone:

                mirror_item = items.get(mirror_bone)

                if mirror_item:

                    if size is not None:
                        mirror_item.control_size = size

                    if shape is not None:
                        mirror_item.control_shape = shape

                    if color is not None:
                        mirror_item.control_color = color

                    mirror_widget = self.window.control_list.controls.get(
                        mirror_bone
                    )

                    if mirror_widget:
                        mirror_widget.set_appearance(
                            size=size,
                            shape=shape,
                            color=color
                        )

        # Update control positions once in case size changed
        self.window.control_list.container.layout_controls()

    def show_all(self):
        bpy.ops.rp.show_all()

    # ---------------------------------------------------------

    def hide_all(self):
        bpy.ops.rp.hide_all()

    def capture_view(self):
        bpy.ops.rp.capture_view()
        image_path = bpy.context.scene.rp_background_image

        if image_path:
            self.window.control_list.set_background(image_path)

    def clear_all(self):
        bpy.ops.rp.clear_all()
        self.refresh()

    def delete_selected(self):
        if not self.selected_bones:
            return

        selected = list(self.selected_bones)

        for bone in selected:
            bpy.ops.rp.remove_by_name(bone_name=bone)

        self.selected_bones.clear()

        self.window.size_combo.setEnabled(False)
        self.window.shape_combo.setEnabled(False)
        self.window.color_combo.setEnabled(False)

        # Force hiding remaining controls & update UI
        bpy.ops.rp.hide_all()
        self.refresh()

    def select_all(self):
        self.selected_bones = set(self.window.control_list.controls.keys())

        # Update UI widgets
        for name, widget in self.window.control_list.controls.items():
            widget.active = True
            widget.update()

        from ..backend import arm
        rig = arm()

        if rig and rig.type == 'ARMATURE':
            for pb in rig.pose.bones:
                if pb.name in self.selected_bones:
                    pb.bone.hide = False
                    pb.select = True
                else:
                    pb.bone.hide = True
                    pb.select = False

            # Refresh 3D Viewport immediately
            refresh_3d_view(bpy.context)

        # Update appearance control dropdowns
        if self.selected_bones:
            first = next(iter(self.selected_bones))
            item = next(
                (item for item in bpy.context.scene.rp_items if item.bone_name == first),
                None,
            )

            if item:
                self.window.set_selected_control(
                    item.control_size,
                    item.control_shape,
                    item.control_color,
                )

    def deselect_all(self):
        self.selected_bones.clear()

        for widget in self.window.control_list.controls.values():
            widget.active = False
            widget.update()

        self.window.size_combo.setEnabled(False)
        self.window.shape_combo.setEnabled(False)
        self.window.color_combo.setEnabled(False)

        from ..backend import arm
        rig = arm()

        if rig and rig.type == 'ARMATURE':
            for pb in rig.pose.bones:
                pb.select = False
                pb.bone.hide = True
            rig.data.bones.active = None

            # Refresh 3D Viewport immediately
            refresh_3d_view(bpy.context)

    def toggle_symmetry(self, enabled):
        scene = bpy.context.scene
        scene.rp_symmetry = enabled

        canvas = self.window.control_list.container
        canvas.symmetry_enabled = enabled

        if scene.rp_symmetry_x < 0:
            if canvas.background:
                scene.rp_symmetry_x = canvas.background.width() / 2

        canvas.symmetry_x = scene.rp_symmetry_x
        canvas.update()