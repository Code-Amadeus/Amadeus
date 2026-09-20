# C4 Acceptance / Recovery Tools

These tools are deployment and recovery safety tooling for the
Amadeus C4.1 baseline.

## pre_migration_backup.py

Creates and verifies a transactionally consistent SQLite backup
before a schema migration.

Safety properties:

- rejects missing databases
- rejects zero-byte databases
- requires PRAGMA integrity_check = ok
- uses SQLite online backup API
- verifies the produced backup
- records SHA-256 evidence
- does not modify the source database

## rollback_recovery_verify.py

Validates a genuine pre-C4 database backup and restores it into an
isolated recovery directory.

Safety properties:

- requires the expected pre-C4 schema
- rejects schema-4 substitution
- requires integrity_check = ok
- performs whole-database restore only
- does not downgrade schema
- verifies SHA-256 after restore
- does not overwrite an existing recovery database

## c3_recovery_runtime_probe.py

Opens a restored database under the actual C3 v1.1 runtime and
executes the real C3 MemoryRetriever path.

Safety properties:

- requires schema 3
- requires integrity_check = ok
- imports from the supplied C3 source tree
- executes real C3 ContinuityStore / MemoryRetriever APIs
- does not print memory content

## Acceptance rule

Passing these tooling tests proves that the backup and rollback
mechanism works.

It does NOT substitute for a genuine historical pre-C4 database
backup. Formal rollback PASS still requires recovery of the genuine
pre-C4 artifact.
## C5 preflight use

`pre_migration_backup.py` is intentionally reusable for the first real C4.1
schema-4 -> C5 schema-5 migration. Run it against the actual target C4.1 DB
before applying migration 5 and preserve the generated integrity/schema/count
and SHA-256 evidence with the C5 acceptance artifacts.

`rollback_recovery_verify.py` and `c3_recovery_runtime_probe.py` remain C4
historical-recovery tools and must not be repurposed to fabricate the missing
real pre-C4 production backup.
