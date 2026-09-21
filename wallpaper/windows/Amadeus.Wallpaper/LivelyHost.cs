using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using Google.Protobuf.WellKnownTypes;
using Grpc.Core;
using GrpcDotNetNamedPipes;
using Microsoft.Win32;
using Amadeus.Wallpaper.Protocol.Desktop;
using Amadeus.Wallpaper.Protocol.Display;
using Amadeus.Wallpaper.Protocol.Settings;
using Amadeus.Wallpaper.Protocol.Commands;

namespace Amadeus.Wallpaper;

public sealed class LivelyHost : IWallpaperHost
{
    public const string SupportedVersion = "2.2.1.0";
    readonly DesktopService.DesktopServiceClient desktop;
    readonly DisplayService.DisplayServiceClient displays;
    readonly SettingsService.SettingsServiceClient settings;
    readonly CommandsService.CommandsServiceClient commands;
    public ProcessIdentity? StartedProcess { get; private set; }
    public static string DataDirectory => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Lively Wallpaper");
    static DateTime Deadline => DateTime.UtcNow.AddSeconds(15);
    static string LaunchBackup => Path.Combine(DataDirectory, "Amadeus.launch-settings.json");
    record ImageOptions(bool Desktop, bool Lock);
    static void WriteSettings(JsonNode json)
    {
        var file = Path.Combine(DataDirectory, "Settings.json");
        var temporary = file + ".amadeus.tmp";
        using (var stream = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None))
        {
            JsonSerializer.Serialize(stream, json);
            stream.Flush(true);
        }
        File.Move(temporary, file, true);
    }

    // Lively restores its last scene during startup. Suppress its optional
    // screenshot-to-system-wallpaper side effects BEFORE that happens, then
    // restore these two settings after startup. A small durable backup covers
    // interruption between the offline edit and the live RPC handover.
    static void PrepareLaunchImages()
    {
        if (File.Exists(LaunchBackup)) return;
        var file = Path.Combine(DataDirectory, "Settings.json");
        var json = JsonNode.Parse(File.ReadAllText(file))!;
        var previous = new ImageOptions(json["DesktopAutoWallpaper"]?.GetValue<bool>() ?? false,
            json["LockScreenAutoWallpaper"]?.GetValue<bool>() ?? false);
        if (!previous.Desktop && !previous.Lock) return;
        using (var backup = new FileStream(LaunchBackup, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        {
            JsonSerializer.Serialize(backup, previous);
            backup.Flush(true);
        }
        json["DesktopAutoWallpaper"] = false;
        json["LockScreenAutoWallpaper"] = false;
        WriteSettings(json);
    }

    public async Task RecoverLaunchImages()
    {
        if (!File.Exists(LaunchBackup)) return;
        var previous = JsonSerializer.Deserialize<ImageOptions>(File.ReadAllText(LaunchBackup))!;
        using var running = Running();
        if (running is not null)
        {
            var value = await settings.GetSettingsAsync(new Empty(), deadline: Deadline);
            value.DesktopAutoWallpaper = previous.Desktop;
            value.LockScreenAutoWallpaper = previous.Lock;
            await settings.SetSettingsAsync(value, deadline: Deadline);
        }
        else
        {
            var file = Path.Combine(DataDirectory, "Settings.json");
            var json = JsonNode.Parse(File.ReadAllText(file))!;
            json["DesktopAutoWallpaper"] = previous.Desktop;
            json["LockScreenAutoWallpaper"] = previous.Lock;
            WriteSettings(json);
        }
        File.Delete(LaunchBackup);
    }

    public LivelyHost()
    {
        var pipe = new NamedPipeChannel(".", "Grpc_LIVELY:DESKTOPWALLPAPERSYSTEM" + Environment.UserName);
        desktop = new(pipe);
        displays = new(pipe);
        settings = new(pipe);
        commands = new(pipe);
    }

    public static Process? Running() => Process.GetProcessesByName("Lively")
        .FirstOrDefault(p => p.SessionId == Process.GetCurrentProcess().SessionId);

    public static string? FindExecutable()
    {
        using var running = Running();
        if (running is not null) return running.MainModule?.FileName;
        const string uninstall = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall";
        foreach (var hive in new[] { Registry.CurrentUser, Registry.LocalMachine })
        {
            using var root = hive.OpenSubKey(uninstall);
            foreach (var name in root?.GetSubKeyNames() ?? [])
            {
                using var key = root!.OpenSubKey(name);
                if (!(key?.GetValue("DisplayName") as string ?? "").StartsWith("Lively Wallpaper")) continue;
                var location = key?.GetValue("InstallLocation") as string;
                if (location is not null && File.Exists(Path.Combine(location, "Lively.exe")))
                    return Path.Combine(location, "Lively.exe");
            }
        }
        return new[] {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Lively Wallpaper", "Lively.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Lively Wallpaper", "Lively.exe")
        }.FirstOrDefault(File.Exists);
    }

    public static bool PrepareFirstRun(string directory)
    {
        Directory.CreateDirectory(directory);
        var file = Path.Combine(directory, "Settings.json");
        // An existing installation belongs to the user. Never overwrite its setup.
        if (File.Exists(file)) return false;
        using var stream = new FileStream(file, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        JsonSerializer.Serialize(stream, new {
            AppVersion = SupportedVersion, IsFirstRun = false, Startup = false,
            WebBrowser = 1, SysTrayIcon = false, DesktopAutoWallpaper = false, LockScreenAutoWallpaper = false
        });
        stream.Flush(true);
        return true;
    }

    public async Task EnsureRunning()
    {
        try { await StartAndWait(); }
        finally { await RecoverLaunchImages(); }
    }

    async Task StartAndWait()
    {
        using var running = Running();
        List<DesktopItem>? expectedLayout = null;
        if (running is null)
        {
            var executable = FindExecutable() ?? throw new InvalidOperationException(
                "Lively Wallpaper is missing. Run the Windows wallpaper setup.");
            CheckVersion(FileVersionInfo.GetVersionInfo(executable).FileVersion ?? "");
            PrepareFirstRun(DataDirectory);
            PrepareLaunchImages();
            var layoutFile = Path.Combine(DataDirectory, "WallpaperLayout.json");
            if (File.Exists(layoutFile))
            {
                using var layout = JsonDocument.Parse(File.ReadAllText(layoutFile));
                expectedLayout = layout.RootElement.EnumerateArray().Select(item => new DesktopItem(
                    item.GetProperty("LivelyScreen").GetProperty("DeviceId").GetString()!,
                    item.GetProperty("LivelyInfoPath").GetString()!)).ToList();
            }
            using var started = Process.Start(new ProcessStartInfo(executable) {
                UseShellExecute = false, CreateNoWindow = true, WindowStyle = ProcessWindowStyle.Hidden
            }) ?? throw new IOException("Could not start Lively.");
            StartedProcess = new(started.Id, started.StartTime.ToUniversalTime().Ticks);
        }
        var until = DateTime.UtcNow.AddSeconds(45);
        Exception? last = null;
        while (DateTime.UtcNow < until)
        {
            try
            {
                var stats = await desktop.GetCoreStatsAsync(new Empty(), deadline: DateTime.UtcNow.AddSeconds(2));
                CheckVersion(stats.AssemblyVersion);
                if (stats.IsCoreInitialized)
                {
                    // Core readiness precedes restoration of saved wallpapers.
                    // Do not snapshot an empty desktop while they are still loading.
                    var screens = await displays.GetScreensAsync(new Empty(), deadline: Deadline);
                    var connected = screens.Screens_.Select(x => x.DeviceId).ToHashSet();
                    var current = await GetWallpapers();
                    if (expectedLayout is null || expectedLayout.Where(x => connected.Contains(x.Monitor))
                        .All(old => current.Any(x => x.Monitor == old.Monitor && Session.SamePath(x.Path, old.Path))))
                        return;
                }
            }
            catch (RpcException e) { last = e; }
            await Task.Delay(200);
        }
        throw new TimeoutException("Lively did not become ready.", last);
    }

    static void CheckVersion(string version)
    {
        if (version != SupportedVersion)
            throw new NotSupportedException($"Managed wallpaper supports Lively {SupportedVersion}; found {version}. Existing installation was not changed.");
    }

    public async Task<HostOptions> GetOptions()
    {
        var value = await settings.GetSettingsAsync(new Empty(), deadline: Deadline);
        return new(value.WallpaperArrangement, value.WebBrowser, value.DesktopAutoWallpaper, value.LockScreenAutoWallpaper);
    }

    public async Task SetSessionOptions()
    {
        var value = await settings.GetSettingsAsync(new Empty(), deadline: Deadline);
        if (value.WebBrowser == 1 && !value.DesktopAutoWallpaper && !value.LockScreenAutoWallpaper) return;
        value.WebBrowser = 1;
        // Keep the Windows wallpaper/slideshow underneath intact. Lively's
        // screenshot-to-wallpaper features must not replace it during our session.
        value.DesktopAutoWallpaper = false;
        value.LockScreenAutoWallpaper = false;
        await settings.SetSettingsAsync(value, deadline: Deadline);
    }

    public async Task RestoreOptions(HostOptions previous, bool images = true)
    {
        var value = await settings.GetSettingsAsync(new Empty(), deadline: Deadline);
        var before = value.Clone();
        if (value.WebBrowser == 1) value.WebBrowser = previous.Browser;
        if (images && !value.DesktopAutoWallpaper) value.DesktopAutoWallpaper = previous.DesktopImage;
        if (images && !value.LockScreenAutoWallpaper) value.LockScreenAutoWallpaper = previous.LockImage;
        if (!value.Equals(before)) await settings.SetSettingsAsync(value, deadline: Deadline);
    }

    public async Task<List<DesktopItem>> GetWallpapers()
    {
        using var call = desktop.GetWallpapers(new Empty(), deadline: Deadline);
        var result = new List<DesktopItem>();
        while (await call.ResponseStream.MoveNext(CancellationToken.None))
            result.Add(new(call.ResponseStream.Current.Screen.DeviceId, call.ResponseStream.Current.LivelyInfoPath));
        return result;
    }

    public async Task<string> PrimaryMonitor()
    {
        var value = await displays.GetScreensAsync(new Empty(), deadline: Deadline);
        return value.Screens_.Single(x => x.IsPrimary).DeviceId;
    }

    public async Task<string> WriteProject(string url)
    {
        var value = await settings.GetSettingsAsync(new Empty(), deadline: Deadline);
        var directory = Path.Combine(value.WallpaperDir, "wallpapers", "amadeus-managed");
        Directory.CreateDirectory(directory);
        var metadata = new {
            AppVersion = SupportedVersion, Title = "Amadeus", Desc = "Managed by Amadeus", Author = "Amadeus",
            Type = 3, FileName = url, IsAbsolutePath = true, Arguments = "", Contact = "", License = "AGPL-3.0"
        };
        File.WriteAllText(Path.Combine(directory, "LivelyInfo.json"), JsonSerializer.Serialize(metadata));
        return directory;
    }

    public async Task SetWallpaper(DesktopItem wallpaper)
    {
        await desktop.SetWallpaperAsync(new SetWallpaperRequest {
            LivelyInfoPath = wallpaper.Path, MonitorId = wallpaper.Monitor
        }, deadline: Deadline);
        var until = DateTime.UtcNow.AddSeconds(30);
        while (DateTime.UtcNow < until)
        {
            if ((await GetWallpapers()).Any(x => Session.SamePath(x.Path, wallpaper.Path) && x.Monitor == wallpaper.Monitor)) return;
            await Task.Delay(200);
        }
        throw new TimeoutException("Lively did not confirm the wallpaper mount.");
    }

    public async Task CloseProject(string project) =>
        await desktop.CloseWallpaperLibraryAsync(new CloseRequest { LivelyInfoPath = project }, deadline: Deadline);

    public async Task Screenshot(string file)
    {
        await desktop.TakeScreenshotAsync(new ScreenshotRequest {
            MonitorId = await PrimaryMonitor(), SavePath = Path.GetFullPath(file)
        }, deadline: Deadline);
        if (!File.Exists(file)) throw new IOException("The Lively player did not produce a screenshot.");
    }

    public async Task StopOwnedProcess(ProcessIdentity identity)
    {
        Process process;
        try { process = Process.GetProcessById(identity.Id); }
        catch (ArgumentException) { return; }
        using (process)
        {
            if (process.StartTime.ToUniversalTime().Ticks != identity.Started) return;
            var request = new AutomationCommandRequest();
            request.Args.AddRange(new[] { "app", "--shutdown", "true" });
            try { await commands.AutomationCommandAsync(request, deadline: Deadline); }
            catch (RpcException) when (process.HasExited) { return; }
            await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(15));
        }
    }
}
