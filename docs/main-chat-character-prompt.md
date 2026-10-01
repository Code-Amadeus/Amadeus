# Main Chat character prompt

Settings → General → Main Chat character previews the Japanese identity and
personality currently used by Main Chat. Copy the built-in character into the
editor to start from it, or enter a replacement in Japanese, then save.
Clearing the editor and saving restores the built-in character. Whitespace-only
input also restores it. The default remains in code and follows future updates;
it is not copied into the saved override unless explicitly chosen for editing.

The override changes Japanese Main Chat identity/personality only. Language,
reasoning disclosure, expression/TTS formatting, delegation, provider routing
and control contracts remain composed by the application. English Main Chat,
the Hybrid first-sentence module, VN prompts and inherited Browser/AUIP role
prompts keep their existing character settings. This is a prompt customization,
not a replacement of the character's art, voice or host identity.

Saving applies to subsequent Main Chat model requests, including existing
Cooperative Chat sessions. Existing messages and history are not rewritten.
Desktop settings persist across backend restarts. If the backend is disconnected
or cannot apply the update, the UI reports that it is saved for the next start;
the current-runtime preview requires a backend connection.

The setting is `AMADEUS_MAIN_CHAT_CHARACTER_PROMPT_JA` (maximum 8192 characters).
An explicit parent-process value locks GUI editing, as for other desktop
settings. An explicitly saved empty string takes precedence over a project
`.env` override so restoring the built-in character survives restart.
`LOCAL_LLM_SYSTEM_PROMPT` remains the independent whole-prompt override for
legacy direct CLI calls that do not receive a Main Chat system prompt.
