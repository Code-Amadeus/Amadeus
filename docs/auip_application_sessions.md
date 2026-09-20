# AUIP application sessions

AUIP is Amadeus's cooperative application protocol. It allows an application
produced or registered as a verified Work Artifact to become an interactive
`AppSession` without treating role narration as execution authority.

The current contract version is `amadeus.auip/v0`.

## What AUIP is not

AUIP is not:

- a Work Provider;
- an MCP server or Skill surface;
- a way to give Main Chat arbitrary application or filesystem tools;
- a replacement for Work, Project, Draft, or Artifact ownership.

Work Providers create or modify attributable artifacts. AUIP begins only after
the Host has identified an exact artifact and the user chooses to interact with
it. Main Chat, Provider execution, and an attached application remain separate
authority domains.

## Attach lifecycle

The supported lifecycle is:

```text
verified Artifact
  -> auip.attach.prepare
  -> short-lived, single-use attach ticket
  -> restricted application WebSocket
  -> auip.register with a declared manifest
  -> bounded AppSession
  -> state / semantic events / typed action receipts
  -> disconnect or explicit close
```

The Host resolves the current conversation Session and validates the Artifact
before issuing a ticket. The ticket binds the Session, an immutable Artifact
reference, and an expiry. The application submits the ticket rather than a
conversation id or an arbitrary filesystem path.

Registration consumes the ticket exactly once. One application connection is
bound to one AppSession and receives only action requests for that AppSession.
Tickets are never model-visible state and must not be placed in durable
settings, logs, or application query strings.

## Manifest contract

An application registers a manifest containing:

- a stable application id, title, and version;
- declared semantic event types;
- declared typed actions and their input schemas;
- supported stances (`spectator` or `participant`);
- optional standard situation kinds and controller policy.

The validator is implemented in
[`server/auip_contract.py`](../server/auip_contract.py). Messages are bounded in
size, identifiers and semantic types are validated, and undeclared events or
actions are rejected.

The current standard situation shapes include action availability, grids,
choices, scalar state, sequences, and controller state. These are bounded
semantic projections, not a generic channel for copying an application's
entire internal state into the character context.

## Authority boundary

The Host owns:

- AppSession identity and lifecycle;
- the authoritative revision;
- the Artifact binding;
- action legality, preconditions, and user authorization;
- controller leases and takeover rules;
- action invocation and verified receipts;
- the compact state projected to the conversational role.

An application may publish only declared state and semantic events and may
receive only declared actions. AUIP grants no `work.*`, `provider.*`, `tts.*`,
arbitrary filesystem, or other-Session authority.

The role may interpret the user's intent or narrate a verified transition, but
its prose is never proof that an action ran. Completion belongs to the
Host-accepted receipt path.

## Disconnect and recovery behavior

A lost application connection becomes a visible `disconnected` AppSession
state. Pending actions are invalidated rather than continued against stale
state. The current v0 attach path does not promise automatic reconnection or
cross-restart AppSession recovery.

An application may be opened successfully at the operating-system level before
it has registered. Amadeus therefore distinguishes an OS open result from a
connected AppSession and does not narrate a successful attachment until the
Host observes registration.

## Artifact interaction

The Electron Work and Artifact surfaces can preview or open ordinary files.
For an AUIP-capable artifact, **Interact with Amadeus** prepares the bounded
attach handoff. The Host validates workspace ownership, file type, digest, and
launch entry before exposing a launch descriptor.

Artifact discovery is intentionally scoped to attributable Work changes. It
does not scan the user's Desktop or home directory. External material must
enter through an explicit Provider artifact event or an approved export
contract before it can become an AUIP attachment candidate.

## Developer status

The v0 protocol and runtime are implemented inside this repository. The
separate [Code-Amadeus/auip](https://github.com/Code-Amadeus/auip) repository is
currently a public namespace placeholder. This release does not claim a
standalone SDK, reviewed reference application set, or conformance suite.

Relevant implementation entry points:

- [`server/auip_contract.py`](../server/auip_contract.py) — manifest and message validation;
- [`server/auip_runtime.py`](../server/auip_runtime.py) — Host-owned AppSession ledger;
- [`server/auip_app_connection.py`](../server/auip_app_connection.py) — restricted application connection;
- [`server/handlers/auip_handler.py`](../server/handlers/auip_handler.py) — trusted Host control methods;
- [`server/work_artifact_registry.py`](../server/work_artifact_registry.py) — attributable Artifact discovery.
