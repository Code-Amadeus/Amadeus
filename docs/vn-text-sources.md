# VN text sources (experimental)

VN Player accepts live displayed text through the existing `vn.line` method.
`AgentVNTextSource` and `LunaVNTextSource` translate their external transports into
that input; they do not choose reactions, alter VN memory, or interpret the story.
The current VN runtime and game profile remain PARANORMASIGHT-oriented. Connecting
another game's text does not yet qualify its semantic behavior.

## 0xDC00 Agent

Install [0xDC00 Agent](https://github.com/0xDC00/agent/releases) and select a
compatible script from its [script collection](https://github.com/0xDC00/scripts)
before connecting Amadeus. First confirm that Agent captures the game's text on
its own. The PARANORMASIGHT launch profile still looks for the separately installed
`visual novel player` directory beside the Amadeus checkout. It uses the local
Agent executable and game script from that directory. Amadeus does not install,
update, or redistribute Agent or game files.

The Agent adapter optionally launches the installed executable with its script,
consumes `copyText` messages from its WebSocket, and uses the existing clipboard
fallback when that connection is unavailable. Plain text is valid. A modified
game script may additionally supply `speaker` and `script_id` in JSON. The adapter
passes them through `vn.line` without interpreting their story meaning.

The adapter owns only the Agent process it starts. If a matching Agent process
is already running, close it before asking Amadeus to launch another; Amadeus
does not terminate externally started Agent processes.

## LunaTranslator

Configure text extraction and enable [LunaTranslator's network service](https://docs.lunatranslator.org/en/apiservice.html) in Luna.
In VN Player's advanced options, select **LunaTranslator original text**, enter
the WebSocket URL shown by Luna with the path `/api/ws/text/origin`, and start the
session. Amadeus connects to Luna's already-running original-text stream. It does
not launch Luna, select hooks, change Luna settings, or consume translations.
The original-text stream supplies text; speaker, script ID, choices, and scene
metadata are not assumed. Repeated text is forwarded as repeated observations.

When using the `vn.launch.start` API directly, pass `textSource: "luna"`,
`lunaWsUrl: "ws://127.0.0.1:<configured-port>/api/ws/text/origin"`, and
`launchOverlay: false`. The port comes from the user's Luna network-service
configuration. Stop with `vn.launch.stop`; stopping disconnects Amadeus without
stopping Luna.

## Current boundary

`vn.line` remains the only live VN text input. Nonempty `text` is required;
`speaker` and `script_id` are optional. The source adapters report connection
state and the most recent text preview through `vn.launch.status`.

The adapters never deduplicate by text or by an unverified Agent message ID. A
text-only stream cannot reliably distinguish a replay from a real repeated line;
both are forwarded. Agent hybrid mode keeps only one live transport active at a
time, avoiding routine WebSocket/clipboard double delivery. Source-specific
replay suppression can be added when the external transport supplies a verified
event identity.
