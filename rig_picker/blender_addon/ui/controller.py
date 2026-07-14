"""
controller.py

Connects the PySide UI with Blender.
"""

import bpy


class Controller:

    def __init__(self):

        self.window = None

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

        bpy.ops.rp.select(
            bone_name=bone_name
        )

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