"""Recoverable JAM writer: held lock, prepare/apply/settle, and TZUTC dates."""

from __future__ import annotations

import os
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from golded_ftn import (
    CommitState,
    ControlLine,
    LockTimeoutError,
    MessagePatch,
    OutgoingMessage,
    RecoveryError,
    WriterOptions,
)
from test_writer import message, snapshot

from golded_ftn_jam import JamReader, JamWriter


def _area(tmp_path: Path) -> tuple[Path, Path]:
    area_dir = tmp_path / "area_dir"
    area_dir.mkdir()
    return area_dir / "area", tmp_path / "recovery"


def test_prepare_append_apply_settle_committed_updates_jhr_jdt_jdx(
    tmp_path: Path,
) -> None:
    base, recovery_root = _area(tmp_path)
    writer = JamWriter()
    writer.create(base)
    Path(str(base) + ".JLR").write_bytes(b"lastread-must-stay")
    before = snapshot(base)
    tx = uuid.uuid4()
    with writer.recovery_session(base, recovery_root=recovery_root) as session:
        assert session.physical_base.format == "jam"
        jhr = Path(os.path.realpath(str(base) + ".JHR"))
        assert session.physical_base.canonical_key == str(jhr)
        st = jhr.stat()
        assert session.physical_base.lock_inode == (st.st_dev, st.st_ino)
        session.initialize_recovery_root(has_ready_journal_rows=False)
        assert stat.S_IMODE(recovery_root.stat().st_mode) == 0o700
        mutation = session.prepare_append(tx, message("hello"))
        ready = recovery_root / f"{tx}.ready.json"
        assert ready.is_file()
        assert stat.S_IMODE(ready.stat().st_mode) == 0o600
        result = session.apply(mutation)
        session.settle(mutation, CommitState.COMMITTED)
        saved = session.read(result.identity.msgno)
        assert saved.message.body_text == "hello"
        assert session.pending() == ()
    after = snapshot(base)
    assert after[0] != before[0]
    assert after[1] == before[1] + b"hello"
    assert len(after[2]) == 8
    assert after[3] == b"lastread-must-stay"
    with writer.open(base) as crud:
        assert crud.read(1).message.body_text == "hello"


def test_settle_uncommitted_restores_pre_image_and_keeps_lock(
    tmp_path: Path,
) -> None:
    base, recovery_root = _area(tmp_path)
    writer = JamWriter()
    writer.create(base)
    Path(str(base) + ".JLR").write_bytes(b"lastread-must-stay")
    before = snapshot(base)
    with writer.recovery_session(base, recovery_root=recovery_root) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        mutation = session.prepare_append(uuid.uuid4(), message("temp"))
        session.apply(mutation)
        assert snapshot(base)[:3] != before[:3]
        session.settle(mutation, CommitState.UNCOMMITTED)
        assert snapshot(base)[:3] == before[:3]
        assert snapshot(base)[3] == b"lastread-must-stay"
        assert session.pending() == ()
        with pytest.raises(LockTimeoutError):
            with writer.open(base, WriterOptions(lock_timeout=0.05)) as other:
                other.append(message())
    assert snapshot(base)[3] == b"lastread-must-stay"
    with writer.open(base) as crud:
        assert crud.append(message()).identity.msgno == 1


def test_recover_uncommitted_torn_header(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    writer = JamWriter()
    writer.create(base)
    Path(str(base) + ".JLR").write_bytes(b"lastread-must-stay")
    before = snapshot(base)
    tx = uuid.uuid4()
    with writer.recovery_session(base, recovery_root=recovery_root) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        mutation = session.prepare_append(tx, message("torn"))
        session.apply(mutation)
    jhr = Path(str(base) + ".JHR")
    applied = jhr.read_bytes()
    assert len(applied) > len(before[0])
    jhr.write_bytes(applied[: len(before[0]) + 16])
    jdx = Path(str(base) + ".JDX")
    jdx.write_bytes(jdx.read_bytes()[:4])
    with writer.recovery_session(base, recovery_root=recovery_root) as session:
        refs = session.pending()
        assert len(refs) == 1
        assert refs[0].transaction_id == tx
        assertion = session.issue_offline_assertion()
        recovered = session.recover(assertion, lambda _: CommitState.UNCOMMITTED)
        assert [item.transaction_id for item in recovered] == [tx]
        assert session.pending() == ()
    assert snapshot(base)[:3] == before[:3]
    assert snapshot(base)[3] == b"lastread-must-stay"
    with writer.open(base) as crud:
        assert crud.append(message()).identity.msgno == 1


def test_jlr_unchanged_after_append_and_recover(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    writer = JamWriter()
    writer.create(base)
    marker = b"lastread-must-stay"
    Path(str(base) + ".JLR").write_bytes(marker)
    tx = uuid.uuid4()
    with writer.recovery_session(base, recovery_root=recovery_root) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        mutation = session.prepare_append(tx, message("keep-jlr"))
        session.apply(mutation)
    assert Path(str(base) + ".JLR").read_bytes() == marker
    with writer.recovery_session(base, recovery_root=recovery_root) as session:
        assertion = session.issue_offline_assertion()
        session.recover(assertion, lambda _: CommitState.UNCOMMITTED)
    assert Path(str(base) + ".JLR").read_bytes() == marker


def test_recovery_session_accepts_concurrent(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    writer = JamWriter()
    writer.create(base)
    with writer.recovery_session(
        base,
        recovery_root=recovery_root,
        options=WriterOptions(concurrent=True),
    ) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        assert session.pending() == ()


def test_aware_tzutc_round_trip_and_naive_policy(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    writer = JamWriter()
    writer.create(base)
    aware = datetime(2024, 3, 15, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    naive = datetime(2024, 3, 15, 12, 0, 0)
    tzutc = (ControlLine(name="TZUTC", value="+0200", raw=""),)
    with writer.open(base) as session:
        first = session.append(
            OutgoingMessage(
                from_name="Alice",
                to_name="Bob",
                subject="Aware",
                body_text="aware",
                posted_at=aware,
                control_lines=tzutc,
            )
        )
        saved = session.read(first.identity.msgno)
        assert saved.message.posted_at == datetime(2024, 3, 15, 12, 0, 0)
        assert saved.message.posted_at is not None
        assert saved.message.posted_at.tzinfo is None
        assert any(
            control.name == "TZUTC" and control.value == "+0200"
            for control in (
                saved.message.control_lines.kludges
                if saved.message.control_lines
                else ()
            )
        )
        second = session.append(
            OutgoingMessage(
                from_name="Alice",
                to_name="Bob",
                subject="Naive",
                body_text="naive",
                posted_at=naive,
            )
        )
        naive_saved = session.read(second.identity.msgno)
        assert naive_saved.message.posted_at == naive
        assert naive_saved.message.posted_at is not None
        assert naive_saved.message.posted_at.tzinfo is None
    standalone = list(JamReader().read(base))
    assert standalone[0].posted_at == datetime(2024, 3, 15, 10, 0, 0)
    assert standalone[1].posted_at == naive
    with writer.recovery_session(base, recovery_root=recovery_root) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        recovered = session.read(1)
        assert recovered.message.posted_at == datetime(2024, 3, 15, 12, 0, 0)


def test_tzutc_only_update_preserves_wall_time(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    writer = JamWriter()
    writer.create(base)
    posted = datetime(2024, 3, 15, 12, 0, 0)
    original = (ControlLine(name="TZUTC", value="+0200", raw=""),)
    updated = (ControlLine(name="TZUTC", value="+0100", raw=""),)
    with writer.open(base) as session:
        result = session.append(
            OutgoingMessage(
                from_name="Alice",
                to_name="Bob",
                subject="When",
                body_text="body",
                posted_at=posted,
                control_lines=original,
            )
        )
        saved = session.read(result.identity.msgno)
        assert saved.message.posted_at == posted
        session.update(
            result.identity,
            MessagePatch(control_lines=updated),
            saved.revision,
        )
        after = session.read(result.identity.msgno)
        assert after.message.posted_at == posted
        assert after.message.control_lines is not None
        assert any(
            control.name == "TZUTC" and control.value == "+0100"
            for control in after.message.control_lines.kludges
        )
    with writer.recovery_session(base, recovery_root=recovery_root) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        current = session.read(1)
        assert current.message.posted_at == posted
        mutation = session.prepare_update(
            uuid.uuid4(),
            current.identity,
            MessagePatch(
                control_lines=(ControlLine(name="TZUTC", value="-0500", raw=""),)
            ),
            current.revision,
        )
        session.apply(mutation)
        session.settle(mutation, CommitState.COMMITTED)
        recovered = session.read(1)
        assert recovered.message.posted_at == posted
        assert recovered.message.control_lines is not None
        assert any(
            control.name == "TZUTC" and control.value == "-0500"
            for control in recovered.message.control_lines.kludges
        )


def test_aware_posted_at_without_tzutc_or_mismatch_is_rejected(tmp_path: Path) -> None:
    base, recovery_root = _area(tmp_path)
    writer = JamWriter()
    writer.create(base)
    aware = datetime(2024, 3, 15, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    missing = OutgoingMessage(
        from_name="Alice",
        to_name="Bob",
        subject="Aware",
        body_text="aware",
        posted_at=aware,
    )
    mismatch = OutgoingMessage(
        from_name="Alice",
        to_name="Bob",
        subject="Aware",
        body_text="aware",
        posted_at=aware,
        control_lines=(ControlLine(name="TZUTC", value="-0500", raw=""),),
    )
    with writer.open(base) as session:
        with pytest.raises(
            ValueError, match="Aware posted_at requires a TZUTC control"
        ):
            session.append(missing)
        with pytest.raises(
            ValueError, match="Aware posted_at offset does not match TZUTC"
        ):
            session.append(mismatch)
    with writer.recovery_session(base, recovery_root=recovery_root) as session:
        session.initialize_recovery_root(has_ready_journal_rows=False)
        with pytest.raises(
            ValueError, match="Aware posted_at requires a TZUTC control"
        ):
            session.prepare_append(uuid.uuid4(), missing)
        with pytest.raises(
            ValueError, match="Aware posted_at offset does not match TZUTC"
        ):
            session.prepare_append(uuid.uuid4(), mismatch)


@pytest.mark.parametrize("suffix", (".JDT", ".JDX"))
def test_same_dir_symlink_data_file_is_unsafe(tmp_path: Path, suffix: str) -> None:
    base, recovery_root = _area(tmp_path)
    writer = JamWriter()
    writer.create(base)
    data_path = Path(str(base) + suffix)
    payload = data_path.read_bytes()
    data_path.unlink()
    target = base.parent / f"other{suffix}"
    target.write_bytes(payload)
    data_path.symlink_to(target.name)
    with pytest.raises(RecoveryError) as raised:
        with writer.recovery_session(base, recovery_root=recovery_root):
            pass
    assert raised.value.code == "unsafe_recovery_path"


def test_missing_data_file_error_omits_sentinel_path(tmp_path: Path) -> None:
    sentinel = "SENTINEL_LEAK_fjam_9f3a"
    area_dir = tmp_path / sentinel
    area_dir.mkdir()
    base = area_dir / "area"
    recovery_root = tmp_path / "recovery"
    writer = JamWriter()
    writer.create(base)
    Path(str(base) + ".JDT").unlink()
    with pytest.raises(RecoveryError) as raised:
        with writer.recovery_session(base, recovery_root=recovery_root):
            pass
    text = f"{raised.value!s}{raised.value!r}"
    assert sentinel not in text
    assert raised.value.code == "unsafe_recovery_path"
