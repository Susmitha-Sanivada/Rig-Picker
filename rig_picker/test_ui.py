import sys
import os
from unittest.mock import MagicMock

# 1. Dynamically locate the addon folder sitting right next to this script
current_dir = os.path.dirname(os.path.abspath(__file__))

# Find the directory next to this script containing your addon
addon_folder_name = "rig_picker"  # Default folder name
for item in os.listdir(current_dir):
    item_path = os.path.join(current_dir, item)
    if os.path.isdir(item_path) and not item.startswith(".") and item != "__pycache__":
        if os.path.exists(os.path.join(item_path, "ui")):
            addon_folder_name = item
            break

addon_path = os.path.join(current_dir, addon_folder_name)

# Force Python to search inside your addon folder FIRST
sys.path.insert(0, addon_path)
sys.path.insert(0, current_dir)

print(f"Targeting addon folder: {addon_path}")

# ==========================================
# 2. MOCK BLENDER ENVIRONMENT (bqt & bpy)
# ==========================================

# Mock 'bqt' using local PySide6
try:
    import PySide6
    from PySide6 import QtCore, QtWidgets, QtGui
    
    sys.modules['bqt'] = PySide6
    sys.modules['bqt.QtCore'] = QtCore
    sys.modules['bqt.QtWidgets'] = QtWidgets
    sys.modules['bqt.QtGui'] = QtGui
    print("Successfully bypassed 'bqt' using local PySide6 environment!")
except ImportError:
    print("Error: PySide6 is not installed. Run 'pip install PySide6' in your terminal.")
    sys.exit(1)

# Mock 'bpy' (Blender Python API) so 'controller.py' can import it without crashing
bpy_mock = MagicMock()
sys.modules['bpy'] = bpy_mock
print("Successfully mocked Blender 'bpy' module!")

# ==========================================
# 3. RUN APPLICATION
# ==========================================
try:
    from ui.main_window import RigPickerWindow as MainWindow
except ModuleNotFoundError as e:
    print("\n--- IMPORT ERROR DIAGNOSTIC ---")
    print("Could not find 'ui' folder inside search paths.")
    print(f"Current sys.path directories being searched:\n" + "\n".join(sys.path[:3]))
    print("--------------------------------\n")
    raise e

def main():
    app = QtWidgets.QApplication(sys.argv)
    
    # Initialize and show your window
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()