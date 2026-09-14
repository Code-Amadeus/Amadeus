# Compact companion panel — 0.15 Alpha candidate

The optional panel brings the existing VN-style portrait and speaking card beside a
selected Work preview. It reuses the existing portrait cache and Host presentation
signals; it does not start another VN session, TTS pipeline, Work, or AUIP authority.

Speech completion must leave the just-spoken line on an already open card. Empty
subtitle cleanup and the transition to standby must not replace it with a welcome
line; only the next non-empty subtitle replaces it. The card also restores the
latest line on reopening or reconnecting, including speech received while closed.
This is a presentation snapshot held in memory for the current bridge lifetime;
it is not saved conversation history. A fresh bridge starts without a retained line.
Wallpaper subtitles continue to clear normally. The VN-style `VOICE` indicator and
animated signal bars follow the Host's speaking state, flattening at standby or
disconnect. They indicate speech activity, not measured audio amplitude.

Use the companion button in the Electron Slice to open or close it. Docking reserves
space beside the selected preview; dragging detaches the panel. Closing restores a
preview whose Host-adjusted bounds have not subsequently changed. Missing optional
portrait media leaves the text/avatar presentation available.

Only an attached companion suppresses the corresponding wallpaper portrait/captions.
Disconnecting or closing restores presentation without stopping audio or work.
Subscription replay and credential refresh keep a reconnecting Slice on current Host
state. The existing macOS wallpaper and keyboard input lifecycle remain intact.

This PR covers presentation and window lifecycle. It is separate from routing and ACP.
Automated projection, docking/layout, reconnect and build checks do not claim complete
packaged desktop or physical microphone acceptance.
