using System.Text.Json;
using System.IO.Pipes;
using Google.Protobuf;
using Google.Protobuf.WellKnownTypes;
using Grpc.Core;
using GrpcDotNetNamedPipes;
using Amadeus.Wallpaper;
using Amadeus.Wallpaper.Protocol.Desktop;
using Amadeus.Wallpaper.Protocol.Settings;

if (args.Length == 2 && args[0] == "--cold-start-probe")
{
    await ColdStartProbe.Run(args[1]);
    return;
}
if (args.Length == 1 && args[0] == "--false-empty-counterexample")
{
    var directory = Path.Combine(Path.GetTempPath(), "amadeus-wallpaper-counterexample", Guid.NewGuid().ToString("N"));
    Directory.CreateDirectory(directory);
    try
    {
        var journal = Path.Combine(directory, "session.json");
        var host = new FakeHost { ReportedItems = [], CloseNoOp = true, FailAfterMount = true };
        var session = new Session(host, journal);
        var error = await Failure(() => session.Mount(new("amadeus", "primary", host.Options, [], new(10, 20))));
        // 仅记录尚未修复的协议反例，不能计入恢复安全契约的通过数。
        Console.WriteLine(JsonSerializer.Serialize(new {
            diagnostic = "known false-empty protocol counterexample; not a recovery acceptance pass",
            error = error.Message, actual = host.ActualItems, reported = await host.GetWallpapers(),
            closeReturnedWithoutRemoval = host.ClosedProjects.Count > 0 && host.ActualItems.Count > 0,
            journalRetained = File.Exists(journal), stopRequested = host.Stopped is not null,
            strongRecoveryProven = false
        }));
    }
    finally { Directory.Delete(directory, true); }
    return;
}

static void Check(bool condition, string message)
{
    if (!condition) throw new Exception(message);
}
var passed = 0;
var failures = new List<Exception>();
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
    catch (Exception error)
    {
        failures.Add(new Exception(name, error));
        Console.WriteLine("FAIL " + name + ": " + error.Message);
    }
    finally { Directory.Delete(directory, true); }
}
Snapshot Before(FakeHost host, ProcessIdentity? owned = null) =>
    new("amadeus", "primary", host.Options, host.ActualItems.ToList(), owned);
async Task<Exception> Failure(Func<Task> operation)
{
    try { await operation(); }
    catch (Exception error) { return error; }
    throw new Exception("expected failure");
}
void Phase(string journal, string expected)
{
    using var saved = JsonDocument.Parse(File.ReadAllText(journal));
    Check(saved.RootElement.GetProperty("Phase").GetString() == expected, "wrong durable operation phase");
}
void WriteLegacy(string journal, Snapshot snapshot)
{
    File.WriteAllText(journal, JsonSerializer.Serialize(new {
        snapshot.Project, snapshot.Monitor, snapshot.Options, snapshot.Wallpapers,
        snapshot.OwnedProcess, snapshot.Mounted
    }));
}

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
await Test("failed mount rolls back after Lively has closed the old wallpaper", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    host.FailNextMount = true;
    try { await session.Mount(Before(host)); throw new Exception("expected failure"); }
    catch (IOException) { }
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "rollback failed");
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
await Test("a settled failed mount restores an originally empty desktop", async (host, session, journal) => {
    var process = new ProcessIdentity(10, 20);
    host.FailNextMount = true;
    var original = await Failure(() => session.Mount(Before(host, process)));
    Check(original is IOException && host.ActualItems.Count == 0, "empty desktop rollback failed");
    Check(host.Stopped == process && !File.Exists(journal), "empty rollback did not release ownership");
    await new Session(host, journal).Restore();
    await session.Mount(Before(host));
    await session.Restore();
    Check(!File.Exists(journal), "failed mount permanently blocked the next session");
});
await Test("settled but unconfirmed mounting still automatically rolls back", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    host.FailAfterMount = true;
    var original = await Failure(() => session.Mount(Before(host)));
    Check(ReferenceEquals(original, host.MountFailure), "original mount error was lost");
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "settled rollback lost the old scene");
    Check(host.ClosedProjects.SequenceEqual(new[] { "amadeus" }), "rollback did not scope close to the submitted project");
});
await Test("partially applied session settings roll back without wallpaper mutations", async (host, session, journal) => {
    host.Options = new(0, 0, true, true);
    host.ActualItems.Add(new("primary", "old"));
    var before = Before(host, new(10, 20));
    host.OptionsFailure = new IOException("settings write failed after applying");
    var original = await Failure(() => session.Mount(before));
    Check(ReferenceEquals(original, host.OptionsFailure), "settings error was replaced");
    Check(host.Options == before.Options && host.ActualItems.SequenceEqual(before.Wallpapers), "partial settings not restored");
    Check(host.SetCalls == 0 && host.ClosedProjects.Count == 0, "settings rollback dispatched wallpaper mutations");
    Check(host.RestoreCalls == 1 && !File.Exists(journal), "settings rollback was skipped or repeated");
});
await Test("preparation cleanup failure retains both errors and retries settings only", async (host, session, journal) => {
    host.Options = new(0, 0, true, true);
    var before = Before(host);
    host.OptionsFailure = new IOException("partial settings failure");
    host.RestoreFailure = new IOException("settings rollback failed");
    var error = await Failure(() => session.Mount(before));
    Check(error is AggregateException both && both.InnerExceptions.SequenceEqual(new[] { host.OptionsFailure, host.RestoreFailure }), "settings primary and cleanup errors were not retained");
    Phase(journal, "Preparing");
    Check(host.SetCalls == 0 && host.ClosedProjects.Count == 0, "preparation cleanup mutated wallpapers");
    host.RestoreFailure = null;
    await new Session(host, journal).Restore();
    Check(host.Options == before.Options && !File.Exists(journal), "preparation recovery could not retry");
});
await Test("mount and cleanup failures retain both causes and can retry", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    host.FailAfterMount = true;
    host.RestoreFailure = new IOException("restoration unavailable");
    var error = await Failure(() => session.Mount(Before(host, new(10, 20))));
    Check(error is AggregateException both && both.InnerExceptions.SequenceEqual(new Exception[] { host.MountFailure, host.RestoreFailure }), "mount and cleanup errors were not retained");
    Phase(journal, "Settled");
    Check(File.Exists(journal) && host.Stopped is null, "failed rollback discarded retry state");
    host.RestoreFailure = null;
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "recovery retry did not restore the original scene");
});
await Test("a visible close no-op keeps the journal and can retry", async (host, session, journal) => {
    await session.Mount(Before(host, new(10, 20)));
    host.CloseNoOp = true;
    var error = await Failure(() => session.Restore());
    Check(error is IOException && File.Exists(journal) && host.Stopped is null, "visible remaining scene was accepted as closed");
    Check(host.ActualItems.Single().Path == "amadeus", "fake no-op unexpectedly removed the scene");
    host.CloseNoOp = false;
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Count == 0 && !File.Exists(journal), "close no-op recovery could not retry");
});
await Test("an explicit close exception keeps recovery state for retry", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    await session.Mount(Before(host));
    host.CloseFailure = new IOException("project close unavailable");
    var error = await Failure(() => session.Restore());
    Check(ReferenceEquals(error, host.CloseFailure) && File.Exists(journal), "close failure lost journal or error");
    host.CloseFailure = null;
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "close exception recovery could not retry");
});
await Test("missing original wallpaper after restore keeps retry state", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    await session.Mount(Before(host));
    host.IgnoreRestoredWallpaper = true;
    var error = await Failure(() => session.Restore());
    Check(error is IOException && File.Exists(journal), "missing original wallpaper was treated as restored");
    host.IgnoreRestoredWallpaper = false;
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "missing-original recovery could not retry");
});
await Test("the original project can be the same managed project on its original monitor", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "amadeus"));
    var before = Before(host);
    await session.Mount(before);
    await session.Restore();
    Check(host.ActualItems.SequenceEqual(before.Wallpapers) && !File.Exists(journal), "the original managed project was rejected as a leftover");
});
await Test("the original project does not permit a leftover on another monitor", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "amadeus"));
    await session.Mount(Before(host));
    host.ReportedItems = [new("secondary", "amadeus")];
    var error = await Failure(() => session.Restore());
    Check(error is IOException && File.Exists(journal), "a managed project outside the before-image monitor was accepted");
});
await Test("pending RPC can mount late and only a new host identity unlocks recovery", async (host, session, journal) => {
    var process = new ProcessIdentity(10, 20);
    host.ActualItems.Add(new("primary", "old"));
    host.FailPendingMount = true;
    var original = await Failure(() => session.Mount(Before(host, process)));
    Check(ReferenceEquals(original, host.PendingFailure), "pending RPC primary error was lost");
    Phase(journal, "Pending");
    Check(host.ClosedProjects.Count == 0 && host.RestoreCalls == 0 && host.Stopped is null, "pending RPC was cleaned up as settled");
    var reads = host.WallpaperReads;
    Check(await Failure(() => new Session(host, journal).Restore()) is InvalidOperationException, "pending RPC was accepted without restart");
    Check(await Failure(() => new Session(host, journal).Restore(process)) is InvalidOperationException, "same process was accepted as a restart");
    Check(host.WallpaperReads == reads && host.ClosedProjects.Count == 0, "pending recovery inspected or mutated the old host");
    host.CompletePendingMount();
    Check(host.ActualItems.Single().Path == "amadeus" && File.Exists(journal), "fixture did not reproduce the late mount");
    Check(await Failure(() => new Session(host, journal).Restore()) is InvalidOperationException, "a visible late mount cleared pending without restart");
    host.ActualItems.Clear();
    host.LiveProcesses.Clear();
    var restarted = new ProcessIdentity(11, 30);
    host.RunningProcess = restarted;
    host.LiveProcesses.Add(restarted);
    await new Session(host, journal).Restore(restarted);
    Check(host.ActualItems.Single().Path == "old" && host.Stopped == restarted && !File.Exists(journal), "actual host restart did not unlock recovery");
});
await Test("pending journal records the request receiver separately from cleanup ownership", async (host, session, journal) => {
    host.FailPendingMount = true;
    await Failure(() => session.Mount(Before(host)));
    using var saved = JsonDocument.Parse(File.ReadAllText(journal));
    Check(saved.RootElement.TryGetProperty("RequestProcess", out var receiver), "pending journal has no request receiver identity");
    Check(JsonSerializer.Deserialize<ProcessIdentity>(receiver) == host.RunningProcess, "request receiver was replaced by cleanup ownership");
});
await Test("a live request receiver blocks recovery even if the helper supplies another identity", async (host, session, journal) => {
    host.FailPendingMount = true;
    await Failure(() => session.Mount(Before(host)));
    var error = await Failure(() => new Session(host, journal).Restore(new(11, 30)));
    Check(error is InvalidOperationException && File.Exists(journal), "live request receiver was treated as exited");
    Check(host.Stopped is null && host.ClosedProjects.Count == 0, "pending recovery changed the live request receiver");
});
await Test("manual host restart unlocks pending recovery without helper startup ownership", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    host.FailPendingMount = true;
    await Failure(() => session.Mount(Before(host)));
    host.LiveProcesses.Clear();
    host.RunningProcess = new(11, 30);
    host.LiveProcesses.Add(host.RunningProcess);
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "manual restart did not unlock recovery");
    Check(host.Stopped is null, "recovery stopped a manually started shared host");
});
await Test("a reused PID with a new start identity does not keep the old request alive", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    host.FailPendingMount = true;
    await Failure(() => session.Mount(Before(host)));
    var previous = host.RunningProcess!;
    host.LiveProcesses.Clear();
    host.RunningProcess = previous with { Started = previous.Started + 1 };
    host.LiveProcesses.Add(host.RunningProcess);
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "PID reuse blocked recovery");
});
await Test("receiver identity lookup failure cannot dispatch a mount", async (host, session, journal) => {
    host.IdentityFailure = new IOException("request receiver identity unavailable");
    var error = await Failure(() => session.Mount(Before(host)));
    Check(ReferenceEquals(error, host.IdentityFailure), "receiver lookup error was hidden");
    Check(host.SetCalls == 0 && host.ClosedProjects.Count == 0, "mount dispatched without a receiver identity");
    Check(host.RestoreCalls == 1 && !File.Exists(journal), "pre-dispatch failure did not complete settings-only rollback");
});
await Test("receiver liveness lookup failure preserves pending recovery for retry", async (host, session, journal) => {
    host.FailPendingMount = true;
    await Failure(() => session.Mount(Before(host)));
    host.IdentityFailure = new IOException("request receiver liveness unavailable");
    var error = await Failure(() => new Session(host, journal).Restore(new(11, 30)));
    Check(ReferenceEquals(error, host.IdentityFailure) && File.Exists(journal), "unknown liveness was treated as process exit");
    Check(host.Stopped is null && host.ClosedProjects.Count == 0, "unknown liveness changed the desktop");
});
await Test("manual restart recovery keeps a new user wallpaper and its host", async (host, session, journal) => {
    var owned = host.RunningProcess;
    host.FailPendingMount = true;
    await Failure(() => session.Mount(Before(host, owned)));
    host.LiveProcesses.Clear();
    host.RunningProcess = new(11, 30);
    host.LiveProcesses.Add(host.RunningProcess);
    host.ActualItems.Add(new("primary", "user-new"));
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "user-new" && host.Stopped is null && !File.Exists(journal), "manual recovery removed a user's new scene or host");
});
await Test("recovery after receiver exit retains failures and can retry", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    host.FailPendingMount = true;
    await Failure(() => session.Mount(Before(host)));
    host.LiveProcesses.Clear();
    host.RunningProcess = new(11, 30);
    host.LiveProcesses.Add(host.RunningProcess);
    host.RestoreFailure = new IOException("recovery options unavailable");
    var error = await Failure(() => new Session(host, journal).Restore());
    Check(ReferenceEquals(error, host.RestoreFailure) && File.Exists(journal), "receiver exit was mistaken for restoration success");
    host.RestoreFailure = null;
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "receiver-exit recovery lost retry state");
});
foreach (var running in new[] { true, false }) {
    await Test("an identity-less pending journal is invalid with host running=" + running, async (host, session, journal) => {
        host.ActualItems.Add(new("primary", "old"));
        var before = Before(host);
        var invalid = JsonSerializer.Serialize(new {
            before.Project, before.Monitor, before.Options, before.Wallpapers,
            before.OwnedProcess, before.Mounted, Phase = "Pending"
        });
        File.WriteAllText(journal, invalid);
        if (!running) {
            host.LiveProcesses.Clear();
            host.RunningProcess = null;
        }
        var error = await Failure(() => session.Restore());
        Check(error is InvalidDataException, "pending journal without a receiver was not rejected as invalid");
        Check(File.ReadAllText(journal) == invalid, "invalid journal was rewritten or deleted");
        Check(host.SetCalls == 0 && host.RestoreCalls == 0 && host.WallpaperReads == 0 &&
            host.ClosedProjects.Count == 0 && host.Stopped is null, "invalid journal recovery touched the host");
        Check(host.ActualItems.SequenceEqual(before.Wallpapers) && host.Options == before.Options,
            "invalid journal recovery changed the desktop");
    });
}
await Test("a user selection wins after a failed rollback", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    host.FailAfterMount = true;
    host.CloseFailure = new IOException("close unavailable");
    await Failure(() => session.Mount(Before(host, new(10, 20))));
    host.CloseFailure = null;
    await host.SetWallpaper(new("primary", "user-new"));
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "user-new" && host.Stopped is null && !File.Exists(journal), "uncertain recovery overwrote the user's new scene");
});
await Test("legacy journals without a phase keep their original recovery semantics", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    var snapshot = Before(host);
    WriteLegacy(journal, snapshot);
    host.ActualItems.Clear();
    await session.Restore();
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "legacy pre-mount recovery changed");
    WriteLegacy(journal, snapshot with { Mounted = true });
    await host.SetWallpaper(new("primary", "user-new"));
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "user-new" && !File.Exists(journal), "legacy mounted recovery overwrote a user choice");
});
await Test("mounted lifecycle does not repeat a failed mount rollback", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    host.FailNextMount = true;
    var invoked = false;
    var error = await Failure(() => session.RunMounted(Before(host), () => { invoked=true; return Task.CompletedTask; }));
    Check(error is IOException && !invoked, "failed mount ran the mounted body");
    Check(host.ClosedProjects.Count == 1 && host.RestoreCalls == 2 && !File.Exists(journal), "failed mount rollback repeated or was skipped");
});
await Test("mounted body and cleanup errors remain observable together", async (host, session, journal) => {
    host.ActualItems.Add(new("primary", "old"));
    var bodyError = new IOException("mounted body failed");
    host.RestoreFailure = new IOException("mounted cleanup failed");
    var error = await Failure(() => session.RunMounted(Before(host), () => Task.FromException(bodyError)));
    Check(error is AggregateException both && both.InnerExceptions.SequenceEqual(new[] { bodyError, host.RestoreFailure }), "body or cleanup error was lost");
    Check(host.ClosedProjects.Count == 1 && host.RestoreCalls == 1 && File.Exists(journal), "mounted cleanup was skipped or repeated");
    host.RestoreFailure = null;
    await new Session(host, journal).Restore();
    Check(host.ActualItems.Single().Path == "old" && !File.Exists(journal), "body cleanup failure lost retry state");
});
await Test("outer failure cleanup preserves a user choice after settled mount rollback", async (host, session, journal) => {
    var process = new ProcessIdentity(10, 20);
    host.ActualItems.Add(new("primary", "old"));
    host.FailAfterMount = true;
    host.OnMountConfirmation = () => {
        host.ActualItems.Clear();
        host.ActualItems.Add(new("primary", "user-new"));
    };
    Exception? original = null;
    try { await session.RunMounted(Before(host, process), () => Task.CompletedTask); }
    catch (Exception error) { original = error; await session.ReleaseUnstartedHost(process); }
    Check(ReferenceEquals(original, host.MountFailure), "outer cleanup replaced the settled mount failure");
    Check(!File.Exists(journal) && host.ActualItems.Single().Path == "user-new", "mount rollback did not retain the user choice");
    Check(host.Stopped is null, "outer catch stopped the host after owned rollback kept a user choice");
});
await Test("outer failure cleanup preserves a user choice after mounted body failure", async (host, session, journal) => {
    var process = new ProcessIdentity(10, 20);
    var bodyError = new IOException("body failed after the user selected a new scene");
    Exception? original = null;
    try {
        await session.RunMounted(Before(host, process), async () => {
            await host.SetWallpaper(new("primary", "user-new"));
            throw bodyError;
        });
    }
    catch (Exception error) { original = error; await session.ReleaseUnstartedHost(process); }
    Check(ReferenceEquals(original, bodyError), "outer cleanup replaced the mounted body error");
    Check(!File.Exists(journal) && host.ActualItems.Single().Path == "user-new", "body cleanup did not retain the user choice");
    Check(host.Stopped is null, "outer catch stopped the host after body cleanup kept a user choice");
});
await Test("initial journal save failure can release the unstarted owned host", async (host, _, journal) => {
    var process = new ProcessIdentity(10, 20);
    var blocked = Path.Combine(Path.GetDirectoryName(journal)!, "not-a-directory");
    File.WriteAllText(blocked, "synthetic obstruction");
    var failedJournal = Path.Combine(blocked, "session.json");
    var session = new Session(host, failedJournal);
    Exception? original = null;
    var invoked = false;
    try { await session.RunMounted(Before(host, process), () => { invoked=true; return Task.CompletedTask; }); }
    catch (Exception error) { original=error; await session.ReleaseUnstartedHost(process); }
    Check(original is IOException && !invoked && !File.Exists(failedJournal), "fixture did not fail the initial save");
    Check(host.Stopped == process && host.SetCalls == 0 && host.RestoreCalls == 0, "unstarted host was not released without wallpaper mutation");
});
await Test("an existing pending journal prevents outer initialization cleanup", async (host, session, journal) => {
    var process = new ProcessIdentity(10, 20);
    File.WriteAllText(journal, JsonSerializer.Serialize(new {
        Project = "amadeus", Monitor = "primary", host.Options, Wallpapers = host.ActualItems,
        OwnedProcess = process, Mounted = false, Phase = "Pending", RequestProcess = host.RunningProcess
    }));
    Exception? original = null;
    try { await session.RunMounted(Before(host, process), () => Task.CompletedTask); }
    catch (Exception error) { original=error; await session.ReleaseUnstartedHost(process); }
    Check(original is InvalidOperationException && File.Exists(journal), "existing pending journal was discarded");
    Check(host.Stopped is null && host.SetCalls == 0 && host.RestoreCalls == 0, "pending host was stopped or mutated by outer cleanup");
});
await Test("snapshot failure after recovery preserves the recovered host for a user choice", async (host, session, journal) => {
    var restarted = new ProcessIdentity(10, 20);
    WriteLegacy(journal, Before(host, new(9, 10)) with { Mounted = true });
    host.ActualItems.Add(new("primary", "user-new"));
    await session.Restore(restarted);
    Check(!File.Exists(journal) && host.Stopped is null, "recovery did not preserve the user scene");
    host.OptionsReadFailure = new IOException("snapshot options read unavailable");
    Exception? original = null;
    try {
        var next = new Snapshot("amadeus", "primary", await host.GetOptions(), await host.GetWallpapers(), restarted);
        await session.RunMounted(next, () => Task.CompletedTask);
    }
    catch (Exception error) { original=error; await session.ReleaseUnstartedHost(restarted); }
    Check(ReferenceEquals(original, host.OptionsReadFailure), "snapshot error was replaced");
    Check(host.Stopped is null && host.ActualItems.Single().Path == "user-new", "outer cleanup stopped the recovered host kept for a user choice");
});
await Test("snapshot failure can release a different host started after recovery stopped the old host", async (host, session, journal) => {
    var recovered = new ProcessIdentity(10, 20);
    WriteLegacy(journal, Before(host, new(9, 10)));
    await session.Restore(recovered);
    Check(host.Stopped == recovered && !File.Exists(journal), "recovery did not release the old owned host");
    var newlyStarted = new ProcessIdentity(10, 21);
    host.OptionsReadFailure = new IOException("new host snapshot unavailable");
    Exception? original = null;
    try {
        var next = new Snapshot("amadeus", "primary", await host.GetOptions(), await host.GetWallpapers(), newlyStarted);
        await session.RunMounted(next, () => Task.CompletedTask);
    }
    catch (Exception error) { original=error; await session.ReleaseUnstartedHost(newlyStarted); }
    Check(ReferenceEquals(original, host.OptionsReadFailure), "new host snapshot error was replaced");
    Check(host.Stopped == newlyStarted, "cleanup ownership of the prior process prevented releasing the unstarted new host");
});
Task BoundRequest(string pipeName, Action<ProcessIdentity> dispatching,
    Func<NamedPipeClientStream, ProcessIdentity>? identify = null,
    Action<NamedPipeChannel, Action<NamedPipeClientStream>>? bind = null, DateTime? deadline = null)
    => WallpaperRequest.Send(pipeName, new SetWallpaperRequest { LivelyInfoPath = "synthetic", MonitorId = "primary" }, dispatching, identify, bind, deadline);
Method<SetWallpaperRequest, Empty> SetMethod() => new(MethodType.Unary,
    DesktopService.Descriptor.FullName, "SetWallpaper",
    Marshallers.Create<SetWallpaperRequest>(value => value.ToByteArray(), SetWallpaperRequest.Parser.ParseFrom),
    Marshallers.Create<Empty>(value => value.ToByteArray(), Empty.Parser.ParseFrom));
await Test("real Set transport persists its exact pipe receiver before dispatch", async (host, session, journal) => {
    var pipeName = "amadeus-wallpaper-contract-" + Guid.NewGuid().ToString("N");
    using var server = new NamedPipeServer(pipeName);
    var calls = 0;
    server.ServiceBinder.AddMethod(SetMethod(), (SetWallpaperRequest _, ServerCallContext _) => {
        using var saved = JsonDocument.Parse(File.ReadAllText(journal));
        var identity = JsonSerializer.Deserialize<ProcessIdentity>(saved.RootElement.GetProperty("RequestProcess"));
        using var receiver = System.Diagnostics.Process.GetCurrentProcess();
        Check(identity == new ProcessIdentity(Environment.ProcessId, receiver.StartTime.ToUniversalTime().Ticks), "Set executed before its actual receiver identity was persisted");
        Check(saved.RootElement.GetProperty("Phase").GetString() == "Pending", "Set executed before its pending journal was durable");
        calls++;
        return Task.FromResult(new Empty());
    });
    server.Start();
    host.Dispatch = dispatching => BoundRequest(pipeName, dispatching);
    await session.Mount(Before(host));
    Check(calls == 1, "bound Set did not execute exactly once");
    await session.Restore();
    Check(!File.Exists(journal), "synthetic transport session did not clean up");
});
foreach (var stage in new[] { "identity", "journal", "hook" }) {
    await Test("real Set transport refuses dispatch after " + stage + " failure", async (host, session, journal) => {
        var pipeName = "amadeus-wallpaper-contract-" + Guid.NewGuid().ToString("N");
        using var server = new NamedPipeServer(pipeName);
        var calls = 0;
        server.ServiceBinder.AddMethod(SetMethod(), (SetWallpaperRequest _, ServerCallContext _) => {
            calls++;
            return Task.FromResult(new Empty());
        });
        server.Start();
        var failure = new IOException("synthetic " + stage + " failure");
        host.ActualItems.Add(new("primary", "old"));
        host.Dispatch = dispatching => {
            if (stage == "journal") Directory.CreateDirectory(journal + ".tmp");
            return BoundRequest(pipeName, dispatching,
                stage == "identity" ? _ => throw failure : null,
                stage == "hook" ? (channel, capture) => WallpaperRequest.Bind(channel, capture, null) : null);
        };
        var error = await Failure(() => session.Mount(Before(host)));
        Check(stage == "journal" ? error is UnauthorizedAccessException : stage == "hook"
            ? error is NotSupportedException : ReferenceEquals(error, failure), "the " + stage + " failure was hidden or replaced");
        Check(calls == 0 && !File.Exists(journal), "Set executed without its durable receiver identity");
        Check(host.RestoreCalls == 1 && host.ClosedProjects.Count == 0 && host.ActualItems.Single().Path == "old", "pre-dispatch transport failure did not finish settings-only rollback");
    });
}
await Test("a Set connection arriving after timeout cannot identify persist or send", async (host, session, journal) => {
    var pipeName = "amadeus-wallpaper-contract-" + Guid.NewGuid().ToString("N");
    var identities = 0;
    var dispatches = 0;
    host.Dispatch = dispatching => BoundRequest(pipeName, identity => {
        Interlocked.Increment(ref dispatches);
        dispatching(identity);
    }, _ => {
        Interlocked.Increment(ref identities);
        return new(Environment.ProcessId, 1);
    }, deadline: DateTime.UtcNow.AddMilliseconds(100));
    var error = await Failure(() => session.Mount(Before(host))).WaitAsync(TimeSpan.FromSeconds(5));
    Check(error is RpcException rpc && rpc.StatusCode == StatusCode.DeadlineExceeded, "fixture did not expire before connection");
    Check(!File.Exists(journal) && host.RestoreCalls == 1, "an unstarted request did not finish settings rollback");
    using var pipe = new NamedPipeServerStream(pipeName, PipeDirection.InOut, 1,
        PipeTransmissionMode.Message, PipeOptions.Asynchronous);
    await pipe.WaitForConnectionAsync().WaitAsync(TimeSpan.FromSeconds(5));
    var bytes = await pipe.ReadAsync(new byte[4096]).AsTask().WaitAsync(TimeSpan.FromSeconds(5));
    Check(identities == 0 && dispatches == 0, "an ended Set invocation later identified or persisted its receiver");
    Check(bytes == 0 && !File.Exists(journal), "an ended Set invocation later wrote request bytes or recreated its journal");
});
await Test("timeout waits for an entered persistence boundary before allowing cleanup", async (host, session, journal) => {
    var pipeName = "amadeus-wallpaper-contract-" + Guid.NewGuid().ToString("N");
    using var server = new NamedPipeServer(pipeName);
    server.ServiceBinder.AddMethod(SetMethod(), (SetWallpaperRequest _, ServerCallContext _) => Task.FromResult(new Empty()));
    server.Start();
    var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
    var boundaryExited = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
    using var release = new ManualResetEventSlim();
    var deadline = DateTime.UtcNow.AddMilliseconds(500);
    host.Dispatch = dispatching => BoundRequest(pipeName, identity => {
        try
        {
            entered.TrySetResult();
            if (!release.Wait(TimeSpan.FromSeconds(5))) throw new TimeoutException("synthetic persistence release was not signalled");
            dispatching(identity);
        }
        finally { boundaryExited.TrySetResult(); }
    }, deadline: deadline);
    var mounting = session.Mount(Before(host));
    try
    {
        await entered.Task.WaitAsync(TimeSpan.FromSeconds(5));
        await Task.Delay(TimeSpan.FromMilliseconds(Math.Max(0, (deadline - DateTime.UtcNow).TotalMilliseconds) + 100));
        Check(!mounting.IsCompleted, "Set returned at its deadline while its persistence boundary was still running");
        Check(File.Exists(journal) && host.RestoreCalls == 0, "deadline cleanup raced the running persistence boundary");
    }
    finally
    {
        release.Set();
        await boundaryExited.Task.WaitAsync(TimeSpan.FromSeconds(5));
    }
    var error = await Failure(() => mounting).WaitAsync(TimeSpan.FromSeconds(5));
    Check(error is RpcException rpc && rpc.StatusCode == StatusCode.DeadlineExceeded, "the original request deadline was hidden");
    Phase(journal, "Pending");
    Check(host.RestoreCalls == 0, "a persisted uncertain request was rolled back as unstarted");
});
Console.WriteLine($"{passed} contracts passed.");
if (failures.Count > 0) throw new AggregateException("Wallpaper contract failures", failures);

sealed class FakeHost : IWallpaperHost
{
    public Func<Action<ProcessIdentity>, Task>? Dispatch;
    public ProcessIdentity? RunningProcess = new(100, 200);
    public HashSet<ProcessIdentity> LiveProcesses = [new(100, 200)];
    public Exception? IdentityFailure;
    public bool IsProcessRunning(ProcessIdentity identity) {
        if (IdentityFailure is not null) throw IdentityFailure;
        return LiveProcesses.Contains(identity);
    }
    public List<DesktopItem> ActualItems = [];
    public List<DesktopItem>? ReportedItems;
    public HostOptions Options = new(0, 1, false, false);
    public ProcessIdentity? Stopped;
    public bool FailNextMount;
    public bool FailAfterMount;
    public Action? OnMountConfirmation;
    public bool FailPendingMount;
    public bool CloseNoOp;
    public bool IgnoreRestoredWallpaper;
    public int SetCalls;
    public int RestoreCalls;
    public int WallpaperReads;
    public List<string> ClosedProjects = [];
    DesktopItem? pendingWallpaper;
    public readonly TimeoutException MountFailure = new("wallpaper mount confirmation timed out");
    public readonly IOException PendingFailure = new("native SetWallpaper RPC deadline exceeded");
    public Exception? OptionsFailure;
    public Exception? OptionsReadFailure;
    public Exception? CloseFailure;
    public Exception? RestoreFailure;
    public Task<HostOptions> GetOptions() {
        if (OptionsReadFailure is not null) throw OptionsReadFailure;
        return Task.FromResult(Options);
    }
    public Task<List<DesktopItem>> GetWallpapers() {
        WallpaperReads++;
        return Task.FromResult((ReportedItems ?? ActualItems).ToList());
    }
    public async Task SetWallpaper(DesktopItem wallpaper, Action? requestSettled = null,
        Action<ProcessIdentity>? requestDispatching = null) {
        if (requestDispatching is not null) {
            if (Dispatch is not null) await Dispatch(requestDispatching);
            else {
                if (IdentityFailure is not null) throw IdentityFailure;
                requestDispatching(RunningProcess ?? throw new IOException("no request receiver"));
            }
        }
        SetCalls++;
        ActualItems.RemoveAll(x => x.Monitor == wallpaper.Monitor);
        // RPC 截止后，服务器仍可在测试明确放行时完成挂载；不调用结算回调。
        if (FailPendingMount) { FailPendingMount = false; pendingWallpaper = wallpaper; throw PendingFailure; }
        if (FailNextMount) { FailNextMount = false; requestSettled?.Invoke(); throw new IOException("player failed"); }
        if (!IgnoreRestoredWallpaper || wallpaper.Path == "amadeus") ActualItems.Add(wallpaper);
        requestSettled?.Invoke();
        if (FailAfterMount) { FailAfterMount = false; OnMountConfirmation?.Invoke(); throw MountFailure; }
        return;
    }
    public void CompletePendingMount() {
        var wallpaper = pendingWallpaper ?? throw new InvalidOperationException("no pending native request");
        ActualItems.RemoveAll(x => x.Monitor == wallpaper.Monitor);
        ActualItems.Add(wallpaper);
        pendingWallpaper = null;
    }
    public Task CloseProject(string project) {
        ClosedProjects.Add(project);
        if (CloseFailure is not null) throw CloseFailure;
        if (!CloseNoOp) ActualItems.RemoveAll(x => Session.SamePath(x.Path, project));
        return Task.CompletedTask;
    }
    public Task SetSessionOptions() {
        Options = Options with { Browser = 1, DesktopImage = false, LockImage = false };
        if (OptionsFailure is not null) throw OptionsFailure;
        return Task.CompletedTask;
    }
    public Task RestoreOptions(HostOptions previous, bool images = true) {
        RestoreCalls++;
        if (RestoreFailure is not null) throw RestoreFailure;
        Options = Options with { Browser = Options.Browser == 1 ? previous.Browser : Options.Browser,
            DesktopImage = images && !Options.DesktopImage ? previous.DesktopImage : Options.DesktopImage,
            LockImage = images && !Options.LockImage ? previous.LockImage : Options.LockImage };
        return Task.CompletedTask;
    }
    public Task StopOwnedProcess(ProcessIdentity process) { Stopped = process; return Task.CompletedTask; }
}
