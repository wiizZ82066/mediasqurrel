"""Move one verified archive to the OS trash, with no permanent-delete fallback."""
import sys


def recycle(folder):
    if sys.platform != 'win32':
        from send2trash import send2trash
        send2trash(str(folder))
        return

    import pythoncom
    from win32com.server.exception import COMException
    from win32com.server.policy import DesignatedWrapPolicy
    from win32com.shell import shell, shellcon

    class RecycleOnly(DesignatedWrapPolicy):
        _com_interfaces_ = [shell.IID_IFileOperationProgressSink]
        _public_methods_ = ['PreDeleteItem', 'PostDeleteItem']
        completed = False

        def __init__(self):
            self._wrap_(self)

        def PreDeleteItem(self, flags, item):
            if not flags & shellcon.TSF_DELETE_RECYCLE_IF_POSSIBLE:
                # Returning an HRESULT as a Python value doesn't veto deletion.
                raise COMException(scode=-2147467260)  # E_ABORT

        def PostDeleteItem(self, flags, item, result, recycled_item):
            self.completed = result >= 0 and recycled_item is not None

    pythoncom.CoInitialize()
    try:
        operation = pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None,
                                               pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IFileOperation)
        recycle_on_delete = 0x00080000  # FOFX_RECYCLEONDELETE
        operation.SetOperationFlags(recycle_on_delete | shellcon.FOF_NOERRORUI |
                                    shellcon.FOF_SILENT | shellcon.FOF_NOCONFIRMATION | shellcon.FOFX_EARLYFAILURE)
        sink = RecycleOnly()
        source = shell.SHCreateItemFromParsingName(str(folder), None, shell.IID_IShellItem)
        operation.DeleteItem(source, pythoncom.WrapObject(sink, shell.IID_IFileOperationProgressSink))
        operation.PerformOperations()
        if operation.GetAnyOperationsAborted() or not sink.completed:
            raise OSError('系统回收站未接收此目录')
    except pythoncom.com_error as error:
        raise OSError('系统回收站不可用，或文件正在使用') from error
    finally:
        pythoncom.CoUninitialize()
