# Support

The initial supported surface is deliberately narrow:

- Windows as the current verified reference platform;
- CPython 3.12.10;
- Node.js 22 for the Electron source checkout;
- the model-less CPU backend as the reproducible baseline;
- optional CUDA 12.4 ASR/TTS on compatible NVIDIA hardware.

macOS, Linux, packaged installers, automatic updates, third-party character packs, and
arbitrary local model combinations are not yet supported contracts.

When reporting a problem, include the commit, selected profile, relevant IDs and phases,
and a redacted log excerpt. Do not post `.env`, tokens, conversation text, model or voice
files, or personal filesystem paths. Raw text logging is disabled by default; enable
`LOG_USER_CONTENT=true` only for a local diagnostic run when its privacy cost is acceptable.

Feature requests and ordinary reproducible bugs may use public issues. Security-sensitive
reports should follow `SECURITY.md`.
