using System.Drawing;
using System.Runtime.InteropServices;
using SideCrab.Panel;

namespace SideCrab.Panel.Tests;

/// <summary>lane O. The monitor list the host reads, under the test's control. Every
/// hardware event the 2026-09-21 audit could not reach on a PC with no Edge attached -
/// attach, detach, two identical monitors, a changed device id, a DPI change - is this
/// class plus a Repin.
///
/// Locked: the form reads it on its own message-loop thread while the test writes it.</summary>
internal sealed class DisplaySet
{
    private readonly object _lock = new();
    private List<DisplayInfo> _now = new();
    private int _reads;

    public DisplaySet(params DisplayInfo[] displays) => Set(displays);

    public void Set(params DisplayInfo[] displays)
    {
        lock (_lock) _now = displays.ToList();
    }

    public IReadOnlyList<DisplayInfo> Read()
    {
        lock (_lock)
        {
            _reads++;
            return _now.ToList();
        }
    }

    /// <summary>How many times the host has asked. A poll that stopped is the defect
    /// SCA-002 names, and this is the cheapest proof it is still running.</summary>
    public int Reads { get { lock (_lock) return _reads; } }
}

/// <summary>The monitors this estate's traps are about. The device id is the real shape
/// Windows returns from EnumDisplayDevices with the interface-name flag; the selector
/// matches on a SUBSTRING of it, so the shape matters more than the GUID.</summary>
internal static class FakeDisplay
{
    public static DisplayInfo Edge(string deviceName = @"\\.\DISPLAY2", int x = 0, int y = -8000,
                                   uint dpi = 96, string uid = "UID4358") =>
        new(deviceName,
            @"\\?\DISPLAY#CRXED00#5&a4ae9a5&0&" + uid + @"#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}",
            new Rectangle(x, y, 2560, 720), Primary: false, Dpi: dpi);

    /// <summary>The operator's own monitor: off the virtual desktop in these tests, so a
    /// window a test shows never lands on the desktop of whoever is running them.</summary>
    public static DisplayInfo Other(string deviceName = @"\\.\DISPLAY1", bool primary = true,
                                    int width = 2560, int height = 1600, uint dpi = 144) =>
        new(deviceName,
            @"\\?\DISPLAY#ACME1234#5&1234abc&0&UID256#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}",
            new Rectangle(-9000, -9000, width, height), primary, dpi);
}

/// <summary>lane O. A real PanelForm on a thread with a REAL message loop, so the five
/// second re-pin poll, the settings debounce and the WM_DISPLAYCHANGE path are the code
/// that runs rather than a method a test called by hand. The window is headless only in
/// the sense that the tray icon and the WebView are not started: the form, its handle, its
/// timers and its window messages are the shipping ones.
///
/// Every monitor in these tests is placed off the virtual desktop, so a window the harness
/// shows is never on the screen of whoever runs the suite.</summary>
internal sealed class HostHarness : IDisposable
{
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool PostMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);

    private const int WM_DISPLAYCHANGE = 0x007E;
    private const int WM_DPICHANGED = 0x02E0;

    private readonly Thread _thread;
    private readonly ManualResetEventSlim _ready = new();
    private PanelForm? _form;
    private Exception? _failed;

    public string Dir { get; }
    public Log Log { get; }
    public DisplaySet Displays { get; }
    public PanelForm Form => _form ?? throw new InvalidOperationException("the harness form is gone");

    private HostHarness(string dir, DisplaySet displays, string[] args)
    {
        Dir = dir;
        Displays = displays;
        Log = new Log(Path.Combine(dir, "logs", PanelLogic.LogFileName(windowed: false)));
        _thread = new Thread(() => Body(args)) { IsBackground = true };
        _thread.SetApartmentState(ApartmentState.STA);
    }

    public static HostHarness Start(string dir, DisplaySet displays, params string[] args)
    {
        var h = new HostHarness(dir, displays, args);
        h._thread.Start();
        if (!h._ready.Wait(TimeSpan.FromSeconds(30))) throw new TimeoutException("the harness host did not start");
        if (h._failed is not null) throw new InvalidOperationException("the harness host threw", h._failed);
        return h;
    }

    private void Body(string[] args)
    {
        try
        {
            var form = new PanelForm(HostOptions.Parse(args), Dir, Log, DateTime.Now, Displays.Read)
            {
                // No tray icon on the desktop of whoever is running the suite, and no
                // browser process: this harness is about the window, the timers and the
                // watcher.
                HeadlessForTests = true,
            };
            _form = form;
            form.StartServices();
        }
        catch (Exception ex) { _failed = ex; }
        finally { _ready.Set(); }
        if (_failed is not null) return;
        // An ApplicationContext with no main form: the loop must outlive a form that is
        // hidden the whole time, which is the state SCA-002 is about.
        Application.Run(new ApplicationContext());
    }

    public void Do(Action a)
    {
        var form = _form;
        if (form is null || form.IsDisposed) return;
        form.Invoke(a);
    }

    public T Get<T>(Func<T> f)
    {
        var form = _form ?? throw new InvalidOperationException("the harness form is gone");
        return (T)form.Invoke(f)!;
    }

    public bool WaitFor(Func<bool> predicate, string what, int seconds = 15)
    {
        var deadline = DateTime.UtcNow.AddSeconds(seconds);
        while (DateTime.UtcNow < deadline)
        {
            if (Get(predicate)) return true;
            Thread.Sleep(50);
        }
        return false;
    }

    public void Expect(Func<bool> predicate, string what, int seconds = 15)
    {
        if (!WaitFor(predicate, what, seconds))
            Assert.Fail($"{what} did not happen within {seconds}s. Log tail:\n{LogTail(12)}");
    }

    /// <summary>The real window message Windows sends when the display set changes, posted
    /// to the real window. The alternative - calling the handler - would prove the handler
    /// and not the WndProc case that dispatches it.</summary>
    public void PostDisplayChange() => PostMessage(Get(() => Form.Handle), WM_DISPLAYCHANGE, IntPtr.Zero, IntPtr.Zero);

    public void PostDpiChanged() => PostMessage(Get(() => Form.Handle), WM_DPICHANGED, IntPtr.Zero, IntPtr.Zero);

    public string ReadLog()
    {
        for (var attempt = 0; attempt < 20; attempt++)
        {
            if (!File.Exists(Log.Path)) return string.Empty;
            try
            {
                // ReadWrite sharing: the host opens and closes this file per line and a
                // plain File.ReadAllText loses the race often enough to matter.
                using var fs = new FileStream(Log.Path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                using var sr = new StreamReader(fs);
                return sr.ReadToEnd();
            }
            catch (IOException) { Thread.Sleep(25); }
        }
        return string.Empty;
    }

    public string LogTail(int lines) =>
        string.Join(Environment.NewLine,
                    ReadLog().Split('\n', StringSplitOptions.RemoveEmptyEntries).TakeLast(lines));

    public bool LogContains(string text) => ReadLog().Contains(text, StringComparison.Ordinal);

    /// <summary>The log is the host's own account of what it did, so for the paths whose
    /// only visible effect is a line (a re-pin reason, a re-armed watcher) it is the
    /// assertion rather than a field a test reaches into.</summary>
    public void ExpectLog(string text, int seconds = 12)
    {
        var deadline = DateTime.UtcNow.AddSeconds(seconds);
        while (DateTime.UtcNow < deadline)
        {
            if (LogContains(text)) return;
            Thread.Sleep(50);
        }
        Assert.Fail($"'{text}' was never written to the log within {seconds}s. Tail:\n{LogTail(15)}");
    }

    public void Dispose()
    {
        try
        {
            var form = _form;
            if (form is not null && !form.IsDisposed)
                form.Invoke(() =>
                {
                    form.Close();
                    form.Dispose();
                    Application.ExitThread();
                });
        }
        catch (InvalidOperationException) { }
        _thread.Join(TimeSpan.FromSeconds(10));
        _ready.Dispose();
    }

    /// <summary>What Windows will actually give a window that asks for this size. Every
    /// top-level window is capped at SM_CXMAXTRACK by SM_CYMAXTRACK - the PRIMARY monitor
    /// plus its sizing border - so an assertion on the bounds has to allow for it or it
    /// only passes on a PC whose primary is big enough (LO-011).</summary>
    public static Size AsWindowsAllows(Size wanted) =>
        new(Math.Min(wanted.Width, SystemInformation.MaxWindowTrackSize.Width),
            Math.Min(wanted.Height, SystemInformation.MaxWindowTrackSize.Height));

    /// <summary>A settings directory per test, removed afterwards.</summary>
    public static string Scratch(string? settingsJson = null)
    {
        var dir = Path.Combine(Path.GetTempPath(), "sidecrab-laneo-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        if (settingsJson is not null) File.WriteAllText(Path.Combine(dir, "panel-settings.json"), settingsJson);
        return dir;
    }

    public static void Cleanup(string dir)
    {
        for (var attempt = 0; attempt < 5; attempt++)
        {
            if (!Directory.Exists(dir)) return;
            try { Directory.Delete(dir, recursive: true); return; }
            catch (IOException) { Thread.Sleep(50); }
            catch (UnauthorizedAccessException) { Thread.Sleep(50); }
        }
    }
}
