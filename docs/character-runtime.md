# Character startup and conversation ownership

`AMADEUS_CHARACTER_ID` selects the backend's character prompt pack. Its default is
`kurisu`. Configure it through the existing startup environment, project `.env`,
or persisted desktop setting, then restart the backend. A saved desktop value is
pending until restart; an explicit parent-process environment value takes
precedence. There is no character selector or live switching API. Changes to a
character file also apply only after restart.

A custom packaged role needs a matching `characters/<id>.toml` resource with a
stable lowercase id and a recognizable name, for example:

```toml
[names]
character_id = "mira"
name = "Mira"
```

Other name forms, persona text and Japanese Host phrases are optional. Missing
entries use name-based or neutral defaults; they do not inherit Kurisu's persona
or character-reference corpus. The built-in Kurisu resource remains a required
part of the application and its Kurisu-specific persona editor. Selecting a
prompt role does not select or reload voice models, images or emotion assets.

Every conversation stores an immutable `character_id`. New conversations use the
startup character. Older session files without the field belong to `kurisu`;
explicit invalid values fail validation. Opening or continuing another character's
conversation is refused before the active history, project context, or turn
authority changes. The error names the startup setting needed to reopen it. The
conversation list retains its title and character identity without exposing its
transcript through a separate history reader.

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

Accepted Work addressed to a cooperative Provider context reads that context's
existing persistent state without recreating its speaking loop. Existing checks
for settled state, revision, native identity and workspace still apply. This does
not add automatic retries or permit redispatch of an already-bound effect.
