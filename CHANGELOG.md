# Changelog

## 1.3.0 — 2026-10-25

- Add recoverable sessions and explicit `extension_case` handling.
- Retain the JHR lock descriptor across reads and mutations; preserve raw
  metadata and revisions.
- Require `golded-ftn>=1.4.0,<2`.
- Add `JamWriter.recovery_session` implementing `RecoverableMessageBaseWriter`.
- Hold the `.JHR` byte-0 lock across prepare/apply/settle and reuse that descriptor for header I/O. Cover `.JHR`, `.JDT` and `.JDX`; never touch `.JLR`.
- Recover torn header or index artifacts before ordinary JAM validation.
- Invert DateWritten through TZUTC on writer and recovery reads so aware timestamps round-trip; keep the naive epoch mapping when TZUTC is absent.

## 1.2.1 — 2026-10-07

- Open JDT/JDX file descriptors in binary mode on Windows to preserve index bytes, text offsets and CTRL-Z data.
- Add literal-byte regressions for the Bob recipient CRC and LF/CRLF/CTRL-Z body data.

## 1.2.0 — 2026-10-05

- Add offline create/read/append/update/delete sessions, message revisions, record locks and rollback.
- Preserve raw subfields and maintain JAM indices, CRCs and counters.
- GoldED coexistence remains disabled pending build-specific integration tests.

- Replace controls and MSGID across JHR/JDT placements; preserve omitted structured fields and reject conflicting controls.

## 1.1.0 — Unreleased

Add reported archive reading: keep bounded oversized subfields and malformed
TZUTC metadata, retain distinct PID/FLAGS/TZUTC controls, try configured fallback
for mislabeled ASCII, and skip failed indexed records. Ambiguous structure stops
with a report. Strict reading remains the default.

## 1.0.0

- Read indexed JAM revision 1 areas with strict binary and decoding checks.
- Preserve message text, supported subfields, reply links and provenance.
- Ship typed public exports and independent synthetic fixtures.
- Report charset conflict offsets against stored subfield bytes.
- Keep development-only uv sources out of source distributions.
