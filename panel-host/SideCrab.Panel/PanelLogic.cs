using System.Drawing;
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
}
