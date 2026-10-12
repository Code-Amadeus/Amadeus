# Owned backend shutdown

Desktop restart and quit first request authenticated graceful shutdown. If the
owned backend remains alive, Windows force-stop targets that PID and its tree;
it does not search for or terminate unrelated Python processes. Other platforms
retain root-process termination and do not claim descendant cleanup.

Stopping the root and confirming child lifetime are distinct facts. A successful
Windows tree termination followed by root exit confirms both. A root that exits
during a failed escalation is stopped, but child cleanup remains unconfirmed.
Callers must retain any referenced resources when that fact is false. A root
that is still alive causes an explicit failure; restart cannot start another
backend until its owned process stops. A later stop attempt may retry.

Quit reports cleanup failures and still requests application exit. Its promise
settles without an unhandled rejection. This does not claim that a refused
termination succeeded. Normal successful shutdown stays on the graceful path.

Regressions cover graceful and forced success, live-process failure and retry,
exit races, listener cleanup, non-Windows uncertainty and quit failure handling.
The force-failure cases use synthetic owned-process adapters; they do not alter
OS permissions or kill a user's real backend.
