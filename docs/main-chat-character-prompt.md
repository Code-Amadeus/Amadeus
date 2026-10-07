# Kurisu Japanese persona

Settings → General → Kurisu Japanese persona has one editor for Kurisu's Japanese
identity/personality override. When empty, the editor displays her built-in
character as a placeholder. The placeholder is a hint, not editable content,
and is never saved as the override. Enter a replacement in Japanese, then save.
Clearing the editor and saving restores the built-in character. Whitespace-only
input also restores it. The default comes from `characters/kurisu.toml` and follows future updates.

The override applies when Kurisu is the startup character and replies are Japanese.
It affects subsequent Main Chat and inherited AUIP/browser requests, including
AUIP B2 action selection, proposal authorization and browser planning. Personality
can influence both speech and reasonable choices within each branch's contract.
Host permissions, legal actions, receipt verification and execution authority
continue to come from the application's state and rules, not the persona text.
Language, reasoning disclosure, expression/TTS formatting, delegation, provider routing
and control contracts remain composed by the application. English replies,
the Hybrid first-sentence module, VN prompts and Work commentary keep their own
prompts and do not inherit this override. Editing the persona does not change
the startup character, Host identity, art or voice assets.

Saving applies to subsequent applicable model requests, including existing
Cooperative Chat, AUIP and browser branches. Existing messages and history are
not rewritten. No backend restart is needed for an edit to the active Kurisu
persona. Changing the startup character requires a backend restart.
See [character startup and conversation ownership](character-runtime.md) for
the startup setting and the boundary between role-specific chats and shared Work.
Desktop settings persist across backend restarts. If the backend is disconnected
or cannot apply the update, the UI reports that it is saved for the next start;
the default placeholder is loaded from the backend and remains available
while disconnected if it has already been loaded.

The setting is `AMADEUS_MAIN_CHAT_CHARACTER_PROMPT_JA` (maximum 8192 characters).
An explicit parent-process value locks GUI editing, as for other desktop
settings. An explicitly saved empty string takes precedence over a project
`.env` override so restoring the built-in character survives restart.
The saved Japanese override always belongs to Kurisu. When another character,
such as Mira, is active, the editor still shows Kurisu's saved override and default
and marks it inactive; it does not edit Mira. English output also makes it inactive.
The setting remains stored and applies again after starting as Kurisu with Japanese
output. Character prompt data is packaged
independently of art and voice assets; loading it does not depend on the working
directory. Retired direct CLI queries and `LOCAL_LLM_SYSTEM_PROMPT` are removed;
the messages interface continues to reject the CLI backend.
