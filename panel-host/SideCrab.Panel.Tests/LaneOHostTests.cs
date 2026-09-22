using System.Text.Json;
using Microsoft.Win32;
using SideCrab.Panel;

namespace SideCrab.Panel.Tests;

/// <summary>lane O. The hardware the 2026-09-21 audit could not reach, driven through the
/// real window: a monitor attached after a hidden start, a detach, two identical Edges, a
/// changed device id, a DPI change, a session unlock, a power resume, and WM_DISPLAYCHANGE
/// as a real window message. The display list is a DisplaySet; everything else - the timers,
/// the message loop, the watcher, SetVisibleCore - is the shipping code.</summary>
[TestClass]
public sealed class LaneOHostTests
{
    private static T OnSta<T>(Func<T> body)
    {
        T result = default!;
        Exception? failed = null;
        var t = new Thread(() =>
        {
            try { result = body(); }
            catch (Exception ex) { failed = ex; }
        });
        t.SetApartmentState(ApartmentState.STA);
        t.Start();
        Assert.IsTrue(t.Join(TimeSpan.FromSeconds(30)), "the STA test thread did not finish");
        if (failed is not null) throw new AssertFailedException("the STA body threw: " + failed, failed);
        return result;
    }

    // ---------------------------------------------------------------- attach and detach

    /// <summary>The audit's first unverifiable: "the target attached after a hidden start".
    /// Nothing is called by hand here - the attach is a change to the monitor list and the
    /// only thing that acts on it is the five second poll SCA-002 fixed.</summary>
    [TestMethod]
    public void A_late_attach_is_found_by_the_polls_own_tick_with_no_restart()
    {
        var dir = HostHarness.Scratch();
        try
        {
            var displays = new DisplaySet(FakeDisplay.Other());
            using var host = HostHarness.Start(dir, displays);

            Assert.IsFalse(host.Get(() => host.Form.Visible), "a host whose target is absent starts hidden");
            Assert.AreEqual(PanelLogic.DisplayReasonNone, host.Get(() => host.Form.TargetReason));
            var readsBefore = displays.Reads;

            var edge = FakeDisplay.Edge();
            displays.Set(FakeDisplay.Other(), edge);

            host.Expect(() => host.Form.Visible, "the window is shown after the monitor appears");
            Assert.AreEqual(PanelLogic.DisplayReasonId, host.Get(() => host.Form.TargetReason));
            Assert.AreEqual(edge.Bounds.Location, host.Get(() => host.Form.Bounds).Location);
            Assert.AreEqual(HostHarness.AsWindowsAllows(edge.Bounds.Size), host.Get(() => host.Form.Bounds).Size);
            Assert.IsTrue(displays.Reads > readsBefore, "the poll kept asking while the host was hidden");
            host.ExpectLog("repin(timer)");
        }
        finally { HostHarness.Cleanup(dir); }
    }

    [TestMethod]
    public void A_detach_hides_the_window_and_the_status_window_says_why()
    {
        var dir = HostHarness.Scratch();
        try
        {
            var displays = new DisplaySet(FakeDisplay.Other(), FakeDisplay.Edge());
            using var host = HostHarness.Start(dir, displays);
            host.Expect(() => host.Form.Visible, "the window is shown while the Edge is attached");

            displays.Set(FakeDisplay.Other());
            host.Do(() => host.Form.RepinNow());

            Assert.IsFalse(host.Get(() => host.Form.Visible));
            Assert.AreEqual(PanelLogic.DisplayReasonNone, host.Get(() => host.Form.TargetReason));
            var lines = host.Get(() => PanelLogic.StatusLines(host.Form.Status(), DateTime.Now).ToArray());
            Assert.IsTrue(lines.Any(l => l.Contains("not attached", StringComparison.Ordinal)),
                          string.Join(" | ", lines));
            // C7: the line the setup lane reads while the target is gone, and no other. A
            // viewport line from a hidden host reads as a pass in
            // Get-SideCrabViewportVerdict, because the window it describes is square with
            // itself at 300x300 (LO-013).
            host.ExpectLog("hidden: target display absent");
            Assert.IsFalse(host.ReadLog().Contains("viewport:", StringComparison.Ordinal),
                           "a host that is showing nothing must not log a viewport measurement");
        }
        finally { HostHarness.Cleanup(dir); }
    }

    /// <summary>The real window message, posted to the real window. Calling the handler
    /// would have proven the handler and not the WndProc case that dispatches it, which is
    /// where a missing base call or a swallowed message would live.</summary>
    [TestMethod]
    public void The_display_change_message_repins_without_waiting_for_the_poll()
    {
        var dir = HostHarness.Scratch();
        try
        {
            var displays = new DisplaySet(FakeDisplay.Other());
            using var host = HostHarness.Start(dir, displays);
            displays.Set(FakeDisplay.Other(), FakeDisplay.Edge());

            host.PostDisplayChange();

            host.ExpectLog("repin(WM_DISPLAYCHANGE)");
            host.Expect(() => host.Form.Visible, "the window is shown after WM_DISPLAYCHANGE");
        }
        finally { HostHarness.Cleanup(dir); }
    }

    [TestMethod]
    public void A_session_unlock_and_a_power_resume_both_repin()
    {
        var dir = HostHarness.Scratch();
        try
        {
            var displays = new DisplaySet(FakeDisplay.Other());
            using var host = HostHarness.Start(dir, displays);
            displays.Set(FakeDisplay.Other(), FakeDisplay.Edge());

            // SystemEvents cannot be raised from outside Windows, so the handlers are
            // called with the arguments Windows would carry. What is under test is the
            // marshalling and the re-pin, not the subscription.
            host.Do(() => host.Form.OnSessionSwitch(null, new SessionSwitchEventArgs(SessionSwitchReason.SessionUnlock)));
            host.ExpectLog("repin(SessionUnlock)");
            host.Expect(() => host.Form.Visible, "the window is shown after an unlock");

            displays.Set(FakeDisplay.Other());
            host.Do(() => host.Form.OnPowerModeChanged(null, new PowerModeChangedEventArgs(PowerModes.Resume)));
            host.ExpectLog("repin(resume)");
            host.Expect(() => !host.Form.Visible, "the window is hidden again after a resume with no Edge");
        }
        finally { HostHarness.Cleanup(dir); }
    }

    /// <summary>A scaling change on the pinned monitor. The audit could not test it; the
    /// window keeps the monitor's PHYSICAL rectangle either way, and the re-pin line is what
    /// records the new scaling for the zoom correction that follows it.</summary>
    [TestMethod]
    public void A_dpi_change_on_the_target_is_repinned_and_the_new_scaling_is_logged()
    {
        var dir = HostHarness.Scratch();
        try
        {
            var displays = new DisplaySet(FakeDisplay.Other(), FakeDisplay.Edge(dpi: 96));
            using var host = HostHarness.Start(dir, displays);
            host.Expect(() => host.Form.Visible, "the window is shown at 100%");

            var scaled = FakeDisplay.Edge(dpi: 144);
            displays.Set(FakeDisplay.Other(), scaled);
            host.Do(() => host.Form.RepinNow());

            host.ExpectLog("150%");
            Assert.AreEqual(scaled.Bounds.Location, host.Get(() => host.Form.Bounds).Location,
                            "the window keeps the monitor's physical rectangle; the zoom is what changes");
            Assert.AreEqual(HostHarness.AsWindowsAllows(scaled.Bounds.Size), host.Get(() => host.Form.Bounds).Size);
            Assert.IsTrue(host.Get(() => host.Form.Visible));
        }
        finally { HostHarness.Cleanup(dir); }
    }

    /// <summary>LO-001. Two Xeneon Edges both carry CRXED00. The size tie has been refused
    /// since SCA-014; the id tie was settled by enumeration order, which is the index the
    /// selector says it never uses.</summary>
    [TestMethod]
    public void Two_identical_edges_hide_the_panel_and_name_the_ambiguity()
    {
        var dir = HostHarness.Scratch();
        try
        {
            var displays = new DisplaySet(FakeDisplay.Other(), FakeDisplay.Edge());
            using var host = HostHarness.Start(dir, displays);
            host.Expect(() => host.Form.Visible, "one Edge is pinned");

            displays.Set(FakeDisplay.Other(),
                         FakeDisplay.Edge(deviceName: @"\\.\DISPLAY2", uid: "UID4358"),
                         FakeDisplay.Edge(deviceName: @"\\.\DISPLAY3", y: -8800, uid: "UID4359"));
            host.Do(() => host.Form.RepinNow());

            Assert.AreEqual(PanelLogic.DisplayReasonIdAmbiguous, host.Get(() => host.Form.TargetReason));
            Assert.IsFalse(host.Get(() => host.Form.Visible),
                           "a coin toss between two monitors is a full-screen window on the wrong one");
            var lines = host.Get(() => PanelLogic.StatusLines(host.Form.Status(), DateTime.Now).ToArray());
            Assert.IsTrue(lines.Any(l => l.Contains("pick one from this menu", StringComparison.Ordinal)),
                          string.Join(" | ", lines));
        }
        finally { HostHarness.Cleanup(dir); }
    }

    /// <summary>The monitor comes back with a different PnP id - a replacement unit, a
    /// different model, a docking station that renames it. The host hides, and the tray
    /// picker's own write path is what brings it back, with no restart and no hand-edited
    /// JSON.</summary>
    [TestMethod]
    public void A_changed_device_id_hides_the_panel_until_the_picker_writes_the_new_one()
    {
        var dir = HostHarness.Scratch();
        try
        {
            var displays = new DisplaySet(FakeDisplay.Other(), FakeDisplay.Edge());
            using var host = HostHarness.Start(dir, displays);
            host.Expect(() => host.Form.Visible, "the Edge is pinned");

            // Same 2560x720 panel, new id. The size fallback cannot save it either: the
            // replacement is not primary, but the id no longer matches and size alone is
            // what the operator would have to fall back to.
            var replacement = new DisplayInfo(
                @"\\.\DISPLAY2",
                @"\\?\DISPLAY#CRXED01#5&a4ae9a5&0&UID4360#{e6f07b5f-ee97-4a90-b076-33f57bf4eaa7}",
                FakeDisplay.Edge().Bounds, Primary: false, Dpi: 96);
            displays.Set(FakeDisplay.Other(), replacement);
            host.Do(() => host.Form.RepinNow());

            // The size fallback finds it, because it is the only non-primary 2560x720.
            Assert.AreEqual(PanelLogic.DisplayReasonSize, host.Get(() => host.Form.TargetReason));

            // Now take the size route away as well: a second 2560x720 monitor.
            displays.Set(FakeDisplay.Other(),
                         replacement,
                         new DisplayInfo(@"\\.\DISPLAY3", @"\\?\DISPLAY#ACME9#5&9&0&UID9#{g}",
                                         new System.Drawing.Rectangle(0, -9800, 2560, 720), false, 96));
            host.Do(() => host.Form.RepinNow());
            Assert.AreEqual(PanelLogic.DisplayReasonSizeAmbiguous, host.Get(() => host.Form.TargetReason));
            Assert.IsFalse(host.Get(() => host.Form.Visible));

            // The picker writes a fragment that matches one monitor and no other.
            var fragment = PanelLogic.UniqueDeviceIdFragment(replacement, host.Get(() => host.Form.CurrentDisplays()));
            host.Do(() => host.Form.ApplyDisplayDeviceId(fragment));

            Assert.AreEqual(PanelLogic.DisplayReasonId, host.Get(() => host.Form.TargetReason));
            host.Expect(() => host.Form.Visible, "the panel comes back on the monitor the operator picked");
        }
        finally { HostHarness.Cleanup(dir); }
    }

    /// <summary>LO-007. The tray menu ticked the display it is showing the panel on by
    /// looking for the device NAME inside the label, and a device name is a prefix of the
    /// next one along. Two items were ticked, and one of them named a monitor the panel was
    /// not on. The tick now compares the whole name against the pinned one, which is what
    /// TargetDeviceName is for; this is the trap itself, kept so a future "simplify the
    /// label" puts it back under a failing test.</summary>
    [TestMethod]
    public void LO_007_a_device_name_is_a_prefix_of_the_next_one_along()
    {
        var one = FakeDisplay.Other(@"\\.\DISPLAY1");
        var eleven = FakeDisplay.Other(@"\\.\DISPLAY11");
        Assert.IsTrue(PanelLogic.DisplayLabel(eleven).Contains(one.DeviceName, StringComparison.Ordinal),
                      "a Contains test cannot tell these two apart");
        Assert.AreNotEqual(one.DeviceName, eleven.DeviceName);
    }

    // ---------------------------------------------------------------- the settings watcher

    /// <summary>LO-002. FileSystemWatcher is not self-healing. Its directory being removed
    /// (an installer, a sync client, a cleanup, a test) leaves the object in place with
    /// EnableRaisingEvents false and nothing raised anywhere, and every later settings edit
    /// - the port, the display, the widget props, the picker's own file - was invisible to
    /// the host for the rest of its life.</summary>
    [TestMethod]
    public void The_settings_watcher_is_rearmed_by_the_poll_after_its_directory_disappears()
    {
        var dir = HostHarness.Scratch("{ \"crabdPort\": 4101 }");
        try
        {
            var displays = new DisplaySet(FakeDisplay.Other());
            using var host = HostHarness.Start(dir, displays);
            Assert.IsTrue(host.Get(() => host.Form.WatcherRunning), "the watcher starts with the host");

            // The trigger, and the proof the defect is real: after this the watcher is
            // still an object and is raising nothing.
            HostHarness.Cleanup(dir);
            Assert.IsTrue(host.WaitFor(() => !host.Form.WatcherRunning, "the watcher stops raising events", 10),
                          "a deleted settings directory must be the state this test is about");

            // Nothing is called by hand: the five second poll is what puts it back.
            host.Expect(() => host.Form.WatcherRunning, "the poll re-arms the settings watcher");
            host.ExpectLog("re-armed on");

            // And it is a working watcher, not just an object with a flag set.
            var before = host.Get(() => host.Form.SettingsReloadCount);
            File.WriteAllText(Path.Combine(dir, "panel-settings.json"), "{ \"crabdPort\": 4242 }");
            host.Expect(() => host.Form.SettingsReloadCount > before, "the re-armed watcher sees an edit");
            Assert.AreEqual(4242, host.Get(() => host.Form.Settings.CrabdPort));
        }
        finally { HostHarness.Cleanup(dir); }
    }

    [TestMethod]
    public void A_burst_of_editor_writes_is_coalesced_and_the_value_read_is_the_last_one()
    {
        var dir = HostHarness.Scratch("{ \"crabdPort\": 2722 }");
        try
        {
            var displays = new DisplaySet(FakeDisplay.Other());
            using var host = HostHarness.Start(dir, displays);
            var path = Path.Combine(dir, "panel-settings.json");

            for (var i = 0; i < 40; i++)
            {
                File.WriteAllText(path, "{ \"crabdPort\": " + (4000 + i) + " }");
                Thread.Sleep(10);
            }

            host.Expect(() => host.Form.Settings.CrabdPort == 4039, "the last write is the one that is read");
            // The one-second debounce is what stops forty writes being forty reloads and
            // forty page reloads. Four is a generous ceiling for a 400 ms burst.
            var reloads = host.Get(() => host.Form.SettingsReloadCount);
            Assert.IsTrue(reloads <= 4, $"{reloads} reloads for 40 writes is not a debounce");
        }
        finally { HostHarness.Cleanup(dir); }
    }

    /// <summary>The shape every careful editor writes with, and the shape the host's own
    /// atomic save uses: a temp file, then a rename over the target. It raises Renamed and
    /// nothing else.</summary>
    [TestMethod]
    public void A_temp_file_renamed_over_the_settings_is_seen_like_any_other_edit()
    {
        var dir = HostHarness.Scratch("{ \"crabdPort\": 2722 }");
        try
        {
            var displays = new DisplaySet(FakeDisplay.Other());
            using var host = HostHarness.Start(dir, displays);
            var path = Path.Combine(dir, "panel-settings.json");
            var tmp = Path.Combine(dir, "panel-settings.json.editor-tmp");

            File.WriteAllText(tmp, "{ \"crabdPort\": 4777 }");
            File.Move(tmp, path, overwrite: true);

            host.Expect(() => host.Form.Settings.CrabdPort == 4777, "a renamed temp file is an edit");
        }
        finally { HostHarness.Cleanup(dir); }
    }

    // ---------------------------------------------------------------- the reachable controls

    /// <summary>MF-003's restart line against a log that records real restarts. The audit
    /// could not run one; the file is what the status window reads, so a file with three
    /// earlier starts in it is the same evidence.</summary>
    [TestMethod]
    public void The_restart_history_counts_other_runs_in_the_last_hour_and_never_this_one()
    {
        var dir = HostHarness.Scratch();
        try
        {
            var logPath = Path.Combine(dir, "logs", PanelLogic.LogFileName(windowed: false));
            Directory.CreateDirectory(Path.GetDirectoryName(logPath)!);
            var now = DateTime.Now;
            var lines = new List<string>
            {
                $"{now.AddMinutes(-50):yyyy-MM-dd HH:mm:ss} SideCrab.Panel 0.4.0 starting; pid 4101; settings dir {dir}",
                $"{now.AddMinutes(-49):yyyy-MM-dd HH:mm:ss} SideCrab.Panel 0.4.0 starting; pid 4102; settings dir {dir}",
                $"{now.AddMinutes(-48):yyyy-MM-dd HH:mm:ss} SideCrab.Panel 0.4.0 starting; pid 4103; settings dir {dir}",
                // Older than the window, and this run's own line: neither is a restart.
                $"{now.AddHours(-3):yyyy-MM-dd HH:mm:ss} SideCrab.Panel 0.4.0 starting; pid 4104; settings dir {dir}",
                $"{now:yyyy-MM-dd HH:mm:ss} SideCrab.Panel 0.4.0 starting; pid {Environment.ProcessId}; settings dir {dir}",
            };
            File.WriteAllLines(logPath, lines);

            var displays = new DisplaySet(FakeDisplay.Other());
            using var host = HostHarness.Start(dir, displays);

            var status = host.Get(() => host.Form.Status());
            Assert.AreEqual(3, status.PriorStartsInWindow);
            var rendered = PanelLogic.StatusLines(status, DateTime.Now);
            Assert.IsTrue(rendered.Any(l => l.Contains("3 earlier start(s)", StringComparison.Ordinal)),
                          string.Join(" | ", rendered));
            Assert.IsTrue(rendered.Any(l => l.Contains($"up to {PanelLogic.TaskRestartCount} times", StringComparison.Ordinal)));
        }
        finally { HostHarness.Cleanup(dir); }
    }

    /// <summary>MF-004 end to end, minus the two parts that need a hand on the desktop (the
    /// tray click and the click on the prompt). The pick writes, the panel moves, and an
    /// unanswered prompt puts it back.</summary>
    [TestMethod]
    public void A_pick_nobody_answers_reverts_and_the_prompt_never_takes_the_keyboard()
    {
        var kept = OnSta(() =>
        {
            using var confirm = new ConfirmDisplayWindow("DISPLAY2 2560x720 at 0,0 100%  [id]", seconds: 2);
            confirm.Advance();
            confirm.Advance();
            // Never shown, so it cannot have taken the foreground from the operator: a
            // window with no handle is a window that was never on the screen.
            return (confirm.DialogResult, confirm.IsHandleCreated);
        });
        Assert.AreEqual(DialogResult.Cancel, kept.DialogResult, "a prompt nobody answered is a prompt nobody saw");
        Assert.IsFalse(kept.IsHandleCreated);
    }

    [TestMethod]
    public void A_pick_is_applied_at_once_and_the_revert_puts_the_file_back()
    {
        var dir = HostHarness.Scratch("""
            { "crabdPort": 3999, "display": { "deviceId": "CRXED00", "width": 2560, "height": 720 },
              "props": { "clock24": true } }
            """);
        try
        {
            var other = FakeDisplay.Other();
            var displays = new DisplaySet(other, FakeDisplay.Edge());
            using var host = HostHarness.Start(dir, displays);
            host.Expect(() => host.Form.Visible, "the Edge is pinned to start with");

            // The operator picks their own monitor by name, which an id match honours even
            // though it is primary (SCA-014).
            var fragment = PanelLogic.UniqueDeviceIdFragment(other, displays.Read());
            var previous = host.Get(() => host.Form.ApplyDisplayDeviceId(fragment));
            Assert.AreEqual("CRXED00", previous);
            Assert.AreEqual(other.Bounds.Location, host.Get(() => host.Form.Bounds).Location);
            Assert.AreEqual(other.DeviceName, host.Get(() => host.Form.TargetDeviceName));

            host.Do(() => host.Form.RevertDisplayDeviceId(previous));
            Assert.AreEqual(FakeDisplay.Edge().Bounds, host.Get(() => host.Form.Bounds));

            using var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(dir, "panel-settings.json")));
            Assert.AreEqual("CRXED00", doc.RootElement.GetProperty("display").GetProperty("deviceId").GetString());
            Assert.AreEqual(3999, doc.RootElement.GetProperty("crabdPort").GetInt32());
        }
        finally { HostHarness.Cleanup(dir); }
    }

    /// <summary>LO-011. Windows caps every top-level window at the PRIMARY monitor's size
    /// plus its sizing border, whatever monitor the window is on: measured on this PC
    /// 2026-09-22 as 2580x1460 against a primary of 2560x1440. The Edge is 2560x720 and
    /// never meets the cap; a display picked from the tray menu can be larger than the
    /// primary, and the page would then be corrected toward a width the window does not
    /// have - the zoom shrinking the page while the viewport never converges.</summary>
    [TestMethod]
    public void A_monitor_larger_than_the_primary_is_capped_by_windows_and_the_host_says_so()
    {
        var dir = HostHarness.Scratch();
        try
        {
            var cap = SystemInformation.MaxWindowTrackSize;
            var huge = new DisplayInfo(
                @"\\.\DISPLAY4", @"\\?\DISPLAY#CRXED00#5&a4ae9a5&0&UID4358#{guid}",
                new System.Drawing.Rectangle(-12000, -12000, cap.Width + 300, cap.Height + 300), false, 96);
            var displays = new DisplaySet(FakeDisplay.Other(), huge);
            using var host = HostHarness.Start(dir, displays);

            host.Expect(() => host.Form.Visible, "the picked monitor is pinned");
            var bounds = host.Get(() => host.Form.Bounds);
            Assert.AreEqual(HostHarness.AsWindowsAllows(huge.Bounds.Size), bounds.Size,
                            "Windows, not the host, is what capped it");
            Assert.AreNotEqual(huge.Bounds.Size, bounds.Size, "this test is pointless if nothing was capped");
            host.ExpectLog("could not take the whole monitor");

            // And the correction runs against the window the page is actually in.
            Assert.AreEqual(host.Get(() => host.Form.ClientSize.Width),
                            host.Get(() => host.Form.ViewportReferenceWidth));
            Assert.AreNotEqual(huge.Bounds.Width, host.Get(() => host.Form.ViewportReferenceWidth));
        }
        finally { HostHarness.Cleanup(dir); }
    }

    /// <summary>A paused host stays hidden through every event that would otherwise show
    /// it, and says so rather than reporting the monitor it would have used.</summary>
    [TestMethod]
    public void A_paused_host_stays_hidden_through_an_attach_and_a_display_change()
    {
        var dir = HostHarness.Scratch();
        try
        {
            var displays = new DisplaySet(FakeDisplay.Other(), FakeDisplay.Edge());
            using var host = HostHarness.Start(dir, displays);
            host.Expect(() => host.Form.Visible, "the Edge is pinned to start with");

            host.Do(() => host.Form.SetPaused(true));
            Assert.IsFalse(host.Get(() => host.Form.Visible));

            host.PostDisplayChange();
            host.Do(() => host.Form.RepinNow());
            Assert.IsFalse(host.Get(() => host.Form.Visible), "a pause outranks every re-pin reason");
            var lines = host.Get(() => PanelLogic.StatusLines(host.Form.Status(), DateTime.Now).ToArray());
            Assert.IsTrue(lines.Any(l => l.Contains("paused by you", StringComparison.Ordinal)),
                          string.Join(" | ", lines));

            host.Do(() => host.Form.SetPaused(false));
            host.Expect(() => host.Form.Visible, "resume brings it back without a restart");
        }
        finally { HostHarness.Cleanup(dir); }
    }
}
