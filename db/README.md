# Persistent job registry

`JobStore` uses Python's SQLite driver and defaults to
`/app/config/dndeezer/jobs.sqlite3`. Mount `/app/config` on persistent storage;
the database must live outside the replaceable plugin installation directory.

## Lifecycle integration

1. Call `initialize()` before use.
2. Call `recover_interrupted()` once on startup, after old workers have stopped
   and before starting new ones. Never share recovery with another live instance.
3. Call `create(...)` before starting a download. Exact job names, backend IDs,
   and workspace paths cannot be reused while their records exist.
4. Call `update(...)` to persist state and completed file evidence from backend
   lifecycle callbacks. Resolve missing in-memory handles with `get(job_name)`.
5. Stop active work, validate ownership and filesystem confinement, and remove
   the workspace outside this package. Only call `mark_cleaned(job_name)` after
   verified removal. Failed removal must leave the record available for retry.
6. Retain cleaned records to identify repeated host cleanup calls. Optionally
   call `prune_cleaned(before=<Unix timestamp>)` when those retries are no longer
   expected. No automatic expiry is applied.
7. Run `maintain()` while idle for an integrity check and space reclamation.

Methods are synchronous. In plugin async code, use `run_blocking` (and
`functools.partial` for keyword arguments). Each operation opens and closes its
own connection and commits atomically. SQLite locking serializes writers.

Paths are checked lexically when stored; filesystem/symlink confinement must
still be checked at deletion time. Database backups should use SQLite's backup 
API.
