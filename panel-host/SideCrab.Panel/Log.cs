namespace SideCrab.Panel;

/// <summary>Append-only line log at <c>~/.sidecrab/logs/panel.log</c>, rolled once to
/// <c>panel.log.1</c> past 1 MB. The only account of what the host did: which monitor it
/// picked, the viewport it measured, every navigation it refused, every re-pin.</summary>
public sealed class Log
{
    private const long RollBytes = 1024 * 1024;
    private readonly object _lock = new();
    private readonly string _path;

    public Log(string path) { _path = path; }

    public string Path => _path;

    public void Write(string line)
    {
        var text = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {line}";
        lock (_lock)
        {
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
            }
            catch
            {
                // A log that cannot be written must never take the panel down.
            }
        }
    }
}
