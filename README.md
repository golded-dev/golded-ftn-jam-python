# golded-ftn-jam

Repository: [`golded-ftn-jam-python`](https://github.com/golded-dev/golded-ftn-jam-python).
The distribution remains `golded-ftn-jam`; imports use `golded_ftn_jam`.
The source is public on GitHub. [Version 1.2.1 is available on PyPI](https://pypi.org/project/golded-ftn-jam/1.2.1/).

Install with Python 3.12 or newer:

```sh
python -m pip install golded-ftn-jam==1.2.1
```

Read and edit JAM revision 1 areas through the `golded-ftn` models. Python 3.12+.

For development, clone core and the format package:

```sh
git clone https://github.com/golded-dev/golded-ftn-python.git
git clone https://github.com/golded-dev/golded-ftn-jam-python.git
cd golded-ftn-jam-python
uv sync --locked
```

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from golded_ftn import MessageBaseReader, ReaderOptions
from golded_ftn_jam import JamReader

# A minimal empty area. Real callers pass the area basename, without an extension.
with TemporaryDirectory() as directory:
    base = Path(directory) / "example"
    header = bytearray(1024)
    header[:4] = b"JAM\0"
    header[20:24] = (1).to_bytes(4, "little")
    base.with_suffix(".JHR").write_bytes(header)
    base.with_suffix(".JDT").write_bytes(b"")
    base.with_suffix(".JDX").write_bytes(b"")
    reader: MessageBaseReader = JamReader()
    messages = list(reader.read(base, ReaderOptions(fallback_charset="CP850")))
    assert messages == []
```

The basename is exact; `.JHR`, `.JDT` and `.JDX` extensions are matched without
regard to case. Each must identify one regular file; symlinks and ambiguous
extension variants are rejected. The standalone reader ignores `.JLR`; editing
preserves it and creation initializes an empty file.

The index determines message numbers and which headers are live. Index holes and
deleted messages are skipped. Old unindexed headers are ignored. The reader
validates signatures, revision, record boundaries, offsets, subfield lengths,
message numbers and reused header offsets. It reads the whole area into memory
and validates all indexed records before returning a tuple. A malformed later
record cannot leave a caller with a partial result. Filesystem errors remain
filesystem errors; damaged data and decoding errors raise `ParserException` with
source path, byte offset and a chained cause.

The standalone `JamReader` requires a stable area with no concurrent writes. It
does not lock the area or produce a snapshot across the three files. Stop the
writer or use a consistent copy before reading.

Decoding is strict, with core charset detection and CP850 fallback. Header
FTSKLUDGE declarations are considered before body declarations. Conflicting
charset declarations and IDs fail. Unknown charset names use the configured
fallback. Mojibake repair is the caller's choice.

`body_text` contains only normalized `.JDT` text. Header control and routing
subfields go into `control_lines`, ahead of body metadata. Repeated controls and
routing entries keep their order within the core model's kludges, seen_by and
path sequences. Unknown subfields and nonzero HiID fields are bounds-checked
and skipped. The first originating/destination address is preserved as decoded
text, including node 0, point and domain. Other single-value subfields must agree
when repeated. Header MSGID/REPLYID take precedence over matching body IDs;
missing MSGID uses the core synthetic ID over decoded, normalized fields.

Dates are naive `1970-01-01 + DateWritten seconds`, independent of the machine's
timezone; zero means `None`. TZUTC is retained as a control line. Raw attributes,
all three reply links and `.JHR` provenance are retained; zero reply links become
`None`. Unknown area metadata stays `None`.

Compressed, encrypted and escaped active messages are unsupported. Area
discovery, databases, packing and repair are outside this package.

## Development

```sh
uv sync --locked --python 3.14
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python -m mypy.stubtest golded_ftn_jam
uv build
uv run twine check dist/*
uv run python scripts/verify_distribution.py
```

uv uses the sibling `../golded-ftn-python` checkout during development. Published metadata
contains only `golded-ftn>=1.2.0,<2`. The sdist build hook removes `tool.uv.sources`
from the packed `pyproject.toml`; the development lock is also excluded.
Unpacked sources use the public dependency constraint.
See [CONTRIBUTING.md](CONTRIBUTING.md) and [release checks](docs/release.md).

## Format references and credits

- [JAM-001 revision 1](https://raw.githubusercontent.com/Mithgol/node-fidonet-jam/master/JAM.txt)
- [GoldED JAM structures](https://github.com/golded-dev/golded-linux-macos/blob/main/goldlib/gmb3/gmojamm.h)
- The sibling `laravel-ftn-jam/src/JamReader.php` and its reader tests informed
  model mapping. Its sequential scan and permissive truncation are not used.

JAM(mbp) - Copyright 1993 Joaquim Homrighausen, Andrew Milner, Mats Birch, Mats Wallin. ALL RIGHTS RESERVED.

The package code is MIT licensed; the JAM specification has its own terms.

## Archive mode

Strict reading remains the default. Archive mode requires a report callback:

```python
from golded_ftn import ReaderIssue, ReaderOptions

issues: list[ReaderIssue] = []
options = ReaderOptions(archive_mode=True, on_issue=issues.append)
# Pass options to JamReader().read(source, options).
```

Issues carry `recovered`, `skipped` or `stopped`, the actual filename, record
identity and physical offset. Their detail contains no message contents. A stop
means the traversal is incomplete; a validated prefix may still be returned.
Multiple issues can describe one record, including recovery followed by a skip.
Filesystem errors and callback exceptions propagate. Files must remain stable.

Bounded fields exceeding JAM's specification limits are retained and reported.
Malformed TZUTC metadata is retained without interpretation. Distinct PID, FLAGS
and TZUTC subfields stay in source order. Conflicting names, subjects, IDs or
charset declarations are skipped rather than guessed. Failed records are skipped
using the next fixed index slot; reused header offsets stop traversal.

If declared ASCII cannot decode a payload, the configured fallback is tried
strictly and reported. The original charset control stays unchanged. Other
decoding failures are skipped; there is no lossy decoding or mojibake repair.

## Writing

`JamWriter.create(base)` creates `.JHR`, `.JDT`, `.JDX` and an empty `.JLR`.
Existing files are refused. The initial message number is 1.

```python
from golded_ftn import MessagePatch, OutgoingMessage
from golded_ftn_jam import JamWriter

writer = JamWriter()
writer.create("new-area")
with writer.open("new-area") as session:
    result = session.append(
        OutgoingMessage(
            from_name="Alice",
            to_name="Bob",
            subject="Hello",
            body_text="Hello Bob",
        )
    )
    result = session.update(
        result.identity, MessagePatch(subject="Changed"), result.revision
    )
    session.delete(result.identity, result.revision)
```

Every operation rereads and validates the index, referenced headers, text ranges
and active-message count under the byte-0 `.JHR` record lock. A session read returns
`SessionMessage` with a raw-byte SHA-256 revision. Changes to other messages do not
invalidate it. The revision includes identity, physical index/header/text locations
and SHA-256 over raw header, subfield and text bytes. An omitted patch field
retains its raw metadata; explicit `None` clears optional metadata. Names, subject, text and attributes reject `None`.

`control_lines` replaces general controls in both header subfields and inline
text. MSGID, address controls and routing retain their separate fields when
omitted; conflicting explicit controls are rejected. `external_id` and routing
patches remove obsolete inline copies as well as replacing their header metadata.
Body-only changes retain existing inline controls and routing. Unknown subfields
and nonzero HiID values survive unless their supported field is explicitly changed.

Content updates append a header, subfields and text, then redirect the index.
Header-only changes retain the text bytes and record position. Unknown subfields,
reserved words, timestamps and reply links remain intact. Delete marks the header,
sets the recipient index CRC to FFFFFFFF, and decrements the active count. Message
numbers and lastread data remain unchanged.

CP850 is the default. Encoding is strict; conflicting charset declarations,
truncation, unsupported compression flags and arbitrary reply lists are refused.
No MSGID, routing or duplicate detection is generated. Full-file snapshots support
in-place rollback during ordinary I/O failures; rollback failure poisons the session.
This provides no process-kill or power-loss transaction guarantee.

GoldED coexistence is disabled on every platform (`concurrent=True` is refused).
macOS/Linux use POSIX record locking. Core provides Windows offline record locks
and I/O. CI passed on Windows and macOS with Python 3.12/3.14 and Linux
with Python 3.12/3.13/3.14. Version 1.2.1 opens JDT/JDX descriptors in binary
mode on Windows. GoldED interoperability remains unverified. Keep GoldED closed and
avoid direct file access while these sessions operate. GoldED builds and
integration tests are deferred.

### Original-source evidence

The reference checkout is `golded-open-source`, commit
`600266252b73174ff5116cee697ef9a97aeb1859`. It contains
`goldlib/gmb3/gmojamm.h` (`JamHdrInfo`,
`JamHdr`, `JamIndex`, subfield IDs), `gmojamm2.cpp` (`open_area` initialization,
`scan` indexing), and `gmojamm4.cpp` (`lock`, `unlock`, `save_message`).
`lock` takes byte 0 length 1 in `.JHR`; `save_message` maintains recipient/MSGID/
REPLY CRCs, active count and modification counter. Its CRC seed and missing final
complement are confirmed by `goldlib/gall/gcrcs32.cpp::strCrc32`.
These source checks establish layout and write semantics. They do not establish
that a running GoldED reader refreshes safely after external changes.
