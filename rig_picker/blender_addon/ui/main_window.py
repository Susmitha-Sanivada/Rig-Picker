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
    QStatusBar,
    QSizePolicy
)

from .control_list import ControlList
from .controller import Controller
from .flow_layout import FlowLayout


class RigPickerWindow(QMainWindow):

    def __init__(self, parent=None):
        super().__init__(parent)

        self.setWindowTitle("Rig Picker")

        self.controller = Controller()
        self.controller.set_window(self)

        self.build_ui()

        # Load existing controls from Blender
        self.controller.refresh()

    # --------------------------------------------------------

    def build_ui(self):

        central = QWidget()
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)

        # ----------------------------------------------------
        # Toolbar
        # ----------------------------------------------------

        toolbar = FlowLayout()

        self.capture_button = QPushButton("Capture View")
        self.add_button = QPushButton("Add Selected")
        self.clear_button = QPushButton("Clear All")
        self.show_button = QPushButton("Show All")
        self.hide_button = QPushButton("Hide All")

        toolbar.addWidget(self.capture_button)
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.clear_button)
        toolbar.addWidget(self.show_button)
        toolbar.addWidget(self.hide_button)

        layout.addLayout(toolbar)

        # ----------------------------------------------------
        # Picker Area
        # ----------------------------------------------------

        self.control_list = ControlList()

        layout.addWidget(self.control_list)

        # ----------------------------------------------------
        # Connections
        # ----------------------------------------------------

        self.capture_button.clicked.connect(
            self.controller.capture_view
        )

        self.add_button.clicked.connect(
            self.controller.add_selected
        )

        self.clear_button.clicked.connect(
            self.controller.clear_all
        )

        self.show_button.clicked.connect(
            self.controller.show_all
        )

        self.hide_button.clicked.connect(
            self.controller.hide_all
        )

        # ----------------------------------------------------
        # Status Bar
        # ----------------------------------------------------

        self.status = QStatusBar()

        self.setStatusBar(self.status)

        self.status.showMessage("Ready")

    # --------------------------------------------------------

    def connect_item(self, item):

        item.clicked.connect(
            self.controller.select_control
        )