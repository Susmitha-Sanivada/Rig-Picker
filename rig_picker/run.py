import sys

from PySide6.QtWidgets import QApplication

from rig_picker.ui.main_window import RigPickerWindow


def main():

    app = QApplication(sys.argv)

    window = RigPickerWindow()

    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()