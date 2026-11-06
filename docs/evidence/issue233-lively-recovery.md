# Windows managed wallpaper: #233

The offline counterexample reproduced on unmodified Amadeus main
`53f8f4f2b130edcc6e30cf22826b657bbac344ec`. A settled Set was followed by
a mount-confirmation timeout; Lively's simulated list returned no entries, and
its project Close succeeded without removing the managed scene.

| Same synthetic case | Recovery journal | Owned-host shutdown |
| --- | --- | --- |
| Unmodified main, failed Close | Deleted | Requested |
| With the player check, failed Close | Retained; both errors reported | Not requested |
| Retry after Close actually removes the scene | Deleted | Requested |

Run `python tools/probes/windows_wallpaper_counterexample.py` and
`python -m pytest tests/test_windows_wallpaper_helper.py`. The latter includes
a real Windows child process with synthetic player arguments, left running
after the fake registry has removed its row. It exercises native process
inspection; it is not a stock-Lively desktop reproduction. The host's
enumeration and close semantics still need independent correction upstream.

## Span and process-exit regressions

On PR head `5658a379`, the span repro refused both restoration attempts: the
synthetic registry moved the project from the old primary to the new one while
the real hidden test wrapper kept its original launch arguments. No Close was
sent. With the project-count check, the same repro closed the project once,
restored the original scene and cleared the journal. A separate two-wrapper
case still vetoes a partial report even when an unrelated project is listed.
These are Windows process and lifecycle tests with a simulated registry, not
a physical multi-monitor Lively run.

The command-line query now translates NTSTATUS using Windows' own mapping.
`STATUS_PROCESS_IS_TERMINATING` reaches the same access-denied handling as
the image query, while an unrelated invalid-parameter error remains distinct.
The two-second post-Close window was introduced by this PR. The nine earlier
stock desktop runs belong to `5658a379`; they do not qualify this revision.

## Upstream report drafts (not submitted)

Destination: [lively-community/lively issues](https://github.com/lively-community/lively/issues).
The two reports concern different RPC contracts. Both were checked on the
official 2.2.1.0 release tag (`6860a4093fc50058c4815908658a4391c4449935`)
and default branch (`25a0a5f4ed1988ff42117d3a17feb321062237fe`). The
source findings below do not rely on a modified installed DLL.

### GetWallpapers can complete successfully with an incomplete inventory

**Problem.** Clients cannot distinguish an empty desktop from an error
enumerating its wallpapers. An exception after the first row can also yield a
successful partial inventory.

**Upstream check and evidence.** In
[GetWallpapers](https://github.com/lively-community/lively/blob/25a0a5f4ed1988ff42117d3a17feb321062237fe/src/Lively/Lively/RPC/WinDesktopCoreServer.cs#L197-L241),
the server assigns the possibly null display name to a protobuf string inside
the enumeration loop. Its catch logs the exception and returns normally.
Suggested regression: supply a registry with a null display name, both as the
first and second entry. Return the complete valid records or a failed RPC;
never return success with just the prefix. This is source evidence, not a
new live reproduction of the fault. No matching report was found by method name.

### CloseWallpaperLibrary success does not establish project/player cleanup

**Problem.** A project-scoped Close can report success on a metadata error;
even when the registry removes a matching WebView2 wallpaper, the reply does
not establish that its player exited.

**Upstream check and evidence.** [CloseWallpaperLibrary](https://github.com/lively-community/lively/blob/25a0a5f4ed1988ff42117d3a17feb321062237fe/src/Lively/Lively/RPC/WinDesktopCoreServer.cs#L285-L298)
catches exceptions constructing/closing the library item, then returns an empty
success response. [The core](https://github.com/lively-community/lively/blob/25a0a5f4ed1988ff42117d3a17feb321062237fe/src/Lively/Lively/Core/WinDesktopCore.cs#L981-L1005)
closes/disposes matching players and removes their records; the RPC does not
wait for a verified exit of the specific player. Check bad metadata and an
induced player-close failure separately from a valid project already absent.
The downstream [#233](https://github.com/Code-Amadeus/Amadeus/issues/233)
records both a real IPC close control and a separately labelled simulated
counterexample. [Lively #2939](https://github.com/lively-community/lively/issues/2939)
concerns browser subprocesses after switching wallpapers, not this RPC's
completion contract.
