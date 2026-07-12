from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QHBoxLayout,
)


class RenameDialog(QDialog):

    def __init__(self, current_name):
        super().__init__()

        self.setWindowTitle("Rename Control")

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("New Name"))

        self.name_edit = QLineEdit(current_name)
        layout.addWidget(self.name_edit)

        buttons = QHBoxLayout()

        ok = QPushButton("OK")
        cancel = QPushButton("Cancel")

        buttons.addWidget(ok)
        buttons.addWidget(cancel)

        layout.addLayout(buttons)

        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)

    def new_name(self):
        return self.name_edit.text().strip()