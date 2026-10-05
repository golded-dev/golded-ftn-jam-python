"""Synthetic fixtures built with explicit specification offsets, not reader helpers."""

import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from golded_ftn import (
    MessageBaseReader,
    ParsedMessage,
    ParserException,
    ReaderOptions,
    synthetic_id,
)

import golded_ftn_jam
from golded_ftn_jam import JamReader


def u32(value: int) -> bytes:
    return value.to_bytes(4, "little")


def subfield(lo: int, value: bytes, hi: int = 0) -> bytes:
    return lo.to_bytes(2, "little") + hi.to_bytes(2, "little") + u32(len(value)) + value


def record(
    number: int = 1,
    fields: bytes = b"",
    text_offset: int = 0,
    text_length: int = 0,
    attributes: int = 0,
    date: int = 0,
    replies: tuple[int, int, int] = (0, 0, 0),
) -> bytes:
    header = bytearray(76)
    header[:4] = b"JAM\0"
    header[4:6] = (1).to_bytes(2, "little")
    for offset, value in {
        8: len(fields),
        24: replies[0],
        28: replies[1],
        32: replies[2],
        36: date,
        48: number,
        52: attributes,
        60: text_offset,
        64: text_length,
    }.items():
        header[offset : offset + 4] = u32(value)
    return bytes(header) + fields


def area(
    directory: Path,
    headers: bytes = b"",
    body: bytes = b"",
    entries: tuple[tuple[int, int], ...] = (),
    base_number: int = 1,
    endings: tuple[str, str, str] = (".JHR", ".JDT", ".JDX"),
) -> Path:
    base = directory / "area"
    info = bytearray(1024)
    info[:4] = b"JAM\0"
    info[20:24] = u32(base_number)
    for ending, data in zip(
        endings,
        (
            bytes(info) + headers,
            body,
            b"".join(u32(crc) + u32(offset) for crc, offset in entries),
        ),
        strict=True,
    ):
        Path(str(base) + ending).write_bytes(data)
    return base


def one(
    directory: Path,
    fields: bytes = b"",
    body: bytes = b"",
    attributes: int = 0,
    date: int = 0,
    replies: tuple[int, int, int] = (0, 0, 0),
) -> Path:
    return area(
        directory,
        record(
            fields=fields,
            text_length=len(body),
            attributes=attributes,
            date=date,
            replies=replies,
        ),
        body,
        ((0, 1024),),
    )


def read(base: Path, options: ReaderOptions | None = None) -> list[ParsedMessage]:
    reader: MessageBaseReader = JamReader()
    return list(reader.read(base, options))


def patch(base: Path, suffix: str, offset: int, data: bytes) -> None:
    path = Path(str(base) + suffix)
    original = path.read_bytes()
    path.write_bytes(original[:offset] + data + original[offset + len(data) :])


def failure(base: Path, suffix: str, offset: int) -> ParserException:
    with pytest.raises(ParserException) as caught:
        # The read itself must fail before an iterable can escape.
        JamReader().read(base)
    error = caught.value
    assert str(base) + suffix in str(error)
    assert f"offset {offset}" in str(error)
    assert isinstance(error.__cause__, (ValueError, LookupError))
    return error


def test_empty_and_public_exports(tmp_path: Path) -> None:
    assert golded_ftn_jam.__all__ == ["JamReader", "JamSession", "JamWriter"]
    assert read(area(tmp_path)) == []
    assert hasattr(golded_ftn_jam, "JamWriter")


def test_readme(tmp_path: Path) -> None:
    readme = Path(__file__).resolve().parents[1] / "README.md"
    source = readme.read_text().split("```python\n")[1].split("```", 1)[0]
    exec(compile(source, str(readme), "exec"), {})


def test_index_is_authority(tmp_path: Path) -> None:
    # Old, moved and unindexed headers physically precede/follow live headers.
    old = record(500, subfield(6, b"old"))
    second = record(502, subfield(6, b"second"), attributes=4)
    first = record(500, subfield(6, b"first"))
    deleted = record(503, attributes=0x80080000)
    tail = record(777, subfield(6, b"unindexed"))
    p_second = 1024 + len(old)
    p_first = p_second + len(second)
    p_deleted = p_first + len(first)
    base = area(
        tmp_path,
        old + second + first + deleted + tail,
        entries=(
            (0, p_first),
            (0xFFFFFFFF, 0xFFFFFFFF),
            (0, p_second),
            (0, p_deleted),
        ),
        base_number=500,
        endings=(".jHr", ".JdT", ".jdX"),
    )
    base.with_suffix(".JLR").write_bytes(b"unused garbage")
    messages = read(base)
    assert [(m.msgno, m.subject) for m in messages] == [(500, "first"), (502, "second")]
    assert messages[1].attributes_raw == 4
    assert messages[0].provenance is not None
    assert messages[0].provenance.source_path == str(base) + ".jHr"
    assert messages[0].provenance.source_offset == p_first
    assert messages[0].provenance.source_id == "500"
    assert messages[0].provenance.source_type == "jam"


def test_unindexed_garbage_is_ignored(tmp_path: Path) -> None:
    assert read(area(tmp_path, b"broken unindexed bytes")) == []


@pytest.mark.parametrize("suffix", [".JHR", ".JDT", ".JDX"])
def test_missing_files(tmp_path: Path, suffix: str) -> None:
    base = area(tmp_path)
    base.with_suffix(suffix).unlink()
    with pytest.raises(FileNotFoundError):
        read(base)


@pytest.mark.parametrize("suffix", [".JHR", ".JDT", ".JDX"])
def test_wrong_file_type(tmp_path: Path, suffix: str) -> None:
    base = area(tmp_path)
    base.with_suffix(suffix).unlink()
    base.with_suffix(suffix).mkdir()
    with pytest.raises(OSError):
        read(base)


def test_wrong_parent_and_extension(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read(tmp_path / "missing" / "area")
    plain = tmp_path / "file"
    plain.write_bytes(b"")
    with pytest.raises(NotADirectoryError):
        read(plain / "area")
    base = area(tmp_path)
    with pytest.raises(FileNotFoundError):
        read(base.with_suffix(".JHR"))


def test_symlink_rejected(tmp_path: Path) -> None:
    base = area(tmp_path)
    base.with_suffix(".JDT").unlink()
    target = tmp_path / "target"
    target.write_bytes(b"")
    try:
        base.with_suffix(".JDT").symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(OSError):
        read(base)


def test_ambiguous_endings(tmp_path: Path) -> None:
    base = area(tmp_path)
    lower = base.with_suffix(".jhr")
    if lower.exists():
        pytest.skip("Case-insensitive filesystem cannot hold extension variants")
    lower.write_bytes(base.with_suffix(".JHR").read_bytes())
    with pytest.raises(ParserException, match="Ambiguous") as caught:
        read(base)
    assert caught.value.__cause__ is not None


@pytest.mark.parametrize(
    ("suffix", "offset", "data", "error_offset"),
    [
        (".JHR", 0, b"BAD\0", 0),
        (".JHR", 20, u32(0), 20),
        (".JHR", 1024, b"BAD\0", 1024),
        (".JHR", 1028, b"\x02\x00", 1028),
        (".JHR", 1072, u32(2), 1072),
        (".JHR", 1032, u32(0xFFFFFFFF), 1100),
        (".JHR", 1084, u32(1), 1),
        (".JHR", 1088, u32(1), 0),
        (".JDX", 4, u32(100), 100),
        (".JDX", 4, u32(0xFFFFFFFF), 0xFFFFFFFF),
    ],
)
def test_corrupt_fields(
    tmp_path: Path, suffix: str, offset: int, data: bytes, error_offset: int
) -> None:
    base = one(tmp_path)
    patch(base, suffix, offset, data)
    failure(base, ".JDT" if offset in {1084, 1088} else ".JHR", error_offset)


@pytest.mark.parametrize(
    ("suffix", "length", "offset"),
    [
        (".JHR", 1023, 0),
        (".JHR", 1099, 1024),
        (".JDX", 7, 0),
    ],
)
def test_truncated_records(
    tmp_path: Path, suffix: str, length: int, offset: int
) -> None:
    base = one(tmp_path)
    path = base.with_suffix(suffix)
    path.write_bytes(path.read_bytes()[:length])
    failure(base, suffix, offset)


def test_index_overflow(tmp_path: Path) -> None:
    base = area(
        tmp_path, entries=((0xFFFFFFFF, 0xFFFFFFFF),) * 2, base_number=0xFFFFFFFF
    )
    failure(base, ".JDX", 0)


def test_reused_offset(tmp_path: Path) -> None:
    base = area(tmp_path, record(), entries=((0, 1024), (0, 1024)))
    failure(base, ".JDX", 12)


def test_no_partial_result(tmp_path: Path) -> None:
    base = area(tmp_path, record() + b"broken", entries=((0, 1024), (0, 1100)))
    failure(base, ".JHR", 1100)


@pytest.mark.parametrize(
    "fields", [b"\x00", b"\x00" * 7, b"\x02\x00\x00\x00" + u32(100) + b"short"]
)
def test_subfield_overflow(tmp_path: Path, fields: bytes) -> None:
    failure(one(tmp_path, fields), ".JHR", 1100)


def test_unknown_fields_checked_but_not_decoded(tmp_path: Path) -> None:
    fields = (
        subfield(999, b"\xff")
        + subfield(2, b"\xff", 1)
        + subfield(2000, b"CHRS: UTF-8 4")
        + subfield(2, b"Known")
    )
    assert read(one(tmp_path, fields))[0].from_name == "Known"
    # Unknown fields still cannot exceed the declared subfield block.
    base = one(tmp_path, subfield(999, b"x"))
    patch(base, ".JHR", 1104, u32(2))
    failure(base, ".JHR", 1100)


@pytest.mark.parametrize("attributes", [0x80000, 0x100000, 0x200000])
def test_unsupported_active(tmp_path: Path, attributes: int) -> None:
    failure(one(tmp_path, attributes=attributes), ".JHR", 1076)


@pytest.mark.parametrize("encoding", ["CP850", "UTF-8"])
def test_text_metadata_and_provenance(tmp_path: Path, encoding: str) -> None:
    fields = b"".join(
        subfield(lo, value.encode(encoding))
        for lo, value in [
            (2, "Søren"),
            (3, "Ægir"),
            (6, "Blåbær"),
            (0, "2:230/0.7@fidonet"),
            (0, "3:4/5"),
            (1, "2:230/0@other"),
            (4, "2:230/0 abc"),
            (5, "parent"),
            (7, "GoldED"),
            (2000, f"CHRS: {encoding} 4"),
            (2000, "X-CUSTOM: first"),
            (2001, "230/0 1"),
            (2001, "230/2"),
            (2002, "230/0"),
            (2002, "230/3"),
            (2003, "DIR"),
            (2004, "+0200"),
            (2000, "X-CUSTOM: second"),
        ]
    )
    body = (
        "Héj\r\nverden\r\x01MSGID: 2:230/0 abc\r\x01REPLY: parent\r"
        "SEEN-BY: 230/4\rPATH: 230/5\x00"
    )
    message = read(
        one(
            tmp_path,
            fields,
            body.encode(encoding),
            attributes=4,
            date=1,
            replies=(2, 3, 4),
        )
    )[0]
    assert (message.from_name, message.to_name, message.subject) == (
        "Søren",
        "Ægir",
        "Blåbær",
    )
    assert message.body_text == body.rstrip("\x00").replace("\r\n", "\n").replace(
        "\r", "\n"
    )
    assert "CHRS" not in message.body_text
    assert message.from_address == "2:230/0.7@fidonet"
    assert message.to_address == "2:230/0@other"
    assert message.external_id == "2:230/0 abc"
    assert (
        message.reply_to_msgno,
        message.reply1st_msgno,
        message.reply_next_msgno,
    ) == (2, 3, 4)
    assert message.posted_at == datetime(1970, 1, 1, 0, 0, 1)
    controls = message.control_lines
    assert controls is not None
    assert controls.msgid == message.external_id
    assert controls.reply == "parent"
    assert controls.seen_by == ("230/0 1", "230/2", "230/4")
    assert controls.path == ("230/0", "230/3", "230/5")
    assert [(k.name, k.value) for k in controls.kludges] == [
        ("MSGID", "2:230/0 abc"),
        ("REPLY", "parent"),
        ("PID", "GoldED"),
        ("CHRS", f"{encoding} 4"),
        ("X-CUSTOM", "first"),
        ("FLAGS", "DIR"),
        ("TZUTC", "+0200"),
        ("X-CUSTOM", "second"),
        ("MSGID", "2:230/0 abc"),
        ("REPLY", "parent"),
    ]
    assert message.area_code is None
    assert message.area_name is None
    assert message.area_sort_order is None
    assert message.area_meta_key is None


def test_defaults_and_synthetic_id(tmp_path: Path) -> None:
    message = read(one(tmp_path, subfield(2, "Æ".encode("CP850")), b"a\rb\x00"))[0]
    assert message.external_id == synthetic_id("Æ", "", "", None, "a\nb")
    assert message.posted_at is None
    assert message.from_address is message.to_address is None
    assert (
        message.reply_to_msgno
        is message.reply1st_msgno
        is message.reply_next_msgno
        is None
    )


@pytest.mark.parametrize(
    ("fields", "body", "expected"),
    [
        (subfield(4, b"header"), b"", "header"),
        (b"", b"\x01MSGID: body\r", "body"),
        (subfield(4, b"same"), b"\x01MSGID: same\r", "same"),
    ],
)
def test_id_priority(tmp_path: Path, fields: bytes, body: bytes, expected: str) -> None:
    assert read(one(tmp_path, fields, body))[0].external_id == expected


@pytest.mark.parametrize("lo", [2, 3, 4, 5, 6, 7, 2003])
def test_single_field_duplicates(tmp_path: Path, lo: int) -> None:
    fields = subfield(lo, b"same") * 2
    assert len(read(one(tmp_path, fields))) == 1
    base = one(tmp_path, subfield(lo, b"first") + subfield(lo, b"second"))
    failure(base, ".JHR", 1100 + 8 + 5 + 8)


@pytest.mark.parametrize(
    ("fields", "body", "suffix"),
    [
        (subfield(4, b"header"), b"\x01MSGID: body", ".JDT"),
        (subfield(5, b"header"), b"\x01REPLY: body", ".JDT"),
        (b"", b"\x01MSGID: a\r\x01MSGID: b", ".JDT"),
        (b"", b"\x01REPLY: a\r\x01REPLY: b", ".JDT"),
        (subfield(4, b"a") + subfield(2000, b"MSGID: b"), b"", ".JHR"),
    ],
)
def test_conflicting_ids(
    tmp_path: Path, fields: bytes, body: bytes, suffix: str
) -> None:
    failure(one(tmp_path, fields, body), suffix, 0 if suffix == ".JDT" else 1100)


@pytest.mark.parametrize(
    ("fields", "body", "suffix", "offset"),
    [
        (subfield(2000, b"CHRS: UTF-8 4"), b"\x01CHRS: CP850 2", ".JDT", 0),
        (
            subfield(2000, b"CHRS: UTF-8 4") + subfield(2000, b"CHARSET: CP850"),
            b"",
            ".JHR",
            1129,
        ),
        (b"", b"\x01CHRS: UTF-8 4\r\x01CHARSET: CP850", ".JDT", 15),
        (subfield(2000, b"CHRS: UNKNOWN"), b"\x01CHRS: DIFFERENT", ".JDT", 0),
    ],
)
def test_conflicting_charsets(
    tmp_path: Path, fields: bytes, body: bytes, suffix: str, offset: int
) -> None:
    failure(one(tmp_path, fields, body), suffix, offset)


def test_charset_aliases_and_unknown_fallback(tmp_path: Path) -> None:
    fields = subfield(2000, b"CHRS: IBM850 2") + subfield(2000, b"CHARSET: CP850")
    assert read(one(tmp_path, fields, "Æ".encode("CP850")))[0].body_text == "Æ"
    fields = subfield(2000, b"CHRS: UNKNOWN 2")
    assert (
        read(
            one(tmp_path, fields, "Æ".encode()),
            ReaderOptions(fallback_charset="UTF-8"),
        )[0].body_text
        == "Æ"
    )


def test_body_charset_and_fallback(tmp_path: Path) -> None:
    body = "\x01CHRS: UTF-8 4\rBlå".encode()
    assert read(one(tmp_path, body=body))[0].body_text.endswith("Blå")
    assert (
        read(
            one(tmp_path, body="Blå".encode()), ReaderOptions(fallback_charset="UTF-8")
        )[0].body_text
        == "Blå"
    )


@pytest.mark.parametrize(
    ("fields", "body", "suffix", "offset"),
    [
        (subfield(2000, b"CHRS: UTF-8 4"), b"\xff", ".JDT", 0),
        (subfield(2, b"\xff") + subfield(2000, b"CHRS: UTF-8 4"), b"", ".JHR", 1108),
    ],
)
def test_decode_errors(
    tmp_path: Path, fields: bytes, body: bytes, suffix: str, offset: int
) -> None:
    assert isinstance(
        failure(one(tmp_path, fields, body), suffix, offset).__cause__,
        UnicodeDecodeError,
    )


def test_invalid_fallback(tmp_path: Path) -> None:
    with pytest.raises(ParserException) as caught:
        read(one(tmp_path), ReaderOptions(fallback_charset="not-a-codec"))
    assert isinstance(caught.value.__cause__, LookupError)


def test_no_mojibake_repair(tmp_path: Path) -> None:
    text = "BlÃ¥"
    fields = subfield(2000, b"CHRS: UTF-8 4")
    assert read(one(tmp_path, fields, text.encode()))[0].body_text == text


def test_timezone_independent_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = one(tmp_path, date=0xFFFFFFFF)
    tzset: Callable[[], None] | None = getattr(time, "tzset", None)
    original = os.environ.get("TZ")
    try:
        for zone in ["UTC0", "EST5EDT", "JST-9"]:
            monkeypatch.setenv("TZ", zone)
            if tzset is not None:
                tzset()
            assert read(base)[0].posted_at == datetime(2106, 2, 7, 6, 28, 15)
    finally:
        if original is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original)
        if tzset is not None:
            tzset()


def test_ambiguous_endings_independent_of_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "area"

    def candidates(_path: Path) -> list[Path]:
        return [base.with_suffix(".JHR"), base.with_suffix(".jhr")]

    monkeypatch.setattr(Path, "iterdir", candidates)
    with pytest.raises(ParserException, match="Ambiguous") as caught:
        read(base)
    assert str(base.with_suffix(".JHR")) in str(caught.value)
    assert caught.value.__cause__ is not None


@pytest.mark.parametrize(("lo", "size"), [(0, 101), (2, 101), (7, 41), (2000, 256)])
def test_specification_field_limits(tmp_path: Path, lo: int, size: int) -> None:
    failure(one(tmp_path, subfield(lo, b"x" * size)), ".JHR", 1100)


@pytest.mark.parametrize("value", [b"", b"020", b"+02000", b"abcd"])
def test_invalid_tzutc(tmp_path: Path, value: bytes) -> None:
    failure(one(tmp_path, subfield(2004, value)), ".JHR", 1108)


def test_tzutc_duplicates(tmp_path: Path) -> None:
    assert len(read(one(tmp_path, subfield(2004, b"0200") * 2))) == 1
    base = one(tmp_path, subfield(2004, b"0200") + subfield(2004, b"0300"))
    failure(base, ".JHR", 1120)


def test_nonzero_text_offset(tmp_path: Path) -> None:
    base = area(
        tmp_path,
        record(text_offset=7, text_length=4),
        b"ignoredbodytail",
        ((0xFFFFFFFF, 1024),),
    )
    assert read(base)[0].body_text == "body"
    patch(base, ".JHR", 1084, u32(15))
    patch(base, ".JHR", 1088, u32(0))
    assert read(base)[0].body_text == ""
    patch(base, ".JHR", 1084, u32(16))
    failure(base, ".JDT", 16)


def test_unsupported_hiid_still_checked(tmp_path: Path) -> None:
    base = one(tmp_path, subfield(2, b"x", 1))
    patch(base, ".JHR", 1104, u32(2))
    failure(base, ".JHR", 1100)


def test_deleted_records_still_have_checked_bounds(tmp_path: Path) -> None:
    base = one(tmp_path, attributes=0x80000000)
    patch(base, ".JHR", 1088, u32(1))
    failure(base, ".JDT", 0)


@pytest.mark.parametrize("prefix", [b"", b"\x01", b"\x01\x01"])
def test_charset_conflict_offset_uses_stored_bytes(
    tmp_path: Path, prefix: bytes
) -> None:
    payload = prefix + b"CHRS: UTF-8 4\x01CHARSET: CP850"
    base = one(tmp_path, subfield(2000, payload))
    failure(base, ".JHR", 1108 + payload.index(b"\x01CHARSET"))


def test_charset_conflict_offset_with_leading_soh(tmp_path: Path) -> None:
    first = subfield(2000, b"CHRS: UTF-8 4")
    second = subfield(2000, b"\x01\x01CHARSET: CP850")
    failure(one(tmp_path, first + second), ".JHR", 1108 + len(first) + 1)
