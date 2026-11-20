# Work permission snapshot publication

Permission changes that arrive while a provider snapshot is being published mark
the projection dirty. The publisher emits the latest snapshot again, preserving
its existing throttling for ordinary updates and priority for permission or
terminal changes. Sequential requests therefore update the Work card with the
current request ID, pending count and revision.

The permission ledger and authorization rules remain unchanged. Regression
coverage blocks the first publication, then creates/resolves/expires later
requests and checks the emitted state rather than relying on timing alone.
