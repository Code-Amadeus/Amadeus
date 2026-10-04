# BC7 cache experiment

This is an opt-in hardware experiment on top of PR #155's published 2 GiB
CPU + GPU budget. It does not enable sampling or caching in the product.

`build.cjs ROOT NEW_DIRECTORY` derives every installed Kurisu KTX2 frame with
the repository's exact Basis JS/WASM, BC7 format 6 and the same transcoder flags.
Two workers compress to zstd level 15, verify lossless roundtrips, flush temporary
files and rename them, then publish the index last. It refuses existing output.
The separate build summary records CPU time, wall time and disk payload sizes.
The original pack is untouched. This prototype supports one-level 2D frames.

The texture probe's `--bc7-cache DIRECTORY` verifies the manifest, transcoder
and every source content hash before the timed run. The existing asset server
mounts the cache read-only. A probe-only adapter replaces source fetches with
cache fetches when the actual WebGL context exposes BPTC. Two browser workers
check compressed SHA-256, inflate via the pinned zstddec WASM decoder and check
raw BC7 SHA-256 before constructing the same Pixi compressed resource. Invalid
cache files fall back once to the UASTC source; cancellation stays cancelled.
There is no decoded side cache: FrameStore retains ownership and counts CPU
and GPU bytes exactly as before. Its scheduling, eviction, GC and context
restoration are unchanged. The shared UASTC counter excludes cache hits; the
backend's older `transcodesCompleted` counter counts both kinds of decode jobs.
Use `render.transcodes` and `render.bc7Cache` to distinguish them.
Likewise, backend `fetchedPayloadBytes` counts decoded bytes in this adapter;
use the cache's `compressedBytes` counter for cache payload transfer volume.

The experiment uses an already-built cache. Automatic background derivation,
runtime repair/publishing, disk eviction, other hosts and package distribution
are not implemented. Its fallback can recover playback but does not rebuild a
bad file. Source validation is once per session, outside the playback timer;
cache integrity checks are inside timed loads. Do not edit assets during runs.

`run_texture_matrix.py` runs standard 60 FPS with an identical fixed clip route,
first sampling off, then sampling on, then sampling plus cache. Each run uses a
fresh Electron profile and host, the current source, actual GPU verification,
2-second samples, and post-journey companion, GPU disposal and context pixel
checks. An independent `nvidia-smi` observer records total device memory; that
includes other applications and is not per-process allocation. Electron GPU
process private memory is system process memory, not dedicated VRAM.

Report the full 600 seconds and the last 300 seconds separately. Fixed clip
selection improves comparability but does not reproduce every natural graph
decision or real microphone/model workload. Sampling retains its existing
playback semantics, including its change to fast-transition stepping.
