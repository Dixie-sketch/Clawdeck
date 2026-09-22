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

    /// <summary>Which monitor is the Edge. By device id first (a substring of the PnP id
    /// Windows gives the monitor), then by exact physical size. NEVER by index and never
    /// the primary as a fallback: the wrong answer here is a full-screen topmost window
    /// over the operator's main display.</summary>
    public static DisplayInfo? SelectDisplay(IReadOnlyList<DisplayInfo> displays,
                                             string? deviceIdFragment, int? width, int? height)
    {
        if (!string.IsNullOrWhiteSpace(deviceIdFragment))
        {
            var byId = displays.FirstOrDefault(d =>
                d.DeviceId.Contains(deviceIdFragment, StringComparison.OrdinalIgnoreCase));
            if (byId is not null) return byId;
        }
        if (width is > 0 && height is > 0)
        {
            return displays.FirstOrDefault(d => d.Bounds.Width == width && d.Bounds.Height == height);
        }
        return null;
    }

    /// <summary>The script injected before any page script runs. ONE object, never bare
    /// `let` globals: a prop named like a widget function would otherwise collide at parse
    /// time (widget 0.27.0 shipped blank for exactly that). System.Text.Json escapes
    /// &lt; and &gt; by default, so a value cannot close a script tag either.</summary>
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
        return "window.__sidecrabHost = " + json + ";";
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

    /// <summary>The single-instance mutex, chosen by MODE. The pinned kiosk keeps the bare
    /// name byte-for-byte, so the scheduled task and anything already running exclude each
    /// other exactly as before; a --windowed dev host takes its own name.
    ///
    /// The trap is in Program.cs, where the name is used.</summary>
    public static string MutexName(bool windowed) => windowed ? MutexWindowed : MutexKiosk;

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
    /// </summary>
    public static int ScoreWindow(WindowCandidate w, FocusRequest req)
    {
        if (w is null || req is null) return 0;
        var title = (w.Title ?? string.Empty).Trim();
        if (title.Length == 0) return 0;

        var score = 0;
        var want = req.Title.Trim();
        if (want.Length > 0 && string.Equals(title, want, StringComparison.OrdinalIgnoreCase))
            score += FocusScoreTitleExact;
        else if (want.Length >= FocusMinTitleMatch && title.Contains(want, StringComparison.OrdinalIgnoreCase))
            score += FocusScoreTitleContains;

        if (IsTerminalWindow(w.ProcessName, w.ClassName))
        {
            var repo = req.Repo.Trim();
            if (repo.Length >= FocusMinWordMatch && title.Contains(repo, StringComparison.OrdinalIgnoreCase))
                score += FocusScoreRepo;
            var leaf = CwdLeaf(req.Cwd);
            if (leaf.Length >= FocusMinWordMatch && title.Contains(leaf, StringComparison.OrdinalIgnoreCase))
                score += FocusScoreLeaf;
        }
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
