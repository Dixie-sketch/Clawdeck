using System.Drawing;
using System.Text.Json;
using SideCrab.Panel;

namespace SideCrab.Panel.Tests;

/// <summary>lane O, the decisions that do not need a window: the ambiguity the selector
/// used to settle by enumeration order, the two profile names that land on another host's
/// files, the command line that swallowed a switch, the zoom loop at 125/150/175 %, what a
/// finished navigation means, the log roll, and the --check report.</summary>
[TestClass]
public sealed class LaneOLogicTests
{
    private static DisplayInfo Edge(string name, string uid, int y = 0, uint dpi = 96) =>
        new(name, @"\\?\DISPLAY#CRXED00#5&a4ae9a5&0&" + uid + "#{guid}",
            new Rectangle(0, y, 2560, 720), false, dpi);

    private static DisplayInfo Primary() =>
        new(@"\\.\DISPLAY1", @"\\?\DISPLAY#ACME1234#5&1&0&UID256#{guid}",
            new Rectangle(0, 0, 2560, 1600), true, 144);

    // ------------------------------------------------------------------ LO-001

    [TestMethod]
    public void LO_001_two_monitors_carrying_the_configured_id_are_refused_not_ordered()
    {
        var displays = new List<DisplayInfo> { Primary(), Edge(@"\\.\DISPLAY2", "UID4358"), Edge(@"\\.\DISPLAY3", "UID4359", 720) };

        var choice = PanelLogic.ChooseDisplay(displays, "CRXED00", 2560, 720);

        Assert.IsNull(choice.Display, "picking the first of two identical Edges is picking by index");
        Assert.AreEqual(PanelLogic.DisplayReasonIdAmbiguous, choice.Reason);
        Assert.IsTrue(PanelLogic.HiddenReason(choice.Reason).Contains("pick one from this menu", StringComparison.Ordinal));
    }

    [TestMethod]
    public void LO_001_the_ordinary_one_edge_case_is_untouched_and_the_picker_resolves_a_tie()
    {
        var one = new List<DisplayInfo> { Primary(), Edge(@"\\.\DISPLAY2", "UID4358") };
        Assert.AreEqual(PanelLogic.DisplayReasonId, PanelLogic.ChooseDisplay(one, "CRXED00", 2560, 720).Reason);

        var two = new List<DisplayInfo> { Primary(), Edge(@"\\.\DISPLAY2", "UID4358"), Edge(@"\\.\DISPLAY3", "UID4359", 720) };
        // What the tray picker writes for the second of them.
        var fragment = PanelLogic.UniqueDeviceIdFragment(two[2], two);
        var picked = PanelLogic.ChooseDisplay(two, fragment, 2560, 720);
        Assert.AreEqual(PanelLogic.DisplayReasonId, picked.Reason);
        Assert.AreEqual(@"\\.\DISPLAY3", picked.Display!.DeviceName);
    }

    [TestMethod]
    public void LO_001_a_fragment_that_matches_nothing_still_falls_through_to_the_size()
    {
        var displays = new List<DisplayInfo> { Primary(), Edge(@"\\.\DISPLAY2", "UID4358") };
        var choice = PanelLogic.ChooseDisplay(displays, "NO-SUCH-MONITOR", 2560, 720);
        Assert.AreEqual(PanelLogic.DisplayReasonSize, choice.Reason);
        Assert.AreEqual(@"\\.\DISPLAY2", choice.Display!.DeviceName);
    }

    // ------------------------------------------------------------------ LO-004

    /// <summary>The collision itself, before the refusal: these are the two names that
    /// resolve to files the unnamed hosts already own.</summary>
    [TestMethod]
    public void LO_004_the_two_reserved_profile_names_land_on_another_hosts_files()
    {
        const string local = @"C:\Users\x\AppData\Local";
        Assert.AreEqual(PanelLogic.WebViewUserDataDir(local, PanelLogic.ProfileName(windowed: false)),
                        PanelLogic.WebViewUserDataDir(local, PanelLogic.ProfileName(windowed: false, profile: "kiosk")),
                        "--profile kiosk shares the installed host's WebView2 folder");
        Assert.AreEqual(PanelLogic.LogFileName(windowed: true),
                        PanelLogic.LogFileName(windowed: false, profile: "windowed"),
                        "--profile windowed shares the windowed host's log file");
        Assert.IsTrue(PanelLogic.IsReservedProfile("kiosk"));
        Assert.IsTrue(PanelLogic.IsReservedProfile("KIOSK!"), "the safe name is what collides, not what was typed");
        Assert.IsTrue(PanelLogic.IsReservedProfile("windowed"));
        Assert.IsFalse(PanelLogic.IsReservedProfile("qa"));
        Assert.IsFalse(PanelLogic.IsReservedProfile("laneo"));
    }

    [TestMethod]
    public void LO_004_a_reserved_profile_is_refused_at_the_command_line()
    {
        var o = HostOptions.Parse(new[] { "--profile", "kiosk" });
        Assert.IsNotNull(o.Error);
        Assert.IsTrue(o.Error!.Contains("reserved", StringComparison.Ordinal), o.Error);

        Assert.IsNull(HostOptions.Parse(new[] { "--profile", "qa" }).Error);
    }

    [TestMethod]
    public void LO_004_a_switch_cannot_be_swallowed_as_the_value_of_the_switch_before_it()
    {
        var o = HostOptions.Parse(new[] { "--profile", "--windowed" });
        Assert.IsNotNull(o.Error, "--profile --windowed used to start a KIOSK whose profile was 'windowed'");
        Assert.IsNull(o.Profile);

        foreach (var pair in new[] { "--display", "--sidecrab-dir", "--port", "--devtools-port" })
            Assert.IsNotNull(HostOptions.Parse(new[] { pair, "--windowed" }).Error, pair);
    }

    [TestMethod]
    public void LO_004_a_settings_directory_is_absolute_from_the_command_line_on()
    {
        var o = HostOptions.Parse(new[] { "--sidecrab-dir", "relative-dir" });
        Assert.IsNull(o.Error);
        Assert.IsTrue(Path.IsPathRooted(o.SideCrabDir), o.SideCrabDir);
        Assert.AreEqual(Path.GetFullPath("relative-dir"), o.SideCrabDir);
    }

    [TestMethod]
    public void The_check_switch_is_parsed_and_takes_no_value()
    {
        Assert.IsTrue(HostOptions.Parse(new[] { "--check" }).Check);
        Assert.IsTrue(HostOptions.Parse(new[] { "--doctor" }).Check);
        var both = HostOptions.Parse(new[] { "--check", "--windowed" });
        Assert.IsTrue(both.Check);
        Assert.IsTrue(both.Windowed);
        Assert.IsNull(both.Error);
        Assert.IsFalse(HostOptions.Parse(Array.Empty<string>()).Check);
    }

    // ------------------------------------------------------------------ LO-003

    [TestMethod]
    public void LO_003_a_settings_directory_that_cannot_be_created_is_reported_and_not_swallowed()
    {
        var file = Path.Combine(Path.GetTempPath(), "sidecrab-laneo-" + Guid.NewGuid().ToString("N") + ".txt");
        File.WriteAllText(file, "a file, not a directory");
        try
        {
            // A path UNDER a file cannot be created, on any Windows.
            var impossible = Path.Combine(file, "logs");
            Assert.IsFalse(HostCheck.DirectoryIsUsable(impossible, out var error));
            Assert.IsNotNull(error);

            // The check's own probe leaves the RUNNING host's log untouched: an existing
            // file is opened for append and closed, which writes nothing at all.
            var live = Path.Combine(Path.GetTempPath(), "sidecrab-laneo-" + Guid.NewGuid().ToString("N") + ".log");
            File.WriteAllText(live, "2026-09-22 09:00:00 SideCrab.Panel 0.4.0 starting; pid 1" + Environment.NewLine);
            var before = File.ReadAllBytes(live);
            Assert.IsTrue(HostCheck.LogIsWritable(live, out var liveError));
            Assert.IsNull(liveError);
            CollectionAssert.AreEqual(before, File.ReadAllBytes(live), "the check must not write into a live log");
            File.Delete(live);

            // And the tolerant path is intact: a directory that can be made reports usable
            // and leaves nothing behind.
            var fine = Path.Combine(Path.GetTempPath(), "sidecrab-laneo-" + Guid.NewGuid().ToString("N"));
            Assert.IsTrue(HostCheck.DirectoryIsUsable(fine, out var none));
            Assert.IsNull(none);
            Assert.AreEqual(0, Directory.GetFiles(fine).Length, "the write probe is removed");
            Directory.Delete(fine, recursive: true);
        }
        finally { File.Delete(file); }
    }

    // ------------------------------------------------------------------ LO-006

    [TestMethod]
    public void LO_006_a_request_with_no_usable_id_no_longer_clears_the_channel()
    {
        var gate = new PanelLogic.BridgeGate();
        gate.Accepted(PanelLogic.ChannelSettings, "first");
        // A legacy page, or an id over the cap, which RequestId answers with null.
        gate.Accepted(PanelLogic.ChannelSettings, null);

        Assert.IsFalse(gate.IsCurrent(PanelLogic.ChannelSettings, "first"),
                       "a reply to the attempt before the newest one is superseded, whatever the newest one looked like");
        Assert.IsTrue(gate.IsCurrent(PanelLogic.ChannelSettings, null), "the legacy attempt itself is the current one");

        gate.Accepted(PanelLogic.ChannelSettings, "second");
        Assert.IsTrue(gate.IsCurrent(PanelLogic.ChannelSettings, "second"));
        Assert.IsFalse(gate.IsCurrent(PanelLogic.ChannelSettings, null));
        // Channels are still independent.
        Assert.IsTrue(gate.IsCurrent(PanelLogic.ChannelFocus, "anything"));
    }

    // ------------------------------------------------------------------ LO-005

    [TestMethod]
    public void LO_005_a_flood_of_focus_requests_is_floored_and_a_real_click_is_not()
    {
        var t0 = new DateTime(2026, 9, 22, 10, 0, 0, DateTimeKind.Utc);
        Assert.IsTrue(PanelLogic.FocusAllowedNow(null, t0), "the first request is always allowed");
        Assert.IsFalse(PanelLogic.FocusAllowedNow(t0, t0.AddMilliseconds(16)), "a page in a render loop");
        Assert.IsFalse(PanelLogic.FocusAllowedNow(t0, t0.AddMilliseconds(249)));
        Assert.IsTrue(PanelLogic.FocusAllowedNow(t0, t0.AddMilliseconds(250)));
        // A double click on the panel's own button is two presses about 300 ms apart at
        // worst, and the second one still works.
        Assert.IsTrue(PanelLogic.FocusAllowedNow(t0, t0.AddMilliseconds(300)));
    }

    // ------------------------------------------------------------------ the zoom loop

    /// <summary>The browser's side of the correction, modelled:
    /// innerWidth = round(physical / (dpiScale * zoom)). It is a model and says so, but it
    /// is the same arithmetic the measured 100 % line on this PC satisfies, and it is the
    /// only way to run 125, 150 and 175 % without three monitors.</summary>
    private static int InnerWidth(int physicalWidth, double dpiScale, double zoom) =>
        (int)Math.Round(physicalWidth / (dpiScale * zoom), MidpointRounding.AwayFromZero);

    [TestMethod]
    public void The_zoom_correction_converges_in_one_step_at_125_150_and_175_percent()
    {
        foreach (var scale in new[] { 1.0, 1.25, 1.5, 1.75 })
        {
            var zoom = 1.0;
            var corrections = 0;
            for (var attempt = 0; attempt < 5; attempt++)
            {
                var next = PanelLogic.ZoomAfterMeasure(zoom, InnerWidth(2560, scale, zoom), 2560);
                if (next is null) break;
                zoom = next.Value;
                corrections++;
            }
            Assert.IsTrue(corrections <= 1,
                          $"{scale:0.##}x took {corrections} corrections; the host allows 3 before it gives up");
            Assert.AreEqual(2560, InnerWidth(2560, scale, zoom), PanelLogic.ViewportTolerancePx,
                            $"the viewport is not the monitor's width at {scale:0.##}x");
        }
    }

    [TestMethod]
    public void A_viewport_already_within_two_pixels_is_left_alone()
    {
        Assert.IsNull(PanelLogic.ZoomAfterMeasure(1.0, 2560, 2560));
        Assert.IsNull(PanelLogic.ZoomAfterMeasure(0.667, 2561, 2560), "chasing the last pixel loops the correction");
        Assert.IsNull(PanelLogic.ZoomAfterMeasure(0.667, 2558, 2560));
        Assert.IsNotNull(PanelLogic.ZoomAfterMeasure(1.0, 2048, 2560));
        // A measurement that makes no sense leaves the zoom where it is.
        Assert.IsNull(PanelLogic.ZoomAfterMeasure(1.0, 0, 2560));
        Assert.IsNull(PanelLogic.ZoomAfterMeasure(0, 2048, 2560));
    }

    /// <summary>LO-013. The page loads while the monitor is absent, and the window is the
    /// WinForms default size then, so the measurement described a window nobody can see -
    /// and described it CONSISTENTLY (300 css px in a 300 px window), which is what the
    /// setup lane's verdict reads as a pass.</summary>
    [TestMethod]
    public void LO_013_a_hidden_host_measures_no_viewport_at_all()
    {
        Assert.IsFalse(PanelLogic.ShouldMeasureViewport(visible: false, pageFailed: false, showingFallback: false),
                       "a hidden host's only C7 line is the hidden one");
        Assert.IsTrue(PanelLogic.ShouldMeasureViewport(true, false, false));
        Assert.IsFalse(PanelLogic.ShouldMeasureViewport(true, pageFailed: true, showingFallback: false));
        Assert.IsFalse(PanelLogic.ShouldMeasureViewport(true, false, showingFallback: true),
                       "the fallback page is the host's own document, not the panel");
    }

    // ------------------------------------------------------------------ what a load means

    [TestMethod]
    public void A_refused_connection_and_a_404_are_both_failures_and_are_told_apart()
    {
        var url = PanelLogic.PanelUrl(2722);

        // Nothing listening: WebView2 reports IsSuccess false, a WebErrorStatus, and no code.
        var refused = PanelLogic.JudgeNavigation(false, 0, "ConnectionRefused", null, url);
        Assert.AreEqual(PanelLogic.PageOutcome.Failed, refused.Outcome);
        Assert.AreEqual("ConnectionRefused", refused.Reason);

        // crabd answering, but not with the panel: the navigation SUCCEEDED.
        var notFound = PanelLogic.JudgeNavigation(true, 404, "Unknown", "http://127.0.0.1:2722/panel/", url);
        Assert.AreEqual(PanelLogic.PageOutcome.Failed, notFound.Outcome);
        Assert.AreEqual("HTTP 404", notFound.Reason);

        Assert.AreEqual(PanelLogic.PageOutcome.Failed,
                        PanelLogic.JudgeNavigation(true, 500, "Unknown", "http://127.0.0.1:2722/panel/", url).Outcome);
    }

    [TestMethod]
    public void A_navigation_that_succeeded_somewhere_else_is_not_a_loaded_panel()
    {
        var url = PanelLogic.PanelUrl(2722);
        Assert.AreEqual(PanelLogic.PageOutcome.Loaded,
                        PanelLogic.JudgeNavigation(true, 200, "Unknown", "http://127.0.0.1:2722/panel/", url).Outcome);
        Assert.AreEqual(PanelLogic.PageOutcome.NotThePanel,
                        PanelLogic.JudgeNavigation(true, 200, "Unknown", "about:blank", url).Outcome);
        Assert.AreEqual(PanelLogic.PageOutcome.NotThePanel,
                        PanelLogic.JudgeNavigation(true, 0, "Unknown", null, url).Outcome);
        // The host's own fallback page is a document, not the panel: treating it as loaded
        // is what stopped the retry with a dark window (SCA-028).
        Assert.AreEqual(PanelLogic.PageOutcome.NotThePanel,
                        PanelLogic.JudgeNavigation(true, 200, "Unknown", "data:text/html,<p>x", url).Outcome);
        // And a newline in the document the window landed on cannot forge a log line.
        var forged = PanelLogic.JudgeNavigation(true, 200, "Unknown", "http://127.0.0.1:9/a\nb", url);
        Assert.IsFalse(forged.Reason.Contains('\n'));
    }

    // ------------------------------------------------------------------ the log roll

    [TestMethod]
    public void The_log_rolls_past_a_megabyte_and_the_line_that_caused_it_is_in_the_new_file()
    {
        var dir = HostHarness.Scratch();
        try
        {
            var path = Path.Combine(dir, "logs", "panel.log");
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, new string('x', (1024 * 1024) + 64) + Environment.NewLine);

            var log = new Log(path);
            log.Write("the line that rolled it");
            log.Write("and the one after it");

            Assert.IsTrue(File.Exists(path + ".1"), "the previous megabyte is kept as one generation");
            Assert.IsTrue(new FileInfo(path + ".1").Length > 1024 * 1024);
            var now = File.ReadAllText(path);
            Assert.IsTrue(now.Contains("the line that rolled it", StringComparison.Ordinal),
                          "the line that triggered the roll must not be the one that is lost");
            Assert.IsTrue(now.Contains("and the one after it", StringComparison.Ordinal));
            Assert.IsTrue(new FileInfo(path).Length < 4096);
            Assert.AreEqual(0, log.Dropped);
        }
        finally { HostHarness.Cleanup(dir); }
    }

    // ------------------------------------------------------------------ LO-010

    [TestMethod]
    public void LO_010_a_wrongly_typed_device_id_keeps_the_default_and_is_named()
    {
        var dir = HostHarness.Scratch("""
            { "crabdPort": 4311, "display": { "deviceId": 5, "width": 2560, "height": 720 },
              "props": { "clock24": true } }
            """);
        try
        {
            var said = new List<string>();
            var s = PanelSettings.Parse(Path.Combine(dir, "panel-settings.json"), said.Add);

            Assert.AreEqual(PanelLogic.DefaultDisplayDeviceId, s.DisplayDeviceId,
                            "a typo must not quietly turn off pinning by identity");
            Assert.AreEqual(4311, s.CrabdPort, "and it must not take the rest of the file with it (SCA-029)");
            Assert.IsTrue(s.Props.ContainsKey("clock24"));
            Assert.IsTrue(said.Any(l => l.Contains("display.deviceId", StringComparison.Ordinal)),
                          string.Join(" | ", said));
        }
        finally { HostHarness.Cleanup(dir); }
    }

    [TestMethod]
    public void LO_010_an_explicit_null_device_id_still_means_match_on_size_alone()
    {
        var dir = HostHarness.Scratch("""
            { "display": { "deviceId": null, "width": 2560, "height": 720 } }
            """);
        try
        {
            var said = new List<string>();
            var s = PanelSettings.Parse(Path.Combine(dir, "panel-settings.json"), said.Add);
            Assert.IsNull(s.DisplayDeviceId);
            Assert.AreEqual(0, said.Count, "an operator saying 'size only' is not a mistake to report");
        }
        finally { HostHarness.Cleanup(dir); }
    }

    // ------------------------------------------------------------------ --check

    private static PanelSettings Settings(string source = "defaults (no panel-settings.json)") => new()
    {
        CrabdPort = 2722,
        DisplayDeviceId = PanelLogic.DefaultDisplayDeviceId,
        DisplayWidth = 2560,
        DisplayHeight = 720,
        Source = source,
    };

    private static HostCheck.CheckInput Input(IReadOnlyList<DisplayInfo> displays, string? webview = "141.0.3537.85",
                                              string? logError = null, PanelSettings? settings = null,
                                              IReadOnlyList<string>? warnings = null) =>
        new("0.4.0", Windowed: false, "kiosk", @"C:\x\.sidecrab", @"C:\x\.sidecrab\panel-settings.json",
            settings ?? Settings(), warnings ?? Array.Empty<string>(), null, null, displays,
            webview, webview is null ? "WebView2RuntimeNotFoundException" : null,
            @"C:\x\.sidecrab\logs\panel.log", logError, AnotherHostRunning: false);

    [TestMethod]
    public void The_check_prints_one_fact_per_line_and_exits_zero_when_the_panel_would_show()
    {
        var edge = Edge(@"\\.\DISPLAY2", "UID4358");
        var report = HostCheck.Build(Input(new List<DisplayInfo> { Primary(), edge }));

        Assert.AreEqual(0, report.ExitCode);
        foreach (var line in report.Lines)
        {
            Assert.IsFalse(line.Contains('\n'), line);
            Assert.IsTrue(line.Contains(": ", StringComparison.Ordinal), "every line is a key and a value: " + line);
            Assert.IsFalse(line.Contains('\u001b'), "no colour: " + line);
        }
        Assert.IsTrue(report.Lines.Any(l => l.StartsWith("display: ", StringComparison.Ordinal) &&
                                            l.Contains("CRXED00", StringComparison.Ordinal) &&
                                            l.Contains("2560x720", StringComparison.Ordinal)),
                      "every display is listed with its id and its size");
        Assert.IsTrue(report.Lines.Contains("pick-reason: " + PanelLogic.DisplayReasonId));
        Assert.IsTrue(report.Lines.Any(l => l.StartsWith("webview2: 141.", StringComparison.Ordinal)));
        Assert.IsTrue(report.Lines.Any(l => l.StartsWith("log: ", StringComparison.Ordinal)));
        Assert.IsTrue(report.Lines.Contains("log-writable: yes"));
        Assert.IsTrue(report.Lines.Contains("result: ok"));
        Assert.IsFalse(report.Lines.Any(l => l.StartsWith("problem: ", StringComparison.Ordinal)));
    }

    [TestMethod]
    public void The_check_exits_two_and_names_every_reason_the_panel_would_not_show()
    {
        var warnings = new List<string> { "panel-settings.json: display.height is not a usable number (String); using 720." };
        var report = HostCheck.Build(Input(new List<DisplayInfo> { Primary() }, webview: null,
                                           logError: "UnauthorizedAccessException: Access to the path is denied.",
                                           settings: Settings("defaults (panel-settings.json unreadable)"),
                                           warnings: warnings));

        Assert.AreEqual(2, report.ExitCode);
        var problems = report.Lines.Where(l => l.StartsWith("problem: ", StringComparison.Ordinal)).ToList();
        Assert.AreEqual(4, problems.Count, string.Join(" | ", problems));
        Assert.IsTrue(problems.Any(p => p.Contains("WebView2", StringComparison.Ordinal)));
        Assert.IsTrue(problems.Any(p => p.Contains("no display matched", StringComparison.Ordinal)));
        Assert.IsTrue(problems.Any(p => p.Contains("log directory", StringComparison.Ordinal)));
        Assert.IsTrue(problems.Any(p => p.Contains("panel-settings.json could not be read", StringComparison.Ordinal)));
        Assert.IsTrue(report.Lines.Any(l => l.StartsWith("settings-warning: ", StringComparison.Ordinal)));
        Assert.IsTrue(report.Lines.Contains("result: problem"));
        Assert.AreEqual("result: problem", report.Lines[^1], "the verdict is the last line, so a tail reads it");
    }

    [TestMethod]
    public void The_check_never_claims_a_pick_the_host_would_not_make()
    {
        // The same tie the selector refuses. A check that reported a monitor here would be
        // a check that disagreed with the host it is checking.
        var two = new List<DisplayInfo> { Primary(), Edge(@"\\.\DISPLAY2", "UID4358"), Edge(@"\\.\DISPLAY3", "UID4359", 720) };
        var report = HostCheck.Build(Input(two));
        Assert.AreEqual(2, report.ExitCode);
        Assert.IsTrue(report.Lines.Contains("pick: none"));
        Assert.IsTrue(report.Lines.Any(l => l.StartsWith("pick-reason: " + PanelLogic.DisplayReasonIdAmbiguous,
                                                         StringComparison.Ordinal)));
    }

    [TestMethod]
    public void The_check_reads_the_settings_file_it_would_read_and_repeats_its_warnings()
    {
        var dir = HostHarness.Scratch("""
            { "crabdPort": 4311, "display": { "deviceId": "CRXED00", "width": "720" } }
            """);
        try
        {
            var warnings = new List<string>();
            var settings = PanelSettings.Load(dir, warnings.Add);
            var report = HostCheck.Build(new HostCheck.CheckInput(
                "0.4.0", false, "kiosk", dir, Path.Combine(dir, "panel-settings.json"),
                settings, warnings, null, null, new List<DisplayInfo> { Primary(), Edge(@"\\.\DISPLAY2", "UID4358") },
                "141.0.3537.85", null, Path.Combine(dir, "logs", "panel.log"), null, false));

            Assert.IsTrue(report.Lines.Contains("crabd-port: 4311"));
            Assert.IsTrue(report.Lines.Contains("panel-url: http://127.0.0.1:4311/panel/"));
            Assert.IsTrue(report.Lines.Any(l => l.StartsWith("settings-warning: ", StringComparison.Ordinal) &&
                                                l.Contains("display.width", StringComparison.Ordinal)),
                          string.Join(" | ", report.Lines));
            Assert.AreEqual(0, report.ExitCode, "a warned-about dimension is not a reason to fail the check");
        }
        finally { HostHarness.Cleanup(dir); }
    }
}
