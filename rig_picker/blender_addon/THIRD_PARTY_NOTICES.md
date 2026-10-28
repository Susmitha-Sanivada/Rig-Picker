# Third-Party Notices

Rig Picker itself is licensed under the GNU General Public License,
version 3 or later (GPL-3.0-or-later). See `LICENSE`.

It uses the third-party software listed below. None of it is modified.

## Bundled with this addon

### BQT (PySide Qt wrapper for Blender)

- Where: `vendor/bqt.zip` (unpacked by the addon on first launch)
- Version: 2.2.0 (GitHub release tag `2.2.0`, commit `d4311a213ebc6b4edcf0e6f67bb91693ffc79282`)
- Source: https://github.com/techartorg/bqt
- License: Mozilla Public License 2.0 (MPL-2.0)
- License text: `vendor/BQT-LICENSE-MPL-2.0.txt`

The BQT files are unmodified and keep their own MPL-2.0 notices. The
source is included as plain Python files inside `vendor/bqt.zip`.

The `bqt/` folder inside `vendor/bqt.zip` is identical to the `bqt/` folder
of that tag. Its `blender_manifest.toml` still says version 2.0.0, because
BQT's author raised the number in a commit made just after creating the tag.

## Downloaded on first launch (not included in this addon)

When the picker is first opened, the addon runs `pip` to download the
packages below from PyPI into the addon's own `python/` folder on the
user's machine. They are not part of the addon zip, and this addon does
not redistribute them.

| Package | Version | License | Project |
|---|---|---|---|
| PySide6 | 6.8.3 (pinned) | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | https://pyside.org |
| shiboken6 | 6.8.3 (pinned) | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only | https://pyside.org |
| qtpy | latest at install time (2.4.3 when checked) | MIT | https://github.com/spyder-ide/qtpy |
| blender-qt-stylesheet | latest at install time (0.0.3 when checked) | MPL-2.0 | https://github.com/hannesdelbeke/blender-qt-stylesheet |

PySide6 and shiboken6 include the Qt libraries, which are also available
under the LGPL-3.0 and GPL licenses. Each package's own license text is
installed together with the package.

Licenses above were taken from each package's PyPI listing.
