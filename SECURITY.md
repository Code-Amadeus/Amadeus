# Security policy

Amadeus is currently a source Alpha. Only the latest `0.1.x` source snapshot is in
scope for security fixes. It is designed for a trusted local Windows account and is
not a hardened multi-user or remote-host service.

Please use GitHub's private vulnerability reporting for this repository when it is
available. Do not include secrets, model files, private conversations, voice samples,
or personal paths in a public issue. If no private reporting channel is available,
withhold sensitive reproduction material until the project owner publishes one.

Security-relevant invariants include:

- backend mutation endpoints and WebSocket sessions require per-launch local auth;
- provider output, application content, and canvas actions are untrusted input;
- the Host alone grants permissions and commits durable work facts;
- AUIP actions require exact session/revision authority and application receipts;
- runtime logs redact user-derived text unless `LOG_USER_CONTENT=true` is explicitly set.

Known Alpha limits are recorded honestly: the source checkout is the supported desktop
entry, local model and character packs are user-supplied, and no signed installer or
automatic updater is currently distributed.
