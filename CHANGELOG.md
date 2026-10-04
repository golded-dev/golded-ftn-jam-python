# Changelog

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
