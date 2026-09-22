using System.Drawing;

namespace SideCrab.Panel;

/// <summary>MF-003 and MF-004 (host 0.4.0, provisional label): the host's reachable
/// controls, on the PRIMARY display, where the operator is.
///
/// The panel itself is a borderless never-activating window on a 2560x720 strip that may
/// be switched off, unplugged, or out of the display set entirely under Remote Desktop.
/// Before this, a host in that state could only be reached by reading panel.log and
/// editing JSON.
///
/// NOTHING here activates a window on its own. A NotifyIcon and its menu are shown by the
/// shell in answer to the operator's own click, the status window is ShowWithoutActivation,
/// and the only window that takes the foreground is the display-picker confirmation, which
/// exists precisely because the operator has just asked for something they may not be able
/// to see.</summary>
public sealed class TrayUi : IDisposable
{
    private readonly PanelForm _panel;
    private readonly Log _log;
    private readonly NotifyIcon _icon;
    private readonly ContextMenuStrip _menu;
    private readonly ToolStripMenuItem _pause;
    private readonly ToolStripMenuItem _displays;
    private StatusWindow? _status;

    public TrayUi(PanelForm panel, Log log)
    {
        _panel = panel;
        _log = log;

        _menu = new ContextMenuStrip { ShowImageMargin = false };
        _menu.Opening += (_, _) => BuildMenu();
        _pause = new ToolStripMenuItem("Pause (hide the panel)", null, (_, _) => TogglePause());
        _displays = new ToolStripMenuItem("Show the panel on...");

        _icon = new NotifyIcon
        {
            Icon = LoadIcon(),
            Text = "SideCrab panel",           // 63 characters max; the status window has the rest
            Visible = true,
            ContextMenuStrip = _menu,
        };
        // A double-click on the icon is the shortest route to the status window, which is
        // the only thing here that answers "why is the glass dark".
        _icon.DoubleClick += (_, _) => ShowStatus();
        BuildMenu();
    }

    /// <summary>The app icon out of the running assembly, or the system default. A tray
    /// icon that failed to load is an invisible control, so the fallback is not optional.</summary>
    private static Icon LoadIcon()
    {
        try
        {
            var exe = Environment.ProcessPath;
            if (exe is not null)
            {
                var ico = Icon.ExtractAssociatedIcon(exe);
                if (ico is not null) return ico;
            }
        }
        catch (Exception) { /* SystemIcons below */ }
        return SystemIcons.Application;
    }

    private void BuildMenu()
    {
        _menu.Items.Clear();
        var status = _panel.Status();
        // The first item is not clickable; it is the one-line answer to "what is it doing",
        // so the menu says it without a second window.
        _menu.Items.Add(new ToolStripMenuItem(PanelLogic.EscapeMnemonics(Headline(status))) { Enabled = false });
        _menu.Items.Add(new ToolStripSeparator());
        _menu.Items.Add("Status and diagnostics...", null, (_, _) => ShowStatus());
        _menu.Items.Add("Open the log folder", null, (_, _) => _panel.OpenLogFolder());
        _menu.Items.Add(new ToolStripSeparator());
        _menu.Items.Add("Reload the panel", null, (_, _) => _panel.ReloadPanel());
        _menu.Items.Add("Re-pin now", null, (_, _) => _panel.RepinNow());
        _pause.Text = _panel.Paused ? "Resume the panel" : "Pause (hide the panel)";
        _menu.Items.Add(_pause);
        _menu.Items.Add(new ToolStripSeparator());
        BuildDisplayMenu();
        _menu.Items.Add(_displays);
        _menu.Items.Add(new ToolStripSeparator());
        _menu.Items.Add("Quit until next logon", null, (_, _) => _panel.QuitUntilLogon());
    }

    private static string Headline(PanelLogic.HostStatus s)
    {
        if (s.Paused) return "Paused";
        if (s.Visible && s.TargetLabel is not null) return "On " + s.TargetLabel;
        return "Hidden: " + PanelLogic.HiddenReason(s.TargetReason);
    }

    /// <summary>MF-004. Every display, with the full device id, size, position, scaling
    /// and whether it is primary. Two same-size monitors are told apart by the id, which
    /// is also what gets written: UniqueDeviceIdFragment lengthens the fragment until it
    /// matches one monitor and no other.
    ///
    /// A display whose PnP id is empty (a virtual adapter with nothing attached) is listed
    /// and disabled: there is no fragment that would find it again.</summary>
    private void BuildDisplayMenu()
    {
        _displays.DropDownItems.Clear();
        var displays = _panel.CurrentDisplays();
        if (displays.Count == 0)
        {
            _displays.DropDownItems.Add(new ToolStripMenuItem("No displays were enumerated") { Enabled = false });
            return;
        }
        // LO-007: the WHOLE GDI name, not a Contains over the label. A device name is a
        // prefix of the next one along (DISPLAY1 inside DISPLAY11, which a PC with a
        // virtual display adapter does reach), so two items were ticked and the menu said
        // the panel was on a monitor it was not on.
        var current = _panel.TargetDeviceName;
        foreach (var d in displays)
        {
            var fragment = PanelLogic.UniqueDeviceIdFragment(d, displays);
            var text = PanelLogic.DisplayLabelShort(d) + "\n" + PanelLogic.DisplayLabelId(d);
            var item = new ToolStripMenuItem(PanelLogic.EscapeMnemonics(text))
            {
                Enabled = fragment.Length > 0,
                Checked = string.Equals(current, d.DeviceName, StringComparison.Ordinal),
            };
            if (fragment.Length == 0) item.Text += "   (no device id; cannot be selected)";
            else item.Click += (_, _) => PickDisplay(d, fragment);
            _displays.DropDownItems.Add(item);
        }
    }

    private void TogglePause()
    {
        _panel.SetPaused(!_panel.Paused);
        BuildMenu();
        RepaintStatus();
    }

    /// <summary>LO-009. The status window reads its facts on every OPEN, which left it
    /// saying "Panel: shown on ..." for the rest of a pause, and naming the old display for
    /// the rest of a pick, with the operator looking straight at it. Every control that
    /// changes what it says repaints it, and only while it is already visible, so nothing
    /// here puts a window on the screen.</summary>
    private void RepaintStatus()
    {
        if (_status is null || _status.IsDisposed || !_status.Visible) return;
        _status.Repaint(_panel.Status());
    }

    private void ShowStatus()
    {
        if (_status is null || _status.IsDisposed) _status = new StatusWindow(_panel);
        _status.Repaint(_panel.Status());
        _status.Show();
        // Not Activate(): the operator clicked the tray, and this host takes the keyboard
        // from nothing, ever. The shell raises the window for the click itself.
        _status.BringToFront();
    }

    /// <summary>Apply, then ask. The confirmation window is on the PRIMARY display and
    /// counts down <see cref="PanelLogic.DisplayRevertSeconds"/>: a picked display that
    /// turns out to be the wrong one, switched off, or out of the display set leaves the
    /// operator with the panel somewhere they cannot see, and the only way back would be
    /// the JSON file this menu exists to replace. Nothing is kept unless it is confirmed.</summary>
    private void PickDisplay(DisplayInfo d, string fragment)
    {
        string? previous;
        try
        {
            previous = _panel.ApplyDisplayDeviceId(fragment);
        }
        catch (Exception ex)
        {
            MessageBox.Show("The display could not be saved: " + ex.Message,
                            "SideCrab panel", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }
        using var confirm = new ConfirmDisplayWindow(PanelLogic.DisplayLabel(d));
        var kept = confirm.ShowDialog() == DialogResult.OK;
        if (kept) _log.Write("tray: display selection kept");
        else _panel.RevertDisplayDeviceId(previous);
        BuildMenu();
        RepaintStatus();
    }

    public void Dispose()
    {
        _icon.Visible = false;
        _icon.Dispose();
        _menu.Dispose();
        _status?.Dispose();
    }
}

/// <summary>What the host is doing and where to look, in plain lines. Never topmost, and
/// it does not activate itself: PanelLogic.StatusLines is the wording and a test pins it.</summary>
public sealed class StatusWindow : Form
{
    private readonly TextBox _text;

    public StatusWindow(PanelForm panel)
    {
        Text = "SideCrab panel host";
        Size = new Size(780, 320);
        StartPosition = FormStartPosition.CenterScreen;
        MinimizeBox = false;
        ShowInTaskbar = true;

        _text = new TextBox
        {
            Multiline = true,
            ReadOnly = true,
            Dock = DockStyle.Fill,
            ScrollBars = ScrollBars.Vertical,
            Font = new Font("Consolas", 9.75f),
            BackColor = Color.White,
        };
        var buttons = new FlowLayoutPanel
        {
            Dock = DockStyle.Bottom,
            FlowDirection = FlowDirection.RightToLeft,
            Height = 44,
            Padding = new Padding(6),
        };
        buttons.Controls.Add(MakeButton("Close", (_, _) => Hide()));
        buttons.Controls.Add(MakeButton("Refresh", (_, _) => Repaint(panel.Status())));
        buttons.Controls.Add(MakeButton("Open the log folder", (_, _) => panel.OpenLogFolder()));
        buttons.Controls.Add(MakeButton("Re-pin now", (_, _) => { panel.RepinNow(); Repaint(panel.Status()); }));
        buttons.Controls.Add(MakeButton("Reload the panel", (_, _) => { panel.ReloadPanel(); Repaint(panel.Status()); }));
        Controls.Add(_text);
        Controls.Add(buttons);
    }

    private static Button MakeButton(string text, EventHandler onClick)
    {
        var b = new Button { Text = text, AutoSize = true, Margin = new Padding(4) };
        b.Click += onClick;
        return b;
    }

    protected override bool ShowWithoutActivation => true;

    /// <summary>Closing this window must not close the host. The operator reaching for the
    /// X means "hide this", not "stop the panel".</summary>
    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        if (e.CloseReason == CloseReason.UserClosing)
        {
            e.Cancel = true;
            Hide();
            return;
        }
        base.OnFormClosing(e);
    }

    public void Repaint(PanelLogic.HostStatus status) =>
        _text.Lines = PanelLogic.StatusLines(status, DateTime.Now).ToArray();
}

/// <summary>"Keep this display?", on the primary, counting down. The same shape Windows
/// uses for a resolution change and for the same reason: the operator may not be able to
/// see the result of what they just picked. Shown for EVERY pick, the Edge included: a
/// pick that cannot be kept is not a choice, and one that cannot be undone is a panel on a
/// monitor the operator cannot see.</summary>
public sealed class ConfirmDisplayWindow : Form
{
    private readonly Label _countdown;
    private readonly System.Windows.Forms.Timer _tick = new() { Interval = 1000 };
    private int _left;

    /// <summary><paramref name="seconds"/> is the production constant everywhere but the
    /// test that proves a timeout is a REVERT: at ten it would be a ten-second test, and
    /// showing the window to run it would take the operator's keyboard.</summary>
    public ConfirmDisplayWindow(string displayLabel, int seconds = PanelLogic.DisplayRevertSeconds)
    {
        _left = seconds;
        Text = "SideCrab panel";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MinimizeBox = false;
        MaximizeBox = false;
        // CenterScreen is the PRIMARY display, which is the point: the panel has just
        // moved somewhere the operator may not be looking at, or able to look at.
        StartPosition = FormStartPosition.CenterScreen;
        Size = new Size(660, 300);
        TopMost = true;

        // A read-only TextBox and not a Label. The device id is one long ampersand-laden
        // token: a Label eats the ampersands as mnemonics and will not break a token with
        // no spaces in it, so the line the operator most needs to read is the one that
        // does not render.
        var body = new TextBox
        {
            Dock = DockStyle.Fill,
            Multiline = true,
            ReadOnly = true,
            BorderStyle = BorderStyle.None,
            BackColor = SystemColors.Control,
            TabStop = false,
            WordWrap = true,
            ScrollBars = ScrollBars.Vertical,
            Text = "The panel is now set to:\r\n\r\n" + displayLabel,
        };
        // The countdown is its OWN control, docked above the buttons. Inside the body it
        // scrolled out of sight behind a long device id, which is the one line that must
        // never be the one below the fold.
        _countdown = new Label { Dock = DockStyle.Bottom, Height = 32, Padding = new Padding(12, 6, 12, 0) };
        var keep = new Button { Text = "Keep this display", DialogResult = DialogResult.OK, AutoSize = true };
        var revert = new Button { Text = "Put it back", DialogResult = DialogResult.Cancel, AutoSize = true };
        var row = new FlowLayoutPanel
        {
            Dock = DockStyle.Bottom,
            FlowDirection = FlowDirection.RightToLeft,
            Height = 48,
            Padding = new Padding(8),
        };
        row.Controls.Add(keep);
        row.Controls.Add(revert);
        Controls.Add(body);
        Controls.Add(_countdown);
        Controls.Add(row);
        AcceptButton = keep;
        CancelButton = revert;

        Countdown();
        _tick.Tick += (_, _) => Advance();
        _tick.Start();
    }

    /// <summary>One second of the countdown. Internal so the timeout can be run with no
    /// window on the desktop and without waiting out the real ten.</summary>
    internal void Advance()
    {
        _left--;
        if (_left <= 0)
        {
            _tick.Stop();
            // Timed out is REVERT, never keep. A dialog nobody answered is a dialog
            // nobody saw.
            DialogResult = DialogResult.Cancel;
            Close();
            return;
        }
        Countdown();
    }

    private void Countdown() =>
        _countdown.Text = $"Keeping it? This goes back on its own in {_left} second(s).";

    protected override void Dispose(bool disposing)
    {
        if (disposing) _tick.Dispose();
        base.Dispose(disposing);
    }
}
