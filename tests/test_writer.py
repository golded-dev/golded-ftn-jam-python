"""Mutation checks against independently constructed binary records."""

import struct
import subprocess
import sys
from pathlib import Path

import pytest
from golded_ftn import (
    ConflictError,
    ControlLine,
    MessagePatch,
    OutgoingMessage,
    ParserException,
    RollbackError,
    UnsupportedOperationError,
    WriterError,
    WriterOptions,
)
from golded_ftn._writer_io import IO
from test_reader import one, subfield

from golded_ftn_jam import JamWriter


def message(body: str = "text") -> OutgoingMessage:
    return OutgoingMessage(
        from_name="sender",
        to_name="recipient",
        subject="subject",
        body_text=body,
        external_id="2:1/2 id",
        reply_to_msgno=7,
    )


def snapshot(base: Path) -> tuple[bytes, ...]:
    return tuple(
        Path(str(base) + suffix).read_bytes()
        for suffix in (".JHR", ".JDT", ".JDX", ".JLR")
    )


def test_create_append_update_delete(tmp_path: Path) -> None:
    base = tmp_path / "area"
    writer = JamWriter()
    writer.create(base)
    raw = snapshot(base)
    assert raw[0][:4] == b"JAM\0" and len(raw[0]) == 1024
    assert struct.unpack_from("<III", raw[0], 12) == (0, 0xFFFFFFFF, 1)
    assert raw[1:] == (b"", b"", b"")
    with pytest.raises(FileExistsError):
        writer.create(base)
    with writer.open(base) as session:
        first = session.append(message("one\ntwo"))
        second = session.append(message())
        assert first.identity.msgno == 1 and second.identity.msgno == 2
        read = session.read(1)
        assert read.message.body_text == "one\ntwo"
        assert read.revision == first.revision
        assert read.message.reply_to_msgno == 7
        changed = session.update(
            first.identity, MessagePatch(body_text="x"), first.revision
        )
        assert changed.revision.location[1] > first.revision.location[1]
        assert session.read(1).message.body_text == "x"
        with pytest.raises(ConflictError):
            session.delete(first.identity, first.revision)
        session.delete(first.identity, changed.revision)
        with pytest.raises(ConflictError):
            session.read(1)
        assert session.read(2).revision == second.revision
        third = session.append(message())
        assert third.identity.msgno == 3
    info = snapshot(base)[0]
    assert struct.unpack_from("<II", info, 8) == (5, 2)


def test_independent_unknown_metadata_and_header_preservation(tmp_path: Path) -> None:
    unknown = subfield(4321, b"\x00\xffprivate", hi=12)
    base = one(
        tmp_path,
        fields=subfield(2, b"old") + unknown,
        body=b"old\rtext",
        replies=(19, 20, 21),
    )
    path = Path(str(base) + ".JHR")
    raw = bytearray(path.read_bytes())
    raw[12:16] = (1).to_bytes(4, "little")
    raw[1030:1032] = b"\xaa\xbb"  # Reserved header word.
    raw[1064:1068] = (12345).to_bytes(4, "little")  # DateReceived.
    path.write_bytes(raw)
    Path(str(base) + ".JLR").write_bytes(b"lastread preserved")
    with JamWriter().open(base) as session:
        read = session.read(1)
        changed = session.update(
            read.identity, MessagePatch(subject="new"), read.revision
        )
        offset = changed.revision.location[1]
        after = path.read_bytes()
        assert unknown in after[offset:]
        assert after[offset + 6 : offset + 8] == b"\xaa\xbb"
        assert after[offset + 40 : offset + 44] == (12345).to_bytes(4, "little")
        assert session.read(1).message.reply_next_msgno == 21
        original_text = Path(str(base) + ".JDT").read_bytes()
        attr = session.update(
            changed.identity, MessagePatch(attributes_raw=0x04000000), changed.revision
        )
        assert attr.revision.location == changed.revision.location
        assert Path(str(base) + ".JDT").read_bytes() == original_text
        assert Path(str(base) + ".JLR").read_bytes() == b"lastread preserved"


@pytest.mark.parametrize("step", range(1, 8))
def test_append_failure_rollback(tmp_path: Path, step: int) -> None:
    base = tmp_path / "area"
    writer = JamWriter()
    writer.create(base)

    class FailingIO(IO):
        count = 0

        def fail(self) -> None:
            self.count += 1
            if self.count == step:
                raise OSError("injected")

        def write(self, fd: int, offset: int, data: bytes) -> None:
            self.fail()
            super().write(fd, offset, data)

        def flush(self, fd: int) -> None:
            self.fail()
            super().flush(fd)

    with writer.open(base) as session:
        completed = session.append(message("completed"))
        before = snapshot(base)
        session._io = FailingIO()
        with pytest.raises(OSError, match="injected"):
            session.append(message())
        assert snapshot(base) == before
        assert session.read(1).revision == completed.revision
        assert session.append(message()).identity.msgno == 2


def test_rollback_failure_poisons_session(tmp_path: Path) -> None:
    base = tmp_path / "area"
    writer = JamWriter()
    writer.create(base)

    class BrokenIO(IO):
        def write(self, fd: int, offset: int, data: bytes) -> None:
            raise OSError("broken")

    with writer.open(base) as session:
        session._io = BrokenIO()
        with pytest.raises(RollbackError, match="append.*rollback"):
            session.append(message())
        with pytest.raises(WriterError, match="unusable"):
            session.read(1)


def test_validation_and_sessions(tmp_path: Path) -> None:
    base = tmp_path / "area"
    writer = JamWriter()
    writer.create(base)
    with pytest.raises(UnsupportedOperationError):
        writer.open(base, WriterOptions(concurrent=True))
    with writer.open(base) as left, writer.open(base) as right:
        first = left.append(message())
        second = right.append(message())
        assert left.read(1).revision == first.revision
        right.update(second.identity, MessagePatch(subject="changed"), second.revision)
        left.update(first.identity, MessagePatch(subject="still valid"), first.revision)
        before = snapshot(base)
        with pytest.raises(UnicodeEncodeError):
            left.append(message("😀"))
        with pytest.raises(ValueError):
            left.append(
                OutgoingMessage(
                    from_name="a",
                    to_name="b",
                    subject="c",
                    body_text="",
                    control_lines=(ControlLine(name="CHRS", value="UTF-8 4", raw=""),),
                )
            )
        assert snapshot(base) == before
    with pytest.raises(WriterError):
        left.read(1)
    damaged = bytearray(Path(str(base) + ".JHR").read_bytes())
    damaged[12:16] = (999).to_bytes(4, "little")
    Path(str(base) + ".JHR").write_bytes(damaged)
    with writer.open(base) as session, pytest.raises(ParserException):
        session.append(message())


def test_deterministic_process_lock_timeout(tmp_path: Path) -> None:
    import subprocess
    import sys

    from golded_ftn import LockTimeoutError

    base = tmp_path / "area"
    writer = JamWriter()
    writer.create(base)
    helper = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sys\nf=open(sys.argv[1],'r+b')\n"
            "if os.name == 'nt':\n"
            " import msvcrt\n msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)\n"
            "else:\n"
            " import fcntl\n fcntl.lockf(f,fcntl.LOCK_EX,1,0)\n"
            "print('locked',flush=True)\nsys.stdin.readline()",
            str(base) + ".JHR",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert helper.stdout is not None
        assert helper.stdout.readline() == "locked\n"
        with writer.open(base, WriterOptions(lock_timeout=0.02)) as session:
            with pytest.raises(LockTimeoutError):
                session.append(message())
    finally:
        helper.communicate("release\n", timeout=5)
    with writer.open(base) as session:
        assert session.append(message()).identity.msgno == 1


@pytest.mark.parametrize(
    "operation,step",
    [("update", step) for step in range(1, 8)]
    + [("delete", step) for step in range(1, 7)],
)
def test_update_delete_failures(tmp_path: Path, operation: str, step: int) -> None:
    base = tmp_path / "area"
    writer = JamWriter()
    writer.create(base)

    class FailingIO(IO):
        count = 0

        def fail(self) -> None:
            self.count += 1
            if self.count == step:
                raise OSError("injected")

        def write(self, fd: int, offset: int, data: bytes) -> None:
            self.fail()
            super().write(fd, offset, data)

        def flush(self, fd: int) -> None:
            self.fail()
            super().flush(fd)

    with writer.open(base) as session:
        first = session.append(message())
        before = snapshot(base)
        session._io = FailingIO()
        with pytest.raises(OSError, match="injected"):
            if operation == "update":
                session.update(
                    first.identity,
                    MessagePatch(body_text="longer changed text"),
                    first.revision,
                )
            else:
                session.delete(first.identity, first.revision)
        assert snapshot(base) == before
        assert session.read(1).revision == first.revision


@pytest.mark.parametrize(
    "field,value", [("from_name", "a\nb"), ("to_name", "a\rb"), ("subject", "a\x01b")]
)
def test_metadata_separators_rejected(tmp_path: Path, field: str, value: str) -> None:
    from dataclasses import replace

    base = tmp_path / "area"
    writer = JamWriter()
    writer.create(base)
    with writer.open(base) as session:
        before = snapshot(base)
        with pytest.raises(ValueError):
            bad = (
                replace(message(), from_name=value)
                if field == "from_name"
                else replace(message(), to_name=value)
                if field == "to_name"
                else replace(message(), subject=value)
            )
            session.append(bad)
        assert snapshot(base) == before


def test_control_and_address_validation(tmp_path: Path) -> None:
    from dataclasses import replace

    from golded_ftn import FtnAddress

    base = tmp_path / "area"
    writer = JamWriter()
    writer.create(base)
    with writer.open(base) as session:
        before = snapshot(base)
        for bad in (
            replace(message(), from_address=FtnAddress(zone=65536, net=1, node=2)),
            replace(
                message(),
                control_lines=(ControlLine(name="bad:name", value="x", raw=""),),
            ),
            replace(message(), routing_path=("1/2\n3/4",)),
        ):
            with pytest.raises(ValueError):
                session.append(bad)
        assert snapshot(base) == before


def test_forced_exit_after_text_write_leaves_unindexed_bytes(tmp_path: Path) -> None:
    from golded_ftn_jam import JamReader

    base = tmp_path / "area"
    writer = JamWriter()
    writer.create(base)
    with writer.open(base) as session:
        session.append(message())
    before = snapshot(base)
    program = """
import os, sys
from golded_ftn import OutgoingMessage
from golded_ftn._writer_io import IO
from golded_ftn_jam import JamWriter
class ExitIO(IO):
    def write(self, fd, offset, data):
        super().write(fd, offset, data)
        os._exit(23)
with JamWriter().open(sys.argv[1]) as session:
    session._io = ExitIO()
    session.append(OutgoingMessage(
        from_name='Alice', to_name='Bob', subject='Demo', body_text='newtext'))
"""
    child = subprocess.run(
        [sys.executable, "-c", program, str(base)], timeout=5, check=False
    )
    assert child.returncode == 23
    after = snapshot(base)
    for index in (0, 2, 3):
        assert after[index] == before[index]
    assert after[1] == before[1] + b"newtext"
    assert [item.msgno for item in JamReader().read(base)] == [1]
    with writer.open(base) as session:
        assert session.read(1).message.subject == message().subject


@pytest.mark.parametrize("placement", ("header", "body", "both"))
def test_control_patch_handles_independent_record_placements(
    tmp_path: Path, placement: str
) -> None:
    fields = subfield(2, b"Sender") + subfield(3, b"Reader")
    controls = (
        b"\x01MSGID: 2:230/1 old\r\x01PID: old\r\x01FMPT: 7\r"
        b"SEEN-BY: 230/1\r\x01PATH: 230/2\r"
    )
    if placement in ("header", "both"):
        fields += (
            subfield(4, b"2:230/1 old")
            + subfield(7, b"old")
            + subfield(2000, b"FMPT: 7")
            + subfield(2001, b"230/1")
            + subfield(2002, b"230/2")
        )
    base = one(
        tmp_path,
        fields=fields,
        body=(controls if placement in ("body", "both") else b"") + b"Hello",
    )
    path = Path(str(base) + ".JHR")
    raw = bytearray(path.read_bytes())
    raw[12:16] = (1).to_bytes(4, "little")
    path.write_bytes(raw)
    with JamWriter().open(base) as session:
        original = session.read(1)
        before_controls = original.message.control_lines
        assert before_controls is not None
        changed = session.update(
            original.identity, MessagePatch(control_lines=()), original.revision
        )
        cleared = session.read(1).message.control_lines
        assert cleared is not None
        assert not any(c.name == "PID" for c in cleared.kludges)
        assert cleared.msgid == "2:230/1 old"
        assert (
            cleared.seen_by == before_controls.seen_by
            and cleared.path == before_controls.path
        )
        assert any(c.name == "FMPT" for c in cleared.kludges)
        changed = session.update(
            changed.identity,
            MessagePatch(control_lines=(ControlLine(name="PID", value="new", raw=""),)),
            changed.revision,
        )
        updated = session.read(1).message.control_lines
        assert updated is not None
        assert [c.value for c in updated.kludges if c.name == "PID"] == ["new"]
        changed = session.update(
            changed.identity, MessagePatch(external_id="2:230/1 new"), changed.revision
        )
        assert session.read(1).message.external_id == "2:230/1 new"
        changed = session.update(
            changed.identity,
            MessagePatch(external_id=None, routing_seen_by=(), routing_path=()),
            changed.revision,
        )
        final = session.read(1).message.control_lines
        assert (
            final is not None
            and final.msgid is None
            and not final.seen_by
            and not final.path
        )


def test_inline_controls_survive_body_patch_and_conflicting_msgid_is_rejected(
    tmp_path: Path,
) -> None:
    base = one(tmp_path, body=b"\x01MSGID: old\r\x01X-UNKNOWN: keep\rHello")
    path = Path(str(base) + ".JHR")
    raw = bytearray(path.read_bytes())
    raw[12:16] = (1).to_bytes(4, "little")
    path.write_bytes(raw)
    with JamWriter().open(base) as session:
        original = session.read(1)
        changed = session.update(
            original.identity, MessagePatch(body_text="Edited"), original.revision
        )
        assert session.read(1).message.external_id == "old"
        assert "\x01X-UNKNOWN: keep" in session.read(1).message.body_text
        before = path.read_bytes()
        with pytest.raises(ValueError, match="MSGID"):
            session.update(
                changed.identity,
                MessagePatch(
                    control_lines=(ControlLine(name="MSGID", value="other", raw=""),)
                ),
                changed.revision,
            )
        assert path.read_bytes() == before


def test_public_control_replacement_removes_inline_controls(tmp_path: Path) -> None:
    writer = JamWriter()
    base = tmp_path / "base"
    writer.create(base)
    with writer.open(base) as session:
        original = session.append(message("\x01PID: old\rHello"))
        changed = session.update(
            original.identity, MessagePatch(control_lines=()), original.revision
        )
        current = session.read(1).message
        assert current.external_id == "2:1/2 id"
        assert current.control_lines is not None
        assert not any(c.name == "PID" for c in current.control_lines.kludges)
        before = snapshot(base)
        with pytest.raises(ValueError, match="FMPT"):
            session.update(
                changed.identity,
                MessagePatch(
                    control_lines=(ControlLine(name="FMPT", value="99", raw=""),)
                ),
                changed.revision,
            )
        assert snapshot(base) == before
