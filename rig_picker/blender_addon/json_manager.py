"""
json_manager.py

Single source of truth for reading/writing Rig Picker data on disk.

Storage layout (rig_picker_data.json):
{
    "ArmatureName": {
        "background": "C:/.../capture.png",
        "symmetry": false,
        "symmetry_x": -1.0,
        "items": [
            {
                "bone_name": "hand.L",
                "label": "hand.L",
                "x": 120.0,
                "y": 84.0,
                "control_size": 36,
                "control_shape": "CIRCLE",
                "control_color": "GREEN"
            },
            ...
        ]
    },
    ...
}

No Blender Scene properties, no .blend storage - this file on disk is the
only place picker layouts live. It is shared across every .blend file, so a
rig named "Hero" keeps its picker layout no matter which file it is opened
in.
"""

import os
import re
import json

FILE_NAME = "rig_picker_data.json"
IMAGES_FOLDER_NAME = "rig_picker_images"

# In-memory cache of the whole file, loaded lazily and kept in sync with disk.
_data = None


# ---------------------------------------------------------
# STORAGE LOCATION
# ---------------------------------------------------------

def get_storage_path():
    """Returns the full path to rig_picker_data.json.

    Requested location:
        %APPDATA%\\Blender Foundation\\Blender\\5.0\\scripts\\addons\\rig_picker_data.json

    Falls back to Blender's own user scripts/addons folder on platforms
    where APPDATA isn't defined (macOS/Linux), so the addon still works
    there instead of crashing.
    """

    appdata = os.getenv("APPDATA")

    if appdata:
        folder = os.path.join(
            appdata,
            "Blender Foundation",
            "Blender",
            "5.0",
            "scripts",
            "addons",
        )
    else:
        import bpy
        folder = bpy.utils.user_resource('SCRIPTS', path="addons")

    os.makedirs(folder, exist_ok=True)

    return os.path.join(folder, FILE_NAME)


# ---------------------------------------------------------
# PER-ARMATURE IMAGE STORAGE
# ---------------------------------------------------------

def get_images_folder():
    """Folder that holds one persistent captured-view image per armature,
    stored next to rig_picker_data.json (not in the OS temp folder, which
    can be cleared at any time)."""

    folder = os.path.join(
        os.path.dirname(get_storage_path()),
        IMAGES_FOLDER_NAME,
    )
    os.makedirs(folder, exist_ok=True)
    return folder


def get_image_path(armature_name):
    """Returns the dedicated image path for one armature's captured view.

    Each armature gets its own file (named after the armature, sanitized
    for filesystem safety) so capturing a view for one rig can never
    overwrite another rig's saved background.
    """
    safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', armature_name)
    return os.path.join(get_images_folder(), f"{safe_name}.png")


# ---------------------------------------------------------
# LOAD / SAVE
# ---------------------------------------------------------

def _load():
    """Loads the JSON file into the in-memory cache (once)."""
    global _data

    if _data is not None:
        return _data

    path = get_storage_path()

    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf8") as f:
                _data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[Rig Picker] Could not read {path}: {e}")

            # Fall back to the last known-good backup instead of silently
            # wiping every armature's saved picker data.
            backup_path = path + ".bak"
            if os.path.exists(backup_path):
                try:
                    with open(backup_path, "r", encoding="utf8") as f:
                        _data = json.load(f)
                    print(f"[Rig Picker] Recovered data from {backup_path}")
                except (OSError, json.JSONDecodeError) as e2:
                    print(f"[Rig Picker] Backup also unreadable: {e2}")
                    _data = {}
            else:
                _data = {}
    else:
        _data = {}

    return _data


def _flush():
    """Writes the in-memory cache to disk.

    Writes are atomic (write to a temp file, then os.replace() it into
    place) so a crash, force-quit, or antivirus/sync lock mid-write can
    never leave rig_picker_data.json truncated or corrupted - a corrupted
    file would otherwise be indistinguishable from "no data" on next load,
    silently wiping every armature's saved state.

    A .bak copy of the previous good version is also kept as a fallback
    in case the file is ever found unreadable on load anyway.
    """
    path = get_storage_path()
    tmp_path = path + ".tmp"
    backup_path = path + ".bak"

    try:
        with open(tmp_path, "w", encoding="utf8") as f:
            json.dump(_data, f, indent=2)

        if os.path.exists(path):
            try:
                import shutil
                shutil.copyfile(path, backup_path)
            except OSError:
                pass

        os.replace(tmp_path, path)

    except OSError as e:
        print(f"[Rig Picker] Could not write {path}: {e}")


def reload():
    """Forces the next access to re-read the file from disk."""
    global _data
    _data = None


# ---------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------

def new_armature_data():
    """Returns an empty picker data structure for an armature with no saved state."""
    return {
        "background": "",
        "symmetry": False,
        "symmetry_x": -1.0,
        "image_offset_x": 0.0,
        "image_offset_y": 0.0,
        "items": [],
    }


def get_armature_data(armature_name):
    """Returns the stored data for an armature, or None if nothing is stored."""
    data = _load()
    return data.get(armature_name)


def save_armature_data(armature_name, armature_data):
    """Persists the given data for an armature and writes it to disk immediately."""
    if not armature_name:
        return

    data = _load()
    data[armature_name] = armature_data
    _flush()


def delete_armature_data(armature_name):
    """Removes stored data for an armature (e.g. if the user deletes the rig)."""
    data = _load()

    if armature_name in data:
        del data[armature_name]
        _flush()
