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

    /// <summary>The JSON object out of the injected script, without the frame guard
    /// around it. Built from the same constants the host uses, so a change to the guard
    /// is a change in one place.</summary>
    private static string Payload(string script) =>
        script[PanelLogic.HostScriptPrefix.Length..^PanelLogic.HostScriptSuffix.Length];

    [TestMethod]
    public void The_host_script_is_one_object_with_the_props_and_the_pairing_code()
    {
        var props = new Dictionary<string, object?> { ["clock24"] = true, ["accentColor"] = "#BE7E6E", ["toastThreshold"] = 120L };
        var script = PanelLogic.HostScript(props, "K7QXM-2PDAB\n", "0.1.0");
        // SCA-022: the assignment is inside a top-frame guard, so a child document the
        // page embeds runs the script and assigns nothing.
        StringAssert.StartsWith(script, "if (window.top === window) { window.__sidecrabHost = ");
        Assert.IsTrue(script.EndsWith("; }"), script);
        using var doc = JsonDocument.Parse(Payload(script));
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
        using var doc = JsonDocument.Parse(Payload(script));
        Assert.IsFalse(doc.RootElement.GetProperty("props").TryGetProperty("panelToken", out _));
        var blank = PanelLogic.HostScript(new Dictionary<string, object?>(), "   ", "0.1.0");
        using var doc2 = JsonDocument.Parse(Payload(blank));
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
        using var doc = JsonDocument.Parse(Payload(script));
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
            Assert.IsFalse(s.Props.ContainsKey("nested"));        // not a property type the sheet ever had
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

/// <summary>lane B: the settings a page may write. The validator is the gate between an
/// arbitrary web message and the operator's own settings file, so every one of these was
/// broken on purpose while it was written.</summary>
[TestClass]
public sealed class SettingsBridgeTests
{
    private static JsonElement Props(string json) => JsonDocument.Parse(json).RootElement;

    // ---- the whitelist ---------------------------------------------------------------

    [TestMethod]
    public void Every_whitelisted_key_survives_with_its_own_type()
    {
        var clean = PanelLogic.ValidateSettingsProps(Props("""
            { "clock24": true, "alertFlash": false, "crabStyle": true, "touchDiag": false,
              "chime": true, "textColor": "#ede7df", "accentColor": "#BE7E6E",
              "backgroundColor": "#0F0E0D", "transparency": 12, "chimeVolume": 60 }
            """));
        Assert.AreEqual(10, clean.Count);
        Assert.AreEqual(true, clean["clock24"]);
        Assert.AreEqual(false, clean["alertFlash"]);
        Assert.AreEqual("#EDE7DF", clean["textColor"]);     // upper-cased on the way in
        Assert.AreEqual(12L, clean["transparency"]);
        Assert.AreEqual(60L, clean["chimeVolume"]);
    }

    [TestMethod]
    public void Unknown_keys_are_dropped()
    {
        var clean = PanelLogic.ValidateSettingsProps(Props("""
            { "clock24": true, "quietStart": "22:00", "recapRepos": ["C:\\x"],
              "allowReply": true, "__proto__": {"x": 1}, "": true }
            """));
        Assert.AreEqual(1, clean.Count);
        Assert.IsTrue(clean.ContainsKey("clock24"));
    }

    [TestMethod]
    public void The_three_keys_a_page_may_never_write_are_not_on_the_list()
    {
        // panelToken is the pairing code; crabdPort and display are how this host finds
        // the companion and the glass. None of them is a SETTING the page may move.
        var clean = PanelLogic.ValidateSettingsProps(Props("""
            { "panelToken": "AAAAA-AAAAA", "crabdPort": 9999,
              "display": { "deviceId": "OTHER" }, "devtoolsPort": 9224 }
            """));
        Assert.AreEqual(0, clean.Count);
        CollectionAssert.DoesNotContain(PanelLogic.SettingsBooleans, "panelToken");
        CollectionAssert.DoesNotContain(PanelLogic.SettingsPercents, "crabdPort");
    }

    [TestMethod]
    public void Wrong_types_are_dropped_and_never_coerced()
    {
        var clean = PanelLogic.ValidateSettingsProps(Props("""
            { "clock24": "true", "alertFlash": 1, "chime": null, "touchDiag": [],
              "transparency": "40", "chimeVolume": {}, "accentColor": 16777215 }
            """));
        Assert.AreEqual(0, clean.Count, "a coerced setting is a value nobody chose");
    }

    [TestMethod]
    public void A_colour_must_be_six_hex_digits()
    {
        foreach (var bad in new[] { "\"#FFF\"", "\"red\"", "\"#GGGGGG\"", "\"#FFFFFFFF\"",
                                    "\"rgb(1,2,3)\"", "\"\"", "\"   \"", "\"#12345\"",
                                    "\"#123456 ; background: url(x)\"" })
        {
            var clean = PanelLogic.ValidateSettingsProps(Props($"{{ \"accentColor\": {bad} }}"));
            Assert.AreEqual(0, clean.Count, bad);
        }
        foreach (var good in new[] { "#123456", " #abcdef ", "ABCDEF" })
        {
            var clean = PanelLogic.ValidateSettingsProps(Props($"{{ \"accentColor\": \"{good}\" }}"));
            Assert.AreEqual(1, clean.Count, good);
            StringAssert.Matches((string)clean["accentColor"]!, new System.Text.RegularExpressions.Regex("^#[0-9A-F]{6}$"));
        }
    }

    [TestMethod]
    public void Percentages_are_clamped_and_rounded_never_refused()
    {
        // Clamped rather than dropped: a slider that arrives out of range is a value to
        // correct, and dropping it would leave the operator's own move unsaved.
        Assert.AreEqual(100L, PanelLogic.ValidateSettingsProps(Props("""{"transparency": 400}""")) ["transparency"]);
        Assert.AreEqual(0L, PanelLogic.ValidateSettingsProps(Props("""{"transparency": -7}""")) ["transparency"]);
        Assert.AreEqual(61L, PanelLogic.ValidateSettingsProps(Props("""{"chimeVolume": 60.7}""")) ["chimeVolume"]);
        Assert.AreEqual(0L, PanelLogic.ValidateSettingsProps(Props("""{"chimeVolume": 0}""")) ["chimeVolume"]);
    }

    [TestMethod]
    public void A_body_that_is_not_an_object_is_empty_and_not_an_exception()
    {
        foreach (var raw in new[] { "[]", "\"props\"", "5", "null", "true" })
            Assert.AreEqual(0, PanelLogic.ValidateSettingsProps(Props(raw)).Count, raw);
    }

    [TestMethod]
    public void An_empty_result_is_what_the_caller_refuses_to_write_on()
    {
        // The rule the caller keeps, pinned here because it is the only thing that stops
        // a page rewriting the file with rubbish: nothing survived, so nothing is stored.
        Assert.AreEqual(0, PanelLogic.ValidateSettingsProps(Props("""{"nope": 1}""")).Count);
    }

    // ---- the merge -------------------------------------------------------------------

    [TestMethod]
    public void Every_other_key_in_the_file_survives_a_save()
    {
        var existing = """
            { "crabdPort": 2800, "devtoolsPort": 9224,
              "display": { "deviceId": "CRXED00", "width": 2560, "height": 720 },
              "props": { "clock24": false, "quietStart": "22:00" } }
            """;
        var clean = PanelLogic.ValidateSettingsProps(Props("""{"clock24": true, "chimeVolume": 40}"""));
        using var doc = JsonDocument.Parse(PanelLogic.MergeSettingsJson(existing, clean));
        var root = doc.RootElement;
        Assert.AreEqual(2800, root.GetProperty("crabdPort").GetInt32());
        Assert.AreEqual(9224, root.GetProperty("devtoolsPort").GetInt32());
        Assert.AreEqual("CRXED00", root.GetProperty("display").GetProperty("deviceId").GetString());
        Assert.AreEqual(720, root.GetProperty("display").GetProperty("height").GetInt32());
        var props = root.GetProperty("props");
        Assert.IsTrue(props.GetProperty("clock24").GetBoolean(), "the saved value wins");
        Assert.AreEqual(40, props.GetProperty("chimeVolume").GetInt32());
        Assert.AreEqual("22:00", props.GetProperty("quietStart").GetString(),
            "a prop this sheet does not edit is not deleted by editing the ones it does");
    }

    [TestMethod]
    public void A_pairing_code_in_the_props_is_dropped_on_the_way_through()
    {
        var existing = """{ "props": { "panelToken": "AAAAA-AAAAA", "clock24": false } }""";
        var clean = PanelLogic.ValidateSettingsProps(Props("""{"clock24": true}"""));
        using var doc = JsonDocument.Parse(PanelLogic.MergeSettingsJson(existing, clean));
        Assert.IsFalse(doc.RootElement.GetProperty("props").TryGetProperty("panelToken", out _),
            "the code's one home is panel-token; a copy here is a second place to leak it");
    }

    [TestMethod]
    public void An_unreadable_or_missing_file_becomes_one_carrying_only_the_props()
    {
        foreach (var existing in new string?[] { null, "", "   ", "{ not json", "[1,2,3]", "42" })
        {
            var clean = PanelLogic.ValidateSettingsProps(Props("""{"chime": false}"""));
            using var doc = JsonDocument.Parse(PanelLogic.MergeSettingsJson(existing, clean));
            Assert.IsFalse(doc.RootElement.GetProperty("props").GetProperty("chime").GetBoolean(),
                existing ?? "null");
            Assert.AreEqual(1, doc.RootElement.EnumerateObject().Count(), existing ?? "null");
        }
    }

    [TestMethod]
    public void The_merged_file_is_what_PanelSettings_reads_back()
    {
        // The round trip is the thing that matters: a file this host writes and cannot
        // parse would silently lose the port and the display on the next start.
        var existing = """{ "crabdPort": 2800, "display": { "deviceId": "CRXED00" } }""";
        var clean = PanelLogic.ValidateSettingsProps(Props(
            """{"clock24": true, "accentColor": "#2E7FF2", "transparency": 25, "chime": false}"""));
        var path = Path.Combine(Path.GetTempPath(), "sidecrab-laneb-" + Guid.NewGuid().ToString("N") + ".json");
        File.WriteAllText(path, PanelLogic.MergeSettingsJson(existing, clean));
        try
        {
            var s = PanelSettings.Parse(path, _ => Assert.Fail("the host could not read its own file"));
            Assert.AreEqual(2800, s.CrabdPort);
            Assert.AreEqual("CRXED00", s.DisplayDeviceId);
            Assert.AreEqual(true, s.Props["clock24"]);
            Assert.AreEqual("#2E7FF2", s.Props["accentColor"]);
            // A whole number must come back a long, or the widget reads "25" as "25.0".
            Assert.AreEqual(25L, s.Props["transparency"]);
            Assert.AreEqual(false, s.Props["chime"]);
        }
        finally { File.Delete(path); }
    }

    // ---- the source check ------------------------------------------------------------

    [TestMethod]
    public void Only_the_panel_page_may_send_a_web_message()
    {
        // WebMessageReceived fires for every frame, and Source is the only thing that
        // says which one. The handler reuses the navigation lock rather than carrying a
        // second rule that can drift from it.
        var panel = PanelLogic.PanelUrl(2722);
        Assert.IsTrue(PanelLogic.IsAllowedNavigation("http://127.0.0.1:2722/panel/", panel));
        foreach (var source in new[] { "http://evil.example/", "http://127.0.0.1:2722/v1/state",
                                       "https://127.0.0.1:2722/panel/", "file:///C:/x.html", "" })
            Assert.IsFalse(PanelLogic.IsAllowedNavigation(source, panel), source);
    }

    // ---- lane E: the single-instance mutex --------------------------------------------

    [TestMethod]
    public void The_pinned_kiosk_keeps_the_bare_mutex_name_and_windowed_takes_its_own()
    {
        // The literal is repeated here rather than read from the constant: this name is a
        // compatibility surface with every host already running and with the scheduled
        // task's IgnoreNew policy, and a test that reads the constant would agree with a
        // rename that silently un-guarded the Edge.
        Assert.AreEqual(@"Local\SideCrab.Panel", PanelLogic.MutexName(windowed: false));
        Assert.AreEqual(@"Local\SideCrab.Panel.windowed", PanelLogic.MutexName(windowed: true));
        Assert.AreNotEqual(PanelLogic.MutexName(true), PanelLogic.MutexName(false));

        // The mode comes from the one place that parses it, so a dev host and the pinned
        // one cannot end up on the same name through a second copy of the rule.
        Assert.IsTrue(HostOptions.Parse(new[] { "--windowed" }).Windowed);
        Assert.IsFalse(HostOptions.Parse(Array.Empty<string>()).Windowed);
        Assert.IsFalse(HostOptions.Parse(new[] { "--port", "2722" }).Windowed);
    }

    // ---- lane E: bring a session to the front -----------------------------------------

    private static JsonElement Json(string s) => JsonDocument.Parse(s).RootElement.Clone();

    private static PanelLogic.FocusRequest Req(string title, string cwd = "", string repo = "") =>
        new("11111111-2222-3333-4444-555555555555", title, cwd, repo);

    private static PanelLogic.WindowCandidate Win(long h, string proc, string title, string cls = "", bool min = false) =>
        new(h, proc, title, cls, min);

    [TestMethod]
    public void A_focus_message_needs_a_session_id_and_nothing_else_is_read()
    {
        Assert.IsNull(PanelLogic.ValidateFocusRequest(Json("{}")));
        Assert.IsNull(PanelLogic.ValidateFocusRequest(Json("""{"sessionId":""}""")));
        Assert.IsNull(PanelLogic.ValidateFocusRequest(Json("""{"sessionId":42}""")));
        Assert.IsNull(PanelLogic.ValidateFocusRequest(Json("[1,2,3]")));

        // The four allowlisted keys survive; a wrong type is dropped to empty rather than
        // coerced, and a key nobody named is not read at all.
        var r = PanelLogic.ValidateFocusRequest(Json(
            """{"sessionId":"abc","title":"SideCrab Panel","cwd":"C:\\Dev\\x","repo":3,"hwnd":66666,"command":"calc.exe"}"""));
        Assert.IsNotNull(r);
        Assert.AreEqual("abc", r.SessionId);
        Assert.AreEqual("SideCrab Panel", r.Title);
        Assert.AreEqual(@"C:\Dev\x", r.Cwd);
        Assert.AreEqual("", r.Repo);
    }

    [TestMethod]
    public void A_focus_message_cannot_forge_a_line_in_the_panel_log()
    {
        // panel.log is the only account of what the host did. A newline inside a title
        // would let a page write a second, invented entry under the host's own timestamp.
        var r = PanelLogic.ValidateFocusRequest(Json(
            """{"sessionId":"a\u0000b","title":"ok\n2026-01-01 00:00:00 focus granted\r\tx"}"""));
        Assert.IsNotNull(r);
        Assert.AreEqual("ab", r.SessionId);
        Assert.AreEqual("ok2026-01-01 00:00:00 focus grantedx", r.Title);
        Assert.IsFalse(r.Title.Contains('\n'));

        // And it cannot be made arbitrarily long either.
        var big = PanelLogic.ValidateFocusRequest(Json(
            "{\"sessionId\":\"" + new string('s', 500) + "\",\"title\":\"" + new string('t', 900) + "\"}"));
        Assert.IsNotNull(big);
        Assert.AreEqual(PanelLogic.FocusIdMax, big.SessionId.Length);
        Assert.AreEqual(PanelLogic.FocusTextMax, big.Title.Length);
    }

    [TestMethod]
    public void The_cwd_leaf_is_the_project_folder_and_never_a_drive()
    {
        Assert.AreEqual("sidecrab", PanelLogic.CwdLeaf(@"C:\Dev\sidecrab"));
        Assert.AreEqual("sidecrab", PanelLogic.CwdLeaf(@"C:\Dev\sidecrab\"));
        Assert.AreEqual("sidecrab", PanelLogic.CwdLeaf("/home/x/sidecrab"));
        Assert.AreEqual("", PanelLogic.CwdLeaf(@"C:\"));
        Assert.AreEqual("", PanelLogic.CwdLeaf("C:"));
        Assert.AreEqual("", PanelLogic.CwdLeaf(null));
        Assert.AreEqual("", PanelLogic.CwdLeaf("   "));
    }

    [TestMethod]
    public void An_exact_title_match_beats_a_folder_name_and_both_beat_the_desktop_app()
    {
        var req = Req("Panel host spike", @"C:\Dev\sidecrab", "sidecrab");
        var term = Win(1, "WindowsTerminal", "Panel host spike", "CASCADIA_HOSTING_WINDOW_CLASS");
        var app = Win(2, "claude", "Claude");
        var shell = Win(3, "pwsh", "pwsh in sidecrab", "ConsoleWindowClass");

        Assert.AreEqual(PanelLogic.FocusScoreTitleExact, PanelLogic.ScoreWindow(term, req));
        // The app scores NOTHING on evidence; it is the fallback, never a competitor.
        Assert.AreEqual(0, PanelLogic.ScoreWindow(app, req));
        // The repo name and the folder name are both in that shell's title.
        Assert.AreEqual(PanelLogic.FocusScoreRepo + PanelLogic.FocusScoreLeaf, PanelLogic.ScoreWindow(shell, req));

        var pick = PanelLogic.SelectWindow(new[] { app, shell, term }, req);
        Assert.AreEqual("matched", pick.Reason);
        Assert.AreEqual(1L, pick.Window!.Handle);

        // A real console title carries an elevation prefix, so the CONTAINS branch is
        // what a live match actually goes through: measured 2026-09-21 as
        // "Administrator:  lane E focus probe session ".
        var elevated = Win(4, "cmd", "Administrator:  Panel host spike ", "ConsoleWindowClass");
        Assert.AreEqual(PanelLogic.FocusScoreTitleContains, PanelLogic.ScoreWindow(elevated, req));
    }

    [TestMethod]
    public void The_desktop_app_is_a_labelled_fallback_and_never_a_silent_success()
    {
        // The defect this test exists for, found by a live run on 2026-09-21 and not by
        // reading the code: while the Claude process scored a flat 30, a request for a
        // session NOTHING answered to still brought the app forward and reported success,
        // so "No window found for this session" could not fire while the app was running.
        var nothing = Req("zzz no such window zzz", @"C:\zzz\nowhere", "zzz-nothing");
        var app = Win(1, "claude", "Claude");
        var other = Win(2, "explorer", "Program Manager");

        var pick = PanelLogic.SelectWindow(new[] { app, other }, nothing);
        Assert.AreEqual("desktop-app", pick.Reason);
        Assert.AreEqual(1L, pick.Window!.Handle);
        Assert.AreEqual(0, pick.Score);

        // Without the app there is nothing to fall back to, and it says so. This is the
        // half that used to be unreachable.
        Assert.AreEqual("no-match", PanelLogic.SelectWindow(new[] { other }, nothing).Reason);

        // Evidence anywhere else wins over the fallback, so the app can never swallow a
        // terminal that really is the session.
        var named = Req("Panel host spike", @"C:\Dev\sidecrab", "sidecrab");
        var term = Win(3, "pwsh", "Panel host spike", "ConsoleWindowClass");
        Assert.AreEqual(3L, PanelLogic.SelectWindow(new[] { app, term }, named).Window!.Handle);

        // Two app windows and no evidence is the same coin toss as two consoles.
        Assert.AreEqual("ambiguous",
            PanelLogic.SelectWindow(new[] { app, Win(4, "claude", "Claude") }, nothing).Reason);
    }

    [TestMethod]
    public void A_short_session_title_is_not_evidence_on_its_own()
    {
        // "IT" appears in half the title bars on a working desktop; a substring match on
        // it would point the operator at whatever happened to be enumerated first.
        var req = Req("IT", @"C:\IT", "it");
        var term = Win(1, "pwsh", "IT department", "ConsoleWindowClass");
        Assert.AreEqual(0, PanelLogic.ScoreWindow(term, req));
        Assert.AreEqual("no-match", PanelLogic.SelectWindow(new[] { term }, req).Reason);

        // Same title at full length is a match, on the same window.
        var full = Req("IT department", @"C:\IT", "it");
        Assert.AreEqual(PanelLogic.FocusScoreTitleExact, PanelLogic.ScoreWindow(term, full));

        // SCA-023: the same full-length title on a browser is still nothing. The window
        // was RETYPED here, from chrome to a console, because a browser can no longer
        // score at all - see the SCA-023 test for why that changed.
        Assert.AreEqual(0, PanelLogic.ScoreWindow(Win(2, "chrome", "IT department"), full));
    }

    [TestMethod]
    public void The_measured_four_identical_console_windows_are_refused_not_guessed()
    {
        // Measured on this PC 2026-09-21: four visible windows titled "pwsh in IT", none
        // of them a Claude Code session. A coin toss between them is wrong three times in
        // four and takes the keyboard away from whatever was being typed into.
        var req = Req("Some long session title", @"C:\Dev\sidecrab", "sidecrab");
        var a = Win(1, "pwsh", "pwsh in sidecrab", "ConsoleWindowClass");
        var b = Win(2, "pwsh", "pwsh in sidecrab", "ConsoleWindowClass");
        var pick = PanelLogic.SelectWindow(new[] { a, b }, req);
        Assert.AreEqual("ambiguous", pick.Reason);
        Assert.IsNull(pick.Window);

        // One of them alone is not ambiguous, so the tie is what is refused, not the
        // weak score: a test that refused both ways would report success forever.
        Assert.AreEqual("matched", PanelLogic.SelectWindow(new[] { a }, req).Reason);
    }

    [TestMethod]
    public void Nothing_to_focus_says_so_rather_than_picking_something()
    {
        var req = Req("SideCrab Panel Windows app", @"C:\Dev\sidecrab", "sidecrab");
        Assert.AreEqual("no-window", PanelLogic.SelectWindow(Array.Empty<PanelLogic.WindowCandidate>(), req).Reason);
        Assert.AreEqual("no-request", PanelLogic.SelectWindow(new[] { Win(1, "claude", "Claude") }, null).Reason);
        Assert.AreEqual("no-match", PanelLogic.SelectWindow(
            new[] { Win(1, "explorer", "Program Manager"), Win(2, "chrome", "Inbox") }, req).Reason);
        // An untitled window scores nothing even when its process is the desktop app.
        Assert.AreEqual(0, PanelLogic.ScoreWindow(Win(3, "claude", "   "), req));
    }

    [TestMethod]
    public void The_repo_and_folder_scores_are_for_terminals_only()
    {
        // A browser tab or an editor carrying the repo name is not the session.
        var req = Req("Long enough session title", @"C:\Dev\sidecrab", "sidecrab");
        Assert.AreEqual(0, PanelLogic.ScoreWindow(Win(1, "chrome", "sidecrab - GitHub"), req));
        Assert.AreEqual(0, PanelLogic.ScoreWindow(Win(2, "Code", "PanelForm.cs - sidecrab"), req));
        Assert.AreEqual(0, PanelLogic.ScoreWindow(Win(3, "claude", "Claude - sidecrab"), req));

        // The class name is enough on its own: a console hosted by a name this list does
        // not carry is still a console.
        Assert.IsTrue(PanelLogic.IsTerminalWindow("someshell", "ConsoleWindowClass"));
        Assert.IsFalse(PanelLogic.IsTerminalWindow("chrome", "Chrome_WidgetWin_1"));
    }
}
