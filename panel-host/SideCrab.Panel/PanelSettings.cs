using System.Text.Json;

namespace SideCrab.Panel;

/// <summary>What the host reads from <c>~/.sidecrab</c>: the optional
/// <c>panel-settings.json</c> (crabd port, which display, and the widget props that stand
/// in for the retired property sheet) and the pairing code in <c>panel-token</c>. Every key is
/// optional; an absent or malformed file is the defaults, logged, never a crash.
///
/// <code>
/// {
///   "crabdPort": 2722,
///   "display": { "deviceId": "CRXED00", "width": 2560, "height": 720 },
///   "props":   { "clock24": true, "accentColor": "#6F94CC", "touchDiag": false }
/// }
/// </code>
///
/// Quiet hours, toast, digest and budget are NOT props here: in this host config.json is
/// their one master (the widget's property sync is off when it runs standalone).</summary>
public sealed class PanelSettings
{
    public int CrabdPort { get; init; } = PanelLogic.DefaultPort;
    public string? DisplayDeviceId { get; init; } = PanelLogic.DefaultDisplayDeviceId;
    public int DisplayWidth { get; init; } = PanelLogic.DefaultDisplayWidth;
    public int DisplayHeight { get; init; } = PanelLogic.DefaultDisplayHeight;
    public Dictionary<string, object?> Props { get; init; } = new(StringComparer.Ordinal);
    public string? PanelToken { get; init; }
    /// <summary>Chromium's remote-debugging port, loopback only, for measuring the page from
    /// a script (`"devtoolsPort": 9224`). 0 = off, the default. Leave it off in normal use.</summary>
    public int DevToolsPort { get; init; }
    public string Source { get; init; } = "defaults";

    public static string SideCrabDir =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".sidecrab");

    public static PanelSettings Load(string sidecrabDir, Action<string> log)
    {
        var settingsPath = Path.Combine(sidecrabDir, "panel-settings.json");
        var tokenPath = Path.Combine(sidecrabDir, "panel-token");
        var parsed = Parse(settingsPath, log);
        string? token = null;
        try
        {
            if (File.Exists(tokenPath))
            {
                token = File.ReadAllText(tokenPath).Trim();
                if (token.Length == 0) token = null;
            }
        }
        catch (Exception ex) { log($"panel-token unreadable: {ex.GetType().Name}: {ex.Message}"); }
        return new PanelSettings
        {
            CrabdPort = parsed.CrabdPort,
            DisplayDeviceId = parsed.DisplayDeviceId,
            DisplayWidth = parsed.DisplayWidth,
            DisplayHeight = parsed.DisplayHeight,
            Props = parsed.Props,
            PanelToken = token,
            DevToolsPort = parsed.DevToolsPort,
            Source = parsed.Source,
        };
    }

    /// <summary>The settings file alone (no token), so a test can hand it a literal path.</summary>
    public static PanelSettings Parse(string settingsPath, Action<string> log)
    {
        if (!File.Exists(settingsPath)) return new PanelSettings { Source = "defaults (no panel-settings.json)" };
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(settingsPath));
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object) throw new JsonException("root is not an object");

            int port = PanelLogic.DefaultPort;
            if (root.TryGetProperty("crabdPort", out var p) && p.ValueKind == JsonValueKind.Number
                && p.TryGetInt32(out var pv) && pv is > 0 and < 65536) port = pv;

            int devtools = 0;
            if (root.TryGetProperty("devtoolsPort", out var dt) && dt.ValueKind == JsonValueKind.Number
                && dt.TryGetInt32(out var dv) && dv is > 0 and < 65536) devtools = dv;

            string? deviceId = PanelLogic.DefaultDisplayDeviceId;
            int w = PanelLogic.DefaultDisplayWidth, h = PanelLogic.DefaultDisplayHeight;
            if (root.TryGetProperty("display", out var d) && d.ValueKind == JsonValueKind.Object)
            {
                if (d.TryGetProperty("deviceId", out var id))
                {
                    // LO-010, the third case SCA-029 left out. A JSON null is an operator
                    // saying "no id, match on size alone" and is honoured. Any OTHER wrong
                    // type used to become null just as quietly, which turned a typo into
                    // "the panel is no longer pinned by identity" with nothing in the log
                    // to say so; it now keeps the default and names the property, exactly
                    // as a wrong width does.
                    if (id.ValueKind == JsonValueKind.String) deviceId = id.GetString();
                    else if (id.ValueKind == JsonValueKind.Null) deviceId = null;
                    else
                        log($"panel-settings.json: display.deviceId is not a string ({id.ValueKind}); " +
                            $"using {deviceId}. Everything else in {settingsPath} is unchanged.");
                }
                // SCA-029: the ValueKind guard the sibling numbers already had. TryGetInt32
                // THROWS InvalidOperationException on a non-number, and the throw landed in
                // the file-level catch below, which answers with the whole file defaulted:
                // a quoted "720" in a hand-edited display block took crabdPort and every
                // widget prop down with it. Only the wrong dimension defaults now.
                w = Dimension(d, "width", w, settingsPath, log);
                h = Dimension(d, "height", h, settingsPath, log);
            }

            var props = new Dictionary<string, object?>(StringComparer.Ordinal);
            if (root.TryGetProperty("props", out var pr) && pr.ValueKind == JsonValueKind.Object)
            {
                foreach (var kv in pr.EnumerateObject())
                {
                    // A prop named panelToken in the FILE is ignored: the code comes from
                    // panel-token, the one place crabd writes it, never from a copy.
                    if (kv.Name == "panelToken") continue;
                    object? v = kv.Value.ValueKind switch
                    {
                        JsonValueKind.True => true,
                        JsonValueKind.False => false,
                        // Boxed on both arms: a bare ternary promotes long to double and a
                        // slider value of 90 would reach the page as 90.0.
                        JsonValueKind.Number => kv.Value.TryGetInt64(out var l) ? (object)l : (object)kv.Value.GetDouble(),
                        JsonValueKind.String => kv.Value.GetString(),
                        _ => null,
                    };
                    if (v is not null) props[kv.Name] = v;
                }
            }
            return new PanelSettings
            {
                CrabdPort = port, DisplayDeviceId = deviceId, DisplayWidth = w, DisplayHeight = h,
                Props = props, DevToolsPort = devtools, Source = settingsPath,
            };
        }
        catch (Exception ex)
        {
            log($"panel-settings.json ignored ({ex.GetType().Name}: {ex.Message}); using defaults");
            return new PanelSettings { Source = "defaults (panel-settings.json unreadable)" };
        }
    }

    /// <summary>One display dimension, or the default for THAT dimension with the
    /// rejected property named. Bounded at 32768 px: a positive integer that is not a
    /// monitor size is still a value nothing will ever match.</summary>
    private static int Dimension(JsonElement display, string key, int fallback, string path, Action<string> log)
    {
        if (!display.TryGetProperty(key, out var v)) return fallback;
        if (v.ValueKind == JsonValueKind.Number && v.TryGetInt32(out var n) && n is > 0 and <= 32768) return n;
        log($"panel-settings.json: display.{key} is not a usable number " +
            $"({v.ValueKind}); using {fallback}. Everything else in {path} is unchanged.");
        return fallback;
    }
}
