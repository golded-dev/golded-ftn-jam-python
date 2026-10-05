# golded-ftn-jam

This Python package reads and writes JAM revision 1 areas through golded-ftn models.
Use the .JDX index as authority. Preserve raw metadata on updates. Keep discovery,
databases and changes to core outside this package.

Validate binary bounds before decoding. Preserve filesystem errors and chain
parser failures with actual source paths and offsets. Decode strictly through
core helpers; mojibake repair belongs to callers. Reading requires a stable area.

Protect behavior with independent synthetic binary fixtures. Keep private message
archives out of tests. Run README checks and scripts/verify_distribution.py;
distribution dependencies must use the public core constraint, never a local path.

Edit this fragment or agent-compose.toml, then preview, build and check. Keep
private persona sources outside distributions. Commit, push, tags and publication
require an explicit request.

Strict reading stays the default. Archive mode requires an issue callback and
reports every recovery, skipped record and unsafe traversal stop. Keep source
paths, identities and byte offsets in issues; keep message contents out. Callback
failures propagate. Protect both modes with independent synthetic fixtures.

Writer operations lock byte 0 of .JHR through core locking and I/O helpers. Never
open or close another .JHR descriptor under that lock. Snapshot before mutation,
rollback in place, and poison the session if rollback fails. GoldED concurrency
stays disabled until read, write and refresh interoperability has been tested.
