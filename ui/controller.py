"""
controller.py

Acts as the bridge between the UI and Blender.

For now these functions are stubs.
Later they will call bpy.
"""


class Controller:

    def __init__(self):

        self.window = None

    def set_window(self, window):

        self.window = window

    # ------------------------

    def select_control(self, bone_name):

        print(f"Select : {bone_name}")

    # ------------------------

    def rename_control(self, bone_name, new_label):

        print(f"Rename {bone_name} -> {new_label}")

    # ------------------------

    def remove_control(self, bone_name):

        print(f"Remove {bone_name}")

    # ------------------------

    def add_selected(self):

        print("Add Selected")

    # ------------------------

    def show_all(self):

        print("Show All")

    # ------------------------

    def hide_all(self):

        print("Hide All")