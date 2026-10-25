# F-JAM Handoff: `golded-ftn-jam` `1.3.0`

## Scope
- **Worktree only:** `/Users/odinn/Projects/golded-dev/ftn-node-work/worktrees/golded-ftn-jam-python-f1/`
- **Main checkouts untouched:** `golded-ftn-jam-python/` and `golded-ftn-python/` were not modified.
- **No git commit or publication.**
- **Engine:** editable `golded-ftn==1.3.0` from `../golded-ftn-python-r1`.
- **Contracts:** recovery-v1 §2/§3/§4 F-JAM, k1.md JAM date/TZUTC inverse, C0-04/C0-06.
- **Reviews:** `f-jam-claude-review.txt` blockers B1–B4 plus coordinator D1/D2.

## Version
`golded-ftn-jam` **1.3.0** with `dependencies = ["golded-ftn>=1.3.0,<2"]`.

## What landed
1. `JamWriter.recovery_session(path, *, recovery_root, options=None)` implements `RecoverableMessageBaseWriter`.
2. Physical base: `format="jam"`; `canonical_key` is the realpath of the locked `.jhr`; `lock_inode` is `(st_dev, st_ino)` of that descriptor.
3. Lock is `<base>.jhr` byte 0 via `locks.acquire`. The held `.jhr` descriptor is `FileMutationPlan.fd` and is reused for header reads, snapshots, writes, and restores. Plans always cover `.jhr`, `.jdt`, and `.jdx`. `.jlr` is never opened or written.
4. Session open does not run ordinary JAM validation. `pending()` / `recover()` can restore a torn header or index first. `read` / `prepare_*` validate after that.
5. Existing `JamWriter` / `JamSession` CRUD still uses per-operation locks. The recovery session never re-enters those public lock methods. `concurrent=True` is still rejected.
6. Date adapter: naive `posted_at` plus a valid `TZUTC` stores the UTC epoch and inverts to the same wall time on writer and recovery reads. Aware `posted_at` is accepted only when `utcoffset()` is not `None` and matches TZUTC; otherwise a fixed `ValueError`. A `control_lines` patch that changes or removes TZUTC without `posted_at` recomputes `DateWritten` from the old wall time so the clock does not shift. TZUTC hours follow the packet range (`MM` 00–59, magnitude `< 24*60`). Non-ASCII 2004 is unparseable (`None`), not a Unicode leak. Standalone `JamReader` keeps the existing naive-epoch contract (C0-06).
7. Flag and reply mapping stay format-owned JAM Attribute / three-link fields. Packet `attributes_raw` is not copied as packet bits.
8. Recovery root `0o700` and artifacts `0o600` are enforced by the R1 engine.
9. Recovery open derives `base_root` from the parent directory realpath only. `.jhr` / `.jdt` / `.jdx` are rejected when `lstat` says symlink (`RecoveryError(code="unsafe_recovery_path")`). `.jdt` / `.jdx` open with `O_NOFOLLOW`. After the lock, each fd `fstat` must match `lstat` of the same rel path.
10. `prepare_*` calls `check_usable()`, `require_binding()`, and active-mutation / pending checks before snapshotting. `OSError` / `UnicodeEncodeError` / `UnicodeDecodeError` at the recovery-session / prepare / apply boundary become `RecoveryError` or a fixed `ValueError` raised `from None`, with no path or payload in `str(error)`.

## Files changed
- `src/golded_ftn_jam/writer.py`
- `tests/test_recovery.py`
- `evidence/handoff.md`

## Verification
- `.venv/bin/pytest -q`: **144 passed, 1 skipped** (145 collected; skip is the existing case-insensitive filesystem check)
- `.venv/bin/ruff check src tests`: all checks passed
- `.venv/bin/ruff format --check src tests`: 7 files already formatted
- `.venv/bin/mypy --strict src tests scripts`: Success: no issues found in 9 source files

## Remaining gaps
- Standalone `JamReader` still returns naive `1970-01-01 + DateWritten` even when `TZUTC` is present. Inverse conversion is the writer/recovery adapter, not a silent reader contract change (D5).
- Write still takes TZUTC from controls/subfields; read takes it from kludges. A body-only `^ATZUTC` can still shift on recovery read (D3).
- `apply` durability still depends on whether `IO.flush` fsyncs (D7). Same-process POSIX `fcntl` lock drop if another path opens and closes `.jhr` is unguarded (D8).
- Append `WriteResult.revision` framing may still disagree with `read()` (D9).
- `concurrent=True` remains rejected; GoldED lock/cache coexistence is still unqualified (C0-04).
- Crash-injection is limited to the required torn-header recover case; no full kill-barrier matrix.
- Confidentiality coverage is the missing-`.JDT` sentinel case, not the full unencodable-subject / corrupt-base / failed-apply-directory matrix.
- `__init__.py` still does not export `JamRecoverableSession`.
- `scripts/verify_distribution.py` was previously updated to `golded-ftn>=1.3.0,<2` but a wheel/sdist build was not run.
