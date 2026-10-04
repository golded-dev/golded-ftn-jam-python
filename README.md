# golded-ftn-jam

Read JAM revision 1 message areas through the `golded-ftn` models. Python 3.12+.

```sh
pip install golded-ftn-jam
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
extension variants are rejected. `.JLR` is unused.

The index determines message numbers and which headers are live. Index holes and
deleted messages are skipped. Old unindexed headers are ignored. The reader
validates signatures, revision, record boundaries, offsets, subfield lengths,
message numbers and reused header offsets. It reads the whole area into memory
and validates all indexed records before returning a tuple. A malformed later
record cannot leave a caller with a partial result. Filesystem errors remain
filesystem errors; damaged data and decoding errors raise `ParserException` with
source path, byte offset and a chained cause.

Read a stable area with no concurrent writes. This package does not lock the
area or produce a transactional snapshot across the three files. Stop the
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

Compressed, encrypted and escaped active messages are unsupported. There is no
writer, area discovery, database adapter or core modification here.

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

uv uses the sibling `../golded-ftn` checkout during development. Published metadata
contains only `golded-ftn>=1.0.0,<2`. The sdist build hook removes `tool.uv.sources`
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
