using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace SideCrab.Panel;

/// <summary>lane E (host 0.3.0, provisional): the window half of "bring that session to
/// the front". Enumerating the desktop and handing over the foreground are the two parts
/// of it that cannot be pure; the ranking lives in PanelLogic and is tested there.
///
/// Nothing in this file synthesises input. It restores a window and asks Windows to make
/// it the foreground one; it never clicks, types or drives anything inside it.</summary>
public static class WindowFocus
{
    private delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumProc proc, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "GetWindowTextW")]
    private static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "GetClassNameW")]
    private static extern int GetClassName(IntPtr hWnd, StringBuilder text, int count);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);

    [DllImport("user32.dll")]
    private static extern bool GetWindowPlacement(IntPtr hWnd, ref WINDOWPLACEMENT wp);

    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromRect(ref RECT rect, uint flags);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, EntryPoint = "GetMonitorInfoW")]
    private static extern bool GetMonitorInfo(IntPtr monitor, ref MONITORINFO info);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int cmd);

    [DllImport("user32.dll")]
    private static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern void SwitchToThisWindow(IntPtr hWnd, bool altTab);

    [DllImport("user32.dll")]
    private static extern bool AllowSetForegroundWindow(int pid);

    [DllImport("user32.dll")]
    private static extern bool IsWindow(IntPtr hWnd);

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int Left, Top, Right, Bottom; }

    [StructLayout(LayoutKind.Sequential)]
    private struct POINT { public int X, Y; }

    [StructLayout(LayoutKind.Sequential)]
    private struct WINDOWPLACEMENT
    {
        public int length, flags, showCmd;
        public POINT ptMinPosition, ptMaxPosition;
        public RECT rcNormalPosition;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MONITORINFO { public int cbSize; public RECT rcMonitor, rcWork; public uint dwFlags; }

    private const int SW_RESTORE = 9;
    private const uint MONITOR_DEFAULTTONULL = 0;
    private const uint MONITORINFOF_PRIMARY = 1;
    private const int ASFW_ANY = -1;

    /// <summary>Every top-level window that COULD be brought forward: visible, titled, on
    /// the PRIMARY display, and not this process's own.
    ///
    /// The primary test reads rcNormalPosition from GetWindowPlacement and not the live
    /// rectangle, because a minimised window sits at -32000,-32000 and MonitorFromWindow
    /// answers with whichever monitor is nearest to that - on this PC, with only the
    /// primary attached, it answered "primary" for a minimised window that could equally
    /// have belonged to the Edge. The restored rectangle is where the window will be.
    ///
    /// DEFAULTTONULL and not DEFAULTTONEAREST: a window whose restored rectangle is off
    /// every monitor is dropped rather than snapped onto the primary.</summary>
    public static List<PanelLogic.WindowCandidate> Candidates(int excludePid)
    {
        var names = ProcessNames();
        var list = new List<PanelLogic.WindowCandidate>();
        EnumWindows((h, _) =>
        {
            if (!IsWindowVisible(h)) return true;
            var title = new StringBuilder(512);
            if (GetWindowText(h, title, title.Capacity) <= 0) return true;
            var text = title.ToString();
            if (string.IsNullOrWhiteSpace(text)) return true;
            GetWindowThreadProcessId(h, out var pid);
            if (pid == 0 || pid == (uint)excludePid) return true;
            if (!OnPrimary(h)) return true;
            var cls = new StringBuilder(256);
            GetClassName(h, cls, cls.Capacity);
            list.Add(new PanelLogic.WindowCandidate(
                h.ToInt64(),
                names.TryGetValue(pid, out var n) ? n : string.Empty,
                text, cls.ToString(), IsIconic(h)));
            return true;
        }, IntPtr.Zero);
        return list;
    }

    private static Dictionary<uint, string> ProcessNames()
    {
        // One sweep rather than a GetProcessById per window: EnumWindows returns ~200
        // windows on a working desktop and this runs on the UI thread.
        var map = new Dictionary<uint, string>();
        try
        {
            foreach (var p in Process.GetProcesses())
            {
                try { map[(uint)p.Id] = p.ProcessName; }
                catch (InvalidOperationException) { }
                finally { p.Dispose(); }
            }
        }
        catch (Exception) { /* an empty map only costs the process-name score */ }
        return map;
    }

    private static bool OnPrimary(IntPtr hWnd)
    {
        var wp = new WINDOWPLACEMENT { length = Marshal.SizeOf<WINDOWPLACEMENT>() };
        if (!GetWindowPlacement(hWnd, ref wp)) return false;
        var rect = wp.rcNormalPosition;
        if (rect.Right <= rect.Left || rect.Bottom <= rect.Top) return false;
        var mon = MonitorFromRect(ref rect, MONITOR_DEFAULTTONULL);
        if (mon == IntPtr.Zero) return false;
        var mi = new MONITORINFO { cbSize = Marshal.SizeOf<MONITORINFO>() };
        return GetMonitorInfo(mon, ref mi) && (mi.dwFlags & MONITORINFOF_PRIMARY) != 0;
    }

    /// <summary>What one focus attempt did. <paramref name="How"/> names the call that
    /// worked, which is the only way to tell a real handover from a taskbar flash.</summary>
    public sealed record FocusOutcome(bool Ok, string How, bool PanelTookFocus);

    /// <summary>Restore the window if it is minimised, then hand it the foreground.
    ///
    /// THE FOREGROUND LOCK. SetForegroundWindow succeeds only for a process that already
    /// owns the foreground or received the last input event; refused, it flashes the
    /// taskbar button and returns as if it worked, so every step below is verified by
    /// re-reading GetForegroundWindow rather than by its own return value.
    ///
    /// The panel window carries WS_EX_NOACTIVATE and answers MA_NOACTIVATE, so this host
    /// is usually NOT the foreground process when a tap arrives - which is why
    /// SwitchToThisWindow is here. It is the call Alt+Tab makes and it is not subject to
    /// the same lock.
    ///
    /// AttachThreadInput is deliberately NOT used, even though it is the better-known
    /// workaround. It joins this thread's input queue to the foreground application's, and
    /// a hung application on the other end hangs the panel with it: this is the one window
    /// in the estate that must never stop repainting. SwitchToThisWindow reaches the same
    /// place without the shared queue.
    ///
    /// <paramref name="ourWindow"/> is this host's own handle, re-read afterwards: a panel
    /// that took the keyboard while bringing something else forward is a defect, and it is
    /// recorded rather than assumed away.</summary>
    public static FocusOutcome Bring(long handle, IntPtr ourWindow)
    {
        var h = new IntPtr(handle);
        if (!IsWindow(h)) return new FocusOutcome(false, "gone", false);

        // Lets the target's process take the foreground from us, which is the other half
        // of the dance when the target answers by activating itself.
        try { AllowSetForegroundWindow(ASFW_ANY); } catch (EntryPointNotFoundException) { }

        if (IsIconic(h)) ShowWindow(h, SW_RESTORE);

        var how = "refused";
        if (SetForegroundWindow(h) && GetForegroundWindow() == h)
        {
            how = "SetForegroundWindow";
        }
        else
        {
            SwitchToThisWindow(h, true);
            if (GetForegroundWindow() == h) how = "SwitchToThisWindow";
            else
            {
                BringWindowToTop(h);
                if (GetForegroundWindow() == h) how = "BringWindowToTop";
            }
        }

        var fg = GetForegroundWindow();
        return new FocusOutcome(fg == h, how, fg == ourWindow);
    }
}
