using System.Drawing;
using System.Runtime.InteropServices;

namespace SideCrab.Panel;

/// <summary>Monitor enumeration through user32/shcore. Every field the selector needs is
/// read here and nowhere else: the GDI name from EnumDisplayMonitors, the PnP device id
/// from EnumDisplayDevices (the interface-name form carries the EDID id), and the
/// per-monitor DPI.</summary>
public static class Displays
{
    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct MONITORINFOEX
    {
        public int cbSize;
        public RECT rcMonitor;
        public RECT rcWork;
        public uint dwFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string szDevice;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct DISPLAY_DEVICE
    {
        public int cb;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
        public uint StateFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
    }

    private delegate bool MonitorEnumProc(IntPtr hMonitor, IntPtr hdc, ref RECT rect, IntPtr data);

    [DllImport("user32.dll")]
    private static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc proc, IntPtr data);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFOEX info);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool EnumDisplayDevices(string? device, uint devNum, ref DISPLAY_DEVICE dd, uint flags);

    [DllImport("shcore.dll")]
    private static extern int GetDpiForMonitor(IntPtr hMonitor, int dpiType, out uint dpiX, out uint dpiY);

    private const uint MONITORINFOF_PRIMARY = 1;
    private const uint EDD_GET_DEVICE_INTERFACE_NAME = 1;

    public static List<DisplayInfo> Enumerate()
    {
        var list = new List<DisplayInfo>();
        EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, (IntPtr h, IntPtr dc, ref RECT r, IntPtr d) =>
        {
            var mi = new MONITORINFOEX { cbSize = Marshal.SizeOf<MONITORINFOEX>() };
            if (!GetMonitorInfo(h, ref mi)) return true;
            uint dpi = 96;
            if (GetDpiForMonitor(h, 0, out var dx, out _) == 0 && dx > 0) dpi = dx;
            var bounds = Rectangle.FromLTRB(mi.rcMonitor.Left, mi.rcMonitor.Top, mi.rcMonitor.Right, mi.rcMonitor.Bottom);
            list.Add(new DisplayInfo(mi.szDevice, DeviceIdFor(mi.szDevice), bounds,
                                     (mi.dwFlags & MONITORINFOF_PRIMARY) != 0, dpi));
            return true;
        }, IntPtr.Zero);
        return list;
    }

    /// <summary>The monitor's PnP interface name, e.g.
    /// <c>\\?\DISPLAY#CRXED00#5&amp;a4ae9a5&amp;0&amp;UID4358#{...}</c>. Empty when the
    /// adapter has no attached monitor (a virtual DISPLAYn).</summary>
    private static string DeviceIdFor(string gdiDeviceName)
    {
        var mon = new DISPLAY_DEVICE { cb = Marshal.SizeOf<DISPLAY_DEVICE>() };
        return EnumDisplayDevices(gdiDeviceName, 0, ref mon, EDD_GET_DEVICE_INTERFACE_NAME)
            ? mon.DeviceID ?? string.Empty
            : string.Empty;
    }
}
