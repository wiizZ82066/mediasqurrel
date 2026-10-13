"""Open a validated local folder or select a media file in Windows Explorer."""
import os
from pathlib import Path


def reveal_path(path, *, select_file=False):
    target = Path(path)
    if not target.exists() or (not select_file and not target.is_dir()):
        raise FileNotFoundError('文件或目录已移动，请更新媒体索引')
    if os.name != 'nt':
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
