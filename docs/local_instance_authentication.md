# Local instance authentication

Amadeus is currently a single-user, loopback-only desktop application. Its
authentication boundary identifies one Electron process and the Python backend
that process owns; it does not introduce accounts, roles, or a second product
permission system.

## Shipping desktop contract

Electron generates a random token and instance nonce in process memory, then
starts `server.app` with authentication set to `required`.

- `/health` exposes the nonce but never the token. Electron refuses to reuse a
  process whose nonce does not match, so an unrelated service on the fixed port
  cannot impersonate backend readiness.
- the main WebSocket sends the token as a WebSocket subprotocol value; the
  token is not placed in the URL or access log;
- mutation HTTP endpoints send the token in `X-Amadeus-Token`;
- the preload returns the bounded connection descriptor only to an owned
  Amadeus renderer;
- credentials are not persisted and remain valid only for that desktop
  process lifetime.
- Python removes the credential variables from its environment immediately
  after constructing the authentication policy, so Provider and model child
  processes cannot inherit desktop authority.

Origin checks remain an independent defense. Authentication proves which
desktop instance connected; it does not make an untrusted browser origin safe.

## Separate authority realms

Authentication does not grant product authority.

- Work permissions remain bound to WorkItem, Attempt, action, and scope.
- Provider output remains evidence, never permission.
- AUIP applications do not receive the desktop credential. They retain their
  one-time attach ticket, per-connection bridge token, method allowlist, and
  receipt authority.
- Electron IPC continues to validate the exact sender for privileged desktop
  operations.

This separation leaves a stable extension point: a future remote or multi-user
transport can replace the local authentication policy with a durable identity
provider while reusing the existing Work/AUIP authorization contracts.

## Direct backend development

`AMADEUS_BACKEND_AUTH_MODE=auto` is the default. With no token and nonce, a
direct `python -m server.app` launch remains an unauthenticated loopback-only
development backend and emits a warning. Existing probes and browser-only
development therefore keep their current behavior.

To prelaunch an authenticated backend for Electron, give both processes the
same URL-safe values:

```text
AMADEUS_BACKEND_AUTH_MODE=required
AMADEUS_BACKEND_TOKEN=<at least 32 URL-safe characters>
AMADEUS_BACKEND_INSTANCE_NONCE=<at least 16 URL-safe characters>
```

Partial or weak credentials fail at startup. Binding beyond loopback is not a
supported consequence of this local scheme; remote access requires a separate
threat model, transport security, durable identity, rate limits, and explicit
user/session isolation.
