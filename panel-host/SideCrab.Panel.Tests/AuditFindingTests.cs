using System.Drawing;
using System.Text.Json;
using SideCrab.Panel;

namespace SideCrab.Panel.Tests;

/// <summary>One test per finding closed in the standalone audit wave, each written from
/// that finding's own acceptance test. The finding id is in the method name so a failure
/// names what regressed.</summary>
[TestClass]
public sealed class AuditFindingTests
{
    private static readonly Uri Panel = PanelLogic.PanelUrl(2722);

    private static DisplayInfo Edge(string uid = "UID4358", bool primary = false, int w = 2560, int h = 720) =>
        new(@"\\.\DISPLAY1", $@"\\?\DISPLAY#CRXED00#5&a4ae9a5&0&{uid}#{{guid}}",
            new Rectangle(1228, 2160, w, h), primary, 96);

    private static DisplayInfo Monitor(string name, string model, string uid, Rectangle bounds,
                                       bool primary = false, uint dpi = 96) =>
        new(name, $@"\\?\DISPLAY#{model}#5&a4ae9a5&0&{uid}#{{guid}}", bounds, primary, dpi);

    // ---- SCA-014: the size fallback may not pick an unrelated primary -------------------

    [TestMethod]
    public void SCA_014_size_only_fallback_never_lands_on_the_primary()
    {
        // The audit's own reproduction: an unrelated primary monitor named AUDIT_NOT_EDGE
        // that happens to be 2560x720. It was selected, and a borderless topmost window
        // would have covered the operator's desktop.
        var notEdge = Monitor(@"\\.\DISPLAY2", "AUDIT_NOT_EDGE", "UID1", new Rectangle(0, 0, 2560, 720), primary: true);
        var choice = PanelLogic.ChooseDisplay(new[] { notEdge }, "CRXED00", 2560, 720);
        Assert.IsNull(choice.Display);
        Assert.AreEqual(PanelLogic.DisplayReasonSizePrimary, choice.Reason);
    }

    [TestMethod]
    public void SCA_014_an_explicitly_named_target_still_works_even_when_it_is_primary()
    {
        // The other half, and the one a fix could break: naming a monitor by its device id
        // is an instruction, not a guess, and a single-monitor PC where the Edge IS the
        // primary must still light up.
        var edgeIsPrimary = Edge(primary: true);
        var choice = PanelLogic.ChooseDisplay(new[] { edgeIsPrimary }, "CRXED00", 2560, 720);
        Assert.AreSame(edgeIsPrimary, choice.Display);
        Assert.AreEqual(PanelLogic.DisplayReasonId, choice.Reason);
    }

    [TestMethod]
    public void SCA_014_two_displays_of_the_target_size_are_refused_not_guessed()
    {
        var a = Monitor(@"\\.\DISPLAY1", "ACME0001", "UID1", new Rectangle(-2560, 0, 2560, 720));
        var b = Monitor(@"\\.\DISPLAY3", "ACME0002", "UID2", new Rectangle(0, 2160, 2560, 720));
        var primary = Monitor(@"\\.\DISPLAY2", "GSMC4B8", "UID3", new Rectangle(0, 0, 5120, 2160), primary: true);

        var ambiguous = PanelLogic.ChooseDisplay(new[] { primary, a, b }, "CRXED00", 2560, 720);
        Assert.IsNull(ambiguous.Display);
        Assert.AreEqual(PanelLogic.DisplayReasonSizeAmbiguous, ambiguous.Reason);

        // One of them alone is not ambiguous, so it is the TIE that is refused and not
        // the size fallback itself: a fix that refused both ways would hide the Edge.
        var single = PanelLogic.ChooseDisplay(new[] { primary, a }, "CRXED00", 2560, 720);
        Assert.AreSame(a, single.Display);
        Assert.AreEqual(PanelLogic.DisplayReasonSize, single.Reason);
    }

    // ---- SCA-021 / C2: exactly one typed terminal reply per accepted request ------------

    private static JsonElement Json(string s) => JsonDocument.Parse(s).RootElement.Clone();

    private static string Serialize(object o) => JsonSerializer.Serialize(o);

    [TestMethod]
    public void SCA_021_every_reply_carries_the_request_it_answers()
    {
        var info = Json(Serialize(PanelLogic.HostInfoReply("r-1", "0.4.0", 4242, "2026-09-21T10:00:00",
                                                           @"C:\x\panel-settings.json", hasToken: true)));
        Assert.AreEqual("host-info", info.GetProperty("type").GetString());
        Assert.AreEqual("r-1", info.GetProperty("requestId").GetString());
        Assert.AreEqual(4242, info.GetProperty("pid").GetInt32());
        Assert.AreEqual("2026-09-21T10:00:00", info.GetProperty("startedAt").GetString());
        Assert.IsTrue(info.GetProperty("hasToken").GetBoolean());
        var caps = info.GetProperty("capabilities");
        Assert.IsTrue(caps.GetProperty("saveSettings").GetBoolean());
        Assert.IsTrue(caps.GetProperty("focusSession").GetBoolean());
        Assert.IsTrue(caps.GetProperty("pickDisplay").GetBoolean());
        // The pairing code has ONE home. host-info says whether there is one, never what.
        Assert.IsFalse(info.TryGetProperty("panelToken", out _));

        var saved = Json(Serialize(PanelLogic.SettingsReply("r-2", true, null)));
        Assert.AreEqual("settings-result", saved.GetProperty("type").GetString());
        Assert.AreEqual("r-2", saved.GetProperty("requestId").GetString());
        Assert.IsTrue(saved.GetProperty("ok").GetBoolean());

        // A failure is a terminal answer too. The sheet used to sit on "saving" for sixty
        // measured seconds with the bridge connected and nothing ever arriving.
        var failed = Json(Serialize(PanelLogic.SettingsReply("r-3", false, "the settings file could not be written")));
        Assert.IsFalse(failed.GetProperty("ok").GetBoolean());
        Assert.AreEqual("the settings file could not be written", failed.GetProperty("error").GetString());

        var refused = Json(Serialize(PanelLogic.FocusReply("r-4", "", false, "invalid-request", null)));
        Assert.AreEqual("focus-result", refused.GetProperty("type").GetString());
        Assert.AreEqual("r-4", refused.GetProperty("requestId").GetString());
        Assert.IsFalse(refused.GetProperty("ok").GetBoolean());
        Assert.AreEqual("invalid-request", refused.GetProperty("reason").GetString());
    }

    [TestMethod]
    public void SCA_021_a_legacy_page_without_a_request_id_is_answered_not_refused()
    {
        var legacy = PanelLogic.ParseBridgeRequest(Json("""{"type":"settings","props":{}}"""));
        Assert.IsNotNull(legacy);
        Assert.AreEqual("settings", legacy!.Type);
        Assert.IsNull(legacy.RequestId);

        var reply = Json(Serialize(PanelLogic.SettingsReply(legacy.RequestId, false, "nothing in it passed the whitelist")));
        Assert.AreEqual(JsonValueKind.Null, reply.GetProperty("requestId").ValueKind);
        Assert.IsFalse(reply.GetProperty("ok").GetBoolean());
    }

    [TestMethod]
    public void SCA_021_a_request_id_is_bounded_and_never_truncated()
    {
        Assert.AreEqual("abc", PanelLogic.RequestId(Json("""{"requestId":"  abc  "}""")));
        Assert.IsNull(PanelLogic.RequestId(Json("""{"requestId":""}""")));
        Assert.IsNull(PanelLogic.RequestId(Json("""{"requestId":7}""")));
        Assert.IsNull(PanelLogic.RequestId(Json("""{"requestId":"a\nb"}""")));
        Assert.IsNull(PanelLogic.RequestId(Json("{}")));
        // Over the cap is null, NOT the first 64 characters: a truncated id would
        // correlate with the wrong attempt, which is worse than no id at all.
        var tooLong = new string('x', PanelLogic.RequestIdMax + 1);
        Assert.IsNull(PanelLogic.RequestId(Json($$"""{"requestId":"{{tooLong}}"}""")));
        Assert.AreEqual(new string('x', PanelLogic.RequestIdMax),
                        PanelLogic.RequestId(Json($$"""{"requestId":"{{new string('x', PanelLogic.RequestIdMax)}}"}""")));
    }

    [TestMethod]
    public void SCA_021_a_late_reply_is_known_to_be_superseded()
    {
        var gate = new PanelLogic.BridgeGate();
        gate.Accepted(PanelLogic.ChannelSettings, "first");
        Assert.IsTrue(gate.IsCurrent(PanelLogic.ChannelSettings, "first"));

        gate.Accepted(PanelLogic.ChannelSettings, "second");
        Assert.IsFalse(gate.IsCurrent(PanelLogic.ChannelSettings, "first"));
        Assert.IsTrue(gate.IsCurrent(PanelLogic.ChannelSettings, "second"));

        // Channels are independent: a newer settings save says nothing about a focus
        // request the operator is still waiting on.
        Assert.IsTrue(gate.IsCurrent(PanelLogic.ChannelFocus, "focus-1"));
    }

    // ---- SCA-022: the pairing code is for the top-level panel only ---------------------

    [TestMethod]
    public void SCA_022_the_injected_script_assigns_nothing_in_a_child_frame()
    {
        var script = PanelLogic.HostScript(new Dictionary<string, object?>(), "K7QXM-2PDAB", "0.4.0");
        StringAssert.Contains(script, "window.top === window");
        // The guard is BEFORE the assignment, not a check the assignment could outrun.
        Assert.IsTrue(script.IndexOf("window.top === window", StringComparison.Ordinal)
                      < script.IndexOf("__sidecrabHost", StringComparison.Ordinal), script);
        StringAssert.Contains(script, "K7QXM-2PDAB");
    }

    [TestMethod]
    public void SCA_022_a_child_frame_may_only_load_the_panels_own_origin()
    {
        // Stricter than the top-level lock: about:blank and the data: fallback are the
        // HOST's own two documents and a frame has no business at either.
        Assert.IsFalse(PanelLogic.IsAllowedFrameNavigation("about:blank", Panel));
        Assert.IsFalse(PanelLogic.IsAllowedFrameNavigation("data:text/html,<p>x</p>", Panel));
        Assert.IsFalse(PanelLogic.IsAllowedFrameNavigation("http://evil.example/", Panel));
        Assert.IsFalse(PanelLogic.IsAllowedFrameNavigation("http://127.0.0.1:2722/v1/state", Panel));
        Assert.IsFalse(PanelLogic.IsAllowedFrameNavigation("file:///C:/x.html", Panel));
        Assert.IsFalse(PanelLogic.IsAllowedFrameNavigation(null, Panel));
        Assert.IsTrue(PanelLogic.IsAllowedFrameNavigation("http://127.0.0.1:2722/panel/frame.html", Panel));

        // The top-level lock is unchanged, so the host's own fallback page still loads.
        Assert.IsTrue(PanelLogic.IsAllowedNavigation("about:blank", Panel));
        Assert.IsTrue(PanelLogic.IsAllowedNavigation("data:text/html,<p>x</p>", Panel));
    }

    // ---- SCA-023: an unrelated exact title is not evidence ------------------------------

    [TestMethod]
    public void SCA_023_a_nonterminal_exact_title_cannot_beat_the_session_terminal()
    {
        // The audit's reproduction, verbatim: a session named "Release notes", a Notepad
        // window with exactly that title, and the real Windows Terminal carrying it plus
        // the host suffix. It chose Notepad at 100 over the terminal at 60.
        var req = new PanelLogic.FocusRequest("11111111-2222-3333-4444-555555555555",
                                              "Release notes", @"C:\Dev\sidecrab", "sidecrab");
        var notepad = new PanelLogic.WindowCandidate(1, "notepad", "Release notes", "Notepad", false);
        var terminal = new PanelLogic.WindowCandidate(2, "WindowsTerminal", "Release notes - Windows Terminal",
                                                      "CASCADIA_HOSTING_WINDOW_CLASS", false);

        Assert.AreEqual(0, PanelLogic.ScoreWindow(notepad, req));
        Assert.AreEqual(PanelLogic.FocusScoreTitleContains, PanelLogic.ScoreWindow(terminal, req));

        var pick = PanelLogic.SelectWindow(new[] { notepad, terminal }, req);
        Assert.AreEqual("matched", pick.Reason);
        Assert.AreEqual(2L, pick.Window!.Handle);

        // Notepad alone is not a weaker answer, it is no answer: the foreground it would
        // take belongs to whatever the operator was typing into.
        Assert.AreEqual("no-match", PanelLogic.SelectWindow(new[] { notepad }, req).Reason);
    }

    [TestMethod]
    public void SCA_023_two_terminals_with_the_same_title_are_still_refused()
    {
        var req = new PanelLogic.FocusRequest("11111111-2222-3333-4444-555555555555",
                                              "Release notes", @"C:\Dev\sidecrab", "sidecrab");
        var a = new PanelLogic.WindowCandidate(1, "pwsh", "Release notes", "ConsoleWindowClass", false);
        var b = new PanelLogic.WindowCandidate(2, "wt", "Release notes", "CASCADIA_HOSTING_WINDOW_CLASS", false);
        var pick = PanelLogic.SelectWindow(new[] { a, b }, req);
        Assert.AreEqual("ambiguous", pick.Reason);
        Assert.IsNull(pick.Window);
    }

    // ---- SCA-024: kiosk and windowed do not share a WebView2 profile --------------------

    [TestMethod]
    public void SCA_024_the_two_modes_get_separate_user_data_folders()
    {
        const string local = @"C:\Users\x\AppData\Local";
        var kiosk = PanelLogic.WebViewUserDataDir(local, PanelLogic.ProfileName(windowed: false));
        var windowed = PanelLogic.WebViewUserDataDir(local, PanelLogic.ProfileName(windowed: true));
        Assert.AreNotEqual(kiosk, windowed);
        // The kiosk folder is unchanged byte-for-byte: a running panel must not lose its
        // profile to this fix and re-do first-run on the glass.
        Assert.AreEqual(Path.Combine(local, "SideCrab", "Panel", "WebView2"), kiosk);
        Assert.AreEqual(Path.Combine(local, "SideCrab", "Panel", "WebView2-windowed"), windowed);
    }

    [TestMethod]
    public void SCA_024_a_named_profile_is_its_own_folder_and_its_own_log()
    {
        const string local = @"C:\Users\x\AppData\Local";
        var qa = PanelLogic.ProfileName(windowed: true, profile: "QA Run 2");
        Assert.AreEqual("qarun2", qa);
        Assert.AreEqual(Path.Combine(local, "SideCrab", "Panel", "WebView2-qarun2"),
                        PanelLogic.WebViewUserDataDir(local, qa));
        Assert.AreEqual("panel-qarun2.log", PanelLogic.LogFileName(windowed: true, profile: "QA Run 2"));

        // The name reaches a path and a file name, so it is reduced, not trusted.
        // A traversal reduces to one harmless segment: the separators and the dots are not
        // in the allowed set, so nothing that could leave the folder survives.
        Assert.AreEqual("windows", PanelLogic.SafeProfileName(@"..\..\windows"));
        Assert.AreEqual("profile", PanelLogic.SafeProfileName(@"..\.."));
        Assert.AreEqual("ab-cd", PanelLogic.SafeProfileName(@"AB-c:\d"));
        Assert.AreEqual("a_b-1", PanelLogic.SafeProfileName("A_b-1"));
        var opts = HostOptions.Parse(new[] { "--profile", "dev" });
        Assert.AreEqual("dev", opts.Profile);
        Assert.IsNull(HostOptions.Parse(new[] { "--profile" }).Profile);
        Assert.IsNotNull(HostOptions.Parse(new[] { "--profile" }).Error);
    }

    [TestMethod]
    public void SCA_024_a_named_profile_can_run_beside_an_installed_kiosk()
    {
        // Measured on this PC 2026-09-21: separate folders and separate logs are not
        // isolation while the mutex refuses to start the second host, and the installed
        // kiosk holds the bare name. A QA host that exits 3 diagnoses nothing.
        Assert.AreEqual(@"Local\SideCrab.Panel.laneh", PanelLogic.MutexName(windowed: false, profile: "laneh"));
        Assert.AreEqual(@"Local\SideCrab.Panel.laneh", PanelLogic.MutexName(windowed: true, profile: "laneh"));
        Assert.AreNotEqual(PanelLogic.MutexName(false), PanelLogic.MutexName(false, "laneh"));

        // The two unnamed names are unchanged, because the installed kiosk and anything
        // already running exclude each other through them.
        Assert.AreEqual(@"Local\SideCrab.Panel", PanelLogic.MutexName(windowed: false));
        Assert.AreEqual(@"Local\SideCrab.Panel.windowed", PanelLogic.MutexName(windowed: true));
    }

    // ---- SCA-028: a successful navigation to something else is not "loaded" -------------

    [TestMethod]
    public void SCA_028_about_blank_is_allowed_to_load_but_is_not_the_panel()
    {
        Assert.IsTrue(PanelLogic.IsAllowedNavigation("about:blank", Panel));
        Assert.IsFalse(PanelLogic.IsPanelDocument("about:blank", Panel));

        // The host's own fallback page is a data: document and is likewise not the panel.
        // It is excluded from the retry by _showingFallback, not by this predicate.
        Assert.IsFalse(PanelLogic.IsPanelDocument("data:text/html,<p>fallback</p>", Panel));

        Assert.IsTrue(PanelLogic.IsPanelDocument("http://127.0.0.1:2722/panel/", Panel));
        Assert.IsTrue(PanelLogic.IsPanelDocument("http://127.0.0.1:2722/panel/index.html", Panel));
        Assert.IsFalse(PanelLogic.IsPanelDocument("http://127.0.0.1:2722/v1/state", Panel));
        Assert.IsFalse(PanelLogic.IsPanelDocument("", Panel));
        Assert.IsFalse(PanelLogic.IsPanelDocument(null, Panel));
    }

    // ---- SCA-029: a bad dimension defaults that dimension and nothing else --------------

    [TestMethod]
    public void SCA_029_a_quoted_dimension_keeps_the_port_and_the_props()
    {
        foreach (var bad in new[] { "\"720\"", "null", "{}", "\"\"", "0", "-5", "40000", "72.5" })
        {
            var json = $$"""
                {
                  "crabdPort": 3999,
                  "display": { "deviceId": "CRXED00", "width": 2560, "height": {{bad}} },
                  "props": { "clock24": true }
                }
                """;
            var s = ParseJson(json, out var logged);
            Assert.AreEqual(3999, s.CrabdPort, bad);
            Assert.AreEqual(1, s.Props.Count, bad);
            Assert.AreEqual("CRXED00", s.DisplayDeviceId, bad);
            Assert.AreEqual(2560, s.DisplayWidth, bad);
            // Only the wrong dimension defaults.
            Assert.AreEqual(PanelLogic.DefaultDisplayHeight, s.DisplayHeight, bad);
            // The rejected property is named, so the operator can find the typo.
            Assert.IsTrue(logged.Any(l => l.Contains("display.height", StringComparison.Ordinal)), bad);
            Assert.IsFalse(logged.Any(l => l.Contains("using defaults", StringComparison.Ordinal)), bad);
        }
    }

    [TestMethod]
    public void SCA_029_the_valid_control_is_unchanged_and_a_broken_file_still_defaults()
    {
        var ok = ParseJson("""
            { "crabdPort": 3999, "display": { "deviceId": "CRXED00", "width": 1920, "height": 515 },
              "props": { "clock24": true } }
            """, out var quiet);
        Assert.AreEqual(3999, ok.CrabdPort);
        Assert.AreEqual(1920, ok.DisplayWidth);
        Assert.AreEqual(515, ok.DisplayHeight);
        Assert.IsFalse(quiet.Any(l => l.Contains("display.", StringComparison.Ordinal)));

        // File-level malformed input is a separate, documented answer and is untouched.
        var broken = ParseJson("{ not json", out var loud);
        Assert.AreEqual(PanelLogic.DefaultPort, broken.CrabdPort);
        Assert.IsTrue(loud.Any(l => l.Contains("using defaults", StringComparison.Ordinal)));
    }

    private static PanelSettings ParseJson(string json, out List<string> logged)
    {
        var path = Path.Combine(Path.GetTempPath(), "sidecrab-sca029-" + Guid.NewGuid().ToString("N") + ".json");
        var lines = new List<string>();
        try
        {
            File.WriteAllText(path, json);
            var s = PanelSettings.Parse(path, lines.Add);
            logged = lines;
            return s;
        }
        finally { File.Delete(path); }
    }

    // ---- SCA-030: an untrusted field cannot forge a log line ----------------------------

    [TestMethod]
    public void SCA_030_a_newline_in_an_untrusted_field_stays_on_one_line()
    {
        var forged = "settings\nFORGED_SYNTHETIC_LINE";
        var escaped = PanelLogic.EscapeForLog(forged);
        Assert.IsFalse(escaped.Contains('\n'));
        Assert.IsFalse(escaped.Contains('\r'));
        StringAssert.Contains(escaped, "FORGED_SYNTHETIC_LINE");   // still readable, not dropped
        StringAssert.Contains(escaped, "\\n");

        Assert.AreEqual(@"a\rb", PanelLogic.EscapeForLog("a\rb"));
        Assert.AreEqual(@"a\tb", PanelLogic.EscapeForLog("a\tb"));
        Assert.AreEqual(@"a\x00b", PanelLogic.EscapeForLog("a\0b"));
        // The escape character itself, or a value could write a literal \n of its own.
        Assert.AreEqual(@"a\\nb", PanelLogic.EscapeForLog(@"a\nb"));
        Assert.AreEqual("", PanelLogic.EscapeForLog(null));
    }

    [TestMethod]
    public void SCA_030_a_long_untrusted_field_is_bounded()
    {
        var long_ = new string('x', 5000);
        var escaped = PanelLogic.EscapeForLog(long_);
        Assert.IsTrue(escaped.Length <= PanelLogic.LogFieldMax + 3, escaped.Length.ToString());
        Assert.IsTrue(escaped.EndsWith("...", StringComparison.Ordinal));
        // Under the cap there is no ellipsis to mistake for the value's own.
        Assert.AreEqual("short", PanelLogic.EscapeForLog("short"));
    }

    // ---- SCA-031: two hosts do not share one log file -----------------------------------

    [TestMethod]
    public void SCA_031_each_mode_owns_its_own_log_file()
    {
        // The kiosk name is a compatibility surface: the setup lane's smoke check reads
        // the viewport line out of panel.log by that name.
        Assert.AreEqual("panel.log", PanelLogic.LogFileName(windowed: false));
        Assert.AreEqual("panel-windowed.log", PanelLogic.LogFileName(windowed: true));
        Assert.AreNotEqual(PanelLogic.LogFileName(true), PanelLogic.LogFileName(false));
    }

    [TestMethod]
    public void SCA_031_two_writers_on_two_files_keep_every_numbered_line()
    {
        var dir = Path.Combine(Path.GetTempPath(), "sidecrab-sca031-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        try
        {
            const int each = 500;
            var kiosk = new Log(Path.Combine(dir, PanelLogic.LogFileName(windowed: false)));
            var windowed = new Log(Path.Combine(dir, PanelLogic.LogFileName(windowed: true)));
            Parallel.Invoke(
                () => { for (var i = 0; i < each; i++) kiosk.Write("kiosk " + i); },
                () => { for (var i = 0; i < each; i++) windowed.Write("windowed " + i); });

            // The audit's fixture attempted 4000 lines into ONE file and recorded 986,
            // under the 1 MB roll. Two files lose none.
            Assert.AreEqual(each, File.ReadAllLines(kiosk.Path).Length);
            Assert.AreEqual(each, File.ReadAllLines(windowed.Path).Length);
            Assert.AreEqual(0, kiosk.Dropped);
            Assert.AreEqual(0, windowed.Dropped);
        }
        finally { try { Directory.Delete(dir, recursive: true); } catch (IOException) { } }
    }

    [TestMethod]
    public void SCA_031_a_line_that_could_not_be_written_is_counted_and_reported()
    {
        var dir = Path.Combine(Path.GetTempPath(), "sidecrab-sca031b-" + Guid.NewGuid().ToString("N"));
        // The directory is a FILE, so every append throws and the roll cannot be the cause.
        File.WriteAllText(dir, "not a directory");
        try
        {
            var log = new Log(Path.Combine(dir, "logs", "panel.log"));
            log.Write("one");
            log.Write("two");
            Assert.AreEqual(2, log.Dropped);
        }
        finally { File.Delete(dir); }

        var ok = Path.Combine(Path.GetTempPath(), "sidecrab-sca031c-" + Guid.NewGuid().ToString("N"), "panel.log");
        try
        {
            var log = new Log(ok);
            log.Write("first");
            Assert.AreEqual(0, log.Dropped);
            Assert.IsFalse(File.ReadAllText(ok).Contains("could not be written", StringComparison.Ordinal));
        }
        finally { try { Directory.Delete(Path.GetDirectoryName(ok)!, recursive: true); } catch (IOException) { } }
    }

    // ---- C7: the two lines the setup lane reads -----------------------------------------

    [TestMethod]
    public void C7_the_viewport_line_keeps_its_field_order_and_names_this_run()
    {
        var line = PanelLogic.ViewportLine(2560, 720, 1.0, 1.0, 2560, 720, 4242, "2026-09-21T10:00:00");
        Assert.AreEqual("viewport: 2560x720 css px, dpr 1, zoom 1, window 2560x720 physical, " +
                        "pid 4242, started 2026-09-21T10:00:00", line);

        // A scaled monitor is where the numbers stop being the same number.
        var scaled = PanelLogic.ViewportLine(2048, 576, 1.25, 0.8, 2560, 720, 7, "2026-09-21T10:00:00");
        StringAssert.Contains(scaled, "dpr 1.25, zoom 0.8");
    }

    [TestMethod]
    public void C7_the_hidden_line_is_written_at_start_and_then_once_a_minute()
    {
        Assert.AreEqual("hidden: target display absent, pid 4242, started 2026-09-21T10:00:00",
                        PanelLogic.HiddenLine(4242, "2026-09-21T10:00:00"));

        var start = new DateTime(2026, 9, 21, 10, 0, 0, DateTimeKind.Local);
        Assert.IsTrue(PanelLogic.ShouldLogHidden(null, start));
        // The poll behind it runs every five seconds; unthrottled that is ~17,000 lines a
        // day and the evidence of what happened before rolls out of the file.
        Assert.IsFalse(PanelLogic.ShouldLogHidden(start, start.AddSeconds(5)));
        Assert.IsFalse(PanelLogic.ShouldLogHidden(start, start.AddSeconds(59)));
        Assert.IsTrue(PanelLogic.ShouldLogHidden(start, start.AddSeconds(60)));
    }

    // ---- MF-004: the display picker -----------------------------------------------------

    [TestMethod]
    public void MF_004_the_label_carries_everything_needed_to_tell_two_displays_apart()
    {
        var label = PanelLogic.DisplayLabel(Monitor(@"\\.\DISPLAY3", "ACME0001", "UID1",
                                                    new Rectangle(-2560, 0, 2560, 720), dpi: 120));
        StringAssert.Contains(label, @"\\.\DISPLAY3");
        StringAssert.Contains(label, "2560x720");
        StringAssert.Contains(label, "at -2560,0");
        StringAssert.Contains(label, "125%");
        StringAssert.Contains(label, "ACME0001");
        Assert.IsFalse(label.Contains("primary", StringComparison.Ordinal));
        StringAssert.Contains(PanelLogic.DisplayLabel(Edge(primary: true)), "primary");
    }

    [TestMethod]
    public void MF_004_a_device_id_survives_a_menu_item_and_a_label()
    {
        // Measured 2026-09-21 on a dev host: the picker's confirmation window rendered
        // "The panel is now set to:" and then nothing. A PnP id is full of ampersands and
        // WinForms reads a single one as a mnemonic prefix, which eats it, underlines the
        // next letter, and on a Label drops the rest of the text.
        var label = PanelLogic.DisplayLabel(Monitor(@"\\.\DISPLAY1", "CRXED00", "UID4358",
                                                    new Rectangle(1228, 2160, 2560, 720)));
        StringAssert.Contains(label, "5&a4ae9a5&0&UID4358");

        var forMenu = PanelLogic.EscapeMnemonics(label);
        StringAssert.Contains(forMenu, "5&&a4ae9a5&&0&&UID4358");
        // What the menu draws is the original, character for character.
        Assert.AreEqual(label, forMenu.Replace("&&", "&"));

        // The label itself is NOT pre-escaped: it is also what the log and the status
        // window carry, and a doubled ampersand there would be wrong.
        Assert.IsFalse(label.Contains("&&", StringComparison.Ordinal));
        Assert.AreEqual("", PanelLogic.EscapeMnemonics(null));
    }

    [TestMethod]
    public void MF_004_two_identical_edges_get_fragments_that_pick_one_each()
    {
        var a = Edge("UID4358");
        var b = new DisplayInfo(@"\\.\DISPLAY4", @"\\?\DISPLAY#CRXED00#5&a4ae9a5&0&UID4361#{guid}",
                                new Rectangle(0, 2880, 2560, 720), false, 96);
        var all = new[] { a, b };

        var fa = PanelLogic.UniqueDeviceIdFragment(a, all);
        var fb = PanelLogic.UniqueDeviceIdFragment(b, all);
        Assert.AreNotEqual(fa, fb);
        // The model part alone is what both share, so it cannot be what gets written.
        Assert.AreNotEqual("CRXED00", fa);
        // Each fragment is what the selector will use, so it must find its own display.
        Assert.AreSame(a, PanelLogic.SelectDisplay(all, fa, null, null));
        Assert.AreSame(b, PanelLogic.SelectDisplay(all, fb, null, null));

        // One Edge on its own gets the short, recognisable form.
        Assert.AreEqual("CRXED00", PanelLogic.UniqueDeviceIdFragment(a, new[] { a }));

        // A monitor with no PnP id has no fragment that would find it again; the picker
        // disables it rather than writing a setting that can never match.
        var virtual_ = new DisplayInfo(@"\\.\DISPLAY9", "", new Rectangle(0, 0, 640, 480), false, 96);
        Assert.AreEqual("", PanelLogic.UniqueDeviceIdFragment(virtual_, new[] { virtual_ }));
    }

    [TestMethod]
    public void MF_004_writing_the_device_id_leaves_every_other_setting_alone()
    {
        var before = """
            {
              "crabdPort": 3999,
              "devtoolsPort": 9224,
              "display": { "deviceId": "CRXED00", "width": 2560, "height": 720 },
              "props": { "clock24": true, "accentColor": "#6F94CC" },
              "somethingAnotherVersionWrote": [1, 2]
            }
            """;
        var after = PanelLogic.MergeDisplayDeviceIdJson(before, "ACME0001#5&a4ae9a5&0&UID1");
        using var doc = JsonDocument.Parse(after);
        var root = doc.RootElement;
        Assert.AreEqual(3999, root.GetProperty("crabdPort").GetInt32());
        Assert.AreEqual(9224, root.GetProperty("devtoolsPort").GetInt32());
        Assert.AreEqual(2, root.GetProperty("props").EnumerateObject().Count());
        Assert.AreEqual(2, root.GetProperty("somethingAnotherVersionWrote").GetArrayLength());
        var display = root.GetProperty("display");
        Assert.AreEqual("ACME0001#5&a4ae9a5&0&UID1", display.GetProperty("deviceId").GetString());
        // The size fallback survives the pick: it is what finds the monitor the day its
        // id changes.
        Assert.AreEqual(2560, display.GetProperty("width").GetInt32());
        Assert.AreEqual(720, display.GetProperty("height").GetInt32());

        // What the host reads back is what was written.
        var path = Path.Combine(Path.GetTempPath(), "sidecrab-mf004-" + Guid.NewGuid().ToString("N") + ".json");
        try
        {
            File.WriteAllText(path, after);
            var s = PanelSettings.Parse(path, _ => { });
            Assert.AreEqual("ACME0001#5&a4ae9a5&0&UID1", s.DisplayDeviceId);
            Assert.AreEqual(3999, s.CrabdPort);
            Assert.AreEqual(2, s.Props.Count);
        }
        finally { File.Delete(path); }
    }

    [TestMethod]
    public void MF_004_an_unreadable_settings_file_becomes_one_carrying_only_the_display()
    {
        var after = PanelLogic.MergeDisplayDeviceIdJson("{ not json", "CRXED00");
        using var doc = JsonDocument.Parse(after);
        Assert.AreEqual("CRXED00", doc.RootElement.GetProperty("display").GetProperty("deviceId").GetString());
        Assert.AreEqual(1, doc.RootElement.EnumerateObject().Count());

    }

    // ---- MF-003: what the status window says --------------------------------------------

    private static PanelLogic.HostStatus Status(bool visible = true, bool paused = false,
                                                string? target = @"\\.\DISPLAY1 2560x720 at 1228,2160 100%",
                                                string reason = PanelLogic.DisplayReasonId,
                                                bool loaded = true, string? failure = null, int priorStarts = 0) =>
        new("kiosk", "0.4.0", 4242, new DateTime(2026, 9, 21, 10, 0, 0), visible, paused, target, reason,
            loaded, failure, @"C:\Users\x\.sidecrab\logs\panel.log", priorStarts);

    [TestMethod]
    public void MF_003_a_running_panel_says_where_it_is()
    {
        var lines = PanelLogic.StatusLines(Status(), new DateTime(2026, 9, 21, 12, 30, 0));
        StringAssert.Contains(lines[0], "0.4.0");
        StringAssert.Contains(lines[0], "pid 4242");
        StringAssert.Contains(lines[1], "up 2h 30m");
        StringAssert.Contains(lines[2], "shown on");
        StringAssert.Contains(lines[3], "loaded");
        StringAssert.Contains(lines[4], "none this run");
        StringAssert.Contains(lines[^1], "panel.log");
    }

    [TestMethod]
    public void MF_003_a_hidden_panel_says_why_and_what_to_do_about_it()
    {
        var absent = PanelLogic.StatusLines(Status(visible: false, target: null,
                                                   reason: PanelLogic.DisplayReasonNone, loaded: false),
                                            new DateTime(2026, 9, 21, 10, 0, 30));
        StringAssert.Contains(absent[2], "hidden");
        StringAssert.Contains(absent[2], "not attached");

        // The two refusals SCA-014 introduced are the ones an operator cannot guess, so
        // each names the picker rather than only saying no.
        var ambiguous = PanelLogic.StatusLines(Status(visible: false, target: null,
                                                      reason: PanelLogic.DisplayReasonSizeAmbiguous),
                                               new DateTime(2026, 9, 21, 10, 1, 0));
        StringAssert.Contains(ambiguous[2], "two displays match");
        StringAssert.Contains(ambiguous[2], "pick one");

        var wouldBePrimary = PanelLogic.StatusLines(Status(visible: false, target: null,
                                                           reason: PanelLogic.DisplayReasonSizePrimary),
                                                    new DateTime(2026, 9, 21, 10, 1, 0));
        StringAssert.Contains(wouldBePrimary[2], "primary display");
    }

    [TestMethod]
    public void MF_003_the_restart_policy_is_stated_as_bounded_and_honestly()
    {
        var quiet = PanelLogic.StatusLines(Status(), new DateTime(2026, 9, 21, 10, 5, 0));
        var line = quiet.Single(l => l.StartsWith("Restart on failure", StringComparison.Ordinal));
        // The task allows three, a minute apart, and then stops. A status window that
        // implied an endless retry would be the control that reports success forever.
        StringAssert.Contains(line, "up to 3 times");
        StringAssert.Contains(line, "no earlier start");

        var restarted = PanelLogic.StatusLines(Status(priorStarts: 2), new DateTime(2026, 9, 21, 10, 5, 0));
        StringAssert.Contains(restarted.Single(l => l.StartsWith("Restart on failure", StringComparison.Ordinal)),
                              "2 earlier start(s)");
    }

    [TestMethod]
    public void MF_003_pause_is_reported_as_the_operators_own_doing()
    {
        var paused = PanelLogic.StatusLines(Status(visible: false, paused: true),
                                            new DateTime(2026, 9, 21, 10, 5, 0));
        StringAssert.Contains(paused[2], "paused by you");
        // Not confused with an absent display, which is the state it looks like on the glass.
        Assert.IsFalse(paused[2].Contains("not attached", StringComparison.Ordinal));
    }

    [TestMethod]
    public void MF_003_a_failure_this_run_is_escaped_like_any_other_untrusted_text()
    {
        var lines = PanelLogic.StatusLines(Status(failure: "HTTP 500\nFORGED"), new DateTime(2026, 9, 21, 10, 5, 0));
        var failure = lines.Single(l => l.StartsWith("Last failure", StringComparison.Ordinal));
        Assert.IsFalse(failure.Contains('\n'));
        StringAssert.Contains(failure, "HTTP 500");
    }
}
