using System.Text.Json;
using System.Text.Json.Serialization;

namespace Amadeus.Wallpaper;

public record DesktopItem(string Monitor, string Path);
public record HostOptions(int Arrangement, int Browser, bool DesktopImage, bool LockImage);
public record ProcessIdentity(int Id, long Started);
public enum MountPhase { Preparing, Pending, Settled }
public record Snapshot(string Project, string Monitor, HostOptions Options,
    List<DesktopItem> Wallpapers, ProcessIdentity? OwnedProcess, bool Mounted = false,
    bool MountUnconfirmed = false, MountPhase? Phase = null);

public interface IWallpaperHost
{
    Task<HostOptions> GetOptions();
    Task<List<DesktopItem>> GetWallpapers();
    // 原生请求正常返回后、场景确认前回调；请求异常结束不得回调。
    Task SetWallpaper(DesktopItem wallpaper, Action? requestSettled = null);
    Task CloseProject(string project);
    Task SetSessionOptions();
    Task RestoreOptions(HostOptions previous, bool images = true);
    Task StopOwnedProcess(ProcessIdentity process);
}

// 单个会话持有持久化的原状态；任何桌面变更之前先保存日志，恢复核验通过后才删除。
public sealed class Session(IWallpaperHost host, string journal)
{
    static readonly JsonSerializerOptions Json = new() {
        WriteIndented = true, Converters = { new JsonStringEnumConverter<MountPhase>(allowIntegerValues: false) }
    };
    ProcessIdentity? managedCleanupProcess;
    public static bool SamePath(string left, string right) =>
        string.Equals(left, right, StringComparison.OrdinalIgnoreCase);

    public Task ReleaseUnstartedHost(ProcessIdentity? process) =>
        process is not null && process != managedCleanupProcess && !File.Exists(journal)
            ? host.StopOwnedProcess(process) : Task.CompletedTask;

    public async Task Mount(Snapshot snapshot)
    {
        if (File.Exists(journal)) throw new InvalidOperationException("Recover the previous wallpaper session first.");
        snapshot = snapshot with { Mounted = false, MountUnconfirmed = false, Phase = MountPhase.Preparing };
        Save(snapshot);
        managedCleanupProcess = snapshot.OwnedProcess;
        try
        {
            await host.SetSessionOptions();
            var pending = snapshot with { Phase = MountPhase.Pending };
            Save(pending);
            snapshot = pending;
            await host.SetWallpaper(new(snapshot.Monitor, snapshot.Project), () => {
                // 正常 RPC 返回只排除本请求仍在加载，不代表挂载成功。
                var settled = snapshot with { Phase = MountPhase.Settled };
                Save(settled);
                snapshot = settled;
            });
            Save(snapshot with { Mounted = true, Phase = MountPhase.Settled });
        }
        catch (Exception primary)
        {
            // 未结算的请求可能迟到；关闭当前项目不能取消它，保留日志和原错。
            if (snapshot.Phase == MountPhase.Pending) throw;
            try { await Restore(); }
            catch (Exception cleanup) { throw new AggregateException("Wallpaper mount and rollback failed.", primary, cleanup); }
            throw;
        }
    }

    public async Task RunMounted(Snapshot snapshot, Func<Task> body)
    {
        // Mount 负责自己的失败回滚；只有挂载成功后才进入本轮 finally。
        await Mount(snapshot);
        Exception? primary = null;
        try { await body(); }
        catch (Exception error) { primary = error; throw; }
        finally
        {
            try { await Restore(); }
            catch (Exception cleanup) when (primary is not null)
            {
                throw new AggregateException("Wallpaper body and cleanup failed.", primary, cleanup);
            }
        }
    }

    public async Task Restore(ProcessIdentity? restartedProcess = null)
    {
        if (!File.Exists(journal)) return;
        var snapshot = JsonSerializer.Deserialize<Snapshot>(File.ReadAllText(journal), Json)
            ?? throw new InvalidDataException("Invalid wallpaper recovery journal.");
        managedCleanupProcess = restartedProcess ?? snapshot.OwnedProcess;
        var pending = snapshot.Phase == MountPhase.Pending || snapshot.MountUnconfirmed;
        if (pending)
        {
            if (restartedProcess is null || restartedProcess == snapshot.OwnedProcess)
                throw new InvalidOperationException("Wallpaper mount request is still pending; fully exit Lively, then run recover so the helper can restart it.");
            snapshot = snapshot with { Mounted = false, MountUnconfirmed = false, Phase = MountPhase.Settled };
            Save(snapshot);
        }
        if (snapshot.Phase == MountPhase.Preparing)
        {
            // 只可能部分写入设置，尚未发送挂载请求；不重设或关闭任何壁纸。
            await host.RestoreOptions(snapshot.Options);
            var preparedOptions = await host.GetOptions();
            var preparedWallpapers = await host.GetWallpapers();
            var changed = preparedOptions.Arrangement != snapshot.Options.Arrangement || preparedWallpapers.Any(x =>
                !snapshot.Wallpapers.Any(old => old.Monitor == x.Monitor && SamePath(old.Path, x.Path)));
            if (snapshot.OwnedProcess is not null && !changed)
                await host.StopOwnedProcess(restartedProcess ?? snapshot.OwnedProcess);
            File.Delete(journal);
            return;
        }
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
        if (ours.Count > 0 || (snapshot.Phase == MountPhase.Settled && !snapshot.Mounted))
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
        if (after.Any(x => SamePath(x.Path, snapshot.Project) && !snapshot.Wallpapers.Any(old =>
                old.Monitor == x.Monitor && SamePath(old.Path, x.Path))) || (layoutUnchanged &&
            snapshot.Wallpapers.Where(x => restorable.Contains(x.Monitor)).Any(old =>
                !after.Any(x => x.Monitor == old.Monitor && SamePath(x.Path, old.Path)))))
            throw new IOException("Wallpaper restoration was not confirmed; recovery journal retained.");
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
