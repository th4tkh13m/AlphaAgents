## Private scratch storage and trustworthy evidence inspection

### 1. Purpose and permission boundaries

The exact invocation envelope supplies `scratch_directory`: the absolute path
of a private writable directory reserved for this invocation. Use it for temporary
scripts, decoded traces, intermediate calculations, derived observation records,
and database copies when the fallback procedure below is necessary.

Scratch access does not change any other permission. The assigned harness,
canonical evidence, and shared code views retain their existing access rules.
Only the implementer may edit its separate writable harness workspace, within
its assigned intervention. Planning and assessment roles remain read-only with
respect to the harness. Do not write into canonical evidence, another invocation's
directory, or a shared code view. Shared views may be in concurrent use.

Keep temporary inspection files in scratch rather than in the harness. They must
not become part of a candidate's frozen patch or artifact. Scratch access does not
authorize changes to benchmark outcomes, measurements, or search statistics.

### 2. Establish scratch access before writing derived files

1. Read the exact `scratch_directory` string from the invocation envelope. Reuse
   that path; do not reconstruct it from a request ID or another directory.
2. Before an inspection that needs temporary files, perform a small write-and-read
   probe inside that directory. Remove the probe file after checking its contents.
   A directory listing or a readable harness does not establish write access.
3. Use a tool or interpreter actually available in the restricted environment,
   such as `/usr/bin/python3` when present. A missing PATH-based `python` command
   is an executable lookup failure, not evidence of a filesystem permission error.
4. If the probe fails, preserve the attempted path, operation, command exit status,
   and actual error. Correct command or path mistakes within the supplied scope.
   If access is genuinely unavailable, continue useful inspection that requires
   no temporary writes and report the resulting limitation.

Do not assume the host's `/tmp`, sibling request directories, or directories
inside a read-only harness are writable or visible. An access failure does not
authorize a broader permission profile or an alternate ungranted write location.

### 3. Choose the SQLite inspection path

Before copying any database, look for controller-supplied `sqlite_inspection`
entries in the evidence context or `.alpha_inspection_sqlite/index.json` within
the supplied evidence root. These describe standalone inspection snapshots
prepared before evidence sealing. Their source records and snapshot hashes are
bound to the sealed evidence inventory.

Choose the procedure for the database you need to inspect:

| Available evidence | Required action |
| --- | --- |
| A supplied inspection entry has `status=ready` | Read its standalone snapshot directly, following section 4. |
| A supplied entry has `status=unavailable` | Record its preparation error and use other permitted evidence. Follow section 6. |
| No sealed inspection snapshot is supplied for that database | Use the private scratch-copy fallback in section 5. |
| A supplied snapshot has an identity or integrity mismatch | Treat it as an evidence error. Follow section 6. |

The existence of an original database does not justify copying it when a usable
sealed inspection snapshot has already been supplied.

### 4. Read a sealed standalone snapshot directly

Use the ready entry's exact `read_uri` with SQLite URI handling enabled. That URI
specifies `mode=ro&immutable=1`. If locating a snapshot through the supplied index,
use its declared standalone file and inventory binding; do not guess a filename
or substitute a similarly named database from elsewhere.

Read the standalone snapshot in place. Do not copy it into scratch, create another
backup, checkpoint it, or modify its auxiliary metadata. Reuse the same snapshot
for subsequent queries in this invocation. Close database connections when done.

Immutable mode is appropriate for the sealed standalone inspection snapshot.
Never apply it to an original database with a live or unaccounted-for write-ahead
log (WAL): doing so can hide committed records that are absent from the main file.

### 5. Fallback for evidence without a sealed inspection snapshot

Use this procedure only when no sealed inspection snapshot is supplied for the
original database you need to inspect:

1. Create a dedicated directory beneath your exact `scratch_directory`.
2. Copy the original main database and any existing `-wal`, `-shm`, and `-journal`
   companions into that directory. Preserve their original basenames and sibling
   relationships. The WAL may contain committed records; a rollback journal may
   be required to recover a consistent view. Copying only the main database can
   produce incomplete or misleading observations.
3. Establish that the source files did not change during copying, using available
   file metadata or sealed inventory records. If a consistent copy cannot be
   established, report that limitation rather than treating a mixed copy as fact.
4. Open only the copied database, allowing any required recovery to affect only
   the private copies. Derive a standalone SQLite backup from that connection,
   then inspect the backup. Check that the derived database is readable and
   internally consistent before relying on its records.
5. Reuse that derived backup for further queries instead of making another copy
   for each inspection. Close all connections when finished.

Never open the original database to checkpoint it, recover its journal, change
its journal mode, or modify its auxiliary files. Do not use immutable mode to
bypass recovery on the original or on an unresolved DB/WAL/journal copy.

### 6. Handle unavailable or contradictory evidence

An entry with `status=unavailable` records a source-bound preparation error; it
does not provide a readable snapshot. Record the error and its effect on your
investigation. Do not repeatedly recreate the same failed backup without new
evidence. Use other supplied records where they can support the conclusion.

A snapshot identity or integrity mismatch is an evidence error. Do not repair the
original, silently substitute another database, or invent missing observations.
Distinguish an incorrect command, unavailable interpreter, permission denial,
inconsistent copy, and unreadable database when reporting the actual blocker.

An empty exported trajectory file does not establish that its source database
contains no recorded steps. Inspect the permitted database records when possible
and state any remaining coverage gap. Likewise, an inspection outage does not
change a canonical outcome into a failure or a success. Preserve unknown or
partial status where the role-specific contract requires it.

### 7. Cite authoritative evidence and explain derivations

Scratch content is an inspection aid, not a new benchmark observation. Cite the
original sealed evidence identifiers or exact original receipt paths supplied
by the invocation. Explain how a query, decoding step, or calculation supports
the finding, separating observed records from your interpretation.

Do not cite scratch paths as authoritative canonical results, register derived
files as additional task samples, change outcome flags, or replace the
controller's reward and backup records with your calculations. State the exact
source and coverage limitation whenever an inspection cannot be completed.

### 8. Scratch lifecycle and retention

Private scratch databases and their sidecars are disposable derived data. Once
a validated role has finished and its owned processes have exited, the controller
may evict them while retaining scripts, logs, responses, canonical evidence,
and retention receipts. A final result must remain understandable from its
authoritative references and explanation even after those copies are removed.

Leave shared snapshots, shared code views, canonical evidence, and retention
decisions to the controller. Keep all inspection writes within your own scratch
directory and all authorized implementation edits within the implementer's
separate writable workspace.
