using System.Text.Json;
using Amadeus.Wallpaper;

if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException("Windows wallpaper helper only.");
void Report(object value)
{
    try { Console.WriteLine(JsonSerializer.Serialize(value)); Console.Out.Flush(); }
    catch (IOException) { /* The owner may have died; closed telemetry must not interrupt restoration. */ }
}
var state = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Amadeus", "wallpaper");
var journal = Path.Combine(state, "session.json");
var host = new LivelyHost();
var session = new Session(host, journal);
try
{
    var command = args.FirstOrDefault() ?? "inspect";
    if (command == "screenshot" && args.Length == 2)
    {
        await host.Screenshot(args[1]);
        Report(new { screenshot = Path.GetFullPath(args[1]) });
        return 0;
    }
    if (command == "inspect")
    {
        using var running = LivelyHost.Running();
        Report(new { executable = LivelyHost.FindExecutable(), running = running is not null,
            recoveryPending = File.Exists(journal),
            wallpapers = running is null ? null : await host.GetWallpapers(),
            options = running is null ? null : await host.GetOptions() });
        return 0;
    }
    if (command == "prepare")
    {
        Report(new { prepared = LivelyHost.PrepareFirstRun(LivelyHost.DataDirectory) });
        return 0;
    }
    if (command is not ("run" or "recover")) throw new ArgumentException("Expected inspect, prepare, run or recover.");
    if (command == "run" && (args.Length != 2 || !Uri.TryCreate(args[1], UriKind.Absolute, out var url)
        || url.Scheme != "http" || url.Host != "127.0.0.1" || url.AbsolutePath != "/wallpaper/lively/index.html"))
        throw new ArgumentException("Expected the local Amadeus wallpaper wrapper URL.");
    Directory.CreateDirectory(state);
    // Cross-checkout exclusion: an independent cleanup process may still own the desktop.
    using var sessionLock = new FileStream(Path.Combine(state, "session.lock"), FileMode.OpenOrCreate,
        FileAccess.ReadWrite, FileShare.None);
    if (command == "recover" && !File.Exists(journal))
    {
        await host.RecoverLaunchImages();
        Report(new { status = "clean" });
        return 0;
    }
    // 启动宿主前先验证 Pending 日志及其原请求接收进程。
    session.PrepareRecovery();
    await host.EnsureRunning();
    await session.Restore(host.StartedProcess);
    if (command == "recover") { Report(new { status = "restored" }); return 0; }
    // Recovery may have stopped the old owned host.
    await host.EnsureRunning();
    var snapshot = new Snapshot(await host.WriteProject(args[1]), await host.PrimaryMonitor(),
        await host.GetOptions(), await host.GetWallpapers(), host.StartedProcess);
    await session.RunMounted(snapshot, async () => {
        Report(new { status = "mounted" });
        // EOF also fires if Electron crashes. This process stays alive to restore.
        using var lively = LivelyHost.Running() ?? throw new IOException("Lively exited after mounting.");
        var input = Console.In.ReadLineAsync();
        var hostExited = lively.WaitForExitAsync();
        if (await Task.WhenAny(input, hostExited) == hostExited)
            throw new IOException("Lively exited unexpectedly; the wallpaper recovery journal is retained.");
    });
    Report(new { status = "restored" });
    return 0;
}
catch (Exception error)
{
    // 未接管清理的初始化错误才允许释放自有宿主。
    try { await session.ReleaseUnstartedHost(host.StartedProcess); }
    catch (Exception cleanup) { Console.Error.WriteLine(cleanup.Message); }
    Report(new { status = "error", error = error.Message, recoveryPending = File.Exists(journal) });
    return 1;
}
