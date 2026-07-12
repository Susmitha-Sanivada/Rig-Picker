"""
controller.py

Connects the PySide UI with Blender.
"""

import bpy


class Controller:

    def __init__(self):
        self.window = None

    # ----------------------------------------------------

    def set_window(self, window):
        self.window = window

    # ----------------------------------------------------

    def refresh(self):

        if self.window is None:
            return

        self.window.control_list.clear_controls()

        for item in bpy.context.scene.rp_items:

            self.window.control_list.add_control(
                bone_name=item.bone_name,
                label=item.label,
            )

            widget = self.window.control_list.controls[item.bone_name]

            self.window.connect_item(widget)

    # ----------------------------------------------------

    def add_selected(self):

        bpy.ops.rp.add_selected()

        self.refresh()

    # ----------------------------------------------------

    def select_control(self, bone_name):

        bpy.ops.rp.select(
            bone_name=bone_name
        )

    # ----------------------------------------------------

    def rename_control(self, bone_name, new_name):

        print("Rename:", bone_name, new_name)

    # ----------------------------------------------------

    def remove_control(self, bone_name):

        items = bpy.context.scene.rp_items

        for index, item in enumerate(items):

            if item.bone_name == bone_name:

                bpy.ops.rp.remove(index=index)

                break

        self.refresh()

    # ----------------------------------------------------

    def show_all(self):

        bpy.ops.rp.show_all()

    # ----------------------------------------------------

    def hide_all(self):

        bpy.ops.rp.hide_all()