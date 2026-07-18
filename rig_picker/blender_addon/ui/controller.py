"""
controller.py

Connects the PySide UI with Blender.
"""

import bpy


class Controller:

    def __init__(self):

        self.window = None
        self.selected_bone_name = None
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

            widget = self.window.control_list.controls[
                item.bone_name
            ]

            self.window.connect_item(widget)

    # ---------------------------------------------------------

    def add_selected(self):

        bpy.ops.rp.add_selected()

        self.refresh()

    # ---------------------------------------------------------

    def select_control(self, bone_name):

        self.selected_bone_name = bone_name

        # ----------------------------------------
        # Update active control
        # ----------------------------------------

        for widget in self.window.control_list.controls.values():
            widget.active = False
            widget.update()

        widget = self.window.control_list.controls.get(bone_name)
        if widget:
            widget.active = True
            widget.update()

        # ----------------------------------------

        item = next(
            (item for item in bpy.context.scene.rp_items
            if item.bone_name == bone_name),
            None
        )

        if item:
            self.window.set_selected_control(
                item.control_size,
                item.control_shape,
                item.control_color,
            )

        bpy.ops.rp.select(
            bone_name=bone_name
        )

    def set_selected_size(self, size):
        self._set_selected_appearance(size=size)

    def set_selected_shape(self, shape):
        self._set_selected_appearance(shape=shape)

    def set_selected_color(self, color):
        self._set_selected_appearance(color=color)

    def _set_selected_appearance(self, size=None, shape=None, color=None):
        if not self.selected_bone_name:
            return

        for item in bpy.context.scene.rp_items:
            if item.bone_name != self.selected_bone_name:
                continue
            if size is not None:
                item.control_size = size
            if shape is not None:
                item.control_shape = shape
            if color is not None:
                item.control_color = color

            widget = self.window.control_list.controls.get(item.bone_name)
            if widget:
                widget.set_appearance(size=size, shape=shape, color=color)
                self.window.control_list.container.layout_controls()
            break

    # ---------------------------------------------------------

    def show_all(self):

        bpy.ops.rp.show_all()

    # ---------------------------------------------------------

    def hide_all(self):

        bpy.ops.rp.hide_all()

    def capture_view(self):

        bpy.ops.rp.capture_view()

        image_path = bpy.context.scene.rp_background_image

        if image_path:

            self.window.control_list.set_background(
                image_path
            )
    
    def clear_all(self):

        bpy.ops.rp.clear_all()

        self.refresh()
