namespace SideCrab.Panel;

internal static class Program
{
    public const string Version = "0.1.0";

    [STAThread]
    private static int Main(string[] args)
    {
        var opts = HostOptions.Parse(args);
        var dir = opts.SideCrabDir ?? PanelSettings.SideCrabDir;
        var log = new Log(Path.Combine(dir, "logs", "panel.log"));
        if (opts.Error is not null)
        {
            log.Write("args: " + opts.Error);
            return 2;
        }

        // One panel per session. A second instance exits 3 rather than stacking a second
        // topmost window on the Edge; the scheduled task's IgnoreNew policy is the other
        // half of the same guard.
        using var mutex = new Mutex(initiallyOwned: true, name: @"Local\SideCrab.Panel", out var first);
        if (!first)
        {
            log.Write("another SideCrab.Panel is already running in this session; exiting 3");
            return 3;
        }

        ApplicationConfiguration.Initialize();
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
        Application.ThreadException += (_, e) =>
        {
            // Exit non-zero so the scheduled task's restart-on-failure relaunches a clean
            // process; a swallowed exception would leave a window that looks alive and is not.
            log.Write("unhandled: " + e.Exception);
            Environment.Exit(1);
        };
        AppDomain.CurrentDomain.UnhandledException += (_, e) =>
            log.Write("unhandled (domain): " + e.ExceptionObject);

        log.Write($"SideCrab.Panel {Version} starting; pid {Environment.ProcessId}; settings dir {dir}; " +
                  $"args: {string.Join(' ', args)}");
        Application.Run(new PanelForm(opts, dir, log));
        log.Write("exit 0");
        return 0;
    }
}

/// <summary>Command-line switches. All optional; the settings file carries the same facts
/// for the scheduled task, so these exist for a desk-side check and for tests.</summary>
public sealed class HostOptions
{
    public int? Port { get; private set; }
    public string? DisplayDeviceId { get; private set; }
    public int DevToolsPort { get; private set; }
    public bool Windowed { get; private set; }
    public string? SideCrabDir { get; private set; }
    public string? Error { get; private set; }

    public static HostOptions Parse(string[] args)
    {
        var o = new HostOptions();
        for (var i = 0; i < args.Length; i++)
        {
            string? Next() => i + 1 < args.Length ? args[++i] : null;
            switch (args[i])
            {
                case "--port":
                    if (int.TryParse(Next(), out var p) && p is > 0 and < 65536) o.Port = p;
                    else o.Error = "--port needs a number 1..65535";
                    break;
                case "--display":
                    o.DisplayDeviceId = Next();
                    if (string.IsNullOrWhiteSpace(o.DisplayDeviceId)) o.Error = "--display needs a device id fragment";
                    break;
                case "--devtools-port":
                    // Chromium's remote-debugging port, loopback only, for measuring the
                    // page (viewport, console) from a script. Off unless asked for.
                    if (int.TryParse(Next(), out var d) && d is > 0 and < 65536) o.DevToolsPort = d;
                    else o.Error = "--devtools-port needs a number 1..65535";
                    break;
                case "--windowed":
                    // A normal resizable window on whatever monitor: for developing the
                    // host on a PC with no Edge. Never used by the scheduled task.
                    o.Windowed = true;
                    break;
                case "--sidecrab-dir":
                    o.SideCrabDir = Next();
                    if (string.IsNullOrWhiteSpace(o.SideCrabDir)) o.Error = "--sidecrab-dir needs a path";
                    break;
                default:
                    o.Error = "unknown argument " + args[i];
                    break;
            }
            if (o.Error is not null) break;
        }
        return o;
    }
}
