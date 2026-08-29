# Amadeus Lively Wallpaper Entry

This folder is a lightweight Lively Wallpaper wrapper for the local Amadeus
web wallpaper runtime. Lively is the recommended external wallpaper host on
Windows; Wallpaper Engine remains compatible.

## Recommended setup

1. Start Amadeus.
2. In Lively, add a web wallpaper using the stable local URL below. WebView2 is
   the recommended Lively web player.
3. Click **Wallpaper** in the Amadeus sidebar. The wrapper can remain loaded:
   while wallpaper mode is off it waits and retries discovery without starting
   the bridge itself.

```text
http://127.0.0.1:17777/wallpaper/lively/index.html
```

The backend-served wrapper discovers the current asset and bridge ports from
`/wallpaper/bridge-info`. Do not hard-code the normally observed `17778` asset
port or `17797` bridge port because both can move when a port is occupied.

For standalone diagnostics, run:

```powershell
py -3.12 tools\run_wallpaper_engine_bridge.py
```

Keep that process open and use the exact `Lively URL` it prints. Explicit port
parameters remain available for troubleshooting:

```text
index.html?assetPort=17778&bridgePort=17797
```

The bridge endpoint is host-agnostic and uses:

```text
http://127.0.0.1:<bridgePort>/wallpaper/state
http://127.0.0.1:<bridgePort>/wallpaper/events
```

Lively supports webpages as wallpaper; see its
[Web Player documentation](https://github.com/rocksdanister/lively/wiki/Web-Player).
