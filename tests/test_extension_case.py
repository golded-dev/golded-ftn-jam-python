"""Open the sidecar casing that is already on disk; create uses configured case."""

from __future__ import annotations

from pathlib import Path

import pytest
from golded_ftn import OutgoingMessage

from golded_ftn_jam import JamReader, JamWriter, JamWriterOptions


def _message() -> OutgoingMessage:
    return OutgoingMessage(
        from_name="Alice",
        to_name="All",
        subject="case",
        body_text="hello\r",
    )


def test_create_default_writes_uppercase(tmp_path: Path) -> None:
    base = tmp_path / "area"
    JamWriter().create(base)
    assert (tmp_path / "area.JHR").is_file()
    assert not (tmp_path / "area.jhr").exists() or (tmp_path / "area.jhr").samefile(
        tmp_path / "area.JHR"
    )


def test_create_lower_writes_lowercase_and_open_finds_it(tmp_path: Path) -> None:
    base = tmp_path / "area"
    JamWriter().create(base, JamWriterOptions(extension_case="lower"))
    assert (tmp_path / "area.jhr").is_file()
    names = {path.name for path in tmp_path.iterdir() if path.is_file()}
    assert "area.JHR" not in names or (tmp_path / "area.JHR").samefile(
        tmp_path / "area.jhr"
    )
    with JamWriter().open(base) as session:
        session.append(_message())
    subjects = [message.subject for message in JamReader().read(base)]
    assert subjects == ["case"]


def test_open_existing_uppercase_when_create_default_is_unused(tmp_path: Path) -> None:
    base = tmp_path / "area"
    JamWriter().create(base, JamWriterOptions(extension_case="upper"))
    with JamWriter().open(base) as session:
        session.append(_message())
    assert list(JamReader().read(base))[0].subject == "case"


def test_create_rejects_other_casing_as_already_there(tmp_path: Path) -> None:
    base = tmp_path / "area"
    JamWriter().create(base, JamWriterOptions(extension_case="lower"))
    with pytest.raises(FileExistsError):
        JamWriter().create(base, JamWriterOptions(extension_case="upper"))
