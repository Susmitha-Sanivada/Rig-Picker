"""
safe_qt.py

Guards for Python overrides of Qt virtual methods (paintEvent, mousePressEvent,
eventFilter, ...).

Why this exists: when a Python override of a Qt virtual raises, shiboken
(PySide6 6.8 on Blender's bundled Python 3.11) has to catch the exception on
the C++ side and report it. In Blender that error-reporting path has been
seen to crash the whole application with an access violation inside
shiboken6.dll (Shiboken::Errors::storePythonOverrideErrorOrPrint) instead of
just printing a traceback. A bug in a handler therefore took Blender down
rather than being a harmless console message.

Wrapping each override so the exception is caught and printed *in Python*
means it never reaches shiboken's C++ error path.
"""

import functools
import traceback


def safe_override(default=None):
    """Decorator for a Qt virtual-method override. Any exception is printed
    and `default` is returned instead (None for ordinary event handlers,
    False for eventFilter, etc.)."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception:
                print(f"Rig Picker: error in Qt handler {fn.__qualname__}")
                traceback.print_exc()
                return default

        return wrapper

    return decorator
