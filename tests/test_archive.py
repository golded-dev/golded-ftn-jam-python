from pathlib import Path

import pytest
from golded_ftn import ParserException, ReaderIssue, ReaderOptions
from test_reader import area, one, record, subfield

from golded_ftn_jam import JamReader


def options(issues: list[ReaderIssue]) -> ReaderOptions:
    return ReaderOptions(archive_mode=True, on_issue=issues.append)


@pytest.mark.parametrize(("lo", "size"), [(6, 101), (7, 43), (2000, 335)])
def test_archive_keeps_bounded_oversize_subfield(
    tmp_path: Path, lo: int, size: int
) -> None:
    base = one(tmp_path, subfield(lo, b"x" * size))
    with pytest.raises(ParserException):
        JamReader().read(base)
    issues: list[ReaderIssue] = []
    messages = list(JamReader().read(base, options(issues)))
    assert len(messages) == 1
    assert [(i.code, i.action, i.source_offset) for i in issues] == [
        ("subfield_length_exceeded", "recovered", 1100)
    ]
    if lo == 6:
        assert messages[0].subject == "x" * size


def test_archive_skips_bad_record_and_reads_next_index(tmp_path: Path) -> None:
    bad = bytearray(record(1))
    bad[:4] = b"BAD!"
    good = record(2, subfield(6, b"good"))
    base = area(tmp_path, bytes(bad) + good, entries=((0, 1024), (0, 1100)))
    issues: list[ReaderIssue] = []
    assert [m.msgno for m in JamReader().read(base, options(issues))] == [2]
    assert [(i.action, i.source_id, i.source_offset) for i in issues] == [
        ("skipped", "1", 1024)
    ]


def test_archive_ascii_fallback_retains_declared_control(tmp_path: Path) -> None:
    base = one(tmp_path, subfield(2000, b"CHRS: ASCII 1"), body=b"\x82")
    issues: list[ReaderIssue] = []
    message = list(JamReader().read(base, options(issues)))[0]
    assert message.body_text == "é"
    assert message.control_lines is not None
    assert message.control_lines.charset == "ASCII 1"
    assert [i.code for i in issues] == ["ascii_decode_fallback"]


def test_archive_preserves_malformed_timezone_metadata(tmp_path: Path) -> None:
    base = one(tmp_path, subfield(2004, b"01xx"))
    issues: list[ReaderIssue] = []
    message = list(JamReader().read(base, options(issues)))[0]
    assert message.control_lines is not None
    assert message.control_lines.kludges[0].value == "01xx"
    assert [i.code for i in issues] == ["invalid_tzutc"]


def test_archive_conflicting_ids_are_skipped_not_guessed(tmp_path: Path) -> None:
    base = one(tmp_path, subfield(4, b"one") + subfield(4, b"two"))
    issues: list[ReaderIssue] = []
    assert list(JamReader().read(base, options(issues))) == []
    assert issues[-1].action == "skipped"


def test_archive_reused_offset_stops_with_report(tmp_path: Path) -> None:
    base = area(tmp_path, record(1), entries=((0, 1024), (0, 1024)))
    issues: list[ReaderIssue] = []
    assert [m.msgno for m in JamReader().read(base, options(issues))] == [1]
    assert issues[-1].action == "stopped"


def test_reporter_failure_propagates(tmp_path: Path) -> None:
    base = one(tmp_path, subfield(7, b"x" * 41))

    def fail(issue: ReaderIssue) -> None:
        raise RuntimeError("report unavailable")

    with pytest.raises(RuntimeError, match="report unavailable"):
        JamReader().read(base, ReaderOptions(archive_mode=True, on_issue=fail))


def test_archive_preserves_distinct_pid_controls(tmp_path: Path) -> None:
    base = one(tmp_path, subfield(7, b"First tool") + subfield(7, b"Second tool"))
    with pytest.raises(ParserException):
        JamReader().read(base)
    issues: list[ReaderIssue] = []
    message = list(JamReader().read(base, options(issues)))[0]
    assert message.control_lines is not None
    assert [c.value for c in message.control_lines.kludges] == [
        "First tool",
        "Second tool",
    ]
    assert [i.code for i in issues] == ["repeated_control_subfield"]


def test_archive_ascii_fallback_accepts_core_alias(tmp_path: Path) -> None:
    base = one(tmp_path, subfield(2000, b"CHRS: ASCII 1"), body=b"\x82")
    issues: list[ReaderIssue] = []
    message = list(
        JamReader().read(
            base,
            ReaderOptions(
                archive_mode=True, on_issue=issues.append, fallback_charset="IBMPC"
            ),
        )
    )[0]
    assert message.body_text == "é"
    assert [i.code for i in issues] == ["ascii_decode_fallback"]
