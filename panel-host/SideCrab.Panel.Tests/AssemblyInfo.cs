// The PanelForm tests construct real WinForms objects on their own STA thread and the log
// tests write whole files. Running them beside each other buys nothing and would make a
// failure hard to read.
[assembly: DoNotParallelize]
