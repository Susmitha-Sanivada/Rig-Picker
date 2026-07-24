"""controller.py

Connects the PySide UI with Blender.

The Controller is the single owner of the current picker's data. It holds
that data (for whichever armature is active) in memory as plain Python
dicts/lists, and is responsible for saving/loading it through the JSON
Manager. Blender's Scene is never used for storage.
"""

import os
import tempfile

import bpy

from .. import json_manager


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

        # Name of the armature the currently-loaded data belongs to.
        self.armature_name = None

        # In-memory picker data for the current armature:
        # {"background": str, "symmetry": bool, "symmetry_x": float, "items": [...]}
        self.data = json_manager.new_armature_data()

    # ---------------------------------------------------------

    def set_window(self, window):
        self.window = window

    # ---------------------------------------------------------
    # ARMATURE SWITCHING
    # ---------------------------------------------------------

    def load_armature(self, rig):
        """Saves the current picker to JSON, then loads the given armature's
        picker (or a blank one) from JSON and refreshes the UI."""

        # Persist whatever is currently loaded before switching away from it.
        self.save()

        self.selected_bones = set()

        if rig is None:
            self.armature_name = None
            self.data = json_manager.new_armature_data()
        else:
            self.armature_name = rig.name
            stored = json_manager.get_armature_data(rig.name)
            self.data = stored if stored is not None else json_manager.new_armature_data()

        self.refresh()

        if self.window is not None:
            self.window.size_combo.setEnabled(False)
            self.window.shape_combo.setEnabled(False)
            self.window.color_combo.setEnabled(False)

    def save(self):
        """Writes the current in-memory data to rig_picker_data.json."""
        if self.armature_name:
            json_manager.save_armature_data(self.armature_name, self.data)

    def find_item(self, bone_name):
        return next(
            (item for item in self.data["items"] if item["bone_name"] == bone_name),
            None,
        )

    # ---------------------------------------------------------

    def refresh(self):
        if self.window is None:
            return

        self.window.control_list.clear_controls()

        canvas = self.window.control_list.container

        if self.data.get("background"):
            self.window.control_list.set_background(
                self.data["background"],
                self.data.get("image_offset_x", 0.0),
                self.data.get("image_offset_y", 0.0),
            )
        else:
            self.window.control_list.clear_background()

        canvas.symmetry_enabled = self.data.get("symmetry", False)
        canvas.symmetry_x = self.data.get("symmetry_x", -1.0)

        # Sync the checkbox to match the loaded armature's stored value.
        # blockSignals() prevents this from re-triggering toggle_symmetry(),
        # which would otherwise immediately re-save the value we just loaded
        # (harmless, but pointless) and could clobber symmetry_x if the
        # background isn't set up yet.
        self.window.symmetry_checkbox.blockSignals(True)
        self.window.symmetry_checkbox.setChecked(
            self.data.get("symmetry", False)
        )
        self.window.symmetry_checkbox.blockSignals(False)

        for item in self.data["items"]:
            x = None if item["x"] < 0 else item["x"]
            y = None if item["y"] < 0 else item["y"]

            self.window.control_list.add_control(
                item["bone_name"],
                x,
                y,
                item.get("control_size", 36),
                item.get("control_shape", "CIRCLE"),
                item.get("control_color", "GREEN"),
            )

            widget = self.window.control_list.controls[item["bone_name"]]
            self.window.connect_item(widget)

        self.window.control_list.container.layout_controls()
        self.window.control_list.container.update()

    # ---------------------------------------------------------

    def add_selected(self):
        from ..backend import arm, mirror_name

        rig = arm()
        if not rig:
            return

        CONTROL_SIZE = 36.0
        existing = {item["bone_name"] for item in self.data["items"]}

        for pb in rig.pose.bones:
            if not pb.select or pb.name in existing:
                continue

            new_item = {
                "bone_name": pb.name,
                "label": pb.name,
                "x": -1.0,
                "y": -1.0,
                "control_size": 36,
                "control_shape": "CIRCLE",
                "control_color": "GREEN",
            }

            mirror = mirror_name(pb.name)

            if self.data.get("symmetry") and mirror is not None:
                mirror_item = self.find_item(mirror)

                if mirror_item and mirror_item["x"] >= 0 and mirror_item["y"] >= 0:
                    mirror_center = mirror_item["x"] + CONTROL_SIZE / 2
                    new_center = 2 * self.data.get("symmetry_x", -1.0) - mirror_center

                    new_item["x"] = new_center - CONTROL_SIZE / 2
                    new_item["y"] = mirror_item["y"]

            self.data["items"].append(new_item)

        self.save()
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
        item = self.find_item(bone_name)

        if item:
            self.window.set_selected_control(
                item["control_size"],
                item["control_shape"],
                item["control_color"],
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
        items = {item["bone_name"]: item for item in self.data["items"]}

        for item in self.data["items"]:

            if item["bone_name"] not in self.selected_bones:
                continue

            # -----------------------------
            # Update selected control data
            # -----------------------------
            if size is not None:
                item["control_size"] = size

            if shape is not None:
                item["control_shape"] = shape

            if color is not None:
                item["control_color"] = color

            # -----------------------------
            # Update selected widget
            # -----------------------------
            widget = self.window.control_list.controls.get(item["bone_name"])

            if widget:
                widget.set_appearance(
                    size=size,
                    shape=shape,
                    color=color
                )

            # -----------------------------
            # Update mirrored control
            # -----------------------------
            mirror_bone = mirror_name(item["bone_name"])

            if mirror_bone:

                mirror_item = items.get(mirror_bone)

                if mirror_item:

                    if size is not None:
                        mirror_item["control_size"] = size

                    if shape is not None:
                        mirror_item["control_shape"] = shape

                    if color is not None:
                        mirror_item["control_color"] = color

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
        self.save()

    def show_all(self):
        bpy.ops.rp.show_all()

    # ---------------------------------------------------------

    def hide_all(self):
        bpy.ops.rp.hide_all()

    def capture_view(self):
        if not self.armature_name:
            return

        bpy.ops.rp.capture_view()

        # RP_OT_CaptureView always renders to this fixed, shared temp path.
        temp_path = os.path.join(
            tempfile.gettempdir(),
            "rig_picker_capture.png"
        )

        if not os.path.exists(temp_path):
            return

        # Copy it into this armature's own dedicated image file so it can
        # never be overwritten by capturing a different armature's view.
        dest_path = json_manager.get_image_path(self.armature_name)

        import shutil
        shutil.copyfile(temp_path, dest_path)

        self.data["background"] = dest_path
        self.data["image_offset_x"] = 0.0
        self.data["image_offset_y"] = 0.0
        self.save()

        self.window.control_list.set_background(dest_path, 0.0, 0.0)

    def clear_all(self):
        self.data["items"] = []
        self.save()

        bpy.ops.rp.hide_all()
        self.refresh()

    def delete_selected(self):
        if not self.selected_bones:
            return

        selected = self.selected_bones

        self.data["items"] = [
            item for item in self.data["items"]
            if item["bone_name"] not in selected
        ]
        self.save()

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
            item = self.find_item(first)

            if item:
                self.window.set_selected_control(
                    item["control_size"],
                    item["control_shape"],
                    item["control_color"],
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
        self.data["symmetry"] = enabled

        canvas = self.window.control_list.container
        canvas.symmetry_enabled = enabled

        if self.data.get("symmetry_x", -1.0) < 0:
            if canvas.background:
                self.data["symmetry_x"] = canvas.background.width() / 2

        canvas.symmetry_x = self.data["symmetry_x"]
        canvas.update()

        self.save()
