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
    new("amadeus", "primary", host.Options, host.ActualItems.ToList(), owned);

await Test("restores the existing wallpaper without closing a shared Lively", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    await session.Mount(Before(host));
    await session.Restore();
    Check(host.ActualItems.SequenceEqual(new[] { new DesktopItem("primary", "old") }), "original wallpaper missing");
    Check(host.Stopped is null && !File.Exists(journal), "shared host stopped or journal left behind");
});
await Test("static wallpaper is revealed by removing only our layer", async (host, session, journal) => {
    var process = new ProcessIdentity(10, 20);
    await session.Mount(Before(host, process));
    await session.Restore();
    Check(host.ActualItems.Count == 0 && host.Stopped == process, "owned host not released");
    Check(!File.Exists(journal), "journal retained after successful restore");
});
await Test("secondary monitors keep their original wallpaper", async (host, session, _) => {
    host.ActualItems.AddRange([new("primary", "old"), new("secondary", "second")]);
    await session.Mount(Before(host));
    Check(host.ActualItems.Contains(new("secondary", "second")), "mount changed secondary monitor");
    await session.Restore();
    Check(host.ActualItems.Contains(new("secondary", "second")), "restore changed secondary monitor");
});
await Test("user wallpaper changes win over our original snapshot", async (host, session, _) => {
    host.ActualItems.Add(new("primary", "old"));
    await session.Mount(Before(host, new(10, 20)));
    await host.SetWallpaper(new("primary", "user-new"));
    await session.Restore();
    Check(host.ActualItems.Single().Path == "user-new" && host.Stopped is null, "user's choice overwritten");
});
await Test("user clearing the wallpaper is not undone", async (host, session, _) => {
    host.ActualItems.Add(new("primary", "old"));
    await session.Mount(Before(host));
    host.ActualItems.Clear();
    await session.Restore();
    Check(host.ActualItems.Count == 0, "user's clear was undone");
});
await Test("failed mount retains recovery until the old wallpaper is observable", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    host.FailNextMount = true;
    try { await session.Mount(Before(host)); throw new Exception("expected failure"); }
    catch (IOException) { }
    Check(host.ActualItems.Count == 0 && File.Exists(journal), "failed mount discarded recovery state");
    host.ActualItems.Add(new("primary", "old"));
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "observable original scene could not settle recovery");
});
await Test("crash recovery uses the durable journal in a new session", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    await session.Mount(Before(host));
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "old", "recovery lost original");
});
await Test("restoration failure retains enough state to retry after our layer closes", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    await session.Mount(Before(host));
    host.FailNextMount = true;
    try { await session.Restore(); throw new Exception("expected failure"); }
    catch (IOException) { }
    Check(File.Exists(journal), "failed recovery discarded journal");
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "retry failed");
});
await Test("a changed layout is preserved", async (host, session, _) => {
    host.ActualItems.Add(new("primary", "old"));
    await session.Mount(Before(host, new(10, 20)));
    host.Options = host.Options with { Arrangement = 1 };
    await session.Restore();
    Check(host.Options.Arrangement == 1 && host.ActualItems.Count == 0 && host.Stopped is null, "new layout overwritten");
});
await Test("span layout restores its original project", async (host, session, _) => {
    host.Options = host.Options with { Arrangement = 1 };
    host.ActualItems.Add(new("primary", "span-old"));
    await session.Mount(Before(host));
    await session.Restore();
    Check(host.ActualItems.Single().Path == "span-old", "span restore failed");
});
await Test("duplicate layout restores all original displays", async (host, session, _) => {
    host.Options = host.Options with { Arrangement = 2 };
    host.ActualItems.AddRange([new("primary", "old"), new("secondary", "old")]);
    await session.Mount(Before(host));
    await session.Restore();
    Check(host.ActualItems.Count == 2 && host.ActualItems.All(x => x.Path == "old"), "duplicate restore failed");
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
await Test("an acted but unconfirmed mount retains recovery through the caller finally", async (host, session, journal) => {
    var process = new ProcessIdentity(10, 20);
    host.ReportedItems = [];
    host.FailAfterMount = true;
    Exception? original = null;
    try {
        try { await session.Mount(Before(host, process)); }
        finally { await session.Restore(); }
    }
    catch (Exception error) { original = error; }
    Check(ReferenceEquals(original, host.MountFailure), "original mount error was lost");
    Check(File.Exists(journal), "unconfirmed mount discarded the only recovery journal");
    using (var saved = JsonDocument.Parse(File.ReadAllText(journal)))
        Check(saved.RootElement.GetProperty("MountUnconfirmed").GetBoolean(), "uncertainty was not durable");
    Check(host.ActualItems.Single().Path == "amadeus" && host.Stopped is null, "unconfirmed mount stopped the owned host");
    var emptyRejected = false;
    try { await new Session(host, journal).Restore(); }
    catch (InvalidOperationException) { emptyRejected = true; }
    Check(emptyRejected && File.Exists(journal) && host.Stopped is null, "empty report was accepted as recovery confirmation");
    host.Options = host.Options with { Arrangement = 1 };
    emptyRejected = false;
    try { await new Session(host, journal).Restore(); }
    catch (InvalidOperationException) { emptyRejected = true; }
    Check(emptyRejected && File.Exists(journal) && host.Options.Arrangement == 1 && host.Stopped is null,
        "layout was treated as mount confirmation or the user's layout was changed");
    host.Options = host.Options with { Arrangement = 0 };
    host.ReportedItems = null;
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Count == 0 && host.Stopped == process && !File.Exists(journal), "reliable explicit recovery could not retry");
});
await Test("failed uncertain recovery preserves the mount error and can retry", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    host.ReportedItems = [];
    host.FailAfterMount = true;
    host.RestoreFailure = new IOException("restoration unavailable");
    Exception? original = null;
    try {
        try { await session.Mount(Before(host, new(10, 20))); }
        finally { await session.Restore(); }
    }
    catch (Exception error) { original = error; }
    Check(ReferenceEquals(original, host.MountFailure), "cleanup replaced the original mount failure");
    host.ReportedItems = null;
    Exception? recovery = null;
    try { await new Session(host, journal).Restore(); }
    catch (Exception error) { recovery = error; }
    Check(ReferenceEquals(recovery, host.RestoreFailure) && File.Exists(journal) && host.Stopped is null, "failed recovery lost retry state");
    host.RestoreFailure = null;
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "recovery retry did not restore the original scene");
    Check(ReferenceEquals(original, host.MountFailure), "later recovery changed the recorded mount error");
});
await Test("a user selection wins after an unconfirmed mount", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    host.ReportedItems = [];
    host.FailAfterMount = true;
    try { await session.Mount(Before(host, new(10, 20))); }
    catch (TimeoutException) { }
    host.ReportedItems = null;
    await host.SetWallpaper(new("primary", "user-new"));
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "user-new" && host.Stopped is null && !File.Exists(journal), "uncertain recovery overwrote the user's new scene");
});
Console.WriteLine($"{passed} contracts passed.");

sealed class FakeHost : IWallpaperHost
{
    public List<DesktopItem> ActualItems = [];
    public List<DesktopItem>? ReportedItems;
    public HostOptions Options = new(0, 1, false, false);
    public ProcessIdentity? Stopped;
    public bool FailNextMount;
    public bool FailAfterMount;
    public readonly TimeoutException MountFailure = new("wallpaper mount confirmation timed out");
    public Exception? RestoreFailure;
    public Task<HostOptions> GetOptions() => Task.FromResult(Options);
    public Task<List<DesktopItem>> GetWallpapers() => Task.FromResult((ReportedItems ?? ActualItems).ToList());
    public Task SetWallpaper(DesktopItem wallpaper) {
        ActualItems.RemoveAll(x => x.Monitor == wallpaper.Monitor);
        if (FailNextMount) { FailNextMount = false; throw new IOException("player failed"); }
        ActualItems.Add(wallpaper);
        if (FailAfterMount) { FailAfterMount = false; throw MountFailure; }
        return Task.CompletedTask;
    }
    public Task CloseProject(string project) { ActualItems.RemoveAll(x => Session.SamePath(x.Path, project)); return Task.CompletedTask; }
    public Task SetSessionOptions() => Task.CompletedTask;
    public Task RestoreOptions(HostOptions previous, bool images = true) {
        if (RestoreFailure is not null) throw RestoreFailure;
        return Task.CompletedTask;
    }
    public Task StopOwnedProcess(ProcessIdentity process) { Stopped = process; return Task.CompletedTask; }
}
