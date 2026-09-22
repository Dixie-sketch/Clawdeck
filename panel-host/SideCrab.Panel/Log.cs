namespace SideCrab.Panel;

/// <summary>Append-only line log under <c>~/.sidecrab/logs/</c>, rolled once to
/// <c>&lt;name&gt;.1</c> past 1 MB. The only account of what the host did: which monitor
/// it picked, the viewport it measured, every navigation it refused, every re-pin.
///
/// SCA-031: the file is per INSTANCE (PanelLogic.LogFileName), because this lock is per
/// object and spans neither instance nor process. Two writers on one file lost 3014 of
/// 4000 lines in the audit's fixture, under the roll size, and swallowed every one of
/// them. Anything this instance still cannot write is COUNTED and reported on the next
/// line that gets through, so a gap in the file is visible as a number rather than as
/// nothing at all.</summary>
public sealed class Log
{
    private const long RollBytes = 1024 * 1024;
    private readonly object _lock = new();
    private readonly string _path;
    private int _dropped;

    public Log(string path) { _path = path; }

    public string Path => _path;

    /// <summary>Lines this instance could not write. Never resets on read.</summary>
    public int Dropped => Volatile.Read(ref _dropped);

    public void Write(string line)
    {
        lock (_lock)
        {
            var prefix = string.Empty;
            if (_dropped > 0) prefix = $"[{_dropped} earlier line(s) could not be written] ";
            var text = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {prefix}{line}";
            try
            {
                var dir = System.IO.Path.GetDirectoryName(_path);
                if (dir is not null) Directory.CreateDirectory(dir);
                var info = new FileInfo(_path);
                if (info.Exists && info.Length > RollBytes)
                {
                    File.Copy(_path, _path + ".1", overwrite: true);
                    File.WriteAllText(_path, string.Empty);
                }
                File.AppendAllText(_path, text + Environment.NewLine);
                _dropped = 0;
            }
            catch
            {
                // A log that cannot be written must never take the panel down.
                if (_dropped < int.MaxValue) _dropped++;
            }
        }
    }
}
