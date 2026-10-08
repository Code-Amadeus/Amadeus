# Character startup and conversation ownership

`AMADEUS_CHARACTER_ID` selects the backend's conversational character. Its default
is `kurisu`. Settings can create/edit user characters and select the next startup
identity; the existing startup environment and project `.env` are still supported.
A saved selection is pending until restart, and an explicit parent-process
environment value takes precedence. The built-in Kurisu Japanese override keeps
its existing immediate-request behavior. User definitions apply after restart;
there is no live identity switch. See [local character management](character-management.md)
for personality scope, shared appearance/voice, and offline startup recovery.

Built-in Kurisu remains in `characters/kurisu.toml`. User roles are read from
`.amadeus/characters/<id>.toml` (or explicit `AMADEUS_CHARACTER_DIR`), not the
program's `characters/` directory. The GUI generates stable IDs. Manually authored
records can retain a valid existing ID and a recognizable name, for example:

```toml
[names]
character_id = "mira"
name = "Mira"
```

Other name forms, persona text and the 14 Japanese VN commentary lines are
optional. Missing entries use name-based or neutral defaults; they do not inherit
Kurisu's persona or character-reference corpus. The existing RAG corpus belongs
to Kurisu: other characters return no reference and do not query that index.
Per-character knowledge-base management is not implemented. The built-in Kurisu
resource remains a required part of the application and its Kurisu-specific
persona editor. Selecting a prompt role does not select or reload voice models,
images or emotion assets.

The 93 Japanese Host fact/status lines are fixed Host templates, including their
reviewed Kurisu wording and neutral wording for other characters. Character
files cannot override execution, verification, receipt, permission or focus
conclusions, even with matching template variables. The optional `voice_lines`
table accepts only VN commentary keys used by the story reaction path; those
comments are not Host execution evidence. An experimental pack written against
an earlier draft must remove Host fact keys from `voice_lines`; explicitly
providing one fails loading, including for the built-in character id.

Every conversation stores an immutable `character_id`. New conversations use the
startup character. Older session files without the field belong to `kurisu`;
explicit invalid values fail validation. Opening, continuing, renaming or deleting
another character's conversation is refused before its file, active history,
project context or turn authority changes. The error names the startup setting
needed to use the owning character. The conversation list retains its title and
character identity, with rename/delete controls disabled, without exposing its
transcript through a separate history reader. This is the application's character
ownership rule, not a separate local-user access-control system or global session
administration interface.

Only conversations belonging to another character show a role label in the list.
Startup restores a conversation for the selected character, or creates one if
none exists. Deleting the current conversation selects another conversation for
that character if available; otherwise it leaves the chat empty, even if other
characters' conversations remain. Selection errors appear separately from chat
history.

Saving an existing conversation requires reading its persisted ownership first.
If its file is unreadable or invalid, saving fails and leaves the existing file
untouched. Only a missing character field in an otherwise valid legacy record
has the historical Kurisu default.

Projects, Work, and artifacts use their existing shared references and permissions.
Opening a retained project or Work item selects or creates a conversation for the
startup character. It does not import the original character's conversation.
Unkept drafts retain their existing conversation-scoped lookup rules; after a
draft is retained as a project, existing project and explicit-file references can
find it from another conversation.

The first acceptance of a Work plan snapshots its source conversation's character
identity in the immutable admission evidence. Replay and recovery use that accepted
fact, including the historical Kurisu default for older admissions, without loading
the original character pack or opening its chat. Allowed old-Work notices can be
presented by the startup character through existing delivery policy. Such narration
is not added as an assistant message to the foreign original conversation or
copied into the current conversation as shared history.

For Work accepted from a conversation, Retry and Resume retain the original
accepted role name, including when another character is active. Caller metadata
cannot replace that name. A newly accepted amendment retains its own source
identity instead of inheriting the previous Attempt's role. The execution prompt
therefore keeps the conversation's role-reference context on recovery, just as
on its first execution. These rules do not rewrite the accepted plan or add
recovery authority.

Tasks created directly on the Work page have no source conversation role. Their
initial execution, Retry and Resume omit that identity and its role-reference
context; they do not reinterpret "yourself" as the conversational character.
Execution guidance that needs to name the frontend character uses the fixed
startup name for these independent tasks, without recording it as a task source.
The historical Kurisu default applies only when reading an older accepted
conversation plan, not when an independent request has no role field.

Accepted Work addressed to a cooperative Provider context reads that context's
existing persistent state without recreating its speaking loop. Existing checks
for settled state, revision, native identity and workspace still apply. This does
not add automatic retries or permit redispatch of an already-bound effect.
