"""Tray bitmap resources and menu refresh use synthetic images and fake Win32."""
import ctypes
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
from app import tray_windows as tray


class TrayBitmapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.assets = Path(self.temporary.name)
        (self.assets / 'tray').mkdir()
        Image.new('RGBA', (48, 48), (100, 150, 200, 128)).save(self.assets / 'tray' / 'folder.png')
        Image.new('RGBA', (48, 48), (10, 20, 30, 255)).save(self.assets / 'squirrel-tray.png')
        self.api = MagicMock()
        self.api.menu_size.return_value = (16, 16)
        self.api.create.side_effect = [0x100000001, 0x100000002, 0x100000003]

    def test_alpha_pixels_are_premultiplied_bgra(self):
        image = Image.new('RGBA', (1, 1), (100, 150, 200, 128))
        self.assertEqual(tray._premultiplied_bgra(image), bytes((100, 75, 50, 128)))
        image.putpixel((0, 0), (200, 150, 100, 0))
        self.assertEqual(tray._premultiplied_bgra(image), bytes(4))

    def test_bitmap_cache_tracks_size_and_releases_every_handle_once(self):
        cache = tray._MenuBitmaps(self.assets, self.api)
        first = cache.get('folder')
        self.assertEqual(first, 0x100000001)
        self.assertEqual(cache.get('folder'), first)
        self.api.create.assert_called_once()
        self.assertEqual(self.api.create.call_args.args[0].size, (16, 16))
        self.api.menu_size.return_value = (24, 24)
        second = cache.get('folder')
        brand = cache.get('brand')
        self.assertEqual((second, brand), (0x100000002, 0x100000003))
        cache.close()
        cache.close()
        self.assertCountEqual([call.args[0] for call in self.api.delete.call_args_list],
                              [first, second, brand])
        self.assertIsNone(cache.get('folder'))

    def test_unknown_missing_and_corrupt_assets_keep_text_menu_available(self):
        cache = tray._MenuBitmaps(self.assets, self.api)
        for key in (None, '../../private', 'unknown'):
            self.assertIsNone(cache.get(key))
        self.api.menu_size.assert_not_called()
        (self.assets / 'tray' / 'close.png').write_bytes(b'not an image')
        with self.assertLogs(tray._LOG, level='WARNING') as logged:
            self.assertIsNone(cache.get('library'))
            self.assertIsNone(cache.get('library'))
            self.assertIsNone(cache.get('close'))
        self.assertEqual(len(logged.records), 2)
        self.api.create.assert_not_called()
        cache.close()
        self.api.delete.assert_not_called()

    def test_gdi_failure_does_not_prevent_other_menu_actions(self):
        self.api.create.side_effect = OSError('simulated bitmap failure')
        cache = tray._MenuBitmaps(self.assets, self.api)
        with self.assertLogs(tray._LOG, level='WARNING'):
            self.assertIsNone(cache.get('folder'))
            self.assertIsNone(cache.get('folder'))
        self.api.create.assert_called_once()
        cache.close()
        self.api.delete.assert_not_called()

    def test_native_api_has_pointer_sized_handles_and_top_down_pixels(self):
        user32, gdi32 = MagicMock(), MagicMock()
        user32.GetSystemMetrics.return_value = 16
        memory = ctypes.create_string_buffer(4)
        captured = {}
        def create(_dc, pointer, _usage, bits, _section, _offset):
            info = ctypes.cast(pointer, ctypes.POINTER(tray._BitmapInfo)).contents
            captured['header'] = (info.bmiHeader.biSize, info.bmiHeader.biHeight,
                                  info.bmiHeader.biBitCount)
            ctypes.cast(bits, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(memory)
            return 0x100000001
        gdi32.CreateDIBSection.side_effect = create
        gdi32.DeleteObject.return_value = 1
        api = tray._BitmapApi(user32, gdi32)
        self.assertIs(gdi32.CreateDIBSection.restype, ctypes.c_void_p)
        self.assertEqual(gdi32.DeleteObject.argtypes, [ctypes.c_void_p])
        self.assertEqual(api.menu_size(), (16, 16))
        bitmap = api.create(Image.new('RGBA', (1, 1), (100, 150, 200, 128)))
        self.assertEqual(bitmap, 0x100000001)
        self.assertEqual(captured['header'], (40, -1, 32))
        self.assertEqual(memory.raw, bytes((100, 75, 50, 128)))
        api.delete(bitmap)
        gdi32.DeleteObject.assert_called_once_with(bitmap)

    def test_missing_dib_memory_releases_allocated_handle(self):
        user32, gdi32 = MagicMock(), MagicMock()
        gdi32.CreateDIBSection.return_value = 0x100000001
        gdi32.DeleteObject.return_value = 1
        api = tray._BitmapApi(user32, gdi32)
        with self.assertRaisesRegex(OSError, '创建'):
            api.create(Image.new('RGBA', (1, 1)))
        gdi32.DeleteObject.assert_called_once_with(0x100000001)


class TrayBackendTests(unittest.TestCase):
    def backend(self, events, failure=False):
        class FakeIcon:
            def __init__(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs

            def _create_menu_item(self, descriptor, callbacks):
                callbacks.append(descriptor)
                return SimpleNamespace(fMask=1, hbmpItem=None)

            def update_menu(self):
                events.append('refresh')

            def _on_notify(self, wparam, lparam):
                events.append(('notify', wparam, lparam))
                return 'shown'

            def _mainloop(self):
                try:
                    if failure:
                        raise RuntimeError('synthetic loop failure')
                    return 'stopped'
                finally:
                    events.append('destroy_menu')

        return tray._make_windows_icon_class(FakeIcon, SimpleNamespace(MIIM_BITMAP=128, WM_RBUTTONUP=517))

    def test_native_menu_adds_only_requested_bitmap(self):
        icon = self.backend([])('fixture')
        icon._menu_bitmaps = MagicMock()
        icon._menu_bitmaps.get.side_effect = [0x100000001, None]
        callbacks = []
        item = icon._create_menu_item(SimpleNamespace(icon_key='folder'), callbacks)
        self.assertEqual(item.fMask, 129)
        self.assertEqual(item.hbmpItem, 0x100000001)
        plain = icon._create_menu_item(SimpleNamespace(), callbacks)
        self.assertEqual(plain.fMask, 1)
        self.assertIsNone(plain.hbmpItem)
        self.assertEqual(len(callbacks), 2)

    def test_right_click_refreshes_before_native_popup_and_preserves_left_click(self):
        events = []
        icon = self.backend(events)('fixture')
        self.assertEqual(icon._on_notify(0, 517), 'shown')
        self.assertEqual(events, ['refresh', ('notify', 0, 517)])
        events.clear()
        self.assertEqual(icon._on_notify(0, 514), 'shown')
        self.assertEqual(events, [('notify', 0, 514)])

    def test_menu_is_destroyed_before_bitmaps_on_normal_or_failed_exit(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                events = []
                icon = self.backend(events, failure)('fixture')
                icon._menu_bitmaps = MagicMock()
                icon._menu_bitmaps.close.side_effect = lambda: events.append('delete_bitmaps')
                if failure:
                    with self.assertRaisesRegex(RuntimeError, 'loop failure'):
                        icon._mainloop()
                else:
                    self.assertEqual(icon._mainloop(), 'stopped')
                self.assertEqual(events, ['destroy_menu', 'delete_bitmaps'])

    def test_factory_uses_windows_adapter_or_other_platform_backend_lazily(self):
        native = MagicMock()
        with patch.dict(sys.modules, {'pystray': SimpleNamespace(Icon=native)}), \
                patch.object(tray.os, 'name', 'posix'), \
                patch.object(tray, '_windows_icon_class') as windows:
            self.assertIs(tray.create_icon('fixture', title='Media Squirrel'), native.return_value)
            native.assert_called_once_with('fixture', title='Media Squirrel')
            windows.assert_not_called()
        adapter = MagicMock()
        with patch.object(tray.os, 'name', 'nt'), \
                patch.object(tray, '_windows_icon_class', return_value=adapter):
            self.assertIs(tray.create_icon('fixture', title='Media Squirrel'), adapter.return_value)
            adapter.assert_called_once_with('fixture', title='Media Squirrel')

    @unittest.skipUnless(os.name == 'nt', 'Windows GDI smoke test only')
    def test_windows_gdi_creates_and_releases_bitmap_without_ui(self):
        api = tray._BitmapApi()
        bitmap = api.create(Image.new('RGBA', (16, 16), (20, 100, 200, 128)))
        self.assertTrue(bitmap)
        api.delete(bitmap)


if __name__ == '__main__':
    unittest.main()
