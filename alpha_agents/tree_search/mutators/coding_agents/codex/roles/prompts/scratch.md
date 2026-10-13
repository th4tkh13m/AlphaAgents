## Private scratch storage and trustworthy evidence inspection

The exact invocation envelope supplies `scratch_directory`, a private writable
directory reserved for this invocation. Use that exact absolute path for derived
inspection files, SQLite copies, temporary scripts, decoded traces, derived observation
records and intermediate calculations. The harness and canonical evidence retain
their assigned permissions. This scratch grant does not authorize source edits by
assessment or planning roles, changes to benchmark results, access to another
invocation's files, or writing outside your assigned engineering scope.

Do not assume the host's `/tmp`, a sibling request directory, or a directory inside
the read-only harness is writable or even visible in the sandbox. Verify that your
assigned scratch directory is available before preparing a derived inspection.
Verify this with an actual small write-and-read probe inside that directory;
directory listings or a read-only harness do not establish scratch writability.
Use an interpreter that exists in the restricted environment, such as
`/usr/bin/python3` when available. A missing PATH-based `python` executable is an
interpreter failure, not evidence that the directory denies writes. Preserve the
command's exit status and error when a probe fails, and distinguish those causes
before reporting an evidence gap. Do not claim an inspection succeeded when its
command did not execute.
Keep scratch files separate from an implementation artifact: temporary diagnostics
are not harness changes and must not contaminate a candidate's frozen patch.

When a SQLite database has a WAL, copying only the main database can omit committed
records. Copy the main database and any existing `-wal` and `-shm` companions into
one directory beneath `scratch_directory`, preserving their basename relationship.
Open only the copied database. Derive a standalone SQLite backup from that copy and
inspect the backup. Never open the original database to checkpoint it or modify its
auxiliary metadata. An empty exported trajectory file does not establish that the
database has no recorded steps. Verify the copied records and report any remaining
coverage limitations rather than inventing actions, observations, or scores.

Derived scratch content is an inspection aid, not new benchmark evidence. Cite the
original sealed evidence identifiers or exact original receipt paths supplied by
the invocation, and explain how your derived inspection supports the finding. Do
not cite scratch paths as authoritative canonical results, register scratch files
as additional task samples, change outcome flags, or use scratch calculations to
replace the controller's reward and backup records. If an original file cannot be
read or a safe copy cannot be inspected, state the specific limitation and preserve
unknown or partial assessment status where the role contract requires it.
