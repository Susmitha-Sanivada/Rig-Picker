import threading

import bpy

from . import backend
from . import dependency


class RP_OT_OpenPicker(bpy.types.Operator):
    bl_idname = "rigpicker.open"
    bl_label = "Open Rig Picker"

    # Shared across instances so a second click while an install is
    # already running (e.g. impatient double-click) can't start a
    # second background pip install into the same target folder.
    _installing = False

    # The actual background Thread object currently doing that install,
    # if any - shared at the class level (unlike self._thread below,
    # which is per-instance) so register() in __init__.py, which has no
    # operator instance to ask, can still tell whether a real thread is
    # genuinely still running. See _finish_installing() below for why
    # this matters.
    _active_thread = None

    _timer = None
    _thread = None
    _install_error = None
    _install_done = False

    @staticmethod
    def _show_unsaved_popup(context):
        from .custom_actions_quick import show_unsaved_file_popup
        show_unsaved_file_popup(context)

    def execute(self, context):

        # The picker's data lives in "<file name>.rigpicker" beside the
        # .blend, which can't exist for a file that has no name yet.
        if not bpy.data.filepath:
            self._show_unsaved_popup(context)
            return {'CANCELLED'}

        obj = context.object

        # Cache the armature directly if an armature is active
        if obj and obj.type == 'ARMATURE':
            backend._CACHED_ARM = obj
            backend._CACHED_ARM_NAME = obj.name

        try:
            needs_install = (
                not dependency.has_pyside()
                or not dependency.has_bqt()
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}

        if not needs_install:
            return self._open_picker_now(context)

        if RP_OT_OpenPicker._installing:
            self.report(
                {'WARNING'},
                "Rig Picker is already installing its dependencies - "
                "please wait for that to finish.",
            )
            return {'CANCELLED'}

        # First-time setup: PySide6/BQT aren't installed yet. That means
        # downloading and unpacking tens of MB via pip - doing that
        # synchronously here would freeze Blender's UI thread with no
        # feedback, indistinguishable from a hang. Run it on a
        # background thread instead and keep the user informed via the
        # status bar while a modal timer polls for completion.
        RP_OT_OpenPicker._installing = True
        # Class-level, not self.*: the worker thread below reports back
        # through these, and Blender can free this operator instance
        # (e.g. modal cancelled on file reload) while the thread is still
        # running - writing to a freed operator raises ReferenceError.
        RP_OT_OpenPicker._install_error = None
        RP_OT_OpenPicker._install_done = False

        self._thread = threading.Thread(
            target=self._install_worker,
            daemon=True,
        )
        RP_OT_OpenPicker._active_thread = self._thread
        self._thread.start()

        wm = context.window_manager
        self._timer = wm.event_timer_add(0.25, window=context.window)
        wm.modal_handler_add(self)

        self._set_status(
            context,
            "Rig Picker: installing Qt dependencies (first-time setup, "
            "this can take a minute)...",
        )

        return {'RUNNING_MODAL'}

    def _install_worker(self):
        try:
            dependency.ensure_qt_dependencies()
        except Exception as e:
            import traceback
            traceback.print_exc()
            RP_OT_OpenPicker._install_error = str(e)
        finally:
            RP_OT_OpenPicker._install_done = True

    def modal(self, context, event):

        if event.type != 'TIMER':
            return {'PASS_THROUGH'}

        if not RP_OT_OpenPicker._install_done:
            return {'RUNNING_MODAL'}

        self._stop_modal(context)

        # Unlike cancel()'s use of _finish_installing() below, this path
        # only runs once self._install_done is True - i.e. the worker's
        # own finally block has already run and the thread is finished
        # (or is finishing its very last statement) - so it's safe to
        # clear the guard unconditionally rather than checking is_alive().
        RP_OT_OpenPicker._installing = False
        RP_OT_OpenPicker._active_thread = None

        if RP_OT_OpenPicker._install_error:
            self.report(
                {'ERROR'},
                f"Rig Picker: failed to install Qt dependencies - "
                f"{RP_OT_OpenPicker._install_error}",
            )
            return {'CANCELLED'}

        return self._open_picker_now(context)

    def cancel(self, context):
        self._stop_modal(context)
        self._finish_installing()

    def _finish_installing(self):
        """Clears the shared _installing guard - but only once the
        background thread has actually finished, never just because
        this operator's own modal loop stopped. Blender can cancel a
        modal operator (e.g. on file reload) while the pip install it
        kicked off keeps running regardless - it's a daemon thread with
        no way to cleanly abort a subprocess mid-install. Clearing the
        guard while that thread is still alive would let a second click
        start a second, concurrent install into the same target folder,
        same as register() in __init__.py now also checks for."""
        thread = RP_OT_OpenPicker._active_thread
        if thread is not None and thread.is_alive():
            return
        RP_OT_OpenPicker._installing = False
        RP_OT_OpenPicker._active_thread = None

    def _stop_modal(self, context):
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        self._set_status(context, None)

    def _set_status(self, context, text):
        # Best-effort - not every context this operator can run from is
        # guaranteed to have a workspace status bar.
        try:
            context.workspace.status_text_set(text)
        except Exception:
            pass

    def _open_picker_now(self, context):

        try:
            # Import only when button is clicked
            from .ui.launcher import show_picker

            # Dependencies are already installed at this point (either
            # they always were, or the background install above just
            # finished) - this only imports PySide6 and spins up the
            # QApplication, both fast and both main-thread-only.
            dependency.ensure_qt()

            show_picker()

            self.report({'INFO'}, "Rig Picker Opened")

        except Exception as e:

            import traceback
            traceback.print_exc()

            self.report({'ERROR'}, str(e))

            return {'CANCELLED'}

        return {'FINISHED'}


class RP_PT_MainPanel(bpy.types.Panel):

    bl_label = "Rig Picker"
    bl_idname = "RP_PT_MAIN"

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Rig Picker"

    def draw(self, context):

        self.layout.operator(
            "rigpicker.open",
            icon='WINDOW'
        )