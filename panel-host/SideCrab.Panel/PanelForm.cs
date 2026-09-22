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
            core.NewWindowRequested += (_, e) => { e.Handled = true; _log.Write("new window refused: " + e.Uri); };
            core.NavigationCompleted += OnNavigationCompleted;
            core.ProcessFailed += OnProcessFailed;
            core.WebMessageReceived += OnWebMessageReceived;   // lane B: the settings bridge
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
               ".h{font-size:46px;font-weight:600;color:#6F94CC}.m{opacity:.7;font-size:22px}</style></head><body><div class=\"w\">" +
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
                _log.Write("web message refused: source " + e.Source);
                return;
            }
            using var doc = JsonDocument.Parse(e.WebMessageAsJson);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object) return;
            if (!root.TryGetProperty("type", out var type) || type.ValueKind != JsonValueKind.String) return;
            switch (type.GetString())
            {
                case "host-info": SendHostInfo(); return;
                case "settings": SaveSettingsFromPage(root); return;
                case "focus-session": FocusSessionFromPage(root); return;   // lane E
                default: _log.Write("web message ignored: type " + type.GetString()); return;
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

    /// <summary>What the sheet says about where a save goes. `hasToken` and never the
    /// token: the page has the code already (the host script injects it as a prop), and
    /// re-serving it here would be a second door to the same secret for no gain.</summary>
    private void SendHostInfo() => Post(new
    {
        type = "host-info",
        version = Program.Version,
        settingsPath = Path.Combine(_dir, "panel-settings.json"),
        hasToken = _settings.PanelToken is not null,
    });

    private void SaveSettingsFromPage(JsonElement root)
    {
        if (!root.TryGetProperty("props", out var props))
        {
            _log.Write("settings save ignored: no props");
            return;
        }
        var clean = PanelLogic.ValidateSettingsProps(props);
        if (clean.Count == 0)
        {
            // Nothing survived the whitelist, so nothing is written. A page that posts
            // rubbish must not be able to rewrite the operator's file at all.
            _log.Write("settings save ignored: nothing in it passed the whitelist");
            return;
        }
        var path = Path.Combine(_dir, "panel-settings.json");
        try
        {
            string? existing = File.Exists(path) ? File.ReadAllText(path) : null;
            var merged = PanelLogic.MergeSettingsJson(existing, clean);
            // Atomic: a half-written settings file is one the host reads as unparseable
            // and silently replaces with defaults on the next start, which would lose
            // the port and the display along with everything else. Same-directory temp,
            // so the move is a rename and not a copy across volumes.
            var tmp = path + ".tmp";
            Directory.CreateDirectory(_dir);
            File.WriteAllText(tmp, merged);
            File.Move(tmp, path, overwrite: true);
            _selfWriteAt = DateTime.UtcNow;
            _log.Write($"settings saved from the panel: {string.Join(", ", clean.Keys)}");
        }
        catch (Exception ex)
        {
            _log.Write("settings save failed: " + ex.GetType().Name + ": " + ex.Message);
            return;
        }

        _settings = PanelSettings.Load(_dir, _log.Write);
        ReinjectHostScript();
        // What was ACTUALLY stored, not what the page sent: the whitelist and the clamps
        // sit between the two, and the sheet repaints itself from this reply.
        Post(new { type = "settings-saved", props = clean });
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
    private void FocusSessionFromPage(JsonElement root)
    {
        var req = PanelLogic.ValidateFocusRequest(root);
        if (req is null)
        {
            _log.Write("focus ignored: no usable sessionId in the message");
            return;
        }

        var sw = System.Diagnostics.Stopwatch.StartNew();
        List<PanelLogic.WindowCandidate> candidates;
        try { candidates = WindowFocus.Candidates(Environment.ProcessId); }
        catch (Exception ex)
        {
            _log.Write("focus failed: window enumeration threw " + ex.GetType().Name);
            Post(new { type = "focus-result", sessionId = req.SessionId, ok = false, reason = "enumerate-failed" });
            return;
        }

        var choice = PanelLogic.SelectWindow(candidates, req);
        if (choice.Window is null)
        {
            _log.Write($"focus({req.SessionId}) '{req.Title}': {choice.Reason}; " +
                       $"{candidates.Count} candidate window(s) on the primary display in {sw.ElapsedMilliseconds} ms");
            Post(new { type = "focus-result", sessionId = req.SessionId, ok = false, reason = choice.Reason });
            return;
        }

        var target = choice.Window;
        var outcome = WindowFocus.Bring(target.Handle, Handle);
        // The panel's own window is re-read on every attempt, not assumed: WS_EX_NOACTIVATE
        // is the whole reason a tap on the glass does not steal the keyboard, and a focus
        // handover that ended with this window in front would be that guarantee broken.
        _log.Write($"focus({req.SessionId}) '{req.Title}' -> {target.ProcessName} '{target.Title}' " +
                   $"[{target.ClassName}] score {choice.Score}, {candidates.Count} candidates, " +
                   $"{(target.Minimised ? "restored, " : "")}{outcome.How}, " +
                   $"ok={outcome.Ok}, panel took focus={outcome.PanelTookFocus}, {sw.ElapsedMilliseconds} ms");
        Post(new
        {
            type = "focus-result",
            sessionId = req.SessionId,
            ok = outcome.Ok,
            // The FALLBACK travels to the page under its own name. A session with no
            // window of its own ends up in front of the Claude app, and the page must be
            // able to say that rather than claim the session's own window was found.
            reason = outcome.Ok ? (choice.Reason == "desktop-app" ? "desktop-app" : "focused") : "refused",
            window = target.Title,
        });
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
}
