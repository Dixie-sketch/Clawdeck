using System.Drawing;
using System.Net;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using Microsoft.Win32;

namespace SideCrab.Panel;

/// <summary>The kiosk window. Borderless, topmost, a tool window (no taskbar or Alt+Tab
/// entry) that never activates: WS_EX_NOACTIVATE plus MA_NOACTIVATE on every mouse or
/// touch press, the shape a vendor dashboard window takes on the Edge. It pins
/// itself to the target monitor on a 5 s timer and on every display, power and session
/// event, hides while that monitor is absent, and shows its own dark fallback page while
/// crabd is unreachable.</summary>
public sealed class PanelForm : Form
{
    private const int WS_EX_TOPMOST = 0x00000008;
    private const int WS_EX_TOOLWINDOW = 0x00000080;
    private const int WS_EX_NOACTIVATE = 0x08000000;
    private const int WM_MOUSEACTIVATE = 0x0021;
    private const int MA_NOACTIVATE = 3;
    private const int WM_DISPLAYCHANGE = 0x007E;
    private const int WM_DPICHANGED = 0x02E0;
    private static readonly IntPtr HWND_TOPMOST = new(-1);
    private const uint SWP_NOSIZE = 0x0001, SWP_NOMOVE = 0x0002, SWP_NOACTIVATE = 0x0010, SWP_NOOWNERZORDER = 0x0200;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);

    private readonly HostOptions _opts;
    private readonly string _dir;
    private readonly Log _log;
    private readonly DateTime _startedAt;
    private PanelSettings _settings;
    private Uri _panelUrl;
    private WebView2? _web;
    private DisplayInfo? _target;
    private string _targetReason = PanelLogic.DisplayReasonNone;
    private bool _allowVisible;
    private bool _displayMissingLogged;
    private DateTime? _hiddenLoggedAt;
    private bool _paused;
    private bool _pageFailed;
    private bool _showingFallback;
    private bool _reinitPending;
    private bool _servicesStarted;
    private int _viewportChecks;
    private int _unexpectedDocuments;
    private string? _lastFailure;
    private string? _hostScriptId;
    private FileSystemWatcher? _watcher;
    private TrayUi? _tray;
    private readonly PanelLogic.BridgeGate _bridge = new();
    private readonly System.Windows.Forms.Timer _repin = new() { Interval = 5000 };
    private readonly System.Windows.Forms.Timer _retry = new() { Interval = 5000 };
    private readonly System.Windows.Forms.Timer _viewport = new() { Interval = 700 };
    private readonly System.Windows.Forms.Timer _settingsDebounce = new() { Interval = 1000 };
    // SCA-002: the message loop's first tick, not the first VISIBLE OnLoad. A kiosk that
    // starts with its monitor absent is hidden by SetVisibleCore, OnLoad never fires, and
    // everything that was hooked there never started: the re-pin poll that would find the
    // monitor when it appears, the watcher that would see a settings edit, and the
    // WebView. The audit measured it - 6.5 s in, loaded=false, repinEnabled=false,
    // watcherCreated=false - with the task reporting Running the whole time.
    private readonly System.Windows.Forms.Timer _startup = new() { Interval = 1 };

    public PanelForm(HostOptions opts, string dir, Log log, DateTime startedAt)
    {
        _opts = opts;
        _dir = dir;
        _log = log;
        _startedAt = startedAt;
        _settings = PanelSettings.Load(dir, log.Write);
        _panelUrl = PanelLogic.PanelUrl(opts.Port ?? _settings.CrabdPort);

        Text = "SideCrab Panel";
        FormBorderStyle = FormBorderStyle.None;
        ShowInTaskbar = false;
        ControlBox = false;
        StartPosition = FormStartPosition.Manual;
        AutoScaleMode = AutoScaleMode.None;
        BackColor = Color.Black;
        if (opts.Windowed)
        {
            FormBorderStyle = FormBorderStyle.Sizable;
            ShowInTaskbar = true;
            Size = new Size(1280, 360);
            _allowVisible = true;
        }
        else
        {
            // Placed on the target BEFORE the first show, so the window never flashes on
            // the primary. A missing target starts hidden (SetVisibleCore) and shows when
            // the 5 s repin finds it.
            var displays = SafeEnumerate();
            var initial = PanelLogic.ChooseDisplay(displays, opts.DisplayDeviceId ?? _settings.DisplayDeviceId,
                                                   _settings.DisplayWidth, _settings.DisplayHeight);
            _log.Write($"displays: {string.Join(" | ", displays.Select(Describe))}");
            _targetReason = initial.Reason;
            if (initial.Display is not null)
            {
                _target = initial.Display;
                Bounds = initial.Display.Bounds;
                _allowVisible = true;
                _log.Write($"target ({initial.Reason}): {Describe(initial.Display)}");
            }
            else
            {
                _log.Write($"target display not found at startup ({initial.Reason}); " +
                           "starting hidden until the 5 s repin finds it");
            }
        }

        _repin.Tick += (_, _) => Repin("timer");
        _retry.Tick += async (_, _) => await RetryAsync();
        _viewport.Tick += async (_, _) => { _viewport.Stop(); await MeasureViewportAsync(); };
        _settingsDebounce.Tick += async (_, _) => { _settingsDebounce.Stop(); await ReloadSettingsAsync(); };
        _startup.Tick += (_, _) => { _startup.Stop(); BeginStartup(); };
        _startup.Start();
        SystemEvents.DisplaySettingsChanged += OnDisplaySettingsChanged;
        SystemEvents.PowerModeChanged += OnPowerModeChanged;
        SystemEvents.SessionSwitch += OnSessionSwitch;
    }

    // ------------------------------------------------------------------ startup

    /// <summary>What a test asks after calling <see cref="StartServices"/>. Public so the
    /// SCA-002 acceptance test can assert the poll and the watcher are running on a host
    /// whose target display is absent and whose window was therefore never shown.</summary>
    public bool ServicesStarted => _servicesStarted;
    public bool RepinRunning => _repin.Enabled;
    public bool WatcherRunning => _watcher?.EnableRaisingEvents == true;
    public bool AllowVisible => _allowVisible;
    public string TargetReason => _targetReason;

    private bool _webStarted;

    private void BeginStartup()
    {
        StartServices();
        StartUi();
        if (_webStarted) return;
        _webStarted = true;
        _ = InitWebViewAsync();
    }

    /// <summary>The poll and the watcher. Idempotent: the startup tick and OnLoad both
    /// call it, and on a host that does show a window they both arrive.</summary>
    public void StartServices()
    {
        if (_servicesStarted) return;
        _servicesStarted = true;
        // The handle FIRST, and on this thread. A hidden kiosk never creates one by
        // itself: SetVisibleCore refuses the show, and the only other line that touches
        // Handle is the SetWindowPos in Repin, which the absent-target path returns before
        // reaching. Control.InvokeRequired answers FALSE for a handleless control, so
        // SafeInvoke then ran the settings watcher's callback on the watcher's own
        // threadpool thread, _settingsDebounce created its native window there, and its
        // tick was posted to a thread with no message loop. Measured 2026-09-21 on a dev
        // host: MainWindowHandle 0, an edit to panel-settings.json, and no reload, ever.
        // The window is created, not shown; SetVisibleCore still holds it back.
        if (!IsHandleCreated) _ = Handle;
        Repin("startup");
        _repin.Start();
        StartWatcher();
    }

    private void StartUi()
    {
        if (_tray is not null) return;
        try
        {
            _tray = new TrayUi(this, _log);
        }
        catch (Exception ex)
        {
            // MF-003 is the reachable control, not a dependency of the panel: a tray that
            // will not create must not stop the glass from rendering.
            _log.Write("tray icon not created: " + ex.GetType().Name + ": " + ex.Message);
        }
    }

    // ------------------------------------------------------------------ window shape

    // Null-safe on purpose: Control's own constructor reads CreateParams (and WinForms may
    // read ShowWithoutActivation) BEFORE this class's constructor body has assigned _opts.
    // The first build crashed on exactly that line at every task start (0xC0000005 in the
    // task history, a NullReferenceException in panel.log).
    private bool Kiosk => _opts is null || !_opts.Windowed;

    protected override bool ShowWithoutActivation => Kiosk;

    protected override CreateParams CreateParams
    {
        get
        {
            var cp = base.CreateParams;
            if (Kiosk) cp.ExStyle |= WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE;
            return cp;
        }
    }

    protected override void SetVisibleCore(bool value) => base.SetVisibleCore(value && _allowVisible);

    protected override void WndProc(ref Message m)
    {
        switch (m.Msg)
        {
            case WM_MOUSEACTIVATE when Kiosk:
                // A tap must reach the page without making this the foreground window,
                // or every touch on the Edge steals focus from whatever the operator is
                // typing into.
                m.Result = (IntPtr)MA_NOACTIVATE;
                return;
            case WM_DISPLAYCHANGE:
                base.WndProc(ref m);
                SafeInvoke(() => Repin("WM_DISPLAYCHANGE"));
                return;
            case WM_DPICHANGED:
                base.WndProc(ref m);
                SafeInvoke(() => Repin("WM_DPICHANGED"));
                return;
        }
        base.WndProc(ref m);
    }

    protected override void OnLoad(EventArgs e)
    {
        base.OnLoad(e);
        // Kept as a second door onto the same idempotent call. The startup tick is the
        // one that fires on a hidden host; this one fires first when a window is shown.
        BeginStartup();
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        SystemEvents.DisplaySettingsChanged -= OnDisplaySettingsChanged;
        SystemEvents.PowerModeChanged -= OnPowerModeChanged;
        SystemEvents.SessionSwitch -= OnSessionSwitch;
        _watcher?.Dispose();
        _tray?.Dispose();
        base.OnFormClosed(e);
    }

    // ------------------------------------------------------------------ pinning

    private void OnDisplaySettingsChanged(object? s, EventArgs e) => SafeInvoke(() => Repin("DisplaySettingsChanged"));

    private void OnPowerModeChanged(object? s, PowerModeChangedEventArgs e)
    {
        if (e.Mode != PowerModes.Resume) return;
        SafeInvoke(async () =>
        {
            Repin("resume");
            if (_pageFailed) await RetryAsync();
        });
    }

    private void OnSessionSwitch(object? s, SessionSwitchEventArgs e)
    {
        if (e.Reason is SessionSwitchReason.SessionUnlock or SessionSwitchReason.ConsoleConnect
            or SessionSwitchReason.RemoteConnect or SessionSwitchReason.SessionLogon)
            SafeInvoke(() => Repin(e.Reason.ToString()));
    }

    private void SafeInvoke(Action a)
    {
        if (IsDisposed) return;
        try { if (InvokeRequired) BeginInvoke(a); else a(); }
        catch (ObjectDisposedException) { }
        catch (InvalidOperationException) { }
    }

    private void SafeInvoke(Func<Task> a) => SafeInvoke(() => { _ = a(); });

    private List<DisplayInfo> SafeEnumerate()
    {
        try { return Displays.Enumerate(); }
        catch (Exception ex)
        {
            _log.Write($"display enumeration failed: {ex.GetType().Name}: {ex.Message}");
            return new List<DisplayInfo>();
        }
    }

    private void Repin(string why)
    {
        if (_opts.Windowed || IsDisposed) return;
        if (_paused)
        {
            _allowVisible = false;
            if (Visible) Hide();
            return;
        }
        var displays = SafeEnumerate();
        var wanted = _opts.DisplayDeviceId ?? _settings.DisplayDeviceId;
        var choice = PanelLogic.ChooseDisplay(displays, wanted, _settings.DisplayWidth, _settings.DisplayHeight);
        _targetReason = choice.Reason;
        var target = choice.Display;
        if (target is null)
        {
            if (!_displayMissingLogged)
            {
                _log.Write($"repin({why}): target display not found ({choice.Reason}: id " +
                           $"'{PanelLogic.EscapeForLog(wanted, 80)}' or " +
                           $"{_settings.DisplayWidth}x{_settings.DisplayHeight}); displays: " +
                           string.Join(" | ", displays.Select(Describe)));
                _displayMissingLogged = true;
            }
            _allowVisible = false;
            if (Visible) Hide();
            // C7: the line the setup lane reads while the target is absent. At start and
            // then at most once a minute - the poll behind it runs every five seconds.
            var now = DateTime.Now;
            if (PanelLogic.ShouldLogHidden(_hiddenLoggedAt, now))
            {
                _hiddenLoggedAt = now;
                _log.Write(PanelLogic.HiddenLine(Environment.ProcessId, Iso(_startedAt)));
            }
            return;
        }
        _displayMissingLogged = false;
        _hiddenLoggedAt = null;
        _allowVisible = true;
        var moved = _target is null || _target.DeviceName != target.DeviceName
                    || _target.Bounds != target.Bounds || _target.Dpi != target.Dpi;
        _target = target;
        var wasHidden = !Visible;
        if (Bounds != target.Bounds) Bounds = target.Bounds;
        if (wasHidden) Show();
        // Re-assert topmost without moving: another topmost window (a vendor dashboard,
        // if it is still on) can stack above after a display or session event.
        SetWindowPos(Handle, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER);
        if (moved || wasHidden)
        {
            _log.Write($"repin({why}): {Describe(target)}; window now {Bounds.Width}x{Bounds.Height} at {Bounds.X},{Bounds.Y}");
            _viewportChecks = 0;
            _viewport.Start();
        }
    }

    private static string Describe(DisplayInfo d) =>
        $"{d.DeviceName} {d.Bounds.Width}x{d.Bounds.Height} at {d.Bounds.X},{d.Bounds.Y} {d.ScalePercent}%" +
        $"{(d.Primary ? " primary" : "")} [{PanelLogic.EscapeForLog(d.DeviceId, 160)}]";

    /// <summary>The one timestamp format the C7 log lines carry.</summary>
    private static string Iso(DateTime t) => t.ToString("yyyy-MM-ddTHH:mm:ss");

    // ------------------------------------------------------------------ the web view

    private string HostScript() => PanelLogic.HostScript(_settings.Props, _settings.PanelToken, Program.Version);

    private async Task InitWebViewAsync()
    {
        _reinitPending = false;
        try
        {
            var web = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.Black, TabStop = false };
            Controls.Add(web);
            _web = web;
            // SCA-024: per-profile, so a --windowed QA host asking for its own
            // --remote-debugging-port does not meet the kiosk's environment in the same
            // folder. CreateAsync answers that with COMException 0x8007139F and the host
            // retries forever against a profile it can never open.
            var profile = PanelLogic.ProfileName(_opts.Windowed, _opts.Profile);
            var userData = PanelLogic.WebViewUserDataDir(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), profile);
            var envOpts = new CoreWebView2EnvironmentOptions();
            // The command line wins; the settings file is how the scheduled-task instance
            // (which takes no arguments) opens the port for a desk-side measurement.
            var devtools = _opts.DevToolsPort > 0 ? _opts.DevToolsPort : _settings.DevToolsPort;
            var browserArgs = new List<string>();
            if (devtools > 0) browserArgs.Add($"--remote-debugging-port={devtools}");
            // lane B. The panel's chime is Web Audio with no user gesture anywhere near
            // it: the window never activates (WS_EX_NOACTIVATE) and the alert it answers
            // arrives while nobody is touching the glass, so Chromium's default policy
            // leaves the AudioContext suspended and the chime is silent with no error.
            browserArgs.Add("--autoplay-policy=no-user-gesture-required");
            envOpts.AdditionalBrowserArguments = string.Join(' ', browserArgs);
            var env = await CoreWebView2Environment.CreateAsync(null, userData, envOpts);
            await web.EnsureCoreWebView2Async(env);
            var core = web.CoreWebView2;
            var s = core.Settings;
            s.IsSwipeNavigationEnabled = false;      // a horizontal swipe is a card gesture, not Back
            s.IsPinchZoomEnabled = false;
            s.AreDefaultContextMenusEnabled = false; // a long press is pin/unpin, not a menu
            s.AreBrowserAcceleratorKeysEnabled = false;
            s.IsZoomControlEnabled = false;
            s.IsStatusBarEnabled = false;
            s.AreDefaultScriptDialogsEnabled = false;
            s.IsBuiltInErrorPageEnabled = false;     // the host's own fallback page instead
            s.IsGeneralAutofillEnabled = false;
            s.IsPasswordAutosaveEnabled = false;
            s.AreHostObjectsAllowed = false;
            // lane B. Host OBJECTS stay off - that is a live .NET surface reachable from
            // script. Web MESSAGES are a JSON channel with a Source this host checks and
            // a whitelist it validates against, which is what the settings sheet needs
            // and the smallest thing that works.
            s.IsWebMessageEnabled = true;
            s.AreDevToolsEnabled = devtools > 0;
            // Named in the UA so crabd's originsSeen shows which build is polling.
            try { s.UserAgent = s.UserAgent + " SideCrab.Panel/" + Program.Version; }
            catch (Exception ex) { _log.Write("user agent not set: " + ex.GetType().Name); }
            core.NavigationStarting += OnNavigationStarting;
            core.NewWindowRequested += (_, e) =>
            {
                e.Handled = true;
                _log.Write("new window refused: " + PanelLogic.EscapeForLog(e.Uri));
            };
            // SCA-022. Three refusals a kiosk owes, none of which the panel needs:
            // a child frame anywhere but the panel's own origin, any permission prompt
            // (there is no one at the glass to answer it, and a defaulted prompt is a
            // grant nobody made), and any download (a borderless window has no download
            // bar, so the file would arrive with no trace on the screen).
            core.FrameNavigationStarting += OnFrameNavigationStarting;
            core.PermissionRequested += (_, e) =>
            {
                e.State = CoreWebView2PermissionState.Deny;
                e.Handled = true;
                _log.Write($"permission denied: {e.PermissionKind} for {PanelLogic.EscapeForLog(e.Uri)}");
            };
            core.DownloadStarting += (_, e) =>
            {
                e.Cancel = true;
                _log.Write("download cancelled: " + PanelLogic.EscapeForLog(e.DownloadOperation.Uri));
            };
            core.NavigationCompleted += OnNavigationCompleted;
            core.ProcessFailed += OnProcessFailed;
            core.WebMessageReceived += OnWebMessageReceived;   // lane B: the settings bridge
            _hostScriptId = await core.AddScriptToExecuteOnDocumentCreatedAsync(HostScript());
            _log.Write($"webview2 {env.BrowserVersionString} ready; profile {profile}; user data {userData}; " +
                       $"props {_settings.Props.Count} ({_settings.Source}); " +
                       $"pairing code {(_settings.PanelToken is null ? "ABSENT" : "present")}; " +
                       $"devtools {(devtools > 0 ? "port " + devtools : "off")}");
            NavigateToPanel("startup");
        }
        catch (Exception ex)
        {
            // The WebView2 Runtime missing is the usual cause. Nothing to render, so log
            // it and keep retrying: the task is up, the log says why the glass is dark.
            _log.Write("webview2 init failed: " + ex);
            _lastFailure = "WebView2 would not start: " + ex.GetType().Name;
            _pageFailed = true;
            _retry.Start();
        }
    }

    private void NavigateToPanel(string why)
    {
        var core = _web?.CoreWebView2;
        if (core is null) return;
        _showingFallback = false;
        _log.Write($"navigate({why}): {_panelUrl}");
        core.Navigate(_panelUrl.ToString());
    }

    private void OnNavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs e)
    {
        if (PanelLogic.IsAllowedNavigation(e.Uri, _panelUrl)) return;
        e.Cancel = true;
        _log.Write("navigation refused: " + PanelLogic.EscapeForLog(e.Uri));
    }

    private void OnFrameNavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs e)
    {
        if (PanelLogic.IsAllowedFrameNavigation(e.Uri, _panelUrl)) return;
        e.Cancel = true;
        _log.Write("frame navigation refused: " + PanelLogic.EscapeForLog(e.Uri));
    }

    /// <summary>SCA-028: a navigation that SUCCEEDED is not the same fact as the panel
    /// being on the glass. The completion event carries no URI, so the document is read
    /// from CoreWebView2.Source. about:blank is the case that matters: the WebView starts
    /// there, and a later arrival at it - a cancelled navigation, a renderer rebuild -
    /// used to clear the failure flag and STOP the retry, leaving a black 2560x720 window
    /// with nothing scheduled to notice.
    ///
    /// The re-navigations are bounded. An unexpected document that keeps coming back is a
    /// loop this host cannot win, so after <see cref="UnexpectedDocumentMax"/> tries it
    /// shows the fallback page, which names the tray's Reload panel.</summary>
    private const int UnexpectedDocumentMax = 5;

    private void OnNavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs e)
    {
        if (_showingFallback) return;
        var http = e.HttpStatusCode;
        if (!e.IsSuccess || http >= 400)
        {
            var reason = e.IsSuccess ? $"HTTP {http}" : e.WebErrorStatus.ToString();
            _log.Write($"panel failed to load: {reason}");
            _lastFailure = reason;
            _pageFailed = true;
            ShowFallback(reason);
            _retry.Start();
            return;
        }

        var source = _web?.CoreWebView2?.Source;
        if (!PanelLogic.IsPanelDocument(source, _panelUrl))
        {
            _pageFailed = true;
            _unexpectedDocuments++;
            var what = PanelLogic.EscapeForLog(source, 120);
            if (_unexpectedDocuments > UnexpectedDocumentMax)
            {
                _lastFailure = $"the window is showing {what}, not the panel";
                _log.Write($"navigation succeeded to '{what}', not the panel, " +
                           $"{_unexpectedDocuments} times; showing the fallback page");
                ShowFallback($"the window is showing {what}");
                _retry.Stop();
                return;
            }
            _log.Write($"navigation succeeded to '{what}', not the panel; retrying " +
                       $"({_unexpectedDocuments}/{UnexpectedDocumentMax})");
            _retry.Start();
            return;
        }

        _pageFailed = false;
        _unexpectedDocuments = 0;
        _retry.Stop();
        _log.Write("panel loaded");
        _viewportChecks = 0;
        _viewport.Start();
    }

    private void OnProcessFailed(object? sender, CoreWebView2ProcessFailedEventArgs e)
    {
        _log.Write($"webview2 process failed: {e.ProcessFailedKind} reason {e.Reason} exit {e.ExitCode}");
        _lastFailure = $"WebView2 {e.ProcessFailedKind} ({e.Reason})";
        if (e.ProcessFailedKind is CoreWebView2ProcessFailedKind.BrowserProcessExited
            or CoreWebView2ProcessFailedKind.RenderProcessExited
            or CoreWebView2ProcessFailedKind.RenderProcessUnresponsive)
        {
            if (_reinitPending) return;
            _reinitPending = true;
            SafeInvoke(ReinitWebViewAsync);
        }
    }

    private async Task ReinitWebViewAsync()
    {
        try
        {
            if (_web is not null)
            {
                Controls.Remove(_web);
                _web.Dispose();
                _web = null;
            }
        }
        catch (Exception ex) { _log.Write("webview2 dispose failed: " + ex.GetType().Name); }
        _log.Write("webview2 re-created after a process failure");
        await InitWebViewAsync();
    }

    private async Task RetryAsync()
    {
        if (!_pageFailed) { _retry.Stop(); return; }
        if (_web?.CoreWebView2 is null)
        {
            if (_reinitPending) return;
            _reinitPending = true;
            await ReinitWebViewAsync();
            return;
        }
        NavigateToPanel("retry");
    }

    private void ShowFallback(string reason)
    {
        var core = _web?.CoreWebView2;
        if (core is null) return;
        _showingFallback = true;
        core.NavigateToString(FallbackHtml(_panelUrl.ToString(), reason));
    }

    /// <summary>What the glass shows while crabd is unreachable: dark, named, honest.
    /// Deliberately not the widget's own art - that page IS the thing that is missing.</summary>
    public static string FallbackHtml(string url, string reason)
    {
        var u = WebUtility.HtmlEncode(url);
        var r = WebUtility.HtmlEncode(reason);
        return "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>SideCrab</title>" +
               "<style>html,body{margin:0;height:100%;background:#0F0E0D;color:#EDE7DF;font:28px/1.4 'Segoe UI',system-ui,sans-serif}" +
               ".w{display:flex;height:100%;flex-direction:column;align-items:center;justify-content:center;gap:14px;text-align:center;padding:0 8vw}" +
               ".h{font-size:46px;font-weight:600;color:#6F94CC}.m{opacity:.7;font-size:22px}</style></head><body><div class=\"w\">" +
               "<div class=\"h\">SideCrab companion not reachable</div>" +
               $"<div>{u}: {r}</div>" +
               "<div class=\"m\">The panel retries every 5 seconds. Start or update the companion with " +
               "Update-SideCrab.ps1, or use Reload the panel in the SideCrab tray menu.</div>" +
               "</div></body></html>";
    }

    // ------------------------------------------------------------------ viewport

    private async Task MeasureViewportAsync()
    {
        var web = _web;
        var core = web?.CoreWebView2;
        if (web is null || core is null || _pageFailed || _showingFallback) return;
        try
        {
            var raw = await core.ExecuteScriptAsync(
                "JSON.stringify({w:window.innerWidth,h:window.innerHeight,dpr:window.devicePixelRatio})");
            // ExecuteScript returns the JSON encoding of the script's string result.
            var inner = JsonDocument.Parse(raw).RootElement.GetString();
            if (inner is null) return;
            using var doc = JsonDocument.Parse(inner);
            var w = doc.RootElement.GetProperty("w").GetInt32();
            var h = doc.RootElement.GetProperty("h").GetInt32();
            var dpr = doc.RootElement.GetProperty("dpr").GetDouble();
            var physical = _target?.Bounds.Width ?? Width;
            // C7: field order is the setup lane's smoke check. pid and started are what
            // stop yesterday's line passing for this run's.
            _log.Write(PanelLogic.ViewportLine(w, h, dpr, web.ZoomFactor, Width, Height,
                                               Environment.ProcessId, Iso(_startedAt)));
            // Within 2 css px is equal: 2560 / 1.5 rounds to 1707 logical px, which reads back
            // as 2561 at zoom 0.667, and chasing that last pixel would loop the correction.
            if (Math.Abs(w - physical) <= 2) return;
            var corrected = PanelLogic.CorrectedZoom(web.ZoomFactor, w, physical);
            if (Math.Abs(corrected - web.ZoomFactor) > 0.001 && _viewportChecks < 3)
            {
                _viewportChecks++;
                web.ZoomFactor = corrected;
                _log.Write($"zoom corrected to {corrected:0.###} so the viewport is {physical} css px wide");
                _viewport.Start();
            }
        }
        catch (Exception ex) { _log.Write("viewport measure failed: " + ex.GetType().Name); }
    }

    // ------------------------------------------------------- lane B: the settings bridge

    /// <summary>Set when THIS host wrote panel-settings.json, so the watcher's run does
    /// not reload the page under the operator's hand: the sheet has already applied the
    /// props live and a reload would throw it away.</summary>
    private DateTime _selfWriteAt = DateTime.MinValue;

    private void OnWebMessageReceived(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        try
        {
            // FIRST, before the body is even looked at: the message must have come from
            // the page this host loaded. WebMessageReceived fires for every frame,
            // including one the page was made to embed, and Source is the only thing
            // that says which. The navigation lock already refuses to NAVIGATE anywhere
            // else, so this is the second half of the same guarantee.
            if (!PanelLogic.IsAllowedNavigation(e.Source, _panelUrl))
            {
                _log.Write("web message refused: source " + PanelLogic.EscapeForLog(e.Source));
                return;
            }
            using var doc = JsonDocument.Parse(e.WebMessageAsJson);
            var root = doc.RootElement;
            var req = PanelLogic.ParseBridgeRequest(root);
            if (req is null) { _log.Write("web message ignored: no usable type"); return; }
            switch (req.Type)
            {
                case PanelLogic.ChannelHostInfo:
                    Accept(PanelLogic.ChannelHostInfo, req.RequestId);
                    SendHostInfo(req.RequestId);
                    return;
                case PanelLogic.ChannelSettings:
                    Accept(PanelLogic.ChannelSettings, req.RequestId);
                    SaveSettingsFromPage(root, req.RequestId);
                    return;
                case PanelLogic.ChannelFocus:                                // lane E
                    Accept(PanelLogic.ChannelFocus, req.RequestId);
                    FocusSessionFromPage(root, req.RequestId);
                    return;
                default:
                    // SCA-030: the type is a string the PAGE chose. Unescaped, a \n in it
                    // wrote a second, unprefixed line into the only account of what this
                    // host did.
                    _log.Write("web message ignored: type " + PanelLogic.EscapeForLog(req.Type, 80));
                    return;
            }
        }
        catch (Exception ex)
        {
            // A page cannot be allowed to kill the window with a malformed message.
            _log.Write("web message failed: " + ex.GetType().Name + ": " + ex.Message);
        }
    }

    private void Post(object payload)
    {
        var core = _web?.CoreWebView2;
        if (core is null) return;
        try { core.PostWebMessageAsJson(JsonSerializer.Serialize(payload)); }
        catch (Exception ex) { _log.Write("web message not posted: " + ex.GetType().Name); }
    }

    /// <summary>SCA-021 / C2. Records which attempt on a channel is the newest, so a
    /// reply that arrives after the operator has already asked again is LOGGED as
    /// superseded. Every accepted request still gets its own reply, carrying the id it
    /// answers; that id is what stops a late reply being read as the answer to the newer
    /// attempt.</summary>
    private void Accept(string channel, string? requestId) => _bridge.Accepted(channel, requestId);

    private void Reply(string channel, string? requestId, object payload)
    {
        if (!_bridge.IsCurrent(channel, requestId))
            _log.Write($"bridge: replying to a superseded {channel} request " +
                       $"({PanelLogic.EscapeForLog(requestId, PanelLogic.RequestIdMax)})");
        Post(payload);
    }

    /// <summary>What the sheet says about where a save goes, and what this host can do.
    /// `hasToken` and never the token: the page has the code already (the host script
    /// injects it as a prop), and re-serving it here would be a second door to the same
    /// secret for no gain.</summary>
    private void SendHostInfo(string? requestId) =>
        Reply(PanelLogic.ChannelHostInfo, requestId,
              PanelLogic.HostInfoReply(requestId, Program.Version, Environment.ProcessId, Iso(_startedAt),
                                       Path.Combine(_dir, "panel-settings.json"),
                                       _settings.PanelToken is not null));

    /// <summary>SCA-021: EXACTLY ONE terminal reply per accepted request, on every path.
    /// Four of them used to end in a log line and a return - no props, nothing past the
    /// whitelist, a write failure, and a throw - and the sheet sat on "saving" with no
    /// deadline. The audit held one open for sixty seconds with the bridge connected.
    ///
    /// The error strings are for a human reading the sheet; the log keeps the exception
    /// type and message.</summary>
    private void SaveSettingsFromPage(JsonElement root, string? requestId)
    {
        void Fail(string error)
        {
            _log.Write("settings save refused: " + error);
            Reply(PanelLogic.ChannelSettings, requestId, PanelLogic.SettingsReply(requestId, false, error));
        }

        if (!root.TryGetProperty("props", out var props)) { Fail("the message carried no props"); return; }
        var clean = PanelLogic.ValidateSettingsProps(props);
        if (clean.Count == 0)
        {
            // Nothing survived the whitelist, so nothing is written. A page that posts
            // rubbish must not be able to rewrite the operator's file at all.
            Fail("nothing in it passed the whitelist");
            return;
        }
        try
        {
            var path = Path.Combine(_dir, "panel-settings.json");
            string? existing = File.Exists(path) ? File.ReadAllText(path) : null;
            WriteSettingsAtomic(PanelLogic.MergeSettingsJson(existing, clean));
            _log.Write($"settings saved from the panel: {string.Join(", ", clean.Keys)}");
        }
        catch (Exception ex)
        {
            _log.Write("settings save failed: " + ex.GetType().Name + ": " + ex.Message);
            Reply(PanelLogic.ChannelSettings, requestId,
                  PanelLogic.SettingsReply(requestId, false, "the settings file could not be written"));
            return;
        }

        _settings = PanelSettings.Load(_dir, _log.Write);
        ReinjectHostScript();
        Reply(PanelLogic.ChannelSettings, requestId, PanelLogic.SettingsReply(requestId, true, null));
    }

    /// <summary>The one writer for panel-settings.json. Atomic: a half-written settings
    /// file is one the host reads as unparseable and silently replaces with defaults on
    /// the next start, which would lose the port and the display along with everything
    /// else. Same-directory temp, so the move is a rename and not a copy across volumes.
    ///
    /// Throws. Every caller owes the operator an answer about the failure.</summary>
    private void WriteSettingsAtomic(string json)
    {
        var path = Path.Combine(_dir, "panel-settings.json");
        var tmp = path + ".tmp";
        Directory.CreateDirectory(_dir);
        File.WriteAllText(tmp, json);
        File.Move(tmp, path, overwrite: true);
        _selfWriteAt = DateTime.UtcNow;
    }

    // ------------------------------------------- lane E: bring a session to the front

    /// <summary>The page asked for a session's window. Validate like a settings save
    /// (allowlisted keys, capped strings, nothing else read), rank the windows the host
    /// enumerated itself, hand over the foreground, log the attempt and its result, and
    /// answer the page either way.
    ///
    /// The page never names a window: it sends four facts about a session and this host
    /// decides. A handle from the page would be a window picker a visited page could aim
    /// anywhere on the desktop.</summary>
    private void FocusSessionFromPage(JsonElement root, string? requestId)
    {
        var req = PanelLogic.ValidateFocusRequest(root);
        if (req is null)
        {
            // SCA-021: a refused payload is a terminal answer too. This path used to be a
            // log line and a return, and the page's button stayed on "bringing it to the
            // front" until something else redrew it.
            _log.Write("focus refused: no usable sessionId in the message");
            Reply(PanelLogic.ChannelFocus, requestId,
                  PanelLogic.FocusReply(requestId, string.Empty, false, "invalid-request", null));
            return;
        }

        var sw = System.Diagnostics.Stopwatch.StartNew();
        List<PanelLogic.WindowCandidate> candidates;
        try { candidates = WindowFocus.Candidates(Environment.ProcessId); }
        catch (Exception ex)
        {
            _log.Write("focus failed: window enumeration threw " + ex.GetType().Name);
            Reply(PanelLogic.ChannelFocus, requestId,
                  PanelLogic.FocusReply(requestId, req.SessionId, false, "enumerate-failed", null));
            return;
        }

        var choice = PanelLogic.SelectWindow(candidates, req);
        if (choice.Window is null)
        {
            _log.Write($"focus({PanelLogic.EscapeForLog(req.SessionId, PanelLogic.FocusIdMax)}) " +
                       $"'{PanelLogic.EscapeForLog(req.Title)}': {choice.Reason}; " +
                       $"{candidates.Count} candidate window(s) on the primary display in {sw.ElapsedMilliseconds} ms");
            Reply(PanelLogic.ChannelFocus, requestId,
                  PanelLogic.FocusReply(requestId, req.SessionId, false, choice.Reason, null));
            return;
        }

        var target = choice.Window;
        var outcome = WindowFocus.Bring(target.Handle, Handle);
        // The panel's own window is re-read on every attempt, not assumed: WS_EX_NOACTIVATE
        // is the whole reason a tap on the glass does not steal the keyboard, and a focus
        // handover that ended with this window in front would be that guarantee broken.
        //
        // SCA-030: the window title came off the DESKTOP, not from this host. Any window
        // on the primary display can carry a newline in its title.
        _log.Write($"focus({PanelLogic.EscapeForLog(req.SessionId, PanelLogic.FocusIdMax)}) " +
                   $"'{PanelLogic.EscapeForLog(req.Title)}' -> {PanelLogic.EscapeForLog(target.ProcessName, 64)} " +
                   $"'{PanelLogic.EscapeForLog(target.Title)}' " +
                   $"[{PanelLogic.EscapeForLog(target.ClassName, 64)}] score {choice.Score}, " +
                   $"{candidates.Count} candidates, {(target.Minimised ? "restored, " : "")}{outcome.How}, " +
                   $"ok={outcome.Ok}, panel took focus={outcome.PanelTookFocus}, {sw.ElapsedMilliseconds} ms");
        // The FALLBACK travels to the page under its own name. A session with no window of
        // its own ends up in front of the Claude app, and the page must be able to say
        // that rather than claim the session's own window was found.
        var reason = outcome.Ok ? (choice.Reason == "desktop-app" ? "desktop-app" : "focused") : "refused";
        Reply(PanelLogic.ChannelFocus, requestId,
              PanelLogic.FocusReply(requestId, req.SessionId, outcome.Ok, reason, target.Title));
    }

    private async void ReinjectHostScript()
    {
        var core = _web?.CoreWebView2;
        if (core is null) return;
        try
        {
            if (_hostScriptId is not null) core.RemoveScriptToExecuteOnDocumentCreated(_hostScriptId);
            _hostScriptId = await core.AddScriptToExecuteOnDocumentCreatedAsync(HostScript());
        }
        catch (Exception ex) { _log.Write("host script not re-injected: " + ex.GetType().Name); }
    }

    // ------------------------------------------------------------------ settings watch

    private void StartWatcher()
    {
        try
        {
            Directory.CreateDirectory(_dir);
            _watcher = new FileSystemWatcher(_dir) { IncludeSubdirectories = false, EnableRaisingEvents = true };
            FileSystemEventHandler h = (_, e) =>
            {
                if (e.Name is "panel-settings.json" or "panel-token") SafeInvoke(() => _settingsDebounce.Start());
            };
            _watcher.Changed += h;
            _watcher.Created += h;
            _watcher.Renamed += (s, e) => h(s, e);
        }
        catch (Exception ex) { _log.Write("settings watcher not started: " + ex.GetType().Name); }
    }

    private async Task ReloadSettingsAsync()
    {
        _settings = PanelSettings.Load(_dir, _log.Write);
        var url = PanelLogic.PanelUrl(_opts.Port ?? _settings.CrabdPort);
        var core = _web?.CoreWebView2;
        _log.Write($"settings reloaded ({_settings.Source}); props {_settings.Props.Count}; " +
                   $"pairing code {(_settings.PanelToken is null ? "ABSENT" : "present")}");
        if (core is null) { _panelUrl = url; return; }
        try
        {
            if (_hostScriptId is not null) core.RemoveScriptToExecuteOnDocumentCreated(_hostScriptId);
            _hostScriptId = await core.AddScriptToExecuteOnDocumentCreatedAsync(HostScript());
            // lane B: our OWN save fired this watcher. Re-read and re-inject, so a later
            // navigation carries the new props, but do NOT reload: the page applied them
            // live the moment it got settings-saved, and a reload here would shut the
            // settings sheet under the operator's hand. Three seconds is the debounce
            // (1 s) plus room for a slow write, and it only ever suppresses a reload.
            var mine = DateTime.UtcNow - _selfWriteAt < TimeSpan.FromSeconds(3);
            if (url != _panelUrl) { _panelUrl = url; NavigateToPanel("port changed"); }
            else if (_pageFailed) NavigateToPanel("settings changed");
            else if (!mine) core.Reload();
            else _log.Write("settings change was our own save; the page already has it");
        }
        catch (Exception ex) { _log.Write("settings reload failed: " + ex.GetType().Name); }
        Repin("settings changed");
    }

    // ------------------------------------------- MF-003: what the tray menu can do

    /// <summary>The facts the status window shows. Read on every open, never cached: a
    /// status window that shows what was true when the tray was created is the control
    /// that reports success forever.</summary>
    public PanelLogic.HostStatus Status() => new(
        Mode: _opts.Windowed ? "windowed" : "kiosk",
        Version: Program.Version,
        Pid: Environment.ProcessId,
        StartedAt: _startedAt,
        Visible: Visible,
        Paused: _paused,
        // DisplayLabel and not Describe: Describe is the LOG form and doubles every
        // backslash, which is right in a log line and reads as a typo in a window.
        TargetLabel: _target is null ? null : PanelLogic.DisplayLabel(_target),
        TargetReason: _targetReason,
        PanelLoaded: !_pageFailed && !_showingFallback && _web?.CoreWebView2 is not null,
        LastFailure: _lastFailure,
        LogPath: _log.Path,
        PriorStartsInWindow: PriorStartsInLog());

    /// <summary>How many earlier starts this log records in the last hour. The scheduled
    /// task restarts a failed host three times, a minute apart, and then stops; without
    /// this the status window could only state the policy and not whether it was being
    /// used. Bounded: the last 64 KB of the file, nothing older.</summary>
    private int PriorStartsInLog()
    {
        try
        {
            var path = _log.Path;
            if (!File.Exists(path)) return 0;
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            const int window = 64 * 1024;
            if (fs.Length > window) fs.Seek(-window, SeekOrigin.End);
            using var sr = new StreamReader(fs);
            var cutoff = DateTime.Now.AddHours(-1);
            // By PID and not by timestamp. _startedAt is read a moment BEFORE the startup
            // line is written, so the two can straddle a second and this run's own line
            // would be counted as an earlier start: a status window that occasionally
            // invents a restart is worse than one that does not count them.
            var mine = $"pid {Environment.ProcessId};";
            var starts = 0;
            while (sr.ReadLine() is { } line)
            {
                if (!line.Contains("SideCrab.Panel", StringComparison.Ordinal)
                    || !line.Contains(" starting; pid ", StringComparison.Ordinal)) continue;
                if (line.Length < 19) continue;
                if (!DateTime.TryParse(line[..19], out var at) || at < cutoff) continue;
                if (line.Contains(mine, StringComparison.Ordinal)) continue;   // this run's own line
                starts++;
            }
            return starts;
        }
        catch (Exception ex)
        {
            _log.Write("restart history unreadable: " + ex.GetType().Name);
            return 0;
        }
    }

    public IReadOnlyList<DisplayInfo> CurrentDisplays() => SafeEnumerate();

    public string SettingsPath => Path.Combine(_dir, "panel-settings.json");

    public bool Paused => _paused;

    public void ReloadPanel()
    {
        _log.Write("tray: reload the panel");
        _unexpectedDocuments = 0;
        var core = _web?.CoreWebView2;
        if (core is null) { _ = ReinitWebViewAsync(); return; }
        NavigateToPanel("tray reload");
    }

    public void RepinNow() => Repin("tray");

    /// <summary>Hide until resumed. Distinct from Quit until next logon, and from
    /// disabling the scheduled task, which is Install-SideCrab.ps1's job and not this
    /// menu's: a paused panel is still running and comes back from this same menu.</summary>
    public void SetPaused(bool paused)
    {
        if (_paused == paused) return;
        _paused = paused;
        _log.Write(paused ? "tray: paused, hiding until resumed" : "tray: resumed");
        if (paused)
        {
            _allowVisible = false;
            if (Visible) Hide();
        }
        else
        {
            _displayMissingLogged = false;
            Repin("resumed");
        }
    }

    /// <summary>Exit 0 so the task's restart-on-failure does NOT relaunch. The logon
    /// trigger brings it back next time; the startup entry itself is untouched.</summary>
    public void QuitUntilLogon()
    {
        _log.Write("tray: quit until next logon (the scheduled task's logon trigger is unchanged)");
        Close();
    }

    public void OpenLogFolder()
    {
        var dir = Path.GetDirectoryName(_log.Path);
        if (dir is null) return;
        try
        {
            Directory.CreateDirectory(dir);
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
            {
                FileName = "explorer.exe",
                Arguments = $"/select,\"{_log.Path}\"",
                UseShellExecute = true,
            });
            _log.Write("tray: opened the log folder");
        }
        catch (Exception ex) { _log.Write("log folder not opened: " + ex.GetType().Name); }
    }

    // ------------------------------------------- MF-004: the display picker

    /// <summary>Write <c>display.deviceId</c> and re-pin. Returns the previous value so
    /// the caller can put it back; the caller owns the revert timer, because the window
    /// that asks "keep this display?" is on the primary and this class is on the Edge.
    ///
    /// Only the deviceId is written. Width and height stay whatever the file said, so the
    /// size fallback is still there the day the monitor's id changes.</summary>
    public string? ApplyDisplayDeviceId(string deviceId)
    {
        var previous = _settings.DisplayDeviceId;
        try
        {
            var path = SettingsPath;
            string? existing = File.Exists(path) ? File.ReadAllText(path) : null;
            WriteSettingsAtomic(PanelLogic.MergeDisplayDeviceIdJson(existing, deviceId));
        }
        catch (Exception ex)
        {
            _log.Write("display pick not saved: " + ex.GetType().Name + ": " + ex.Message);
            throw;
        }
        _settings = PanelSettings.Load(_dir, _log.Write);
        _displayMissingLogged = false;
        _log.Write($"tray: display set to '{PanelLogic.EscapeForLog(deviceId, 160)}' " +
                   $"(was '{PanelLogic.EscapeForLog(previous, 160)}')");
        Repin("display picked");
        return previous;
    }

    /// <summary>Undo an ApplyDisplayDeviceId. A null previous value means the file had no
    /// deviceId at all, and the default goes back in rather than an empty string: an
    /// empty id would make the size fallback the only route and that is not what was
    /// there before.</summary>
    public void RevertDisplayDeviceId(string? previous)
    {
        try
        {
            ApplyDisplayDeviceId(previous ?? PanelLogic.DefaultDisplayDeviceId);
            _log.Write("tray: display selection reverted");
        }
        catch (Exception ex) { _log.Write("display revert failed: " + ex.GetType().Name); }
    }
}
