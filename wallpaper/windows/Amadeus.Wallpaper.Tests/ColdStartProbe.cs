using System.Diagnostics;
using System.Text.Json;
using Amadeus.Wallpaper;

// Opt-in integration experiment. Original settings/layout are backed up before
// touching them and restored in finally. It never uninstalls the user's host.
static class ColdStartProbe
{
    public static async Task Run(string output)
    {
        var host = new LivelyHost();
        var executable = LivelyHost.FindExecutable() ?? throw new Exception("Lively must already be installed for this probe.");
        var recovery = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Amadeus", "wallpaper", "session.json");
        if (File.Exists(recovery)) throw new Exception("Resolve the active wallpaper session before the cold-start probe.");
        using var original = LivelyHost.Running();
        var baseline = original is null ? [] : await host.GetWallpapers();
        var backup = Path.Combine(Path.GetFullPath(output), "first-run-backup-" + DateTime.UtcNow.ToString("yyyyMMddHHmmss"));
        Directory.CreateDirectory(backup);
        var files = new[] { "Settings.json", "WallpaperLayout.json" };
        var saved = files.ToDictionary(name => name, name => {
            var file = Path.Combine(LivelyHost.DataDirectory, name);
            var bytes = File.Exists(file) ? File.ReadAllBytes(file) : null;
            if (bytes is not null) File.WriteAllBytes(Path.Combine(backup, name), bytes);
            return bytes;
        });
        try
        {
            if (original is not null)
                await host.StopOwnedProcess(new(original.Id, original.StartTime.ToUniversalTime().Ticks));
            // Exact fresh-profile input, with a separate library for the experiment.
            File.Delete(Path.Combine(LivelyHost.DataDirectory, "Settings.json"));
            File.WriteAllText(Path.Combine(LivelyHost.DataDirectory, "WallpaperLayout.json"), "[]");
            LivelyHost.PrepareFirstRun(LivelyHost.DataDirectory);
            var fresh = Path.Combine(LivelyHost.DataDirectory, "Settings.json");
            var configuration = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(File.ReadAllText(fresh))!;
            configuration["WallpaperDir"] = JsonSerializer.SerializeToElement(Path.Combine(backup, "test-library"));
            File.WriteAllText(fresh, JsonSerializer.Serialize(configuration));
            var prepared = new LivelyHost();
            await prepared.EnsureRunning();
            await Task.Delay(3000);
            var ui = Process.GetProcessesByName("Lively.UI.WinUI");
            if (ui.Any(p => p.SessionId == Process.GetCurrentProcess().SessionId))
                throw new Exception("First-run preparation still opened Lively UI.");
            if ((await prepared.GetOptions()).Browser != 1)
                throw new Exception("Fresh settings did not select WebView2.");
            using var settings = JsonDocument.Parse(File.ReadAllText(fresh));
            if (settings.RootElement.GetProperty("IsFirstRun").GetBoolean()
                || settings.RootElement.GetProperty("Startup").GetBoolean())
                throw new Exception("Fresh launch re-enabled the wizard or independent autostart.");
            var project = Path.Combine(backup, "test-library", "wallpapers", "amadeus-cold-start-test");
            Directory.CreateDirectory(project);
            var page = Path.Combine(project, "index.html");
            File.WriteAllText(page, "<!doctype html><body style='background:#102436;color:white;font:32px sans-serif'>Amadeus startup experiment</body>");
            File.WriteAllText(Path.Combine(project, "LivelyInfo.json"), JsonSerializer.Serialize(new {
                Title = "Amadeus startup experiment", Type = 1, FileName = page, IsAbsolutePath = true
            }));
            var session = new Session(prepared, Path.Combine(backup, "test-session.json"));
            await session.Mount(new Snapshot(project, await prepared.PrimaryMonitor(), await prepared.GetOptions(),
                await prepared.GetWallpapers(), prepared.StartedProcess));
            await session.Restore();
            using var leftover = LivelyHost.Running();
            if (leftover is not null) throw new Exception("The newly owned Lively process was not released after restore.");
            File.WriteAllText(Path.Combine(output, "first-run-experiment.json"), JsonSerializer.Serialize(new {
                passed = true, wizardVisible = false, webView2 = true, independentAutostart = false,
                ownedHostReleased = true,
                kind = "fresh-settings-on-installed-Lively", backup
            }, new JsonSerializerOptions { WriteIndented = true }));
            Console.WriteLine("PASS real Lively cold start with fresh settings: no UI, WebView2, no independent autostart");
        }
        finally
        {
            using var running = LivelyHost.Running();
            if (running is not null)
                await host.StopOwnedProcess(new(running.Id, running.StartTime.ToUniversalTime().Ticks));
            foreach (var (name, bytes) in saved)
            {
                var file = Path.Combine(LivelyHost.DataDirectory, name);
                if (bytes is null) File.Delete(file); else File.WriteAllBytes(file, bytes);
            }
            if (original is not null)
            {
                await host.EnsureRunning();
                var deadline = DateTime.UtcNow.AddSeconds(30);
                while (DateTime.UtcNow < deadline && !(await host.GetWallpapers()).SequenceEqual(baseline))
                    await Task.Delay(200);
                if (!(await host.GetWallpapers()).SequenceEqual(baseline))
                    throw new Exception("Original Lively layout did not restore. Backup: " + backup);
            }
            Console.WriteLine("Original settings, layout and running state restored. Backup: " + backup);
        }
    }
}
