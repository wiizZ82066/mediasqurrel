// Source-run host: load the selected local CPython, with no packaged runtime.
using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

[assembly: AssemblyTitle("Media Squirrel")]
[assembly: AssemblyDescription("Media Squirrel local media service")]
[assembly: AssemblyProduct("Media Squirrel")]
[assembly: AssemblyVersion("1.0.0.0")]

internal static class MediaSquirrelHost
{
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr LoadLibraryExW(string name, IntPtr file, uint flags);
    [DllImport("kernel32.dll", CharSet = CharSet.Ansi)]
    private static extern IntPtr GetProcAddress(IntPtr module, string name);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    private delegate int PythonMain(int argc, IntPtr argv);

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length < 5 || args[0] != "--python" || args[2] != "--dll" || args[4] != "--")
            return 2;
        if (!Path.IsPathRooted(args[1]) || !File.Exists(args[1]) ||
            !Path.IsPathRooted(args[3]) || !File.Exists(args[3]))
            return 2;
        IntPtr argv = IntPtr.Zero;
        IntPtr[] values = new IntPtr[args.Length - 4];
        try
        {
            // Search the explicit DLL directory and Windows defaults, not CWD.
            IntPtr library = LoadLibraryExW(args[3], IntPtr.Zero, 0x00000100 | 0x00001000);
            if (library == IntPtr.Zero) return 126;
            IntPtr entry = GetProcAddress(library, "Py_Main");
            if (entry == IntPtr.Zero) return 127;
            values[0] = Marshal.StringToHGlobalUni(args[1]);
            for (int index = 5; index < args.Length; index++)
                values[index - 4] = Marshal.StringToHGlobalUni(args[index]);
            argv = Marshal.AllocHGlobal((values.Length + 1) * IntPtr.Size);
            for (int index = 0; index < values.Length; index++)
                Marshal.WriteIntPtr(argv, index * IntPtr.Size, values[index]);
            Marshal.WriteIntPtr(argv, values.Length * IntPtr.Size, IntPtr.Zero);
            // Bind the marker to this process; Python children must not inherit
            // a false claim that they themselves are the branded host.
            Environment.SetEnvironmentVariable("MS_BRANDED_HOST",
                System.Diagnostics.Process.GetCurrentProcess().Id.ToString());
            PythonMain main = (PythonMain)Marshal.GetDelegateForFunctionPointer(entry, typeof(PythonMain));
            return main(values.Length, argv);
        }
        finally
        {
            foreach (IntPtr value in values) if (value != IntPtr.Zero) Marshal.FreeHGlobal(value);
            if (argv != IntPtr.Zero) Marshal.FreeHGlobal(argv);
        }
    }
}
