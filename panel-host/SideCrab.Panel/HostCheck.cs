using System.Runtime.InteropServices;
using Microsoft.Web.WebView2.Core;

namespace SideCrab.Panel;

/// <summary>lane O (host 0.5.0, provisional label): <c>--check</c>. What this host WOULD do,
/// printed, and nothing else. No window, no WebView2 environment, no mutex, and above all
/// no Log: the check runs while the installed host is running, and opening its log file
/// would be the second writer SCA-031 measured losing 3014 of 4000 lines.
///
/// One line per fact, "key: value", no colour and no table, because the setup lane's smoke
/// test parses it. Exit 0 when the panel would show, 2 when something named in the output
/// stops it.</summary>
internal static class HostCheck
{
    /// <summary>Everything the report is built from, so its wording and its exit code are
    /// pinned by tests instead of by whatever hardware this PC has today.</summary>
    public sealed record CheckInput(
        string Version, bool Windowed, string Profile, string SettingsDir, string SettingsPath,
        PanelSettings Settings, IReadOnlyList<string> SettingsWarnings,
        string? DisplayOverride, int? PortOverride, IReadOnlyList<DisplayInfo> Displays,
        string? WebViewVersion, string? WebViewError,
        string LogPath, string? LogError, bool AnotherHostRunning);

    public sealed record CheckReport(IReadOnlyList<string> Lines, int ExitCode);

    public static CheckReport Build(CheckInput i)
    {
        var lines = new List<string>();
        var problems = new List<string>();
        var settings = i.Settings;

        lines.Add($"sidecrab-panel-check: {i.Version}");
        lines.Add($"mode: {(i.Windowed ? "windowed" : "kiosk")}");
        lines.Add($"profile: {i.Profile}");
        lines.Add($"settings-dir: {i.SettingsDir}");
        lines.Add($"settings-file: {i.SettingsPath} ({(File.Exists(i.SettingsPath) ? "present" : "absent")})");
        lines.Add($"settings-source: {settings.Source}");
        foreach (var w in i.SettingsWarnings) lines.Add("settings-warning: " + PanelLogic.EscapeForLog(w, 300));
        if (settings.Source.Contains("unreadable", StringComparison.Ordinal))
            problems.Add("panel-settings.json could not be read and every value in it was defaulted");

        var port = i.PortOverride ?? settings.CrabdPort;
        lines.Add($"crabd-port: {port}{(i.PortOverride is null ? "" : " (--port)")}");
        lines.Add($"panel-url: {PanelLogic.PanelUrl(port)}");
        lines.Add($"pairing-code: {(settings.PanelToken is null ? "absent" : "present")}");
        lines.Add($"devtools-port: {(settings.DevToolsPort > 0 ? settings.DevToolsPort.ToString() : "off")}");

        var wanted = i.DisplayOverride ?? settings.DisplayDeviceId;
        lines.Add($"target-device-id: {(string.IsNullOrWhiteSpace(wanted) ? "(none)" : PanelLogic.EscapeForLog(wanted, 160))}" +
                  $"{(i.DisplayOverride is null ? "" : " (--display)")}");
        lines.Add($"target-size: {settings.DisplayWidth}x{settings.DisplayHeight}");
        lines.Add($"display-count: {i.Displays.Count}");
        foreach (var d in i.Displays) lines.Add("display: " + PanelLogic.DisplayLabel(d));

        // The real selector, not a second copy of its rules: a check that agreed with a
        // paraphrase and disagreed with the host would be worse than no check at all.
        var choice = PanelLogic.ChooseDisplay(i.Displays, wanted, settings.DisplayWidth, settings.DisplayHeight);
        if (i.Windowed)
        {
            lines.Add("pick: not applicable in windowed mode");
        }
        else if (choice.Display is not null)
        {
            lines.Add("pick: " + PanelLogic.DisplayLabel(choice.Display));
            lines.Add("pick-reason: " + choice.Reason);
        }
        else
        {
            lines.Add("pick: none");
            lines.Add($"pick-reason: {choice.Reason} ({PanelLogic.HiddenReason(choice.Reason)})");
            problems.Add("no display matched, so the panel would start hidden: " +
                         PanelLogic.HiddenReason(choice.Reason));
        }

        if (i.WebViewVersion is not null)
        {
            lines.Add("webview2: " + i.WebViewVersion);
        }
        else
        {
            lines.Add("webview2: not found (" + PanelLogic.EscapeForLog(i.WebViewError ?? "no runtime", 200) + ")");
            problems.Add("the WebView2 runtime is not installed, so the window would have nothing to render");
        }

        lines.Add("log: " + i.LogPath);
        lines.Add("log-writable: " + (i.LogError is null ? "yes" : "no"));
        if (i.LogError is not null)
        {
            lines.Add("log-error: " + PanelLogic.EscapeForLog(i.LogError, 200));
            problems.Add("the log directory cannot be written, so the host would run with no account of what it did");
        }

        lines.Add("another-host-running: " + (i.AnotherHostRunning ? "yes" : "no"));
        foreach (var p in problems) lines.Add("problem: " + p);
        lines.Add("result: " + (problems.Count == 0 ? "ok" : "problem"));
        return new CheckReport(lines, problems.Count == 0 ? 0 : 2);
    }

    public static int Run(HostOptions opts, string dir)
    {
        var warnings = new List<string>();
        var settings = PanelSettings.Load(dir, warnings.Add);
        var displays = new List<DisplayInfo>();
        try { displays.AddRange(Displays.Enumerate()); }
        catch (Exception ex) { warnings.Add("display enumeration failed: " + ex.GetType().Name + ": " + ex.Message); }

        string? version = null, webviewError = null;
        // Reads the registry and the install folder; it does not start a browser process,
        // which is the whole reason the check can run beside a live host.
        try { version = CoreWebView2Environment.GetAvailableBrowserVersionString(); }
        catch (Exception ex) { webviewError = ex.GetType().Name + ": " + ex.Message; }

        var logPath = Path.Combine(dir, "logs", PanelLogic.LogFileName(opts.Windowed, opts.Profile));
        LogIsWritable(logPath, out var logError);

        var running = false;
        try
        {
            running = Mutex.TryOpenExisting(PanelLogic.MutexName(opts.Windowed, opts.Profile), out var m);
            m?.Dispose();
        }
        catch (Exception) { /* a name this account may not open reads as "no"; it is not a fault of the check */ }

        var report = Build(new CheckInput(
            Program.Version, opts.Windowed, PanelLogic.ProfileName(opts.Windowed, opts.Profile),
            dir, Path.Combine(dir, "panel-settings.json"), settings, warnings,
            opts.DisplayDeviceId, opts.Port, displays, version, webviewError, logPath, logError, running));

        foreach (var line in report.Lines) HostConsole.WriteLine(line);
        return report.ExitCode;
    }

    /// <summary>Can this host append to the log it would append to? Opening the EXISTING
    /// file for append and closing it writes nothing, changes no timestamp and leaves the
    /// running host's own file exactly as it was, which matters because the usual way to
    /// run a check is on a PC where the installed host is live. Only when the file is not
    /// there yet does this fall back to making the directory and probing it.</summary>
    public static bool LogIsWritable(string logPath, out string? error)
    {
        if (File.Exists(logPath))
        {
            try
            {
                using var fs = new FileStream(logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
                error = null;
                return true;
            }
            catch (Exception ex)
            {
                error = ex.GetType().Name + ": " + ex.Message;
                return false;
            }
        }
        return DirectoryIsUsable(Path.GetDirectoryName(logPath) ?? logPath, out error);
    }

    /// <summary>Can this host write where it is about to write? A probe file and not an ACL
    /// reading: the answer that matters is what File.AppendAllText will do. The probe is
    /// removed whether it worked or not.</summary>
    public static bool DirectoryIsUsable(string dir, out string? error)
    {
        var probe = Path.Combine(dir, ".sidecrab-write-probe");
        try
        {
            Directory.CreateDirectory(dir);
            File.WriteAllText(probe, "probe");
            error = null;
            return true;
        }
        catch (Exception ex)
        {
            error = ex.GetType().Name + ": " + ex.Message;
            return false;
        }
        finally
        {
            try { if (File.Exists(probe)) File.Delete(probe); }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }
}

/// <summary>Standard output for a WinExe. The subsystem flag means Windows gives this
/// process no console of its own, so a bare run at a prompt has nowhere to print until it
/// borrows the caller's. A REDIRECTED stdout - which is what capturing the output in a
/// variable or sending it to a file both produce - is already a valid handle and must not
/// be replaced by the parent's console.</summary>
internal static class HostConsole
{
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AttachConsole(int processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr GetStdHandle(int which);

    private const int ATTACH_PARENT_PROCESS = -1;
    private const int STD_OUTPUT_HANDLE = -11;
    private static bool _ready;

    private static void Ensure()
    {
        if (_ready) return;
        _ready = true;
        // Before the first touch of Console: .NET binds Console.Out to whatever the handle
        // is at that moment, and attaching afterwards prints into a stream nobody reads.
        try
        {
            var h = GetStdHandle(STD_OUTPUT_HANDLE);
            if (h == IntPtr.Zero || h == new IntPtr(-1)) AttachConsole(ATTACH_PARENT_PROCESS);
        }
        catch (DllNotFoundException) { }
        catch (EntryPointNotFoundException) { }
    }

    public static void WriteLine(string line)
    {
        Ensure();
        try { Console.Out.WriteLine(line); Console.Out.Flush(); }
        catch (IOException) { /* no console and no redirection: the exit code is the answer */ }
    }
}
