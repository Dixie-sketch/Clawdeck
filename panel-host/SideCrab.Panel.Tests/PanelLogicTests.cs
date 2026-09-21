using System.Drawing;
using System.Text.Json;
using SideCrab.Panel;

namespace SideCrab.Panel.Tests;

/// <summary>The host's three gates, each broken on purpose while this was written: the
/// navigation lock, the display selector, and the host-script encoding.</summary>
[TestClass]
public sealed class PanelLogicTests
{
    private static readonly Uri Panel = PanelLogic.PanelUrl(2722);

    // ---- the navigation lock ---------------------------------------------------------

    [TestMethod]
    public void The_panel_url_itself_and_paths_under_it_are_allowed()
    {
        foreach (var uri in new[]
                 {
                     "http://127.0.0.1:2722/panel/",
                     "http://127.0.0.1:2722/panel/index.html",
                     "http://127.0.0.1:2722/panel/?mock=normal",
                     "http://127.0.0.1:2722/panel/#top",
                     "about:blank",
                     "data:text/html,<p>fallback</p>",
                 })
            Assert.IsTrue(PanelLogic.IsAllowedNavigation(uri, Panel), uri);
    }

    [TestMethod]
    public void Everything_that_is_not_the_panel_on_this_socket_is_refused()
    {
        foreach (var uri in new[]
                 {
                     "http://127.0.0.1:2722/v1/state",          // crabd, but not the panel
                     "http://127.0.0.1:2722/",
                     "http://127.0.0.1:2722/panelx/",            // prefix trick
                     "http://127.0.0.1:2722/panel/../v1/state",  // normalises out of /panel/
                     "http://127.0.0.1:2723/panel/",             // another port
                     "http://localhost:2722/panel/",             // another name for the socket: the host only ever loads 127.0.0.1
                     "https://127.0.0.1:2722/panel/",
                     "http://evil.example/panel/",
                     "http://127.0.0.1.evil.example:2722/panel/",
                     "file:///C:/Dev/sidecrab/widget/index.html",
                     "javascript:alert(1)",
                     "data:text/plain,hello",
                     "",
                     "   ",
                     "not a url",
                 })
            Assert.IsFalse(PanelLogic.IsAllowedNavigation(uri, Panel), uri);
        Assert.IsFalse(PanelLogic.IsAllowedNavigation(null, Panel));
    }

    [TestMethod]
    public void The_lock_follows_the_configured_port()
    {
        var other = PanelLogic.PanelUrl(2800);
        Assert.IsTrue(PanelLogic.IsAllowedNavigation("http://127.0.0.1:2800/panel/", other));
        Assert.IsFalse(PanelLogic.IsAllowedNavigation("http://127.0.0.1:2722/panel/", other));
    }

    // ---- the display selector --------------------------------------------------------

    private static readonly DisplayInfo Primary =
        new(@"\\.\DISPLAY2", @"\\?\DISPLAY#GSMC4B8#5&a4ae9a5&0&UID4352#{guid}", new Rectangle(0, 0, 5120, 2160), true, 96);
    private static readonly DisplayInfo Edge =
        new(@"\\.\DISPLAY1", @"\\?\DISPLAY#CRXED00#5&a4ae9a5&0&UID4358#{guid}", new Rectangle(1228, 2160, 2560, 720), false, 96);
    private static readonly DisplayInfo Other720 =
        new(@"\\.\DISPLAY3", @"\\?\DISPLAY#ACME0001#1&2&3&UID1#{guid}", new Rectangle(-2560, 0, 2560, 720), false, 120);

    [TestMethod]
    public void The_device_id_finds_the_edge_wherever_it_is_in_the_list()
    {
        var list = new[] { Primary, Other720, Edge };
        Assert.AreSame(Edge, PanelLogic.SelectDisplay(list, "CRXED00", 2560, 720));
        Assert.AreSame(Edge, PanelLogic.SelectDisplay(list, "crxed00", null, null));   // case-insensitive
        Assert.AreSame(Edge, PanelLogic.SelectDisplay(new[] { Edge }, "CRXED00", null, null));
    }

    [TestMethod]
    public void The_device_id_outranks_the_size_match()
    {
        // Other720 has the Edge's size and comes first; the id still wins.
        var list = new[] { Primary, Other720, Edge };
        Assert.AreSame(Edge, PanelLogic.SelectDisplay(list, "CRXED00", 2560, 720));
    }

    [TestMethod]
    public void Without_an_id_match_the_exact_size_is_used()
    {
        var list = new[] { Primary, Other720 };
        Assert.AreSame(Other720, PanelLogic.SelectDisplay(list, "CRXED00", 2560, 720));
        Assert.AreSame(Other720, PanelLogic.SelectDisplay(list, null, 2560, 720));
        Assert.AreSame(Other720, PanelLogic.SelectDisplay(list, "", 2560, 720));
    }

    [TestMethod]
    public void Nothing_matching_is_null_never_the_primary_and_never_an_index()
    {
        // The wrong answer here is a topmost full-screen window over the main display.
        Assert.IsNull(PanelLogic.SelectDisplay(new[] { Primary }, "CRXED00", 2560, 720));
        Assert.IsNull(PanelLogic.SelectDisplay(new[] { Primary }, null, 2560, 720));
        Assert.IsNull(PanelLogic.SelectDisplay(new[] { Primary }, null, null, null));
        Assert.IsNull(PanelLogic.SelectDisplay(Array.Empty<DisplayInfo>(), "CRXED00", 2560, 720));
        // A size of the wrong shape is not "close enough".
        Assert.IsNull(PanelLogic.SelectDisplay(new[] { Primary, Other720 }, null, 2560, 1440));
    }

    // ---- the host script ---------------------------------------------------------------

    [TestMethod]
    public void The_host_script_is_one_object_with_the_props_and_the_pairing_code()
    {
        var props = new Dictionary<string, object?> { ["clock24"] = true, ["accentColor"] = "#BE7E6E", ["toastThreshold"] = 120L };
        var script = PanelLogic.HostScript(props, "K7QXM-2PDAB\n", "0.1.0");
        StringAssert.StartsWith(script, "window.__sidecrabHost = ");
        Assert.IsTrue(script.EndsWith(";"), script);
        using var doc = JsonDocument.Parse(script["window.__sidecrabHost = ".Length..^1]);
        var root = doc.RootElement;
        Assert.AreEqual("standalone", root.GetProperty("kind").GetString());
        Assert.AreEqual("0.1.0", root.GetProperty("version").GetString());
        var p = root.GetProperty("props");
        Assert.IsTrue(p.GetProperty("clock24").GetBoolean());
        Assert.AreEqual("#BE7E6E", p.GetProperty("accentColor").GetString());
        Assert.AreEqual(120, p.GetProperty("toastThreshold").GetInt32());
        Assert.AreEqual("K7QXM-2PDAB", p.GetProperty("panelToken").GetString());   // trimmed
    }

    [TestMethod]
    public void No_pairing_code_means_no_panelToken_prop_at_all()
    {
        var script = PanelLogic.HostScript(new Dictionary<string, object?>(), null, "0.1.0");
        using var doc = JsonDocument.Parse(script["window.__sidecrabHost = ".Length..^1]);
        Assert.IsFalse(doc.RootElement.GetProperty("props").TryGetProperty("panelToken", out _));
        var blank = PanelLogic.HostScript(new Dictionary<string, object?>(), "   ", "0.1.0");
        using var doc2 = JsonDocument.Parse(blank["window.__sidecrabHost = ".Length..^1]);
        Assert.IsFalse(doc2.RootElement.GetProperty("props").TryGetProperty("panelToken", out _));
    }

    [TestMethod]
    public void A_hostile_value_cannot_break_out_of_the_script()
    {
        var evil = "\"; alert(1); </script><script>x=1//";
        var props = new Dictionary<string, object?> { ["textColor"] = evil };
        var script = PanelLogic.HostScript(props, evil, "0.1.0");
        Assert.IsFalse(script.Contains("</script>", StringComparison.OrdinalIgnoreCase), script);
        Assert.IsFalse(script.Contains("<script", StringComparison.OrdinalIgnoreCase), script);
        // It is still exactly one JSON object followed by a semicolon, and it round-trips.
        using var doc = JsonDocument.Parse(script["window.__sidecrabHost = ".Length..^1]);
        Assert.AreEqual(evil, doc.RootElement.GetProperty("props").GetProperty("textColor").GetString());
        Assert.AreEqual(evil, doc.RootElement.GetProperty("props").GetProperty("panelToken").GetString());
    }

    // ---- the zoom correction -----------------------------------------------------------

    [TestMethod]
    public void A_100_percent_monitor_needs_no_correction_and_a_scaled_one_gets_the_ratio()
    {
        Assert.AreEqual(1.0, PanelLogic.CorrectedZoom(1.0, 2560, 2560));
        Assert.AreEqual(0.8, PanelLogic.CorrectedZoom(1.0, 2048, 2560), 1e-9);   // 125 %
        Assert.AreEqual(1.0 / 1.5, PanelLogic.CorrectedZoom(1.0, 1706, 2560) * 2560 / 1706 / 1.5, 1e-9);
        // total on nonsense measurements
        Assert.AreEqual(0.9, PanelLogic.CorrectedZoom(0.9, 0, 2560));
        Assert.AreEqual(0.9, PanelLogic.CorrectedZoom(0.9, 2048, 0));
        Assert.AreEqual(0.0, PanelLogic.CorrectedZoom(0.0, 2048, 2560));
    }

    // ---- settings parsing ---------------------------------------------------------------

    [TestMethod]
    public void Settings_parse_every_key_and_ignore_a_panelToken_in_the_file()
    {
        var path = Path.Combine(Path.GetTempPath(), "sidecrab-panel-" + Guid.NewGuid().ToString("N") + ".json");
        File.WriteAllText(path, """
            { "crabdPort": 2800,
              "display": { "deviceId": "ACME0001", "width": 1920, "height": 480 },
              "props": { "clock24": true, "toastThreshold": 90, "accentColor": "#112233",
                         "panelToken": "NOT-FROM-HERE", "nested": { "x": 1 } } }
            """);
        try
        {
            var logged = new List<string>();
            var s = PanelSettings.Parse(path, logged.Add);
            Assert.AreEqual(2800, s.CrabdPort);
            Assert.AreEqual("ACME0001", s.DisplayDeviceId);
            Assert.AreEqual(1920, s.DisplayWidth);
            Assert.AreEqual(480, s.DisplayHeight);
            Assert.AreEqual(true, s.Props["clock24"]);
            Assert.AreEqual(90L, s.Props["toastThreshold"]);
            Assert.AreEqual("#112233", s.Props["accentColor"]);
            Assert.IsFalse(s.Props.ContainsKey("panelToken"));   // the code comes from panel-token only
            Assert.IsFalse(s.Props.ContainsKey("nested"));        // not a property type iCUE has
            Assert.AreEqual(0, logged.Count);
        }
        finally { File.Delete(path); }
    }

    [TestMethod]
    public void A_missing_or_broken_settings_file_is_the_defaults_and_is_logged_not_thrown()
    {
        var missing = PanelSettings.Parse(Path.Combine(Path.GetTempPath(), "no-such-" + Guid.NewGuid().ToString("N")), _ => { });
        Assert.AreEqual(PanelLogic.DefaultPort, missing.CrabdPort);
        Assert.AreEqual(PanelLogic.DefaultDisplayDeviceId, missing.DisplayDeviceId);
        Assert.AreEqual(0, missing.Props.Count);

        var path = Path.Combine(Path.GetTempPath(), "sidecrab-panel-" + Guid.NewGuid().ToString("N") + ".json");
        File.WriteAllText(path, "{ not json");
        try
        {
            var logged = new List<string>();
            var s = PanelSettings.Parse(path, logged.Add);
            Assert.AreEqual(PanelLogic.DefaultPort, s.CrabdPort);
            Assert.AreEqual(1, logged.Count);
            StringAssert.Contains(logged[0], "ignored");
        }
        finally { File.Delete(path); }
    }

    // ---- the fallback page ---------------------------------------------------------------

    [TestMethod]
    public void The_fallback_page_escapes_what_it_is_told()
    {
        var html = PanelForm.FallbackHtml("http://127.0.0.1:2722/panel/", "<img src=x onerror=alert(1)>");
        Assert.IsFalse(html.Contains("<img", StringComparison.Ordinal));
        StringAssert.Contains(html, "&lt;img");
        StringAssert.Contains(html, "SideCrab companion not reachable");
    }

    // ---- the command line ----------------------------------------------------------------

    [TestMethod]
    public void Options_parse_and_an_unknown_switch_is_an_error()
    {
        var ok = HostOptions.Parse(new[] { "--port", "2800", "--display", "CRXED00", "--devtools-port", "9224", "--windowed" });
        Assert.IsNull(ok.Error);
        Assert.AreEqual(2800, ok.Port);
        Assert.AreEqual("CRXED00", ok.DisplayDeviceId);
        Assert.AreEqual(9224, ok.DevToolsPort);
        Assert.IsTrue(ok.Windowed);
        Assert.IsNotNull(HostOptions.Parse(new[] { "--bogus" }).Error);
        Assert.IsNotNull(HostOptions.Parse(new[] { "--port", "70000" }).Error);
        Assert.IsNotNull(HostOptions.Parse(new[] { "--port" }).Error);
    }
}
