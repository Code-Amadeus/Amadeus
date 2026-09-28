using System.Text.Json;
using Google.Protobuf;
using Amadeus.Wallpaper;
using Amadeus.Wallpaper.Protocol.Settings;

if (args.Length == 2 && args[0] == "--cold-start-probe")
{
    await ColdStartProbe.Run(args[1]);
    return;
}

static void Check(bool condition, string message)
{
    if (!condition) throw new Exception(message);
}
var passed = 0;
async Task Test(string name, Func<FakeHost, Session, string, Task> test)
{
    var directory = Path.Combine(Path.GetTempPath(), "amadeus-wallpaper-tests", Guid.NewGuid().ToString("N"));
    Directory.CreateDirectory(directory);
    try
    {
        var host = new FakeHost();
        var journal = Path.Combine(directory, "session.json");
        await test(host, new Session(host, journal), journal);
        Console.WriteLine("PASS " + name);
        passed++;
    }
    finally { Directory.Delete(directory, true); }
}
Snapshot Before(FakeHost host, ProcessIdentity? owned = null) =>
    new("amadeus", "primary", host.Options, host.Items.ToList(), owned);

await Test("restores the existing wallpaper without closing a shared Lively", async (host, session, journal) => {
    host.Items.Add(new("primary", "old"));
    await session.Mount(Before(host));
    await session.Restore();
    Check(host.Items.SequenceEqual(new[] { new DesktopItem("primary", "old") }), "original wallpaper missing");
    Check(host.Stopped is null && !File.Exists(journal), "shared host stopped or journal left behind");
});
await Test("static wallpaper is revealed by removing only our layer", async (host, session, journal) => {
    var process = new ProcessIdentity(10, 20);
    await session.Mount(Before(host, process));
    await session.Restore();
    Check(host.Items.Count == 0 && host.Stopped == process, "owned host not released");
    Check(!File.Exists(journal), "journal retained after successful restore");
});
await Test("secondary monitors keep their original wallpaper", async (host, session, _) => {
    host.Items.AddRange([new("primary", "old"), new("secondary", "second")]);
    await session.Mount(Before(host));
    Check(host.Items.Contains(new("secondary", "second")), "mount changed secondary monitor");
    await session.Restore();
    Check(host.Items.Contains(new("secondary", "second")), "restore changed secondary monitor");
});
await Test("user wallpaper changes win over our original snapshot", async (host, session, _) => {
    host.Items.Add(new("primary", "old"));
    await session.Mount(Before(host, new(10, 20)));
    await host.SetWallpaper(new("primary", "user-new"));
    await session.Restore();
    Check(host.Items.Single().Path == "user-new" && host.Stopped is null, "user's choice overwritten");
});
await Test("user clearing the wallpaper is not undone", async (host, session, _) => {
    host.Items.Add(new("primary", "old"));
    await session.Mount(Before(host));
    host.Items.Clear();
    await session.Restore();
    Check(host.Items.Count == 0, "user's clear was undone");
});
await Test("failed mount rolls back after Lively has closed the old wallpaper", async (host, session, journal) => {
    host.Items.Add(new("primary", "old"));
    host.FailNextMount = true;
    try { await session.Mount(Before(host)); throw new Exception("expected failure"); }
    catch (IOException) { }
    Check(host.Items.Single().Path == "old" && !File.Exists(journal), "rollback failed");
});
await Test("crash recovery uses the durable journal in a new session", async (host, session, journal) => {
    host.Items.Add(new("primary", "old"));
    await session.Mount(Before(host));
    await new Session(host, journal).Restore();
    Check(host.Items.Single().Path == "old", "recovery lost original");
});
await Test("restoration failure retains enough state to retry after our layer closes", async (host, session, journal) => {
    host.Items.Add(new("primary", "old"));
    await session.Mount(Before(host));
    host.FailNextMount = true;
    try { await session.Restore(); throw new Exception("expected failure"); }
    catch (IOException) { }
    Check(File.Exists(journal), "failed recovery discarded journal");
    await new Session(host, journal).Restore();
    Check(host.Items.Single().Path == "old" && !File.Exists(journal), "retry failed");
});
await Test("a changed layout is preserved", async (host, session, _) => {
    host.Items.Add(new("primary", "old"));
    await session.Mount(Before(host, new(10, 20)));
    host.Options = host.Options with { Arrangement = 1 };
    await session.Restore();
    Check(host.Options.Arrangement == 1 && host.Items.Count == 0 && host.Stopped is null, "new layout overwritten");
});
await Test("span layout restores its original project", async (host, session, _) => {
    host.Options = host.Options with { Arrangement = 1 };
    host.Items.Add(new("primary", "span-old"));
    await session.Mount(Before(host));
    await session.Restore();
    Check(host.Items.Single().Path == "span-old", "span restore failed");
});
await Test("duplicate layout restores all original displays", async (host, session, _) => {
    host.Options = host.Options with { Arrangement = 2 };
    host.Items.AddRange([new("primary", "old"), new("secondary", "old")]);
    await session.Mount(Before(host));
    await session.Restore();
    Check(host.Items.Count == 2 && host.Items.All(x => x.Path == "old"), "duplicate restore failed");
});
await Test("first-run preparation is idempotent and preserves existing settings", async (_, _, journal) => {
    var directory = Path.GetDirectoryName(journal)!;
    Check(LivelyHost.PrepareFirstRun(directory), "did not prepare fresh settings");
    var file = Path.Combine(directory, "Settings.json");
    using var settings = JsonDocument.Parse(File.ReadAllText(file));
    Check(!settings.RootElement.GetProperty("IsFirstRun").GetBoolean(), "setup wizard still enabled");
    Check(!settings.RootElement.GetProperty("Startup").GetBoolean(), "second autostart owner created");
    File.WriteAllText(file, "user-owned-settings");
    Check(!LivelyHost.PrepareFirstRun(directory) && File.ReadAllText(file) == "user-owned-settings", "existing settings replaced");
    await Task.CompletedTask;
});
await Test("settings IPC preserves fields outside our wire subset", async (_, _, _) => {
    // field 3 (Startup=true) is intentionally unknown to our client schema.
    var input = new byte[] { 24, 1, 224, 1, 0 };
    var message = Settings.Parser.ParseFrom(input);
    message.WebBrowser = 1;
    var roundTrip = message.ToByteArray();
    var reader = new CodedInputStream(roundTrip);
    var startup = false;
    uint tag;
    while ((tag = reader.ReadTag()) != 0) {
        if (tag == 24) startup = reader.ReadBool(); else reader.SkipLastField();
    }
    Check(startup, "unknown setting was discarded");
    await Task.CompletedTask;
});
Console.WriteLine($"{passed} contracts passed.");

sealed class FakeHost : IWallpaperHost
{
    public List<DesktopItem> Items = [];
    public HostOptions Options = new(0, 1, false, false);
    public ProcessIdentity? Stopped;
    public bool FailNextMount;
    public Task<HostOptions> GetOptions() => Task.FromResult(Options);
    public Task<List<DesktopItem>> GetWallpapers() => Task.FromResult(Items.ToList());
    public Task SetWallpaper(DesktopItem wallpaper) {
        Items.RemoveAll(x => x.Monitor == wallpaper.Monitor);
        if (FailNextMount) { FailNextMount = false; throw new IOException("player failed"); }
        Items.Add(wallpaper);
        return Task.CompletedTask;
    }
    public Task CloseProject(string project) { Items.RemoveAll(x => Session.SamePath(x.Path, project)); return Task.CompletedTask; }
    public Task SetSessionOptions() => Task.CompletedTask;
    public Task RestoreOptions(HostOptions previous, bool images = true) => Task.CompletedTask;
    public Task StopOwnedProcess(ProcessIdentity process) { Stopped = process; return Task.CompletedTask; }
}
