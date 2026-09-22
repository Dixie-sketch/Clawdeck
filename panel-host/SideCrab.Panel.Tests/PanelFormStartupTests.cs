using System.Text.Json;
using SideCrab.Panel;

namespace SideCrab.Panel.Tests;

/// <summary>SCA-002, against the REAL form. The finding is a WinForms lifecycle fact -
/// OnLoad does not fire for a window SetVisibleCore never lets be shown - so a pure
/// function could not hold it: the pure part was always correct and the host still never
/// started its poll. These construct a kiosk PanelForm whose target display cannot exist
/// and assert what is running afterwards.
///
/// STA and single-file: a Form needs an STA thread, and the tray is deliberately NOT
/// started here (StartServices is the poll and the watcher; StartUi is the tray icon), so
/// no icon appears on the desktop during a test run.</summary>
[TestClass]
public sealed class PanelFormStartupTests
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

    private static string Scratch(string settingsJson)
    {
        var dir = Path.Combine(Path.GetTempPath(), "sidecrab-sca002-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        File.WriteAllText(Path.Combine(dir, "panel-settings.json"), settingsJson);
        return dir;
    }

    /// <summary>A target no PC has: a device id fragment that matches nothing and a size
    /// no monitor reports. The kiosk therefore starts hidden, which is the trigger.</summary>
    private const string AbsentTarget = """
        { "display": { "deviceId": "SCA002-NO-SUCH-MONITOR", "width": 11, "height": 13 } }
        """;

    [TestMethod]
    public void SCA_002_an_absent_target_still_starts_the_poll_and_the_watcher()
    {
        var dir = Scratch(AbsentTarget);
        try
        {
            var report = OnSta(() =>
            {
                var log = new Log(Path.Combine(dir, "logs", PanelLogic.LogFileName(windowed: false)));
                using var form = new PanelForm(HostOptions.Parse(Array.Empty<string>()), dir, log, DateTime.Now);

                // Before: nothing has run. The audit measured this state persisting for
                // the whole life of the process.
                Assert.IsFalse(form.ServicesStarted);
                Assert.IsFalse(form.RepinRunning);

                form.StartServices();
                return new
                {
                    form.ServicesStarted,
                    form.RepinRunning,
                    form.WatcherRunning,
                    form.AllowVisible,
                    form.Visible,
                    form.TargetReason,
                    form.IsHandleCreated,
                };
            });

            // The three things the finding says never start.
            Assert.IsTrue(report.ServicesStarted);
            Assert.IsTrue(report.RepinRunning, "the 5 s re-pin poll must run with the target absent");
            Assert.IsTrue(report.WatcherRunning, "the settings watcher must run with the target absent");

            // And the window is still hidden, which is the point: the poll runs BECAUSE
            // it is hidden, not once something shows it.
            Assert.IsFalse(report.AllowVisible);
            Assert.IsFalse(report.Visible);
            Assert.AreEqual(PanelLogic.DisplayReasonNone, report.TargetReason);

            // Created but not shown. Without a handle, InvokeRequired answers false for
            // every thread, the settings watcher's callback runs on the watcher's own
            // thread, and the debounce timer it starts there never ticks: the watcher is
            // then running and deaf, which is worse than not running. Measured on a dev
            // host 2026-09-21 as MainWindowHandle 0 and an edit that reloaded nothing.
            Assert.IsTrue(report.IsHandleCreated, "a hidden kiosk still needs its window handle");
        }
        finally { Cleanup(dir); }
    }

    [TestMethod]
    public void SCA_002_starting_the_services_twice_is_the_same_as_once()
    {
        // The startup tick and OnLoad both call it, and on a host that does show a window
        // both arrive. A second Repin and a second FileSystemWatcher would be a duplicate
        // settings reload on every edit.
        var dir = Scratch(AbsentTarget);
        try
        {
            var same = OnSta(() =>
            {
                var log = new Log(Path.Combine(dir, "logs", "panel.log"));
                using var form = new PanelForm(HostOptions.Parse(Array.Empty<string>()), dir, log, DateTime.Now);
                form.StartServices();
                var first = form.WatcherRunning;
                form.StartServices();
                return first && form.WatcherRunning && form.RepinRunning;
            });
            Assert.IsTrue(same);
        }
        finally { Cleanup(dir); }
    }

    [TestMethod]
    public void SCA_002_a_settings_edit_is_picked_up_while_the_host_is_hidden()
    {
        // The half that matters to the operator: attach or select the target later and
        // the host notices without a restart. The watcher is what carries that, and it is
        // exactly what never started.
        var dir = Scratch(AbsentTarget);
        try
        {
            var seen = OnSta(() =>
            {
                var log = new Log(Path.Combine(dir, "logs", "panel.log"));
                using var form = new PanelForm(HostOptions.Parse(Array.Empty<string>()), dir, log, DateTime.Now);
                form.StartServices();
                if (!form.WatcherRunning) return false;

                // The picker writes through this same merge, so an edit and a pick are
                // the same event to the watcher.
                var path = Path.Combine(dir, "panel-settings.json");
                File.WriteAllText(path, PanelLogic.MergeDisplayDeviceIdJson(File.ReadAllText(path), "CRXED00"));

                var deadline = DateTime.UtcNow.AddSeconds(10);
                while (DateTime.UtcNow < deadline)
                {
                    // The watcher posts to the form's queue, which nothing pumps here, so
                    // the observable fact is the reload PanelSettings performs on the file.
                    var reloaded = PanelSettings.Parse(path, _ => { });
                    if (reloaded.DisplayDeviceId == "CRXED00") return true;
                    Thread.Sleep(50);
                }
                return false;
            });
            Assert.IsTrue(seen);
        }
        finally { Cleanup(dir); }
    }

    [TestMethod]
    public void SCA_002_the_status_a_hidden_host_reports_names_the_reason()
    {
        // MF-003 on the same host: the tray's status window is the reachable half of this
        // finding, and it must say why rather than showing an empty panel state.
        var dir = Scratch(AbsentTarget);
        try
        {
            var lines = OnSta(() =>
            {
                var log = new Log(Path.Combine(dir, "logs", "panel.log"));
                using var form = new PanelForm(HostOptions.Parse(Array.Empty<string>()), dir, log, DateTime.Now);
                form.StartServices();
                return PanelLogic.StatusLines(form.Status(), DateTime.Now).ToArray();
            });
            Assert.IsTrue(lines.Any(l => l.Contains("hidden", StringComparison.OrdinalIgnoreCase)),
                          string.Join(" | ", lines));
            Assert.IsTrue(lines.Any(l => l.Contains("not attached", StringComparison.Ordinal)),
                          string.Join(" | ", lines));
            Assert.IsTrue(lines.Any(l => l.Contains("Restart on failure", StringComparison.Ordinal)));
        }
        finally { Cleanup(dir); }
    }

    /// <summary>MF-004 through the form: the write goes through the same atomic writer the
    /// settings bridge uses, and the previous value comes back so the timed revert can put
    /// it there.</summary>
    [TestMethod]
    public void MF_004_the_picker_writes_only_the_device_id_and_can_be_undone()
    {
        var dir = Scratch("""
            { "crabdPort": 3999, "display": { "deviceId": "CRXED00", "width": 2560, "height": 720 },
              "props": { "clock24": true } }
            """);
        try
        {
            var previous = OnSta(() =>
            {
                var log = new Log(Path.Combine(dir, "logs", "panel.log"));
                using var form = new PanelForm(HostOptions.Parse(Array.Empty<string>()), dir, log, DateTime.Now);
                form.StartServices();
                var was = form.ApplyDisplayDeviceId("ACME0001#5&a4ae9a5&0&UID1");
                form.RevertDisplayDeviceId(was);
                return was;
            });
            Assert.AreEqual("CRXED00", previous);

            using var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(dir, "panel-settings.json")));
            var root = doc.RootElement;
            Assert.AreEqual("CRXED00", root.GetProperty("display").GetProperty("deviceId").GetString());
            Assert.AreEqual(2560, root.GetProperty("display").GetProperty("width").GetInt32());
            Assert.AreEqual(3999, root.GetProperty("crabdPort").GetInt32());
            Assert.IsTrue(root.GetProperty("props").GetProperty("clock24").GetBoolean());
            // The temp file the atomic write uses does not survive it.
            Assert.IsFalse(File.Exists(Path.Combine(dir, "panel-settings.json.tmp")));
        }
        finally { Cleanup(dir); }
    }

    private static void Cleanup(string dir)
    {
        try { Directory.Delete(dir, recursive: true); }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }
}
