# Amadeus Lively Wallpaper Entry

This folder is a lightweight Lively Wallpaper wrapper for the local Amadeus
web wallpaper runtime.

Default local target:

```text
http://127.0.0.1:17778/render/web/wallpaper_engine.html?bridgePort=17797&host=lively
```

If the Python bridge chooses a different port, update the Lively URL or pass
query parameters to `index.html`:

```text
index.html?assetPort=17778&bridgePort=17797
```

The bridge endpoint is host-agnostic and uses:

```text
http://127.0.0.1:<bridgePort>/wallpaper/state
http://127.0.0.1:<bridgePort>/wallpaper/events
```
