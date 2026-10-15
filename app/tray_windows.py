"""Small Windows tray-menu extension; other hosts retain pystray's backend.

Windows draws the menu, including keyboard focus, scaling and system effects.
Only menu-item bitmaps and refresh-on-open are added to pystray's Win32 backend.
Imports do not initialize pystray, a display connection or Windows libraries.
"""
import ctypes
from functools import lru_cache
import logging
import os
from pathlib import Path
import threading


_ASSETS = Path(__file__).resolve().parent / 'assets'
_ICON_KEYS = frozenset(('download', 'library', 'tasks', 'subs', 'settings', 'folder', 'close'))
_LOG = logging.getLogger(__name__)


class _BitmapInfoHeader(ctypes.Structure):
    # Fixed-width Windows fields also let the resource tests run on Linux.
    _fields_ = [('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_int32),
                ('biHeight', ctypes.c_int32), ('biPlanes', ctypes.c_uint16),
                ('biBitCount', ctypes.c_uint16), ('biCompression', ctypes.c_uint32),
                ('biSizeImage', ctypes.c_uint32), ('biXPelsPerMeter', ctypes.c_int32),
                ('biYPelsPerMeter', ctypes.c_int32), ('biClrUsed', ctypes.c_uint32),
                ('biClrImportant', ctypes.c_uint32)]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [('bmiHeader', _BitmapInfoHeader), ('bmiColors', ctypes.c_uint32 * 1)]


def _premultiplied_bgra(image):
    # Windows alpha-blends menu bitmaps; unmultiplied RGB causes dark fringes.
    return image.convert('RGBA').convert('RGBa').tobytes('raw', 'BGRa')


class _BitmapApi:
    """Own only HBITMAPs, with explicit pointer-sized Win32 signatures."""

    def __init__(self, user32=None, gdi32=None):
        self.user32 = user32 if user32 is not None else ctypes.WinDLL('user32', use_last_error=True)
        self.gdi32 = gdi32 if gdi32 is not None else ctypes.WinDLL('gdi32', use_last_error=True)
        self.user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        self.user32.GetSystemMetrics.restype = ctypes.c_int
        self.gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BitmapInfo),
                                              ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p),
                                              ctypes.c_void_p, ctypes.c_uint32]
        self.gdi32.CreateDIBSection.restype = ctypes.c_void_p
        self.gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        self.gdi32.DeleteObject.restype = ctypes.c_int

    def menu_size(self):
        # SM_CXMENUCHECK / SM_CYMENUCHECK follow the native menu's metrics.
        return tuple(max(1, min(128, self.user32.GetSystemMetrics(index) or 16))
                     for index in (71, 72))

    def create(self, image):
        width, height = image.size
        pixels = _premultiplied_bgra(image)
        info = _BitmapInfo()
        info.bmiHeader = _BitmapInfoHeader(biSize=ctypes.sizeof(_BitmapInfoHeader),
                                         biWidth=width, biHeight=-height, biPlanes=1,
                                         biBitCount=32, biCompression=0,
                                         biSizeImage=len(pixels))
        bits = ctypes.c_void_p()
        bitmap = self.gdi32.CreateDIBSection(None, ctypes.byref(info), 0,
                                           ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            if bitmap:
                self.delete(bitmap)
            raise OSError('无法创建托盘菜单位图')
        try:
            ctypes.memmove(bits, pixels, len(pixels))
        except BaseException:
            self.delete(bitmap)
            raise
        return bitmap

    def delete(self, bitmap):
        if not self.gdi32.DeleteObject(bitmap):
            raise OSError('无法释放托盘菜单位图')


class _MenuBitmaps:
    """Cache a small allowlist of images until their last menu is destroyed."""

    def __init__(self, assets=_ASSETS, api=None):
        self.assets = assets
        self._api = api
        self._cache = {}
        self._failed = set()
        self._closed = False
        self._lock = threading.RLock()

    def get(self, key):
        if key != 'brand' and key not in _ICON_KEYS:
            return None
        with self._lock:
            if self._closed or key in self._failed:
                return None
            try:
                from PIL import Image
                if self._api is None:
                    self._api = _BitmapApi()
                size = self._api.menu_size()
                cache_key = (key, size)
                if cache_key not in self._cache:
                    path = (self.assets / 'squirrel-tray.png' if key == 'brand'
                            else self.assets / 'tray' / (key + '.png'))
                    with Image.open(path) as original:
                        image = original.convert('RGBA').resize(size, Image.Resampling.LANCZOS)
                    self._cache[cache_key] = self._api.create(image)
                return self._cache[cache_key]
            except Exception:
                # A missing/corrupt image or unsupported GDI API must not stop
                # the service or make the native text action unavailable.
                self._failed.add(key)
                _LOG.warning('托盘图标 %s 不可用，保留文字菜单', key)
                return None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for bitmap in set(self._cache.values()):
                try:
                    self._api.delete(bitmap)
                except Exception:
                    _LOG.warning('托盘菜单位图释放失败')
            self._cache.clear()


def _make_windows_icon_class(base, win32):
    """Keep the dependency on pystray's Windows internals in one adapter."""
    class WindowsIcon(base):
        def __init__(self, *args, **kwargs):
            self._menu_bitmaps = _MenuBitmaps()
            try:
                super().__init__(*args, **kwargs)
            except BaseException:
                self._menu_bitmaps.close()
                raise

        def _create_menu_item(self, descriptor, callbacks):
            item = super()._create_menu_item(descriptor, callbacks)
            bitmap = self._menu_bitmaps.get(getattr(descriptor, 'icon_key', None))
            if bitmap:
                item.fMask |= win32.MIIM_BITMAP
                item.hbmpItem = bitmap
            return item

        def _on_notify(self, wparam, lparam):
            if lparam == win32.WM_RBUTTONUP:
                # Rebuild on the tray's own message-loop thread before showing
                # the menu, never while TrackPopupMenuEx is using its handles.
                self.update_menu()
            return super()._on_notify(wparam, lparam)

        def _mainloop(self):
            try:
                return super()._mainloop()
            finally:
                # pystray destroys its HMENU in _mainloop's finally block.
                # Windows does not own/free the HBITMAPs attached to those items.
                self._menu_bitmaps.close()

    return WindowsIcon


@lru_cache(maxsize=1)
def _windows_icon_class():
    from pystray._win32 import Icon
    from pystray._util import win32
    return _make_windows_icon_class(Icon, win32)


def create_icon(*args, **kwargs):
    """Construct a pystray-compatible Icon without importing any backend early."""
    if os.name == 'nt':
        return _windows_icon_class()(*args, **kwargs)
    import pystray
    return pystray.Icon(*args, **kwargs)
