using System.Text.Json;

namespace Amadeus.Wallpaper;

public record DesktopItem(string Monitor, string Path);
public record HostOptions(int Arrangement, int Browser, bool DesktopImage, bool LockImage);
public record ProcessIdentity(int Id, long Started);
public record Snapshot(string Project, string Monitor, HostOptions Options,
    List<DesktopItem> Wallpapers, ProcessIdentity? OwnedProcess, bool Mounted = false);

public interface IWallpaperHost
{
    Task<HostOptions> GetOptions();
    Task<List<DesktopItem>> GetWallpapers();
    Task SetWallpaper(DesktopItem wallpaper);
    Task CloseProject(string project);
    Task SetSessionOptions();
    Task RestoreOptions(HostOptions previous, bool images = true);
    Task StopOwnedProcess(ProcessIdentity process);
}

// One session owns one durable before-image. The journal is written before the
// first desktop mutation and removed only after cleanup succeeds.
public sealed class Session(IWallpaperHost host, string journal)
{
    static readonly JsonSerializerOptions Json = new() { WriteIndented = true };
    public static bool SamePath(string left, string right) =>
        string.Equals(left, right, StringComparison.OrdinalIgnoreCase);

    public async Task Mount(Snapshot snapshot)
    {
        if (File.Exists(journal)) throw new InvalidOperationException("Recover the previous wallpaper session first.");
        Save(snapshot);
        try
        {
            await host.SetSessionOptions();
            await host.SetWallpaper(new(snapshot.Monitor, snapshot.Project));
            Save(snapshot with { Mounted = true });
        }
        catch
        {
            await Restore();
            throw;
        }
    }

    public async Task Restore(ProcessIdentity? restartedProcess = null)
    {
        if (!File.Exists(journal)) return;
        var snapshot = JsonSerializer.Deserialize<Snapshot>(File.ReadAllText(journal))
            ?? throw new InvalidDataException("Invalid wallpaper recovery journal.");
        var current = await host.GetWallpapers();
        var options = await host.GetOptions();
        var ours = current.Where(x => SamePath(x.Path, snapshot.Project)).ToList();
        // A user's new layout/selection takes precedence over the old snapshot.
        var layoutUnchanged = options.Arrangement == snapshot.Options.Arrangement;
        var affected = snapshot.Options.Arrangement == 0
            ? new HashSet<string>([snapshot.Monitor])
            : snapshot.Wallpapers.Select(x => x.Monitor).Append(snapshot.Monitor).ToHashSet();
        var restorable = affected.Where(monitor =>
            ours.Any(x => x.Monitor == monitor) ||
            ((!snapshot.Mounted || restartedProcess is not null) && current.All(x => x.Monitor != monitor)))
            .ToHashSet();
        // Span/duplicate is one logical wallpaper, even if RPC reports only one screen.
        if (snapshot.Options.Arrangement != 0 && ours.Count > 0) restorable = affected;
        if (ours.Count > 0)
        {
            // A failed restore can be resumed after our layer has been removed.
            Save(snapshot with { Mounted = false });
            await host.CloseProject(snapshot.Project);
        }
        if (layoutUnchanged)
        {
            // Recreate the user's scene with its original browser, while keeping
            // screenshot-to-system-wallpaper disabled until it has loaded.
            await host.RestoreOptions(snapshot.Options, images: false);
            foreach (var wallpaper in snapshot.Wallpapers.Where(x => restorable.Contains(x.Monitor)))
                await host.SetWallpaper(wallpaper);
        }
        await host.RestoreOptions(snapshot.Options);
        var after = await host.GetWallpapers();
        var userChanged = !layoutUnchanged || after.Any(x =>
            !snapshot.Wallpapers.Any(old => old.Monitor == x.Monitor && SamePath(old.Path, x.Path)));
        // Never stop a host that the user had running, or one now showing their new choice.
        if (snapshot.OwnedProcess is not null && !userChanged)
            await host.StopOwnedProcess(restartedProcess ?? snapshot.OwnedProcess);
        File.Delete(journal);
    }

    void Save(Snapshot snapshot)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(journal)!);
        var temporary = journal + ".tmp";
        using (var stream = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None))
        {
            JsonSerializer.Serialize(stream, snapshot, Json);
            stream.Flush(true);
        }
        File.Move(temporary, journal, true);
    }
}
