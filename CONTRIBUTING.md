# Contributing

Use Python 3.12+ and uv with the sibling `golded-ftn` checkout. Run the checks in
README before proposing a change. Keep runtime dependencies limited to core.

Build fixtures from the JAM specification, independently of reader helpers.
Use synthetic messages; keep private archives out of tests and distributions.
Verify changed behavior through `JamReader.read`, including failures and offsets.

Keep this package focused on JAM reading and editing. Area discovery, databases,
packing and repair belong outside this package. Generated AGENTS.md comes from agent-compose.toml and the
local project fragment; edit those sources and preview, build, check.

Protect writer behavior through the public create/open/read/append/update/delete
API and independent raw records. Check omitted patch fields, explicit clearing,
controls in each supported physical placement, reply structures and unrelated
message revisions. Use controlled helper processes for lock conflicts and the
internal I/O seam for write, truncate, flush and rollback failures. Never reopen
the lock file while its operation lock is held. Preserve existing lastread data.

Keep GoldED closed during editing. A matching write lock alone does not prove
safe concurrent reads or refresh; build interoperability remains deferred.
