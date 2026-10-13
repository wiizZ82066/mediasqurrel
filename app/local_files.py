"""Open a validated local folder or select a media file in Windows Explorer."""
import os
from pathlib import Path
import threading

from .security import MEDIA_EXTENSIONS

IS_WINDOWS = os.name == 'nt'
_open_with_lock = threading.Lock()


class OpenWithBusyError(RuntimeError):
    pass


def open_media(path, *, mode='default'):
    """Only launch an existing media file; never treat input as a command."""
    if mode not in ('default', 'choose'):
        raise ValueError('打开方式无效')
    target = Path(path).resolve()
    if target.suffix.lower() not in MEDIA_EXTENSIONS:
        raise ValueError('仅支持打开图片和视频文件')
    if not target.is_file():
        raise FileNotFoundError('媒体文件已移动，请更新媒体索引')
    if not IS_WINDOWS:
        raise NotImplementedError('使用本机应用打开媒体当前支持 Windows')
    if mode == 'default':
        os.startfile(str(target), 'open')
        return False
    if not _open_with_lock.acquire(blocking=False):
        raise OpenWithBusyError('已有“选择打开方式”窗口，请先完成或取消该窗口')
    try:
        return _open_with_dialog(target)
    finally:
        _open_with_lock.release()


def _open_with_dialog(target):
    """Native per-file chooser; OAIF_EXEC does not change default associations.

    https://learn.microsoft.com/windows/win32/api/shlobj_core/nf-shlobj_core-shopenwithdialog
    https://learn.microsoft.com/windows/win32/api/shlobj_core/ns-shlobj_core-openasinfo
    """
    import ctypes
    from ctypes import wintypes

    class OPENASINFO(ctypes.Structure):
        _fields_ = [('pcszFile', wintypes.LPCWSTR), ('pcszClass', wintypes.LPCWSTR),
                    ('oaifInFlags', wintypes.DWORD)]

    ole = ctypes.WinDLL('ole32')
    shell = ctypes.WinDLL('shell32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    shell.SHOpenWithDialog.argtypes = [wintypes.HWND, ctypes.POINTER(OPENASINFO)]
    shell.SHOpenWithDialog.restype = ctypes.c_long
    result_init = ole.CoInitializeEx(None, 2)
    if result_init not in (0, 1, -2147417850):  # RPC_E_CHANGED_MODE: preserve caller COM.
        raise OSError('无法初始化本机应用选择接口')
    try:
        options = OPENASINFO(str(target), None, 0x00000004)  # OAIF_EXEC only.
        result = shell.SHOpenWithDialog(None, ctypes.byref(options))
        unsigned = result & 0xffffffff
        if unsigned in (0x800704c7, 0x80004004):  # ERROR_CANCELLED / E_ABORT.
            return True
        if unsigned & 0x80000000:
            raise OSError('无法打开应用选择窗口')
        return False
    finally:
        # Balance our successful CoInitializeEx call, not caller-owned COM.
        if result_init in (0, 1):
            ole.CoUninitialize()


def reveal_path(path, *, select_file=False):
    target = Path(path)
    if not target.exists() or (not select_file and not target.is_dir()):
        raise FileNotFoundError('文件或目录已移动，请更新媒体索引')
    if not IS_WINDOWS:
        raise NotImplementedError('此功能当前支持 Windows 文件资源管理器')
    if not select_file:
        os.startfile(str(target), 'open')
        return
    # Pass a shell item rather than an Explorer command line: commas, quotes and
    # non-ASCII names must be treated as paths, never options or commands.
    import ctypes
    from ctypes import wintypes
    ole = ctypes.WinDLL('ole32')
    shell = ctypes.WinDLL('shell32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    shell.SHParseDisplayName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD)]
    shell.SHParseDisplayName.restype = ctypes.c_long
    shell.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
    shell.SHOpenFolderAndSelectItems.restype = ctypes.c_long
    result = ole.CoInitializeEx(None, 2)
    # A worker may already be initialized in another apartment. Preserve that
    # caller-owned initialization; balance only our S_OK/S_FALSE references.
    if result not in (0, 1, -2147417850):  # RPC_E_CHANGED_MODE
        raise OSError('无法初始化文件资源管理器接口')
    initialized = result in (0, 1)
    item = ctypes.c_void_p()
    try:
        attributes = wintypes.DWORD()
        if shell.SHParseDisplayName(str(target), None, ctypes.byref(item), 0, ctypes.byref(attributes)) < 0:
            raise OSError('无法定位此媒体文件')
        if shell.SHOpenFolderAndSelectItems(item, 0, None, 0) < 0:
            raise OSError('无法打开文件资源管理器')
    finally:
        if item:
            ole.CoTaskMemFree(item)
        if initialized:
            ole.CoUninitialize()
