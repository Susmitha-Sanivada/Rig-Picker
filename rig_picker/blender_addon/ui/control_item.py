from PySide6.QtWidgets import (
    QWidget,
    QPushButton,
    QHBoxLayout,
)

from PySide6.QtCore import Signal


class ControlItem(QWidget):

    selected = Signal(str)
    renamed = Signal(str)
    removed = Signal(str)

    def __init__(self, bone_name, label=None):
        super().__init__()

        self.bone_name = bone_name
        self.label = label or bone_name

        self.build_ui()

    def build_ui(self):

        layout = QHBoxLayout(self)

        layout.setContentsMargins(2, 2, 2, 2)

        self.select_btn = QPushButton(self.label)
        self.rename_btn = QPushButton("✏")
        self.delete_btn = QPushButton("❌")

        self.rename_btn.setFixedWidth(35)
        self.delete_btn.setFixedWidth(35)

        self.rename_btn.setToolTip("Rename")
        self.delete_btn.setToolTip("Remove")

        layout.addWidget(self.select_btn)
        layout.addWidget(self.rename_btn)
        layout.addWidget(self.delete_btn)

        self.select_btn.clicked.connect(self.select_clicked)
        self.rename_btn.clicked.connect(self.rename_clicked)
        self.delete_btn.clicked.connect(self.delete_clicked)

    def select_clicked(self):
        self.selected.emit(self.bone_name)

    def rename_clicked(self):
        self.renamed.emit(self.bone_name)

    def delete_clicked(self):
        self.removed.emit(self.bone_name)

    def set_label(self, text):
        self.label = text
        self.select_btn.setText(text)