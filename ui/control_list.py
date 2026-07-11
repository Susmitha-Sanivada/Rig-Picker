from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QScrollArea,
)

from ui.control_item import ControlItem


class ControlList(QScrollArea):

    def __init__(self):
        super().__init__()

        self.controls = {}

        self.container = QWidget()

        self.layout = QVBoxLayout(self.container)

        self.layout.setSpacing(4)
        self.layout.setContentsMargins(4, 4, 4, 4)

        self.setWidget(self.container)
        self.setWidgetResizable(True)

    def add_control(self, bone_name, label=None):

        if bone_name in self.controls:
            return

        item = ControlItem(bone_name, label)

        self.layout.addWidget(item)

        self.controls[bone_name] = item

    def remove_control(self, bone_name):

        if bone_name not in self.controls:
            return

        item = self.controls.pop(bone_name)

        self.layout.removeWidget(item)

        item.deleteLater()

    def clear_controls(self):

        for bone in list(self.controls.keys()):
            self.remove_control(bone)

    def rename_control(self, bone_name, new_label):

        if bone_name not in self.controls:
            return

        self.controls[bone_name].set_label(new_label)