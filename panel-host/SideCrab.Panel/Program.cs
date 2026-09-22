namespace SideCrab.Panel;

internal static class Program
{
    public const string Version = "0.5.1";

    [STAThread]
    private static int Main(string[] args)
    {
        var opts = HostOptions.Parse(args);
        var dir = opts.SideCrabDir ?? PanelSettings.SideCrabDir;
        if (opts.Error is not null)
        {
            // LO-012, measured 2026-09-22 by running it: this used to write the error to a
            // log file named by the options it had just refused to parse, and a failed
            // parse names NOTHING - so "--profile --windowed" appended a line to the
            // installed kiosk's live panel.log, and "--profile kiosk" created a stray
            // panel-kiosk.log beside it. A second writer on a running host's file is the
            // line loss SCA-031 is about, over a typo. The console is where the operator
            // who typed it is; a scheduled task passes no arguments at all
            // (Install-SideCrab.ps1), and --check is the diagnostic that does write nothing.
            HostConsole.WriteLine("args: " + opts.Error);
            return 2;
        }
        // Before the log and before the mutex: --check must not append to a running host's
        // log file, and must not be refused by its single-instance guard.
        if (opts.Check) return HostCheck.Run(opts, dir);

        // SCA-031: per-instance file. The kiosk keeps the bare panel.log name the setup
        // lane's smoke check reads.
        var log = new Log(Path.Combine(dir, "logs", PanelLogic.LogFileName(opts.Windowed, opts.Profile)));

        // LO-003. Only for a directory the operator NAMED. Log.Write swallows a write it
        // cannot make, by design - a full disk must never take the panel down - so a
        // --sidecrab-dir pointing at a path that cannot be created gave a host that ran
        // with no log at all, no line anywhere saying so, and exit 0. The default
        // ~/.sidecrab failing is a bad night and keeps the tolerant path; a named one that
        // cannot be made is a typo, and it is answered before anything else starts.
        if (opts.SideCrabDir is not null &&
            !HostCheck.DirectoryIsUsable(Path.Combine(dir, "logs"), out var dirError))
        {
            HostConsole.WriteLine($"--sidecrab-dir {dir} cannot be used: {dirError}");
            return 2;
        }

        // One panel per session. A second instance exits 3 rather than stacking a second
        // topmost window on the Edge; the scheduled task's IgnoreNew policy is the other
        // half of the same guard.
        //
        // lane E: the name depends on the MODE, and the two must never share one. A
        // --windowed dev host has no kiosk window and pins to nothing, so it cannot cause
        // the harm this guard exists for - but while both used one name, starting a dev
        // host on a PC with the pinned one running exited 3 and measuring anything from
        // the worktree was impossible without stopping the operator's panel. The scheduled
        // task NEVER passes --windowed (see Install-SideCrab.ps1), so the pinned instance
        // always takes the bare name and the guard on the Edge is untouched.
        using var mutex = new Mutex(initiallyOwned: true, name: PanelLogic.MutexName(opts.Windowed, opts.Profile), out var first);
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

        var startedAt = DateTime.Now;
        log.Write($"SideCrab.Panel {Version} starting; pid {Environment.ProcessId}; settings dir {dir}; " +
                  $"profile {PanelLogic.ProfileName(opts.Windowed, opts.Profile)}; " +
                  $"args: {PanelLogic.EscapeForLog(string.Join(' ', args), 300)}");
        Application.Run(new PanelForm(opts, dir, log, startedAt));
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
    /// <summary>SCA-024: names the WebView2 user-data folder and the log file, so a
    /// second host can be run for native QA without sharing either with the kiosk.</summary>
    public string? Profile { get; private set; }
    /// <summary>lane O: print what this host would do and exit, showing no window.</summary>
    public bool Check { get; private set; }
    public string? Error { get; private set; }

    public static HostOptions Parse(string[] args)
    {
        var o = new HostOptions();
        for (var i = 0; i < args.Length; i++)
        {
            // LO-004. A value that is itself a switch is the value the operator LEFT OUT.
            // "--profile --windowed" took the flag as the name: SafeProfileName reduced it
            // to "windowed", the --windowed switch was consumed and gone, and what started
            // was a KIOSK - a topmost full-screen window aimed at the Edge - writing to the
            // windowed host's log file.
            string? Next()
            {
                if (i + 1 >= args.Length) return null;
                var v = args[i + 1];
                if (v.StartsWith("--", StringComparison.Ordinal)) return null;
                i++;
                return v;
            }
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
                    var given = Next();
                    if (string.IsNullOrWhiteSpace(given)) o.Error = "--sidecrab-dir needs a path";
                    // Absolute from here on. A relative path resolves against the CURRENT
                    // directory, which for the scheduled task is system32, so the settings
                    // and the log would land somewhere nobody would think to look.
                    else
                    {
                        try { o.SideCrabDir = Path.GetFullPath(given); }
                        catch (Exception ex) { o.Error = "--sidecrab-dir is not a usable path (" + ex.GetType().Name + ")"; }
                    }
                    break;
                case "--profile":
                    o.Profile = Next();
                    if (string.IsNullOrWhiteSpace(o.Profile)) o.Error = "--profile needs a name";
                    else if (PanelLogic.IsReservedProfile(o.Profile))
                        o.Error = $"--profile {o.Profile} is reserved: it resolves to the unnamed host's " +
                                  "WebView2 folder or log file. Pick another name.";
                    break;
                case "--check":
                case "--doctor":
                    o.Check = true;
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
