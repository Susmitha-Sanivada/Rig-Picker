"""Capture View photographs the 3D viewport the user last interacted with
(falling back to the first 3D viewport when that one is gone / unknown)."""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _stubs import AddonTestCase, PKG_NAME, PKG_DIR, _load  # noqa: E402


class FakeRegion:
    def __init__(self, type_="WINDOW"):
        self.type = type_


class FakeArea:
    _next = 1000

    def __init__(self, type_="VIEW_3D", regions=None):
        self.type = type_
        self.regions = regions if regions is not None else [
            FakeRegion("HEADER"), FakeRegion("WINDOW")]
        FakeArea._next += 16
        self._ptr = FakeArea._next

    def as_pointer(self):
        return self._ptr


class FakeWindow:
    _next = 5000

    def __init__(self, areas):
        self.screen = types.SimpleNamespace(areas=areas)
        FakeWindow._next += 16
        self._ptr = FakeWindow._next

    def as_pointer(self):
        return self._ptr


class CaptureViewportTests(AddonTestCase):
    def setUp(self):
        super().setUp()
        bpy = types.ModuleType("bpy")
        bpy.types = types.SimpleNamespace(Operator=object, Object=object)
        bpy.props = types.SimpleNamespace(
            BoolProperty=lambda **k: None, StringProperty=lambda **k: None,
            IntProperty=lambda **k: None, FloatProperty=lambda **k: None)
        bpy.app = types.SimpleNamespace(timers=mock.MagicMock())
        bpy.context = mock.MagicMock()
        bpy.ops = mock.MagicMock()
        sys.modules["bpy"] = bpy
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [PKG_DIR]
        sys.modules[PKG_NAME] = pkg
        self.bpy = bpy
        self.backend = _load(f"{PKG_NAME}.backend", "backend.py")

    def wm(self, *windows):
        return types.SimpleNamespace(windows=list(windows))

    def test_picks_last_interacted_viewport_not_first(self):
        a1, a2 = FakeArea(), FakeArea()
        w = FakeWindow([a1, a2])
        wm = self.wm(w)
        self.assertIs(self.backend.find_capture_viewport(wm)[1], a1)  # no history
        self.backend._LAST_VIEW3D = (w.as_pointer(), a2.as_pointer())
        self.assertIs(self.backend.find_capture_viewport(wm)[1], a2)

    def test_picks_viewport_in_a_second_window(self):
        a1, a2 = FakeArea(), FakeArea()
        w1, w2 = FakeWindow([a1]), FakeWindow([a2])
        self.backend._LAST_VIEW3D = (w2.as_pointer(), a2.as_pointer())
        got = self.backend.find_capture_viewport(self.wm(w1, w2))
        self.assertIs(got[0], w2)
        self.assertIs(got[1], a2)

    def test_falls_back_to_first_when_last_is_gone_or_not_3d(self):
        a1, a2 = FakeArea(), FakeArea()
        w = FakeWindow([a1, a2])
        self.backend._LAST_VIEW3D = (w.as_pointer(), a2.as_pointer())
        a2.type = "TIMELINE"            # user switched that editor
        self.assertIs(self.backend.find_capture_viewport(self.wm(w))[1], a1)
        w.screen.areas.remove(a2)       # or closed it
        self.backend._LAST_VIEW3D = (w.as_pointer(), a2.as_pointer())
        self.assertIs(self.backend.find_capture_viewport(self.wm(w))[1], a1)

    def test_no_3d_viewport_returns_none(self):
        w = FakeWindow([FakeArea("TIMELINE")])
        self.assertIsNone(self.backend.find_capture_viewport(self.wm(w)))

    def test_skips_viewport_without_window_region(self):
        broken = FakeArea(regions=[FakeRegion("HEADER")])
        good = FakeArea()
        w = FakeWindow([broken, good])
        self.assertIs(self.backend.find_capture_viewport(self.wm(w))[1], good)

    def test_tracker_records_3d_area_and_passes_event_through(self):
        a1, a2 = FakeArea(), FakeArea()
        w = FakeWindow([a1, a2])
        op = self.backend.RP_OT_TrackViewport()
        ctx = types.SimpleNamespace(window=w, area=a2)
        self.assertEqual(op.invoke(ctx, None), {'PASS_THROUGH'})
        self.assertEqual(self.backend._LAST_VIEW3D,
                         (w.as_pointer(), a2.as_pointer()))

    def test_tracker_ignores_other_editors_and_missing_context(self):
        a1 = FakeArea()
        w = FakeWindow([a1])
        op = self.backend.RP_OT_TrackViewport()
        op.invoke(types.SimpleNamespace(window=w, area=a1), None)
        before = self.backend._LAST_VIEW3D
        self.assertEqual(
            op.invoke(types.SimpleNamespace(
                window=w, area=FakeArea("TIMELINE")), None), {'PASS_THROUGH'})
        self.assertEqual(
            op.invoke(types.SimpleNamespace(window=None, area=None), None),
            {'PASS_THROUGH'})
        self.assertEqual(self.backend._LAST_VIEW3D, before)

    def test_capture_operator_uses_the_tracked_viewport(self):
        a1, a2 = FakeArea(), FakeArea()
        w = FakeWindow([a1, a2])
        self.bpy.context.window_manager = self.wm(w)
        self.backend._LAST_VIEW3D = (w.as_pointer(), a2.as_pointer())
        scene = types.SimpleNamespace(
            render=types.SimpleNamespace(filepath="/user/out"))
        op = self.backend.RP_OT_CaptureView()
        op.report = lambda *a, **k: None
        self.assertEqual(op.execute(types.SimpleNamespace(scene=scene)),
                         {'FINISHED'})
        kw = self.bpy.context.temp_override.call_args.kwargs
        self.assertIs(kw["area"], a2)
        self.assertIs(kw["window"], w)
        self.assertEqual(kw["region"].type, "WINDOW")
        self.bpy.ops.render.opengl.assert_called_once_with(write_still=True)
        self.assertEqual(scene.render.filepath, "/user/out")  # restored

    def test_capture_operator_cancels_without_any_viewport(self):
        w = FakeWindow([FakeArea("TIMELINE")])
        self.bpy.context.window_manager = self.wm(w)
        scene = types.SimpleNamespace(
            render=types.SimpleNamespace(filepath="/user/out"))
        op = self.backend.RP_OT_CaptureView()
        reports = []
        op.report = lambda *a, **k: reports.append(a)
        self.assertEqual(op.execute(types.SimpleNamespace(scene=scene)),
                         {'CANCELLED'})
        self.assertEqual(scene.render.filepath, "/user/out")
        self.assertTrue(reports)

    def test_file_load_forgets_the_tracked_viewport(self):
        self.backend._LAST_VIEW3D = (1, 2)
        self.backend.on_load_post()
        self.assertIsNone(self.backend._LAST_VIEW3D)

    def test_keymap_registers_and_removes_tracking_items(self):
        added = []

        class KM:
            def __init__(self, name):
                self.name = name
                self.keymap_items = self
                self.removed = []

            def new(self, idname, type_, value, **kw):
                item = types.SimpleNamespace(
                    idname=idname, type=type_, value=value, kw=kw,
                    properties=types.SimpleNamespace())
                added.append((self.name, item))
                return item

            def remove(self, item):
                self.removed.append(item)

        maps = {}
        kc = types.SimpleNamespace(keymaps=types.SimpleNamespace(
            new=lambda name, space_type: maps.setdefault(name, KM(name))))
        self.bpy.context.window_manager = types.SimpleNamespace(
            keyconfigs=types.SimpleNamespace(addon=kc))
        self.backend.register_picker_undo_keymap()
        track = [i for n, i in added if i.idname == "rp.track_viewport"]
        self.assertEqual({i.type for i in track}, {
            'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE', 'WHEELUPMOUSE',
            'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM'})
        self.assertTrue(all(i.kw.get("any") for i in track))
        self.assertTrue(all(n == "3D View" for n, i in added
                            if i.idname == "rp.track_viewport"))
        # undo guard still registered
        self.assertEqual(
            len([1 for n, i in added if i.idname == "rp.picker_undo_guard"]), 2)
        self.backend.unregister_picker_undo_keymap()
        self.assertEqual(len(maps["3D View"].removed), len(track))
        self.assertEqual(self.backend._PICKER_KEYMAPS, [])


if __name__ == "__main__":
    unittest.main()
