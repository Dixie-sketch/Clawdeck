using System.Drawing;
using System.Text;
using System.Text.Json;

namespace SideCrab.Panel;

/// <summary>One monitor as Windows reports it: the GDI name, the PnP device id, bounds in
/// physical pixels, and the DPI the monitor is running at.</summary>
public sealed record DisplayInfo(string DeviceName, string DeviceId, Rectangle Bounds, bool Primary, uint Dpi)
{
    public int ScalePercent => (int)Math.Round(Dpi * 100.0 / 96.0);
}

/// <summary>The pure decisions of the panel host, kept free of Win32 and WebView2 so the
/// test project can break each one on purpose.</summary>
public static class PanelLogic
{
    public const int DefaultPort = 2722;
    public const string DefaultDisplayDeviceId = "CRXED00";   // the Xeneon Edge's PnP id
    public const int DefaultDisplayWidth = 2560;
    public const int DefaultDisplayHeight = 720;

    public static Uri PanelUrl(int port) => new($"http://127.0.0.1:{port}/panel/");

    /// <summary>The navigation lock. The window may show crabd's panel, the blank page it
    /// starts on, and the host's own fallback page (a data: document), and nothing else.
    /// A page script cannot start a top-level data: navigation in Chromium, so allowing the
    /// scheme admits only the host's NavigateToString. Everything else - another port, the
    /// localhost name, https, file:, javascript:, a path outside /panel/ - is cancelled.</summary>
    public static bool IsAllowedNavigation(string? uri, Uri panelUrl)
    {
        if (string.IsNullOrWhiteSpace(uri)) return false;
        if (uri == "about:blank") return true;
        if (uri.StartsWith("data:text/html", StringComparison.Ordinal)) return true;
        if (!Uri.TryCreate(uri, UriKind.Absolute, out var u)) return false;
        if (u.Scheme != Uri.UriSchemeHttp) return false;
        if (!string.Equals(u.Host, panelUrl.Host, StringComparison.Ordinal)) return false;
        if (u.Port != panelUrl.Port) return false;
        var path = u.AbsolutePath;
        return path == panelUrl.AbsolutePath
            || path.StartsWith(panelUrl.AbsolutePath, StringComparison.Ordinal);
    }

    /// <summary>A monitor and WHY it was chosen, or why there is not one. The reason is
    /// what the log and the tray's status window say; a bare null could not tell "no such
    /// monitor" from "two of them and I refused to guess".</summary>
    public sealed record DisplayChoice(DisplayInfo? Display, string Reason);

    public const string DisplayReasonId = "device-id";
    public const string DisplayReasonSize = "size";
    public const string DisplayReasonSizeAmbiguous = "size-ambiguous";
    public const string DisplayReasonSizePrimary = "size-would-be-primary";
    public const string DisplayReasonNone = "none";

    /// <summary>Which monitor is the Edge. By device id first (a substring of the PnP id
    /// Windows gives the monitor), then by exact physical size. NEVER by index and never
    /// the primary as a fallback: the wrong answer here is a full-screen topmost window
    /// over the operator's main display.
    ///
    /// SCA-014. The size fallback excludes the PRIMARY monitor outright and refuses a tie.
    /// A 2560x720 desktop monitor is an ordinary thing to own, and the audit reproduced
    /// exactly that: an unrelated primary named AUDIT_NOT_EDGE at 2560x720 was selected
    /// and would have been covered. An id match is an explicit instruction and is honoured
    /// whatever the monitor is, primary included - that is the operator naming a target,
    /// not the host guessing one.</summary>
    public static DisplayChoice ChooseDisplay(IReadOnlyList<DisplayInfo> displays,
                                              string? deviceIdFragment, int? width, int? height)
    {
        if (displays is null || displays.Count == 0) return new DisplayChoice(null, DisplayReasonNone);
        if (!string.IsNullOrWhiteSpace(deviceIdFragment))
        {
            var byId = displays.FirstOrDefault(d =>
                d.DeviceId.Contains(deviceIdFragment, StringComparison.OrdinalIgnoreCase));
            if (byId is not null) return new DisplayChoice(byId, DisplayReasonId);
        }
        if (width is > 0 && height is > 0)
        {
            var bySize = displays.Where(d => d.Bounds.Width == width && d.Bounds.Height == height).ToList();
            var usable = bySize.Where(d => !d.Primary).ToList();
            if (usable.Count == 1) return new DisplayChoice(usable[0], DisplayReasonSize);
            if (usable.Count > 1) return new DisplayChoice(null, DisplayReasonSizeAmbiguous);
            if (bySize.Count > 0) return new DisplayChoice(null, DisplayReasonSizePrimary);
        }
        return new DisplayChoice(null, DisplayReasonNone);
    }

    public static DisplayInfo? SelectDisplay(IReadOnlyList<DisplayInfo> displays,
                                             string? deviceIdFragment, int? width, int? height) =>
        ChooseDisplay(displays, deviceIdFragment, width, height).Display;

    /// <summary>The script injected before any page script runs. ONE object, never bare
    /// `let` globals: a prop named like a widget function would otherwise collide at parse
    /// time (widget 0.27.0 shipped blank for exactly that). System.Text.Json escapes
    /// &lt; and &gt; by default, so a value cannot close a script tag either.
    ///
    /// SCA-022. AddScriptToExecuteOnDocumentCreatedAsync runs on EVERY document the
    /// WebView creates, child frames included, and the pairing code rides in this object.
    /// The guard is `window.top === window`, evaluated inside the frame that is running
    /// the script: a child frame sees a different `window` and assigns nothing. It is a
    /// same-origin-safe comparison - reading window.top's PROPERTIES across origins
    /// throws, comparing the reference does not. FrameNavigationStarting is the other
    /// half (PanelForm), and neither is a substitute for the other: this one holds even
    /// for an about:blank child a script creates without navigating.</summary>
    public static string HostScript(IReadOnlyDictionary<string, object?> props, string? panelToken, string hostVersion)
    {
        var merged = new Dictionary<string, object?>(props, StringComparer.Ordinal);
        if (!string.IsNullOrWhiteSpace(panelToken)) merged["panelToken"] = panelToken.Trim();
        var payload = new Dictionary<string, object?>
        {
            ["kind"] = "standalone",
            ["host"] = "panel-host",
            ["version"] = hostVersion,
            ["props"] = merged,
        };
        var json = JsonSerializer.Serialize(payload);
        return HostScriptPrefix + json + HostScriptSuffix;
    }

    public const string HostScriptPrefix = "if (window.top === window) { window.__sidecrabHost = ";
    public const string HostScriptSuffix = "; }";

    /// <summary>SCA-028. Is the document now showing the panel itself? IsAllowedNavigation
    /// answers "may this load", which about:blank and the host's own data: fallback both
    /// pass; this answers "is the panel on the glass", which only the http panel URL does.
    /// A completed navigation to about:blank reported as loaded left the window blank with
    /// the retry stopped and nothing scheduled to notice.</summary>
    public static bool IsPanelDocument(string? uri, Uri panelUrl)
    {
        if (string.IsNullOrWhiteSpace(uri)) return false;
        if (uri == "about:blank") return false;
        if (uri.StartsWith("data:", StringComparison.Ordinal)) return false;
        return IsAllowedNavigation(uri, panelUrl);
    }

    /// <summary>SCA-022. Where a CHILD frame may navigate: the panel's own origin and
    /// path, and nothing else. Stricter than the top-level lock, which also admits
    /// about:blank and the data: fallback page - those two are the host's own doing and a
    /// frame has no business at either.</summary>
    public static bool IsAllowedFrameNavigation(string? uri, Uri panelUrl)
    {
        if (string.IsNullOrWhiteSpace(uri)) return false;
        if (uri == "about:blank") return false;
        if (uri.StartsWith("data:", StringComparison.Ordinal)) return false;
        return IsAllowedNavigation(uri, panelUrl);
    }

    /// <summary>The zoom that makes the page's CSS viewport equal the monitor's physical
    /// width. A 100% monitor needs none; a 125% one reports innerWidth 2048 for 2560 px
    /// and gets 0.8. Total: any non-positive measurement leaves the zoom alone.</summary>
    public static double CorrectedZoom(double currentZoom, int cssWidth, int physicalWidth)
    {
        if (cssWidth <= 0 || physicalWidth <= 0 || currentZoom <= 0) return currentZoom;
        if (cssWidth == physicalWidth) return currentZoom;
        return currentZoom * cssWidth / physicalWidth;
    }

    // ---- lane B: the settings a page may write ----

    /// <summary>The WHOLE list of props the page is allowed to set, with each one's type
    /// and range. Anything not named here is dropped - and the three that matter most are
    /// the ones that are absent: <c>panelToken</c> is the pairing code, which is the one
    /// secret a page must never be able to read back or replace; <c>crabdPort</c> and
    /// <c>display</c> are how this host finds the companion and the glass, so a page that
    /// could move either could point the window at something else or hide it.</summary>
    public static readonly string[] SettingsBooleans =
        { "clock24", "alertFlash", "crabStyle", "touchDiag", "chime" };

    public static readonly string[] SettingsColors =
        { "textColor", "accentColor", "backgroundColor" };

    /// <summary>Whole percent, 0..100, clamped rather than refused: a slider that arrives
    /// out of range is a value to correct, and dropping it would leave the operator's own
    /// move silently unsaved.</summary>
    public static readonly string[] SettingsPercents = { "transparency", "chimeVolume" };

    /// <summary>What the page sent, reduced to what may be stored. A whitelist and not a
    /// filter: an unknown key, a wrong type, a colour that is not #RRGGBB and a
    /// non-finite number are all DROPPED, never coerced, because a coerced setting is a
    /// value nobody chose being written to the operator's file.
    ///
    /// Returns an empty dictionary when nothing survives, and the caller does not write
    /// on empty - a page that posts rubbish must not be able to rewrite the file at all.
    /// </summary>
    public static Dictionary<string, object?> ValidateSettingsProps(JsonElement props)
    {
        var clean = new Dictionary<string, object?>(StringComparer.Ordinal);
        if (props.ValueKind != JsonValueKind.Object) return clean;

        foreach (var key in SettingsBooleans)
        {
            if (!props.TryGetProperty(key, out var v)) continue;
            // The JSON true/false literals only. A string "true" or a 1 is a caller that
            // does not know the shape, and guessing what it meant is how a switch ends up
            // flipped by a typo.
            if (v.ValueKind is JsonValueKind.True or JsonValueKind.False) clean[key] = v.GetBoolean();
        }

        foreach (var key in SettingsColors)
        {
            if (!props.TryGetProperty(key, out var v) || v.ValueKind != JsonValueKind.String) continue;
            var hex = NormalizeHex(v.GetString());
            if (hex is not null) clean[key] = hex;
        }

        foreach (var key in SettingsPercents)
        {
            if (!props.TryGetProperty(key, out var v) || v.ValueKind != JsonValueKind.Number) continue;
            if (!v.TryGetDouble(out var d) || double.IsNaN(d) || double.IsInfinity(d)) continue;
            // Boxed as long: System.Text.Json writes a double 60 as "60" but a double
            // 0.1-accumulated one as "60.000000000000007", and the widget reads this back
            // as a percentage.
            clean[key] = (long)Math.Clamp(Math.Round(d), 0, 100);
        }
        return clean;
    }

    /// <summary>#RRGGBB, upper-cased, or null. Three digits, a named colour, a CSS
    /// function and an alpha channel are all refused: the widget writes this straight
    /// into a custom property and its own reader is the same six-digit shape.</summary>
    public static string? NormalizeHex(string? raw)
    {
        var s = raw?.Trim();
        if (string.IsNullOrEmpty(s)) return null;
        if (s[0] == '#') s = s[1..];
        if (s.Length != 6) return null;
        foreach (var c in s)
        {
            var hex = c is >= '0' and <= '9' or >= 'a' and <= 'f' or >= 'A' and <= 'F';
            if (!hex) return null;
        }
        return "#" + s.ToUpperInvariant();
    }

    /// <summary>The settings file with <paramref name="clean"/> merged over its `props`
    /// and EVERY other key kept byte-for-byte in value: crabdPort, display and
    /// devtoolsPort are this host's own configuration and a settings save may not touch
    /// them. An unreadable or non-object file is replaced by a fresh one carrying only
    /// the props, which is the same answer PanelSettings.Parse gives it.</summary>
    public static string MergeSettingsJson(string? existingJson, IReadOnlyDictionary<string, object?> clean)
    {
        var root = new Dictionary<string, JsonElement>(StringComparer.Ordinal);
        if (!string.IsNullOrWhiteSpace(existingJson))
        {
            try
            {
                using var doc = JsonDocument.Parse(existingJson);
                if (doc.RootElement.ValueKind == JsonValueKind.Object)
                    foreach (var kv in doc.RootElement.EnumerateObject())
                        root[kv.Name] = kv.Value.Clone();
            }
            catch (JsonException) { /* replaced below, exactly as Parse() treats it */ }
        }

        var props = new Dictionary<string, object?>(StringComparer.Ordinal);
        if (root.TryGetValue("props", out var old) && old.ValueKind == JsonValueKind.Object)
        {
            foreach (var kv in old.EnumerateObject())
            {
                // A prop the page may not write keeps whatever the file already said;
                // panelToken is dropped outright, since the code's one home is
                // panel-token and a copy here would be a second place to leak it.
                if (kv.Name == "panelToken") continue;
                props[kv.Name] = JsonValue(kv.Value);
            }
        }
        foreach (var kv in clean) props[kv.Key] = kv.Value;

        var output = new Dictionary<string, object?>(StringComparer.Ordinal);
        foreach (var kv in root)
        {
            if (kv.Key == "props") continue;
            output[kv.Key] = JsonValue(kv.Value);
        }
        output["props"] = props;
        return JsonSerializer.Serialize(output, new JsonSerializerOptions { WriteIndented = true });
    }

    // ---- lane E: bring a session to the front ----
    // Provisional labels: host 0.3.0.

    /// <summary>One top-level window as the enumerator found it. The handle rides as a
    /// number so the ranking is testable with no desktop behind it.</summary>
    public sealed record WindowCandidate(long Handle, string ProcessName, string Title,
                                         string ClassName, bool Minimised);

    /// <summary>What the page asked to bring forward, after the whitelist.</summary>
    public sealed record FocusRequest(string SessionId, string Title, string Cwd, string Repo);

    /// <summary>The ranking's answer: a window, or the reason there is not one.</summary>
    public sealed record FocusChoice(WindowCandidate? Window, string Reason, int Score);

    public const int FocusIdMax = 100;     // a session id is a 36-character UUID
    public const int FocusTextMax = 300;

    /// <summary>The WHOLE list of keys a focus-session message may carry. Same shape as
    /// the settings whitelist and for the same reason: an unknown key is dropped, never
    /// read. Nothing here reaches a file or a setting - these four strings only pick a
    /// window out of a list the host built itself.</summary>
    public static readonly string[] FocusKeys = { "sessionId", "title", "cwd", "repo" };

    /// <summary>The message reduced to what may be acted on, or null when it carries no
    /// session id. Every value is capped and stripped of control characters: panel.log is
    /// the only account of what this host did, and a newline inside a session title would
    /// let a page forge a line in it.</summary>
    public static FocusRequest? ValidateFocusRequest(JsonElement root)
    {
        if (root.ValueKind != JsonValueKind.Object) return null;
        var id = FocusString(root, "sessionId", FocusIdMax);
        if (id.Length == 0) return null;
        return new FocusRequest(id,
                                FocusString(root, "title", FocusTextMax),
                                FocusString(root, "cwd", FocusTextMax),
                                FocusString(root, "repo", FocusTextMax));
    }

    private static string FocusString(JsonElement root, string key, int max)
    {
        if (!root.TryGetProperty(key, out var v) || v.ValueKind != JsonValueKind.String) return string.Empty;
        var s = v.GetString();
        if (string.IsNullOrEmpty(s)) return string.Empty;
        var sb = new StringBuilder(Math.Min(s.Length, max));
        foreach (var c in s)
        {
            if (char.IsControl(c)) continue;
            sb.Append(c);
            if (sb.Length == max) break;
        }
        return sb.ToString().Trim();
    }

    public const string MutexKiosk = @"Local\SideCrab.Panel";
    public const string MutexWindowed = @"Local\SideCrab.Panel.windowed";

    /// <summary>The single-instance mutex, chosen by MODE and by PROFILE. The pinned kiosk
    /// keeps the bare name byte-for-byte, so the scheduled task and anything already
    /// running exclude each other exactly as before; a --windowed dev host takes its own
    /// name.
    ///
    /// SCA-024: a NAMED profile takes a third name. Separate WebView2 folders and separate
    /// logs are not isolation on their own while the mutex still refuses to start the
    /// second host - which is what happens on a PC where the installed kiosk is already
    /// running, and is how this was measured on 2026-09-21.
    ///
    /// The trade: --profile can stack a second kiosk-shaped window on the Edge, which is
    /// exactly what the bare mutex exists to stop. It is an explicit switch nothing
    /// installs, the scheduled task never passes it (Install-SideCrab.ps1), and the
    /// profile is named in the startup line, so a stacked window says which host it is.
    ///
    /// The trap is in Program.cs, where the name is used.</summary>
    public static string MutexName(bool windowed, string? profile = null)
    {
        var p = (profile ?? string.Empty).Trim();
        if (p.Length > 0) return MutexKiosk + "." + SafeProfileName(p);
        return windowed ? MutexWindowed : MutexKiosk;
    }

    public const int FocusScoreTitleExact = 100;
    public const int FocusScoreTitleContains = 60;
    public const int FocusScoreRepo = 20;
    public const int FocusScoreLeaf = 10;

    /// <summary>A session title shorter than this is not evidence on its own: "IT" or
    /// "dev" appears in half the title bars on a working desktop.</summary>
    public const int FocusMinTitleMatch = 6;
    public const int FocusMinWordMatch = 4;

    public const string FocusDesktopProcess = "claude";

    public static readonly string[] FocusTerminalProcesses =
        { "windowsterminal", "pwsh", "powershell", "cmd", "conhost", "wt",
          "alacritty", "wezterm-gui", "kitty", "mintty" };

    /// <summary>How well one window answers to one session, on EVIDENCE only. The Claude
    /// desktop app scores nothing here; it is the labelled fallback in SelectWindow.
    /// Measured on this PC 2026-09-21, and each number is one of those measurements:
    ///
    /// - A terminal-hosted session names its own window (`claude -n`, per `claude --help`
    ///   2.1.278: the name shows in "the prompt box, /resume picker, and terminal title"),
    ///   so a title match is the strongest evidence available and wins outright. The
    ///   contains branch is not decoration: a real console title came back
    ///   "Administrator:  lane E focus probe session " and an equality test missed it.
    /// - The repo name and the cwd leaf are scored for TERMINALS ONLY and only past
    ///   FocusMinWordMatch characters. The reason is four windows on this PC titled
    ///   "pwsh in IT" - the leaf of C:\IT - none of which was a Claude session.
    ///
    /// SCA-023: the TITLE is scored for terminals only as well. A session lives in a
    /// terminal or in the desktop app, so a window that is neither is not a candidate
    /// whatever its title says. The audit's reproduction is the shape of the bug: a
    /// Notepad window titled "Release notes" scored 100 on the exact match and beat the
    /// actual Windows Terminal session titled "Release notes - Windows Terminal" at 60.
    /// An exact title match on an unrelated application is not weak evidence to be
    /// outvoted; it is not evidence, and the foreground it takes is the operator's.
    /// </summary>
    public static int ScoreWindow(WindowCandidate w, FocusRequest req)
    {
        if (w is null || req is null) return 0;
        var title = (w.Title ?? string.Empty).Trim();
        if (title.Length == 0) return 0;
        if (!IsTerminalWindow(w.ProcessName, w.ClassName)) return 0;

        var score = 0;
        var want = req.Title.Trim();
        if (want.Length > 0 && string.Equals(title, want, StringComparison.OrdinalIgnoreCase))
            score += FocusScoreTitleExact;
        else if (want.Length >= FocusMinTitleMatch && title.Contains(want, StringComparison.OrdinalIgnoreCase))
            score += FocusScoreTitleContains;

        var repo = req.Repo.Trim();
        if (repo.Length >= FocusMinWordMatch && title.Contains(repo, StringComparison.OrdinalIgnoreCase))
            score += FocusScoreRepo;
        var leaf = CwdLeaf(req.Cwd);
        if (leaf.Length >= FocusMinWordMatch && title.Contains(leaf, StringComparison.OrdinalIgnoreCase))
            score += FocusScoreLeaf;
        return score;
    }

    public static bool IsDesktopApp(string? processName) =>
        string.Equals((processName ?? string.Empty).Trim(), FocusDesktopProcess,
                      StringComparison.OrdinalIgnoreCase);

    public static bool IsTerminalWindow(string? processName, string? className)
    {
        var p = (processName ?? string.Empty).Trim();
        foreach (var t in FocusTerminalProcesses)
            if (string.Equals(p, t, StringComparison.OrdinalIgnoreCase)) return true;
        var c = (className ?? string.Empty).Trim();
        return string.Equals(c, "ConsoleWindowClass", StringComparison.OrdinalIgnoreCase)
            || string.Equals(c, "CASCADIA_HOSTING_WINDOW_CLASS", StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>The last segment of a working directory. A drive root returns nothing:
    /// "C:" would match any title carrying those two letters.</summary>
    public static string CwdLeaf(string? cwd)
    {
        var s = cwd?.Trim();
        if (string.IsNullOrEmpty(s)) return string.Empty;
        s = s.TrimEnd('\\', '/');
        if (s.Length == 0) return string.Empty;
        var cut = s.LastIndexOfAny(new[] { '\\', '/' });
        var leaf = cut >= 0 ? s[(cut + 1)..] : s;
        return leaf.EndsWith(':') ? string.Empty : leaf;
    }

    /// <summary>Which window to bring forward, or why none. The caller has already
    /// dropped its own process, the hidden and untitled windows and anything not on the
    /// primary display, so everything here is a window that COULD be focused.
    ///
    /// EVIDENCE FIRST, then the desktop app as a LABELLED fallback. The app is not scored
    /// alongside the rest, and the first cut of this code got that wrong in a way only a
    /// live run showed: with a flat 30 points for being the Claude process, a request for
    /// a session that no window answered to still brought the app forward and reported
    /// success. "No window found for this session" was unreachable while the app was
    /// running, which is the shape of a control that reports success forever.
    ///
    /// So the app now wins only when nothing was matched, and it comes back under its own
    /// reason so the page can say what actually happened: the app hosts every session that
    /// has no window of its own (measured 2026-09-21: two live sessions, ONE window titled
    /// "Claude", and no session title anywhere in any window title), and the operator picks
    /// the session in its sidebar.
    ///
    /// A tie is refused rather than broken, on either pass. Focusing the wrong window takes
    /// the keyboard away from whatever the operator was typing into, and the measured tie
    /// on this PC is four identically titled console windows, none of which was a session:
    /// a coin toss there is wrong three times in four.</summary>
    public static FocusChoice SelectWindow(IReadOnlyList<WindowCandidate>? candidates, FocusRequest? req)
    {
        if (req is null) return new FocusChoice(null, "no-request", 0);
        if (candidates is null || candidates.Count == 0) return new FocusChoice(null, "no-window", 0);

        WindowCandidate? best = null;
        var bestScore = 0;
        var tied = 0;
        foreach (var c in candidates)
        {
            var s = ScoreWindow(c, req);
            if (s <= 0) continue;
            if (s > bestScore) { best = c; bestScore = s; tied = 1; }
            else if (s == bestScore) tied++;
        }
        if (best is not null)
            return tied > 1 ? new FocusChoice(null, "ambiguous", bestScore)
                            : new FocusChoice(best, "matched", bestScore);

        WindowCandidate? app = null;
        var apps = 0;
        foreach (var c in candidates)
            if (IsDesktopApp(c.ProcessName)) { app = c; apps++; }
        if (apps == 1) return new FocusChoice(app, "desktop-app", 0);
        // Two app windows and no evidence is the same coin toss as two consoles.
        if (apps > 1) return new FocusChoice(null, "ambiguous", 0);
        return new FocusChoice(null, "no-match", 0);
    }

    // ---- lane H: bridge v2, the log, profiles and the display picker ----
    // Provisional labels: host 0.4.0.

    /// <summary>SCA-030. One untrusted field, safe to put in a log line: control
    /// characters escaped, length bounded. panel.log is the only account of what this
    /// host did, and a newline inside a value a page chose would let that page write a
    /// line of its own into it. The audit's fixture did exactly that with a message type
    /// carrying \n and got a second, unprefixed, forged-looking record.</summary>
    public const int LogFieldMax = 200;

    public static string EscapeForLog(string? raw, int max = LogFieldMax)
    {
        if (string.IsNullOrEmpty(raw)) return string.Empty;
        var sb = new StringBuilder(Math.Min(raw.Length, max) + 8);
        var truncated = false;
        foreach (var c in raw)
        {
            if (sb.Length >= max) { truncated = true; break; }
            switch (c)
            {
                case '\r': sb.Append("\\r"); break;
                case '\n': sb.Append("\\n"); break;
                case '\t': sb.Append("\\t"); break;
                case '\\': sb.Append("\\\\"); break;
                default:
                    if (char.IsControl(c)) sb.Append("\\x").Append(((int)c).ToString("x2"));
                    else sb.Append(c);
                    break;
            }
        }
        if (truncated) sb.Append("...");
        return sb.ToString();
    }

    /// <summary>SCA-031. The log file THIS instance owns. Two hosts appending to one file
    /// lose lines: the audit's two writers attempted 4000 and 986 were recorded, well
    /// under the 1 MB roll, so the loss is the append race and not the roll. The lock in
    /// Log is per-object and spans neither process nor instance.
    ///
    /// The kiosk keeps <c>panel.log</c> byte-for-byte: the setup lane's smoke check reads
    /// the viewport line out of that name.</summary>
    public static string LogFileName(bool windowed, string? profile = null)
    {
        var p = (profile ?? string.Empty).Trim();
        if (p.Length > 0) return "panel-" + SafeProfileName(p) + ".log";
        return windowed ? "panel-windowed.log" : "panel.log";
    }

    /// <summary>SCA-024. The WebView2 user-data folder is per PROFILE, not per host.
    /// CreateAsync refuses a second environment over an occupied folder when the browser
    /// options differ (the audit measured COMException 0x8007139F for a second instance
    /// asking for its own --remote-debugging-port), so a dev host that needs devtools
    /// cannot share the kiosk's folder. Same-option coexistence does work; this stops the
    /// case that does not, and stops the two instances writing each other's preferences.</summary>
    public static string ProfileName(bool windowed, string? profile = null)
    {
        var p = (profile ?? string.Empty).Trim();
        if (p.Length > 0) return SafeProfileName(p);
        return windowed ? "windowed" : "kiosk";
    }

    public static string WebViewUserDataDir(string localAppData, string profileName) =>
        System.IO.Path.Combine(localAppData, "SideCrab", "Panel",
                               profileName == "kiosk" ? "WebView2" : "WebView2-" + SafeProfileName(profileName));

    /// <summary>A profile name reaches a PATH and a file name, so it is reduced to
    /// letters, digits, dash and underscore rather than trusted.</summary>
    public static string SafeProfileName(string? raw)
    {
        var s = (raw ?? string.Empty).Trim();
        var sb = new StringBuilder(Math.Min(s.Length, 32));
        foreach (var c in s)
        {
            if (sb.Length == 32) break;
            if (char.IsAsciiLetterOrDigit(c) || c is '-' or '_') sb.Append(char.ToLowerInvariant(c));
        }
        return sb.Length == 0 ? "profile" : sb.ToString();
    }

    // ---- C2: one typed terminal reply per accepted request ----

    public const int RequestIdMax = 64;

    /// <summary>A page-to-host message reduced to the two things every one of them
    /// carries. A missing or unusable <c>requestId</c> is a legacy page, and it is
    /// answered with a null id rather than refused: the reply is what ends the page's
    /// pending state, and refusing it silently is the defect SCA-021 names.</summary>
    public sealed record BridgeRequest(string Type, string? RequestId);

    public static BridgeRequest? ParseBridgeRequest(JsonElement root)
    {
        if (root.ValueKind != JsonValueKind.Object) return null;
        if (!root.TryGetProperty("type", out var t) || t.ValueKind != JsonValueKind.String) return null;
        var type = t.GetString();
        if (string.IsNullOrWhiteSpace(type)) return null;
        return new BridgeRequest(type, RequestId(root));
    }

    /// <summary>The correlation id, or null. Over <see cref="RequestIdMax"/> characters is
    /// null and not truncated: a truncated id would correlate with the wrong attempt,
    /// which is worse than the legacy path.</summary>
    public static string? RequestId(JsonElement root)
    {
        if (root.ValueKind != JsonValueKind.Object) return null;
        if (!root.TryGetProperty("requestId", out var v) || v.ValueKind != JsonValueKind.String) return null;
        var s = v.GetString();
        if (string.IsNullOrWhiteSpace(s)) return null;
        s = s.Trim();
        if (s.Length > RequestIdMax) return null;
        foreach (var c in s) if (char.IsControl(c)) return null;
        return s;
    }

    public const string ChannelHostInfo = "host-info";
    public const string ChannelSettings = "settings";
    public const string ChannelFocus = "focus-session";

    /// <summary>Which attempt on a channel is the current one. The reply itself always
    /// goes out - exactly one per accepted request is the contract - and carries the id
    /// it answers, so the page discards a stale one by correlation. This records which id
    /// is newest so a late reply can be LOGGED as superseded rather than read as the
    /// answer to the attempt the operator is waiting on.</summary>
    public sealed class BridgeGate
    {
        private readonly Dictionary<string, string> _current = new(StringComparer.Ordinal);

        public void Accepted(string channel, string? requestId)
        {
            if (requestId is null) _current.Remove(channel);
            else _current[channel] = requestId;
        }

        public bool IsCurrent(string channel, string? requestId)
        {
            if (!_current.TryGetValue(channel, out var id)) return true;
            return requestId is not null && string.Equals(id, requestId, StringComparison.Ordinal);
        }
    }

    public static object HostInfoReply(string? requestId, string version, int pid, string startedAt,
                                       string settingsPath, bool hasToken) => new
    {
        type = "host-info",
        requestId,
        version,
        pid,
        startedAt,
        settingsPath,
        hasToken,
        capabilities = new { saveSettings = true, focusSession = true, pickDisplay = true },
    };

    public static object SettingsReply(string? requestId, bool ok, string? error) => new
    {
        type = "settings-result",
        requestId,
        ok,
        error,
    };

    public static object FocusReply(string? requestId, string sessionId, bool ok, string reason, string? window) => new
    {
        type = "focus-result",
        requestId,
        sessionId,
        ok,
        reason,
        window,
    };

    // ---- C7: the two lines the setup lane reads ----

    /// <summary>The viewport line, field order fixed. Test-SideCrab reads it and a
    /// reordered field is a broken smoke check, not a cosmetic change. pid and started
    /// are what tell yesterday's line from this run's.</summary>
    public static string ViewportLine(int cssWidth, int cssHeight, double dpr, double zoom,
                                      int windowWidth, int windowHeight, int pid, string startedAt) =>
        $"viewport: {cssWidth}x{cssHeight} css px, dpr {dpr:0.###}, zoom {zoom:0.###}, " +
        $"window {windowWidth}x{windowHeight} physical, pid {pid}, started {startedAt}";

    public static string HiddenLine(int pid, string startedAt) =>
        $"hidden: target display absent, pid {pid}, started {startedAt}";

    /// <summary>At start and then at most once a minute. The repin timer runs every five
    /// seconds and an absent Edge is a state that lasts days: unthrottled this is 17,000
    /// lines a day, which rolls the evidence of what happened before it out of the file.</summary>
    public static readonly TimeSpan HiddenLineInterval = TimeSpan.FromSeconds(60);

    public static bool ShouldLogHidden(DateTime? lastAt, DateTime now) =>
        lastAt is null || now - lastAt.Value >= HiddenLineInterval;

    // ---- MF-004: the display picker ----

    /// <summary>A string safe to put in a WinForms menu item or Label. A PnP device id is
    /// full of ampersands (<c>5&amp;a4ae9a5&amp;0&amp;UID4358</c>) and both controls read a
    /// single one as a mnemonic prefix: it is swallowed, the next letter is underlined, and
    /// on a Label the rest of the text can be dropped outright. Measured 2026-09-21: the
    /// picker's confirmation window rendered its first line and nothing after it.</summary>
    public static string EscapeMnemonics(string? s) => (s ?? string.Empty).Replace("&", "&&");

    /// <summary>One display as the picker lists it: the full device id, size, position,
    /// scaling and whether it is primary. The whole id and not a fragment - two identical
    /// monitors differ only in the UID part of it. NOT mnemonic-escaped: this is also what
    /// the log and the status window carry, and the caller that puts it in a menu escapes
    /// it there.</summary>
    public static string DisplayLabel(DisplayInfo d) =>
        DisplayLabelShort(d) + "  [" + DisplayLabelId(d) + "]";

    /// <summary>The part an operator reads at a glance, with no device id. The menu puts
    /// the id on a second line: one line carrying both measured 1338 px wide for a single
    /// monitor, which is most of the width of the display the menu opens on.</summary>
    public static string DisplayLabelShort(DisplayInfo d) =>
        $"{d.DeviceName}  {d.Bounds.Width}x{d.Bounds.Height} at {d.Bounds.X},{d.Bounds.Y}  " +
        $"{d.ScalePercent}%{(d.Primary ? "  primary" : "")}";

    /// <summary>The device id as a UI control shows it: control characters dropped and the
    /// length bounded, but backslashes left alone. EscapeForLog doubles them, which is
    /// right in a log line and reads as a typo in a menu - the id is what the operator
    /// compares against what Windows shows them. The log has its own escaped copy
    /// (PanelForm.Describe).</summary>
    public static string DisplayLabelId(DisplayInfo d)
    {
        var raw = d.DeviceId ?? string.Empty;
        var sb = new StringBuilder(Math.Min(raw.Length, 160));
        foreach (var c in raw)
        {
            if (sb.Length == 160) break;
            if (!char.IsControl(c)) sb.Append(c);
        }
        return sb.ToString();
    }

    /// <summary>The shortest fragment of this display's PnP id that matches THIS display
    /// and no other, for writing into <c>display.deviceId</c>. The model part alone
    /// (CRXED00) is what an operator recognises and is what one Edge gets; two identical
    /// Edges both contain it, so the instance part (the adapter key and UID) is added and
    /// the whole id is the last resort.
    ///
    /// Empty when the monitor reports no device id at all (a virtual adapter with nothing
    /// attached): there is no fragment that would find it again, and the picker refuses
    /// it rather than writing a setting that cannot match.</summary>
    public static string UniqueDeviceIdFragment(DisplayInfo d, IReadOnlyList<DisplayInfo> all)
    {
        var id = d.DeviceId ?? string.Empty;
        if (string.IsNullOrWhiteSpace(id)) return string.Empty;
        var parts = id.Split('#');
        var candidates = new List<string>();
        if (parts.Length > 1 && parts[1].Length > 0) candidates.Add(parts[1]);
        if (parts.Length > 2 && parts[2].Length > 0) candidates.Add(parts[1] + "#" + parts[2]);
        candidates.Add(id);
        foreach (var c in candidates)
        {
            var hits = all.Count(x => (x.DeviceId ?? string.Empty)
                                      .Contains(c, StringComparison.OrdinalIgnoreCase));
            if (hits == 1) return c;
        }
        return id;
    }

    /// <summary>The settings file with <c>display.deviceId</c> replaced and EVERYTHING
    /// else kept, width and height included: the picker names a monitor, it does not
    /// resize the window, and the size fallback stays available for the day the id
    /// changes. Same shape as MergeSettingsJson and for the same reason.</summary>
    public static string MergeDisplayDeviceIdJson(string? existingJson, string deviceId)
    {
        var root = new Dictionary<string, JsonElement>(StringComparer.Ordinal);
        if (!string.IsNullOrWhiteSpace(existingJson))
        {
            try
            {
                using var doc = JsonDocument.Parse(existingJson);
                if (doc.RootElement.ValueKind == JsonValueKind.Object)
                    foreach (var kv in doc.RootElement.EnumerateObject())
                        root[kv.Name] = kv.Value.Clone();
            }
            catch (JsonException) { /* replaced, exactly as Parse() treats it */ }
        }

        var display = new Dictionary<string, object?>(StringComparer.Ordinal);
        if (root.TryGetValue("display", out var old) && old.ValueKind == JsonValueKind.Object)
            foreach (var kv in old.EnumerateObject())
                display[kv.Name] = JsonValue(kv.Value);
        display["deviceId"] = deviceId;

        var output = new Dictionary<string, object?>(StringComparer.Ordinal);
        foreach (var kv in root)
        {
            if (kv.Key == "display") continue;
            output[kv.Key] = JsonValue(kv.Value);
        }
        output["display"] = display;
        return JsonSerializer.Serialize(output, new JsonSerializerOptions { WriteIndented = true });
    }

    /// <summary>How long a picked display stands before it is undone unless kept.</summary>
    public const int DisplayRevertSeconds = 10;

    public const string RevertAsk = "ask";
    public const string RevertAuto = "auto";

    /// <summary>What happens when the timer runs out. A monitor the operator can SEE and
    /// reach gets a "keep this display?" prompt; anything else reverts on its own, because
    /// a prompt on a monitor that is off, unplugged or facing away is a panel the operator
    /// cannot get back without editing JSON. The prompt is shown on the PRIMARY display
    /// whichever monitor was picked.</summary>
    public static string RevertMode(DisplayInfo? picked) =>
        picked is not null && picked.Primary ? RevertAsk : RevertAuto;

    // ---- MF-003: what the status window says ----

    /// <summary>The scheduled task's restart policy, as installed. Named here so the
    /// status window can state it instead of implying the task retries forever.</summary>
    public const int TaskRestartCount = 3;
    public const int TaskRestartIntervalMinutes = 1;

    public sealed record HostStatus(string Mode, string Version, int Pid, DateTime StartedAt,
                                    bool Visible, bool Paused, string? TargetLabel, string TargetReason,
                                    bool PanelLoaded, string? LastFailure, string LogPath, int PriorStartsInWindow);

    /// <summary>The status window's text, one line each, in the order it shows them. Pure
    /// so the wording is pinned by a test and not by a screenshot.</summary>
    public static IReadOnlyList<string> StatusLines(HostStatus s, DateTime now)
    {
        var lines = new List<string>
        {
            $"SideCrab panel host {s.Version} ({s.Mode}), pid {s.Pid}",
            $"Started {s.StartedAt:yyyy-MM-dd HH:mm:ss}, up {Uptime(now - s.StartedAt)}",
        };
        if (s.Paused) lines.Add("Panel: paused by you. Resume from this menu.");
        else if (s.Visible && s.TargetLabel is not null) lines.Add($"Panel: shown on {s.TargetLabel}");
        else lines.Add($"Panel: hidden ({HiddenReason(s.TargetReason)})");
        lines.Add(s.PanelLoaded ? "Page: the panel is loaded" : "Page: not loaded");
        lines.Add(s.LastFailure is null ? "Last failure: none this run"
                                        : "Last failure: " + EscapeForLog(s.LastFailure));
        lines.Add($"Restart on failure: up to {TaskRestartCount} times, " +
                  $"{TaskRestartIntervalMinutes} minute apart" +
                  (s.PriorStartsInWindow > 0
                      ? $"; {s.PriorStartsInWindow} earlier start(s) in this log within the last hour"
                      : "; no earlier start in this log within the last hour"));
        lines.Add("Log: " + s.LogPath);
        return lines;
    }

    public static string HiddenReason(string reason) => reason switch
    {
        DisplayReasonSizeAmbiguous => "two displays match the configured size; pick one from this menu",
        DisplayReasonSizePrimary => "the only size match is your primary display; pick a display from this menu",
        DisplayReasonNone => "the target display is not attached",
        _ => reason,
    };

    private static string Uptime(TimeSpan t) =>
        t.TotalDays >= 1 ? $"{(int)t.TotalDays}d {t.Hours}h" :
        t.TotalHours >= 1 ? $"{(int)t.TotalHours}h {t.Minutes}m" :
        $"{(int)t.TotalMinutes}m {t.Seconds}s";

    private static object? JsonValue(JsonElement e) => e.ValueKind switch
    {
        JsonValueKind.True => true,
        JsonValueKind.False => false,
        JsonValueKind.String => e.GetString(),
        // Boxed on both arms: a bare ternary promotes long to double and a port of 2722
        // would be rewritten as 2722.0. PanelSettings.Parse makes the same split.
        JsonValueKind.Number => e.TryGetInt64(out var l) ? (object)l : e.GetDouble(),
        JsonValueKind.Null => null,
        _ => e.Clone(),
    };
}
