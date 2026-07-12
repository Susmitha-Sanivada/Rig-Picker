"""
main_window.py

Main floating window for Rig Picker.
"""



from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLineEdit,
    QLabel,
    QStatusBar,
)

from PySide6.QtCore import Qt
from .control_list import ControlList
from .rename_dialog import RenameDialog
from .controller import Controller


class RigPickerWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle("Rig Picker")
        self.resize(420, 700)

        self.controller = Controller()
        self.controller.set_window(self)

        self.build_ui()

        self.controller.refresh()
        

    def connect_item(self, item):

        item.selected.connect(self.on_select)

        item.renamed.connect(self.on_rename)

        item.removed.connect(self.on_remove)

    def build_ui(self):

        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)

        # ----------------------------
        # Search
        # ----------------------------

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search...")
        layout.addWidget(self.search)

        # ----------------------------
        # Toolbar
        # ----------------------------

        toolbar = QHBoxLayout()

        self.add_button = QPushButton("Add Selected")
        self.show_button = QPushButton("Show All")
        self.hide_button = QPushButton("Hide All")

        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.show_button)
        toolbar.addWidget(self.hide_button)

        layout.addLayout(toolbar)

        self.add_button.clicked.connect(
            self.controller.add_selected
        )

        self.show_button.clicked.connect(
            self.controller.show_all
        )

        self.hide_button.clicked.connect(
            self.controller.hide_all
        )

        self.control_list = ControlList()

        layout.addWidget(self.control_list)

        

        # ----------------------------
        # Status Bar
        # ----------------------------

        self.status = QStatusBar()

        self.setStatusBar(self.status)

        self.status.showMessage("Ready")
    
    def on_select(self, bone):

        self.controller.select_control(bone)


    def on_remove(self, bone):

        self.controller.remove_control(bone)

        self.control_list.remove_control(bone)


    def on_rename(self, bone):

        item = self.control_list.controls[bone]

        dialog = RenameDialog(item.label)

        if dialog.exec():

            text = dialog.new_name()

            if text:

                self.control_list.rename_control(
                    bone,
                    text,
                )
                self.controller.rename_control(
                    bone,
                    text
                )


