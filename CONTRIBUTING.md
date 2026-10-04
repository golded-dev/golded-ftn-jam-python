# Contributing

Use Python 3.12+ and uv with the sibling `golded-ftn` checkout. Run the checks in
README before proposing a change. Keep runtime dependencies limited to core.

Build fixtures from the JAM specification, independently of reader helpers.
Use synthetic messages; keep private archives out of tests and distributions.
Verify changed behavior through `JamReader.read`, including failures and offsets.

Keep this package focused on reading JAM. Writers, discovery and databases need
separate design work. Generated AGENTS.md comes from agent-compose.toml and the
local project fragment; edit those sources and preview, build, check.
