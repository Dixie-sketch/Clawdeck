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
/// touch press, the same shape iCUE's own dashboard window uses on the Edge. It pins
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
    private PanelSettings _settings;
    private Uri _panelUrl;
    private WebView2? _web;
    private DisplayInfo? _target;
    private bool _allowVisible;
    private bool _displayMissingLogged;
    private bool _pageFailed;
    private bool _showingFallback;
    private bool _reinitPending;
    private int _viewportChecks;
    private string? _hostScriptId;
    private FileSystemWatcher? _watcher;
    private readonly System.Windows.Forms.Timer _repin = new() { Interval = 5000 };
    private readonly System.Windows.Forms.Timer _retry = new() { Interval = 5000 };
    private readonly System.Windows.Forms.Timer _viewport = new() { Interval = 700 };
    private readonly System.Windows.Forms.Timer _settingsDebounce = new() { Interval = 1000 };

    public PanelForm(HostOptions opts, string dir, Log log)
    {
        _opts = opts;
        _dir = dir;
        _log = log;
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
            var initial = PanelLogic.SelectDisplay(displays, opts.DisplayDeviceId ?? _settings.DisplayDeviceId,
                                                   _settings.DisplayWidth, _settings.DisplayHeight);
            _log.Write($"displays: {string.Join(" | ", displays.Select(Describe))}");
            if (initial is not null)
            {
                _target = initial;
                Bounds = initial.Bounds;
                _allowVisible = true;
                _log.Write($"target: {Describe(initial)}");
            }
            else
            {
                _log.Write("target display not found at startup; starting hidden until the 5 s repin finds it");
            }
        }

        _repin.Tick += (_, _) => Repin("timer");
        _retry.Tick += async (_, _) => await RetryAsync();
        _viewport.Tick += async (_, _) => { _viewport.Stop(); await MeasureViewportAsync(); };
        _settingsDebounce.Tick += async (_, _) => { _settingsDebounce.Stop(); await ReloadSettingsAsync(); };
        SystemEvents.DisplaySettingsChanged += OnDisplaySettingsChanged;
        SystemEvents.PowerModeChanged += OnPowerModeChanged;
        SystemEvents.SessionSwitch += OnSessionSwitch;
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

    protected override async void OnLoad(EventArgs e)
    {
        base.OnLoad(e);
        Repin("startup");
        _repin.Start();
        StartWatcher();
        await InitWebViewAsync();
    }

    protected override void OnFormClosed(FormClosedEventArgs e)
    {
        SystemEvents.DisplaySettingsChanged -= OnDisplaySettingsChanged;
        SystemEvents.PowerModeChanged -= OnPowerModeChanged;
        SystemEvents.SessionSwitch -= OnSessionSwitch;
        _watcher?.Dispose();
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
        var displays = SafeEnumerate();
        var wanted = _opts.DisplayDeviceId ?? _settings.DisplayDeviceId;
        var target = PanelLogic.SelectDisplay(displays, wanted, _settings.DisplayWidth, _settings.DisplayHeight);
        if (target is null)
        {
            if (!_displayMissingLogged)
            {
                _log.Write($"repin({why}): target display not found (id '{wanted}' or " +
                           $"{_settings.DisplayWidth}x{_settings.DisplayHeight}); displays: " +
                           string.Join(" | ", displays.Select(Describe)));
                _displayMissingLogged = true;
            }
            _allowVisible = false;
            if (Visible) { Hide(); _log.Write("hidden until the display returns"); }
            return;
        }
        _displayMissingLogged = false;
        _allowVisible = true;
        var moved = _target is null || _target.DeviceName != target.DeviceName
                    || _target.Bounds != target.Bounds || _target.Dpi != target.Dpi;
        _target = target;
        var wasHidden = !Visible;
        if (Bounds != target.Bounds) Bounds = target.Bounds;
        if (wasHidden) Show();
        // Re-assert topmost without moving: another topmost window (iCUE's own dashboard,
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
        $"{(d.Primary ? " primary" : "")} [{d.DeviceId}]";

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
            var userData = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                        "SideCrab", "Panel", "WebView2");
            var envOpts = new CoreWebView2EnvironmentOptions();
            // The command line wins; the settings file is how the scheduled-task instance
            // (which takes no arguments) opens the port for a desk-side measurement.
            var devtools = _opts.DevToolsPort > 0 ? _opts.DevToolsPort : _settings.DevToolsPort;
            if (devtools > 0)
                envOpts.AdditionalBrowserArguments = $"--remote-debugging-port={devtools}";
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
            s.IsWebMessageEnabled = false;
            s.AreDevToolsEnabled = devtools > 0;
            // Named in the UA so crabd's originsSeen shows which build is polling.
            try { s.UserAgent = s.UserAgent + " SideCrab.Panel/" + Program.Version; }
            catch (Exception ex) { _log.Write("user agent not set: " + ex.GetType().Name); }
            core.NavigationStarting += OnNavigationStarting;
            core.NewWindowRequested += (_, e) => { e.Handled = true; _log.Write("new window refused: " + e.Uri); };
            core.NavigationCompleted += OnNavigationCompleted;
            core.ProcessFailed += OnProcessFailed;
            _hostScriptId = await core.AddScriptToExecuteOnDocumentCreatedAsync(HostScript());
            _log.Write($"webview2 {env.BrowserVersionString} ready; user data {userData}; " +
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
        _log.Write("navigation refused: " + e.Uri);
    }

    private void OnNavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs e)
    {
        if (_showingFallback) return;
        var http = e.HttpStatusCode;
        if (!e.IsSuccess || http >= 400)
        {
            var reason = e.IsSuccess ? $"HTTP {http}" : e.WebErrorStatus.ToString();
            _log.Write($"panel failed to load: {reason}");
            _pageFailed = true;
            ShowFallback(reason);
            _retry.Start();
            return;
        }
        _pageFailed = false;
        _retry.Stop();
        _log.Write("panel loaded");
        _viewportChecks = 0;
        _viewport.Start();
    }

    private void OnProcessFailed(object? sender, CoreWebView2ProcessFailedEventArgs e)
    {
        _log.Write($"webview2 process failed: {e.ProcessFailedKind} reason {e.Reason} exit {e.ExitCode}");
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
               ".h{font-size:46px;font-weight:600;color:#BE7E6E}.m{opacity:.7;font-size:22px}</style></head><body><div class=\"w\">" +
               "<div class=\"h\">SideCrab companion not reachable</div>" +
               $"<div>{u}: {r}</div>" +
               "<div class=\"m\">The panel retries every 5 seconds. Start or update the companion with Update-SideCrab.ps1.</div>" +
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
            _log.Write($"viewport: {w}x{h} css px, dpr {dpr:0.###}, zoom {web.ZoomFactor:0.###}, window {Width}x{Height} physical");
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
            if (url != _panelUrl) { _panelUrl = url; NavigateToPanel("port changed"); }
            else if (_pageFailed) NavigateToPanel("settings changed");
            else core.Reload();
        }
        catch (Exception ex) { _log.Write("settings reload failed: " + ex.GetType().Name); }
        Repin("settings changed");
    }
}
