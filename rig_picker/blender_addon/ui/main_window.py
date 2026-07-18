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
    QComboBox,
    QLabel,
    QMenu,
    QStatusBar,
    QSizePolicy
)
from PySide6.QtCore import Qt, QPoint, QRectF
from PySide6.QtGui import QPainterPath, QRegion

from .control_list import ControlList
from .controller import Controller
from .flow_layout import FlowLayout

from pathlib import Path


class PickerComboBox(QComboBox):
    """A combo box whose full option list opens directly below the field."""

    def showPopup(self):
        menu = QMenu(self)
        menu.setObjectName("pickerComboPopup")
        menu.setWindowFlags(
            Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
        menu.setFixedWidth(self.width())
        menu.setContentsMargins(0, 0, 0, 0)
        menu.setStyleSheet(self.window().styleSheet())

        for index in range(self.count()):
            action = menu.addAction(self.itemText(index))
            action.setCheckable(True)
            action.setChecked(index == self.currentIndex())
            action.triggered.connect(
                lambda checked=False, value=index: self.setCurrentIndex(value)
            )

        menu.adjustSize()
        menu.setFixedWidth(self.width())
        rounded_path = QPainterPath()
        rounded_path.addRoundedRect(QRectF(menu.rect()), 5, 5)
        menu.setMask(QRegion(rounded_path.toFillPolygon().toPolygon()))

        menu.exec(self.mapToGlobal(QPoint(0, self.height())))

class RigPickerWindow(QMainWindow):

    def __init__(self, parent=None):
        super().__init__(parent)

        theme = (
            Path(__file__).parent /
            "themes" /
            "blender_dark.qss"
        )

        with open(theme, encoding="utf8") as f:

            self.setStyleSheet(f.read())

        self.setWindowTitle("Rig Picker")
        self.resize(360, 500)
        self.setMinimumSize(360, 500)

        self.controller = Controller()
        self.controller.set_window(self)

        self.build_ui()

        # Load existing controls from Blender
        self.controller.refresh()

    # --------------------------------------------------------

    def build_ui(self):

        central = QWidget()
        central.setObjectName("rigPickerCentral")
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(5)

        # ----------------------------------------------------
        # Toolbar
        # ----------------------------------------------------

        toolbar = FlowLayout()

        self.capture_button = QPushButton("Capture View")
        self.add_button = QPushButton("Add Selected")
        self.clear_button = QPushButton("Clear All")
        self.show_button = QPushButton("Show All")
        self.hide_button = QPushButton("Hide All")
        self.size_combo = PickerComboBox()
        self.size_combo.setObjectName("pickerCombo")
        self.size_combo.addItems([
            "40 px", "36 px", "32 px", "28 px",
        ])
        self.shape_combo = PickerComboBox()
        self.shape_combo.setObjectName("pickerCombo")
        self.shape_combo.addItem("Circle", "CIRCLE")
        self.shape_combo.addItem("Rectangle", "RECTANGLE")
        self.shape_combo.addItem("Triangle", "TRIANGLE")
        self.shape_combo.addItem("Square", "SQUARE")
        self.color_combo = PickerComboBox()
        self.color_combo.setObjectName("pickerCombo")
        self.color_combo.addItem("Red", "RED")
        self.color_combo.addItem("Green", "GREEN")
        self.color_combo.addItem("Blue", "BLUE")
        self.color_combo.addItem("Yellow", "YELLOW")

        for combo in (self.size_combo, self.shape_combo, self.color_combo):
            combo.setMaxVisibleItems(combo.count())
            combo.view().setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            combo.view().setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.size_combo.setEnabled(False)
        self.shape_combo.setEnabled(False)
        self.color_combo.setEnabled(False)

        toolbar.addWidget(self.capture_button)
        toolbar.addWidget(self.add_button)
        toolbar.addWidget(self.clear_button)
        toolbar.addWidget(self.show_button)
        toolbar.addWidget(self.hide_button)

        layout.addLayout(toolbar)

        # Keep selection settings together rather than letting the two
        # dropdowns wrap independently in the action-button flow layout.
        selection_row = QHBoxLayout()
        selection_row.setContentsMargins(0, 0, 0, 0)
        selection_row.setSpacing(0)
        size_field = QWidget()
        size_field.setObjectName("pickerSelectorField")
        size_layout = QHBoxLayout(size_field)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.setSpacing(0)
        size_label = QLabel("Size")
        size_label.setObjectName("pickerSelectorLabel")
        size_layout.addWidget(size_label)
        size_layout.addWidget(self.size_combo)
        selection_row.addWidget(size_field)

        selection_row.addSpacing(8)

        shape_field = QWidget()
        shape_field.setObjectName("pickerSelectorField")
        shape_layout = QHBoxLayout(shape_field)
        shape_layout.setContentsMargins(0, 0, 0, 0)
        shape_layout.setSpacing(0)
        shape_label = QLabel("Shape")
        shape_label.setObjectName("pickerSelectorLabel")
        shape_layout.addWidget(shape_label)
        shape_layout.addWidget(self.shape_combo)
        selection_row.addWidget(shape_field)

        selection_row.addSpacing(8)

        color_field = QWidget()
        color_field.setObjectName("pickerSelectorField")
        color_layout = QHBoxLayout(color_field)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(0)
        color_label = QLabel("Color")
        color_label.setObjectName("pickerSelectorLabel")
        color_layout.addWidget(color_label)
        color_layout.addWidget(self.color_combo)
        selection_row.addWidget(color_field)
        selection_row.addStretch()
        layout.addLayout(selection_row)

        combo_style = """
            QComboBox {
                min-height: 20px;
                margin: 0;
                padding: 1px 26px 1px 9px;
                border: none;
                border-radius: 5px;
                background: #292929;
                color: #f4f5f7;
            }
            QComboBox:hover { background: #333333; }
            QComboBox:focus { background: #333333; }
            QComboBox:disabled {
                color: #858991;
                background: #292929;
            }
            QComboBox::drop-down {
                width: 24px;
                border: 0;
                border-top-right-radius: 5px;
                border-bottom-right-radius: 5px;
                background: transparent;
            }
            QComboBox::down-arrow { image: none; }
            QComboBox QAbstractItemView {
                padding: 4px;
                border: none;
                border-radius: 4px;
                background: #292929;
                color: #f4f5f7;
                selection-background-color: #386b9b;
                selection-color: white;
            }
        """
        self.size_combo.setFixedWidth(74)
        self.shape_combo.setFixedWidth(112)
        self.color_combo.setFixedWidth(86)
        self.size_combo.setFixedHeight(24)
        self.shape_combo.setFixedHeight(24)
        self.color_combo.setFixedHeight(24)
        self.size_combo.setStyleSheet(combo_style)
        self.shape_combo.setStyleSheet(combo_style)
        self.color_combo.setStyleSheet(combo_style)

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
        self.size_combo.currentTextChanged.connect(
            lambda text: self.controller.set_selected_size(int(text.split()[0]))
        )
        self.shape_combo.currentIndexChanged.connect(
            lambda index: self.controller.set_selected_shape(self.shape_combo.itemData(index))
        )
        self.color_combo.currentIndexChanged.connect(
            lambda index: self.controller.set_selected_color(self.color_combo.itemData(index))
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

    def set_selected_control(self, size, shape, color):
        self.size_combo.setEnabled(True)
        self.shape_combo.setEnabled(True)
        self.color_combo.setEnabled(True)

        size_index = self.size_combo.findText(f"{size} px")
        if size_index >= 0:
            self.size_combo.blockSignals(True)
            self.size_combo.setCurrentIndex(size_index)
            self.size_combo.blockSignals(False)

        shape_index = self.shape_combo.findData(shape)
        if shape_index >= 0:
            self.shape_combo.blockSignals(True)
            self.shape_combo.setCurrentIndex(shape_index)
            self.shape_combo.blockSignals(False)

        color_index = self.color_combo.findData(color)
        if color_index >= 0:
            self.color_combo.blockSignals(True)
            self.color_combo.setCurrentIndex(color_index)
            self.color_combo.blockSignals(False)
